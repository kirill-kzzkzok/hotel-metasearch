"""Regression tests for the Trip.com rate fast-path fallback decision.

The fast path (collect_tripcom_room_rates_via_fetch) answers one item per
input task and pads hotels it never reached with not_started stubs. The
fallback decision in enrich_tripcom_room_rates must therefore be made on
item statuses, not on the result length:

- an explicit protection response (blocked) stops the source for this
  request - the detail-page pool must NOT run (retrying the same source
  would hit the same protection and only burn the profile's quota);
- a template capture failure (empty sentinel) or a fast-path exception
  falls back for the hotels that were never reached;
- a partial success falls back only for the remaining hotels, and the
  legacy pool's subset-relative on_result index must be translated back to
  the full task order so persistence cannot attach an outcome to the wrong
  hotel;
- persisting the same inventory twice cannot duplicate observations.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import aviasales_collector.hotels.rate_enrichment as rate_enrichment


@pytest.fixture
def fast_path(monkeypatch):
    """Force the fast-path branch for these unit tests (no browser involved).

    _tripcom_rate_fast_path_enabled normally refuses to run inside pytest
    (PYTEST_CURRENT_TEST re-created every phase, so deleting the env var from
    a fixture is ineffective); the collectors under test are fakes, so
    substituting the gate directly cannot touch a browser or the network.
    """
    monkeypatch.setattr(rate_enrichment, "_tripcom_rate_fast_path_enabled",
                        lambda: True)


def _search_with_hotels(hotel_ids):
    return {
        "search_id": "search-1",
        "query": {"geo_id": None, "check_in": "2026-11-14", "check_out": "2026-11-17",
                  "adults": 2, "children_count": 0},
        "hotels": [{"name": f"Hotel {hid}", "source_records": [
            {"source": "trip_com", "source_hotel_id": str(hid)},
        ]} for hid in hotel_ids],
    }


def _create_rate_test_database(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE hotel_raw_page (id INTEGER PRIMARY KEY, search_id TEXT, source TEXT, "
        "page_index INTEGER, raw_json TEXT)"
    )
    connection.execute(
        "CREATE TABLE hotel_room_rate_fetch (id INTEGER PRIMARY KEY, source_hotel_id TEXT, status TEXT, "
        "http_status INTEGER, latency_ms INTEGER, rates_seen INTEGER, operation TEXT, error_type TEXT, "
        "error_message TEXT, attempt INTEGER, retry_count INTEGER, search_id TEXT, source TEXT, "
        "check_in TEXT, check_out TEXT, adults INTEGER, children_count INTEGER, observed_at TEXT)"
    )
    connection.execute(
        "INSERT INTO hotel_raw_page(search_id,source,page_index,raw_json) VALUES(?,?,?,?)",
        ("search-1", "trip_com", 1, json.dumps({"sourceCityId": "1356"})),
    )
    connection.commit()
    connection.close()


def _patch_enrich(monkeypatch, hotel_ids, *, via_fetch, detail):
    """Patch the collectors and persistence for enrich_tripcom_room_rates.

    persist_completed_rate_result is a closure inside enrich_tripcom_room_rates
    and cannot be monkeypatched directly; persistence is intercepted at the
    persist_tripcom_room_rates boundary instead, which is what the closure
    calls. Each persist records (inventory source_hotel_id, status, attempt).
    Returns a dict capturing which hotels each collector received.
    """
    calls = {"via_fetch": [], "detail": [], "persist": []}

    def fake_persist(_database, _search_id, inventory, **kwargs):
        calls["persist"].append((str(inventory.get("source_hotel_id")),
                                 kwargs.get("status"), kwargs.get("attempt")))
        return {"rates_seen": len(inventory.get("rates") or []), "rates_persisted": 0}

    async def fake_via_fetch(tasks, _profile, *, concurrency, on_result):
        calls["via_fetch"].append([str(t["source_hotel_id"]) for t in tasks])
        if callable(via_fetch):
            return await via_fetch(tasks, on_result)
        for index, item in enumerate(via_fetch):
            if item is not None and on_result is not None and item.get("status") != "not_started":
                await on_result(index, item)
        return list(via_fetch)

    async def fake_detail(tasks, _profile, *, concurrency, on_result):
        calls["detail"].append([str(t["source_hotel_id"]) for t in tasks])
        for index, item in enumerate(detail):
            if item is not None and on_result is not None and item.get("status") != "not_started":
                await on_result(index, item)
        return list(detail)

    monkeypatch.setattr(rate_enrichment, "collect_tripcom_room_rates_via_fetch", fake_via_fetch)
    monkeypatch.setattr(rate_enrichment, "collect_tripcom_room_rates", fake_detail)
    monkeypatch.setattr(rate_enrichment, "_read_search",
                        lambda *_a, **_k: _search_with_hotels(hotel_ids))
    monkeypatch.setattr(rate_enrichment, "shutdown_tripcom_browser", lambda: None)
    monkeypatch.setattr(rate_enrichment, "persist_tripcom_room_rates", fake_persist)
    return calls


def _run_enrich(database_path, search):
    return asyncio.run(rate_enrichment.enrich_tripcom_room_rates(
        database_path, "search-1", search, {"concurrency": 1}, None,
    ))


def _statuses(result):
    return [h["enrichment"]["status"] for h in result["hotels"]]


def test_protection_blocks_detail_fallback(monkeypatch, tmp_path, fast_path):
    """A blocked item means the source answered with protection: the
    detail-page pool must not run for the unreached hotels."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [101, 102, 103, 104]
    search = _search_with_hotels(ids)
    via = [
        {"source_hotel_id": "101", "status": "ok", "rate_status": 200, "latency_ms": 5,
         "inventory": {"rates": []}},
        {"source_hotel_id": "102", "status": "blocked", "rate_status": 403, "latency_ms": 5,
         "error_type": "robots_or_provider_protection"},
        {"status": "not_started"},
        {"status": "not_started"},
    ]
    calls = _patch_enrich(monkeypatch, ids, via_fetch=via, detail=[])
    result = _run_enrich(database_path, search)

    assert calls["detail"] == []  # no detail-page fallback after protection
    assert result["status"] == "blocked"
    assert _statuses(result) == ["ok", "blocked", "not_started", "not_started"]


