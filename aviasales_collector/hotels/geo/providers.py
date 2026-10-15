"""Identity-checked provider and geocoder facts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException as HTTPClientException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from ...hotel_matching import _normalize_country, normalize_hotel_name
from ..models import CityReference, utc_now
from ..runtime import (_ACTIVE_ENRICHMENT_PROFILE, _profiled_sqlite_connect,
                       _profiled_urlopen, _record_cache, _safe_json_loads)
from ..schema import init_hotel_store
from .city import _read_city_reference, register_city_reference
from .core import (_coordinate_distance_m, _coordinate_pair,
                   _coordinate_source_metadata, _coordinate_source_priority,
                   _geometry_distance_m, _hotel_coordinate_cache_key)

GEOCODER_LOCK = threading.Lock()

GEOCODER_LAST_REQUEST = 0.0

PHOTON_CITY_EXTENTS: dict[tuple[str, str], tuple[float, float, float, float] | None] = {}

PHOTON_CITY_EXTENTS_LOCK = threading.Lock()

GEOCODE_REQUEST_LOCKS: dict[str, threading.Lock] = {}

GEOCODE_REQUEST_LOCKS_GUARD = threading.Lock()

PROVIDER_DETAIL_LOCKS: dict[str, threading.Lock] = {}

PROVIDER_DETAIL_LOCKS_GUARD = threading.Lock()

GEOCODE_PROPERTY_GENERIC = {
    "hotel", "hotels", "hostel", "hostels", "resort", "resorts", "spa", "the",
    "by", "collection", "beach", "villa", "villas", "inn", "suites", "suite",
    "apartments", "apartment", "residence", "residences", "guesthouse", "motel",
}

def _beach_geometry(database_path: str | Path, osm_type: str, osm_id: str) -> dict[str, Any] | None:
    normalized_type = osm_type.upper()
    if normalized_type not in {"N", "W", "R"} or not str(osm_id).isdigit():
        return None
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        cached = connection.execute(
            "SELECT geometry_json,status,retrieved_at FROM hotel_beach_feature_cache WHERE osm_type=? AND osm_id=?",
            (normalized_type, str(osm_id)),
        ).fetchone()
    if cached:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached[2]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            age = timedelta.max
        if age < timedelta(days=30):
            if cached[1] == "ok":
                decoded = _safe_json_loads(cached[0], None) if cached[0] else None
                if isinstance(decoded, dict):
                    return decoded
                # Corrupt geometry must be fetched again instead of silently
                # converting a previously verified beach into not_found.
    if normalized_type == "N":
        geometry = None
        status = "not_found"
    else:
        query = urlencode({"osmtype": normalized_type, "osmid": str(osm_id),
                           "polygon_geojson": 1, "format": "json"})
        request = Request(
            f"https://nominatim.openstreetmap.org/details?{query}",
            headers={"User-Agent": "HotelMetaSearch/0.1 (verified beach distance)", "Accept": "application/json"},
        )
        try:
            global GEOCODER_LAST_REQUEST
            with GEOCODER_LOCK:
                remaining = 1.1 - (time.monotonic() - GEOCODER_LAST_REQUEST)
                if remaining > 0:
                    time.sleep(remaining)
                GEOCODER_LAST_REQUEST = time.monotonic()
                with _profiled_urlopen(request, timeout=5, stage="beach_geometry") as response:
                    payload = json.loads(response.read(2 * 1024 * 1024))
            geometry = payload.get("geometry") if isinstance(payload, dict) else None
            status = "ok" if _geometry_distance_m(geometry, 0, 0) is not None else "not_found"
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
            return None
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO hotel_beach_feature_cache(osm_type,osm_id,name,geometry_json,retrieved_at,status) "
            "VALUES (?,?,?,?,?,?)",
            (normalized_type, str(osm_id), None, json.dumps(geometry) if geometry else None, utc_now(), status),
        )
        connection.commit()
    return geometry if status == "ok" else None

def _photon_city_extent(city: str, country: str | None) -> tuple[float, float, float, float] | None:
    key = (normalize_hotel_name(city), normalize_hotel_name(country or ""))
    with PHOTON_CITY_EXTENTS_LOCK:
        if key in PHOTON_CITY_EXTENTS:
            return PHOTON_CITY_EXTENTS[key]
    query = ", ".join(part for part in (city, country) if part)
    request = Request(
        f"https://photon.komoot.io/api/?{urlencode({'q': query, 'limit': 10})}",
        headers={"User-Agent": "HotelMetaSearch/0.1 (city extent verification)", "Accept": "application/json"},
    )
    payload = json.loads(_profiled_urlopen(request, timeout=5, stage="city_extent").read(2 * 1024 * 1024))
    extent = None
    for feature in payload.get("features", []) if isinstance(payload, dict) else []:
        properties = feature.get("properties") or {} if isinstance(feature, dict) else {}
        if not isinstance(properties, dict):
            continue
        is_place = properties.get("osm_key") == "place" and properties.get("osm_value") in {
            "city", "town", "village", "island", "municipality", "province",
        }
        is_city_boundary = properties.get("osm_key") == "boundary" and properties.get("osm_value") == "administrative"
        if not is_place and not is_city_boundary:
            continue
        bounds = properties.get("extent")
        if not isinstance(bounds, list) or len(bounds) != 4:
            continue
        try:
            west, north, east, south = map(float, bounds)
        except (TypeError, ValueError):
            continue
        if all(map(math.isfinite, (west, north, east, south))) and -180 <= west <= east <= 180 and -90 <= south <= north <= 90:
            extent = (west, south, east, north)
            break
    with PHOTON_CITY_EXTENTS_LOCK:
        PHOTON_CITY_EXTENTS[key] = extent
    return extent

def _resolve_city_reference(database_path: str | Path, city: str, country: str | None,
                            city_slug: str, country_slug: str) -> CityReference | None:
    """Resolve and persist a city place-point from Photon; never use a hotel or centroid fallback."""
    if not city or not country or not city_slug or not country_slug:
        return None
    init_hotel_store(database_path)
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        cached = _read_city_reference(connection, {"city_slug": city_slug, "country_slug": country_slug})
    if cached:
        return cached

    city_forms = [normalize_hotel_name(value) for value in (city, city_slug.replace("-", " ")) if value]
    expected_countries = [
        set(_geocode_country_tokens(value)) for value in (country, country_slug.replace("-", " ")) if value
    ]
    queries = list(dict.fromkeys(", ".join(value for value in parts if value)
                                 for parts in ((city, country), (city_slug.replace("-", " "), country_slug.replace("-", " ")))))
    for query in queries:
        request = Request(
            f"https://photon.komoot.io/api/?{urlencode({'q': query, 'limit': 10, 'lang': 'en'})}",
            headers={"User-Agent": "HotelMetaSearch/0.1 (city reference point)", "Accept": "application/json"},
        )
        try:
            payload = json.loads(_profiled_urlopen(request, timeout=6, stage="city_profile").read(2 * 1024 * 1024))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
            continue
        for feature in payload.get("features", []) if isinstance(payload, dict) else []:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            if not isinstance(properties, dict) or properties.get("osm_key") != "place" \
                    or properties.get("osm_value") not in {"city", "town", "municipality"}:
                continue
            candidate_names = [normalize_hotel_name(properties.get(key)) for key in ("name", "city", "locality")
                               if properties.get(key)]
            city_confirmed = any(
                form and candidate and (
                    form == candidate or "".join(form.split()) == "".join(candidate.split())
                    or set(form.split()).issubset(set(candidate.split()))
                )
                for form in city_forms for candidate in candidate_names
            )
            if not city_confirmed:
                continue
            candidate_country = set(_geocode_country_tokens(str(properties.get("country") or "")))
            if not candidate_country or expected_countries and not any(
                candidate_country == expected or candidate_country.issubset(expected)
                or expected.issubset(candidate_country) for expected in expected_countries if expected
            ):
                continue
            geometry = feature.get("geometry")
            coordinates = None
            if isinstance(geometry, dict) and isinstance(geometry.get("coordinates"), (list, tuple)) \
                    and len(geometry["coordinates"]) >= 2:
                coordinates = _coordinate_pair({"lat": geometry["coordinates"][1],
                                                "lng": geometry["coordinates"][0]})
            if not coordinates:
                continue
            reference = CityReference(
                city_slug=city_slug, country_slug=country_slug,
                name=str(properties.get("name") or city),
                center_lat=coordinates["lat"], center_lon=coordinates["lng"],
                source="photon_city_place", retrieved_at=utc_now(), method="photon_place_point",
            )
            register_city_reference(database_path, reference)
            return reference
    # Photon is the preferred city resolver, but it can be temporarily
    # unavailable or omit smaller destinations. Retry the same verified city
    # lookup through the existing public geocoder before enrichment gives up;
    # this is a city reference point, never a hotel-derived centroid.
    for query in queries:
        request = Request(
            f"https://nominatim.openstreetmap.org/search?{urlencode({'format': 'jsonv2', 'limit': 5, 'q': query, 'addressdetails': 1})}",
            headers={"User-Agent": "HotelMetaSearch/0.1 (city reference point)", "Accept": "application/json"},
        )
        try:
            payload = json.loads(_profiled_urlopen(request, timeout=6, stage="city_profile_nominatim").read(2 * 1024 * 1024))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
            continue
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            address = item.get("address") if isinstance(item.get("address"), dict) else {}
            # 2026-10-15 Cusco live defect: Nominatim ranks the STATE boundary
            # (addresstype=state, -12.5/-72.5, ~125km from the real city)
            # above the city for some destinations. Photon candidates are
            # already filtered to osm_value city/town/municipality; apply the
            # same place-level constraint here so an area centroid can never
            # be persisted as a city center.
            if str(item.get("addresstype") or item.get("type") or "") not in {
                "city", "town", "municipality", "village", "borough", "suburb",
            }:
                continue
            candidate_names = [str(value) for value in (item.get("name"), address.get("city"),
                              address.get("town"), address.get("municipality"), address.get("village")) if value]
            city_confirmed = any(
                _geocode_names_match(city, candidate, city, country) or
                _geocode_names_match(city_slug.replace("-", " "), candidate, city, country)
                for candidate in candidate_names
            )
            if not city_confirmed:
                continue
            candidate_country = set(_geocode_country_tokens(str(address.get("country") or "")))
            if expected_countries and candidate_country and not any(
                candidate_country == expected or candidate_country.issubset(expected)
                or expected.issubset(candidate_country) for expected in expected_countries if expected
            ):
                continue
            coordinates = _coordinate_pair({"lat": item.get("lat"), "lng": item.get("lon")})
            if not coordinates:
                continue
            reference = CityReference(
                city_slug=city_slug, country_slug=country_slug,
                name=str(item.get("name") or city), center_lat=coordinates["lat"],
                center_lon=coordinates["lng"], source="nominatim_city_place",
                retrieved_at=utc_now(), method="nominatim_place_point",
            )
            register_city_reference(database_path, reference)
            return reference
    return None

def _geocode_query_variants(name: str, city: str, country: str | None,
                            address: str | None) -> list[str]:
    cleaned_name = re.sub(r"[\"'“”«»()\[\]]", " ", name)
    cleaned_name = " ".join(cleaned_name.split())
    compact_name = re.sub(r"\b(?:hotel|resort|spa|the|by|collection|beach)\b", " ", cleaned_name, flags=re.I)
    compact_name = " ".join(compact_name.split()).strip(" -,&")
    names = list(dict.fromkeys(value for value in (cleaned_name, compact_name) if value))
    values = []
    if address:
        address_context = [address]
        normalized_address = normalize_hotel_name(address)
        address_context.extend(value for value in (city, country)
                               if value and normalize_hotel_name(value) not in normalized_address)
        address_query = ", ".join(address_context)
        values.append(address_query)
        values.append(", ".join((names[0], address_query)))
    values.append(", ".join(part for part in (names[0], city, country) if part))
    if len(names) > 1:
        values.append(", ".join(part for part in (names[1], city, country) if part))
    return list(dict.fromkeys(" ".join(value.split()) for value in values if value.strip()))

def _geocode_identity_tokens(value: str | None, *location_values: str | None) -> set[str]:
    tokens = set(normalize_hotel_name(value).split())
    for location in location_values:
        ordered_location_tokens = normalize_hotel_name(location).split()
        location_tokens = set(ordered_location_tokens)
        compact = "".join(ordered_location_tokens)
        tokens.difference_update(location_tokens)
        if len(location_tokens) > 1:
            tokens.discard(compact)
    return tokens - GEOCODE_PROPERTY_GENERIC

def _geocode_levenhtein_distance_1(a: str, b: str) -> bool:
    """True when two single tokens differ by at most one edit (insert,
    delete, substitute - including a transposition's two substitutions is
    NOT covered, so check transposition explicitly)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diffs) == 1:
            return True
        if (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]):
            return True  # transposition: cusco/cuzco
        return False
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(longer)):
        if shorter == longer[:i] + longer[i + 1:]:
            return True
    return False


