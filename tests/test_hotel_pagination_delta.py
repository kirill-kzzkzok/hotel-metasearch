from aviasales_collector.hotels.pagination import _select_delta_hotels


def test_pagination_delta_keeps_affected_canonical_group_only():
    hotels = [
        {"canonical_hotel_id": "merged", "source_records": [
            {"source": "trip_com", "source_hotel_id": "101"},
            {"source": "sravniotel", "source_hotel_id": "201"},
        ]},
        {"canonical_hotel_id": "untouched", "source_records": [
            {"source": "sravniotel", "source_hotel_id": "202"},
        ]},
    ]

    selected = _select_delta_hotels(hotels, {"sravniotel:201", "trip_com:101"})

    assert [hotel["canonical_hotel_id"] for hotel in selected] == ["merged"]


def test_pagination_delta_handles_unmerged_source_card():
    hotels = [{"source": "sravniotel", "source_hotel_id": "301", "source_records": []}]

    assert _select_delta_hotels(hotels, {"sravniotel:301"}) == hotels


def test_sravni_page_sync_persistence_does_not_block_async_event_loop(tmp_path, monkeypatch):
    import asyncio
    import json
    import sqlite3
    import threading
    import time
    from datetime import datetime, timezone

    from aviasales_collector.hotels import pagination
    from aviasales_collector.hotels.models import SourceResult
    from aviasales_collector.hotels.schema import init_hotel_store

    database = tmp_path / "pagination.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    search_id = "pagination-search"
    query = {"city_name": "Dubai", "check_in": "2026-10-20", "check_out": "2026-10-25",
             "adults": 2, "children_count": 0, "room_mode": "all"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status,next_page_token) "
            "VALUES (?,?,?,?,?)",
            (search_id, json.dumps(query), now, "running", "cursor-1"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            (search_id, "sravniotel", "running", now),
        )

    page_result = SourceResult(
        "sravniotel", "ok", hotels=({"source_hotel_id": "201"},),
        raw_page={"hotels": [{"id": "201", "name": "Sample Hotel"}],
                  "totalHotels": 1, "nextPageToken": "cursor-2"},
        total_hotels=1, next_page_token="cursor-2", observed_at=now,
    )

    async def search_page(*_args, **_kwargs):
        return page_result

    loop_thread = threading.get_ident()
    worker_threads = []

    def slow_page_processing(*_args, **_kwargs):
        time.sleep(0.12)
        worker_threads.append(threading.get_ident())
        return {"page_processed": True}

    monkeypatch.setattr(pagination, "_search_with_cache", search_page)
    monkeypatch.setattr(pagination, "_persist_and_finalize_sravni_page", slow_page_processing)

    async def exercise():
        ticks = 0
        done = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.01)
                ticks += 1

        heartbeat_task = asyncio.create_task(heartbeat())
        result = await pagination.load_more_sravni_page(
            database, search_id, {"room_mode": "all"}, delta=True,
        )
        done.set()
        await heartbeat_task
        return result, ticks

    result, ticks = asyncio.run(exercise())

    assert result == {"page_processed": True}
    assert worker_threads and worker_threads[0] != loop_thread
    assert ticks >= 5