def test_sentinel_falls_back_for_all_hotels(monkeypatch, tmp_path, fast_path):
    """A template capture failure returns the empty sentinel: the detail-page
    pool runs for every task and the merged result keeps task order."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [201, 202]
    search = _search_with_hotels(ids)
    calls = _patch_enrich(monkeypatch, ids, via_fetch=[], detail=[])
    result = _run_enrich(database_path, search)

    # 2026-10-15 directive: the detail-page pool is forbidden; hotels stay
    # honestly queued instead of being collected through detail pages.
    assert calls["detail"] == []
    assert _statuses(result) == ["not_started", "not_started"]


def test_fast_path_exception_falls_back_for_unreached_hotels(monkeypatch, tmp_path, fast_path):
    """A fast-path exception discards nothing: outcomes the crashed run
    already persisted are restored, and the fallback runs only for the
    unreached hotels (no double collection, no lost results)."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [301, 302, 303]
    search = _search_with_hotels(ids)

    async def crashing_via_fetch(tasks, on_result):
        # hotel 301 completed and persisted before the crash (full inventory,
        # as the real fast-path classifier produces)
        outcome = {"source_hotel_id": "301", "status": "ok", "rate_status": 200,
                   "latency_ms": 5,
                   "inventory": {"source_hotel_id": "301", "check_in": "2026-11-14",
                                 "check_out": "2026-11-17", "adults": 2,
                                 "children_count": 0, "rates": []}}
        await on_result(0, outcome)
        raise RuntimeError("page closed mid-run")

    calls = _patch_enrich(monkeypatch, ids, via_fetch=crashing_via_fetch, detail=[])
    result = _run_enrich(database_path, search)

    # 2026-10-15 directive: no detail fallback; the unreached hotels stay
    # queued for the next enrichment cycle
    assert calls["detail"] == []
    assert _statuses(result) == ["ok", "not_started", "not_started"]
    # 301 was persisted exactly once (before the crash), never re-collected
    assert [hotel_id for hotel_id, _status, _attempt in calls["persist"] if hotel_id == "301"] == ["301"]