def _geocode_names_match(source_name: str, candidate_name: str | None,
                         city: str, country: str | None) -> bool:
    source = _geocode_identity_tokens(source_name, city, country)
    candidate = _geocode_identity_tokens(candidate_name, city, country)
    if not source or not candidate:
        return False
    if source == candidate:
        return True
    shared = source & candidate
    # A single shared token can identify a neighboring property with a similar
    # brand/name, so fuzzy matches need at least two independent name tokens.
    if len(shared) >= 2 and len(shared) / max(len(source), len(candidate)) >= 0.75:
        return True
    # 2026-10-15 Cusco live defect: the geocoder's place-level candidate used
    # the local spelling ('Cuzco') while the requested city was 'Cusco' - a
    # single-token transposition. The exact-match requirement made the real
    # city unmatchable, and the next candidate (the STATE boundary, same
    # spelling as the query) was accepted instead, putting the 'city center'
    # 125 km away. Allow one edit (substitution/insert/delete/transposition)
    # between single-token names so local spellings still resolve to the city.
    if len(source) == 1 and len(candidate) == 1:
        source_token = next(iter(source))
        candidate_token = next(iter(candidate))
        if len(source_token) >= 4 and len(candidate_token) >= 4:
            return _geocode_levenhtein_distance_1(source_token, candidate_token)
    return False

def _address_number_tokens(value: str | None) -> set[str]:
    tokens = normalize_hotel_name(value).split()
    ignored_context = {"district", "dist", "area", "zone", "sector", "po", "box",
                       "postal", "postcode", "zip", "floor", "level", "unit", "apt"}
    numbers = set()
    for index, token in enumerate(tokens):
        if not re.fullmatch(r"(?:[a-z]{1,5}\d+[a-z]?|\d+[a-z]{0,3})", token):
            continue
        if index and tokens[index - 1] in ignored_context:
            continue
        numbers.add(token)
    return numbers

def _geocode_addresses_match(source_address: str | None, candidate_values: list[str],
                             city: str, country: str | None,
                             candidate_number: str | None = None) -> bool:
    source = _geocode_identity_tokens(source_address, city, country)
    candidate = set().union(*(_geocode_identity_tokens(value, city, country)
                              for value in candidate_values if value)) if candidate_values else set()
    if not source or not candidate:
        return False
    shared = source & candidate
    source_numbers = _address_number_tokens(source_address)
    number_evidence = candidate_number if candidate_number is not None else next(
        (value for value in candidate_values if value), None
    )
    candidate_numbers = _address_number_tokens(number_evidence)
    if source_numbers and candidate_numbers and source_numbers.isdisjoint(candidate_numbers):
        return False
    shared_numbers = source_numbers & candidate_numbers
    street_tokens = {token for token in shared if not token.isdigit() and len(token) >= 3}
    # A house number plus a matching street, or a strong multi-token street
    # match, distinguishes the property from nearby hotels in the same area.
    if shared_numbers and street_tokens:
        return True
    source_street = {token for token in source if not token.isdigit() and len(token) >= 3}
    return len(street_tokens) >= 2 and len(street_tokens) / max(1, len(source_street)) >= 0.65

def _geocode_address_conflicts(source_address: str | None, candidate_number: str | None,
                               candidate_street: str | None, city: str,
                               country: str | None) -> bool:
    source = _geocode_identity_tokens(source_address, city, country)
    candidate = _geocode_identity_tokens(candidate_street, city, country)
    source_numbers = _address_number_tokens(source_address)
    candidate_numbers = _address_number_tokens(candidate_number)
    if source_numbers and candidate_numbers and not (source_numbers & candidate_numbers):
        return True
    source_street = {token for token in source if not token.isdigit() and len(token) >= 3}
    candidate_street_tokens = {token for token in candidate if not token.isdigit() and len(token) >= 3}
    return bool(source_street and candidate_street_tokens and not (source_street & candidate_street_tokens))

