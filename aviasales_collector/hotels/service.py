"""Hotel search session and concurrent source orchestration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .cache import _search_with_cache
from .models import SourceResult, utc_now
from .persistence import (_finish_search_session_if_ready,
                          _persist_search_source_result)
from .pipeline import _finalize_search
from .read_model import _read_search
from .runtime import (BACKGROUND_SEARCH_TASKS, _profiled_sqlite_connect,
                      log, log_search_timeline, start_search_timeline)
from .sources.base import HotelSource
from .sources.sravni import SRAVNIOTEL_SOURCE
from .sources.tripcom import TRIP_COM_SOURCE

SRAVNI_INITIAL_RETRY_DELAY_SECONDS = 0.25


def _hotels_containing_source_ids(hotels: list[dict[str, Any]], source: str,
                                 source_hotel_ids: set[str]) -> list[dict[str, Any]]:
    """Pick canonical cards touched by a newly persisted source page."""
    if not source_hotel_ids:
        return []
    selected = []
    for hotel in hotels:
        records = hotel.get("source_records") or [hotel]
        if any(record.get("source") == source
               and str(record.get("source_hotel_id")) in source_hotel_ids for record in records):
            selected.append(hotel)
    return selected


async def _search_sravni_initial_with_recovery(
    database_path: str | Path, query: dict[str, Any],
) -> SourceResult:
    """Retry one transient initial-page timeout/5xx, matching cursor recovery."""
    result = await _search_with_cache(database_path, SRAVNIOTEL_SOURCE, query)
    retryable = result.status == "timeout" or (
        result.status == "http_error" and result.http_status is not None
        and result.http_status >= 500
    )
    if not retryable:
        return result

    log.warning(
        "[SRAVNI] initial page transient failure; retrying once status=%s http_status=%s",
        result.status, result.http_status,
    )
    await asyncio.sleep(SRAVNI_INITIAL_RETRY_DELAY_SECONDS)
    retry = await _search_with_cache(database_path, SRAVNIOTEL_SOURCE, query)
    total_latency = (result.latency_ms or 0) + (retry.latency_ms or 0)
    return SourceResult(**{**retry.__dict__, "latency_ms": total_latency})

async def _run_trip_search_in_background(database_path: str | Path, search_id: str,
                                          query: dict[str, Any], source: HotelSource) -> None:
    started = time.perf_counter()
    log_search_timeline(search_id, "trip_discovery_started")
    try:
        try:
            result = await _search_with_cache(database_path, source, query)
        except Exception as exc:
            log.exception("[TRIP_ERROR] stage=source_search error_class=%s", type(exc).__name__)
            result = SourceResult(source.source, "error", error_class=type(exc).__name__, reason=str(exc)[:300])
        log_search_timeline(search_id, "trip_discovery_received", status=result.status,
                            hotels=result.total_hotels, cache=result.cache_state,
                            source_latency_ms=result.latency_ms,
                            elapsed_ms=int((time.perf_counter() - started) * 1000))
        persist_started = time.perf_counter()
        result = _persist_search_source_result(database_path, search_id, source, query, result)
        _finish_search_session_if_ready(database_path, search_id)
        log_search_timeline(search_id, "trip_discovery_persisted", status=result.status,
                            hotels=result.total_hotels,
                            persist_ms=int((time.perf_counter() - persist_started) * 1000))
        log.info("[TRIP_RESULTS] stage=persisted search_id=%s status=%s hotels=%s elapsed_ms=%d",
                 search_id, result.status, result.total_hotels,
                 int((time.perf_counter() - started) * 1000))
    except Exception as exc:
        log.exception("[TRIP_ERROR] stage=persist error_class=%s search_id=%s", type(exc).__name__, search_id)
        try:
            failed = SourceResult(source.source, "error", error_class=f"persist_{type(exc).__name__}",
                                  reason=str(exc)[:300])
            _persist_search_source_result(database_path, search_id, source, query, failed)
            _finish_search_session_if_ready(database_path, search_id)
        except Exception:
            log.exception("[TRIP_ERROR] stage=persist_status search_id=%s", search_id)


async def start_hotel_search(database_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    search_id = str(uuid.uuid4())
    start_search_timeline(search_id)
    log_search_timeline(search_id, "search_started", city=query.get("city_slug") or query.get("city_name"),
                        check_in=query.get("check_in"), check_out=query.get("check_out"))
    now = utc_now()
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            (search_id, json.dumps(query, ensure_ascii=False), now, "running"),
        )
        for source in (SRAVNIOTEL_SOURCE, TRIP_COM_SOURCE):
            connection.execute(
                "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
                (search_id, source.source, "running", now),
            )
        connection.commit()
    log_search_timeline(search_id, "trip_started")
    trip_task = asyncio.create_task(
        _run_trip_search_in_background(database_path, search_id, dict(query), TRIP_COM_SOURCE),
        name=f"trip-search-{search_id}",
    )
    BACKGROUND_SEARCH_TASKS.add(trip_task)
    trip_task.add_done_callback(BACKGROUND_SEARCH_TASKS.discard)
    log_search_timeline(search_id, "sravni_started")
    sravni_started = time.perf_counter()
    try:
        sravni_result = await _search_sravni_initial_with_recovery(database_path, query)
    except Exception as exc:
        log.exception("hotel source search failed: source=sravniotel")
        sravni_result = SourceResult("sravniotel", "error", error_class=type(exc).__name__,
                                     reason=str(exc)[:300])
    log_search_timeline(search_id, "sravni_initial_page_received", status=sravni_result.status,
                        hotels=sravni_result.total_hotels, cache=sravni_result.cache_state,
                        source_latency_ms=sravni_result.latency_ms,
                        elapsed_ms=int((time.perf_counter() - sravni_started) * 1000))
    persist_started = time.perf_counter()
    _persist_search_source_result(database_path, search_id, SRAVNIOTEL_SOURCE, query, sravni_result)
    log_search_timeline(search_id, "sravni_initial_page_persisted",
                        elapsed_ms=int((time.perf_counter() - persist_started) * 1000))
    # Give a cache hit or an already-completed fast provider coroutine one
    # event-loop turn to persist. Real browser work remains asynchronous.
    await asyncio.sleep(0)
    _finish_search_session_if_ready(database_path, search_id)
    read_started = time.perf_counter()
    result = _read_search(database_path, search_id)
    log_search_timeline(search_id, "initial_read_model_built",
                        elapsed_ms=int((time.perf_counter() - read_started) * 1000),
                        hotels=len(result.get("hotels", [])))
    result["sources"] = [
        {**source,
         "reason": (sravni_result.reason if source["source"] == "sravniotel"
                    else source.get("collection_reason"))}
        for source in result["sources"]
    ]
    finalize_started = time.perf_counter()
    result = _finalize_search(database_path, result)
    log_search_timeline(search_id, "initial_matching_finalized",
                        elapsed_ms=int((time.perf_counter() - finalize_started) * 1000),
                        hotels=len(result.get("hotels", [])), offers=result.get("search_audit", {}).get("offers"))
    return result
