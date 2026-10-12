"""OSM feature caching, geometry and nearest verified beach calculations."""

from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException as HTTPClientException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import httpx

from ..models import utc_now
from ..runtime import (_ACTIVE_ENRICHMENT_PROFILE, _DEFAULT_URLOPEN,
                       MAX_RESPONSE_BYTES, _profiled_sqlite_connect,
                       _profiled_urlopen, _read_http_body_bounded,
                       _record_cache, _safe_json_loads, log)
from .city import _persist_detected_beach_profile, _read_city_geo_profile
from .core import (_coordinate_pair, _geometry_distance_m, _haversine_m,
                   _hotel_beach_cache_key)
from .providers import _beach_geometry

OVERPASS_PRIMARY_URL = "https://overpass-api.de/api/interpreter"

OVERPASS_FALLBACK_URL = "https://overpass.private.coffee/api/interpreter"

GEO_FEATURE_TILE_LOCKS: dict[str, threading.Lock] = {}

GEO_FEATURE_TILE_LOCKS_GUARD = threading.Lock()

_OSM_HTTP_CLIENT = httpx.Client(
    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=20),
    timeout=httpx.Timeout(3.0, connect=1.5, read=3.0, write=1.5, pool=1.5),
)

# Beach and nearby lookups may be scheduled together; serialize their shared
# public Overpass traffic to avoid concurrent queries overloading the dispatcher.
_OSM_REQUEST_LIMITER = threading.BoundedSemaphore(1)
_OVERPASS_REQUEST_SCHEDULE_LOCK = threading.Lock()
_OVERPASS_MIN_START_INTERVAL_SECONDS = 1.5
_OVERPASS_DEFAULT_429_COOLDOWN_SECONDS = 30.0
_OVERPASS_DEFAULT_FAILURE_COOLDOWN_SECONDS = 15.0
_OVERPASS_MAX_FAILURE_COOLDOWN_SECONDS = 120.0
_OVERPASS_NEXT_START_AT = 0.0
_OVERPASS_COOLDOWN_UNTIL = 0.0
_OVERPASS_ENDPOINT_COOLDOWNS: dict[str, float] = {}
_OVERPASS_ENDPOINT_FAILURE_STREAKS: dict[str, int] = {}


class _OverpassRequestDeferred(TimeoutError):
    """Local pacing/cooldown prevented a request from being sent."""


def _overpass_retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    response_headers = getattr(response, "headers", None)
    headers = response_headers or getattr(exc, "headers", None)
    if not headers:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _overpass_reserve_request_start(deadline_at: float, stage: str) -> None:
    global _OVERPASS_NEXT_START_AT
    while True:
        with _OVERPASS_REQUEST_SCHEDULE_LOCK:
            now = time.monotonic()
            allowed_at = max(_OVERPASS_NEXT_START_AT, _OVERPASS_COOLDOWN_UNTIL)
            wait_seconds = max(0.0, allowed_at - now)
            if wait_seconds <= 0.0:
                _OVERPASS_NEXT_START_AT = now + _OVERPASS_MIN_START_INTERVAL_SECONDS
                return
            if wait_seconds > max(0.0, deadline_at - now):
                reason = "rate-limit cooldown" if _OVERPASS_COOLDOWN_UNTIL >= _OVERPASS_NEXT_START_AT else "request pacing"
                raise _OverpassRequestDeferred(f"Overpass {reason} exceeds wait deadline for {stage}")
        # Sleep outside the lock so a concurrent 429 can extend the cooldown
        # before this waiting request reserves a slot.
        time.sleep(wait_seconds)


def _overpass_apply_rate_limit_cooldown(exc: Exception, stage: str,
                                        endpoint: str | None = None) -> None:
    global _OVERPASS_COOLDOWN_UNTIL
    retry_after = _overpass_retry_after_seconds(exc)
    cooldown_seconds = max(_OVERPASS_DEFAULT_429_COOLDOWN_SECONDS, retry_after or 0.0)
    cooldown_until = time.monotonic() + cooldown_seconds
    with _OVERPASS_REQUEST_SCHEDULE_LOCK:
        _OVERPASS_COOLDOWN_UNTIL = max(_OVERPASS_COOLDOWN_UNTIL, cooldown_until)
        if endpoint:
            _OVERPASS_ENDPOINT_COOLDOWNS[endpoint] = max(
                _OVERPASS_ENDPOINT_COOLDOWNS.get(endpoint, 0.0), cooldown_until,
            )
            _OVERPASS_ENDPOINT_FAILURE_STREAKS[endpoint] = 0
    log.warning("[ENRICH] %s upstream_http=429 rate_limited cooldown=%.0fs retry_after=%s",
                stage, cooldown_seconds, "unknown" if retry_after is None else f"{retry_after:.0f}s")


