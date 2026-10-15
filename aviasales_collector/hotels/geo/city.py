"""Verified city center and destination-geometry profiles."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..models import CityGeoProfile, CityReference, utc_now
from ..runtime import _profiled_sqlite_connect
from ..schema import init_hotel_store
from .core import _valid_coordinate_pair


def register_city_reference(path: str | Path, reference: CityReference) -> None:
    """Persist a validated city reference point for calculated distances."""
    pair = _valid_coordinate_pair(reference.center_lat, reference.center_lon)
    if not pair or not reference.city_slug or not reference.country_slug or not reference.source:
        raise ValueError("city reference must contain valid coordinates and source")
    init_hotel_store(path)
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO hotel_city_reference
            (city_slug,country_slug,name,center_lat,center_lon,source,retrieved_at,method)
            VALUES (?,?,?,?,?,?,?,?)""",
            (reference.city_slug, reference.country_slug, reference.name,
             pair[0], pair[1], reference.source, reference.retrieved_at, reference.method),
        )
        connection.commit()

def _read_city_reference(connection: sqlite3.Connection, query: dict[str, Any]) -> CityReference | None:
    city_slug = str(query.get("city_slug") or "").strip().lower()
    country_slug = str(query.get("country_slug") or "").strip().lower()
    if not city_slug or not country_slug:
        return None
    row = connection.execute(
        "SELECT city_slug,country_slug,name,center_lat,center_lon,source,retrieved_at,method "
        "FROM hotel_city_reference WHERE city_slug=? AND country_slug=?",
        (city_slug, country_slug),
    ).fetchone()
    if not row:
        return None
    pair = _valid_coordinate_pair(row[3], row[4])
    if not pair:
        return None
    # 2026-10-15 Cusco live defect: the pre-fix Nominatim fallback accepted a
    # STATE boundary point (-12.5, -72.5) as the city center - ~125 km from
    # every hotel. Such cached rows are area centroids (half-degree rounded),
    # not city place points; treat them as stale so the resolver re-runs.
    if row[7] == "nominatim_place_point" and (
        abs(pair[0] * 10 - round(pair[0] * 10)) < 1e-6 and abs(pair[1] * 10 - round(pair[1] * 10)) < 1e-6
        and (abs(pair[0] * 2 - round(pair[0] * 2)) < 1e-6 or abs(pair[1] * 2 - round(pair[1] * 2)) < 1e-6)
    ):
        return None
    return CityReference(row[0], row[1], row[2], pair[0], pair[1], row[5], row[6], row[7])

def register_city_geo_profile(path: str | Path, profile: CityGeoProfile) -> None:
    if not profile.city_slug or not profile.country_slug or not profile.source:
        raise ValueError("city geo profile must contain slugs and source")
    init_hotel_store(path)
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO hotel_city_geo_profile
            (city_slug,country_slug,is_beach_destination,source,retrieved_at,evidence)
            VALUES (?,?,?,?,?,?)""",
            (profile.city_slug, profile.country_slug, int(profile.is_beach_destination),
             profile.source, profile.retrieved_at, profile.evidence),
        )
        connection.commit()

def _persist_detected_beach_profile(database_path: str | Path, city: str,
                                    country: str | None, city_slug: str | None,
                                    country_slug: str | None) -> CityGeoProfile:
    """Cache a positive beach discovery without a city-specific allowlist.

    A positive profile is written only after a real OSM ``natural=beach``
    feature has been selected near a verified hotel coordinate. Missing or
    failed geometry never becomes ``false``; it remains unknown and may be
    retried later.

    2026-10-15: a curated profile is authoritative and must not be
    overwritten. The live Brugge smoke had the curated inland=False profile
    replaced by a detected=True one (register_city_geo_profile is
    INSERT OR REPLACE) after OSM matched a small water-side feature tagged
    natural=beach near the canal ring, and cards started showing «до пляжа»
    for an inland city. Detection only fills gaps, never overrides curation.
    """
    resolved_city_slug = (city_slug or "-".join(
        part for part in re.split(r"[-_\s]+", city.casefold()) if part)).casefold()
    resolved_country_slug = (country_slug or "-".join(
        part for part in re.split(r"[-_\s]+", (country or "").casefold()) if part)).casefold()
    if resolved_city_slug and resolved_country_slug:
        with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
            existing = connection.execute(
                "SELECT is_beach_destination,source FROM hotel_city_geo_profile "
                "WHERE city_slug=? AND country_slug=?",
                (resolved_city_slug, resolved_country_slug),
            ).fetchone()
        if existing is not None and str(existing[1]) == "curated_city_destination_profile":
            # The curated data file is authoritative: detection fills gaps,
            # never overrides hand-curated city classification.
            return CityGeoProfile(resolved_city_slug, resolved_country_slug,
                                  bool(existing[0]), str(existing[1]), utc_now(),
                                  "Curated profile preserved; detected geometry ignored.")
    profile = CityGeoProfile(
        resolved_city_slug, resolved_country_slug, True,
        "osm_beach_geometry_detection", utc_now(),
        "Verified OSM natural=beach feature selected near a hotel coordinate.",
    )
    if resolved_city_slug and resolved_country_slug:
        register_city_geo_profile(database_path, profile)
    return profile

def _read_city_geo_profile(connection: sqlite3.Connection, city: str,
                           country: str | None, city_slug: str | None = None,
                           country_slug: str | None = None) -> CityGeoProfile | None:
    city_slug = (city_slug or "-".join(part for part in re.split(r"[-_\s]+", city.casefold()) if part)).casefold()
    country_slug = (country_slug or "-".join(part for part in re.split(r"[-_\s]+", (country or "").casefold()) if part)).casefold()
    row = connection.execute(
        "SELECT city_slug,country_slug,is_beach_destination,source,retrieved_at,evidence "
        "FROM hotel_city_geo_profile WHERE city_slug=? AND country_slug=?",
        (city_slug, country_slug),
    ).fetchone()
    if not row:
        return None
    return CityGeoProfile(row[0], row[1], bool(row[2]), row[3], row[4], row[5])
