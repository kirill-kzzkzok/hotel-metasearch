from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.geo import geometry
from aviasales_collector.hotels.geo.enrichment import _geo_enrichment_cache_is_terminal


@pytest.mark.parametrize("failure", ["read_timeout", "upstream_504"])
def test_nearby_overpass_failure_is_terminal_source_unavailable_and_keeps_retry_cache(
    tmp_path, monkeypatch, failure,
):
    db = tmp_path / "nearby-timeout.sqlite3"
    init_hotel_store(db)
    calls = 0

    def timed_out(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if failure == "read_timeout":
            raise httpx.ReadTimeout("Overpass response read timed out")
        request = httpx.Request("POST", "https://overpass-api.de/api/interpreter")
        response = httpx.Response(504, request=request)
        raise httpx.HTTPStatusError("Overpass dispatcher timed out", request=request, response=response)

    monkeypatch.setattr(geometry, "_overpass_post", timed_out)
    coordinates = {"lat": 25.265, "lng": 55.318}

    first = geometry._osm_nearby_features(db, coordinates)

    assert first == {
        "status": "source_unavailable", "source": "openstreetmap_overpass", "places": {},
    }
    with sqlite3.connect(db) as connection:
        cached = connection.execute(
            "SELECT status,diagnostic_json FROM hotel_geo_feature_cache",
        ).fetchone()
    assert cached[0] == "error"  # Internal error TTL remains short and retryable.
    diagnostic = json.loads(cached[1])
    if failure == "read_timeout":
        assert diagnostic["exception"] == "ReadTimeout"
        assert diagnostic["http_status"] is None
    else:
        assert diagnostic["exception"] == "HTTPStatusError"
        assert diagnostic["http_status"] == 504

    def unexpected_retry(*_args, **_kwargs):
        raise AssertionError("fresh temporary-error cache should avoid an immediate retry")

    monkeypatch.setattr(geometry, "_overpass_post", unexpected_retry)
    second = geometry._osm_nearby_features(db, coordinates)
    assert second["status"] == "source_unavailable"
    assert second["places"] == {}
    assert calls == 1

    hotel = {"location": {
        "coordinates": coordinates,
        "beach_status": "not_found",
        "nearby_geo_status": second["status"],
    }}
    assert _geo_enrichment_cache_is_terminal(hotel)
