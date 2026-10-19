"""B03 regression: a finished rate response may not leave orphaned queued rows.

The fast-path crash branch answered with response-only `not_started` stubs
while the `queued` accounting rows written before the pool stayed queued
forever - progress and response disagreed, and reloads rescheduled work
already reported as finished. The fix terminalizes those queued rows
(status=not_started, error_type=rate_pool_aborted) when the response is
padded, so scheduled == terminal after the request completes.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.rate_enrichment import _finalize_aborted_rate_tasks


def _seed_search(tmp_path, search_id: str, hotel_id: str) -> None:
    init_hotel_store(tmp_path)
    with closing(sqlite3.connect(tmp_path)) as db:
        db.execute(
            "INSERT INTO hotel_search_session (id, query_json, status, started_at) VALUES (?,?,?,?)",
            (search_id, json.dumps({
                "geo_id": "1", "city_name": "Test", "check_in": "2026-11-20",
                "check_out": "2026-11-23", "adults": 2, "children_count": 0, "limit": 20,
            }), "running", "2026-10-19T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO hotel_observation (search_id, source, source_hotel_id, name, images_json, "
            "nearby_places_json, raw_json, observed_at) VALUES (?,?,?,?,?,?,?,?)",
            (search_id, "trip_com", hotel_id, "Hotel Test", "[]", "[]", "{}", "2026-10-19T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO hotel_room_rate_fetch "
            "(search_id,source,source_hotel_id,check_in,check_out,adults,children_count,status,"
            " http_status,latency_ms,rates_seen,observed_at,operation,attempt,retry_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (search_id, "trip_com", hotel_id, "2026-11-20", "2026-11-23", 2, 0, "queued",
             None, None, 0, "2026-10-19T00:00:00Z", "tripcom_detail_rate_inventory", 1, 0),
        )
        db.commit()


TASKS = [{"source_hotel_id": "555", "check_in": "2026-11-20",
          "check_out": "2026-11-23", "adults": 2, "children_count": 0, "attempt": 1, "retry_count": 0}]
QUERY = {"check_in": "2026-11-20", "check_out": "2026-11-23",
         "adults": 2, "children_count": 0}


def test_aborted_pool_terminalizes_queued_rows(tmp_path):
    db = tmp_path / "b03.sqlite3"
    _seed_search(db, "search-1", "555")
    _finalize_aborted_rate_tasks(db, "search-1", TASKS, QUERY)
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT status, error_type FROM hotel_room_rate_fetch WHERE search_id='search-1'"
        ).fetchall()
    assert rows == [("not_started", "rate_pool_aborted")]


def test_aborted_pool_leaves_terminal_rows_untouched(tmp_path):
    db = tmp_path / "b03b.sqlite3"
    _seed_search(db, "search-1", "555")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO hotel_room_rate_fetch "
            "(search_id,source,source_hotel_id,check_in,check_out,adults,children_count,status,"
            " http_status,latency_ms,rates_seen,observed_at,operation,attempt,retry_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("search-1", "trip_com", "556", "2026-11-20", "2026-11-23", 2, 0, "ok",
             200, 800, 5, "2026-10-19T00:00:00Z", "tripcom_detail_rate_inventory", 1, 0),
        )
        conn.commit()
    # 556 was never part of the aborted batch: untouched.
    _finalize_aborted_rate_tasks(db, "search-1", TASKS, QUERY)
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT source_hotel_id, status FROM hotel_room_rate_fetch "
            "WHERE search_id='search-1' ORDER BY source_hotel_id"
        ).fetchall()
    assert rows == [("555", "not_started"), ("556", "ok")]


def test_aborted_pool_with_no_tasks_is_a_noop(tmp_path):
    db = tmp_path / "b03c.sqlite3"
    init_hotel_store(db)
    _finalize_aborted_rate_tasks(db, "search-1", [], QUERY)  # must not raise


def test_padding_branch_is_wired_into_the_response_path():
    """The response-padding site must call the finalizer (source contract)."""
    import inspect

    from aviasales_collector.hotels import rate_enrichment

    source = inspect.getsource(rate_enrichment.enrich_tripcom_room_rates)
    assert "_finalize_aborted_rate_tasks" in source, (
        "response padding must terminalize the queued rows it reports as not_started"
    )
