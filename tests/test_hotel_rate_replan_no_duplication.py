"""B03-live regression: replanning must not accumulate queued rows.

Live Phase C evidence (searches 94860388/9118cb3d, 2026-10-19): every
retry of /room-rates inserted a fresh `queued` accounting row per
pending hotel while the previous queued rows stayed - 584/441 queued
rows for 149/150 tasks after a few retries (3 identical queued rows
per hotel, same attempt=1/retry_count=0). The replan must replace the
stale queued rows of the same booking context, not append duplicates.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from aviasales_collector.hotels.schema import init_hotel_store
from aviasales_collector.hotels.rate_enrichment import _persist_rate_task_lifecycle


def _seed(tmp_path):
    database = tmp_path / "replan.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    query = {"check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("search-1", json.dumps(query), now, "running"),
        )
    return database, query


def _tasks(query, ids):
    return [{"source_hotel_id": str(i), "check_in": query["check_in"],
             "check_out": query["check_out"], "adults": query["adults"],
             "children_count": query["children_count"]} for i in ids]


def test_replanned_queued_rows_replace_not_accumulate(tmp_path):
    database, query = _seed(tmp_path)
    # First cycle: both hotels queued.
    _persist_rate_task_lifecycle(database, "search-1", _tasks(query, [1, 2]), query, "queued")
    # Second cycle replans the same two hotels (e.g. after a blocked pool).
    _persist_rate_task_lifecycle(database, "search-1", _tasks(query, [1, 2]), query, "queued")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT source_hotel_id, COUNT(*) FROM hotel_room_rate_fetch "
            "WHERE search_id='search-1' GROUP BY source_hotel_id ORDER BY source_hotel_id",
        ).fetchall()
    assert rows == [("1", 1), ("2", 1)], (
        f"replanned queued rows accumulated: {rows}"
    )


def test_terminal_rows_survive_the_next_replan(tmp_path):
    database, query = _seed(tmp_path)
    _persist_rate_task_lifecycle(database, "search-1", _tasks(query, [1]), query, "queued")
    # A terminal outcome replaced the queued row (persist path).
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE hotel_room_rate_fetch SET status='blocked' WHERE search_id='search-1'"
        )
        connection.commit()
    # The next cycle replans a DIFFERENT hotel; the blocked history row stays.
    _persist_rate_task_lifecycle(database, "search-1", _tasks(query, [2]), query, "queued")
    with sqlite3.connect(database) as connection:
        statuses = dict(connection.execute(
            "SELECT source_hotel_id || ':' || status, COUNT(*) FROM hotel_room_rate_fetch "
            "WHERE search_id='search-1' GROUP BY source_hotel_id, status",
        ).fetchall())
    assert statuses == {"1:blocked": 1, "2:queued": 1}
