import json
import logging
import re
import sqlite3
import pytest

from aviasales_collector.hotels.read_model import _read_search
from aviasales_collector.hotels.schema import init_hotel_store
from aviasales_collector.hotels.pipeline import _finalize_search


def test_read_model_batches_per_hotel_reads_without_losing_card_data(tmp_path, caplog, monkeypatch):
    database = tmp_path / "read-model-batched.sqlite3"
    init_hotel_store(database)
    search_id = "batched-read-search"
    observed_at = "2026-10-08T12:00:00+00:00"
    query = {
        "city_name": "Dubai", "country": "UAE", "check_in": "2026-10-20",
        "check_out": "2026-10-25", "adults": 2, "children_count": 0,
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            (search_id, json.dumps(query), observed_at, "running"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            (search_id, "trip_com", "ok", observed_at),
        )
        for index in range(4):
            hotel_id = str(100 + index)
            source_image = f"https://example.test/source-{hotel_id}.jpg"
            property_details = ({
                "address": "Enriched detail address",
                "coordinates": {"lat": 40.7, "lng": -74.0},
                "stars": 5,
                "images": [f"https://example.test/enriched-{hotel_id}.jpg"],
            } if index == 0 else None)
            connection.execute(
                """INSERT INTO hotel_observation
                   (search_id,source,source_hotel_id,name,address,stars,rating,reviews_count,
                    images_json,nearby_places_json,raw_json,observed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, "trip_com", hotel_id, f"Hotel {index}", "Dubai", 4, 8.5, 20,
                 json.dumps([{"url": source_image}]), "[]",
                 json.dumps({"coordinates": {"lat": 25.2 + index / 100, "lng": 55.3},
                             "images": [{"url": source_image}]}), observed_at),
            )
            connection.execute(
                """INSERT INTO hotel_coordinate_observation
                   (search_id,source,source_hotel_id,latitude,longitude,coordinate_source,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (search_id, "trip_com", hotel_id, 25.2 + index / 100, 55.3,
                 "trip_com", observed_at),
            )
            connection.execute(
                """INSERT INTO hotel_coordinate_resolution
                   (search_id,hotel_key,status,reason,coordinate_source,coordinate_method,
                    coordinate_confidence,latitude,longitude,attempts_json,retrieved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, f"trip_com:{hotel_id}", "PROVIDER_DETAIL", "verified",
                 "trip_com", "provider_coordinates", "high", 25.2 + index / 100, 55.3,
                 "[]", observed_at),
            )
            connection.execute(
                """INSERT INTO hotel_offer_observation
                   (search_id,source,source_hotel_id,provider,amount,currency,labels_json,raw_json,observed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (search_id, "trip_com", hotel_id, "provider", "100", "AED", "[]",
                 json.dumps({"meal": "breakfast"}), observed_at),
            )
            connection.execute(
                """INSERT INTO hotel_provider_metadata
                   (search_id,source,source_hotel_id,provider,metadata_json,retrieved_at)
                   VALUES (?,?,?,?,?,?)""",
                (search_id, "trip_com", hotel_id, "provider",
                 json.dumps({"review_url": f"https://example.test/{hotel_id}"}), observed_at),
            )
            connection.execute(
                """INSERT INTO hotel_room_rate_fetch
                   (search_id,source,source_hotel_id,check_in,check_out,adults,children_count,
                    status,http_status,rates_seen,observed_at,property_details_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, "trip_com", hotel_id, query["check_in"], query["check_out"], 2,
                 0, "ok", 200, 1, observed_at,
                 json.dumps(property_details) if property_details else None),
            )
            connection.execute(
                """INSERT INTO hotel_room_rate_observation
                   (search_id,source,source_hotel_id,provider_rate_id,check_in,check_out,
                    adults,children_count,room_group_index,raw_json,observed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, "trip_com", hotel_id, f"rate-{hotel_id}", query["check_in"],
                 query["check_out"], 2, 0, 0, json.dumps({"amount": 123}), observed_at),
            )
        connection.commit()

    caplog.set_level(logging.INFO, logger="aviasales_collector.hotels.runtime")
    result = _read_search(database, search_id)

    assert len(result["hotels"]) == 4
    for hotel in result["hotels"]:
        assert hotel["offers"][0]["meal"] == "breakfast"
        assert hotel["room_rates"] == [{"amount": 123}]
        assert hotel["provider_details"][0]["review_url"].endswith(hotel["source_hotel_id"])
        assert hotel["coordinates"] == {"lat": hotel["coordinates"]["lat"], "lng": 55.3}
        assert hotel["coordinate_status"] == "PROVIDER_DETAIL"
        assert hotel["_match_evidence"]["address"] == "Dubai"
        assert hotel["_match_evidence"]["stars"] == 4
        assert hotel["_match_evidence"]["coordinates"] == hotel["coordinates"]
        assert hotel["_match_evidence"]["images"] == [{"url": f"https://example.test/source-{hotel['source_hotel_id']}.jpg"}]

    full_read_hotels = list(result["hotels"])
    full_match_evidence = {
        hotel["source_hotel_id"]: hotel["_match_evidence"] for hotel in full_read_hotels
    }
    full_identity_ids = {
        hotel["source_hotel_id"]: hotel["canonical_hotel_id"] for hotel in full_read_hotels
    }
    enriched = next(item for item in result["hotels"] if item["source_hotel_id"] == "100")
    assert enriched["property_details"]["address"] == "Enriched detail address"
    assert enriched["property_details"]["coordinates"] == {"lat": 40.7, "lng": -74.0}
    assert {image["url"] for image in enriched["images"]} == {
        "https://example.test/source-100.jpg", "https://example.test/enriched-100.jpg",
    }
    finalized = _finalize_search(database, result)
    assert all("_match_evidence" not in hotel for hotel in finalized["hotels"])

    profile = re.search(r"event=read_model_built .*?db_query_count=(\d+)", caplog.text)
    assert profile
    assert int(profile.group(1)) <= 25

    from aviasales_collector.hotels import read_model

    def unexpected_full_read(*_args, **_kwargs):
        raise AssertionError("matching-only read must skip public-card enrichment lookups")

    monkeypatch.setattr(read_model, "_read_city_geo_profile", unexpected_full_read)
    monkeypatch.setattr(read_model, "_fetch_lookup_rows", unexpected_full_read)
    matching = _read_search(database, search_id, matching_only=True)

    assert len(matching["hotels"]) == len(full_read_hotels)
    full_by_id = {hotel["source_hotel_id"]: hotel for hotel in full_read_hotels}
    for hotel in matching["hotels"]:
        full = full_by_id[hotel["source_hotel_id"]]
        assert hotel["_match_evidence"] == full_match_evidence[hotel["source_hotel_id"]]
        for field in ("source", "source_hotel_id", "name", "phone", "website", "city",
                      "country", "chain", "nearby_places"):
            assert hotel.get(field) == full.get(field)
        assert hotel["canonical_hotel_id"] == full_identity_ids[hotel["source_hotel_id"]]
        assert "offers" not in hotel
        assert "room_rates" not in hotel
        assert "provider_details" not in hotel

    matching_profile = re.findall(
        r"event=read_model_built .*?matching_only=True.*?db_query_count=(\d+)", caplog.text,
    )
    assert matching_profile and int(matching_profile[-1]) <= 3


@pytest.mark.parametrize(
    ("cache_version", "cache_source", "cache_method", "expected_distance"),
    [
        ("osm-beach-geometry-v11", "openstreetmap_photon_geometry", "nearest_osm_beach_geometry", None),
        ("osm-beach-geometry-v14", "openstreetmap_photon_geometry", "nearest_osm_beach_geometry", None),
        ("osm-beach-geometry-v14", "openstreetmap_overpass", "nearest_osm_beach_geometry", 1746.3795),
        ("osm-beach-geometry-v15", "openstreetmap_overpass", "nearest_osm_beach_point", None),
    ],
)
def test_read_model_uses_verified_beach_geometry_and_rejects_point_cache(
    tmp_path, cache_version, cache_source, cache_method, expected_distance,
):
    from aviasales_collector.hotels.geo.core import _hotel_beach_cache_key

    database = tmp_path / "legacy-photon-beach.sqlite3"
    init_hotel_store(database)
    search_id = "legacy-photon-search"
    observed_at = "2026-10-11T12:00:00+00:00"
    query = {
        "city_name": "Da Nang", "country": "Vietnam", "city_slug": "da-nang",
        "country_slug": "vietnam", "check_in": "2026-10-20", "check_out": "2026-10-25",
        "adults": 2, "children_count": 0, "room_mode": "all",
    }
    coordinates = {"lat": 16.037975311279297, "lng": 108.25018310546875}
    legacy_key = _hotel_beach_cache_key(coordinates, "Da Nang", "Vietnam", cache_version)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            (search_id, json.dumps(query), observed_at, "running"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            (search_id, "sravniotel", "ok", observed_at),
        )
        connection.execute(
            """INSERT INTO hotel_observation
               (search_id,source,source_hotel_id,name,address,stars,rating,reviews_count,
                images_json,nearby_places_json,raw_json,observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (search_id, "sravniotel", "9942863", "V-Hotel Da Nang Beach", "Da Nang", 4, 9, 24,
             "[]", "[]", "{}", observed_at),
        )
        connection.execute(
            """INSERT INTO hotel_coordinate_observation
               (search_id,source,source_hotel_id,latitude,longitude,coordinate_source,retrieved_at,
                coordinate_method,coordinate_confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (search_id, "sravniotel", "9942863", coordinates["lat"], coordinates["lng"],
             "tutu.ru_detail", observed_at, "hotel_detail_structured_data", "high"),
        )
        connection.execute(
            """INSERT INTO hotel_offer_observation
               (search_id,source,source_hotel_id,provider,amount,currency,labels_json,raw_json,observed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (search_id, "sravniotel", "9942863", "provider", "100", "RUB", "[]", "{}", observed_at),
        )
        connection.execute(
            """INSERT INTO hotel_beach_cache
               (cache_key,distance_meters,beach_name,source,retrieved_at,status,beach_osm_type,
                beach_osm_id,beach_method,beach_confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (legacy_key, 1746.3795, "My An Beach", cache_source, observed_at,
             "ok", "N" if cache_method == "nearest_osm_beach_point" else "W",
             "4347089780" if cache_method == "nearest_osm_beach_point" else "1204900824",
             cache_method, "medium" if cache_method == "nearest_osm_beach_point" else "high"),
        )
        connection.commit()

    result = _read_search(database, search_id)
    assert result["hotels"][0]["cached_beach_distance_meters"] == expected_distance

    finalized = _finalize_search(database, result)
    location = finalized["hotels"][0]["location"]
    if expected_distance is None:
        assert location["distance_to_beach_km"] is None
        assert location["beach_status"] == "unknown"
    else:
        assert location["distance_to_beach_km"] == pytest.approx(expected_distance / 1000)
        assert location["beach_status"] == "available"
