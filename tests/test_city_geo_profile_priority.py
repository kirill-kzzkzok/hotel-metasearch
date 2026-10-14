"""Curated city geo profiles must survive OSM beach detection.

Live Brugge smoke (2026-10-15): the curated profile brugge/belgium=False was
REPLACED by osm_beach_geometry_detection=True (register_city_geo_profile is
INSERT OR REPLACE) after OSM matched a small natural=beach feature near the
canal ring; cards then showed «до пляжа» for an inland city. Detection may
fill a missing profile, never override an existing non-detected one.
"""
from __future__ import annotations

import sqlite3

from aviasales_collector.hotels.geo.city import (
    _persist_detected_beach_profile,
    _read_city_geo_profile,
    register_city_geo_profile,
)
from aviasales_collector.hotels.models import CityGeoProfile, utc_now


def test_detection_does_not_override_curated_profile(tmp_path):
    db = tmp_path / "geo.sqlite3"
    register_city_geo_profile(db, CityGeoProfile(
        "brugge", "belgium", False, "curated_city_destination_profile",
        utc_now(), "Inland medieval city."))
    # init would seed the same curated value (INSERT OR IGNORE)
    from aviasales_collector.hotels.schema import init_hotel_store
    init_hotel_store(db)

    returned = _persist_detected_beach_profile(db, "Brugge", "Бельгия", "brugge", "belgium")

    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT is_beach_destination,source FROM hotel_city_geo_profile "
            "WHERE city_slug='brugge' AND country_slug='belgium'").fetchall()
    assert rows == [(0, "curated_city_destination_profile")], (
        "curated profile must survive detection")
    assert returned.is_beach_destination is False
    assert returned.source == "curated_city_destination_profile"


def test_detection_fills_missing_profile(tmp_path):
    db = tmp_path / "geo.sqlite3"
    from aviasales_collector.hotels.schema import init_hotel_store
    init_hotel_store(db)
    # lille is not in the curated file: detection must create the profile
    returned = _persist_detected_beach_profile(db, "Lille", "France", "lille", "france")
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT is_beach_destination,source FROM hotel_city_geo_profile "
            "WHERE city_slug='lille' AND country_slug='france'").fetchall()
    assert rows == [(1, "osm_beach_geometry_detection")]
    assert returned.is_beach_destination is True


def test_detection_can_refresh_its_own_profile(tmp_path):
    db = tmp_path / "geo.sqlite3"
    register_city_geo_profile(db, CityGeoProfile(
        "zanjbar", "croatia", True, "osm_beach_geometry_detection",
        utc_now(), "detected earlier"))
    # re-detection must still be able to rewrite its own row
    _persist_detected_beach_profile(db, "Zanjbar", "Croatia", "zanjbar", "croatia")
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT is_beach_destination,source FROM hotel_city_geo_profile "
            "WHERE city_slug='zanjbar' AND country_slug='croatia'").fetchall()
    assert rows == [(1, "osm_beach_geometry_detection")]


def test_read_profile_reflects_curated_priority(tmp_path):
    db = tmp_path / "geo.sqlite3"
    register_city_geo_profile(db, CityGeoProfile(
        "paris", "france", False, "curated_city_destination_profile",
        utc_now(), "Inland."))
    _persist_detected_beach_profile(db, "Paris", "France", "paris", "france")
    with sqlite3.connect(db) as connection:
        profile = _read_city_geo_profile(connection, "Paris", "France", "paris", "france")
    assert profile.is_beach_destination is False