def test_empty_nonterminal_sravni_delta_skips_detail_read(tmp_path, monkeypatch):
    import asyncio
    import json
    import sqlite3
    from datetime import datetime, timezone

    from aviasales_collector.hotels import pagination
    from aviasales_collector.hotels.models import SourceResult
    from aviasales_collector.hotels.schema import init_hotel_store

    database = tmp_path / "empty-delta.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    search_id = "empty-delta-search"
    query = {"city_name": "Dubai", "check_in": "2026-10-20", "check_out": "2026-10-25",
             "adults": 2, "children_count": 0, "room_mode": "all"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status,next_page_token) "
            "VALUES (?,?,?,?,?)", (search_id, json.dumps(query), now, "running", "cursor-1"),
        )
        connection.execute(
            "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
            (search_id, "sravniotel", "running", now),
        )

    page_result = SourceResult(
        source="sravniotel", status="ok", hotels=(),
        raw_page={"hotels": [], "totalHotels": 20, "nextPageToken": "cursor-2"},
        total_hotels=20, next_page_token="cursor-2", observed_at=now,
    )

    async def search_page(*_args, **_kwargs):
        return page_result

    original_read = pagination._read_search
    read_calls = []

    def track_read(*args, **kwargs):
        read_calls.append(kwargs.copy())
        return original_read(*args, **kwargs)

    def unexpected_finalize(*_args, **_kwargs):
        raise AssertionError("empty nonterminal delta must not build a card read model")

    monkeypatch.setattr(pagination, "_search_with_cache", search_page)
    monkeypatch.setattr(pagination, "_read_search", track_read)
    monkeypatch.setattr(pagination, "_finalize_search", unexpected_finalize)

    result = asyncio.run(pagination.load_more_sravni_page(
        database, search_id, {"room_mode": "all"}, delta=True,
    ))

    assert read_calls == [{"matching_only": True}]
    assert result["hotels"] == []
    assert result["has_more"] is True
    assert result["delta_source_identity_keys"] == []
    assert result["search_audit_scope"] == "delta"


