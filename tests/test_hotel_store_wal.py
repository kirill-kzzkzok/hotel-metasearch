"""Regression: the hotel store enables WAL so reads and writes stay concurrent.

With the default rollback journal a single persistence transaction (rate
results, coordinate batches) blocks every concurrent reader — the browser's
progress polls fail with "database is locked". Measured on a live snapshot:
3 lock errors from 3 readers during one write; with WAL: 0 errors, writes
9ms instead of 59ms. init_hotel_store applies PRAGMA journal_mode=WAL once
per on-disk identity, idempotently.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from aviasales_collector.hotels.schema import init_hotel_store


def test_init_hotel_store_enables_wal(tmp_path):
    database = tmp_path / "wal-store.sqlite3"
    init_hotel_store(database)
    with sqlite3.connect(database) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_wal_readers_are_not_blocked_by_a_write_transaction(tmp_path):
    database = tmp_path / "wal-concurrent.sqlite3"
    init_hotel_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) "
            "VALUES ('s1','{}','2026-10-13T00:00:00+00:00','running')",
        )

    stop = threading.Event()
    errors: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as c:
                    c.execute("SELECT count(*) FROM hotel_search_session").fetchone()
            except sqlite3.OperationalError as exc:
                errors.append(str(exc))

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        with sqlite3.connect(database, timeout=5) as writer:
            writer.execute("BEGIN IMMEDIATE")
            for index in range(200):
                writer.execute(
                    "UPDATE hotel_search_session SET status = 'running' WHERE id = 's1'",
                )
                time.sleep(0.002)
            writer.rollback()
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    assert not errors, f"concurrent readers hit lock errors under WAL: {errors[:3]}"