def _geocode_country_tokens(value: str | None) -> set[str]:
    normalized = _normalize_country(value or "")
    aliases = {
        "vetnam": "vietnam", "viet nam": "vietnam", "tailand": "thailand",
        "tailandiya": "thailand", "tailandia": "thailand", "tayland": "thailand",
        "turkiye": "turkey", "turtsiya": "turkey",
        "frantsiya": "france", "ispaniya": "spain", "italiya": "italy",
        "germaniya": "germany", "yaponiya": "japan", "indoneziya": "indonesia",
        "singapur": "singapore", "malyziya": "malaysia", "egipet": "egypt",
        "obedinennye arabskie emiraty": "united arab emirates",
        "obedinyennye arabskie emiraty": "united arab emirates",
    }
    return set(aliases.get(normalized, normalized).split())

def _tripcom_detail_address(database_path: str | Path, hotel: dict[str, Any],
                            city: str, country: str | None,
                            diagnostic: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if diagnostic is not None:
        diagnostic.update({"status": "NOT_FOUND", "reason": "trip_detail_no_verified_coordinates"})
    trip_offer = next((offer for offer in hotel.get("offers", [])
                       if offer.get("source") == "trip_com" and offer.get("deeplink")), None)
    if not trip_offer:
        return None
    detail_url = str(trip_offer["deeplink"])
    parsed_url = urlsplit(detail_url)
    if parsed_url.scheme != "https" or not (parsed_url.hostname or "").casefold().endswith("trip.com"):
        return None
    query = parse_qs(parsed_url.query)
    requested_city = normalize_hotel_name(city or hotel.get("city") or "")
    linked_city = normalize_hotel_name(next(iter(query.get("cityEnName", [])), ""))
    linked_city_confirmed = bool(requested_city and linked_city
                                 and "".join(requested_city.split()) == "".join(linked_city.split()))
    hotel_id = next(iter(query.get("hotelId", [])), None)
    if not hotel_id:
        match = re.search(r"hotel-detail-(\d+)", parsed_url.path)
        hotel_id = match.group(1) if match else None
    if not hotel_id or not str(hotel_id).isdigit():
        return None

    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hotel_provider_detail_cache ("
            "source TEXT NOT NULL, source_hotel_id TEXT NOT NULL, address TEXT, latitude REAL, longitude REAL, "
            "coordinate_verified INTEGER NOT NULL DEFAULT 0, detail_version TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, "
            "retrieved_at TEXT NOT NULL, PRIMARY KEY(source, source_hotel_id))"
        )
        detail_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(hotel_provider_detail_cache)")}
        for column, declaration in (("latitude", "REAL"), ("longitude", "REAL"),
                                    ("coordinate_verified", "INTEGER NOT NULL DEFAULT 0"),
                                    ("detail_version", "TEXT NOT NULL DEFAULT ''")):
            if column not in detail_columns:
                connection.execute(f"ALTER TABLE hotel_provider_detail_cache ADD COLUMN {column} {declaration}")
        cached = connection.execute(
            "SELECT address,latitude,longitude,coordinate_verified,status,retrieved_at,detail_version "
            "FROM hotel_provider_detail_cache "
            "WHERE source='trip_com' AND source_hotel_id=?", (str(hotel_id),)
        ).fetchone()
    if cached:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached[5]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            age = timedelta.max
        if cached[4] == "ok" and cached[6] == "tripcom-detail-v7" and (cached[0] or cached[3]):
            coordinates = None
            if cached[3] and cached[1] is not None and cached[2] is not None:
                coordinates = {"lat": cached[1], "lng": cached[2]}
            if diagnostic is not None:
                diagnostic.update({"status": "PROVIDER_DETAIL", "reason": "trip_detail_cache"})
            return {"address": cached[0], "coordinates": coordinates,
                    "coordinate_source": "trip_com_detail" if coordinates else None}
        if cached[4] == "not_found" and cached[6] == "tripcom-detail-v7" and age < timedelta(days=7):
            return None

    class ScriptCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.current: dict[str, str] | None = None
            self.scripts: list[dict[str, str]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "script":
                self.current = {key: value or "" for key, value in attrs}
                self.current["text"] = ""

        def handle_data(self, data: str) -> None:
            if self.current is not None:
                self.current["text"] += data

        def handle_endtag(self, tag: str) -> None:
            if tag == "script" and self.current is not None:
                self.scripts.append(self.current)
                self.current = None

    address = None
    status = "not_found"
    try:
        request = Request(detail_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HotelMetaSearch/0.1; provider detail enrichment)",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml",
        })
        with _profiled_urlopen(request, timeout=12, stage="trip_provider_detail") as response:
            final_url = response.geturl() if hasattr(response, "geturl") else detail_url
            final_host = (urlsplit(final_url).hostname or "").casefold()
            if not (final_host == "trip.com" or final_host.endswith(".trip.com")):
                return None
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) <= 4 * 1024 * 1024:
            page_text = body.decode("utf-8", "replace")
            page_hotel_id_confirmed = bool(re.search(
                rf"hotel-detail-{re.escape(str(hotel_id))}(?:/|[?\"\\]|$)", page_text, re.I
            ))
            collector = ScriptCollector()
            collector.feed(page_text)
            decoder = json.JSONDecoder()
            objects: list[tuple[dict[str, Any], str]] = []
            for script in collector.scripts:
                text = script.get("text", "")
                if script.get("type", "").casefold() == "application/ld+json":
                    try:
                        value = json.loads(text)
                        if isinstance(value, dict):
                            objects.append((value, text))
                        elif isinstance(value, list):
                            objects.extend((item, text) for item in value if isinstance(item, dict))
                    except (json.JSONDecodeError, TypeError):
                        pass
                candidates = [text]
                push_start = text.find("push(")
                if push_start >= 0:
                    try:
                        pushed, _ = decoder.raw_decode(text, push_start + len("push("))
                        if isinstance(pushed, list) and len(pushed) > 1 and isinstance(pushed[1], str):
                            candidates.append(pushed[1])
                    except json.JSONDecodeError:
                        pass
                for candidate in candidates:
                    cursor = 0
                    while True:
                        marker = candidate.find("structuredData", cursor)
                        if marker < 0:
                            break
                        start = candidate.find("[", marker)
                        cursor = marker + len("structuredData")
                        if start < 0:
                            continue
                        try:
                            values, _ = decoder.raw_decode(candidate, start)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(values, list):
                            continue
                        for value in values:
                            if isinstance(value, dict):
                                objects.append((value, candidate))
                            elif isinstance(value, str):
                                try:
                                    parsed = json.loads(value)
                                except (json.JSONDecodeError, TypeError):
                                    continue
                                if isinstance(parsed, dict):
                                    objects.append((parsed, candidate))
            coordinates = None
            for item, source_text in objects:
                kind = item.get("@type")
                if not (kind == "Hotel" or isinstance(kind, list) and "Hotel" in kind):
                    continue
                postal = item.get("address")
                name_confirmed = _geocode_names_match(
                    hotel.get("name", ""), item.get("name"),
                    city or hotel.get("city") or "", country or hotel.get("country"),
                )
                address_country = postal.get("addressCountry") if isinstance(postal, dict) else None
                if isinstance(address_country, dict):
                    address_country = address_country.get("name") or address_country.get("@id")
                expected_country_tokens = _geocode_country_tokens(country or hotel.get("country"))
                address_country_tokens = _geocode_country_tokens(str(address_country or ""))
                country_confirmed = bool(address_country_tokens) and (
                    not expected_country_tokens or bool(address_country_tokens & expected_country_tokens)
                )
                detail_identity_confirmed = name_confirmed or (
                    page_hotel_id_confirmed and linked_city_confirmed and country_confirmed
                )
                if not detail_identity_confirmed:
                    continue
                if isinstance(postal, dict):
                    values = [postal.get(key) for key in (
                        "streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry",
                    )]
                    address = ", ".join(dict.fromkeys(
                        " ".join(str(value).split()) for value in values if value and str(value).strip()
                    )) or None
                precise_source = source_text.replace(r'\"', '"')
                precise_match = re.search(
                    r'"mapType"\s*:\s*"[^"]*"\s*,\s*"lat"\s*:\s*"?(-?\d+(?:\.\d+)?)"?'
                    r'\s*,\s*"lng"\s*:\s*"?(-?\d+(?:\.\d+)?)"?'
                    r'\s*,\s*"addressVisible"\s*:\s*(?:true|false)\s*,\s*"preciseCoordinatesVisible"\s*:\s*true',
                    precise_source,
                )
                if precise_match:
                    try:
                        latitude, longitude = map(float, precise_match.groups())
                    except (TypeError, ValueError):
                        latitude = longitude = math.nan
                    if math.isfinite(latitude) and math.isfinite(longitude) \
                            and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                        coordinates = {"lat": latitude, "lng": longitude}
                if address or coordinates:
                    status = "ok"
                    if address and coordinates:
                        break
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException) as exc:
        if diagnostic is not None:
            code = getattr(exc, "code", None)
            diagnostic.update({"status": "ERROR", "reason": f"trip_detail:{type(exc).__name__}:{code or ''}"})
        return None

    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO hotel_provider_detail_cache(source,source_hotel_id,address,latitude,longitude,"
            "coordinate_verified,detail_version,status,retrieved_at) VALUES ('trip_com',?,?,?,?,?,?,?,?)",
            (str(hotel_id), address,
             coordinates["lat"] if coordinates else None, coordinates["lng"] if coordinates else None,
             int(coordinates is not None), "tripcom-detail-v7", status, utc_now())
        )
        connection.commit()
    if diagnostic is not None:
        diagnostic.update({"status": "PROVIDER_DETAIL" if coordinates else "NOT_FOUND",
                           "reason": "trip_detail_verified" if coordinates else "trip_detail_no_match"})
    return {"address": address, "coordinates": coordinates,
            "coordinate_source": "trip_com_detail" if coordinates else None} if status == "ok" else None

