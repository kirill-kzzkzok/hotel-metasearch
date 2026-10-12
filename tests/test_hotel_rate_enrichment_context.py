import asyncio
import json
import sqlite3

import aviasales_collector.hotels.rate_enrichment as rate_enrichment


def _create_rate_test_database(path, *, source_city_id="1356"):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE hotel_raw_page (id INTEGER PRIMARY KEY, search_id TEXT, source TEXT, "
        "page_index INTEGER, raw_json TEXT)"
    )
    connection.execute(
        "CREATE TABLE hotel_room_rate_fetch (id INTEGER PRIMARY KEY, source_hotel_id TEXT, status TEXT, http_status INTEGER, "
        "latency_ms INTEGER, rates_seen INTEGER, operation TEXT, error_type TEXT, error_message TEXT, "
        "attempt INTEGER, retry_count INTEGER, search_id TEXT, source TEXT, check_in TEXT, check_out TEXT, "
        "adults INTEGER, children_count INTEGER, observed_at TEXT)"
    )
    if source_city_id is not None:
        connection.execute(
            "INSERT INTO hotel_raw_page(search_id,source,page_index,raw_json) VALUES(?,?,?,?)",
            ("search-1", "trip_com", 1, json.dumps({"sourceCityId": source_city_id})),
        )
    connection.commit()
    connection.close()


def test_trip_rates_use_trip_city_id_when_sravni_geo_id_is_missing(monkeypatch, tmp_path):
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path)
    search = {
        "search_id": "search-1",
        "query": {"geo_id": None, "check_in": "2026-11-14", "check_out": "2026-11-17",
                  "adults": 2, "children_count": 0},
        "hotels": [{"name": "Trip hotel", "source_records": [
            {"source": "trip_com", "source_hotel_id": "12345"},
        ]}],
    }
    monkeypatch.setattr(rate_enrichment, "_read_search", lambda *_args, **_kwargs: search)
    monkeypatch.setattr(rate_enrichment, "shutdown_tripcom_browser", lambda: None)
    captured = {}

    async def collect(hotels, _profile, *, concurrency, on_result):
        captured["hotels"] = hotels
        captured["concurrency"] = concurrency
        return [{"source_hotel_id": "12345", "status": "no_rates", "rate_status": 200,
                 "latency_ms": 10, "inventory": {"rates": []}}]

    monkeypatch.setattr(rate_enrichment, "collect_tripcom_room_rates", collect)
    result = asyncio.run(rate_enrichment.enrich_tripcom_room_rates(
        database_path, "search-1", search, {"concurrency": 1}, None,
    ))

    assert captured["hotels"] == [{
        "source_hotel_id": "12345", "city_id": "1356", "check_in": "2026-11-14",
        "check_out": "2026-11-17", "adults": 2, "children_count": 0,
    }]
    assert captured["concurrency"] == 1
    assert result["hotels"][0]["enrichment"]["status"] == "no_rates"


def test_trip_rate_city_id_falls_back_to_valid_search_geo_id_when_trip_raw_page_has_none(tmp_path):
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path, source_city_id=None)

    assert rate_enrichment._tripcom_source_city_id(database_path, "search-1", "2658853") == "2658853"
    assert rate_enrichment._tripcom_source_city_id(database_path, "search-1", None) is None


def test_trip_rate_city_id_prefers_trip_namespace_over_search_geo_id(tmp_path):
    database_path = tmp_path / "rates.sqlite3"
    _create_rate_test_database(database_path, source_city_id="1356")

    assert rate_enrichment._tripcom_source_city_id(database_path, "search-1", "2658853") == "1356"
