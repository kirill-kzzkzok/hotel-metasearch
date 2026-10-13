"""СравниОтель search adapter and bounded HTTP response decoding."""

from __future__ import annotations

import asyncio
from http.client import HTTPException
import json
import time
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ..models import SourceError, SourceResult
from ..normalization import normalize_hotel
from ..runtime import EXECUTOR, MAX_RESPONSE_BYTES, _profiled_urlopen, log
from .base import HotelSource

SRAVNI_BASE = "https://hotels.travel.ru/api/v1"

CITY_AUTOCOMPLETE_URL = "https://autocomplete.travelpayouts.com/places2"

def _request_json(url: str, timeout: float = 12.0) -> tuple[dict[str, Any] | list[Any], int, int]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "HotelSearchMVP/0.1"})
    started = time.perf_counter()
    try:
        with _profiled_urlopen(request, timeout=timeout, stage="sravni_search") as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
    except HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        error_body = exc.read(4096).decode("utf-8", "replace")
        upstream_timeout = exc.code == 500 and "запрос превысил 10 с" in error_body.casefold()
        if upstream_timeout:
            raise SourceError("timeout", "Source HTTP 500: upstream search exceeded 10 seconds",
                              http_status=exc.code, latency_ms=elapsed_ms,
                              upstream_timeout=True) from exc
        if exc.code in (401, 403, 429):
            raise SourceError("blocked", f"Source returned HTTP {exc.code}",
                              http_status=exc.code, latency_ms=elapsed_ms) from exc
        raise SourceError("http_error", f"Source returned HTTP {exc.code}",
                          http_status=exc.code, latency_ms=elapsed_ms) from exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise SourceError("timeout", "Source request failed or timed out",
                          latency_ms=int((time.perf_counter() - started) * 1000)) from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("response_too_large", "Source response exceeded size limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SourceError("invalid_response", "Source returned invalid JSON") from exc
    if not isinstance(payload, (dict, list)):
        raise SourceError("invalid_response", "Source returned an unexpected JSON shape")
    return payload, status, int((time.perf_counter() - started) * 1000)

class SravniOtelSource:
    source = "sravniotel"
    auto_paginate = True

    async def suggest(self, query: str) -> tuple[list[dict[str, Any]], int, int]:
        url = f"{SRAVNI_BASE}/suggest?{urlencode({'query': query, 'limit': 8})}"
        payload, http_status, latency_ms = await asyncio.get_running_loop().run_in_executor(
            EXECUTOR, _request_json, url
        )
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        output = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("geo_id") is None:
                continue
            name = " ".join(str(item.get("name") or "").split())
            if not name:
                continue
            output.append({
                "geo_id": str(item["geo_id"]), "name": name,
                "country": item.get("country_name") or item.get("country"),
                "country_slug": item.get("country_slug"), "city_slug": item.get("city_slug"),
                "hotels_count": item.get("hotels_count"),
            })
            if len(output) == 8:
                break
        return output, http_status, latency_ms

    async def search(self, query: dict[str, Any], page_token: str | None = None) -> SourceResult:
        if not query.get("geo_id"):
            return SourceResult(self.source, "unavailable", error_class="city_not_resolved",
                                reason="SravniOtel has no confirmed hotel city ID for this destination")
        params = {key: query[key] for key in
                  ("geo_id", "check_in", "check_out", "adults", "children_count", "limit")}
        requested_limit = params["limit"]
        long_stay = False
        try:
            long_stay = (date.fromisoformat(query["check_out"]) - date.fromisoformat(query["check_in"])).days >= 10
        except (KeyError, TypeError, ValueError):
            pass
        if long_stay and type(requested_limit) is int and requested_limit > 5:
            params["limit"] = 5
        if page_token:
            params["page_token"] = page_token
        url = f"{SRAVNI_BASE}/search?{urlencode(params)}"
        try:
            raw, http_status, latency_ms = await asyncio.get_running_loop().run_in_executor(
                EXECUTOR, _request_json, url
            )
        except SourceError as exc:
            effective_limit = params["limit"]
            if not exc.upstream_timeout or type(effective_limit) is not int:
                return SourceResult(self.source, exc.kind, error_class=exc.kind, reason=str(exc),
                                    http_status=exc.http_status, latency_ms=exc.latency_ms)
            latency_ms = exc.latency_ms or 0
            last_error = exc
            fallback_limit = None
            raw = None
            while effective_limit > 1:
                fallback_limit = 5 if effective_limit > 5 else 1
                fallback_url = f"{SRAVNI_BASE}/search?{urlencode({**params, 'limit': fallback_limit})}"
                try:
                    raw, http_status, retry_latency_ms = await asyncio.get_running_loop().run_in_executor(
                        EXECUTOR, _request_json, fallback_url
                    )
                except SourceError as retry_exc:
                    latency_ms += retry_exc.latency_ms or 0
                    last_error = retry_exc
                    if not retry_exc.upstream_timeout:
                        return SourceResult(self.source, retry_exc.kind, error_class=retry_exc.kind,
                                            reason=str(retry_exc), http_status=retry_exc.http_status,
                                            latency_ms=latency_ms)
                    effective_limit = fallback_limit
                    continue
                latency_ms += retry_latency_ms
                break
            if raw is None:
                return SourceResult(self.source, last_error.kind, error_class=last_error.kind,
                                    reason=str(last_error), http_status=last_error.http_status,
                                    latency_ms=latency_ms)
            if isinstance(raw, dict):
                raw["_sourcePageLimit"] = fallback_limit
            fallback_reason = f"Upstream timed out at 10 seconds; retried with page size {fallback_limit}"
        else:
            fallback_reason = "Long stay uses page size 5 to stay within upstream processing limit" if long_stay else None
            if long_stay and isinstance(raw, dict) and params["limit"] != requested_limit:
                raw["_sourcePageLimit"] = params["limit"]
        if not isinstance(raw, dict) or not isinstance(raw.get("hotels"), list):
            return SourceResult(self.source, "invalid_response", error_class="invalid_response",
                                http_status=http_status, latency_ms=latency_ms,
                                reason="Search response had no hotels list")
        # The live /api/v1 contract renamed pagination fields to snake_case
        # (total_hotels, next_page_token). The downstream cache, persistence and
        # read-model still read the historical camelCase keys, so normalize once
        # here instead of touching every consumer.
        if "totalHotels" not in raw and isinstance(raw.get("total_hotels"), int):
            raw["totalHotels"] = raw["total_hotels"]
        if "nextPageToken" not in raw and raw.get("next_page_token") is not None:
            raw["nextPageToken"] = str(raw["next_page_token"])
        parse_started = time.perf_counter()
        hotels = tuple(item for raw_hotel in raw["hotels"]
                       if isinstance(raw_hotel, dict)
                       if (item := normalize_hotel(raw_hotel)) is not None)
        parse_ms = (time.perf_counter() - parse_started) * 1000
        log.info("[ENRICH] sravni_parse=%.1fms hotels=%d per_hotel=%.2fms",
                 parse_ms, len(hotels), parse_ms / max(1, len(hotels)))
        return SourceResult(self.source, "ok", hotels, raw, raw.get("totalHotels"),
                            raw.get("nextPageToken"), http_status, latency_ms,
                            reason=fallback_reason,
                            cache_state=(f"page-size:{raw['_sourcePageLimit']}"
                                         if fallback_reason and "_sourcePageLimit" in raw else "miss"))

SRAVNIOTEL_SOURCE: HotelSource = SravniOtelSource()
