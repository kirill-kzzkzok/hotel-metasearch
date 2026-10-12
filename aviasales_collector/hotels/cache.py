"""Per-source search cache and request coalescing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import weakref
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import SourceResult, utc_now
from .normalization import normalize_hotel
from .runtime import _profiled_sqlite_connect, _safe_json_loads
from .sources.base import HotelSource
from .sources.tripcom import TRIPCOM_COLLECTION_CACHE_VERSION

CACHE_TTL_SECONDS = {"sravniotel": 300, "trip_com": 60}

CACHE_NONCOMPLETE_TTL_SECONDS = {"sravniotel": 30, "trip_com": 20}

SOURCE_CACHE_LOCKS: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()

SOURCE_CACHE_LOCKS_GUARD = threading.Lock()

def _source_cache_key(source: HotelSource, query: dict[str, Any], page_token: str | None) -> str:
    material = {"source": source.source, "query": query, "page_token": page_token}
    if source.source == "trip_com":
        # Invalidate old results where the Sravni page size was incorrectly
        # reused as Trip.com's card cap and recorded as an upstream partial.
        material["collector_version"] = TRIPCOM_COLLECTION_CACHE_VERSION
    payload = json.dumps(material,
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

async def _search_with_cache(database_path: str | Path, source: HotelSource,
                             query: dict[str, Any], page_token: str | None = None) -> SourceResult:
    loop = asyncio.get_running_loop()
    cache_key = _source_cache_key(source, query, page_token)
    with SOURCE_CACHE_LOCKS_GUARD:
        locks = SOURCE_CACHE_LOCKS.setdefault(loop, {})
        lock = locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        return await _search_with_cache_unlocked(database_path, source, query, page_token)

async def _search_with_cache_unlocked(database_path: str | Path, source: HotelSource,
                                      query: dict[str, Any], page_token: str | None = None) -> SourceResult:
    ttl = CACHE_TTL_SECONDS.get(source.source, 0)
    cache_key = _source_cache_key(source, query, page_token)
    now = datetime.now(timezone.utc)
    if ttl:
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            cached = connection.execute(
                "SELECT * FROM hotel_source_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
        cache_expired = True
        if cached:
            try:
                cache_expired = datetime.fromisoformat(cached["expires_at"]) <= now
            except (TypeError, ValueError):
                cache_expired = True
        if cached and not cache_expired:
            cached_payload = _safe_json_loads(cached["raw_json"], None)
            if not isinstance(cached_payload, dict) or not isinstance(cached_payload.get("hotels"), list):
                # A syntactically broken or structurally incomplete cache
                # entry is a miss. Returning it as an empty successful search
                # would hide provider data and poison the cache until expiry.
                cache_expired = True
        if cached and not cache_expired:
            raw_page = _safe_json_loads(cached["raw_json"], {})
            if not isinstance(raw_page, dict):
                raw_page = {}
            try:
                cached_observed_at = datetime.fromisoformat(cached["observed_at"])
                cached_observed_text = cached["observed_at"]
            except (TypeError, ValueError):
                cached_observed_at = now
                cached_observed_text = utc_now()
            hotels = tuple(item for raw in raw_page.get("hotels", [])
                           if isinstance(raw, dict)
                           if (item := normalize_hotel(raw, cached_observed_text,
                                                       source=source.source, query=query)) is not None)
            age = max(0, int((now - cached_observed_at).total_seconds()))
            cached_status = raw_page.get("_cacheStatus")
            if cached_status not in {"ok", "partial", "no_results"}:
                cached_status = "partial" if raw_page.get("collectionComplete") is False else "ok"
            return SourceResult(source.source, cached_status, hotels, raw_page,
                                raw_page.get("totalHotels"), raw_page.get("nextPageToken"),
                                cached["http_status"], 0, cache_state=f"hit:{age}s",
                                observed_at=cached_observed_text,
                                reported_total=(raw_page.get("sourceReportedTotal")
                                                if source.source == "trip_com"
                                                else raw_page.get("sourceReportedTotal", raw_page.get("totalHotels"))),
                                collection_complete=raw_page.get("collectionComplete"),
                                collection_reason=raw_page.get("collectionReason"),
                                collection_audit=raw_page.get("collectionAudit"))
        if cached:
            with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
                connection.execute("DELETE FROM hotel_source_cache WHERE cache_key=?", (cache_key,))
                connection.commit()

    result = await source.search(query, page_token)
    observed_at = result.observed_at or utc_now()
    cacheable_status = result.status in {"ok", "partial", "no_results"}
    if ttl and cacheable_status and result.raw_page is not None:
        # Keep the cache payload self-describing so a partial result can never
        # be presented as a complete provider response on a warm read.
        cache_payload = dict(result.raw_page)
        cache_payload["_cacheStatus"] = result.status
        effective_ttl = ttl if result.status == "ok" else CACHE_NONCOMPLETE_TTL_SECONDS.get(source.source, 20)
        expires_at = datetime.fromisoformat(observed_at) + timedelta(seconds=effective_ttl)
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            connection.execute(
                """INSERT INTO hotel_source_cache
                (cache_key,source,raw_json,http_status,latency_ms,observed_at,expires_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
                raw_json=excluded.raw_json,http_status=excluded.http_status,latency_ms=excluded.latency_ms,
                observed_at=excluded.observed_at,expires_at=excluded.expires_at""",
                (cache_key, source.source, json.dumps(cache_payload, ensure_ascii=False),
                 result.http_status, result.latency_ms or 0, observed_at, expires_at.isoformat()),
            )
            connection.commit()
    return SourceResult(**{**result.__dict__,
                          "cache_state": result.cache_state if result.cache_state != "miss" else "miss",
                          "observed_at": observed_at,
                          "reported_total": (result.reported_total or result.total_hotels)
                          if source.source == "sravniotel" else result.reported_total})
