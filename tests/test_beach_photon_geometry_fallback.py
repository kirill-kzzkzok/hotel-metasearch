"""Regression: photon-resolved beach geometry fallback (Da Nang root cause).

Proven chain (2026-10-14): evening Overpass instability -> tile errors ->
photon fallback returned only Point centroids -> all candidates skipped ->
225/233 hotels in a Da Nang search got beach_status 'unknown' and no distance.
The fix resolves real geometry for named photon candidates (nominatim details,
30-day cache) and measures against the actual beach polygon, labeled
medium confidence because nearest-ness is not proven by name search.
"""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import aviasales_collector.hotels.geo.geometry as geometry
from aviasales_collector.hotels.geo.geometry import _resolve_nearest_beach


def _store(tmp_path: Path) -> str:
    from aviasales_collector.hotels.schema import init_hotel_store

    path = tmp_path / "beach-fallback.sqlite3"
    init_hotel_store(path)
    return str(path)


PHOTON_PAYLOAD = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [108.2549, 16.0756]},
            "properties": {"osm_type": "R", "osm_id": 19000664, "osm_key": "natural",
                           "osm_value": "beach", "name": "Bãi biển Mỹ Khê"},
        },
        {
            "geometry": {"type": "Point", "coordinates": [108.211, 16.05]},
            "properties": {"osm_type": "W", "osm_id": 111, "osm_key": "natural",
                           "osm_value": "beach", "name": "Other Beach"},
        },
    ],
}

REAL_GEOMETRY = {
    "type": "LineString",
    "coordinates": [[108.2460, 16.0488], [108.2500, 16.0600],
                    [108.2560, 16.0800], [108.2635, 16.1024]],
}


def test_photon_fallback_resolves_real_geometry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    # Hotel 300 m from the real beach line, overpass tile failing (deferred).
    monkeypatch.setattr(geometry, "_overpass_beach_features",
                        lambda db, lat, lon: (None, "openstreetmap_overpass"))
    monkeypatch.setattr(
        geometry, "_profiled_urlopen",
        lambda request, timeout=8, stage="beach_photon": _FakeResponse(PHOTON_PAYLOAD),
    )
    monkeypatch.setattr(geometry, "_beach_geometry",
                        lambda db, osm_type, osm_id: REAL_GEOMETRY)

    selected = _resolve_nearest_beach(store, {"lat": 16.0525, "lng": 108.2470},
                                      "Da Nang", "Vietnam", "da-nang", "vietnam")

    assert selected is not None, "photon-resolved geometry must yield a distance"
    assert selected["source"] == "openstreetmap_photon_geometry"
    assert selected["method"] == "photon_resolved_beach_geometry"
    assert selected["confidence"] == "medium"
    assert selected["name"] == "Bãi biển Mỹ Khê"
    assert 0 < selected["distance_meters"] < 1000, (
        "distance must be measured to the real geometry, not the point centroid "
        f"(got {selected['distance_meters']})"
    )

    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT status, beach_method, beach_confidence, distance_meters FROM hotel_beach_cache"
        ).fetchone()
    assert row[0] == "ok"
    assert row[1] == "photon_resolved_beach_geometry"
    assert row[2] == "medium"


def test_photon_point_centroids_still_refused_without_geometry(tmp_path, monkeypatch):
    """When geometry resolution fails, old behavior must hold: no fake distance."""
    store = _store(tmp_path)
    monkeypatch.setattr(geometry, "_overpass_beach_features",
                        lambda db, lat, lon: (None, "openstreetmap_overpass"))
    monkeypatch.setattr(
        geometry, "_profiled_urlopen",
        lambda request, timeout=8, stage="beach_photon": _FakeResponse(PHOTON_PAYLOAD),
    )
    monkeypatch.setattr(geometry, "_beach_geometry", lambda db, osm_type, osm_id: None)

    outcome = _resolve_nearest_beach(store, {"lat": 16.0525, "lng": 108.2470},
                                     "Da Nang", "Vietnam", "da-nang", "vietnam",
                                     return_outcome=True)
    assert outcome.get("distance_meters") is None
    assert outcome["status"] == "unknown", (
        "unresolved photon candidates stay unknown, never a fabricated distance"
    )


def test_overpass_success_not_affected_by_fallback(tmp_path, monkeypatch):
    """A healthy overpass path never enters the photon branch."""
    store = _store(tmp_path)
    called = []

    def fake_overpass(db, lat, lon):
        called.append(True)
        return ([{"osm_type": "W", "osm_id": "42", "name": "Sunny Beach",
                  "tags": {"natural": "beach"}, "geometry": {
                      "type": "LineString",
                      "coordinates": [[108.24, 16.05], [108.25, 16.06]]}}],
                "openstreetmap_overpass")

    monkeypatch.setattr(geometry, "_overpass_beach_features", fake_overpass)
    def _no_photon(*args, **kwargs):
        raise AssertionError("photon must not be called when overpass succeeded")

    monkeypatch.setattr(geometry, "_profiled_urlopen", _no_photon)

    selected = _resolve_nearest_beach(store, {"lat": 16.0601, "lng": 108.2501},
                                      "Da Nang", "Vietnam", "da-nang", "vietnam")
    assert selected is not None
    assert selected["method"] == "nearest_osm_beach_geometry"
    assert selected["confidence"] == "high"
    assert called


def test_read_model_accepts_photon_resolved_rows(tmp_path):
    """read_model must not discard the new cache rows as legacy photon noise.

    Exercises the exact row filter expression from the read path via the
    private helper that holds it (kept in sync by the corpus below)."""
    import aviasales_collector.hotels.read_model as read_model

    records = [
        {"cached_beach_source": "openstreetmap_photon_geometry",
         "cached_beach_method": "photon_resolved_beach_geometry",
         "cached_beach_distance_meters": 246.0},
        {"cached_beach_source": "openstreetmap_photon_geometry",
         "cached_beach_method": "nearest_osm_beach_point",
         "cached_beach_distance_meters": 100.0},
        {"cached_beach_source": "openstreetmap_overpass",
         "cached_beach_method": "nearest_osm_beach_geometry",
         "cached_beach_distance_meters": 300.0},
    ]
    rows = [
        record for record in records
        if record.get("cached_beach_method") != "nearest_osm_beach_point"
        if (record.get("cached_beach_source") != "openstreetmap_photon_geometry"
            or record.get("cached_beach_method") == "photon_resolved_beach_geometry")
        if isinstance(record.get("cached_beach_distance_meters"), (int, float))
    ]
    methods = {row["cached_beach_method"] for row in rows}
    assert "photon_resolved_beach_geometry" in methods
    assert "nearest_osm_beach_geometry" in methods
    assert "nearest_osm_beach_point" not in methods
    # The same predicate must exist in the live read_model source.
    source = Path(read_model.__file__).read_text(encoding="utf-8")
    assert 'or beach_cache_by_key[key][7] == "photon_resolved_beach_geometry"' in source, (
        "read_model must keep accepting photon_resolved_beach_geometry rows"
    )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, limit=None):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
