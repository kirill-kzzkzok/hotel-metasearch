"""Pure coordinate, distance, and location projection helpers."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from ...hotel_matching import normalize_hotel_name
from ..models import CityReference
from ..normalization import normalize_beach_line


def _valid_coordinate_pair(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None
    return lat, lon

BEACH_LINE_DISTANCE_LIMITS_METERS = {"first": 200, "second": 500, "third": 1000}

def _hotel_coordinate_cache_key(name: str, city: str, country: str | None,
                                address: str | None = None, city_slug: str | None = None,
                                country_slug: str | None = None,
                                provider: str | None = None,
                                provider_hotel_id: str | None = None) -> str:
    slug_city = " ".join(part for part in re.split(r"[-_]+", city_slug or "")
                         if part.isascii() and part.isalnum())
    slug_country = " ".join(part for part in re.split(r"[-_]+", country_slug or "")
                            if part.isascii() and part.isalnum())
    location_city = slug_city or city
    location_country = slug_country or country
    has_identity = provider or provider_hotel_id is not None
    identity = (
        normalize_hotel_name(provider or ""),
        str(provider_hotel_id or ""),
    ) if has_identity else ("", "")
    return hashlib.sha256(
        "|".join(("verified-geocoder-v20" if has_identity else "verified-geocoder-v19", *identity,
                  normalize_hotel_name(name),
                  normalize_hotel_name(address or ""), normalize_hotel_name(location_city),
                  normalize_hotel_name(location_country or ""))).encode("utf-8")
    ).hexdigest()

def _coordinate_pair(value: Any) -> dict[str, float] | None:
    if isinstance(value, dict):
        latitude = value.get("lat", value.get("latitude"))
        longitude = value.get("lng", value.get("lon", value.get("longitude")))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        latitude, longitude = value[0], value[1]
    else:
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if latitude == 0 and longitude == 0:
        return None
    return {"lat": latitude, "lng": longitude}

def _coordinate_source_metadata(source: str | None) -> tuple[str, str]:
    """Map a verified source label to stable API metadata."""
    normalized = str(source or "").casefold()
    if normalized.endswith("_detail"):
        return "hotel_detail_structured_data", "high"
    if normalized in {"photon", "nominatim", "verified_geocoder"}:
        return "address_geocoding", "medium"
    return "provider_coordinates", "high"

def _coordinate_source_priority(source: str | None, confidence: str | None = None) -> int:
    method, default_confidence = _coordinate_source_metadata(source)
    resolved_confidence = str(confidence or default_confidence).casefold()
    return {
        ("provider_coordinates", "high"): 4,
        ("hotel_detail_structured_data", "high"): 3,
        ("address_geocoding", "high"): 2,
        ("address_geocoding", "medium"): 1,
    }.get((method, resolved_confidence), 0)

def _coordinate_distance_m(left: Any, right: Any) -> float | None:
    first, second = _coordinate_pair(left), _coordinate_pair(right)
    if not first or not second:
        return None
    return _haversine_m(first["lat"], first["lng"], second["lat"], second["lng"])

def _hotel_beach_cache_key(coordinates: dict[str, float], city: str, country: str | None,
                           version: str = "osm-beach-geometry-v15") -> str:
    pair = _coordinate_pair(coordinates)
    if not pair:
        return ""
    material = "|".join((version, f"{pair['lat']:.6f}", f"{pair['lng']:.6f}",
                         normalize_hotel_name(city), normalize_hotel_name(country or "")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi, delta_lambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))

def _project_local(lon: float, lat: float, origin_lon: float, origin_lat: float) -> tuple[float, float]:
    radius = 6_371_008.8
    return (radius * math.radians(lon - origin_lon) * math.cos(math.radians(origin_lat)),
            radius * math.radians(lat - origin_lat))

def _point_segment_distance(point: tuple[float, float], start: tuple[float, float],
                            end: tuple[float, float]) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if not length_squared:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    scale = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared))
    return math.hypot(point[0] - start[0] - scale * dx, point[1] - start[1] - scale * dy)

def _geometry_distance_m(geometry: Any, latitude: float, longitude: float) -> float | None:
    if not isinstance(geometry, dict):
        return None
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "GeometryCollection":
        distances = [_geometry_distance_m(item, latitude, longitude)
                     for item in geometry.get("geometries", [])]
        valid = [distance for distance in distances if distance is not None]
        return min(valid) if valid else None
    origin = (longitude, latitude)
    point = (0.0, 0.0)

    def line_distance(line: Any) -> float | None:
        if not isinstance(line, list) or not line:
            return None
        projected = []
        for pair in line:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                lon, lat = float(pair[0]), float(pair[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
                projected.append(_project_local(lon, lat, *origin))
        if not projected:
            return None
        if len(projected) == 1:
            return math.hypot(*projected[0])
        return min(_point_segment_distance(point, projected[index - 1], projected[index])
                   for index in range(1, len(projected)))

    def inside_ring(ring: Any) -> bool:
        projected = []
        for pair in ring if isinstance(ring, list) else []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    projected.append(_project_local(float(pair[0]), float(pair[1]), *origin))
                except (TypeError, ValueError):
                    pass
        inside = False
        if len(projected) < 3:
            return False
        previous = projected[-1]
        for current in projected:
            if (current[1] > point[1]) != (previous[1] > point[1]):
                crossing_x = (previous[0] - current[0]) * (point[1] - current[1]) / (previous[1] - current[1]) + current[0]
                if point[0] < crossing_x:
                    inside = not inside
            previous = current
        return inside

    if kind == "Point" and isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
        try:
            return _haversine_m(latitude, longitude, float(coordinates[1]), float(coordinates[0]))
        except (TypeError, ValueError):
            return None
    if kind == "MultiPoint":
        values = [_geometry_distance_m({"type": "Point", "coordinates": pair}, latitude, longitude)
                  for pair in (coordinates if isinstance(coordinates, list) else [])]
        valid = [distance for distance in values if distance is not None]
        return min(valid) if valid else None
    if kind == "LineString":
        return line_distance(coordinates)
    if kind == "MultiLineString":
        values = [line_distance(line) for line in (coordinates if isinstance(coordinates, list) else [])]
    elif kind == "Polygon":
        rings = coordinates if isinstance(coordinates, list) else []
        if rings and inside_ring(rings[0]) and not any(inside_ring(ring) for ring in rings[1:]):
            return 0.0
        values = [line_distance(ring) for ring in rings]
    elif kind == "MultiPolygon":
        values = []
        for polygon in coordinates if isinstance(coordinates, list) else []:
            rings = polygon if isinstance(polygon, list) else []
            if rings and inside_ring(rings[0]) and not any(inside_ring(ring) for ring in rings[1:]):
                return 0.0
            values.extend(line_distance(ring) for ring in rings)
    else:
        return None
    valid = [distance for distance in values if distance is not None]
    return min(valid) if valid else None

def _is_enrichment_coordinate_source(source: Any) -> bool:
    value = str(source or "").casefold()
    return value in {"verified_geocoder", "photon", "nominatim"} or value.endswith("_detail")

def _public_beach_status(status: Any, *, has_distance: bool = False) -> str:
    """Expose stable beach states without leaking cache implementation values."""
    value = str(status or "").casefold()
    if has_distance:
        return "available"
    if value in {"coordinate_missing", "coordinate_invalid", "no_coordinates"}:
        return "no_coordinates"
    if value in {"not_found", "out_of_range"}:
        return "not_found"
    if value == "unknown":
        return "unknown"
    if value in {"error", "source_unavailable", "timeout", "rate_limited"}:
        return "source_unavailable"
    return "unknown"


def _location_summary(hotel: dict[str, Any], records: list[dict[str, Any]],
                      city_reference: CityReference | None = None) -> dict[str, Any]:
    center_places = [
        (record, place) for record in records for place in record.get("nearby_places", [])
        if isinstance(place, dict) and place.get("type") == "city_center"
        and isinstance(place.get("distance_meters"), (int, float))
        and math.isfinite(place["distance_meters"]) and place["distance_meters"] >= 0
    ]
    landmarks = [
        (record, place) for record in records for place in record.get("nearby_places", [])
        if isinstance(place, dict) and place.get("type") != "city_center"
        and place.get("name") and isinstance(place.get("distance_meters"), (int, float))
        and math.isfinite(place["distance_meters"]) and place["distance_meters"] >= 0
    ]
    center_record, center = min(center_places, key=lambda pair: pair[1]["distance_meters"]) \
        if center_places else (None, None)
    source_center_references = [
        {
            "source": record.get("source"),
            "name": place.get("name"),
            "source_distance_meters": round(float(place["distance_meters"]), 1),
            "source_distance_km": round(float(place["distance_meters"]) / 1000, 2),
            "method": "provider_reported",
        }
        for record, place in center_places
    ]
    landmark_record, landmark = min(landmarks, key=lambda pair: pair[1]["distance_meters"]) \
        if landmarks else (None, None)
    cached_beaches = [
        (record, {"distance_meters": record.get("cached_beach_distance_meters"),
                  "name": record.get("cached_beach_name"), "source": record.get("cached_beach_source"),
                  "osm_type": record.get("cached_beach_osm_type"),
                  "osm_id": record.get("cached_beach_osm_id"),
                  "method": record.get("cached_beach_method"),
                  "confidence": record.get("cached_beach_confidence")})
        for record in records
        if record.get("cached_beach_method") != "nearest_osm_beach_point"
        if record.get("cached_beach_source") != "openstreetmap_photon_geometry"
        if isinstance(record.get("cached_beach_distance_meters"), (int, float))
        and math.isfinite(record["cached_beach_distance_meters"])
        and record["cached_beach_distance_meters"] >= 0
    ]
    if cached_beaches:
        beach_record, beach = min(cached_beaches, key=lambda pair: pair[1]["distance_meters"])
    else:
        beach_record, beach = None, None
    nearby_geo_places: dict[str, dict[str, Any]] = {}
    for record in records:
        geo_places = record.get("cached_geo_places") if isinstance(record.get("cached_geo_places"), dict) else {}
        for category, place in geo_places.items():
            if not isinstance(place, dict) or not isinstance(place.get("distance_meters"), (int, float)):
                continue
            current = nearby_geo_places.get(category)
            if current is None or place["distance_meters"] < current["distance_meters"]:
                nearby_geo_places[category] = place

    coordinate_candidates = []
    for record in records:
        pair = _coordinate_pair(record.get("coordinates"))
        if pair:
            coordinate_candidates.append((record, pair, record.get("coordinate_source") or record.get("source")))
        geocoded = _coordinate_pair(record.get("geocoded_coordinates"))
        if geocoded:
            coordinate_candidates.append((record, geocoded,
                                           record.get("geocoded_coordinate_source") or record.get("source")))
    coordinate_conflict = None
    if len(coordinate_candidates) > 1:
        for index, (_record, pair, source) in enumerate(coordinate_candidates):
            for _other_record, other_pair, other_source in coordinate_candidates[index + 1:]:
                distance = _coordinate_distance_m(pair, other_pair)
                if distance is not None and distance > 25_000:
                    coordinate_conflict = {
                        "sources": [source, other_source],
                        "distance_meters": round(distance, 1),
                    }
                    break
            if coordinate_conflict:
                break
    if coordinate_conflict:
        coordinates = None
        coordinate_source = None
    else:
        selected = next(iter(coordinate_candidates), None)
        coordinates = selected[1] if selected else None
        coordinate_source = selected[2] if selected else None
    if coordinates is None and not coordinate_conflict:
        detail_records = [record for record in records
                          if str(record.get("geocoded_coordinate_source") or "").endswith("_detail")]
        geocoder_records = [record for record in records if record not in detail_records]
        for record in (*detail_records, *geocoder_records):
            coordinates = _coordinate_pair(record.get("geocoded_coordinates"))
            if coordinates:
                coordinate_source = record.get("geocoded_coordinate_source") or "verified_geocoder"
                break

    statuses = [str(record.get("coordinate_status") or "").upper() for record in records]
    coordinate_status = "CONFLICT" if coordinate_conflict else (
        "PROVIDER_COORDINATES" if coordinates and coordinate_source and
        _coordinate_source_metadata(coordinate_source)[0] == "provider_coordinates" else
        "PROVIDER_DETAIL" if coordinates and coordinate_source and
        _coordinate_source_metadata(coordinate_source)[0] == "hotel_detail_structured_data" else
        "GEOCODER" if coordinates else
        next((status for status in statuses if status in {"ERROR", "NOT_FOUND"}), "PENDING")
    )
    coordinate_method, coordinate_confidence = _coordinate_source_metadata(coordinate_source)
    resolution_record = next((record for record in records
                              if record.get("coordinate_reason") or record.get("coordinate_attempts")), None)
    beach_access = next(
        (str(record.get("beach_access")).casefold() for record in records
         if record.get("beach_access") and str(record.get("beach_access")).casefold() != "unknown"),
        str(hotel.get("beach_access") or "unknown").casefold(),
    )
    if beach_access not in {"private", "public_nearby", "direct", "shuttle", "none", "unknown"}:
        beach_access = "unknown"
    source_beach_line_record = next(
        (record for record in records if normalize_beach_line(record.get("beach_line")) != "unknown"), None
    )
    beach_line = normalize_beach_line(source_beach_line_record.get("beach_line")) \
        if source_beach_line_record else "unknown"
    beach_line_source = "source" if source_beach_line_record else None
    beach_line_evidence = source_beach_line_record.get("beach_line_evidence") if source_beach_line_record else None
    if beach_line == "unknown" and beach and isinstance(beach.get("distance_meters"), (int, float)):
        beach_distance_m = float(beach["distance_meters"])
        beach_line = next((line for line, max_distance in BEACH_LINE_DISTANCE_LIMITS_METERS.items()
                           if beach_distance_m <= max_distance), "unknown")
        if beach_line != "unknown":
            beach_line_source = "verified_beach_distance"
            beach_line_evidence = {"distance_meters": round(beach_distance_m, 1),
                                   "rule_thresholds_meters": BEACH_LINE_DISTANCE_LIMITS_METERS}
    calculated_center = None
    if city_reference and coordinates:
        calculated_center = _haversine_m(
            coordinates["lat"], coordinates["lng"],
            city_reference.center_lat, city_reference.center_lon,
        )
    # Prefer the canonical city point whenever configured. If a city has no
    # registered reference yet, preserve the provider value with explicit
    # provider_reported provenance so it is never mistaken for our Haversine.
    center_distance = calculated_center if calculated_center is not None else (
        center["distance_meters"] if center else None
    )
    center_source = city_reference.source if calculated_center is not None else (
        center_record.get("source") if center is not None and center_record else None
    )
    return {
        "address": hotel.get("address") or next((record.get("address") for record in records
                                                    if record.get("address")), None),
        "coordinates": coordinates,
        "coordinate_source": coordinate_source,
        "coordinate_method": coordinate_method if coordinates else None,
        "coordinate_confidence": coordinate_confidence if coordinates else None,
        "coordinate_status": coordinate_status,
        "coordinate_conflict": coordinate_conflict,
        "coordinate_reason": resolution_record.get("coordinate_reason") if resolution_record else None,
        "coordinate_attempts": resolution_record.get("coordinate_attempts", []) if resolution_record else [],
        "distance_to_center_km": round(center_distance / 1000, 2) if center_distance is not None else None,
        "distance_to_center_m": round(center_distance, 1) if center_distance is not None else None,
        "our_distance_to_center_km": round(calculated_center / 1000, 2) if calculated_center is not None else None,
        "our_distance_to_center_m": round(calculated_center, 1) if calculated_center is not None else None,
        "center_source": center_source,
        "distance_to_center_method": "haversine" if calculated_center is not None else (
            "provider_reported" if center is not None else None
        ),
        "source_center_references": source_center_references,
        "center_reference": ({"city_slug": city_reference.city_slug,
                              "country_slug": city_reference.country_slug,
                              "name": city_reference.name,
                              "lat": city_reference.center_lat,
                              "lon": city_reference.center_lon,
                              "source": city_reference.source,
                              "retrieved_at": city_reference.retrieved_at,
                              "method": city_reference.method}
                             if city_reference else None),
        "distance_to_landmark_km": round(landmark["distance_meters"] / 1000, 2) if landmark else None,
        "landmark_name": landmark.get("name") if landmark else None,
        "landmark_type": landmark.get("type") if landmark else None,
        "landmark_source": landmark_record.get("source") if landmark_record else None,
        "distance_to_beach_km": round(beach["distance_meters"] / 1000, 6) if beach else None,
        "beach_name": beach.get("name") if beach else None,
        "beach_source": beach.get("source") if beach else None,
        "distance_to_beach_source": beach.get("source") if beach else None,
        "distance_to_beach_method": beach.get("method") if beach else None,
        "distance_to_beach_confidence": beach.get("confidence") if beach else None,
        "beach_line": beach_line,
        "beach_line_source": beach_line_source,
        "beach_line_evidence": beach_line_evidence,
        "beach_reference": ({"name": beach.get("name"), "source": beach.get("source"),
                             "osm_type": beach.get("osm_type"), "osm_id": beach.get("osm_id")}
                            if beach else None),
        "beach_relevant": next((record.get("cached_beach_relevant") for record in records
                                if record.get("cached_beach_relevant") is not None), None),
        "beach_geometry_available": (
            None if any(record.get("cached_beach_source") == "openstreetmap_photon_geometry"
                        for record in records) else
            True if beach else next((record.get("cached_beach_geometry_available") for record in records
                                     if record.get("cached_beach_geometry_available") is not None), None)
        ),
        "beach_status": (
            "not_applicable" if next((record.get("cached_beach_relevant")
                                      for record in records if record.get("cached_beach_relevant") is not None), None)
            is False else _public_beach_status(
                next((record.get("cached_beach_status") for record in records
                      if record.get("cached_beach_status")), None),
                has_distance=beach is not None,
            )
        ),
        "nearby_geo_places": nearby_geo_places,
        "nearby_geo_source": next((record.get("cached_geo_source") for record in records
                                    if record.get("cached_geo_source")), None),
        "nearby_geo_status": next((record.get("cached_geo_status") for record in records
                                    if record.get("cached_geo_status")), None) or "unknown",
        "distance_to_metro_km": nearby_geo_places.get("metro", {}).get("distance_km"),
        "distance_to_airport_km": nearby_geo_places.get("airport", {}).get("distance_km"),
        # Keep provider-reported nearby references distinct from derived OSM
        # distances; the similarly named field above belongs to provider data.
        "distance_to_osm_landmark_km": nearby_geo_places.get("landmark", {}).get("distance_km"),
        "beach_access": beach_access,
        "retrieved_at": hotel.get("observed_at"),
    }
