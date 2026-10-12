from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from http.client import RemoteDisconnected

import pytest

from aviasales_collector.hotels import pagination
from aviasales_collector.hotels.models import SourceError, SourceResult
from aviasales_collector.hotels.schema import init_hotel_store
from aviasales_collector.hotels.sources import sravni


def test_sravni_remote_disconnect_is_classified_as_retryable_timeout(monkeypatch):
    def disconnect(*_args, **_kwargs):
        raise RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(sravni, "_profiled_urlopen", disconnect)

    with pytest.raises(SourceError) as raised:
        sravni._request_json("https://example.invalid/api/public/hotels/search")

    assert raised.value.kind == "timeout"
    assert raised.value.http_status is None
    assert raised.value.upstream_timeout is False


def test_sravni_page_disconnect_retries_once_then_terminalizes_without_exception(
    tmp_path, monkeypatch,
):
    database = tmp_path / "sravni-disconnect.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    search_id = "sravni-disconnect-search"
    query = {
        "geo_id": "123",
        "city_name": "Dubai",
        "check_in": "2026-10-20",
        "check_out": "2026-10-25",
        "adults": 2,
        "children_count": 0,
        "room_mode": "all",
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status,next_page_token) "
            "VALUES (?,?,?,?,?)",
            (search_id, json.dumps(query), now, "running", "cursor-25"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,pages,observed_at) "
            "VALUES (?,?,?,?,?)",
            (search_id, "sravniotel", "ok", 24, now),
        )

    attempts = []

    async def disconnected_page(*_args, **_kwargs):
        attempts.append(True)
        return SourceResult(
            "sravniotel", "timeout", error_class="timeout",
            reason="Source request failed or timed out",
        )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(pagination, "_search_with_cache", disconnected_page)
    monkeypatch.setattr(pagination.asyncio, "sleep", no_delay)

    result = asyncio.run(pagination.load_more_sravni_page(
        database, search_id, {"room_mode": "all"}, delta=True,
    ))

    assert len(attempts) == 2
    assert result["has_more"] is False
    with sqlite3.connect(database) as connection:
        source = connection.execute(
            "SELECT status,collection_complete,collection_reason,error_class "
            "FROM hotel_source_run WHERE search_id=? AND source='sravniotel'",
            (search_id,),
        ).fetchone()
        session = connection.execute(
            "SELECT status,next_page_token FROM hotel_search_session WHERE id=?",
            (search_id,),
        ).fetchone()

    assert source == ("partial", 0, "source_page_timeout", "timeout")
    assert session == ("partial", None)
