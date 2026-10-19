"""B13-live regression: replayed persist of the same terminal record is idempotent.

Live Phase C evidence (search fceeee6d, Da Nang): the publish-before-reviews
flow calls on_result twice for an ok hotel (rate first, review-enriched item
second). persist_tripcom_room_rates inserted BOTH as separate history rows
(100 duplicate (hotel, attempt, observed_at) groups in one search), because
the replacement DELETE only matched status IN ('queued','running') while the
first row was already terminal 'ok'. The persisted outcome of the same
terminal record must not duplicate: re-persisting the same (search, hotel,
context, attempt, retry, observed_at) tuple replaces the row.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from aviasales_collector.hotels.schema import init_hotel_store
from aviasales_collector.hotels.persistence import persist_tripcom_room_rates


def _seed(tmp_path):
    database = tmp_path / "idempotent.sqlite3"
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
    return database, query


def test_replayed_terminal_persist_replaces_not_duplicates(tmp_path):
    database, query = _seed(tmp_path)
    observed = "2026-10-19T12:00:00+00:00"
    inventory = {"source_hotel_id": "801", "check_in": query["check_in"],
                 "check_out": query["check_out"], "adults": 2, "children_count": 0,
                 "rates": [{"provider_rate_id": "r1", "room_group_index": 0,
                            "observed_at": observed}]}
    # First callback: rate published (no review facts yet).
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok",
                               http_status=200, observed_at=observed,
                               attempt=1, retry_count=0)
    # Second callback: same terminal record, review facts merged (B13 flow).
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok",
                               http_status=200, observed_at=observed,
                               attempt=1, retry_count=0)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT status, observed_at FROM hotel_room_rate_fetch WHERE search_id='search-1'",
        ).fetchall()
    assert rows == [("ok", observed)], (
        f"replayed terminal persist duplicated history rows: {rows}"
    )


def test_distinct_terminal_records_still_append(tmp_path):
    database, query = _seed(tmp_path)
    inventory = {"source_hotel_id": "801", "check_in": query["check_in"],
                 "check_out": query["check_out"], "adults": 2, "children_count": 0,
                 "rates": []}
    # Genuinely different observations (different observed_at) remain
    # separate history rows - the append-only terminal history is preserved.
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok",
                               observed_at="2026-10-19T12:00:00+00:00")
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok",
                               observed_at="2026-10-19T13:00:00+00:00")
    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM hotel_room_rate_fetch WHERE search_id='search-1'",
        ).fetchone()[0]
    assert count == 2