def _provider_schema_metadata(item: dict[str, Any], domain: str) -> dict[str, Any]:
    """Keep a small allowlist of public Hotel JSON-LD facts; discard broken or opaque fields."""
    def clean_text(value: Any, limit: int = 1000) -> str | None:
        if not isinstance(value, str):
            return None
        value = " ".join(unescape(value).split())
        if not value or "\ufffd" in value:
            return None
        return value[:limit]

    postal = item.get("address") if isinstance(item.get("address"), dict) else {}
    address_fields = {
        key: clean_text(postal.get(source_key), 200)
        for key, source_key in (("street", "streetAddress"), ("locality", "addressLocality"),
                                ("region", "addressRegion"), ("postal_code", "postalCode"),
                                ("country", "addressCountry"))
    }
    address_fields = {key: value for key, value in address_fields.items() if value}
    # Avoid presenting isolated postcode/country values as a complete address.
    address = ", ".join(address_fields[key] for key in ("street", "locality", "region", "postal_code", "country")
                         if key in address_fields) if len(address_fields) >= 2 else None
    geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
    coordinates = _coordinate_pair({"lat": geo.get("latitude"), "lng": geo.get("longitude")})
    rating = item.get("aggregateRating") if isinstance(item.get("aggregateRating"), dict) else {}
    stars = item.get("starRating") if isinstance(item.get("starRating"), dict) else {}
    try:
        rating_value = float(rating.get("ratingValue"))
        if not math.isfinite(rating_value) or not 0 < rating_value <= 10:
            rating_value = None
    except (TypeError, ValueError):
        rating_value = None
    try:
        review_count = int(rating.get("reviewCount"))
        if review_count < 0:
            review_count = None
    except (TypeError, ValueError):
        review_count = None
    try:
        star_value = float(stars.get("ratingValue"))
        if not math.isfinite(star_value) or not 0 < star_value <= 5:
            star_value = None
    except (TypeError, ValueError):
        star_value = None

    amenities = []
    raw_amenities = item.get("amenityFeature")
    for feature in raw_amenities if isinstance(raw_amenities, list) else []:
        if not isinstance(feature, dict) or feature.get("value") is False:
            continue
        label = clean_text(feature.get("name"), 100)
        if label and label not in amenities:
            amenities.append(label)

    def clock(value: Any) -> str | None:
        candidate = clean_text(value, 20)
        return candidate if candidate and re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", candidate) else None

    images = []
    raw_images = item.get("image")
    for value in raw_images if isinstance(raw_images, list) else [raw_images]:
        candidate = clean_text(value.get("url") if isinstance(value, dict) else value, 1000)
        if candidate and candidate.startswith("https://") and candidate not in images:
            images.append(candidate)
        if len(images) >= 12:
            break

    return {
        "provider": domain,
        "source": f"{domain}_public_hotel_schema",
        "method": "schema_org_json_ld_hotel",
        "confidence": "high",
        "retrieved_at": utc_now(),
        "name": clean_text(item.get("name"), 200),
        "address": address,
        "address_fields": address_fields,
        "coordinates": coordinates,
        "coordinate_source": f"{domain}_detail" if coordinates else None,
        "stars": star_value,
        "rating": rating_value,
        "reviews_count": review_count,
        "description": clean_text(item.get("description"), 2000),
        "hotel_amenities": amenities,
        "room_amenities": [],
        "check_in": clock(item.get("checkinTime")),
        "check_out": clock(item.get("checkoutTime")),
        "front_desk_hours": None,
        "images": images,
    }

def _provider_schema_detail(database_path: str | Path, hotel: dict[str, Any],
                            city: str, country: str | None,
                            diagnostic: dict[str, Any] | None = None) -> dict[str, Any] | None:
    urls = []
    for offer in hotel.get("offers", []):
        parsed = urlsplit(str(offer.get("deeplink") or ""))
        host = (parsed.hostname or "").casefold()
        if parsed.scheme == "https" and any(host == domain or host.endswith("." + domain)
                                             for domain in ("tutu.ru", "ostrovok.ru", "sutochno.ru")):
            urls.append(f"{host}{parsed.path}")
    if not urls:
        return _provider_schema_detail_unlocked(database_path, hotel, city, country, diagnostic)
    lock_key = hashlib.sha256(sorted(set(urls))[0].encode("utf-8")).hexdigest()
    with PROVIDER_DETAIL_LOCKS_GUARD:
        detail_lock = PROVIDER_DETAIL_LOCKS.setdefault(lock_key, threading.Lock())
    with detail_lock:
        return _provider_schema_detail_unlocked(database_path, hotel, city, country, diagnostic)

