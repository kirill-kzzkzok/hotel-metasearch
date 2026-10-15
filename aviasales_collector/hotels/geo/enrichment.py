"""Bounded asynchronous hotel location enrichment."""

from __future__ import annotations

import asyncio
import collections
import json
import math
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..city_resolution import suggest_cities
from ..models import SourceError, utc_now
from ..normalization import _normalize_city_label
from ..runtime import (_ACTIVE_ENRICHMENT_PROFILE, _EnrichmentProfile,
                       _profiled_sqlite_connect, _profiled_worker_call, log,
                       log_search_timeline)
from .city import _read_city_reference
from .core import (_coordinate_distance_m, _coordinate_pair,
                   _coordinate_source_metadata, _haversine_m)
from .geometry import _osm_nearby_features, _resolve_nearest_beach
from .providers import (_persist_coordinate_resolution,
                        _persist_hotel_coordinates, _persist_provider_metadata,
                        _provider_schema_detail, _resolve_city_reference,
                        _resolve_hotel_coordinates)


def _geo_enrichment_cache_is_terminal(hotel: dict[str, Any]) -> bool:
    """Use only unexpired read-model cache observations to avoid duplicate geo work."""
    location = hotel.get("location") if isinstance(hotel.get("location"), dict) else {}
    records = hotel.get("source_records") or [hotel]
    coordinates = _coordinate_pair(location.get("coordinates"))
    if coordinates:
        beach_statuses = {"ok", "not_found", "out_of_range", "error", "source_unavailable",
                          "timeout", "rate_limited"}
        nearby_statuses = {"ok", "not_found", "error", "source_unavailable", "timeout", "rate_limited"}
        beach_cached = any(str(record.get("cached_beach_status") or "").casefold() in beach_statuses
                           for record in records if isinstance(record, dict))
        nearby_cached = any(str(record.get("cached_geo_status") or "").casefold() in nearby_statuses
                            for record in records if isinstance(record, dict))
        # Canonical merging keeps the already TTL-filtered public status on
        # location, while its compact source_records may omit cache internals.
        # Use those projected statuses as the cache evidence for merged cards.
        if not beach_cached:
            beach_cached = str(location.get("beach_status") or "").casefold() in {
                "available", "not_found", "source_unavailable",
            }
        if not nearby_cached:
            nearby_cached = str(location.get("nearby_geo_status") or "").casefold() in nearby_statuses
        # _read_search exposes these cache states only while their current TTL is valid.
        return beach_cached and nearby_cached

    status = str(location.get("coordinate_status") or "").upper()
    if status not in {"NOT_FOUND", "ERROR", "CONFLICT"}:
        return False
    retrieved_at = next((record.get("coordinate_resolution_retrieved_at")
                         for record in records if isinstance(record, dict)
                         and record.get("coordinate_resolution_retrieved_at")), None)
    try:
        retrieved = datetime.fromisoformat(str(retrieved_at).replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - retrieved.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    ttl = timedelta(days=1) if status == "NOT_FOUND" else timedelta(minutes=1)
    return timedelta(0) <= age < ttl

async def _enrich_search_coordinates(path: str | Path, result: dict[str, Any], limit: int | None = None,
                                     offset: int = 0, *, coordinate_only: bool = False) -> None:
    batch_started = time.perf_counter()
    query = result.get("query", {})
    search_id = str(result.get("search_id") or "")
    city = str(query.get("city_name") or "")
    city_reference = None
    city_label_is_corrupt = "\ufffd" in city
    country_label = str(query.get("country") or "")
    country_label_is_corrupt = "\ufffd" in country_label
    if city and (city_label_is_corrupt or country_label_is_corrupt
                 or not query.get("country") or not query.get("city_slug") or not query.get("country_slug")):
        try:
            lookup_label = (query.get("city_slug") or city) if city_label_is_corrupt else city
            resolved_cities, _http_status, _latency_ms = await suggest_cities(str(lookup_label))
        except SourceError:
            resolved_cities = []
        expected_geo_id = str(query.get("geo_id") or "")
        exact_name = _normalize_city_label(city)
        exact = [item for item in resolved_cities if _normalize_city_label(item.get("name")) == exact_name]
        resolved = next((item for item in resolved_cities if expected_geo_id
                         and str(item.get("geo_id") or "") == expected_geo_id), None)
        if resolved is None and len(exact) == 1:
            resolved = exact[0]
        if resolved:
            if city_label_is_corrupt and resolved.get("name"):
                query["city_name"] = resolved["name"]
            for key in ("country", "city_slug", "country_slug"):
                needs_country = country_label_is_corrupt or not query.get("country")
                if (needs_country if key == "country" else not query.get(key)) and resolved.get(key):
                    query[key] = resolved[key]
            result["query"] = query
            city = str(query.get("city_name") or city)
            if result.get("search_id"):
                with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
                    connection.execute(
                        "UPDATE hotel_search_session SET query_json=? WHERE id=?",
                        (json.dumps(query, ensure_ascii=False), result["search_id"]),
                    )
                    connection.commit()
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        city_reference = _read_city_reference(connection, query)
    if city_reference is None and city and query.get("country"):
        city_started = time.perf_counter()
        city_reference = await asyncio.to_thread(
            _resolve_city_reference, path, city, str(query["country"]),
            str(query.get("city_slug") or ""), str(query.get("country_slug") or ""),
        )
        log.info("[ENRICH] city_profile city=%s elapsed=%.1fms status=%s",
                 city, (time.perf_counter() - city_started) * 1000,
                 "resolved" if city_reference else "unavailable")
    else:
        log.info("[ENRICH] city_profile city=%s elapsed=0.0ms cache=hit status=%s",
                 city, "resolved" if city_reference else "unavailable")
    hotels = result.get("hotels", [])
    def stability_snapshot() -> str:
        return json.dumps([
            {
                # Coordinate and provider-detail enrichment are expected to
                # add their own metadata. They must not alter the stable
                # identity, offers, matching, or ranking inputs.
                "hotel": {key: value for key, value in hotel.items()
                          if key not in {"location", "provider_details"}},
            }
            for hotel in hotels
        ], ensure_ascii=False, sort_keys=True, default=str)

    stable_before = stability_snapshot()
    start = max(0, int(offset or 0))
    batch = hotels[start:] if limit is None else hotels[start:start + max(1, int(limit or 1))]
    cached_terminal = [hotel for hotel in batch if _geo_enrichment_cache_is_terminal(hotel)]
    work_batch = [hotel for hotel in batch if not _geo_enrichment_cache_is_terminal(hotel)]
    log_search_timeline(search_id, "geo_batch_worker_start", offset=start, size=len(batch),
                        work_size=len(work_batch), cache_terminal=len(cached_terminal),
                        total=len(hotels), concurrency=5)
    if cached_terminal:
        log_search_timeline(
            search_id, "geo_batch_cache_reused", offset=start,
            hotel_keys_json=json.dumps([
                hotel.get("canonical_hotel_id") or
                f"{hotel.get('source')}:{hotel.get('source_hotel_id')}"
                for hotel in cached_terminal
            ], ensure_ascii=False, separators=(",", ":")),
            terminal_count=len(cached_terminal),
        )
    # A small pool lets independent hotel lookups overlap while the global
    # provider and OSM clients retain their own stricter connection bounds.
    detail_semaphore = asyncio.Semaphore(5)
    geo_profiles: list[dict[str, Any]] = []

    async def enrich_inner(hotel: dict[str, Any]) -> None:
        location = hotel.get("location") or {}
        expected_country = (hotel.get("country") or result.get("query", {}).get("country")
                            or result.get("query", {}).get("country_slug"))
        wait_started = time.perf_counter()
        async with detail_semaphore:
            profile = _ACTIVE_ENRICHMENT_PROFILE.get()
            if profile:
                profile.add_wait((time.perf_counter() - wait_started) * 1000)
            stage_started = time.perf_counter()
            coordinate_task = asyncio.create_task(asyncio.to_thread(
                _profiled_worker_call, _resolve_hotel_coordinates, path, hotel, city, expected_country,
                result.get("query", {}).get("city_slug"), result.get("query", {}).get("country_slug"),
            ))
            # Keep the bounded hotel worker occupied until coordinate lookup
            # really finishes. Detaching a thread after a short asyncio timeout
            # released this semaphore while the HTTP calls kept running, so a
            # long search could accumulate hundreds of unbounded geo workers.
            resolution = await coordinate_task
            if profile:
                profile.add_stage("coordinates_and_geocoding", (time.perf_counter() - stage_started) * 1000)
            provider_detail = None
            source_records = hotel.get("source_records") or [hotel]
            sravni_records = [record for record in source_records
                              if record.get("source") == "sravniotel"
                              and record.get("source_hotel_id") is not None]
            schema_offers = [offer for offer in hotel.get("offers", [])
                             if (urlsplit(str(offer.get("deeplink") or "")).hostname or "").casefold().endswith(
                                 ("tutu.ru", "ostrovok.ru", "sutochno.ru"))]
            if sravni_records and schema_offers:
                stage_started = time.perf_counter()
                provider_detail = await asyncio.to_thread(
                    _profiled_worker_call, _provider_schema_detail, path, hotel, city,
                    expected_country or result.get("query", {}).get("country_slug"),
                )
                if profile:
                    profile.add_stage("provider_deeplink_jsonld", (time.perf_counter() - stage_started) * 1000)
        provider_metadata = provider_detail.get("provider_details") if provider_detail else None
        provider_coordinate = _coordinate_pair(provider_detail.get("coordinates")) if provider_detail else None
        existing_coordinate = _coordinate_pair(resolution.get("coordinates"))
        if provider_coordinate and existing_coordinate:
            disagreement = _coordinate_distance_m(provider_coordinate, existing_coordinate)
            if disagreement is not None and disagreement > 25_000:
                resolution.update({"coordinates": None, "status": "CONFLICT",
                                   "reason": "provider_detail_coordinate_conflict",
                                   "conflict": {"sources": [resolution.get("coordinate_source"),
                                                            provider_detail.get("coordinate_source")],
                                                "distance_meters": round(disagreement, 1)}})
        elif provider_coordinate and not existing_coordinate:
            resolution.update({"coordinates": provider_coordinate, "status": "PROVIDER_DETAIL",
                               "reason": "provider_detail_verified",
                               "coordinate_source": provider_detail.get("coordinate_source"),
                               "coordinate_method": "hotel_detail_structured_data",
                               "coordinate_confidence": "high"})
            resolution.setdefault("attempts", []).append(
                {"source": "provider_detail", "status": "SUCCESS", "reason": "schema_org_hotel_geo"})
        if provider_metadata:
            current_details = [item for item in hotel.get("provider_details", [])
                               if item.get("provider") != provider_metadata.get("provider")]
            hotel["provider_details"] = current_details + [provider_metadata]
            stage_started = time.perf_counter()
            for record in sravni_records:
                await asyncio.to_thread(
                    _profiled_worker_call, _persist_provider_metadata, path, result.get("search_id", ""),
                    "sravniotel", str(record["source_hotel_id"]), provider_metadata,
                )
            if profile:
                profile.add_stage("db_provider_metadata", (time.perf_counter() - stage_started) * 1000)
        coordinates = resolution.get("coordinates")
        coordinate_source = resolution.get("coordinate_source")
        coordinate_method = resolution.get("coordinate_method")
        coordinate_confidence = resolution.get("coordinate_confidence")
        location["coordinate_status"] = resolution.get("status", "NOT_FOUND")
        location["coordinate_reason"] = resolution.get("reason")
        location["coordinate_attempts"] = resolution.get("attempts", [])
        if resolution.get("conflict"):
            location["coordinate_conflict"] = resolution["conflict"]
        else:
            location.pop("coordinate_conflict", None)
        if coordinates:
            location["coordinates"] = coordinates
            location["coordinate_source"] = coordinate_source or "provider"
            location["coordinate_method"] = coordinate_method or _coordinate_source_metadata(coordinate_source)[0]
            location["coordinate_confidence"] = coordinate_confidence or _coordinate_source_metadata(coordinate_source)[1]
            location["retrieved_at"] = location.get("retrieved_at") or utc_now()
            center_started = time.perf_counter()
            if city_reference:
                center_distance = _haversine_m(
                    coordinates["lat"], coordinates["lng"],
                    city_reference.center_lat, city_reference.center_lon,
                )
                location["distance_to_center_m"] = round(center_distance, 1)
                location["distance_to_center_km"] = round(center_distance / 1000, 2)
                location["distance_to_center_method"] = "haversine"
                location["center_source"] = city_reference.source
                location["center_reference"] = {
                    "city_slug": city_reference.city_slug,
                    "country_slug": city_reference.country_slug,
                    "name": city_reference.name,
                    "lat": city_reference.center_lat,
                    "lon": city_reference.center_lon,
                    "source": city_reference.source,
                    "retrieved_at": city_reference.retrieved_at,
                    "method": city_reference.method,
                }
            else:
                for key in ("distance_to_center_m", "distance_to_center_km",
                            "distance_to_center_method", "center_source", "center_reference"):
                    location.pop(key, None)
            if profile:
                profile.add_stage("center_distance", (time.perf_counter() - center_started) * 1000)
            hotel["location"] = location
            stage_started = time.perf_counter()
            await asyncio.to_thread(
                _profiled_worker_call, _persist_hotel_coordinates, path, result.get("search_id", ""), hotel,
                coordinates, location["coordinate_source"], location["coordinate_method"],
                location["coordinate_confidence"],
            )
            if profile:
                profile.add_stage("db_coordinates", (time.perf_counter() - stage_started) * 1000)
            async def run_geo_stage(stage: str, function: Any, *args: Any) -> Any:
                started = time.perf_counter()
                worker = asyncio.create_task(asyncio.to_thread(_profiled_worker_call, function, *args))
                try:
                    # The source clients have their own bounded request timeouts
                    # and 429 cooldowns. Do not detach a running thread here:
                    # doing so freed the hotel semaphore and caused work to pile
                    # up after the first few dozen cards.
                    return await worker
                finally:
                    if profile:
                        profile.add_stage(stage, (time.perf_counter() - started) * 1000)

            # 2026-10-15 first-card latency: phase 1 (coordinate_only) resolves
            # coordinates and center distance then returns; the stream yields
            # the batch immediately and re-runs the full geo stages as an
            # update pass, so the first complete cards do not wait for cold
            # Overpass deadlines (up to 12s per hotel).
            if coordinate_only:
                hotel["location"] = location
                return  # phase 2 (beach/nearby Overpass) runs as an update pass
            # Beach geometry and generic POIs are independent once the hotel
            # coordinate is known. Fetch both concurrently; each keeps its own
            # spatial cache and source evidence.
            beach, nearby_geo = await asyncio.gather(
                run_geo_stage(
                    "beach_geometry_and_distance", _resolve_nearest_beach,
                    path, coordinates, city, expected_country,
                    result.get("query", {}).get("city_slug"),
                    result.get("query", {}).get("country_slug"), True,
                ),
                run_geo_stage("metro_airport_landmarks", _osm_nearby_features, path, coordinates),
                return_exceptions=True,
            )
            if isinstance(beach, Exception):
                log.warning("[ENRICH] hotel=%s beach failed: %s", hotel.get("name"), type(beach).__name__)
                beach = {"beach_relevant": None, "beach_geometry_available": None, "status": "error"}
            if isinstance(nearby_geo, Exception):
                log.warning("[ENRICH] hotel=%s nearby failed: %s", hotel.get("name"), type(nearby_geo).__name__)
                nearby_geo = {"status": "error", "source": "openstreetmap_overpass", "places": {}}
            beach = beach or {"beach_relevant": None, "beach_geometry_available": None,
                              "status": "unknown"}
            location["beach_relevant"] = beach.get("beach_relevant")
            location["beach_geometry_available"] = beach.get("beach_geometry_available")
            location["beach_status"] = beach.get("public_status") or beach.get("status")
            if beach.get("distance_meters") is not None:
                location["distance_to_beach_km"] = round(beach["distance_meters"] / 1000, 6)
                location["beach_name"] = beach.get("name")
                location["beach_source"] = beach.get("source")
                location["distance_to_beach_method"] = beach.get("method")
                location["distance_to_beach_confidence"] = beach.get("confidence")
                location["beach_reference"] = {
                    "name": beach.get("name"), "source": beach.get("source"),
                    "osm_type": beach.get("osm_type"), "osm_id": beach.get("osm_id"),
                }
            else:
                location.pop("distance_to_beach_km", None)
                location.pop("beach_name", None)
                location.pop("beach_source", None)
                location.pop("distance_to_beach_method", None)
                location.pop("distance_to_beach_confidence", None)
                location.pop("beach_reference", None)
            location["nearby_geo_places"] = nearby_geo.get("places", {})
            location["nearby_geo_source"] = nearby_geo.get("source")
            location["nearby_geo_status"] = nearby_geo.get("status")
            for category, field in (("metro", "distance_to_metro_km"),
                                    ("airport", "distance_to_airport_km"),
                                    ("landmark", "distance_to_osm_landmark_km")):
                place = (nearby_geo.get("places") or {}).get(category)
                if place:
                    location[field] = place["distance_km"]
                else:
                    location.pop(field, None)
        elif city_reference:
            # When a reference point is configured, center distance is derived
            # only from a verified hotel coordinate and Haversine. Provider
            # snippets must not fill the gap with a different or unknown basis.
            for key in ("distance_to_center_m", "distance_to_center_km",
                        "distance_to_center_method", "center_source", "center_reference"):
                location.pop(key, None)
        if not coordinates:
            for key in ("nearby_geo_places", "nearby_geo_source", "nearby_geo_status",
                        "distance_to_metro_km", "distance_to_airport_km", "distance_to_osm_landmark_km"):
                location.pop(key, None)
        hotel["location"] = location
        stage_started = time.perf_counter()
        await asyncio.to_thread(
            _profiled_worker_call, _persist_coordinate_resolution, path, result.get("search_id", ""), hotel,
            resolution.get("status", "NOT_FOUND"), resolution.get("reason"),
            resolution.get("attempts", []), coordinates, location.get("coordinate_source"),
            location.get("coordinate_method"), location.get("coordinate_confidence"),
        )
        if profile:
            profile.add_stage("db_coordinate_resolution", (time.perf_counter() - stage_started) * 1000)

    async def enrich(hotel: dict[str, Any]) -> None:
        key = hotel.get("canonical_hotel_id") or f"{hotel.get('source')}:{hotel.get('source_hotel_id')}"
        profile = _EnrichmentProfile(str(hotel.get("name") or key))
        token = _ACTIVE_ENRICHMENT_PROFILE.set(profile)
        log_search_timeline(search_id, "geo_hotel_started", key=key, name=hotel.get("name"))
        log.info("[ENRICH] hotel=%s key=%s", hotel.get("name"), key)
        try:
            await enrich_inner(hotel)
        except Exception as exc:
            log.exception("[ENRICH] hotel=%s failed", hotel.get("name"))
            # One malformed provider response or database row must not cancel
            # enrichment for every other card in the batch. Keep the card and
            # expose a traceable partial outcome to the API/UI.
            location = hotel.setdefault("location", {})
            location["coordinate_status"] = "ERROR"
            location["coordinate_reason"] = f"enrichment_exception:{type(exc).__name__}"
            location.setdefault("coordinate_attempts", [])
            location["enrichment_status"] = "partial"
        finally:
            snapshot = profile.snapshot()
            location = hotel.get("location") or {}
            geo_profiles.append({
                "key": str(key), "name": str(hotel.get("name") or ""),
                "elapsed_ms": snapshot["total_ms"], "queue_wait_ms": snapshot["waiting_ms"],
                "coordinate_ms": snapshot["stages_ms"].get("coordinates_and_geocoding", 0.0),
                "beach_ms": snapshot["stages_ms"].get("beach_geometry_and_distance", 0.0),
                "nearby_ms": snapshot["stages_ms"].get("metro_airport_landmarks", 0.0),
                "coordinate_status": str(location.get("coordinate_status") or "unknown"),
                "beach_status": str(location.get("beach_status") or "unknown"),
                "cache_hits": snapshot["cache_hits"], "cache_misses": snapshot["cache_misses"],
            })
            for stage, elapsed in sorted(snapshot["stages_ms"].items()):
                log.info("[ENRICH] hotel=%s %s=%.1fms", hotel.get("name"), stage, elapsed)
            for stage, elapsed in sorted(snapshot["http_ms"].items()):
                log.info("[ENRICH] hotel=%s network.%s=%.1fms open=%.1fms read=%.1fms",
                         hotel.get("name"), stage, elapsed,
                         snapshot["http_open_ms"].get(stage, 0.0),
                         snapshot["http_read_ms"].get(stage, 0.0))
            for stage, phases in sorted(snapshot.get("http_phases_ms", {}).items()):
                if phases:
                    log.info("[ENRICH] hotel=%s network.%s.phases=%s", hotel.get("name"), stage,
                             json.dumps({key: round(value, 1) for key, value in phases.items()},
                                        ensure_ascii=False, sort_keys=True))
            for stage, size in sorted(snapshot.get("http_bytes", {}).items()):
                log.info("[ENRICH] hotel=%s network.%s.bytes=%d", hotel.get("name"), stage, size)
            log.info("[ENRICH] hotel=%s total=%.1fms network=%.1fms cpu=%.1fms db=%.1fms "
                     "db_queries=%d commits=%d db_connection_wall=%.1fms waiting=%.1fms http_requests=%d",
                     hotel.get("name"), snapshot["total_ms"], snapshot["network_ms"], snapshot["cpu_ms"],
                     snapshot["db_exec_ms"], snapshot["sqlite_queries"], snapshot["sqlite_commits"],
                     snapshot["db_connection_wall_ms"], snapshot["waiting_ms"],
                     snapshot["actual_http_requests"])
            if snapshot.get("http_attempts", 0) != snapshot.get("actual_http_requests", 0):
                log.info("[ENRICH] hotel=%s http_attempts=%d", hotel.get("name"),
                         snapshot.get("http_attempts", 0))
            log.info("[CACHE] hotel=%s hits=%s misses=%s", hotel.get("name"),
                     snapshot["cache_hits"], snapshot["cache_misses"])
            location = hotel.get("location") or {}
            log_search_timeline(
                search_id, "geo_hotel_terminal", key=key, name=hotel.get("name"),
                elapsed_ms=round(snapshot["total_ms"], 1),
                queue_wait_ms=round(snapshot["waiting_ms"], 1),
                coordinate_status=location.get("coordinate_status"),
                beach_status=location.get("beach_status"),
                beach_distance_km=location.get("distance_to_beach_km"),
                cache_hits=snapshot["cache_hits"], cache_misses=snapshot["cache_misses"],
            )
            _ACTIVE_ENRICHMENT_PROFILE.reset(token)

    await asyncio.gather(*(enrich(hotel) for hotel in work_batch))
    def geo_distribution(field: str) -> dict[str, float | None]:
        values = sorted(float(item[field]) for item in geo_profiles if item.get(field) is not None)
        result: dict[str, float | None] = {}
        for fraction in (0.5, 0.9, 0.95, 0.99):
            if not values:
                result[f"p{int(fraction * 100)}"] = None
                continue
            position = (len(values) - 1) * fraction
            low = int(position)
            high = min(low + 1, len(values) - 1)
            result[f"p{int(fraction * 100)}"] = round(
                values[low] + (values[high] - values[low]) * (position - low), 1)
        result["max"] = round(values[-1], 1) if values else None
        return result

    log_search_timeline(
        search_id, "geo_card_distributions", cards=len(geo_profiles),
        queue_wait_ms=geo_distribution("queue_wait_ms"),
        total_ms=geo_distribution("elapsed_ms"),
        coordinate_ms=geo_distribution("coordinate_ms"),
        beach_ms=geo_distribution("beach_ms"),
        nearby_ms=geo_distribution("nearby_ms"),
        beach_status_counts=dict(collections.Counter(item["beach_status"] for item in geo_profiles)),
        coordinate_status_counts=dict(collections.Counter(item["coordinate_status"] for item in geo_profiles)),
    )
    def has_coordinates(hotel: dict[str, Any]) -> bool:
        value = (hotel.get("location") or {}).get("coordinates")
        if not isinstance(value, dict):
            return False
        try:
            latitude, longitude = float(value.get("lat")), float(value.get("lng"))
        except (TypeError, ValueError):
            return False
        return math.isfinite(latitude) and math.isfinite(longitude) \
            and -90 <= latitude <= 90 and -180 <= longitude <= 180
    result["coordinate_audit"] = {
        "offset": start,
        "limit": len(batch),
        "processed": len(batch),
        "total": len(hotels),
        "coordinates": sum(has_coordinates(hotel) for hotel in hotels),
        "status_counts": dict(collections.Counter(
            str((hotel.get("location") or {}).get("coordinate_status") or "PENDING")
            for hotel in hotels
        )),
        "failures": [
            {
                "hotel_key": hotel.get("canonical_hotel_id") or
                f"{hotel.get('source')}:{hotel.get('source_hotel_id')}",
                "name": hotel.get("name"),
                "status": (hotel.get("location") or {}).get("coordinate_status"),
                "reason": (hotel.get("location") or {}).get("coordinate_reason"),
                "attempts": (hotel.get("location") or {}).get("coordinate_attempts") or [],
            }
            for hotel in hotels
            if not has_coordinates(hotel)
        ],
        "composition_unchanged": stable_before == stability_snapshot(),
    }
    log_search_timeline(search_id, "geo_batch_worker_terminal", offset=start, size=len(batch),
                        work_size=len(work_batch), cache_terminal=len(cached_terminal),
                        elapsed_ms=round((time.perf_counter() - batch_started) * 1000, 1),
                        terminal=len(batch), with_coordinates=result["coordinate_audit"]["coordinates"])
    if not result["coordinate_audit"]["composition_unchanged"]:
        raise RuntimeError("Coordinate enrichment changed hotel identity, offers, matching, or ranking")
