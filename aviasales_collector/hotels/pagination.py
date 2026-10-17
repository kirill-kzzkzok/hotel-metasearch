"""Sravni cursor pagination use case with bounded retries and completion state."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cache import _search_with_cache
from .geo.enrichment import _enrich_search_coordinates
from .models import HotelSearchOperationError, SourceResult, utc_now
from .persistence import (_finish_search_session_if_ready, _persist_page,
                          _stop_sravni_pagination)
from .pipeline import _finalize_search, _reconcile_match_state
from .read_model import _read_search, _sravni_collection_audit
from .runtime import (_profiled_sqlite_connect, _safe_json_loads, log,
                      log_search_timeline)
from .sources.sravni import SRAVNIOTEL_SOURCE

SRAVNI_DISCOVERY_MAX_PAGES = 60
# The measured production-like run spent 165.0 s in 36 cursor-page calls
# (P50 3.8 s, P95 15.3 s) and still had five source pages left.  Keep the
# bounded application guard, with enough headroom for the observed tail and
# the initial page, so a healthy cursor is not classified as a timeout. The
# full concurrent run measured 265 s for 29 pages while a 244-key Geo stream
# was active; 600 s remains a finite guard and stays below the 60-page/400-card
# application caps.
SRAVNI_DISCOVERY_BUDGET_SECONDS = 600
SRAVNI_DISCOVERY_MAX_HOTELS = 400


def _search_identity_map(connection: sqlite3.Connection, search_id: str) -> dict[tuple[str, str], str | None]:
    rows = connection.execute(
        "SELECT o.source,o.source_hotel_id,i.canonical_hotel_id "
        "FROM hotel_observation o LEFT JOIN hotel_source_identity i "
        "ON i.source=o.source AND i.source_hotel_id=o.source_hotel_id "
        "WHERE o.search_id=?",
        (search_id,),
    )
    return {
        (str(row["source"]), str(row["source_hotel_id"])):
            str(row["canonical_hotel_id"]) if row["canonical_hotel_id"] is not None else None
        for row in rows
    }


def _select_delta_hotels(hotels: list[dict[str, Any]], affected_keys: set[str]) -> list[dict[str, Any]]:
    return [
        hotel for hotel in hotels
        if any(
            f"{record.get('source')}:{record.get('source_hotel_id')}" in affected_keys
            for record in (hotel.get("source_records") or [hotel])
        )
    ]


def _persist_and_finalize_sravni_page(
    database_path: str | Path,
    search_id: str,
    accepted_raw_hotels: list[dict[str, Any]],
    page_raw: dict[str, Any],
    token: str,
    page_number: int,
    query: dict[str, Any],
    observed_at: str | None,
    source_result: SourceResult,
    page_source_ids: set[str],
    identities_before: dict[tuple[str, str], str | None],
    requested_mode: str,
    delta: bool,
) -> dict[str, Any]:
    """Persist and build one cursor-page response outside the async event loop."""
    persist_started = time.perf_counter()
    _persist_page(database_path, search_id, accepted_raw_hotels, page_raw,
                  token, "sravniotel", query, observed_at)
    persist_ms = (time.perf_counter() - persist_started) * 1000
    log_search_timeline(search_id, "sravni_page_db_persisted", page=page_number,
                        accepted_hotels=len(accepted_raw_hotels), elapsed_ms=round(persist_ms, 1))
    audit_started = time.perf_counter()
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        persisted_count = connection.execute(
            "SELECT COUNT(DISTINCT source_hotel_id) FROM hotel_observation "
            "WHERE search_id=? AND source='sravniotel'", (search_id,),
        ).fetchone()[0]
        next_token = connection.execute(
            "SELECT next_page_token FROM hotel_search_session WHERE id=?", (search_id,),
        ).fetchone()[0]
    if persisted_count >= SRAVNI_DISCOVERY_MAX_HOTELS and next_token:
        _stop_sravni_pagination(database_path, search_id, "application_hotel_limit")
        _finish_search_session_if_ready(database_path, search_id)
        log.info("[SRAVNI] discovery stopped normally search_id=%s reason=application_hotel_limit "
                 "unique=%d pages=%d", search_id, persisted_count, page_number)
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        audit = _sravni_collection_audit(connection, search_id)
        collection_complete = bool(audit.get("collection_complete"))
        collection_reason = audit.get("collection_reason")
        source_status = "partial" if collection_reason in {
            "repeated_next_page_token", "application_page_limit", "application_time_budget",
            "application_hotel_limit",
        } else "ok"
        connection.execute(
            "UPDATE hotel_source_run SET latency_ms=COALESCE(latency_ms,0)+?,http_status=?,status=?,"
            "error_class=NULL,cache_state=?,reported_total=?,collection_complete=?,collection_reason=?,observed_at=? "
            "WHERE search_id=? AND source='sravniotel'",
            (source_result.latency_ms, source_result.http_status, source_status,
             source_result.cache_state, source_result.reported_total or source_result.total_hotels,
             int(collection_complete), collection_reason,
             source_result.observed_at or utc_now(), search_id),
        )
        connection.commit()
    log_search_timeline(search_id, "sravni_page_audited", page=page_number,
                        audit_and_status_ms=round((time.perf_counter() - audit_started) * 1000, 1),
                        persisted_hotels=persisted_count)
    if source_status == "partial" or not next_token:
        _finish_search_session_if_ready(database_path, search_id)
    log.info("[SRAVNI] page persisted search_id=%s page=%d status=%s source_latency_ms=%s delta=%s",
             search_id, page_number, source_status, source_result.latency_ms, delta)
    if delta:
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            terminal_page = connection.execute(
                "SELECT next_page_token IS NULL FROM hotel_search_session WHERE id=?", (search_id,),
            ).fetchone()
        if terminal_page and terminal_page[0]:
            # Emit one complete, correctly audited result at natural termination.
            # Every preceding cursor response is a scoped delta.
            read_started = time.perf_counter()
            finalized = _read_search(database_path, search_id)
            read_ms = (time.perf_counter() - read_started) * 1000
            if finalized is None:
                return {"search_id": search_id, "hotels": [], "has_more": False}
            finalized["query"]["room_mode"] = requested_mode
            finalize_started = time.perf_counter()
            finalized = _finalize_search(database_path, finalized)
            finalize_ms = (time.perf_counter() - finalize_started) * 1000
            log_search_timeline(
                search_id, "sravni_page_result_built", page=page_number, delta=False,
                terminal_delta_page=True, read_model_ms=round(read_ms, 1),
                matching_finalization_ms=round(finalize_ms, 1), delta_filter_ms=0.0,
                returned_hotels=len(finalized.get("hotels", [])),
                total_offers=finalized.get("search_audit", {}).get("offers"),
            )
            return finalized

        read_started = time.perf_counter()
        matching_result = _read_search(database_path, search_id, matching_only=True)
        read_ms = (time.perf_counter() - read_started) * 1000
        if matching_result is None:
            return {"search_id": search_id, "hotels": [], "has_more": True}
        matching_result["query"]["room_mode"] = requested_mode
        match_started = time.perf_counter()
        match_state = _reconcile_match_state(database_path, matching_result)
        match_ms = (time.perf_counter() - match_started) * 1000

        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            identities_after = _search_identity_map(connection, search_id)
        affected = {("sravniotel", hotel_id) for hotel_id in page_source_ids}
        affected.update(
            identity for identity, canonical_id in identities_after.items()
            if identities_before.get(identity) != canonical_id
        )
        affected_canonical_ids = {
            identities_after[identity] for identity in affected
            if identity in identities_after and identities_after[identity]
        }
        affected.update(
            identity for identity, canonical_id in identities_after.items()
            if canonical_id in affected_canonical_ids
        )
        affected_keys = {f"{source}:{source_hotel_id}" for source, source_hotel_id in affected}
        if not affected:
            log_search_timeline(
                search_id, "sravni_page_result_built", page=page_number, delta=True,
                empty_delta=True, read_model_ms=round(read_ms, 1),
                matching_finalization_ms=round(match_ms, 1), delta_filter_ms=0.0,
                returned_hotels=0, total_offers=0, matching_reconcile_ms=round(match_ms, 1),
            )
            return {
                "search_id": search_id, "hotels": [], "has_more": True,
                "delta_source_identity_keys": [], "search_audit_scope": "delta",
            }
        matching_candidates = {
            (str(hotel.get("source")), str(hotel.get("source_hotel_id"))): hotel.get("match_candidates", [])
            for hotel in matching_result.get("hotels", [])
        }
        detail_started = time.perf_counter()
        finalized = _read_search(database_path, search_id, source_identity_keys=affected)
        detail_ms = (time.perf_counter() - detail_started) * 1000
        if finalized is None:
            return {"search_id": search_id, "hotels": [], "has_more": True}
        finalized["query"]["room_mode"] = requested_mode
        for hotel in finalized.get("hotels", []):
            key = (str(hotel.get("source")), str(hotel.get("source_hotel_id")))
            hotel["match_candidates"] = matching_candidates.get(key, [])
        finalized["delta_source_identity_keys"] = sorted(affected_keys)
        finalized["search_audit_scope"] = "delta"
        finalize_started = time.perf_counter()
        finalized = _finalize_search(database_path, finalized, match_state=match_state)
        finalize_ms = (time.perf_counter() - finalize_started) * 1000
        log_search_timeline(
            search_id, "sravni_delta_matching_reconciled", page=page_number,
            lightweight_read_ms=round(read_ms, 1), matching_reconcile_ms=round(match_ms, 1),
            affected_identities=len(affected), matching_cards=len(matching_result.get("hotels", [])),
        )
        log_search_timeline(
            search_id, "sravni_page_result_built", page=page_number, delta=True,
            read_model_ms=round(read_ms + detail_ms, 1), matching_finalization_ms=round(finalize_ms, 1),
            delta_filter_ms=0.0, returned_hotels=len(finalized.get("hotels", [])),
            total_offers=finalized.get("search_audit", {}).get("offers"),
            lightweight_read_ms=round(read_ms, 1), detail_read_ms=round(detail_ms, 1),
            matching_reconcile_ms=round(match_ms, 1),
        )
        return finalized

    read_started = time.perf_counter()
    finalized = _read_search(database_path, search_id)
    read_ms = (time.perf_counter() - read_started) * 1000
    if finalized is None:
        return {"search_id": search_id, "hotels": [], "has_more": False}
    finalized["query"]["room_mode"] = requested_mode
    finalize_started = time.perf_counter()
    finalized = _finalize_search(database_path, finalized)
    finalize_ms = (time.perf_counter() - finalize_started) * 1000
    log_search_timeline(search_id, "sravni_page_result_built", page=page_number,
                        delta=False, read_model_ms=round(read_ms, 1),
                        matching_finalization_ms=round(finalize_ms, 1),
                        delta_filter_ms=0.0,
                        returned_hotels=len(finalized.get("hotels", [])),
                        total_offers=finalized.get("search_audit", {}).get("offers"))
    return finalized


async def load_more_sravni_page(
    database_path: str | Path, search_id: str, body: dict[str, Any], *, delta: bool = False,
) -> dict[str, Any]:
    request_started = time.perf_counter()
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        session = connection.execute("SELECT * FROM hotel_search_session WHERE id=?", (search_id,)).fetchone()
        if session is None:
            raise HotelSearchOperationError("not_found", "Hotel search not found")
        token = session["next_page_token"]
        started_at = session["started_at"]
        source_run = connection.execute(
            "SELECT pages FROM hotel_source_run WHERE search_id=? AND source='sravniotel'",
            (search_id,),
        ).fetchone()
        pages_so_far = int(source_run[0] or 0) if source_run else 0
        sravni_ids = {
            str(row[0]) for row in connection.execute(
                "SELECT DISTINCT source_hotel_id FROM hotel_observation "
                "WHERE search_id=? AND source='sravniotel'",
                (search_id,),
            )
        }
        identities_before = _search_identity_map(connection, search_id) if delta else {}
        query = _safe_json_loads(session["query_json"], {})
        if not isinstance(query, dict):
            query = {}
        requested_mode = body.get("room_mode", query.get("room_mode", "all"))
        if requested_mode not in {"private", "all"}:
            raise HotelSearchOperationError("invalid_payload", "Unsupported room mode")
        query["room_mode"] = requested_mode
        stop_reason = None
        elapsed_seconds = 0.0
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        except (TypeError, ValueError):
            pass
        if token and len(sravni_ids) >= SRAVNI_DISCOVERY_MAX_HOTELS:
            stop_reason = "application_hotel_limit"
        elif token and pages_so_far >= SRAVNI_DISCOVERY_MAX_PAGES:
            stop_reason = "application_page_limit"
        elif token and elapsed_seconds >= SRAVNI_DISCOVERY_BUDGET_SECONDS:
            stop_reason = "application_time_budget"
        if stop_reason:
            _stop_sravni_pagination(database_path, search_id, stop_reason)
            _finish_search_session_if_ready(database_path, search_id)
            log.warning("[SRAVNI] discovery stopped by application limit search_id=%s reason=%s pages=%d elapsed=%.1fs",
                        search_id, stop_reason, pages_so_far, elapsed_seconds)
            result = _read_search(database_path, search_id)
            result["query"]["room_mode"] = requested_mode
            return _finalize_search(database_path, result)
        if not token:
            # Pagination exhausted: return the already-persisted search.
            # A full geo re-enrichment here added minutes of Overpass
            # deadline waits on a degraded evening (measured 192s on the
            # 400-card search) while adding nothing - the coordinate
            # stream already enriches cards incrementally.
            result = _read_search(database_path, search_id)
            result["query"]["room_mode"] = requested_mode
            return _finalize_search(database_path, result)
        page_number = pages_so_far + 1
    log_search_timeline(search_id, "sravni_page_started", page=page_number,
                        delta=delta, elapsed_since_previous_ms=round((time.perf_counter() - request_started) * 1000, 1))
    source_started = time.perf_counter()
    result = await _search_with_cache(database_path, SRAVNIOTEL_SOURCE, query, token)
    log_search_timeline(search_id, "sravni_page_received", page=page_number, status=result.status,
                        cache=result.cache_state, source_latency_ms=result.latency_ms,
                        request_elapsed_ms=round((time.perf_counter() - source_started) * 1000, 1),
                        normalized_hotels=len(result.hotels), offers=sum(len(h.get("offers", [])) for h in result.hotels))
    if result.status != "ok" or result.raw_page is None:
        first_error = result
        retryable = result.status == "timeout" or (
            result.status == "http_error" and result.http_status is not None
            and result.http_status >= 500
        )
        if retryable:
            # A single short retry can recover a transient page failure
            # without making discovery spin on a broken provider cursor.
            await asyncio.sleep(0.25)
            retry = await _search_with_cache(database_path, SRAVNIOTEL_SOURCE, query, token)
            retry_latency = (first_error.latency_ms or 0) + (retry.latency_ms or 0)
            result = SourceResult(**{**retry.__dict__, "latency_ms": retry_latency})
    if result.status != "ok" or result.raw_page is None:
        failure_status = result.status if result.status in {
            "timeout", "blocked", "http_error", "invalid_response", "response_too_large",
        } else "error"
        failure_reason = f"source_page_{failure_status}"
        _stop_sravni_pagination(
            database_path, search_id, failure_reason,
            error_class=result.error_class or failure_status, http_status=result.http_status,
        )
        _finish_search_session_if_ready(database_path, search_id)
        log.warning("[SRAVNI] pagination failed search_id=%s page=%d status=%s error_class=%s "
                    "http_status=%s attempts=%d",
                    search_id, pages_so_far + 1, result.status,
                    result.error_class or failure_status, result.http_status,
                    2 if retryable else 1)
        failed_result = _read_search(database_path, search_id)
        failed_result["query"]["room_mode"] = requested_mode
        return _finalize_search(database_path, failed_result)

    # Count normalized hotel IDs already persisted, then accept only enough
    # new IDs from this provider page to reach the application cap. Repeated
    # IDs remain harmless and do not consume the remaining slots.
    page_ids = []
    page_seen: set[str] = set()
    for hotel in result.hotels:
        hotel_id = str(hotel.get("source_hotel_id") or "")
        if hotel_id and hotel_id not in sravni_ids and hotel_id not in page_seen:
            page_ids.append(hotel_id)
            page_seen.add(hotel_id)
    page_source_ids = {
        str(hotel.get("source_hotel_id")) for hotel in result.hotels
        if hotel.get("source_hotel_id") is not None
    }
    remaining = max(0, SRAVNI_DISCOVERY_MAX_HOTELS - len(sravni_ids))
    accepted_ids = sravni_ids | set(page_ids[:remaining])
    page_raw = dict(result.raw_page)
    raw_hotels = page_raw.get("hotels") if isinstance(page_raw.get("hotels"), list) else []
    accepted_raw_hotels = [
        hotel for hotel in raw_hotels
        if isinstance(hotel, dict) and hotel.get("id") is not None
        and str(hotel["id"]) in accepted_ids
    ]
    if len(page_ids) > remaining:
        page_raw["hotels"] = accepted_raw_hotels
        page_raw["_applicationDiscardedCardCount"] = len(raw_hotels) - len(accepted_raw_hotels)
    else:
        accepted_raw_hotels = raw_hotels
    return await asyncio.to_thread(
        _persist_and_finalize_sravni_page,
        database_path, search_id, accepted_raw_hotels, page_raw, token,
        pages_so_far + 1, query, result.observed_at, result, page_source_ids, identities_before,
        requested_mode, delta,
    )