def _overpass_retryable_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _overpass_is_retryable(exc: Exception) -> bool:
    status = _overpass_retryable_status(exc)
    if status is not None:
        return status in {502, 503, 504}
    return isinstance(exc, (TimeoutError, URLError, OSError, HTTPClientException,
                            httpx.TimeoutException, httpx.NetworkError))


def _overpass_apply_endpoint_cooldown(endpoint: str, exc: Exception, stage: str) -> float:
    """Temporarily skip an instance after bounded retryable upstream failures."""
    status = _overpass_retryable_status(exc)
    retry_after = _overpass_retry_after_seconds(exc)
    with _OVERPASS_REQUEST_SCHEDULE_LOCK:
        streak = min(_OVERPASS_ENDPOINT_FAILURE_STREAKS.get(endpoint, 0) + 1, 8)
        _OVERPASS_ENDPOINT_FAILURE_STREAKS[endpoint] = streak
        base = min(_OVERPASS_DEFAULT_FAILURE_COOLDOWN_SECONDS * (2 ** (streak - 1)),
                   _OVERPASS_MAX_FAILURE_COOLDOWN_SECONDS)
        # Small jitter spreads clients after a shared public-instance outage.
        cooldown_seconds = max(base * random.uniform(0.8, 1.2), retry_after or 0.0)
        until = time.monotonic() + cooldown_seconds
        _OVERPASS_ENDPOINT_COOLDOWNS[endpoint] = max(
            _OVERPASS_ENDPOINT_COOLDOWNS.get(endpoint, 0.0), until,
        )
    log.warning("[ENRICH] %s endpoint_cooldown endpoint=%s status=%s error=%s "
                "streak=%d cooldown=%.1fs retry_after=%s",
                stage, endpoint, status or "transport", type(exc).__name__, streak,
                cooldown_seconds, "unknown" if retry_after is None else f"{retry_after:.1f}s")
    return cooldown_seconds


def _overpass_endpoint_cooldown_remaining(endpoint: str) -> float:
    with _OVERPASS_REQUEST_SCHEDULE_LOCK:
        return max(0.0, _OVERPASS_ENDPOINT_COOLDOWNS.get(endpoint, 0.0) - time.monotonic())


def _overpass_record_endpoint_success(endpoint: str) -> None:
    with _OVERPASS_REQUEST_SCHEDULE_LOCK:
        _OVERPASS_ENDPOINT_FAILURE_STREAKS.pop(endpoint, None)
        _OVERPASS_ENDPOINT_COOLDOWNS.pop(endpoint, None)

def _overpass_post_impl(url: str, data: bytes, headers: dict[str, str], stage: str,
                        max_bytes: int, deadline_seconds: float = 3.0) -> bytes:
    """Use a pooled HTTPS connection and bound both response and body time."""
    # Keep the existing urllib transport seam available to offline tests and
    # callers that deliberately inject a transport.
    if urlopen is not _DEFAULT_URLOPEN:
        request = Request(url, data=data, headers=headers)
        started = time.perf_counter()
        with _profiled_urlopen(request, timeout=deadline_seconds, stage=stage) as response:
            payload = _read_http_body_bounded(response, max_bytes, deadline_seconds)
        profile = _ACTIVE_ENRICHMENT_PROFILE.get()
        if profile:
            profile.add_stage(f"{stage}_transport", (time.perf_counter() - started) * 1000)
            profile.add_http_bytes(stage, len(payload))
        return payload
    profile = _ACTIVE_ENRICHMENT_PROFILE.get()
    started = time.perf_counter()
    trace_started = time.perf_counter()
    phase_started: dict[str, float] = {}

    def trace(event: str, _info: dict[str, Any]) -> None:
        # HTTPX exposes transport phases through its request trace extension.
        # Keep only the useful elapsed phases; this is diagnostic data and does
        # not affect the request or its timeout budget.
        phase = event.rsplit(".", 1)[-1]
        if phase == "started":
            phase_started[event.rsplit(".", 1)[0]] = time.perf_counter()
            return
        if phase not in {"complete", "failed"}:
            return
        root = event.rsplit(".", 1)[0]
        started_at = phase_started.pop(root, trace_started)
        profile = _ACTIVE_ENRICHMENT_PROFILE.get()
        if profile:
            profile.add_http_phase(stage, root, (time.perf_counter() - started_at) * 1000)

    response = _OSM_HTTP_CLIENT.stream(
        "POST", url, content=data, headers=headers,
        timeout=httpx.Timeout(deadline_seconds, connect=min(2.0, deadline_seconds),
                              read=deadline_seconds, write=min(4.0, deadline_seconds), pool=4.0),
        extensions={"trace": trace},
    )
    try:
        with response as streamed:
            if profile:
                profile.add_http_open(stage, (time.perf_counter() - started) * 1000)
            if streamed.status_code >= 400:
                diagnostic = b"".join(streamed.iter_bytes(8192))[:1800].decode("utf-8", "replace")
                log.warning("[ENRICH] %s upstream_http=%d body=%s", stage, streamed.status_code,
                            re.sub(r"\s+", " ", diagnostic)[:1500])
            streamed.raise_for_status()
            deadline = time.monotonic() + deadline_seconds
            body = bytearray()
            chunks = iter(streamed.iter_bytes(64 * 1024))
            while True:
                read_started = time.perf_counter()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                finally:
                    if profile:
                        profile.add_http_read(stage, (time.perf_counter() - read_started) * 1000)
                if time.monotonic() > deadline:
                    raise TimeoutError("Overpass response exceeded wall deadline")
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError("Overpass response exceeded size limit")
            payload = bytes(body)
            if profile:
                profile.add_http_bytes(stage, len(payload))
            return payload
    except Exception:
        # Include connection and response-read time in diagnostics even when
        # httpx raises before returning a complete body.
        if profile and profile.http_open_ms.get(stage, 0.0) == 0.0:
            profile.add_http_open(stage, (time.perf_counter() - started) * 1000)
        raise

