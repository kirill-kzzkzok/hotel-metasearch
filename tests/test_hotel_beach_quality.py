from __future__ import annotations

import json
import sqlite3
from urllib.parse import parse_qs

from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.geo import geometry


def _feature(natural: str, *, lon: float, lat: float, osm_id: str):
    return {
        "osm_type": "W", "osm_id": osm_id, "name": None,
        "tags": {"natural": natural},
        "geometry": {"type": "LineString", "coordinates": [[lon, lat], [lon, lat + 0.0001]]},
    }


def test_beach_distance_ignores_coastline_and_uses_only_natural_beach(tmp_path, monkeypatch):
    db = tmp_path / "beach.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(
        geometry,
        "_overpass_beach_features",
        lambda *_args: ([_feature("coastline", lon=55.0001, lat=25.0001, osm_id="coast"),
                         _feature("beach", lon=55.002, lat=25.002, osm_id="beach")],
                        "openstreetmap_overpass"),
    )

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 25.0, "lng": 55.0}, "Dubai", "United Arab Emirates",
        "dubai", "united-arab-emirates", True,
    )

    assert outcome["status"] == "ok"
    assert outcome["public_status"] == "available"
    assert outcome["osm_id"] == "beach"
    assert outcome["method"] == "nearest_osm_beach_geometry"
    assert outcome["distance_meters"] > 200


def test_overpass_parser_rejects_closer_coastline_and_uses_natural_beach(tmp_path, monkeypatch):
    import json

    db = tmp_path / "parsed-overpass-beach.sqlite3"
    init_hotel_store(db)
    payload = {
        "elements": [
            {
                "type": "way", "id": 201,
                "tags": {"natural": "beach", "name": "Verified beach"},
                "geometry": [
                    {"lat": 41.1000, "lon": 1.8010},
                    {"lat": 41.1010, "lon": 1.8010},
                    {"lat": 41.1010, "lon": 1.8020},
                    {"lat": 41.1000, "lon": 1.8020},
                    {"lat": 41.1000, "lon": 1.8010},
                ],
            },
            {
                "type": "way", "id": 202,
                "tags": {"natural": "coastline"},
                "geometry": [
                    {"lat": 41.1000, "lon": 1.8001},
                    {"lat": 41.1005, "lon": 1.8001},
                ],
            },
        ],
    }
    monkeypatch.setattr(geometry, "_overpass_post", lambda *_args, **_kwargs: json.dumps(payload).encode())

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 41.1, "lng": 1.8}, "Sitges", "Spain", "sitges", "spain", True,
    )

    assert outcome["public_status"] == "available"
    assert outcome["osm_id"] == "201"
    assert outcome["method"] == "nearest_osm_beach_geometry"
    assert 50 < outcome["distance_meters"] < 150


def test_beach_overpass_query_omits_coastline_geometry_that_is_never_eligible(tmp_path, monkeypatch):
    db = tmp_path / "minimal-beach-query.sqlite3"
    init_hotel_store(db)
    captured = {}

    def capture_request(_url, data, _headers, _stage, _max_bytes, **_kwargs):
        captured["query"] = parse_qs(data.decode("utf-8"))["data"][0]
        return json.dumps({"elements": []}).encode("utf-8")

    monkeypatch.setattr(geometry, "_overpass_post", capture_request)

    features, _source = geometry._overpass_beach_features(db, 25.0, 55.0)

    query = captured["query"]
    assert query.count('["natural"="beach"]') == 3
    assert 'node["natural"="beach"]' in query
    assert 'way["natural"="beach"]' in query
    assert 'relation["natural"="beach"]' in query
    assert "coastline" not in query
    assert ");out geom tags;" in query
    assert features == []


def test_beach_without_natural_beach_is_not_found(tmp_path, monkeypatch):
    db = tmp_path / "beach.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(
        geometry,
        "_overpass_beach_features",
        lambda *_args: ([_feature("coastline", lon=55.0001, lat=25.0001, osm_id="coast")],
                        "openstreetmap_overpass"),
    )
    monkeypatch.setattr(geometry, "_profiled_urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Photon should not be needed after successful empty Overpass feature set")))

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 25.0, "lng": 55.0}, "Dubai", "United Arab Emirates",
        "dubai", "united-arab-emirates", True,
    )

    assert outcome["status"] == "not_found"
    assert outcome.get("distance_meters") is None


