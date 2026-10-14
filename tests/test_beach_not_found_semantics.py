"""Regression: a proven no-beach tile must not be masked as unknown (spec §7).

Paris case (live 2026-10-14): 64 hotels sat on tiles where Overpass answered
'not_found' (no beach within radius - true for central Paris), yet their
hotel_beach_cache rows said 'unknown' because the photon fallback returned
only far/inland candidates that were correctly refused. Status semantics:
a successful check with no beach = not_found, never unknown.
"""
from __future__ import annotations

import json
import sqlite3

import aviasales_collector.hotels.geo.geometry as geometry
from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.geo.geometry import _resolve_nearest_beach, _overpass_tile_key


def _store(tmp_path):
    path = tmp_path / "no-beach-tile.sqlite3"
    init_hotel_store(path)
    return path


def test_overpass_not_found_tile_yields_not_found_not_unknown(tmp_path, monkeypatch):
    path = _store(tmp_path)
    coordinates = {"lat": 48.8566, "lng": 2.3522}  # central Paris
    tile_key, _, _ = _overpass_tile_key(coordinates["lat"], coordinates["lng"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO hotel_beach_search_cache"
            "(tile_key,features_json,source,retrieved_at,status,diagnostic_json) VALUES (?,?,?,?,?,?)",
            (tile_key, None, "openstreetmap_overpass", geometry.utc_now(), "not_found",
             json.dumps({"query_type": "beach_geometry", "objects": 0, "features": 0})),
        )
        connection.commit()

    # Overpass "fails" from the caller's perspective (features None) because the
    # cached tile is a terminal negative; photon returns only far candidates.
    monkeypatch.setattr(geometry, "_overpass_beach_features",
                        lambda *_args: (None, "openstreetmap_overpass"))

    class _PhotonResponse:
        def __init__(self, features):
            self.payload = json.dumps({"features": features}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, *_args):
            return self.payload

    # Far inland "beach" labels (Normandy coast, park event) - must be refused.
    far = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.2, 49.7]},
            "properties": {"osm_key": "natural", "osm_value": "beach", "osm_type": "W",
                           "osm_id": "555", "name": "Plage du Puys"}}]
    monkeypatch.setattr(geometry, "_profiled_urlopen",
                        lambda *_a, **_k: _PhotonResponse(far))
    monkeypatch.setattr(geometry, "_beach_geometry", lambda *_a: None)

    outcome = _resolve_nearest_beach(path, coordinates, "Paris", "France",
                                     return_outcome=True)
    assert outcome["status"] == "not_found", (
        "overpass-verified no-beach tile must surface as not_found, not unknown"
    )
    assert outcome["public_status"] == "not_found"
    assert outcome.get("distance_meters") is None

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT status FROM hotel_beach_cache").fetchone()
    assert row[0] == "not_found"


def test_overpass_error_tile_stays_unknown_when_photon_unresolved(tmp_path, monkeypatch):
    """A transport-failed tile is NOT a proven negative: stays unknown."""
    path = _store(tmp_path)
    coordinates = {"lat": 48.8566, "lng": 2.3522}
    tile_key, _, _ = _overpass_tile_key(coordinates["lat"], coordinates["lng"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO hotel_beach_search_cache"
            "(tile_key,features_json,source,retrieved_at,status,diagnostic_json) VALUES (?,?,?,?,?,?)",
            (tile_key, None, "openstreetmap_overpass", geometry.utc_now(), "error",
             json.dumps({"exception": "ReadTimeout"})),
        )
        connection.commit()

    monkeypatch.setattr(geometry, "_overpass_beach_features",
                        lambda *_args: (None, "openstreetmap_overpass"))

    class _PhotonResponse:
        def __init__(self, features):
            self.payload = json.dumps({"features": features}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, *_args):
            return self.payload

    monkeypatch.setattr(geometry, "_profiled_urlopen",
                        lambda *_a, **_k: _PhotonResponse([]))  # empty search
    outcome = _resolve_nearest_beach(path, coordinates, "Paris", "France",
                                     return_outcome=True)
    assert outcome["status"] == "unknown"
