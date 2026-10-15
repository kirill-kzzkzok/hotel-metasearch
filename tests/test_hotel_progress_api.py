import asyncio
import json
import sqlite3

import httpx
from fastapi import FastAPI

from aviasales_collector.hotels import api as hotel_api
from aviasales_collector.hotels.api import build_router
from aviasales_collector.hotels.models import SourceResult
from aviasales_collector.hotels.persistence import (
    _finish_search_session_if_ready,
    _persist_search_source_result,
)
from aviasales_collector.hotels.schema import init_hotel_store


def test_search_progress_revision_ignores_scoped_deltas_and_tracks_trip_discovery(tmp_path):
    database = tmp_path / "hotels.sqlite3"
    init_hotel_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("search-1", json.dumps({"city_name": "Dubai"}), "2026-10-04T00:00:00Z", "running"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            ("search-1", "sravniotel", "running", "2026-10-04T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            ("search-1", "trip_com", "running", "2026-10-04T00:00:00Z"),
        )

    app = FastAPI()
    app.include_router(build_router(database))

    async def verify():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            first = await client.get("/api/hotels/search/search-1/progress")
            assert first.status_code == 200
            assert "hotels" not in first.json()
            assert first.json()["sources"][0]["status"] == "running"

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE hotel_source_run SET status='partial',pages=2,hotel_count=3,offer_count=4,observed_at=? "
                    "WHERE search_id='search-1' AND source='sravniotel'",
                    ("2026-10-04T00:00:02Z",),
                )
                connection.execute(
                    "UPDATE hotel_search_session SET total_hotels=4,next_page_token='cursor-2' "
                    "WHERE id='search-1'"
                )
                connection.execute(
                    """INSERT INTO hotel_room_rate_fetch
                       (search_id,source,source_hotel_id,check_in,check_out,adults,children_count,status,observed_at)
                       VALUES ('search-1','trip_com','101','2026-10-20','2026-10-25',2,0,'ok',?)""",
                    ("2026-10-04T00:00:02Z",),
                )
                connection.execute(
                    """INSERT INTO hotel_coordinate_observation
                       (search_id,source,source_hotel_id,latitude,longitude,coordinate_source,retrieved_at)
                       VALUES ('search-1','sravniotel','201',25.2,55.3,'provider','2026-10-04T00:00:02Z')"""
                )
            second = await client.get("/api/hotels/search/search-1/progress")
            assert second.json()["progress_revision"] == first.json()["progress_revision"]
            assert second.json()["sources"][0]["status"] == "partial"

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE hotel_source_run SET status='ok',hotel_count=1,offer_count=2,observed_at=? "
                    "WHERE search_id='search-1' AND source='trip_com'",
                    ("2026-10-04T00:00:03Z",),
                )
            third = await client.get("/api/hotels/search/search-1/progress")
            assert third.json()["progress_revision"] != second.json()["progress_revision"]
            assert (await client.get("/api/hotels/search/missing/progress")).status_code == 404

    asyncio.run(verify())