def test_overpass_429_is_source_unavailable_and_does_not_use_photon(tmp_path, monkeypatch):
    db = tmp_path / "beach.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(geometry, "_overpass_beach_features", lambda *_args: (None, "openstreetmap_overpass"))
    tile_key, _lat, _lon = geometry._overpass_tile_key(25.0, 55.0)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO hotel_beach_search_cache(tile_key,features_json,source,retrieved_at,status,diagnostic_json) VALUES (?,?,?,?,?,?)",
            (tile_key, None, "openstreetmap_overpass", geometry.utc_now(), "error",
             json.dumps({"http_status": 429, "exception": "HTTPStatusError"})),
        )
        connection.commit()
    def fail_photon(*_args, **_kwargs):
        raise AssertionError("Photon must not be used to bypass an Overpass 429")
    monkeypatch.setattr(geometry, "_profiled_urlopen", fail_photon)

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 25.0, "lng": 55.0}, "Dubai", "United Arab Emirates",
        "dubai", "united-arab-emirates", True,
    )

    assert outcome["status"] == "error"
    assert outcome["public_status"] == "source_unavailable"
    assert outcome["beach_geometry_available"] is False


def test_missing_coordinates_has_explicit_status(tmp_path):
    db = tmp_path / "beach.sqlite3"
    init_hotel_store(db)
    outcome = geometry._resolve_nearest_beach(
        db, None, "Dubai", "United Arab Emirates",
        "dubai", "united-arab-emirates", True,
    )
    assert outcome["status"] == "coordinate_missing"
    assert outcome["public_status"] == "no_coordinates"


def test_unlisted_coastal_city_is_detected_from_real_beach_feature(tmp_path, monkeypatch):
    db = tmp_path / "beach.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(
        geometry,
        "_overpass_beach_features",
        lambda *_args: ([_feature("beach", lon=98.3005, lat=7.9005, osm_id="phuket-beach")],
                        "openstreetmap_overpass"),
    )

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 7.9, "lng": 98.3}, "Example Coastal City", "Thailand",
        "example-coastal-city", "thailand", True,
    )

    assert outcome["public_status"] == "available"
    assert outcome["osm_id"] == "phuket-beach"
    assert outcome["beach_relevant"] is True


def test_photon_candidates_cannot_prove_nearest_beach_distance(tmp_path, monkeypatch):
    db = tmp_path / "beach.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(geometry, "_overpass_beach_features",
                        lambda *_args: (None, "openstreetmap_overpass"))
    tile_key, _lat, _lon = geometry._overpass_tile_key(7.9, 98.3)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO hotel_beach_search_cache(tile_key,features_json,source,retrieved_at,status,diagnostic_json) "
            "VALUES (?,?,?,?,?,?)",
            (tile_key, None, "openstreetmap_overpass", geometry.utc_now(), "error",
             json.dumps({"http_status": 504, "exception": "TimeoutError"})),
        )
        connection.commit()
    features = []
    for index in range(12):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [98.3 + (index + 1) * 0.0001, 7.9]},
            "properties": {"osm_key": "natural", "osm_value": "beach", "osm_type": "W",
                           "osm_id": str(1000 + index), "name": f"Beach {index}"},
        })
    monkeypatch.setattr(geometry, "_profiled_urlopen", lambda *_a, **_k: _PhotonResponse(features))
    checked = []
    def beach_geometry(_db, osm_type, osm_id):
        checked.append(osm_id)
        longitude = 98.3 + (int(osm_id) - 999) * 0.0001
        return {"type": "LineString", "coordinates": [[longitude, 7.9], [longitude, 7.9001]]}
    monkeypatch.setattr(geometry, "_beach_geometry", beach_geometry)

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 7.9, "lng": 98.3}, "Example Coastal City", "Thailand",
        "example-coastal-city", "thailand", True,
    )

    assert outcome["public_status"] == "unknown"
    assert "distance_meters" not in outcome
    assert "osm_id" not in outcome
    assert checked == []