def _provider_schema_detail_unlocked(database_path: str | Path, hotel: dict[str, Any],
                                     city: str, country: str | None,
                                     diagnostic: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if diagnostic is not None:
        diagnostic.update({"status": "NOT_FOUND", "reason": "provider_detail_no_verified_coordinates"})
    allowed_domains = {"tutu.ru", "ostrovok.ru", "sutochno.ru"}
    candidates = []
    for offer in hotel.get("offers", []):
        raw_url = offer.get("deeplink")
        if not raw_url:
            continue
        parsed = urlsplit(str(raw_url))
        host = (parsed.hostname or "").casefold()
        domain = next((item for item in allowed_domains if host == item or host.endswith("." + item)), None)
        if parsed.scheme == "https" and domain:
            candidates.append((0 if domain == "tutu.ru" else 1, str(raw_url), parsed, domain))
    for _priority, detail_url, parsed_url, domain in sorted(candidates):
        cache_id = hashlib.sha256(f"{parsed_url.hostname}{parsed_url.path}".encode("utf-8")).hexdigest()
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(hotel_provider_detail_cache)")}
            if not columns:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS hotel_provider_detail_cache ("
                    "source TEXT NOT NULL, source_hotel_id TEXT NOT NULL, address TEXT, latitude REAL, longitude REAL, "
                    "coordinate_verified INTEGER NOT NULL DEFAULT 0, detail_version TEXT NOT NULL DEFAULT '', "
                    "metadata_json TEXT, status TEXT NOT NULL, retrieved_at TEXT NOT NULL, "
                    "PRIMARY KEY(source,source_hotel_id))"
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(hotel_provider_detail_cache)")}
            for column, declaration in (("latitude", "REAL"), ("longitude", "REAL"),
                                        ("coordinate_verified", "INTEGER NOT NULL DEFAULT 0"),
                                        ("detail_version", "TEXT NOT NULL DEFAULT ''"),
                                        ("metadata_json", "TEXT")):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE hotel_provider_detail_cache ADD COLUMN {column} {declaration}")
            cached = connection.execute(
                "SELECT address,latitude,longitude,coordinate_verified,status,retrieved_at,detail_version,metadata_json "
                "FROM hotel_provider_detail_cache WHERE source=? AND source_hotel_id=?",
                (domain, cache_id),
            ).fetchone()
        if cached:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(cached[5]).astimezone(timezone.utc)
            except (TypeError, ValueError):
                age = timedelta.max
            if cached[4] == "ok" and cached[6] == "schema-hotel-v6" and (cached[0] or cached[3] or cached[7]):
                _record_cache("provider_detail", True)
                coordinates = {"lat": cached[1], "lng": cached[2]} if cached[3] else None
                metadata = _safe_json_loads(cached[7], None) if cached[7] else None
                if metadata is not None and not isinstance(metadata, dict):
                    metadata = None
                if diagnostic is not None:
                    diagnostic.update({"status": "PROVIDER_DETAIL", "reason": "provider_detail_cache"})
                return {"address": cached[0], "coordinates": coordinates,
                        "coordinate_source": f"{domain}_detail" if coordinates else None,
                        "provider_details": metadata, "provider": domain}
            if cached[4] == "not_found" and cached[6] == "schema-hotel-v6" and age < timedelta(days=7):
                _record_cache("provider_detail", True)
                continue
        _record_cache("provider_detail", False)

        class ScriptCollector(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.current: dict[str, str] | None = None
                self.scripts: list[dict[str, str]] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag == "script":
                    self.current = {key: value or "" for key, value in attrs}
                    self.current["text"] = ""

            def handle_data(self, data: str) -> None:
                if self.current is not None:
                    self.current["text"] += data

            def handle_endtag(self, tag: str) -> None:
                if tag == "script" and self.current is not None:
                    self.scripts.append(self.current)
                    self.current = None

        address = None
        coordinates = None
        provider_metadata = None
        status = "not_found"
        parse_started = None
        try:
            request = Request(detail_url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; HotelMetaSearch/0.1; provider detail enrichment)",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml",
            })
            with _profiled_urlopen(request, timeout=12, stage="provider_detail") as response:
                final_url = response.geturl() if hasattr(response, "geturl") else detail_url
                final_host = (urlsplit(final_url).hostname or "").casefold()
                if final_host != domain and not final_host.endswith("." + domain):
                    continue
                body = response.read(3 * 1024 * 1024 + 1)
            parse_started = time.perf_counter()
            if len(body) <= 3 * 1024 * 1024:
                collector = ScriptCollector()
                collector.feed(body.decode("utf-8", "replace"))
                decoder = json.JSONDecoder()
                for script in collector.scripts:
                    script_text = script.get("text", "")
                    candidates = [script_text]
                    if script.get("type", "").casefold() == "application/ld+json":
                        try:
                            candidates.append(json.loads(script_text))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    push_start = script_text.find("push(")
                    if push_start >= 0:
                        try:
                            pushed, _ = decoder.raw_decode(script_text, push_start + len("push("))
                            if isinstance(pushed, list) and len(pushed) > 1 and isinstance(pushed[1], str):
                                candidates.append(pushed[1])
                        except json.JSONDecodeError:
                            pass
                    roots = []
                    for candidate in candidates:
                        if isinstance(candidate, dict):
                            roots.append(candidate)
                            continue
                        cursor = 0
                        while True:
                            marker = candidate.find("structuredData", cursor)
                            if marker < 0:
                                break
                            start = candidate.find("[", marker)
                            cursor = marker + len("structuredData")
                            if start < 0:
                                continue
                            try:
                                values, _ = decoder.raw_decode(candidate, start)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(values, list):
                                continue
                            for value in values:
                                if isinstance(value, str):
                                    try:
                                        value = json.loads(value)
                                    except (json.JSONDecodeError, TypeError):
                                        continue
                                if isinstance(value, (dict, list)):
                                    roots.append(value)
                    for root in roots:
                        stack = [root]
                        while stack:
                            item = stack.pop()
                            if isinstance(item, list):
                                stack.extend(item)
                                continue
                            if not isinstance(item, dict):
                                continue
                            stack.extend(value for key, value in item.items() if key in {"@graph", "mainEntity"})
                            kind = item.get("@type")
                            types = {kind} if isinstance(kind, str) else set(kind) if isinstance(kind, list) else set()
                            if not types & {"Hotel", "Hostel", "LodgingBusiness", "Resort"}:
                                continue
                            schema_name = unescape(str(item.get("name") or ""))
                            postal = item.get("address")
                            geo = item.get("geo")
                            source_address = (hotel.get("location") or {}).get("address") or hotel.get("address")
                            address_identity_match = False
                            if isinstance(postal, dict) and source_address:
                                address_identity_match = _geocode_addresses_match(
                                    source_address,
                                    [str(postal.get(key)) for key in (
                                        "streetAddress", "addressLocality", "addressRegion", "postalCode",
                                        "addressCountry",
                                    ) if postal.get(key)],
                                    city, country,
                                )
                            if not _geocode_names_match(hotel.get("name", ""), schema_name, city, country) \
                                    and not address_identity_match:
                                continue
                            provider_metadata = _provider_schema_metadata(item, domain)
                            coordinates = provider_metadata.get("coordinates")
                            address = provider_metadata.get("address")
                            address_fields = []
                            address_confirmed = False
                            if isinstance(postal, dict):
                                address_fields = [postal.get(key) for key in (
                                    "streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry",
                                )]
                                address = provider_metadata.get("address")
                                city_fields = " ".join(str(postal.get(key) or "") for key in (
                                    "addressLocality", "addressRegion",
                                ))
                                city_norm = set(normalize_hotel_name(city).split())
                                detail_city = set(normalize_hotel_name(city_fields).split())
                                city_confirmed = bool(city_norm & detail_city)
                                address_confirmed = bool(source_address and address and _geocode_addresses_match(
                                    source_address, [str(value) for value in address_fields if value], city, country))
                                country_value = _geocode_country_tokens(str(postal.get("addressCountry") or ""))
                                expected_country = _geocode_country_tokens(country)
                                country_confirmed = not country_value or not expected_country or bool(
                                    country_value & expected_country)
                                if not city_confirmed and not address_confirmed and isinstance(geo, dict):
                                    try:
                                        test_lat = float(geo.get("latitude"))
                                        test_lng = float(geo.get("longitude"))
                                        extent = _photon_city_extent(city, country)
                                        city_confirmed = bool(extent and extent[0] <= test_lng <= extent[2]
                                                              and extent[1] <= test_lat <= extent[3])
                                    except (TypeError, ValueError, HTTPError, URLError, TimeoutError, OSError):
                                        city_confirmed = False
                                if not country_confirmed or not (city_confirmed or address_confirmed):
                                    continue
                                if source_address and address and _geocode_address_conflicts(
                                        source_address,
                                        postal.get("houseNumber") or postal.get("house_number"),
                                        str(postal.get("streetAddress") or ""), city, country):
                                    continue
                            if not coordinates and isinstance(geo, dict):
                                try:
                                    latitude = float(geo.get("latitude"))
                                    longitude = float(geo.get("longitude"))
                                except (TypeError, ValueError):
                                    continue
                                if math.isfinite(latitude) and math.isfinite(longitude) \
                                        and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                                    coordinates = {"lat": latitude, "lng": longitude}
                            has_schema_facts = bool(
                                provider_metadata.get("name") or provider_metadata.get("description")
                                or provider_metadata.get("hotel_amenities") or provider_metadata.get("check_in")
                                or provider_metadata.get("check_out") or provider_metadata.get("images")
                                or provider_metadata.get("rating") is not None
                                or provider_metadata.get("stars") is not None
                            )
                            if address or coordinates or has_schema_facts:
                                status = "ok"
                                if coordinates or has_schema_facts:
                                    break
                        if status == "ok":
                            break
                    if status == "ok":
                        break
            profile = _ACTIVE_ENRICHMENT_PROFILE.get()
            if profile and parse_started is not None:
                profile.add_stage("jsonld_parse", (time.perf_counter() - parse_started) * 1000)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException) as exc:
            if diagnostic is not None:
                code = getattr(exc, "code", None)
                diagnostic.update({"status": "ERROR", "reason": f"provider_detail:{type(exc).__name__}:{code or ''}"})
            continue
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO hotel_provider_detail_cache(source,source_hotel_id,address,latitude,longitude,"
                "coordinate_verified,detail_version,metadata_json,status,retrieved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (domain, cache_id, address, coordinates["lat"] if coordinates else None,
                 coordinates["lng"] if coordinates else None, int(coordinates is not None),
                 "schema-hotel-v6", json.dumps(provider_metadata, ensure_ascii=False) if provider_metadata else None,
                 status, utc_now()),
            )
            connection.commit()
        if status == "ok":
            if diagnostic is not None:
                diagnostic.update({"status": "PROVIDER_DETAIL" if coordinates else "NOT_FOUND",
                                   "reason": "provider_detail_verified" if coordinates else "provider_detail_no_match"})
            return {"address": address, "coordinates": coordinates,
                    "coordinate_source": f"{domain}_detail" if coordinates else None,
                    "provider_details": provider_metadata, "provider": domain}
    if diagnostic is not None and diagnostic.get("status") != "ERROR":
        diagnostic.update({"status": "NOT_FOUND", "reason": "provider_detail_no_match"})
    return None

