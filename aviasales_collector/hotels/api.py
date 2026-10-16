"""Hotel search FastAPI routes and request validation."""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query
from fastapi import Request as FastAPIRequest
from fastapi.responses import StreamingResponse

from .city_resolution import suggest_cities
from .geo.enrichment import _enrich_search_coordinates
from .models import HotelSearchOperationError, SourceError
from .pagination import (SRAVNI_DISCOVERY_BUDGET_SECONDS,
                         SRAVNI_DISCOVERY_MAX_PAGES, load_more_sravni_page)
from .persistence import _sanitize_enrichment_error_message
from .pipeline import _finalize_search
from .rate_enrichment import enrich_tripcom_room_rates
from .sravni_detail import enrich_sravni_hotel_pages
from .read_model import _read_search
from .runtime import (_profiled_sqlite_connect, _safe_json_loads, log,
                      log_search_timeline)
from .service import start_hotel_search

HOTELS_ASSETS = Path(__file__).resolve().parents[1] / "web_assets"

LOCAL_BROWSER_HOSTS = frozenset({"127.0.0.1", "localhost"})


_OPERATION_HTTP_STATUS = {"not_found": 404, "invalid_payload": 422, "browser_busy": 409}


def _raise_operation_error(error: HotelSearchOperationError) -> None:
    raise HTTPException(status_code=_OPERATION_HTTP_STATUS[error.code], detail=error.detail) from error


def _same_local_browser_origin(request: FastAPIRequest) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        client_host = request.client.host if request.client else ""
        try:
            return ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            return False
    origin_url = urlsplit(origin)
    request_url = urlsplit(str(request.base_url))
    return (
        origin_url.scheme == request_url.scheme == "http"
        and origin_url.hostname in LOCAL_BROWSER_HOSTS
        and origin_url.hostname == request_url.hostname
        and origin_url.port == request_url.port
        and origin_url.username is None
        and origin_url.password is None
        and origin_url.path in ("", "/")
        and not origin_url.query
        and not origin_url.fragment
    )


def _geo_hotel_identity_keys(hotel: dict[str, Any]) -> set[str]:
    records = hotel.get("source_records") or [hotel]
    return {
        f"{record.get('source')}:{record.get('source_hotel_id')}"
        for record in records
        if isinstance(record, dict) and record.get("source")
        and record.get("source_hotel_id") is not None
    }


