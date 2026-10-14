"""Nearby OSM lookups are staged: metro first, wide sweep fail-safe.

Live audit 2026-10-15 (Overpass in a busy period): the single 5-category
query with around:8000 succeeded 0/7 times (dispatcher timeout), while a
metro-only around:2000 query returned in 1-5s. One combined query meant the
card's metro distance was starved by the heavy landmark/airport sweep.
The lookup now runs two stages; stage 2 failing must not discard stage 1.
"""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import parse_qs

from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.geo import geometry


def _metro_elements():
    return {
        "elements": [
            {"type": "node", "id": 1001, "lat": 51.5090, "lon": -0.1270,
             "tags": {"railway": "subway_entrance", "name": "Charing Cross"}},
        ],
    }


def test_metro_stage_result_survives_wide_stage_failure(tmp_path, monkeypatch):
    db = tmp_path / "staged-nearby.sqlite3"
    init_hotel_store(db)
    stages = []

    def fake_overpass(url, data, headers, stage, max_bytes, deadline_seconds=12.0):
        stages.append(stage)
        if stage == "nearby_overpass":
            return json.dumps(_metro_elements()).encode()
        raise TimeoutError("wide sweep timed out")

    monkeypatch.setattr(geometry, "_overpass_post", fake_overpass)
    result = geometry._osm_nearby_features(db, {"lat": 51.5080, "lng": -0.1281})

    assert stages == ["nearby_overpass", "nearby_overpass_wide"]
    assert result["status"] == "ok"
    assert "metro" in result["places"], "stage 1 (metro) must survive stage 2 failure"
    metro = result["places"]["metro"]
    assert metro["name"] == "Charing Cross"
    assert metro["distance_meters"] > 0
    assert "airport" not in result["places"]
    assert "landmark" not in result["places"]

    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT features_json, status, diagnostic_json FROM hotel_geo_feature_cache",
        ).fetchone()
    cached = json.loads(row[0])
    assert row[1] == "ok"
    assert any(feature["type"] == "metro" for feature in cached)
    # the failed wide stage is documented in the diagnostic, not silently lost
    diagnostic = json.loads(row[2])
    assert diagnostic.get("exception") == "TimeoutError"


def test_both_stages_failing_is_terminal_source_unavailable(tmp_path, monkeypatch):
    db = tmp_path / "staged-dead.sqlite3"
    init_hotel_store(db)

    def dead(*_args, **_kwargs):
        raise TimeoutError("overpass down")

    monkeypatch.setattr(geometry, "_overpass_post", dead)
    result = geometry._osm_nearby_features(db, {"lat": 51.5080, "lng": -0.1281})
    assert result["status"] == "source_unavailable"
    assert result["places"] == {}


def test_metro_query_uses_bounded_radius(tmp_path, monkeypatch):
    db = tmp_path / "staged-radius.sqlite3"
    init_hotel_store(db)
    queries = []

    def fake_overpass(_url, data, _headers, stage, _max_bytes, deadline_seconds=12.0):
        raw = data if isinstance(data, str) else str(data, "utf-8")
        queries.append(parse_qs(raw).get("data", [""])[0])
        return json.dumps({"elements": []}).encode()

    monkeypatch.setattr(geometry, "_overpass_post", fake_overpass)
    geometry._osm_nearby_features(db, {"lat": 51.5080, "lng": -0.1281})
    assert any("around:2000," in q for q in queries), "metro stage must use the bounded radius"
    assert any("around:8000," in q for q in queries), "wide stage keeps the landmark/airport radius"
    # metro selectors only in stage 1
    metro_query = next(q for q in queries if "around:2000," in q)
    assert "subway_entrance" in metro_query and "tourism" not in metro_query
    wide_query = next(q for q in queries if "around:8000," in q)
    assert "subway_entrance" not in wide_query and "tourism" in wide_query