def _photon_hotel_coordinates(name: str, city: str, country: str | None,
                              address: str | None = None) -> dict[str, float] | None:
    geo_normalize = lambda value: normalize_hotel_name(str(value or "").replace("đ", "d").replace("Đ", "D"))
    city_tokens = set(geo_normalize(city).split())
    if not city_tokens:
        return None
    hotel_values = {"hotel", "hostel", "guest_house", "motel", "resort", "apartment", "apartments", "construction"}
    country_aliases = {"turkiye": "turkey", "viet nam": "vietnam"}
    expected_country_name = country_aliases.get(_normalize_country(country or ""), _normalize_country(country or ""))
    expected_country = set(expected_country_name.split())

    for query in _geocode_query_variants(name, city, country, address):
        request = Request(
            f"https://photon.komoot.io/api/?{urlencode({'q': query, 'limit': 10})}",
            headers={"User-Agent": "HotelMetaSearch/0.1 (verified hotel coordinates)", "Accept": "application/json"},
        )
        payload = json.loads(_profiled_urlopen(request, timeout=5, stage="geocoding").read(2 * 1024 * 1024))
        for feature in payload.get("features", []) if isinstance(payload, dict) else []:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            if not isinstance(properties, dict):
                continue
            osm_key = str(properties.get("osm_key") or "").casefold()
            osm_value = str(properties.get("osm_value") or "").casefold().replace(" ", "_")
            hotel_feature = osm_value in hotel_values and osm_key in {"tourism", "building", "amenity"}
            address_feature = osm_key == "building" and osm_value in {"yes", "hotel"}
            if not hotel_feature and not address_feature:
                continue
            name_match = _geocode_names_match(name, properties.get("name"), city, country)
            address_fields = [str(properties.get(key) or "") for key in (
                "housenumber", "street", "postcode", "district", "county", "city", "state",
            )]
            exact_address = _geocode_addresses_match(
                address, address_fields, city, country, properties.get("housenumber"))
            address_conflict = _geocode_address_conflicts(
                address, properties.get("housenumber"), properties.get("street"), city, country)
            if not exact_address and (not hotel_feature or not name_match or address_conflict):
                continue
            found_country_name = country_aliases.get(
                _normalize_country(str(properties.get("country") or "")),
                _normalize_country(str(properties.get("country") or "")),
            )
            found_country = set(found_country_name.split())
            country_conflict = bool(expected_country and found_country and not (expected_country & found_country))
            coords = (feature.get("geometry") or {}).get("coordinates")
            if not isinstance(coords, list) or len(coords) < 2:
                continue
            try:
                longitude, latitude = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue
            area_tokens = set()
            for key in ("city", "district", "county", "state", "locality", "suburb"):
                area_tokens.update(geo_normalize(properties.get(key)).split())
            locality_confirmed = city_tokens.issubset(area_tokens)
            if not locality_confirmed or country_conflict:
                try:
                    extent = _photon_city_extent(city, country)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
                    extent = None
                within_city = bool(extent and extent[0] <= longitude <= extent[2]
                                   and extent[1] <= latitude <= extent[3])
                locality_confirmed = within_city if country_conflict else locality_confirmed or within_city
            if locality_confirmed and math.isfinite(latitude) and math.isfinite(longitude) \
                    and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return {"lat": latitude, "lng": longitude}
    return None

def _geocode_hotel_name(database_path: str | Path, name: str, city: str,
                        country: str | None, address: str | None = None,
                        city_slug: str | None = None, country_slug: str | None = None,
                        diagnostic: dict[str, Any] | None = None,
                        provider: str | None = None,
                        provider_hotel_id: str | None = None) -> dict[str, float] | None:
    key = _hotel_coordinate_cache_key(name, city, country, address, city_slug, country_slug,
                                      provider, provider_hotel_id)
    with GEOCODE_REQUEST_LOCKS_GUARD:
        request_lock = GEOCODE_REQUEST_LOCKS.setdefault(key, threading.Lock())
    with request_lock:
        return _geocode_hotel_name_unlocked(
            database_path, name, city, country, address, city_slug, country_slug,
            diagnostic, provider, provider_hotel_id,
        )