def test_sravni_delta_matches_globally_but_finalizes_only_affected_group(tmp_path, monkeypatch):
    import asyncio
    import json
    import sqlite3
    from datetime import datetime, timezone

    from aviasales_collector.hotels import pagination
    from aviasales_collector.hotels.models import SourceResult
    from aviasales_collector.hotels.persistence import _persist_page, persist_tripcom_room_rates
    from aviasales_collector.hotels.read_model import _read_search
    from aviasales_collector.hotels.schema import init_hotel_store

    database = tmp_path / "delta-match.sqlite3"
    init_hotel_store(database)
    now = datetime.now(timezone.utc).isoformat()
    search_id = "delta-match-search"
    query = {"city_name": "Dubai", "country": "United Arab Emirates", "check_in": "2026-10-20",
             "check_out": "2026-10-25", "adults": 2, "children_count": 0, "room_mode": "all"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status,next_page_token) "
            "VALUES (?,?,?,?,?)", (search_id, json.dumps(query), now, "running", "cursor-1"),
        )
        for source in ("trip_com", "sravniotel"):
            connection.execute(
                "INSERT INTO hotel_source_run(search_id,source,status,observed_at) VALUES (?,?,?,?)",
                (search_id, source, "ok" if source == "trip_com" else "running", now),
            )

    shared_trip = {
        "id": "trip-1", "name": "Palm Test Hotel", "address": "1 Main Street", "stars": 4,
        "coordinates": {"lat": 25.1, "lng": 55.2},
        "offers": [{"provider": "Trip", "price": {"value": 1200000, "currency": "USD"},
                    "room_type": "Deluxe room", "meal": "Breakfast included"}],
    }
    unrelated_trip = {
        "id": "trip-2", "name": "Desert Test Hotel", "address": "99 Other Street", "stars": 3,
        "coordinates": {"lat": 25.3, "lng": 55.4},
        "offers": [{"provider": "Trip", "price": {"value": 800000, "currency": "USD"}}],
    }
    _persist_page(
        database, search_id, [shared_trip, unrelated_trip],
        {"hotels": [shared_trip, unrelated_trip]}, None, "trip_com", query, now,
    )
    persist_tripcom_room_rates(database, search_id, {
        "source_hotel_id": "trip-1", "check_in": query["check_in"], "check_out": query["check_out"],
        "adults": 2, "children_count": 0,
        "rates": [{"provider_rate_id": "rate-1", "room_group_index": 0,
                   "room_type": "Deluxe room", "meal_plan": "breakfast", "cancellation": "free",
                   "observed_at": now}],
    }, status="ok", observed_at=now)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO hotel_coordinate_resolution
            (search_id,hotel_key,status,reason,coordinate_source,coordinate_method,
             coordinate_confidence,latitude,longitude,attempts_json,retrieved_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (search_id, "trip_com:trip-1", "success", None, "trip_com", "provider_coordinates",
             "high", 25.1, 55.2, "[]", now),
        )
        connection.commit()

    sravni_hotel = {
        "id": "sravni-1", "name": "Palm Test Hotel", "address": "1 Main Street", "stars": 4,
        "coordinates": {"lat": 25.1, "lng": 55.2},
        "offers": [{"provider": "Sravni", "price": {"value": 1500000, "currency": "USD"},
                    "room_type": "Deluxe room", "meal": "Breakfast included"}],
    }
    page_result = SourceResult(
        source="sravniotel", status="ok", hotels=({"source_hotel_id": "sravni-1"},),
        raw_page={"hotels": [sravni_hotel], "totalHotels": 10, "nextPageToken": "cursor-2"},
        total_hotels=10, next_page_token="cursor-2", observed_at=now,
    )
    terminal_page_result = SourceResult(
        source="sravniotel", status="ok", hotels=(),
        # Sravni uses an empty string for natural cursor exhaustion in some
        # responses; it must be normalized like JSON null in persisted state.
        raw_page={"hotels": [], "totalHotels": 10, "nextPageToken": ""},
        total_hotels=10, next_page_token=None, observed_at=now,
    )
    page_results = [page_result, terminal_page_result]

    async def search_page(*_args, **_kwargs):
        return page_results.pop(0)

    original_read = pagination._read_search
    read_calls = []

    def track_read(*args, **kwargs):
        read_calls.append(kwargs.copy())
        return original_read(*args, **kwargs)

    original_finalize = pagination._finalize_search
    finalize_calls = []

    def track_finalize(*args, **kwargs):
        finalize_calls.append((len(args[1]["hotels"]), kwargs.copy()))
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(pagination, "_search_with_cache", search_page)
    monkeypatch.setattr(pagination, "_read_search", track_read)
    monkeypatch.setattr(pagination, "_finalize_search", track_finalize)
    result = asyncio.run(pagination.load_more_sravni_page(
        database, search_id, {"room_mode": "all"}, delta=True,
    ))

    assert [call.get("matching_only", False) for call in read_calls] == [True, False]
    assert read_calls[1]["source_identity_keys"] == {
        ("trip_com", "trip-1"), ("sravniotel", "sravni-1"),
    }
    assert len(finalize_calls) == 1
    assert finalize_calls[0][1].get("match_state") is not None
    assert len(result["hotels"]) == 1
    merged = result["hotels"][0]
    assert set(merged["sources"]) == {"trip_com", "sravniotel"}
    assert {record["source_hotel_id"] for record in merged["source_records"]} == {"trip-1", "sravni-1"}
    assert len(merged["offers"]) == 2
    assert [rate["provider_rate_id"] for rate in merged["room_rates"]] == ["rate-1"]
    trip_record = next(record for record in merged["source_records"] if record["source"] == "trip_com")
    assert trip_record["coordinate_status"] == "success"
    assert set(result["delta_source_identity_keys"]) == {"trip_com:trip-1", "sravniotel:sravni-1"}

    # The natural final read must preserve the same canonical identity and all
    # persisted enrichment data when it returns the complete search snapshot.
    full = original_finalize(database, original_read(database, search_id))
    full_merged = next(hotel for hotel in full["hotels"] if hotel.get("canonical_hotel_id") == merged["canonical_hotel_id"])
    assert full_merged["canonical_hotel_id"] == merged["canonical_hotel_id"]
    assert len(full_merged["offers"]) == 2
    assert [rate["provider_rate_id"] for rate in full_merged["room_rates"]] == ["rate-1"]

    final_result = asyncio.run(pagination.load_more_sravni_page(
        database, search_id, {"room_mode": "all"}, delta=True,
    ))
    assert "delta_source_identity_keys" not in final_result
    assert final_result["has_more"] is False
    assert len(final_result["hotels"]) == 2
    assert any(hotel.get("canonical_hotel_id") == merged["canonical_hotel_id"]
               for hotel in final_result["hotels"])
    assert len(read_calls) == 3 and read_calls[-1] == {}
    assert len(finalize_calls) == 2 and finalize_calls[-1][1] == {}
    with sqlite3.connect(database) as connection:
        session_status, next_page_token = connection.execute(
            "SELECT status,next_page_token FROM hotel_search_session WHERE id=?", (search_id,),
        ).fetchone()
    assert session_status == "success"
    assert next_page_token is None
