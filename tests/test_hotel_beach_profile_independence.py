"""Regression tests for beach lookup independent of curated city flags."""

from __future__ import annotations

import sqlite3

import pytest


def _feature(osm_id: str, tags: dict[str, str], lat: float, lon: float) -> dict:
    return {
        "osm_type": "W",
        "osm_id": osm_id,
        "name": None,
        "tags": tags,
        "geometry": {"type": "LineString", "coordinates": [[lon, lat], [lon, lat + 0.0001]]},
    }


@pytest.mark.parametrize("existing_profile", [None, False], ids=["new-city", "negative-profile"])
def test_real_beach_is_found_without_positive_city_flag(tmp_path, monkeypatch, existing_profile):
    from aviasales_collector.hotel_search import init_hotel_store
    from aviasales_collector.hotels.geo import geometry
    from aviasales_collector.hotels.geo.city import (
        _read_city_geo_profile, register_city_geo_profile,
    )
    from aviasales_collector.hotels.models import CityGeoProfile, utc_now

    db = tmp_path / "beach-profile-independent.sqlite3"
    init_hotel_store(db)
    if existing_profile is False:
        register_city_geo_profile(
            db, CityGeoProfile("cancun", "mexico", False, "test", utc_now(), "negative fixture"),
        )

    # The real beach is farther than unrelated water/coastline features. Only
    # natural=beach may establish the distance or promote the city profile.
    lookup_calls = []

    def beach_features(*_args):
        lookup_calls.append("overpass")
        return ([
            _feature("301", {"natural": "coastline"}, 21.16000, -86.85100),
            _feature("302", {"natural": "water"}, 21.16001, -86.85100),
            _feature("303", {"natural": "lake"}, 21.16002, -86.85100),
            _feature("304", {"waterway": "river"}, 21.16003, -86.85100),
            _feature("305", {"waterway": "canal"}, 21.16004, -86.85100),
            _feature("306", {"natural": "beach"}, 21.16400, -86.85100),
        ], "openstreetmap_overpass")

    monkeypatch.setattr(geometry, "_overpass_beach_features", beach_features)

    result = geometry._resolve_nearest_beach(
        db, {"lat": 21.16, "lng": -86.851}, "Cancun", "Mexico",
        "cancun", "mexico", True,
    )

    assert result["public_status"] == "available"
    assert result["osm_id"] == "306"
    assert result["method"] == "nearest_osm_beach_geometry"
    assert lookup_calls == ["overpass"]
    with sqlite3.connect(db) as connection:
        profile = _read_city_geo_profile(connection, "Cancun", "Mexico", "cancun", "mexico")
    assert profile is not None and profile.is_beach_destination is True


def test_water_coastline_river_and_canal_do_not_create_beach_or_city_flag(tmp_path, monkeypatch):
    from aviasales_collector.hotel_search import init_hotel_store
    from aviasales_collector.hotels.geo import geometry
    from aviasales_collector.hotels.geo.city import _read_city_geo_profile

    db = tmp_path / "non-beach-features.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(
        geometry, "_overpass_beach_features",
        lambda *_args: ([
            _feature("401", {"natural": "coastline"}, 21.16000, -86.85100),
            _feature("402", {"natural": "water"}, 21.16001, -86.85100),
            _feature("403", {"natural": "lake"}, 21.16002, -86.85100),
            _feature("404", {"waterway": "river"}, 21.16003, -86.85100),
            _feature("405", {"waterway": "canal"}, 21.16004, -86.85100),
        ], "openstreetmap_overpass"),
    )

    result = geometry._resolve_nearest_beach(
        db, {"lat": 21.16, "lng": -86.851}, "Cancun", "Mexico",
        "cancun", "mexico", True,
    )

    assert result["public_status"] == "not_found"
    assert "osm_id" not in result
    with sqlite3.connect(db) as connection:
        profile = _read_city_geo_profile(connection, "Cancun", "Mexico", "cancun", "mexico")
    assert profile is None


def test_concurrent_hotels_share_one_overpass_tile_request(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import json
    import time

    from aviasales_collector.hotel_search import init_hotel_store
    from aviasales_collector.hotels.geo import geometry

    db = tmp_path / "shared-overpass-tile.sqlite3"
    init_hotel_store(db)
    calls = []
    start = Barrier(8)
    payload = json.dumps({"elements": [{
        "type": "way", "id": 601, "tags": {"natural": "beach"},
        "geometry": [{"lat": 21.16, "lon": -86.851},
                     {"lat": 21.161, "lon": -86.851}],
    }]}).encode("utf-8")

    def fake_overpass(*_args, **_kwargs):
        calls.append(1)
        time.sleep(0.1)
        return payload

    monkeypatch.setattr(geometry, "_overpass_post", fake_overpass)

    def request_same_tile(_index):
        start.wait(timeout=2)
        return geometry._overpass_beach_features(db, 21.16, -86.851)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(request_same_tile, range(8)))

    assert len(calls) == 1
    assert all(result[0] and result[0][0]["tags"] == {"natural": "beach"} for result in results)


def test_photon_representative_point_without_geometry_has_unknown_distance(tmp_path, monkeypatch):
    import json

    from aviasales_collector.hotel_search import init_hotel_store
    from aviasales_collector.hotels.geo import geometry

    class PhotonResponse:
        def __init__(self, features):
            self.payload = json.dumps({"features": features}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, *_args):
            return self.payload

    db = tmp_path / "photon-point-retention.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(geometry, "_overpass_beach_features", lambda *_args: (None, "openstreetmap_overpass"))
    photon_features = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [98.3 + (i + 1) * 0.0001, 7.9]},
        "properties": {"osm_key": "natural", "osm_value": "beach", "osm_type": "W",
                       "osm_id": str(700 + i), "name": f"Beach {i}"},
    } for i in range(4)]
    monkeypatch.setattr(geometry, "_profiled_urlopen", lambda *_args, **_kwargs: PhotonResponse(photon_features))
    detail_lookups = []
    def unavailable_geometry(_db, osm_type, osm_id):
        detail_lookups.append(osm_id)
        return None
    monkeypatch.setattr(geometry, "_beach_geometry", unavailable_geometry)

    result = geometry._resolve_nearest_beach(
        db, {"lat": 7.9, "lng": 98.3}, "Phuket", "Thailand", "phuket", "thailand", True,
    )

    assert detail_lookups == []
    # Photon returned only a partial, name-ranked beach inventory. Its closest
    # candidates cannot establish the actual nearest beach distance.
    assert result["public_status"] == "unknown"
    assert result["status"] == "unknown"
    assert result["beach_geometry_available"] is None
    assert "distance_meters" not in result
    assert "osm_id" not in result