def _overpass_post(url: str, data: bytes, headers: dict[str, str], stage: str,
                   max_bytes: int, deadline_seconds: float = 3.0) -> bytes:
    wait_deadline = time.monotonic() + deadline_seconds
    if not _OSM_REQUEST_LIMITER.acquire(timeout=max(0.0, wait_deadline - time.monotonic())):
        raise _OverpassRequestDeferred("Overpass concurrency limit wait exceeded deadline")
    try:
        endpoints = [url]
        if url == OVERPASS_PRIMARY_URL:
            endpoints.append(OVERPASS_FALLBACK_URL)
        available_endpoints = []
        for endpoint in endpoints:
            cooldown_remaining = _overpass_endpoint_cooldown_remaining(endpoint)
            if cooldown_remaining > 0:
                log.info("[ENRICH] %s endpoint_cooldown_skip endpoint=%s remaining=%.1fs",
                         stage, endpoint, cooldown_remaining)
            else:
                available_endpoints.append(endpoint)
        if not available_endpoints:
            raise _OverpassRequestDeferred(
                f"all configured Overpass endpoints are cooling down for {stage}"
            )
        last_error: Exception | None = None
        for index, endpoint in enumerate(available_endpoints):
            try:
                _overpass_reserve_request_start(wait_deadline, stage)
                if endpoint != url:
                    log.warning("[ENRICH] %s fallback_endpoint=%s", stage, endpoint)
                profile = _ACTIVE_ENRICHMENT_PROFILE.get()
                if profile:
                    profile.add_http_attempt()
                payload = _overpass_post_impl(endpoint, data, headers, stage, max_bytes, deadline_seconds)
                _overpass_record_endpoint_success(endpoint)
                return payload
            except Exception as exc:
                last_error = exc
                status = _overpass_retryable_status(exc)
                if status is not None and int(status) == 429:
                    _overpass_apply_rate_limit_cooldown(exc, stage, endpoint)
                    raise
                if isinstance(exc, _OverpassRequestDeferred):
                    raise
                # Do not retry client errors, malformed queries, or oversized
                # responses against another public instance.
                if not _overpass_is_retryable(exc):
                    raise
                _overpass_apply_endpoint_cooldown(endpoint, exc, stage)
                if index + 1 < len(available_endpoints):
                    log.warning("[ENRICH] %s endpoint_failed=%s error=%s status=%s; trying one fallback",
                                stage, endpoint, type(exc).__name__, status)
                    continue
                raise
        assert last_error is not None
        raise last_error
    finally:
        _OSM_REQUEST_LIMITER.release()