def test_room_rate_results_reads_only_requested_trip_hotels(tmp_path):
    database = tmp_path / "hotels.sqlite3"
    init_hotel_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("search-2", json.dumps({"city_name": "Dubai", "country": "UAE"}),
             "2026-10-04T00:00:00Z", "running"),
        )
        for source, hotel_id, name in (
            ("trip_com", "101", "Trip Hotel"), ("sravniotel", "201", "Sravni Hotel"),
        ):
            connection.execute(
                """INSERT INTO hotel_observation
                   (search_id,source,source_hotel_id,name,images_json,nearby_places_json,raw_json,observed_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("search-2", source, hotel_id, name, "[]", "[]", "{}", "2026-10-04T00:00:00Z"),
            )

    app = FastAPI()
    app.include_router(build_router(database))

    async def verify():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(
                "/api/hotels/search/search-2/room-rates/results", params={"hotel_ids": "101"},
            )
            assert response.status_code == 200
            assert [hotel["source_hotel_id"] for hotel in response.json()["hotels"]] == ["101"]
            assert (await client.get(
                "/api/hotels/search/search-2/room-rates/results", params={"hotel_ids": "x"},
            )).status_code == 422

    asyncio.run(verify())


def test_trip_empty_inventory_partial_survives_persistence_progress_and_search_api(tmp_path):
    database = tmp_path / "trip-partial.sqlite3"
    init_hotel_store(database)
    query = {
        "city_name": "Dubai", "country": "UAE", "city_slug": "dubai", "country_slug": "uae",
        "check_in": "2026-10-20", "check_out": "2026-10-25", "adults": 2,
        "children_count": 0, "rooms": 1, "room_mode": "all",
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("partial-search", json.dumps(query), "2026-10-10T00:00:00Z", "running"),
        )
        connection.executemany(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            [
                ("partial-search", "trip_com", "running", "2026-10-10T00:00:00Z"),
                ("partial-search", "sravniotel", "no_results", "2026-10-10T00:00:00Z"),
            ],
        )

    trip_source = type("TripSource", (), {"source": "trip_com"})()
    _persist_search_source_result(
        database, "partial-search", trip_source, query,
        SourceResult(
            source="trip_com", status="partial", http_status=200, reported_total=5358,
            collection_complete=False, collection_reason="inventory_response_empty",
            raw_page={
                "sourceReportedTotal": 5358,
                "collectionComplete": False,
                "collectionReason": "inventory_response_empty",
                "collectionAudit": {
                    "source_reported_total": 5358,
                    "collection_complete": False,
                    "collection_reason": "inventory_response_empty",
                    "empty_inventory_shell": True,
                },
                "nextPageToken": None,
                "hotels": [{
                    "id": "101", "name": "Visible shell card", "rating": 8.5,
                    "reviews_count": 20,
                    "offers": [{"provider": "Trip.com", "price": {"value": 10000, "currency": "AED"}}],
                }],
            },
        ),
    )
    assert _finish_search_session_if_ready(database, "partial-search") is True

    app = FastAPI()
    app.include_router(build_router(database))

    async def verify():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            progress_response = await client.get("/api/hotels/search/partial-search/progress")
            assert progress_response.status_code == 200
            progress = progress_response.json()
            assert progress["status"] == "partial"
            trip_progress = next(source for source in progress["sources"] if source["source"] == "trip_com")
            assert trip_progress["status"] == "partial"
            assert trip_progress["reported_total"] == 5358
            assert trip_progress["http_status"] == 200
            assert trip_progress["collection_complete"] is False
            assert trip_progress["collection_reason"] == "inventory_response_empty"

            result_response = await client.get("/api/hotels/search/partial-search")
            assert result_response.status_code == 200
            result = result_response.json()
            assert result["status"] == "partial"
            trip_result = next(source for source in result["sources"] if source["source"] == "trip_com")
            assert trip_result["status"] == "partial"
            assert trip_result["reported_total"] == 5358
            assert trip_result["collection_complete"] is False
            assert trip_result["collection_reason"] == "inventory_response_empty"
            assert trip_result["collection_audit"]["empty_inventory_shell"] is True
            assert result["search_audit"]["collection_complete_by_source"]["trip_com"] is False
            assert result["search_audit"]["collection_reason_by_source"]["trip_com"] == (
                "inventory_response_empty"
            )
            assert len(result["hotels"]) == 1

    asyncio.run(verify())


def test_search_session_waits_for_sravni_cursor_and_finishes_with_terminal_collection_state(tmp_path):
    database = tmp_path / "session-cursor-lifecycle.sqlite3"
    init_hotel_store(database)
    now = "2026-10-12T00:00:00Z"

    def create_search(search_id, *, cursor, sravni_status):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO hotel_search_session(id,query_json,started_at,status,next_page_token) "
                "VALUES (?,?,?,?,?)",
                (search_id, json.dumps({"city_name": "Dubai"}), now, "running", cursor),
            )
            connection.executemany(
                "INSERT INTO hotel_source_run(search_id,source,status,hotel_count,observed_at) "
                "VALUES (?,?,?,?,?)",
                [
                    (search_id, "sravniotel", sravni_status, 1, now),
                    (search_id, "trip_com", "ok", 1, now),
                ],
            )

    create_search("cursor-search", cursor="cursor-2", sravni_status="ok")
    assert _finish_search_session_if_ready(database, "cursor-search") is False
    with sqlite3.connect(database) as connection:
        status, finished_at = connection.execute(
            "SELECT status,finished_at FROM hotel_search_session WHERE id='cursor-search'",
        ).fetchone()
    assert status == "running"
    assert finished_at is None

    # Natural cursor exhaustion makes the successful source results terminal.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE hotel_search_session SET next_page_token=NULL WHERE id='cursor-search'",
        )
    assert _finish_search_session_if_ready(database, "cursor-search") is True
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM hotel_search_session WHERE id='cursor-search'",
        ).fetchone()[0]
    assert status == "success"

    # An explicit bounded application stop is terminal, but remains partial.
    create_search("limited-search", cursor=None, sravni_status="partial")
    assert _finish_search_session_if_ready(database, "limited-search") is True
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM hotel_search_session WHERE id='limited-search'",
        ).fetchone()[0]
    assert status == "partial"


def test_geo_stream_selects_hotels_by_stable_identity_after_reordering(tmp_path, monkeypatch):
    database = tmp_path / "geo-identity.sqlite3"
    init_hotel_store(database)
    hotels = [
        {"canonical_hotel_id": "canonical-1", "source": "sravniotel", "source_hotel_id": "1",
         "source_records": [{"source": "sravniotel", "source_hotel_id": "1"}], "name": "Hotel 1",
         "location": {}},
        {"canonical_hotel_id": "canonical-2", "source": "sravniotel", "source_hotel_id": "2",
         "source_records": [{"source": "sravniotel", "source_hotel_id": "2"}], "name": "Hotel 2",
         "location": {}},
    ]
    base_result = {
        "search_id": "geo-search", "query": {"city_name": "Dubai", "country": "UAE",
            "city_slug": "dubai", "country_slug": "uae", "room_mode": "all",
            "check_in": "2026-10-20", "check_out": "2026-10-25"},
        "hotels": hotels,
    }
    monkeypatch.setattr(hotel_api, "_read_search", lambda *_args, **_kwargs: {
        **base_result, "hotels": list(base_result["hotels"]), "query": dict(base_result["query"]),
    })

    def reorder(_path, result):
        result["hotels"] = list(reversed(result["hotels"]))
        return result

    async def enrich(_path, result, *, limit=None, offset=0):
        for hotel in result["hotels"][offset:offset + limit]:
            hotel["location"] = {"coordinate_status": "PROVIDER_DETAIL",
                                 "beach_status": "available", "nearby_geo_status": "ok"}

    monkeypatch.setattr(hotel_api, "_finalize_search", reorder)
    monkeypatch.setattr(hotel_api, "_enrich_search_coordinates", enrich)
    app = FastAPI()
    app.include_router(build_router(database))

    async def verify():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.post(
                "/api/hotels/search/geo-search/coordinates",
                json={"room_mode": "all", "stream": True,
                      "hotel_keys": ["sravniotel:1"], "batch_size": 10},
            )
            assert response.status_code == 200
            batches = [json.loads(line) for line in response.text.splitlines() if line.strip()]
            returned = [hotel for batch in batches for hotel in batch["hotels"]]
            # 2026-10-15: the stream is two-phase (fast batch + geo update),
            # so a hotel can legitimately appear twice; uniqueness is over
            # identities, not over stream lines.
            assert sorted({hotel["source_hotel_id"] for hotel in returned}) == ["1"]
            assert len(batches) >= 1
            assert batches[-1]["complete"] is True
            assert batches[-1]["unresolved_hotel_keys"] == []
            assert (await client.post(
                "/api/hotels/search/geo-search/coordinates",
                json={"room_mode": "all", "stream": True,
                      "hotel_keys": ["invalid-key"], "batch_size": 10},
            )).status_code == 422

    asyncio.run(verify())