def _geocode_hotel_name_unlocked(database_path: str | Path, name: str, city: str,
                                 country: str | None, address: str | None = None,
                                 city_slug: str | None = None, country_slug: str | None = None,
                                 diagnostic: dict[str, Any] | None = None,
                                 provider: str | None = None,
                                 provider_hotel_id: str | None = None) -> dict[str, float] | None:
    if diagnostic is not None:
        diagnostic.update({"status": "NOT_FOUND", "reason": "geocoder_no_verified_match"})
    slug_city = " ".join(part for part in re.split(r"[-_]+", city_slug or "")
                         if part.isascii() and part.isalnum())
    slug_country = " ".join(part for part in re.split(r"[-_]+", country_slug or "")
                            if part.isascii() and part.isalnum())
    location_city = slug_city or city
    location_country = slug_country or country
    cache_key = _hotel_coordinate_cache_key(
        name, city, country, address, city_slug, country_slug, provider, provider_hotel_id)
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        cached = connection.execute(
            "SELECT latitude,longitude,status,retrieved_at FROM hotel_coordinate_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
    if cached:
        if cached[2] == "ok":
            _record_cache("geocode", True)
            if diagnostic is not None:
                diagnostic.update({"status": "GEOCODER", "reason": "geocoder_cache"})
            return {"lat": cached[0], "lng": cached[1]}
        try:
            cached_at = datetime.fromisoformat(cached[3])
            if datetime.now(timezone.utc) - cached_at.astimezone(timezone.utc) < timedelta(days=7):
                _record_cache("geocode", True)
                if diagnostic is not None:
                    diagnostic.update({"status": "NOT_FOUND", "reason": "geocoder_negative_cache"})
                return None
        except (TypeError, ValueError):
            pass
    _record_cache("geocode", False)

    result = None
    result_source = "nominatim"
    nominatim_responded = False
    try:
        result = _photon_hotel_coordinates(name, location_city, location_country, address)
        if result:
            result_source = "photon"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException) as exc:
        if diagnostic is not None:
            code = getattr(exc, "code", None)
            diagnostic.update({"status": "ERROR", "reason": f"geocoder:{type(exc).__name__}:{code or ''}"})

    try:
        global GEOCODER_LAST_REQUEST
        normalized_city = normalize_hotel_name(location_city)
        # Provider labels and OSM may transliterate Cyrillic city names differently.
        # Keep the existing normalizer and add the common kh/h variant for validation.
        city_variants = {normalized_city}
        if "kh" in normalized_city:
            city_variants.add(normalized_city.replace("kh", "h"))
        city_tokens = set().union(*(set(value.split()) for value in city_variants))
        compact_city = "".join(normalize_hotel_name(location_city).split())
        city_forms = city_tokens | ({compact_city} if len(city_tokens) > 1 else set())

        def identity_tokens(value: str | None) -> set[str]:
            return {token for token in normalize_hotel_name(value).split()
                    if token not in city_forms and token not in city_tokens}

        source_tokens = identity_tokens(name)
        if not source_tokens:
            return None
        query_values = _geocode_query_variants(name, location_city, location_country, address)
        if city != location_city or country != location_country:
            query_values.extend(_geocode_query_variants(name, city, country, address))
        city_search_tokens = city_tokens
        for query in (() if result else dict.fromkeys(query_values)):
            url = f"https://nominatim.openstreetmap.org/search?{urlencode({'format': 'jsonv2', 'limit': 5, 'q': query})}"
            with GEOCODER_LOCK:
                remaining = 1.1 - (time.monotonic() - GEOCODER_LAST_REQUEST)
                if remaining > 0:
                    time.sleep(remaining)
                GEOCODER_LAST_REQUEST = time.monotonic()
                request = Request(url, headers={
                    "User-Agent": "HotelMetaSearch/0.1 (coordinate cache)",
                    "Accept": "application/json",
                })
                with _profiled_urlopen(request, timeout=8, stage="geocoding") as response:
                    payload = json.loads(response.read(2 * 1024 * 1024))
            nominatim_responded = True
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                category = item.get("class") or item.get("category")
                provider_hotel = category in {"tourism", "building", "amenity"} and item.get("type") in {
                    "hotel", "hostel", "guest_house", "motel", "apartment", "apartments",
                }
                address_feature = (category == "building" and item.get("type") in {"yes", "house", "building"})
                candidate_name = item.get("name")
                address_value = item.get("address")
                address_fields = [str(value) for value in address_value.values()] if isinstance(address_value, dict) else []
                address_match = _geocode_addresses_match(
                    address, address_fields, location_city, location_country,
                    address_value.get("house_number") if isinstance(address_value, dict) else None)
                name_match = bool(candidate_name) and _geocode_names_match(
                    name, candidate_name, location_city, location_country)
                if not provider_hotel and not (address_feature and address_match):
                    continue
                address_data = address_value if isinstance(address_value, dict) else {}
                address_conflict = _geocode_address_conflicts(
                    address, address_data.get("house_number"),
                    " ".join(str(address_data.get(key) or "") for key in (
                        "road", "pedestrian", "residential", "street", "footway",
                    )), location_city, location_country)
                if not address_match and (not name_match or address_conflict):
                    continue
                try:
                    latitude, longitude = float(item["lat"]), float(item["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                candidate_city = " ".join(str(address_data.get(key) or "") for key in (
                    "city", "town", "village", "municipality", "suburb", "county",
                ))
                candidate_country = str(address_data.get("country") or "")
                normalized_candidate_city = normalize_hotel_name(candidate_city)
                candidate_city_tokens = set(normalized_candidate_city.split())
                candidate_city_compact = "".join(normalized_candidate_city.split())
                city_confirmed = bool(candidate_city_tokens & city_search_tokens) or \
                    bool(candidate_city_compact and candidate_city_compact in city_forms)
                candidate_country_tokens = _geocode_country_tokens(candidate_country)
                expected_country_tokens = _geocode_country_tokens(location_country)
                country_confirmed = (not location_country or not candidate_country or not candidate_country_tokens or
                                     bool(candidate_country_tokens & expected_country_tokens))
                if not city_confirmed or not country_confirmed:
                    try:
                        extent = _photon_city_extent(location_city, location_country)
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
                        extent = None
                    city_confirmed = bool(extent and extent[0] <= longitude <= extent[2]
                                           and extent[1] <= latitude <= extent[3])
                if city_confirmed and country_confirmed and math.isfinite(latitude) and math.isfinite(longitude) \
                        and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    result = {"lat": latitude, "lng": longitude}
                    break
            if result:
                break
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, HTTPClientException):
        pass

    if result or nominatim_responded:
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO hotel_coordinate_cache(cache_key,latitude,longitude,source,retrieved_at,status) "
                "VALUES (?,?,?,?,?,?)",
                (cache_key, result["lat"] if result else None, result["lng"] if result else None,
                 result_source, utc_now(), "ok" if result else "not_found"),
            )
            connection.commit()
    if diagnostic is not None:
        diagnostic.update({"status": "GEOCODER" if result else "NOT_FOUND",
                           "reason": "geocoder_verified" if result else
                           "geocoder_no_verified_match" if nominatim_responded else
                           diagnostic.get("reason", "geocoder_no_response")})
    return result

def _persist_hotel_coordinates(database_path: str | Path, search_id: str,
                               hotel: dict[str, Any], coordinates: dict[str, float],
                               coordinate_source: str | None,
                               coordinate_method: str | None = None,
                               coordinate_confidence: str | None = None) -> None:
    if not search_id:
        return
    records = hotel.get("source_records") or [hotel]
    pair = _coordinate_pair(coordinates)
    if not pair:
        return
    retrieved_at = utc_now()
    resolved_source = coordinate_source or "verified_geocoder"
    resolved_method, resolved_confidence = _coordinate_source_metadata(resolved_source)
    resolved_method = coordinate_method or resolved_method
    resolved_confidence = coordinate_confidence or resolved_confidence
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        for record in records:
            source = record.get("source")
            source_hotel_id = record.get("source_hotel_id")
            if not source or source_hotel_id is None:
                continue
            connection.execute(
                """INSERT INTO hotel_coordinate_observation
                (search_id,source,source_hotel_id,latitude,longitude,coordinate_source,
                 coordinate_method,coordinate_confidence,retrieved_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(search_id,source,source_hotel_id) DO UPDATE SET
                    latitude=excluded.latitude,longitude=excluded.longitude,
                    coordinate_source=excluded.coordinate_source,
                    coordinate_method=excluded.coordinate_method,
                    coordinate_confidence=excluded.coordinate_confidence,
                    retrieved_at=excluded.retrieved_at""",
                (search_id, source, str(source_hotel_id), pair["lat"], pair["lng"],
                 resolved_source, resolved_method, resolved_confidence, retrieved_at),
            )
        canonical_id = hotel.get("canonical_hotel_id") or hotel.get("canonical_id")
        if canonical_id:
            existing = connection.execute(
                "SELECT coordinate_source,coordinate_confidence FROM canonical_hotel WHERE id=?",
                (str(canonical_id),),
            ).fetchone()
            incoming_priority = _coordinate_source_priority(resolved_source, resolved_confidence)
            existing_priority = (
                _coordinate_source_priority(existing[0], existing[1])
                if existing and existing[0] else -1
            )
            if existing is None:
                connection.execute(
                    "INSERT OR IGNORE INTO canonical_hotel "
                    "(id,display_name,latitude,longitude,coordinate_source,coordinate_method,"
                    "coordinate_confidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(canonical_id), hotel.get("name") or "", pair["lat"], pair["lng"],
                     resolved_source, resolved_method, resolved_confidence, retrieved_at, retrieved_at),
                )
            elif incoming_priority >= existing_priority:
                connection.execute(
                    "UPDATE canonical_hotel SET latitude=?,longitude=?,coordinate_source=?,"
                    "coordinate_method=?,coordinate_confidence=?,updated_at=? WHERE id=?",
                    (pair["lat"], pair["lng"], resolved_source, resolved_method,
                     resolved_confidence, retrieved_at, str(canonical_id)),
                )
        connection.commit()

def _persist_coordinate_resolution(database_path: str | Path, search_id: str,
                                   hotel: dict[str, Any], status: str,
                                   reason: str | None, attempts: list[dict[str, Any]],
                                   coordinates: dict[str, float] | None = None,
                                   coordinate_source: str | None = None,
                                   coordinate_method: str | None = None,
                                   coordinate_confidence: str | None = None) -> None:
    """Persist the final per-hotel outcome, including failures and conflicts."""
    if not search_id:
        return
    records = hotel.get("source_records") or [hotel]
    keys = {str(hotel.get("canonical_hotel_id") or "")}
    keys.update(
        f"{record.get('source')}:{record.get('source_hotel_id')}"
        for record in records
        if record.get("source") and record.get("source_hotel_id") is not None
    )
    keys.discard("")
    if not keys:
        return
    pair = _coordinate_pair(coordinates)
    method, confidence = _coordinate_source_metadata(coordinate_source)
    method = coordinate_method or method if pair else coordinate_method
    confidence = coordinate_confidence or confidence if pair else coordinate_confidence
    now = utc_now()
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        for key in keys:
            connection.execute(
                """INSERT INTO hotel_coordinate_resolution
                (search_id,hotel_key,canonical_hotel_id,status,reason,coordinate_source,
                 coordinate_method,coordinate_confidence,latitude,longitude,attempts_json,retrieved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(search_id,hotel_key) DO UPDATE SET
                    canonical_hotel_id=excluded.canonical_hotel_id,status=excluded.status,
                    reason=excluded.reason,coordinate_source=excluded.coordinate_source,
                    coordinate_method=excluded.coordinate_method,
                    coordinate_confidence=excluded.coordinate_confidence,
                    latitude=excluded.latitude,longitude=excluded.longitude,
                    attempts_json=excluded.attempts_json,retrieved_at=excluded.retrieved_at""",
                (search_id, key, hotel.get("canonical_hotel_id"), status, reason,
                 coordinate_source, method, confidence,
                 pair["lat"] if pair else None, pair["lng"] if pair else None,
                 json.dumps(attempts, ensure_ascii=False, default=str), now),
            )
        connection.commit()

