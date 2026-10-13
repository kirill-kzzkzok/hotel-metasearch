"""Regression: rate-task accounting rows exist before terminal outcomes.

Previously hotel_room_rate_fetch rows appeared only when a task reached a
terminal status, so an interrupted pool (client disconnect, server restart,
Playwright context crash) silently lost every unfinished task: 150 discovered
Trip hotels with only 114 rows and no way to answer "expected vs completed".
Scheduled tasks now persist a non-terminal "queued" row before browser work;
the terminal persist replaces it instead of adding a duplicate row.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from aviasales_collector.hotels.schema import init_hotel_store
from aviasales_collector.hotels.persistence import persist_tripcom_room_rates
from aviasales_collector.hotels.models import utc_now


def _store_with_session(tmp_path):
    database = tmp_path / "rate-accounting.sqlite3"
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
            ("search-1", "trip_com", "123", "Hotel A", "[]", "[]", "{}", now),
        )
    return database, query


def test_queued_accounting_row_is_written_for_scheduled_tasks(tmp_path):
    from aviasales_collector.hotels.rate_enrichment import _persist_rate_task_lifecycle

    database, query = _store_with_session(tmp_path)
    tasks = [
        {"source_hotel_id": "123", "check_in": query["check_in"],
         "check_out": query["check_out"], "adults": 2, "children_count": 0},
        {"source_hotel_id": "456", "check_in": query["check_in"],
         "check_out": query["check_out"], "adults": 2, "children_count": 0,
         "operation": "tripcom_detail_rate_inventory", "attempt": 2, "retry_count": 1},
    ]

    _persist_rate_task_lifecycle(database, "search-1", tasks, query, "queued")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT source_hotel_id,status,operation,attempt,retry_count FROM hotel_room_rate_fetch "
            "WHERE search_id='search-1' ORDER BY source_hotel_id",
        ).fetchall()
    assert rows == [
        ("123", "queued", "tripcom_detail_rate_inventory", 1, 0),
        ("456", "queued", "tripcom_detail_rate_inventory", 2, 1),
    ]


def test_terminal_persist_replaces_queued_row_without_duplicate(tmp_path):
    from aviasales_collector.hotels.rate_enrichment import _persist_rate_task_lifecycle

    database, query = _store_with_session(tmp_path)
    tasks = [{"source_hotel_id": "123", "check_in": query["check_in"],
              "check_out": query["check_out"], "adults": 2, "children_count": 0}]
    _persist_rate_task_lifecycle(database, "search-1", tasks, query, "queued")

    inventory = {"source_hotel_id": "123", "check_in": query["check_in"],
                 "check_out": query["check_out"], "adults": 2, "children_count": 0,
                 "rates": []}
    persist_tripcom_room_rates(database, "search-1", inventory, status="no_rates")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT status FROM hotel_room_rate_fetch WHERE search_id='search-1'",
        ).fetchall()
    assert rows == [("no_rates",)]


def test_terminal_persist_keeps_row_when_no_queued_row_existed(tmp_path):
    database, query = _store_with_session(tmp_path)
    inventory = {"source_hotel_id": "123", "check_in": query["check_in"],
                 "check_out": query["check_out"], "adults": 2, "children_count": 0,
                 "rates": []}
    persist_tripcom_room_rates(database, "search-1", inventory, status="ok",
                               observed_at="2026-10-13T00:00:00+00:00")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT status FROM hotel_room_rate_fetch WHERE search_id='search-1'",
        ).fetchall()
    assert rows == [("ok",)]


def test_queued_is_not_terminal_for_rescheduling(tmp_path):
    """The scheduler treats a latest queued row as unfinished work."""
    from aviasales_collector.hotels.rate_enrichment import (
        _persist_rate_task_lifecycle,
    )

    database, query = _store_with_session(tmp_path)
    tasks = [{"source_hotel_id": "123", "check_in": query["check_in"],
              "check_out": query["check_out"], "adults": 2, "children_count": 0}]
    _persist_rate_task_lifecycle(database, "search-1", tasks, query, "queued")

    terminal_statuses = {
        "ok", "blocked", "no_rates", "rate_inventory_absent", "rate_inventory_timeout", "body_timeout",
        "navigation_error", "invalid_response", "http_error", "error", "not_started",
    }
    with sqlite3.connect(database) as connection:
        latest = connection.execute(
            "SELECT status FROM hotel_room_rate_fetch WHERE search_id='search-1' "
            "ORDER BY observed_at DESC,id DESC LIMIT 1",
        ).fetchone()[0]
    assert latest not in terminal_statuses