def test_empty_photon_search_after_overpass_failure_is_unknown_not_not_found(tmp_path, monkeypatch):
    db = tmp_path / "empty-photon-beach.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(geometry, "_overpass_beach_features",
                        lambda *_args: (None, "openstreetmap_overpass"))
    monkeypatch.setattr(geometry, "_profiled_urlopen", lambda *_a, **_k: _PhotonResponse([]))

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 16.037975, "lng": 108.250183}, "Da Nang", "Vietnam",
        "da-nang", "vietnam", True,
    )

    assert outcome["status"] == "unknown"
    assert outcome["public_status"] == "unknown"
    assert "distance_meters" not in outcome


class _PhotonResponse:
    def __init__(self, features):
        self._payload = json.dumps({"features": features}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return None
    def read(self, *_args):
        return self._payload


def test_beach_node_without_shoreline_geometry_is_not_a_distance(tmp_path, monkeypatch):
    import json

    db = tmp_path / "beach-point-unknown.sqlite3"
    init_hotel_store(db)
    monkeypatch.setattr(geometry, "_overpass_beach_features", lambda *_args: (None, "openstreetmap_overpass"))
    monkeypatch.setattr(
        geometry, "_profiled_urlopen",
        lambda *_args, **_kwargs: _PhotonResponse([{
            "type": "Feature", "geometry": {"type": "Point", "coordinates": [108.2595335, 16.025095]},
            "properties": {"osm_key": "natural", "osm_value": "beach", "osm_type": "N",
                           "osm_id": "4347089780", "name": "My An Beach"},
        }]),
    )
    monkeypatch.setattr(geometry, "_beach_geometry",
                        lambda *_args: {"type": "Point", "coordinates": [108.2595335, 16.025095]})

    outcome = geometry._resolve_nearest_beach(
        db, {"lat": 16.037975311279297, "lng": 108.25018310546875},
        "Da Nang", "Vietnam", "da-nang", "vietnam", True,
    )

    assert outcome["public_status"] == "unknown"
    assert outcome["beach_geometry_available"] is None
    assert "distance_meters" not in outcome
    assert "osm_id" not in outcome


def test_old_point_cache_is_replaced_by_verified_beach_geometry(tmp_path, monkeypatch):
    from aviasales_collector.hotels.geo.core import _hotel_beach_cache_key

    db = tmp_path / "stale-beach-point-cache.sqlite3"
    init_hotel_store(db)
    coordinates = {"lat": 25.0, "lng": 55.0}
    cache_key = _hotel_beach_cache_key(coordinates, "Dubai", "United Arab Emirates")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO hotel_beach_cache "
            "(cache_key,distance_meters,beach_name,source,retrieved_at,status,beach_osm_type,"
            "beach_osm_id,beach_method,beach_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cache_key, 1746.3795, "My An Beach", "openstreetmap_overpass", geometry.utc_now(),
             "ok", "N", "4347089780", "nearest_osm_beach_point", "medium"),
        )

    monkeypatch.setattr(
        geometry, "_overpass_beach_features",
        lambda *_args: ([_feature("beach", lon=55.001, lat=25.0, osm_id="verified-way")],
                        "openstreetmap_overpass"),
    )
    outcome = geometry._resolve_nearest_beach(
        db, coordinates, "Dubai", "United Arab Emirates", "dubai", "united-arab-emirates", True,
    )

    assert outcome["public_status"] == "available"
    assert outcome["osm_id"] == "verified-way"
    assert outcome["method"] == "nearest_osm_beach_geometry"
    assert outcome["distance_meters"] < 200
    with sqlite3.connect(db) as connection:
        persisted = connection.execute(
            "SELECT distance_meters,beach_osm_id,beach_method FROM hotel_beach_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()
    assert persisted[0] < 200
    assert persisted[1:] == ("verified-way", "nearest_osm_beach_geometry")