def _select_geo_hotels_by_keys(
    hotels: list[dict[str, Any]], requested_keys: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Resolve stable provider identities after the result list is finalized/ranked."""
    selected = []
    matched: set[str] = set()
    for hotel in hotels:
        keys = _geo_hotel_identity_keys(hotel)
        if keys & requested_keys:
            selected.append(hotel)
            matched.update(keys & requested_keys)
    return selected, requested_keys - matched


def _hotel_search_progress(database_path: str | Path, search_id: str) -> dict[str, Any] | None:
    """Return a cheap revision token for polling without building the full read model."""
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        session = connection.execute(
            "SELECT status,total_hotels,next_page_token,finished_at,started_at "
            "FROM hotel_search_session WHERE id=?", (search_id,),
        ).fetchone()
        if session is None:
            return None
        sources = [dict(row) for row in connection.execute(
            "SELECT source,status,latency_ms,pages,hotel_count,offer_count,cache_state,"
            "reported_total,error_class,observed_at,http_status,collection_complete,collection_reason "
            "FROM hotel_source_run WHERE search_id=? ORDER BY id", (search_id,),
        )]
        for source in sources:
            if source.get("collection_complete") is not None:
                source["collection_complete"] = bool(source["collection_complete"])
        # A changed revision makes the browser fetch the complete read model.
        # Sravni pages already arrive through /more deltas, while rates and geo
        # have their own scoped endpoints; including their counters here caused
        # a 30+ MB snapshot fetch for every small enrichment write. Only Trip
        # discovery can add a new source result outside those incremental paths.
        trip_source = next((source for source in sources if source.get("source") == "trip_com"), None)
        trip_revision = tuple(trip_source.get(field) for field in (
            "source", "status", "hotel_count", "offer_count", "cache_state",
            "reported_total", "error_class", "observed_at", "http_status",
            "collection_complete", "collection_reason",
        )) if trip_source else None
        progress_revision = json.dumps(
            trip_revision, ensure_ascii=False, separators=(",", ":"), default=str,
        )
        return {
            "search_id": search_id, "status": session["status"],
            "total_hotels": session["total_hotels"], "has_more": bool(session["next_page_token"]),
            "sources": sources, "progress_revision": progress_revision,
        }

def build_router(database_path: str | Path) -> APIRouter:
    router = APIRouter()

    @router.get("/hotels", include_in_schema=False)
    def hotels_page():
        from fastapi.responses import FileResponse
        return FileResponse(HOTELS_ASSETS / "hotels.html", media_type="text/html; charset=utf-8")

    @router.get("/hotel-assets/{asset_name}", include_in_schema=False)
    def hotel_asset(asset_name: str):
        from fastapi.responses import FileResponse
        if asset_name not in {
            "hotels.css", "hotel-results.css", "hotels.js", "hotel_search_lifecycle.js",
            "leaflet.js", "leaflet.css",
            "leaflet.markercluster.js", "MarkerCluster.css", "MarkerCluster.Default.css",
            "hotel-map-layers-2x.png", "hotel-map-layers.png", "hotel-map-marker-icon-2x.png",
            "hotel-map-marker-icon.png", "hotel-map-marker-shadow.png",
        }:
            raise HTTPException(status_code=404)
        return FileResponse(HOTELS_ASSETS / asset_name)

    @router.get("/api/hotels/cities")
    async def cities(q: str = Query(min_length=2, max_length=80)):
        try:
            found, http_status, latency_ms = await suggest_cities(q.strip())
        except SourceError as exc:
            raise HTTPException(status_code=503, detail={"source": "sravniotel", "status": exc.kind}) from exc
        return {"source": "sravniotel", "status": "ok", "http_status": http_status,
                "latency_ms": latency_ms, "cities": found}

    @router.post("/api/hotels/search")
    async def search(request: FastAPIRequest):
        if not _same_local_browser_origin(request):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("search payload must be an object")
            geo_id = str(payload.get("geo_id") or "").strip()
            city_name = " ".join(str(payload["city_name"]).split())
            country = " ".join(str(payload.get("country") or "").split()) or None
            city_slug = str(payload.get("city_slug") or "").strip().lower()
            country_slug = str(payload.get("country_slug") or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", city_slug):
                city_slug = ""
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", country_slug):
                country_slug = ""
            check_in = date.fromisoformat(str(payload["check_in"]))
            check_out = date.fromisoformat(str(payload["check_out"]))
            adults = payload.get("adults", 2)
            children_count = payload.get("children_count", 0)
            limit = payload.get("limit", 20)
            room_mode = payload.get("room_mode", "all")
            if any(type(value) is not int for value in (adults, children_count, limit)):
                raise ValueError
            if room_mode not in {"private", "all"}:
                raise ValueError
            limit = min(20, max(1, limit))
            if (not city_name or len(city_name) > 100 or len(country or "") > 100
                    or len(geo_id) > 100 or check_in < date.today() or check_out <= check_in
                    or not 1 <= adults <= 12 or not 0 <= children_count <= 8):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail="Choose a city, valid dates, and a supported guest count") from exc

        query = {"geo_id": geo_id, "city_name": city_name, "check_in": check_in.isoformat(),
                 "check_out": check_out.isoformat(), "adults": adults, "children_count": children_count,
                 "limit": limit, "currency": "source_defined", "room_mode": room_mode,
                 "country": country, "city_slug": city_slug, "country_slug": country_slug}
        result = await start_hotel_search(database_path, query)
        if isinstance(result, dict) and result.get("search_id"):
            progress = _hotel_search_progress(database_path, str(result["search_id"]))
            if progress:
                result["progress_revision"] = progress["progress_revision"]
        return result

    @router.get("/api/hotels/search/{search_id}/progress")
    def search_progress(search_id: str):
        progress = _hotel_search_progress(database_path, search_id)
        if progress is None:
            raise HTTPException(status_code=404, detail="Hotel search not found")
        return progress

    @router.get("/api/hotels/search/{search_id}")
    async def get_search(
        search_id: str,
        room_mode: str | None = Query(default=None, pattern="^(private|all)$"),
        trip_hotel_ids: str | None = Query(default=None, max_length=20_000),
    ):
        requested_trip_ids = None
        if trip_hotel_ids is not None:
            values = trip_hotel_ids.split(",")
            if (not values or len(values) > 1000
                    or any(not value.isdigit() or len(value) > 32 for value in values)):
                raise HTTPException(status_code=422, detail="Invalid Trip.com hotel_ids")
            requested_trip_ids = set(values)
        result = _read_search(database_path, search_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Hotel search not found")
        if room_mode:
            result["query"]["room_mode"] = room_mode
        result = _finalize_search(database_path, result)
        progress = _hotel_search_progress(database_path, search_id)
        if progress:
            result["progress_revision"] = progress["progress_revision"]
        if requested_trip_ids is not None:
            result["hotels"] = [
                hotel for hotel in result["hotels"]
                if any(record.get("source") == "trip_com"
                       and str(record.get("source_hotel_id") or "") in requested_trip_ids
                       for record in (hotel.get("source_records") or [hotel]))
            ]
        return result

    @router.post("/api/hotels/search/{search_id}/room-rates")
    async def enrich_room_rates(search_id: str, request: FastAPIRequest):
        if not _same_local_browser_origin(request):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        result = _read_search(database_path, search_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Hotel search not found")
        # SravniOtel policy/amenity enrichment runs off the request path:
        # light HTTP page fetches (no browser, no Trip.com detail pages) that
        # fill check-in/out times and hotel amenities for sravni cards.
        async def _run_sravni_page_enrichment() -> None:
            try:
                await asyncio.to_thread(enrich_sravni_hotel_pages, database_path, search_id)
            except Exception as exc:  # Page enrichment must never break rate enrichment.
                log.info("[SRAVNI_DETAIL] enrichment_skipped search=%s error=%s", search_id, str(exc)[:120])
        sravni_task = asyncio.create_task(_run_sravni_page_enrichment())
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        requested_ids = payload.get("hotel_ids") if isinstance(payload, dict) else None
        if requested_ids is not None and (
            not isinstance(requested_ids, list) or len(requested_ids) > 1000
            or any(not isinstance(value, (str, int)) or not str(value).isdigit() for value in requested_ids)
        ):
            raise HTTPException(status_code=422, detail="Invalid hotel_ids")
        if "concurrency" in payload and (type(payload["concurrency"]) is not int
                                         or not 1 <= payload["concurrency"] <= 5):
            raise HTTPException(status_code=422, detail="Room-rate concurrency must be between 1 and 5")
        wanted = {str(value) for value in requested_ids} if requested_ids is not None else None
        try:
            log_search_timeline(search_id, "rate_enrichment_request", requested_hotels=len(wanted) if wanted else None,
                                concurrency=payload.get("concurrency", 5))
            enriched = await enrich_tripcom_room_rates(database_path, search_id, result, payload, wanted)
            log_search_timeline(search_id, "rate_enrichment_response", hotels=len(enriched.get("hotels", [])),
                                status=enriched.get("status"))
            return enriched
        except HotelSearchOperationError as exc:
            _raise_operation_error(exc)

    @router.post("/api/hotels/search/{search_id}/coordinates")
    async def enrich_coordinates(search_id: str, request: FastAPIRequest):
        if not _same_local_browser_origin(request):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        endpoint_started = time.perf_counter()
        log_search_timeline(search_id, "geo_endpoint_started")
        result = _read_search(database_path, search_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Hotel search not found")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("coordinate payload must be an object")
        except (ValueError, TypeError):
            payload = {"all": True}
        room_mode = payload.get("room_mode", result["query"].get("room_mode", "all"))
        offset = payload.get("offset", 0)
        stream_batches = payload.get("stream", False)
        requested_hotel_keys = payload.get("hotel_keys")
        enrich_all = bool(payload.get("all", False if stream_batches else "limit" not in payload))
        batch_limit = payload.get("limit")
        batch_size = payload.get("batch_size", 10)
        if room_mode not in {"private", "all"}:
            raise HTTPException(status_code=422, detail="Unsupported room mode")
        if type(offset) is not int or offset < 0:
            raise HTTPException(status_code=422, detail="Invalid coordinate batch")
        if type(stream_batches) is not bool:
            raise HTTPException(status_code=422, detail="Invalid coordinate stream flag")
        if requested_hotel_keys is not None and (
            not stream_batches or not isinstance(requested_hotel_keys, list)
            or len(requested_hotel_keys) > 1000
            or any(not isinstance(value, str) or not value or len(value) > 160
                   or ":" not in value or any(ord(character) < 32 for character in value)
                   for value in requested_hotel_keys)
        ):
            raise HTTPException(status_code=422, detail="Invalid stable hotel_keys batch")
        if stream_batches and (type(batch_size) is not int or not 1 <= batch_size <= 20):
            raise HTTPException(status_code=422, detail="Invalid coordinate stream batch size")
        if requested_hotel_keys is not None:
            offset = 0
            batch_limit = len(set(requested_hotel_keys))
        elif enrich_all:
            offset, batch_limit = 0, len(result["hotels"])
        elif stream_batches:
            if offset > len(result["hotels"]):
                raise HTTPException(status_code=422, detail="Invalid coordinate batch")
            if batch_limit is None:
                batch_limit = len(result["hotels"]) - offset
            elif type(batch_limit) is not int or not 1 <= batch_limit <= 400:
                raise HTTPException(status_code=422, detail="Invalid coordinate stream limit")
            else:
                batch_limit = min(batch_limit, len(result["hotels"]) - offset)
        elif type(batch_limit) is not int or not 1 <= batch_limit <= 40:
            raise HTTPException(status_code=422, detail="Invalid coordinate batch")
        result["query"]["room_mode"] = room_mode
        finalize_started = time.perf_counter()
        result = _finalize_search(database_path, result)
        result_total = len(result.get("hotels", []))
        unresolved_hotel_keys: set[str] = set()
        stream_result = result
        if requested_hotel_keys is not None:
            selected_hotels, unresolved_hotel_keys = _select_geo_hotels_by_keys(
                result["hotels"], set(requested_hotel_keys),
            )
            stream_result = {**result, "hotels": selected_hotels}
            batch_limit = len(selected_hotels)
        request_id = uuid.uuid4().hex
        log_search_timeline(search_id, "geo_read_model_and_finalize_complete",
                            elapsed_ms=round((time.perf_counter() - endpoint_started) * 1000, 1),
                            finalize_ms=round((time.perf_counter() - finalize_started) * 1000, 1),
                            hotels=result_total, stream=stream_batches,
                            offset=offset, limit=batch_limit)
        if requested_hotel_keys is not None:
            log_search_timeline(
                search_id, "geo_identity_request_resolved", batch_id=request_id,
                requested_keys=len(set(requested_hotel_keys)),
                selected_hotels=len(stream_result["hotels"]),
                unresolved_keys_json=json.dumps(sorted(unresolved_hotel_keys), separators=(",", ":")),
                selected_canonical_ids_json=json.dumps(
                    [hotel.get("canonical_hotel_id") for hotel in stream_result["hotels"]],
                    separators=(",", ":"),
                ),
            )
        if stream_batches:
            async def enriched_batches():
                hotels_for_stream = stream_result["hotels"]
                total = len(hotels_for_stream)
                end = min(total, offset + (batch_limit or 0))
                for start in range(offset, end, batch_size):
                    stop = min(end, start + batch_size)
                    batch_started = time.perf_counter()
                    batch_id = f"{request_id}:{start}"
                    batch_hotels = hotels_for_stream[start:stop]
                    batch_hotel_ids = [sorted(_geo_hotel_identity_keys(hotel)) for hotel in batch_hotels]
                    canonical_ids = [hotel.get("canonical_hotel_id") for hotel in batch_hotels]
                    batch_details = {
                        "batch_id": batch_id,
                        "offset": start,
                        "size": stop - start,
                        "hotel_ids_json": json.dumps(batch_hotel_ids, ensure_ascii=False, separators=(",", ":")),
                        "canonical_ids_json": json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":")),
                    }
                    log_search_timeline(search_id, "geo_batch_started", **batch_details, total=total)
                    # 2026-10-15 first-card latency (user directive: first cards
                    # within seconds, but COMPLETE). Phase 1 resolves coordinates
                    # + center distance only (fast provider lookups); the batch
                    # is yielded immediately. Phase 2 below runs the full geo
                    # stages (beach/metro Overpass) and re-yields the same
                    # hotels as an update, so cards gain metro/beach lines the
                    # moment they arrive - the render-signature cache re-renders
                    # just those cards.
                    try:
                        await _enrich_search_coordinates(
                            database_path, stream_result, limit=stop - start, offset=start,
                            coordinate_only=True,
                        )
                    except Exception as exc:
                        log.exception("[ENRICH] coordinate batch failed offset=%d", start)
                        for hotel in hotels_for_stream[start:stop]:
                            location = hotel.setdefault("location", {})
                            location["coordinate_status"] = "ERROR"
                            location["coordinate_reason"] = f"coordinate_batch:{type(exc).__name__}"
                            location["beach_status"] = "source_unavailable"
                            location["nearby_geo_status"] = "error"
                    serialization_started = time.perf_counter()
                    line = json.dumps({
                        "hotels": hotels_for_stream[start:stop], "offset": start,
                        "complete": stop >= end, "total": total, "result_total": result_total,
                        "batch_id": batch_id,
                        "unresolved_hotel_keys": sorted(unresolved_hotel_keys) if stop >= end else [],
                    }, ensure_ascii=False, default=str) + "\n"
                    terminal_statuses = {}
                    for hotel in batch_hotels:
                        location = hotel.get("location") or {}
                        state = "/".join(str(location.get(field) or "unknown") for field in
                                         ("coordinate_status", "beach_status", "nearby_geo_status"))
                        terminal_statuses[state] = terminal_statuses.get(state, 0) + 1
                    log_search_timeline(
                        search_id, "geo_batch_terminal", **batch_details,
                        terminal_count=len(batch_hotels), statuses_json=json.dumps(
                            terminal_statuses, ensure_ascii=False, separators=(",", ":")),
                        elapsed_ms=round((time.perf_counter() - batch_started) * 1000, 1),
                        serialization_ms=round((time.perf_counter() - serialization_started) * 1000, 1),
                        bytes=len(line.encode("utf-8")),
                    )
                    yield line
                    # Phase 2 (update pass): full geo stages for the same
                    # batch - beach/metro/airport distances that depend on
                    # Overpass. Frontend's render-signature cache re-renders
                    # only the cards whose fields changed.
                    try:
                        await _enrich_search_coordinates(
                            database_path, stream_result, limit=stop - start, offset=start,
                        )
                    except Exception:
                        log.exception("[ENRICH] geo update pass failed offset=%d", start)
                    update_line = json.dumps({
                        "hotels": hotels_for_stream[start:stop], "offset": start,
                        "complete": stop >= end, "total": total, "result_total": result_total,
                        "batch_id": f"{batch_id}:geo",
                        "unresolved_hotel_keys": [],
                    }, ensure_ascii=False, default=str) + "\n"
                    yield update_line
                if offset >= end:
                    yield json.dumps({"hotels": [], "offset": offset,
                                      "complete": True, "total": total, "result_total": result_total,
                                      "batch_id": request_id,
                                      "unresolved_hotel_keys": sorted(unresolved_hotel_keys)}) + "\n"
            return StreamingResponse(
                enriched_batches(), media_type="application/x-ndjson",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        await _enrich_search_coordinates(database_path, result, limit=batch_limit, offset=offset)
        return result

    @router.get("/api/hotels/search/{search_id}/room-rates/status")
    def room_rate_enrichment_status(search_id: str):
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            session = connection.execute(
                "SELECT query_json FROM hotel_search_session WHERE id=?", (search_id,),
            ).fetchone()
            if session is None:
                raise HTTPException(status_code=404, detail="Hotel search not found")
            query = _safe_json_loads(session["query_json"], {})
            if not isinstance(query, dict):
                query = {}
            rows = connection.execute(
                """SELECT source_hotel_id,status,http_status,latency_ms,rates_seen,observed_at,
                          operation,error_type,error_message,attempt,retry_count
                   FROM hotel_room_rate_fetch
                   WHERE search_id=? AND source='trip_com' AND check_in=? AND check_out=?
                     AND adults=? AND children_count=?
                   ORDER BY observed_at DESC,id DESC""",
                (search_id, query.get("check_in"), query.get("check_out"),
                 query.get("adults"), query.get("children_count", 0)),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            hotel_id = str(row["source_hotel_id"])
            latest.setdefault(hotel_id, {
                "source_hotel_id": hotel_id, "status": row["status"],
                "http_status": row["http_status"], "latency_ms": row["latency_ms"],
                "rates_seen": row["rates_seen"], "observed_at": row["observed_at"],
                "operation": row["operation"], "error_type": row["error_type"],
                "error_message": _sanitize_enrichment_error_message(row["error_message"]),
                "attempt": row["attempt"], "retry_count": row["retry_count"],
            })
        return {"search_id": search_id, "hotels": list(latest.values())}

    @router.get("/api/hotels/search/{search_id}/room-rates/results")
    def room_rate_enrichment_results(
        search_id: str, hotel_ids: str = Query(min_length=1, max_length=20_000),
    ):
        values = hotel_ids.split(",")
        if (not values or len(values) > 1000
                or any(not value.isdigit() or len(value) > 32 for value in values)):
            raise HTTPException(status_code=422, detail="Invalid hotel_ids")
        result = _read_search(database_path, search_id, source_hotel_ids=set(values))
        if result is None:
            raise HTTPException(status_code=404, detail="Hotel search not found")
        enrichment_fields = (
            "source_hotel_id", "address", "stars", "rating", "rating_original", "rating_scale",
            "rating_normalized", "rating_source", "reviews_count", "coordinates", "coordinate_source",
            "coordinate_method", "coordinate_confidence", "coordinate_status", "coordinate_reason",
            "images", "amenities", "provider_details", "room_options", "room_rates",
            "property_details", "review_url", "room_rate_enrichment_status",
        )
        return {
            "search_id": search_id,
            "hotels": [{key: hotel.get(key) for key in enrichment_fields} for hotel in result["hotels"]],
        }

    @router.post("/api/hotels/search/{search_id}/more")
    async def load_more(search_id: str, request: FastAPIRequest):
        if not _same_local_browser_origin(request):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except (ValueError, TypeError):
            body = {}
        if "delta" in body and type(body["delta"]) is not bool:
            raise HTTPException(status_code=422, detail="Invalid pagination delta flag")
        try:
            return await load_more_sravni_page(
                database_path, search_id, body, delta=body.get("delta", False),
            )
        except HotelSearchOperationError as exc:
            _raise_operation_error(exc)

    return router
