from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.geo import enrichment


def test_coordinate_enrichment_waits_for_worker_result(tmp_path, monkeypatch):
    path = tmp_path / "coordinate-worker.sqlite3"
    init_hotel_store(path)
    worker_started = threading.Event()
    release_worker = threading.Event()

    def slow_resolver(*_args):
        worker_started.set()
        if not release_worker.wait(timeout=3):
            raise TimeoutError("test did not release coordinate worker")
        return {
            "coordinates": {"lat": 48.8566, "lng": 2.3522},
            "status": "PROVIDER_COORDINATES",
            "reason": "test_resolved",
            "coordinate_source": "trip_com",
            "coordinate_method": "provider_coordinates",
            "coordinate_confidence": "high",
            "attempts": [],
        }

    monkeypatch.setattr(enrichment, "_resolve_hotel_coordinates", slow_resolver)
    monkeypatch.setattr(enrichment, "_resolve_city_reference", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_resolve_nearest_beach", lambda *_args: {
        "beach_relevant": None, "beach_geometry_available": None,
        "status": "not_found", "public_status": "not_found",
    })
    monkeypatch.setattr(enrichment, "_osm_nearby_features", lambda *_args: {
        "status": "not_found", "source": "openstreetmap_overpass", "places": {},
    })
    result = {
        "search_id": "",
        "query": {"city_name": "Paris", "country": "France", "city_slug": "paris",
                  "country_slug": "france"},
        "hotels": [{"source": "trip_com", "source_hotel_id": "hotel-1", "name": "Slow Hotel",
                    "location": {}, "offers": []}],
    }

    async def exercise():
        task = asyncio.create_task(enrichment._enrich_search_coordinates(path, result, limit=1))
        try:
            assert await asyncio.to_thread(worker_started.wait, 2)
            assert not task.done(), "enrichment must wait for its worker result"
        finally:
            release_worker.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(exercise())

    location = result["hotels"][0]["location"]
    assert location["coordinates"] == {"lat": 48.8566, "lng": 2.3522}
    assert location["coordinate_status"] == "PROVIDER_COORDINATES"


def test_geo_enrichment_reuses_fresh_success_and_not_found_terminal_states(tmp_path, monkeypatch):
    path = tmp_path / "coordinate-cache.sqlite3"
    init_hotel_store(path)

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("fresh terminal geo result must not start another lookup")

    monkeypatch.setattr(enrichment, "_resolve_hotel_coordinates", unexpected_network)
    monkeypatch.setattr(enrichment, "_resolve_city_reference", lambda *_args: None)
    monkeypatch.setattr(enrichment, "_resolve_nearest_beach", unexpected_network)
    monkeypatch.setattr(enrichment, "_osm_nearby_features", unexpected_network)
    now = datetime.now(timezone.utc).isoformat()
    results = [
        {
            "search_id": "",
            "query": {"city_name": "Dubai", "country": "UAE", "city_slug": "dubai",
                      "country_slug": "uae"},
            "hotels": [{
                "source": "sravniotel", "source_hotel_id": "success-1", "name": "Cached Success",
                "canonical_hotel_id": "canonical-success", "location": {
                    "coordinates": {"lat": 25.2, "lng": 55.3}, "coordinate_status": "PROVIDER_DETAIL",
                }, "source_records": [{
                    "source": "sravniotel", "source_hotel_id": "success-1",
                    "cached_beach_status": "ok", "cached_geo_status": "ok",
                }], "offers": [],
            }],
        },
        {
            "search_id": "",
            "query": {"city_name": "Dubai", "country": "UAE", "city_slug": "dubai",
                      "country_slug": "uae"},
            "hotels": [{
                # Auto-matched canonical cards expose the TTL-filtered cache
                # states on location, while compact source_records omit the
                # cache implementation fields.
                "source": "canonical", "canonical_hotel_id": "canonical-merged",
                "name": "Merged Cached Hotel", "location": {
                    "coordinates": {"lat": 25.21, "lng": 55.31},
                    "coordinate_status": "PROVIDER_DETAIL", "beach_status": "available",
                    "nearby_geo_status": "ok",
                }, "source_records": [
                    {"source": "trip_com", "source_hotel_id": "merged-trip"},
                    {"source": "sravniotel", "source_hotel_id": "merged-sravni"},
                ], "offers": [],
            }],
        },
        {
            "search_id": "",
            "query": {"city_name": "Dubai", "country": "UAE", "city_slug": "dubai",
                      "country_slug": "uae"},
            "hotels": [{
                "source": "sravniotel", "source_hotel_id": "missing-1", "name": "Cached Not Found",
                "location": {"coordinate_status": "NOT_FOUND"}, "source_records": [{
                    "source": "sravniotel", "source_hotel_id": "missing-1",
                    "coordinate_resolution_retrieved_at": now,
                }], "offers": [],
            }],
        },
    ]

    async def exercise():
        for result in results:
            await enrichment._enrich_search_coordinates(path, result, limit=1)
            # A later scheduler/pagination pass can include the same canonical
            # card again. Fresh terminal cache evidence must suppress a second
            # coordinate, beach, or nearby-feature request.
            await enrichment._enrich_search_coordinates(path, result, limit=1)

    asyncio.run(exercise())

    assert results[0]["coordinate_audit"]["processed"] == 1
    assert results[0]["coordinate_audit"]["coordinates"] == 1
    assert results[1]["coordinate_audit"]["processed"] == 1
    assert results[1]["coordinate_audit"]["coordinates"] == 1
    assert results[2]["coordinate_audit"]["processed"] == 1
    assert results[2]["coordinate_audit"]["coordinates"] == 0