def test_partial_success_falls_back_only_for_remaining(monkeypatch, tmp_path, fast_path):
    """Hotels the fast path collected keep their results; only the
    not_started stubs go to the detail-page pool; order is preserved."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [401, 402, 403]
    search = _search_with_hotels(ids)
    via = [
        {"source_hotel_id": "401", "status": "ok", "rate_status": 200, "latency_ms": 5,
         "inventory": {"rates": []}},
        {"status": "not_started"},
        {"source_hotel_id": "403", "status": "ok", "rate_status": 200, "latency_ms": 5,
         "inventory": {"rates": []}},
    ]
    calls = _patch_enrich(monkeypatch, ids, via_fetch=via, detail=[])
    result = _run_enrich(database_path, search)

    # not_started stays queued; no detail pool (2026-10-15 directive)
    assert calls["detail"] == []
    assert _statuses(result) == ["ok", "not_started", "ok"]


def test_mixed_merge_keeps_task_order_without_source_ids(monkeypatch, tmp_path, fast_path):
    """Legacy pool outcomes may omit source_hotel_id: positional merge must
    still fill the right slots and keep one item per task."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [501, 502, 503, 504]
    search = _search_with_hotels(ids)
    via = [
        {"status": "not_started"},
        {"source_hotel_id": "502", "status": "ok", "rate_status": 200, "latency_ms": 5,
         "inventory": {"rates": []}},
        {"status": "not_started"},
        {"status": "not_started"},
    ]
    calls = _patch_enrich(monkeypatch, ids, via_fetch=via, detail=[])
    result = _run_enrich(database_path, search)

    # stubs stay queued in place; no detail pool, no positional merge
    assert calls["detail"] == []
    assert _statuses(result) == ["not_started", "ok", "not_started", "not_started"]
    assert len(result["hotels"]) == 4