def _osm_nearby_features(database_path: str | Path,
                         coordinates: dict[str, Any]) -> dict[str, Any]:
    """Fetch/cache generic nearby POI candidates once per small OSM tile."""
    pair = _coordinate_pair(coordinates)
    if not pair:
        return {"status": "coordinate_missing", "places": {}}
    tile_key, center_lat, center_lon = _overpass_nearby_tile_key(pair["lat"], pair["lng"])
    with GEO_FEATURE_TILE_LOCKS_GUARD:
        tile_lock = GEO_FEATURE_TILE_LOCKS.setdefault(tile_key, threading.Lock())
    with tile_lock:
        now = datetime.now(timezone.utc)
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            cached = connection.execute(
                "SELECT features_json,source,status,retrieved_at,diagnostic_json FROM hotel_geo_feature_cache WHERE tile_key=?",
                (tile_key,),
            ).fetchone()
        features = None
        if cached:
            try:
                age = now - datetime.fromisoformat(cached[3]).astimezone(timezone.utc)
            except (TypeError, ValueError):
                age = timedelta.max
            ttl = timedelta(days=30) if cached[2] == "ok" else (
                timedelta(days=1) if cached[2] == "not_found" else timedelta(minutes=1))
            if age < ttl:
                _record_cache("city_geo_features", True)
                if cached[2] == "ok":
                    decoded = _safe_json_loads(cached[0], None) if cached[0] else None
                    if isinstance(decoded, list):
                        features = decoded
                    else:
                        # A corrupt successful cache entry must be retried,
                        # rather than being misreported as a valid empty tile.
                        _record_cache("city_geo_features", False)
                elif cached[2] == "not_found":
                    return {"status": "not_found", "source": cached[1], "places": {}}
                else:
                    # Keep the cache's internal error value so its short TTL
                    # triggers a retry, but expose the honest terminal outcome
                    # to callers instead of leaving a generic error/pending state.
                    return {"status": "source_unavailable", "source": cached[1], "places": {}}
        _record_cache("city_geo_features", False)

        source = "openstreetmap_overpass"
        if features is None:
            around = f"(around:8000,{center_lat:.6f},{center_lon:.6f})"
            query = ("[out:json][timeout:15];("
                     f"nwr[\"railway\"=\"subway_entrance\"]{around};"
                     f"nwr[\"station\"=\"subway\"]{around};"
                     f"nwr[\"aeroway\"~\"^(aerodrome|terminal)$\"]{around};"
                     f"nwr[\"tourism\"~\"^(attraction|museum)$\"]{around};"
                     f"nwr[\"historic\"~\"^(castle|fort|monument|memorial|archaeological_site)$\"]{around};"
                     ");out center tags;")
            request = Request(
                "https://overpass-api.de/api/interpreter",
                data=urlencode({"data": query}).encode("utf-8"),
                headers={"User-Agent": "HotelMetaSearch/0.1 (nearby hotel geography)",
                         "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            try:
                body = _overpass_post(
                    "https://overpass-api.de/api/interpreter",
                    urlencode({"data": query}).encode("utf-8"),
                    {"User-Agent": "HotelMetaSearch/0.1 (nearby hotel geography)",
                     "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                    "nearby_overpass", 4 * 1024 * 1024,
                )
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("Nearby OSM response exceeded size limit")
                payload = json.loads(body)
                elements = payload.get("elements") if isinstance(payload, dict) else None
                if not isinstance(elements, list):
                    raise ValueError("Nearby OSM response did not contain elements")
                collected: dict[tuple[str, str], dict[str, Any]] = {}
                for element in elements:
                    if not isinstance(element, dict):
                        continue
                    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
                    railway = str(tags.get("railway") or "").casefold()
                    station = str(tags.get("station") or "").casefold()
                    aeroway = str(tags.get("aeroway") or "").casefold()
                    tourism = str(tags.get("tourism") or "").casefold()
                    historic = str(tags.get("historic") or "").casefold()
                    amenity = str(tags.get("amenity") or "").casefold()
                    shop = str(tags.get("shop") or "").casefold()
                    categories = []
                    if railway == "subway_entrance" or station == "subway":
                        categories.append("metro")
                    if aeroway in {"aerodrome", "terminal"}:
                        categories.append("airport")
                    if tourism in {"attraction", "museum"} or historic:
                        categories.append("landmark")
                    # Keep parsing these categories for cached/fixture data;
                    # the production query is intentionally narrower and no
                    # longer requests them from Overpass.
                    if amenity in {"restaurant", "cafe"}:
                        categories.append("restaurant")
                    if shop in {"mall", "department_store"}:
                        categories.append("shopping")
                    if not categories:
                        continue
                    lat = element.get("lat")
                    lon = element.get("lon")
                    if lat is None or lon is None:
                        center = element.get("center") if isinstance(element.get("center"), dict) else {}
                        lat, lon = center.get("lat"), center.get("lon")
                    point = _coordinate_pair({"lat": lat, "lng": lon})
                    if not point:
                        continue
                    if _haversine_m(center_lat, center_lon, point["lat"], point["lng"]) > 11_000:
                        continue
                    element_id = str(element.get("id") or "")
                    element_type = str(element.get("type") or "")
                    if not element_id or not element_type:
                        continue
                    for category in categories:
                        collected[(element_type, element_id, category)] = {
                            "name": " ".join(str(tags.get("name") or "").split()) or None,
                            "type": category, "latitude": point["lat"], "longitude": point["lng"],
                            "source": source, "osm_type": element_type, "osm_id": element_id,
                            "tags": {key: tags[key] for key in
                                     ("railway", "station", "aeroway", "tourism", "historic", "amenity", "shop")
                                     if key in tags},
                        }
                features = list(collected.values())
                status = "ok" if features else "not_found"
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError,
                    HTTPClientException, httpx.HTTPError) as exc:
                diagnostic = {
                    "query_type": "nearby_poi_features", "exception": type(exc).__name__,
                    "message": str(exc)[:500], "timeout_stage": "transport" if isinstance(
                        exc, (TimeoutError, httpx.TimeoutException)) else None,
                    "http_status": getattr(getattr(exc, "response", None), "status_code", None),
                    "retrieved_at": utc_now(),
                }
                status, features = "error", None
            else:
                diagnostic = {
                    "query_type": "nearby_poi_features", "objects": len(elements),
                    "features": len(features), "retrieved_at": utc_now(),
                }
            with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO hotel_geo_feature_cache "
                    "(tile_key,features_json,source,retrieved_at,status,diagnostic_json) VALUES (?,?,?,?,?,?)",
                    (tile_key, json.dumps(features, ensure_ascii=False) if features is not None else None,
                     source, utc_now(), status, json.dumps(diagnostic, ensure_ascii=False)),
                )
                connection.commit()
            if features is None:
                public_status = "source_unavailable" if status == "error" else status
                return {"status": public_status, "source": source, "places": {}}
            if not features:
                return {"status": "not_found", "source": source, "places": {}}

    nearest: dict[str, dict[str, Any]] = {}
    for feature in features:
        distance = _haversine_m(pair["lat"], pair["lng"], feature["latitude"], feature["longitude"])
        category = feature["type"]
        candidate = {**feature, "distance_meters": round(distance, 1),
                     "distance_km": round(distance / 1000, 3), "method": "haversine_osm_feature"}
        if category not in nearest or distance < nearest[category]["distance_meters"]:
            nearest[category] = candidate
    return {"status": "ok", "source": source, "places": nearest}

def _overpass_tile_key(latitude: float, longitude: float) -> tuple[str, float, float]:
    """A reusable OSM query tile shared by nearby hotels."""
    # Beach geometry is requested in a 3 km radius.  A 0.02 degree tile keeps
    # the tile centre within that radius for hotels throughout the tile, so a
    # city search reuses one response instead of querying once per card.
    step = 0.02
    lat_index, lon_index = math.floor(latitude / step), math.floor(longitude / step)
    return (f"osm-beach-tile-v7:{lat_index}:{lon_index}",
            (lat_index + 0.5) * step, (lon_index + 0.5) * step)

def _overpass_nearby_tile_key(latitude: float, longitude: float) -> tuple[str, float, float]:
    """Return a wider reusable tile for optional nearby POIs."""
    step = 0.1
    lat_index, lon_index = math.floor(latitude / step), math.floor(longitude / step)
    return (f"osm-nearby-tile-v2:{lat_index}:{lon_index}",
            (lat_index + 0.5) * step, (lon_index + 0.5) * step)

def _overpass_legacy_tile_key(latitude: float, longitude: float) -> str:
    """Key used by the pre-v4 0.005 degree mixed nearby/beach cache."""
    step = 0.005
    lat_index, lon_index = math.floor(latitude / step), math.floor(longitude / step)
    return f"osm-beach-tile-v3:{lat_index}:{lon_index}"

def _overpass_element_feature(element: dict[str, Any]) -> dict[str, Any] | None:
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    natural = str(tags.get("natural") or "").casefold()
    if natural not in {"beach", "coastline"}:
        return None
    osm_type = {"NODE": "N", "WAY": "W", "RELATION": "R"}.get(
        str(element.get("type") or "").upper(), str(element.get("type") or "").upper())
    osm_id = str(element.get("id") or "")
    if osm_type not in {"N", "W", "R"} or not osm_id.isdigit():
        return None

    def line_geometry(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, list):
            return None
        coordinates = []
        for node in raw:
            if not isinstance(node, dict):
                continue
            try:
                lon, lat = float(node["lon"]), float(node["lat"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
                coordinates.append([lon, lat])
        if not coordinates:
            return None
        if len(coordinates) == 1:
            return {"type": "Point", "coordinates": coordinates[0]}
        if natural == "beach" and len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
            return {"type": "Polygon", "coordinates": [coordinates]}
        return {"type": "LineString", "coordinates": coordinates}

    if osm_type == "N":
        try:
            geometry = {"type": "Point", "coordinates": [float(element["lon"]), float(element["lat"])]}
        except (KeyError, TypeError, ValueError):
            return None
    elif osm_type == "W":
        geometry = line_geometry(element.get("geometry"))
    else:
        members = element.get("members") if isinstance(element.get("members"), list) else []
        outer_rings = []
        fallback_geometries = []
        for member in members:
            if not isinstance(member, dict) or member.get("type") != "way":
                continue
            feature_geometry = line_geometry(member.get("geometry"))
            if not feature_geometry:
                continue
            coords = feature_geometry.get("coordinates")
            if (natural == "beach" and member.get("role") == "outer"
                    and feature_geometry.get("type") == "LineString"
                    and len(coords) >= 4 and coords[0] == coords[-1]):
                outer_rings.append([coords])
            else:
                fallback_geometries.append(feature_geometry)
        if outer_rings:
            geometry = ({"type": "Polygon", "coordinates": outer_rings[0]}
                        if len(outer_rings) == 1 else
                        {"type": "MultiPolygon", "coordinates": outer_rings})
        elif fallback_geometries:
            geometry = {"type": "GeometryCollection", "geometries": fallback_geometries}
        else:
            geometry = None
    if not geometry:
        return None
    return {"osm_type": osm_type, "osm_id": osm_id, "name": tags.get("name"),
            "tags": {key: tags[key] for key in ("natural", "leisure", "name", "surface", "access")
                     if key in tags}, "geometry": geometry}

def _overpass_beach_features(database_path: str | Path, latitude: float,
                             longitude: float) -> tuple[list[dict[str, Any]] | None, str]:
    # Neighbouring hotels share a tile. Wait for its first request and then
    # reuse the stored result, including a bounded terminal error, instead of
    # issuing the same Overpass query concurrently for every hotel.
    tile_key, _, _ = _overpass_tile_key(latitude, longitude)
    lock_key = f"{Path(database_path).resolve()}:{tile_key}"
    with GEO_FEATURE_TILE_LOCKS_GUARD:
        tile_lock = GEO_FEATURE_TILE_LOCKS.setdefault(lock_key, threading.Lock())
    with tile_lock:
        return _overpass_beach_features_unlocked(database_path, latitude, longitude)

def _overpass_beach_features_unlocked(database_path: str | Path, latitude: float,
                                     longitude: float) -> tuple[list[dict[str, Any]] | None, str]:
    tile_key, center_lat, center_lon = _overpass_tile_key(latitude, longitude)
    now = datetime.now(timezone.utc)
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        cached = connection.execute(
            "SELECT features_json,source,status,retrieved_at,diagnostic_json FROM hotel_beach_search_cache "
            "WHERE tile_key=? "
            "ORDER BY retrieved_at DESC LIMIT 1",
            (tile_key,),
        ).fetchone()
    if cached:
        try:
            age = now - datetime.fromisoformat(cached[3]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            age = timedelta.max
        ttl = timedelta(days=30) if cached[2] == "ok" else timedelta(days=1) if cached[2] == "not_found" else timedelta(minutes=1)
        if age < ttl:
            _record_cache("city_beach_geometry", True)
            if cached[2] in {"ok", "not_found"}:
                decoded = _safe_json_loads(cached[0], None) if cached[0] else []
                if isinstance(decoded, list):
                    # v7 cache entries are beach-only, but keep the guard so a
                    # manually restored mixed cache cannot reintroduce coastline.
                    return [feature for feature in decoded
                            if isinstance(feature, dict)
                            and str((feature.get("tags") or {}).get("natural") or "").casefold() == "beach"], cached[1]
                _record_cache("city_beach_geometry", False)
            else:
                return None, cached[1]
    _record_cache("city_beach_geometry", False)

    source = "openstreetmap_overpass"
    collected: dict[tuple[str, str], dict[str, Any]] = {}
    query_specs = (
        ("natural", "beach", "node", 3000),
        ("natural", "beach", "way", 3000),
        ("natural", "beach", "relation", 3000),
    )
    # Only request features that can contribute to the measured distance.
    # Coastlines are deliberately excluded by the selector below and only add
    # geometry volume and Overpass work.
    clauses = ";".join(
        f'{osm_kind}["{tag_key}"="{tag_value}"]'
        f'(around:{radius},{center_lat:.6f},{center_lon:.6f})'
        for tag_key, tag_value, osm_kind, radius in query_specs
    ) + ";"
    query = f"[out:json][timeout:12];({clauses});out geom tags;"
    request = Request(
        "https://overpass-api.de/api/interpreter",
        data=urlencode({"data": query}).encode("utf-8"),
        headers={"User-Agent": "HotelMetaSearch/0.1 (OSM beach geometry)",
                 "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    diagnostic: dict[str, Any]
    try:
        payload_bytes = _overpass_post(
            "https://overpass-api.de/api/interpreter",
            urlencode({"data": query}).encode("utf-8"),
            {"User-Agent": "HotelMetaSearch/0.1 (OSM beach geometry)",
             "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            # Keep the OSM request itself bounded; the caller's hotel worker
            # remains occupied until this result or its terminal source status.
            "beach_overpass", MAX_RESPONSE_BYTES, deadline_seconds=12.0,
        )
        if len(payload_bytes) > MAX_RESPONSE_BYTES:
            raise ValueError("Overpass feature response exceeded size limit")
        payload = json.loads(payload_bytes)
        elements = payload.get("elements") if isinstance(payload, dict) else None
        if not isinstance(elements, list):
            raise ValueError("Overpass response did not contain elements")
        for item in elements:
            if isinstance(item, dict):
                feature = _overpass_element_feature(item)
                if feature:
                    collected[(feature["osm_type"], feature["osm_id"])] = feature
        features: list[dict[str, Any]] | None = list(collected.values())
        diagnostic = {
            "query_type": "beach_geometry", "objects": len(elements),
            "features": len(features), "retrieved_at": utc_now(),
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError,
            HTTPClientException, httpx.HTTPError) as exc:
        features = None
        diagnostic = {
            "query_type": "beach_geometry", "exception": type(exc).__name__,
            "message": str(exc)[:500], "timeout_stage": "transport" if isinstance(
                exc, (TimeoutError, httpx.TimeoutException)) else None,
            "http_status": getattr(getattr(exc, "response", None), "status_code", None),
            "retrieved_at": utc_now(),
        }
    status = "ok" if features else "not_found" if features == [] else "error"

    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO hotel_beach_search_cache(tile_key,features_json,source,retrieved_at,status,diagnostic_json) "
            "VALUES (?,?,?,?,?,?)",
            (tile_key, json.dumps(features, ensure_ascii=False) if features is not None else None,
             source, utc_now(), status, json.dumps(diagnostic, ensure_ascii=False)),
        )
        connection.commit()
    return features, source

def _resolve_nearest_beach(database_path: str | Path, coordinates: dict[str, Any],
                           city: str, country: str | None,
                           city_slug: str | None = None,
                           country_slug: str | None = None,
                           return_outcome: bool = False) -> dict[str, Any] | None:
    pair = _coordinate_pair(coordinates)
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        profile = _read_city_geo_profile(connection, city, country, city_slug, country_slug)
    beach_relevant = profile.is_beach_destination if profile else None

    def outcome(available: bool | None, status: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        public_status = ("available" if selected or status == "ok" else
                         "no_coordinates" if status in {"coordinate_missing", "coordinate_invalid", "no_coordinates"} else
                         "not_found" if status in {"not_found", "out_of_range"} else
                         "source_unavailable" if status in {"error", "source_unavailable", "timeout", "rate_limited"} else
                         "unknown")
        value = {
            "beach_relevant": beach_relevant,
            "beach_geometry_available": available,
            "status": status,
            "public_status": public_status,
        }
        if selected:
            value.update(selected)
        return value

    if not pair or not city:
        value = outcome(None, "coordinate_missing")
        return value if return_outcome else None
    cache_key = _hotel_beach_cache_key(pair, city, country)
    if not cache_key:
        value = outcome(None, "coordinate_invalid")
        return value if return_outcome else None

    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        cached = connection.execute(
            "SELECT distance_meters,beach_name,source,status,retrieved_at,beach_osm_type,"
            "beach_osm_id,beach_method,beach_confidence FROM hotel_beach_cache "
            "WHERE (cache_key=? AND (source!='openstreetmap_photon_geometry' OR status!='ok') "
            "AND COALESCE(beach_method,'')!='nearest_osm_beach_point') "
            "OR (cache_key IN (?,?) AND status='ok' "
            "AND source!='openstreetmap_photon_geometry' "
            "AND beach_method='nearest_osm_beach_geometry') "
            "ORDER BY CASE WHEN cache_key=? THEN 0 WHEN cache_key=? THEN 1 ELSE 2 END, "
            "retrieved_at DESC LIMIT 1",
            (cache_key,
             _hotel_beach_cache_key(pair, city, country, "osm-beach-geometry-v14"),
             _hotel_beach_cache_key(pair, city, country, "osm-beach-geometry-v11"),
             cache_key,
             _hotel_beach_cache_key(pair, city, country, "osm-beach-geometry-v14")),
        ).fetchone()
    if cached:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached[4]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            age = timedelta.max
        ttl = timedelta(days=30) if cached[3] in {"ok", "out_of_range"} else (
            timedelta(days=1) if cached[3] == "not_found" else timedelta(minutes=1))
        if age < ttl:
            _record_cache("hotel_beach_distance", True)
            if (cached[3] == "ok" and cached[0] is not None
                    and cached[7] in {"nearest_osm_beach_geometry", "nearest_osm_beach_point"}
                    and (profile is None or not profile.is_beach_destination)):
                profile = _persist_detected_beach_profile(
                    database_path, city, country, city_slug, country_slug,
                )
                beach_relevant = True
            available = cached[3] == "ok" and cached[0] is not None
            selected = None
            if available:
                selected = {
                    "distance_meters": cached[0], "name": cached[1], "source": cached[2],
                    "osm_type": cached[5], "osm_id": cached[6], "method": cached[7],
                    "confidence": cached[8],
                }
            legacy_status = "ok" if selected else cached[3]
            value = outcome(available if selected else (False if legacy_status in {"not_found", "out_of_range"} else None), legacy_status, selected)
            return value if return_outcome else selected
    _record_cache("hotel_beach_distance", False)

    features, feature_source = _overpass_beach_features(database_path, pair["lat"], pair["lng"])
    overpass_rate_limited = False
    overpass_source_failed = features is None
    if overpass_source_failed:
        # Preserve the exact upstream reason from the tile diagnostic. A 429
        # must not trigger another Overpass endpoint or an immediate burst.
        tile_key, _, _ = _overpass_tile_key(pair["lat"], pair["lng"])
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            diagnostic_row = connection.execute(
                "SELECT diagnostic_json FROM hotel_beach_search_cache WHERE tile_key=?",
                (tile_key,),
            ).fetchone()
        diagnostic = _safe_json_loads(diagnostic_row[0], {}) if diagnostic_row else {}
        overpass_rate_limited = (isinstance(diagnostic, dict)
                                 and diagnostic.get("http_status") == 429)
        features = []

    photon_attempted = False
    photon_failed = False
    photon_nearest_unverified = False
    # Photon is a bounded fallback for transport/server failures. On 429 we
    # keep the public status source_unavailable and wait for the cooldown/cache
    # instead of using another upstream path as a rate-limit bypass.
    if not overpass_rate_limited and not features:
        photon_attempted = True
        search_city = " ".join(re.split(r"[-_]+", city_slug or "")).strip() or city
        search_country = " ".join(re.split(r"[-_]+", country_slug or "")).strip() or (country or "")
        params = {"q": " ".join(part for part in ("beach", search_city, search_country) if part),
                  "osm_tag": "natural:beach", "limit": 40, "lat": pair["lat"], "lon": pair["lng"]}
        request = Request(
            f"https://photon.komoot.io/api/?{urlencode(params, doseq=True)}",
            headers={"User-Agent": "HotelMetaSearch/0.1 (verified beach distance)", "Accept": "application/json"},
        )
        try:
            with _profiled_urlopen(request, timeout=8, stage="beach_photon") as response:
                payload = json.loads(response.read(3 * 1024 * 1024))
            photon_succeeded = isinstance(payload, dict) and isinstance(payload.get("features"), list)
            if photon_succeeded:
                feature_source = "openstreetmap_photon_geometry"
            # Photon is name search, not a complete local beach inventory. Even
            # exact geometry for one returned candidate cannot prove that it is
            # the nearest shore, so it must never back a public distance value.
            photon_nearest_unverified = photon_succeeded
            if not photon_succeeded:
                photon_failed = True
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
            photon_failed = True
            features = []

    beach_candidates: list[tuple[float, dict[str, Any]]] = []
    for feature in features or []:
        geometry = feature.get("geometry")
        tags = feature.get("tags") or {}
        natural = str(tags.get("natural") or "").casefold()
        if not isinstance(geometry, dict):
            continue
        if feature_source == "openstreetmap_photon_geometry" and geometry.get("type") == "Point":
            continue
        exact_distance = _geometry_distance_m(geometry, pair["lat"], pair["lng"])
        if natural != "beach" or exact_distance is None or exact_distance > 25_000:
            continue
        if feature_source == "openstreetmap_photon_geometry" and exact_distance > 3_000:
            # Photon supplies named features, not a complete local coastline
            # set. Do not turn a remote named feature into a local beach.
            continue
        beach_candidates.append((exact_distance, feature))
    selected_feature = None
    distance = None
    method = None
    confidence = None
    if beach_candidates:
        distance, selected_feature = min(beach_candidates, key=lambda item: item[0])
        is_point = (selected_feature.get("geometry") or {}).get("type") == "Point"
        method = "nearest_osm_beach_point" if is_point else "nearest_osm_beach_geometry"
        confidence = "medium" if is_point else "high"
    selected = None
    if selected_feature and distance is not None:
        selected = {
            "distance_meters": distance,
            "name": selected_feature.get("name"),
            "source": feature_source,
            "osm_type": selected_feature.get("osm_type"),
            "osm_id": selected_feature.get("osm_id"),
            "method": method,
            "confidence": confidence,
        }
    if selected and (profile is None or not profile.is_beach_destination):
        # City flags are informational only. Promote a missing/negative profile
        # only after selecting a verified natural=beach feature.
        profile = _persist_detected_beach_profile(
            database_path, city, country, city_slug, country_slug,
        )
        beach_relevant = True
    if selected:
        cache_status, legacy_status, available = "ok", "ok", True
    elif overpass_rate_limited or photon_failed or (overpass_source_failed and not photon_attempted):
        cache_status, legacy_status, available = "error", "error", False
    elif photon_nearest_unverified:
        cache_status, legacy_status, available = "unknown", "unknown", None
    elif feature_source == "openstreetmap_photon_geometry" and features:
        # Keep the legacy cache status for callers that distinguish a valid
        # remote feature outside the bounded local radius; public_status maps
        # it to not_found so it cannot look like a usable beach distance.
        cache_status, legacy_status, available = "out_of_range", "out_of_range", True
    else:
        cache_status, legacy_status, available = "not_found", "not_found", False
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO hotel_beach_cache "
            "(cache_key,distance_meters,beach_name,source,retrieved_at,status,beach_osm_type,"
            "beach_osm_id,beach_method,beach_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cache_key, distance if selected else None, selected.get("name") if selected else None,
             feature_source or "openstreetmap", utc_now(), cache_status,
             selected.get("osm_type") if selected else None, selected.get("osm_id") if selected else None,
             method or ("fallback_search_incomplete" if photon_nearest_unverified else None), confidence),
        )
        connection.commit()
    value = outcome(available, legacy_status, selected)
    return value if return_outcome else selected
