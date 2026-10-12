from __future__ import annotations

import asyncio

from aviasales_collector.hotels import service
from aviasales_collector.hotels.models import SourceResult


def test_initial_sravni_timeout_retries_once_and_keeps_cumulative_latency(monkeypatch):
    attempts = []
    first = SourceResult("sravniotel", "timeout", latency_ms=12_000)
    recovered = SourceResult(
        "sravniotel", "ok", hotels=({"source_hotel_id": "hotel-1"},),
        raw_page={"hotels": [{"id": "hotel-1"}]}, http_status=200, latency_ms=2_500,
    )

    async def fake_search_with_cache(database_path, source, query):
        attempts.append((database_path, source.source, query))
        return first if len(attempts) == 1 else recovered

    monkeypatch.setattr(service, "_search_with_cache", fake_search_with_cache)
    monkeypatch.setattr(service, "SRAVNI_INITIAL_RETRY_DELAY_SECONDS", 0)

    result = asyncio.run(service._search_sravni_initial_with_recovery("snapshot.sqlite3", {"geo_id": "1"}))

    assert len(attempts) == 2
    assert result.status == "ok"
    assert result.http_status == 200
    assert result.hotels == recovered.hotels
    assert result.raw_page == recovered.raw_page
    assert result.latency_ms == 14_500


def test_initial_sravni_blocked_response_is_not_retried(monkeypatch):
    attempts = []
    blocked = SourceResult("sravniotel", "blocked", http_status=403, latency_ms=300)

    async def fake_search_with_cache(database_path, source, query):
        attempts.append(source.source)
        return blocked

    monkeypatch.setattr(service, "_search_with_cache", fake_search_with_cache)

    result = asyncio.run(service._search_sravni_initial_with_recovery("snapshot.sqlite3", {"geo_id": "1"}))

    assert len(attempts) == 1
    assert result.status == "blocked"
    assert result.http_status == 403