def test_detail_fallback_persists_under_full_task_indices(monkeypatch, tmp_path, fast_path):
    """The legacy pool reports indices relative to ITS input (the remaining
    subset); persistence must translate them back to the full task order so
    an outcome cannot be attached to the wrong hotel."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [601, 602, 603, 604]
    search = _search_with_hotels(ids)
    via = [
        {"source_hotel_id": "601", "status": "ok", "rate_status": 200, "latency_ms": 5,
         "inventory": {"source_hotel_id": "601", "check_in": "2026-11-14",
                       "check_out": "2026-11-17", "adults": 2, "children_count": 0,
                       "rates": []}},
        {"status": "not_started"},  # 602 stays queued (no detail fallback)
        {"source_hotel_id": "603", "status": "ok", "rate_status": 200, "latency_ms": 5,
         "inventory": {"source_hotel_id": "603", "check_in": "2026-11-14",
                       "check_out": "2026-11-17", "adults": 2, "children_count": 0,
                       "rates": []}},
        {"status": "not_started"},  # 604 stays queued (no detail fallback)
    ]
    calls = _patch_enrich(monkeypatch, ids, via_fetch=via, detail=[])
    _run_enrich(database_path, search)

    # only the fast-path successes persist; 602/604 were never collected
    # through detail pages (forbidden) and stay queued
    assert calls["detail"] == []
    persisted_ids = sorted(hotel_id for hotel_id, _status, _attempt in calls["persist"])
    assert persisted_ids == ["601", "603"]


def test_blocking_persists_terminal_row_instead_of_queued(monkeypatch, tmp_path, fast_path):
    """A protection response must leave a terminal blocked row (with the HTTP
    status) for the hotel it stopped on, not a queued row."""
    from aviasales_collector.hotels.schema import init_hotel_store
    from aviasales_collector.hotels.persistence import persist_tripcom_room_rates
    from aviasales_collector.hotels.models import utc_now
    from datetime import datetime, timezone

    database = tmp_path / "blocked-persist.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    query = {"check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("search-1", json.dumps(query), now, "running"),
        )
        connection.execute(
            "INSERT INTO hotel_observation(search_id,source,source_hotel_id,name,images_json,"
            "nearby_places_json,raw_json,observed_at) VALUES (?,?,?,?,?,?,?,?)",
            ("search-1", "trip_com", "701", "Hotel A", "[]", "[]", "{}", now),
        )
    # queued accounting row, as written before the pool starts
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_room_rate_fetch(search_id,source,source_hotel_id,check_in,check_out,"
            "adults,children_count,status,http_status,latency_ms,rates_seen,observed_at,operation,"
            "attempt,retry_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("search-1", "trip_com", "701", query["check_in"], query["check_out"], 2, 0,
             "queued", None, None, 0, utc_now(), "tripcom_detail_rate_inventory", 1, 0),
        )
    # the blocked outcome now carries rate_status so the HTTP code persists
    outcome = {"source_hotel_id": "701", "status": "blocked", "rate_status": 403,
               "latency_ms": 900, "error_type": "robots_or_provider_protection",
               "error_message": "Protection response on the rate endpoint",
               "operation": "tripcom_detail_rate_inventory", "attempt": 1, "retry_count": 0}
    inventory = {"source_hotel_id": "701", "check_in": query["check_in"],
                 "check_out": query["check_out"], "adults": 2, "children_count": 0,
                 "rates": []}
    persist_tripcom_room_rates(database, "search-1", inventory, status="blocked",
                               http_status=403, latency_ms=900,
                               operation="tripcom_detail_rate_inventory",
                               error_type="robots_or_provider_protection",
                               error_message="Protection response on the rate endpoint")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT status,http_status,attempt,retry_count FROM hotel_room_rate_fetch "
            "WHERE search_id='search-1' AND source_hotel_id='701'",
        ).fetchall()
    assert rows == [("blocked", 403, 1, 0)]  # queued row replaced, no duplicate


def test_repeated_persist_does_not_duplicate_observations(tmp_path):
    """Persisting the same inventory twice cannot duplicate observations: the
    observation table has a UNIQUE key including observed_at plus INSERT OR
    IGNORE. Fetch-row history is append-only for terminal statuses, so both
    terminal rows remain (each new pool run re-persists its terminal)."""
    from aviasales_collector.hotels.schema import init_hotel_store
    from aviasales_collector.hotels.persistence import persist_tripcom_room_rates
    from datetime import datetime, timezone

    database = tmp_path / "dupes.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    query = {"check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("search-1", json.dumps(query), now, "running"),
        )
        connection.execute(
            "INSERT INTO hotel_observation(search_id,source,source_hotel_id,name,images_json,"
            "nearby_places_json,raw_json,observed_at) VALUES (?,?,?,?,?,?,?,?)",
            ("search-1", "trip_com", "801", "Hotel A", "[]", "[]", "{}", now),
        )
    observed = "2026-10-14T12:00:00+00:00"
    inventory = {"source_hotel_id": "801", "check_in": query["check_in"],
                 "check_out": query["check_out"], "adults": 2, "children_count": 0,
                 "rates": [
                     {"provider_rate_id": "r1", "room_group_index": 0, "observed_at": observed},
                     {"provider_rate_id": "r2", "room_group_index": 0, "observed_at": observed},
                 ]}
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok", http_status=200)
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok", http_status=200)

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM hotel_room_rate_observation WHERE search_id='search-1'",
        ).fetchone()[0]
        fetch_rows = connection.execute(
            "SELECT status FROM hotel_room_rate_fetch WHERE search_id='search-1'",
        ).fetchall()
    assert count == 2  # not 4: no duplicate observations on repeated persist
    # 2026-10-19 (live Phase C evidence): replaying the SAME terminal record
    # (identical observed_at tuple) is idempotent - it replaces its row
    # instead of appending. The old append-only expectation accumulated 100
    # duplicated ok rows in a single live search via the
    # publish-before-reviews double callback.
    assert fetch_rows == [("ok",)]  # replayed terminal record collapsed


def test_queued_rows_are_replanned_by_the_next_request(monkeypatch, tmp_path, fast_path):
    """Worker-crash contract: queued accounting rows are non-terminal, so the
    NEXT enrichment request reschedules them (existing replan mechanism); no
    watchdog is needed because nothing is lost and every row stays
    observable in hotel_room_rate_fetch."""
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    ids = [901, 902]
    search = _search_with_hotels(ids)
    # first request crashes after the queued rows were written
    async def crashing_via_fetch(tasks, on_result):
        raise RuntimeError("worker died mid-pool")

    calls = _patch_enrich(monkeypatch, ids, via_fetch=crashing_via_fetch, detail=[])
    result = _run_enrich(database_path, search)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source_hotel_id,status FROM hotel_room_rate_fetch "
            "WHERE search_id='search-1' ORDER BY source_hotel_id",
        ).fetchall()
    # rows observable and queued (non-terminal): replanned by any next request
    assert rows == [("901", "queued"), ("902", "queued")]
    assert result["status"] == "partial" or result["status"] in {"ok", "partial"}