def _persist_provider_metadata(database_path: str | Path, search_id: str,
                               source: str, source_hotel_id: str,
                               metadata: dict[str, Any]) -> None:
    if not search_id or not source or not source_hotel_id or not isinstance(metadata, dict):
        return
    provider = str(metadata.get("provider") or "unknown")
    retrieved_at = str(metadata.get("retrieved_at") or utc_now())
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            """INSERT INTO hotel_provider_metadata
            (search_id,source,source_hotel_id,provider,metadata_json,retrieved_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(search_id,source,source_hotel_id,provider) DO UPDATE SET
              metadata_json=excluded.metadata_json,retrieved_at=excluded.retrieved_at""",
            (search_id, source, str(source_hotel_id), provider,
             json.dumps(metadata, ensure_ascii=False, default=str), retrieved_at),
        )
        connection.commit()

def _coordinate_retryable(reason: str | None) -> bool:
    text = str(reason or "").casefold()
    return not any(token in text for token in ("403", "401", "captcha", "challenge", "access_denied"))

def _resolve_hotel_coordinates(database_path: str | Path, hotel: dict[str, Any],
                               city: str, country: str | None,
                               city_slug: str | None = None,
                               country_slug: str | None = None) -> dict[str, Any]:
    """Run every verified coordinate source and return a traceable outcome."""
    attempts: list[dict[str, Any]] = []
    candidates: list[tuple[dict[str, float], str, str, str]] = []

    def add_candidate(value: Any, source: str | None, label: str) -> None:
        pair = _coordinate_pair(value)
        if not pair:
            return
        method, confidence = _coordinate_source_metadata(source)
        candidates.append((pair, source or label, method, confidence))

    location = hotel.get("location") or {}
    add_candidate(location.get("coordinates"), location.get("coordinate_source"), "provider")
    for record in hotel.get("source_records") or []:
        add_candidate(record.get("coordinates"), record.get("coordinate_source") or record.get("source"),
                      record.get("source") or "provider")
        add_candidate(record.get("geocoded_coordinates"), record.get("geocoded_coordinate_source"),
                      "persisted_enrichment")

    def conflict_between(values: list[tuple[dict[str, float], str, str, str]]) -> dict[str, Any] | None:
        for index, (pair, source, _method, _confidence) in enumerate(values):
            for other, other_source, _other_method, _other_confidence in values[index + 1:]:
                distance = _coordinate_distance_m(pair, other)
                if distance is not None and distance > 25_000:
                    return {"sources": [source, other_source], "distance_meters": round(distance, 1)}
        return None

    existing_conflict = conflict_between(candidates)
    if existing_conflict:
        return {"coordinates": None, "status": "CONFLICT", "reason": "coordinate_sources_disagree",
                "attempts": [{"source": source, "status": "PROVIDER_COORDINATES"}
                             for _pair, source, _method, _confidence in candidates],
                "conflict": existing_conflict}
    if candidates:
        pair, source, method, confidence = candidates[0]
        return {"coordinates": pair, "status": "PROVIDER_DETAIL" if method == "hotel_detail_structured_data"
                else "GEOCODER" if method == "address_geocoding" else "PROVIDER_COORDINATES",
                "reason": "verified_coordinate_already_available", "attempts": [
                    {"source": source, "method": method, "status": "SUCCESS"}],
                "coordinate_source": source, "coordinate_method": method,
                "coordinate_confidence": confidence}

    def run(label: str, callback: Any) -> dict[str, Any] | None:
        for attempt_number in range(1, 4):
            diagnostic: dict[str, Any] = {}
            try:
                value = callback(diagnostic)
            except Exception as exc:  # A resolver must not abort other hotels.
                diagnostic.update({"status": "ERROR", "reason": f"{type(exc).__name__}:{exc}"})
                value = None
            status = str(diagnostic.get("status") or "NOT_FOUND")
            entry = {"source": label, "attempt": attempt_number, "status": status,
                     "reason": diagnostic.get("reason")}
            attempts.append(entry)
            if value:
                pair = _coordinate_pair(
                    value.get("coordinates") if isinstance(value, dict) and "coordinates" in value else value
                )
                if pair:
                    source = (value.get("coordinate_source") if isinstance(value, dict) else None) \
                        or ("verified_geocoder" if label == "geocoder" else label)
                    method, confidence = _coordinate_source_metadata(source)
                    return {"address": value.get("address") if isinstance(value, dict) else None,
                            "coordinates": pair, "status": "PROVIDER_DETAIL" if method == "hotel_detail_structured_data"
                            else "GEOCODER" if method == "address_geocoding" else "PROVIDER_COORDINATES",
                            "reason": diagnostic.get("reason") or "verified_coordinate",
                            "coordinate_source": source, "coordinate_method": method,
                            "coordinate_confidence": confidence}
                if isinstance(value, dict) and value.get("address"):
                    entry["address"] = value["address"]
            if status != "ERROR" or attempt_number >= 3 or not _coordinate_retryable(diagnostic.get("reason")):
                break
            time.sleep(0.25 * attempt_number)
        return None

    has_tripcom_detail = any(offer.get("source") == "trip_com" and offer.get("deeplink")
                             for offer in hotel.get("offers", []))
    has_schema_detail = any(
        (urlsplit(str(offer.get("deeplink") or "")).hostname or "").casefold().endswith(
            ("tutu.ru", "ostrovok.ru", "sutochno.ru")) for offer in hotel.get("offers", [])
    )
    details: list[dict[str, Any]] = []
    if has_tripcom_detail:
        result = run("trip_com_detail", lambda diagnostic: _tripcom_detail_address(
            database_path, hotel, city, country, diagnostic))
        if result:
            details.append(result)
    if has_schema_detail:
        result = run("provider_detail", lambda diagnostic: _provider_schema_detail(
            database_path, hotel, city, country, diagnostic))
        if result:
            details.append(result)
    for detail in details:
        add_candidate(detail.get("coordinates"), detail.get("coordinate_source"), "provider_detail")
    detail_conflict = conflict_between(candidates)
    if detail_conflict:
        return {"coordinates": None, "status": "CONFLICT", "reason": "coordinate_sources_disagree",
                "attempts": attempts, "conflict": detail_conflict}
    if candidates:
        pair, source, method, confidence = candidates[0]
        return {"coordinates": pair, "status": "PROVIDER_DETAIL" if method == "hotel_detail_structured_data"
                else "PROVIDER_COORDINATES", "reason": "provider_detail_verified", "attempts": attempts,
                "coordinate_source": source, "coordinate_method": method,
                "coordinate_confidence": confidence}

    address = location.get("address") or hotel.get("address") or next(
        (attempt.get("address") for attempt in attempts if attempt.get("address")), None)
    identity_record = next(
        (record for record in (hotel.get("source_records") or [hotel])
         if record.get("source") and record.get("source_hotel_id") is not None),
        hotel,
    )
    if address and hotel.get("name") and city:
        result = run("geocoder", lambda diagnostic: _geocode_hotel_name(
            database_path, hotel["name"], city, country, address, city_slug, country_slug, diagnostic,
            identity_record.get("source"), identity_record.get("source_hotel_id")))
        if result:
            result["attempts"] = attempts
            return result
    errors = [item for item in attempts if item.get("status") == "ERROR"]
    return {"coordinates": None, "status": "ERROR" if errors else "NOT_FOUND",
            "reason": "coordinate_sources_failed" if errors else "no_verified_coordinate",
            "attempts": attempts}
