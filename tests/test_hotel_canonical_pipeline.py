from __future__ import annotations

from aviasales_collector.hotel_matching import compare_hotels
from aviasales_collector.hotel_search import (
    CityReference,
    CityGeoProfile,
    _location_summary,
    classify_meal_plan,
    init_hotel_store,
    normalize_hotel,
    reprocess_saved_offer_meals,
    normalize_tripcom_rate_inventory,
    normalize_tripcom_property_details,
    persist_tripcom_room_rates,
    _read_search,
    register_city_reference,
    register_city_geo_profile,
    _resolve_nearest_beach,
    utc_now,
)


def _card(source: str, hotel_id: str, name: str, **extra):
    return {
        "source": source,
        "source_hotel_id": hotel_id,
        "name": name,
        "address": extra.pop("address", "1 Main Street"),
        "city": extra.pop("city", "Dubai"),
        "country": extra.pop("country", "United Arab Emirates"),
        **extra,
    }


def test_matching_accepts_transliteration_but_rejects_coordinate_conflict():
    same = compare_hotels(
        _card("sravniotel", "1", "Marina Bay Отель", coordinates={"lat": 25.1, "lng": 55.2}),
        _card("trip_com", "2", "Marina Bay Hotel", coordinates={"lat": 25.1002, "lng": 55.2002}),
    )
    assert same is not None
    assert same["features"]["coordinates"] > 0.1

    different = compare_hotels(
        _card("sravniotel", "1", "Marina Bay Hotel", coordinates={"lat": 25.1, "lng": 55.2}),
        _card("trip_com", "2", "Marina Bay Hotel", coordinates={"lat": 48.85, "lng": 2.35}),
    )
    assert different is None


def test_meal_normalization_keeps_room_only_and_breakfast_unknown_states():
    assert classify_meal_plan("room only") == "room_only"
    hotel = normalize_hotel({
        "id": "1", "name": "Hotel", "offers": [{
            "price": {"value": 10000, "currency": "RUB"},
            "labels": ["Breakfast available for additional charge"],
        }],
    })
    offer = hotel["offers"][0]
    assert offer["meal_plan"] == "unknown"
    assert offer["breakfast_available"] is True
    assert offer["breakfast_included"] is False

    included = normalize_hotel({
        "id": "2", "name": "Hotel", "offers": [{
            "price": {"value": 10000, "currency": "RUB"},
            "labels": ["breakfast included"],
        }],
    })["offers"][0]
    assert included["meal_plan"] == "breakfast"
    assert included["breakfast_included"] is True

    hotel_amenity_only = normalize_hotel({
        "id": "3", "name": "Hotel", "amenities": ["Breakfast buffet"],
        "offers": [{"price": {"value": 10000, "currency": "RUB"},
                    "room_type": "Twin room", "meal_plan": "unknown"}],
    })
    assert hotel_amenity_only["offers"][0]["meal_plan"] == "unknown"


def test_meal_fixtures_preserve_inclusion_and_availability_semantics():
    cases = [
        ("Breakfast included", "breakfast", True, True),
        ("Включен завтрак для 2 гостей", "breakfast", True, True),
        ("Breakfast for 2,785.53 RUB (optional)", "unknown", False, True),
        ("Завтрак за 2 785,53 ₽ (по желанию)", "unknown", False, True),
        ("Только номер", "room_only", False, None),
        ("Bed and breakfast", "breakfast", True, True),
        ("Room only", "room_only", False, None),
        ("No meals", "room_only", False, None),
        ("Half board", "half_board", True, True),
        ("Full board", "full_board", True, True),
        ("All inclusive", "all_inclusive", True, True),
        ("Breakfast available for additional charge", "unknown", False, True),
        ("Breakfast available", "unknown", None, True),
        (None, "unknown", None, None),
        ("Завтрак включён", "breakfast", True, True),
        ("Без питания", "room_only", False, None),
        ("Полупансион", "half_board", True, True),
        ("Полный пансион", "full_board", True, True),
        ("Всё включено", "all_inclusive", True, True),
    ]
    for raw, expected_plan, expected_included, expected_available in cases:
        offer = normalize_hotel({
            "id": "meal-fixture", "name": "Fixture hotel",
            "offers": [{"price": {"value": 10000, "currency": "RUB"},
                        **({"meal": raw} if raw is not None else {})}],
        }, source="trip_com")["offers"][0]
        assert offer["meal_plan"] == expected_plan, raw
        assert offer["meal_included"] is expected_included, raw
        assert offer["meal_available"] is expected_available, raw
        assert offer["meal_raw_text"] == raw, raw
        assert offer["meal_normalizer_version"] == "meal_v4"


def test_meal_provenance_stays_offer_level_and_hotel_availability_is_separate():
    hotel = normalize_hotel({
        "id": "meal-split", "name": "Hotel",
        "amenities": ["Breakfast available at hotel"],
        "offers": [{"price": {"value": 10000, "currency": "RUB"},
                    "meal": "unknown", "room_type": "Standard room"}],
    }, source="sravniotel")
    offer = hotel["offers"][0]
    assert hotel["breakfast_available"] is True
    assert hotel["breakfast_source"] == "sravniotel:hotel_amenities"
    assert offer["meal_plan"] == "unknown"
    assert offer["meal_available"] is None
    assert offer["meal_included"] is None
    assert offer["meal_source"] is None


def test_tripcom_rate_inventory_keeps_room_rate_variants_and_only_allowlisted_facts():
    payload = {
        "data": {
            "roomList": [{"subRoomList": [{"sRoomId": 501}, {"sRoomId": 502}]}],
            "saleRoomMap": {
                "501": {
                    "id": 501, "physicalRoomId": 77, "mealInfo": {
                        "title": "Завтрак за 900 ₽ (по желанию)", "mealFlag": 0,
                    },
                    "priceInfo": {"price": 12000, "displayPrice": "12 000 ₽", "currency": "RUB",
                                  "taxText": "+ сборы 500 ₽", "untrustedToken": "DO_NOT_COPY"},
                    "cancelInfo": {"title": "Не подлежит возврату"},
                    "saleRoomCategoryList": [{"type": "mealInfo", "title": "Завтрак за 900 ₽ (по желанию)"}],
                    "roomDescList": [{"text": "Бесплатный Wi-Fi"}],
                    "opaqueSessionToken": "DO_NOT_COPY",
                },
                "502": {
                    "id": 502, "physicalRoomId": 77, "mealInfo": {
                        "title": "Включен завтрак для 2 гостей", "mealFlag": 1,
                    },
                    "priceInfo": {"price": 14500, "displayPrice": "14 500 ₽", "currency": "RUB"},
                    "cancelInfo": {"title": "Бесплатная отмена до 18:00"},
                    "saleRoomCategoryList": [{"type": "mealInfo", "title": "Включен завтрак для 2 гостей"}],
                },
                "503": {
                    "id": 501, "physicalRoomId": 101,
                    "mealInfo": {"title": "Только номер"},
                    "priceInfo": {"price": 11000, "displayPrice": "11 000 ₽", "currency": "RUB"},
                },
            },
        },
        "requestToken": "DO_NOT_COPY",
    }
    normalized = normalize_tripcom_rate_inventory(
        payload, source_hotel_id="hotel-1", check_in="2026-11-06", check_out="2026-11-07",
        adults=2, room_details={"77": {"room_type": "Deluxe room", "bed_type": "1 king bed",
                                     "room_amenities": ["City view", "Free Wi-Fi"]}},
    )
    assert len(normalized["rates"]) == 3
    optional, included, repeated_record_id = normalized["rates"]
    assert optional["room_group_index"] == included["room_group_index"] == 0
    assert optional["meal_plan"] == "unknown"
    assert optional["meal_included"] is False
    assert optional["meal_available"] is True
    assert optional["meal_extra_amount"] == "900"
    assert optional["meal_extra_currency"] == "RUB"
    assert optional["price_amount"] == "12000"
    assert optional["room_type"] == "Deluxe room"
    assert optional["bed_type"] == "1 king bed"
    assert optional["room_amenities"] == ["City view", "Free Wi-Fi"]
    assert optional["cancellation"] == "non_refundable"
    assert included["meal_plan"] == "breakfast"
    assert included["meal_included"] is True
    assert included["cancellation"] == "free"
    assert repeated_record_id["provider_rate_id"] == "503"
    assert repeated_record_id["provider_rate_record_id"] == "501"
    assert all("DO_NOT_COPY" not in repr(rate) for rate in normalized["rates"])


def test_tripcom_room_rates_persist_and_reload_only_for_exact_booking_context(tmp_path):
    import json
    import sqlite3
    from datetime import datetime, timedelta, timezone

    database = tmp_path / "room-rates.sqlite3"
    init_hotel_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("search-1", json.dumps({"check_in": "2026-11-06", "check_out": "2026-11-07",
                                     "adults": 2, "children_count": 0}), "2026-09-28T00:00:00Z", "ok"),
        )
        connection.execute(
            """INSERT INTO hotel_observation
            (search_id,source,source_hotel_id,name,images_json,nearby_places_json,raw_json,observed_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            ("search-1", "trip_com", "hotel-1", "Sample", "[]", "[]", "{}", "2026-09-28T00:00:00Z"),
        )
    normalized = normalize_tripcom_rate_inventory(
        {"data": {"saleRoomMap": {
            "501": {"id": "rate-1", "physicalRoomId": 99,
                       "mealInfo": {"title": "Только номер"},
                       "priceInfo": {"price": 12000, "displayPrice": "12 000 ₽", "currency": "RUB"}},
        }}}, source_hotel_id="hotel-1", check_in="2026-11-06", check_out="2026-11-07", adults=2,
    )
    property_details = normalize_tripcom_property_details({
        "address": "123 Example Street", "check_in_text": "Заезд: после 14:00",
        "breakfast_text": "Завтрак · Шведский стол · часы 08:00 · 29 € с человека",
        "amenities": ["Парковка", "Wi-Fi"],
        "nearby_landmarks": [{"name": "Public beach", "category": "Landmarks",
                              "distance_text": "350 м", "distance_meters": 350}],
        "opaqueToken": "DO_NOT_COPY",
    })
    saved = persist_tripcom_room_rates(database, "search-1", normalized, property_details=property_details)
    assert saved["rates_persisted"] == 1
    result = _read_search(database, "search-1")
    hotel = next(item for item in result["hotels"] if item["source_hotel_id"] == "hotel-1")
    assert hotel["room_rates"][0]["meal_plan"] == "room_only"
    assert hotel["room_options"][0]["rates"][0]["provider_rate_id"] == "501"
    assert hotel["property_details"]["address"] == "123 Example Street"
    assert hotel["property_details"]["breakfast"]["available"] is True
    assert hotel["property_details"]["nearby_landmarks"][0]["distance_meters"] == 350
    assert hotel["room_rates"][0]["meal_plan"] == "room_only"  # hotel breakfast does not alter rate meal
    assert "DO_NOT_COPY" not in repr(hotel["property_details"])

    no_inventory = {**normalized, "status": "rate_inventory_absent", "rates": []}
    persist_tripcom_room_rates(database, "search-1", no_inventory,
                               status="rate_inventory_absent", property_details=property_details,
                               observed_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    reloaded = _read_search(database, "search-1")
    refreshed_hotel = next(item for item in reloaded["hotels"] if item["source_hotel_id"] == "hotel-1")
    assert refreshed_hotel["room_rates"] == []
    assert refreshed_hotel["room_rate_enrichment_status"] == "rate_inventory_absent"
    assert refreshed_hotel["property_details"]["address"] == "123 Example Street"

    wrong_context = {**normalized, "check_in": "2026-11-07"}
    try:
        persist_tripcom_room_rates(database, "search-1", wrong_context)
    except ValueError as exc:
        assert "booking context" in str(exc)
    else:
        raise AssertionError("A mismatched booking context must not persist as this search's inventory")


def test_tripcom_property_detail_normalization_keeps_provenance_and_rejects_fake_distances():
    details = normalize_tripcom_property_details({
        "address": "  1 Main Street  ", "check_in_text": "Заезд: после 14:00",
        "breakfast_text": "Breakfast buffet; €18 per person, pay at property",
        "amenities": ["Wi-Fi", "Wi-Fi", "  Pool  ", {"label": "ignore"}],
        "nearby_landmarks": [
            {"name": "City beach", "category": "Landmarks", "distance_text": "350 m",
             "distance_meters": 350},
            {"name": "Invalid", "distance_meters": -1},
            {"name": "No distance"},
        ],
    }, observed_at="2026-09-28T12:00:00Z")
    assert details["source"] == "trip_com_hotel_detail_dom"
    assert details["address"] == "1 Main Street"
    assert details["breakfast"]["available"] is True
    assert details["amenities"] == ["Wi-Fi", "Pool"]
    assert details["nearby_landmarks"] == [
        {"name": "City beach", "category": "Landmarks", "distance_text": "350 m",
         "distance_meters": 350.0, "source": "trip_com_displayed_landmark_distance"},
        {"name": "Invalid", "category": None, "distance_text": None,
         "distance_meters": None, "source": "trip_com_displayed_landmark_distance"},
        {"name": "No distance", "category": None, "distance_text": None,
         "distance_meters": None, "source": "trip_com_displayed_landmark_distance"},
    ]
    conditional = normalize_tripcom_property_details({
        "breakfast_text": "Breakfast: contact the property to check whether it is available."
    })
    unavailable = normalize_tripcom_property_details({"breakfast_text": "Breakfast is not available."})
    assert conditional["breakfast"]["available"] is None
    assert unavailable["breakfast"]["available"] is False


def test_room_rate_fetch_diagnostics_migrate_and_redact_secrets(tmp_path):
    import json
    import sqlite3

    from aviasales_collector.hotel_search import _sanitize_enrichment_error_message

    database = tmp_path / "legacy-rate-fetch.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE hotel_room_rate_fetch (
            id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
            source_hotel_id TEXT NOT NULL, check_in TEXT NOT NULL, check_out TEXT NOT NULL,
            adults INTEGER NOT NULL, children_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, http_status INTEGER, latency_ms INTEGER,
            rates_seen INTEGER NOT NULL DEFAULT 0, observed_at TEXT NOT NULL,
            property_details_json TEXT)""")
    init_hotel_store(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(hotel_room_rate_fetch)")}
        assert {"operation", "error_type", "error_message", "attempt", "retry_count"} <= columns
        connection.execute(
            "INSERT INTO hotel_search_session(id,query_json,started_at,status) VALUES (?,?,?,?)",
            ("diagnostic-search", json.dumps({"check_in": "2026-11-06", "check_out": "2026-11-07",
                                               "adults": 2, "children_count": 0}), "2026-09-28T00:00:00Z", "running"),
        )
        connection.execute(
            """INSERT INTO hotel_observation
            (search_id,source,source_hotel_id,name,images_json,nearby_places_json,raw_json,observed_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            ("diagnostic-search", "trip_com", "123", "Rate Hotel", "[]", "[]", "{}", "2026-09-28T00:00:00Z"),
        )
    inventory = {"source_hotel_id": "123", "check_in": "2026-11-06", "check_out": "2026-11-07",
                 "adults": 2, "children_count": 0, "rates": []}
    persist_tripcom_room_rates(
        database, "diagnostic-search", inventory, status="navigation_error",
        operation="tripcom_detail_rate_inventory", error_type="TimeoutError",
        error_message="https://user:password@example.test/detail?token=secret Cookie: sid=private",
        attempt=2, retry_count=1, observed_at="2026-09-28T00:00:01Z",
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT operation,error_type,error_message,attempt,retry_count,observed_at "
            "FROM hotel_room_rate_fetch WHERE search_id='diagnostic-search'",
        ).fetchone()
    assert row[0:2] == ("tripcom_detail_rate_inventory", "TimeoutError")
    assert row[3:5] == (2, 1)
    assert row[5] == "2026-09-28T00:00:01Z"
    assert "password" not in row[2] and "secret" not in row[2] and "private" not in row[2]
    assert "user:" not in row[2] and "?" not in row[2]
    sanitized = _sanitize_enrichment_error_message("Authorization: Bearer xyz-token")
    assert "xyz-token" not in sanitized


def test_saved_offer_meal_reprocessing_preserves_raw_payload(tmp_path):
    import json
    import sqlite3

    db = tmp_path / "meal.sqlite3"
    init_hotel_store(db)
    raw_offer = {"provider": "trip.com", "meal_plan": "unknown", "labels": [],
                 "price": {"value": 10000, "currency": "RUB"}}
    stored = {"meal_plan": "unknown", "raw_payload": raw_offer, "labels": []}
    with sqlite3.connect(db) as connection:
        connection.execute(
            """INSERT INTO hotel_offer_observation
               (search_id,source,source_hotel_id,labels_json,raw_json,observed_at)
               VALUES (?,?,?,?,?,?)""",
            ("search", "trip_com", "hotel", "[]", json.dumps(stored), "2026-01-01T00:00:00Z"),
        )
    assert reprocess_saved_offer_meals(db) == {"reprocessed": 1, "unknown": 1}
    with sqlite3.connect(db) as connection:
        result = json.loads(connection.execute(
            "SELECT raw_json FROM hotel_offer_observation"
        ).fetchone()[0])
    assert result["raw_payload"] == raw_offer
    assert result["meal_plan"] == "unknown"
    assert result["meal_raw_text"] is None
    assert result["meal_normalizer_version"] == "meal_v4"


def test_city_reference_calculates_center_distance_without_hotel_fallback(tmp_path):
    db = tmp_path / "hotels.sqlite3"
    reference = CityReference("dubai", "uae", "Dubai reference", 25.2048, 55.2708,
                              "validated_test_reference", utc_now())
    register_city_reference(db, reference)
    init_hotel_store(db)
    import sqlite3
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT center_lat,center_lon FROM hotel_city_reference").fetchone()
    assert row == (25.2048, 55.2708)

    location = _location_summary(
        {"name": "Hotel", "address": "Address"},
        [{"source": "trip_com", "coordinates": {"lat": 25.205, "lng": 55.271},
          "nearby_places": []}],
        reference,
    )
    assert location["distance_to_center_m"] is not None
    assert location["center_source"] == "validated_test_reference"
    assert location["center_reference"]["city_slug"] == "dubai"


def test_missing_city_reference_does_not_invent_center_distance():
    location = _location_summary(
        {"name": "Hotel", "address": "Address"},
        [{"source": "trip_com", "coordinates": {"lat": 25.205, "lng": 55.271},
          "nearby_places": []}],
    )
    assert location["distance_to_center_m"] is None


def test_offer_variants_keep_context_and_decimal_amounts():
    hotel = normalize_hotel({
        "id": "1", "name": "Hotel", "offers": [
            {"price": {"value": 10000, "currency": "RUB"}, "room_type": "Standard",
             "labels": ["breakfast_included"]},
            {"price": {"value": 10000, "currency": "RUB"}, "room_type": "Suite",
             "labels": ["room only"]},
        ]}, source="trip_com", query={"check_in": "2027-01-01", "check_out": "2027-01-05", "adults": 2})
    assert len(hotel["offers"]) == 2
    assert {offer["meal_plan"] for offer in hotel["offers"]} == {"breakfast", "room_only"}
    assert {offer["room_type"] for offer in hotel["offers"]} == {"Standard", "Suite"}
    assert all(offer["original_amount"] == "100" for offer in hotel["offers"])


def test_non_beach_city_profile_leaves_beach_distance_unknown(tmp_path, monkeypatch):
    import io

    from aviasales_collector.hotels.geo import geometry

    db = tmp_path / "geo.sqlite3"
    register_city_geo_profile(db, CityGeoProfile(
        "paris", "france", False, "validated_geo_profile", utc_now()))
    lookup_calls = []

    def empty_beach_lookup(*_args):
        lookup_calls.append("overpass")
        return [], "openstreetmap_overpass"

    monkeypatch.setattr(geometry, "_overpass_beach_features", empty_beach_lookup)
    monkeypatch.setattr(geometry, "_profiled_urlopen",
                        lambda *_args, **_kwargs: io.BytesIO(b'{"features":[]}'))

    assert _resolve_nearest_beach(db, {"lat": 48.8566, "lng": 2.3522}, "Paris", "France") is None
    assert lookup_calls == ["overpass"]


def test_offer_comparability_requires_known_booking_terms():
    from aviasales_collector.hotel_search import compare_offers

    base = {
        "check_in": "2027-06-01", "check_out": "2027-06-05", "adults": 2,
        "children_count": 0, "rooms": 1, "currency": "RUB",
        "meal_plan": "breakfast", "cancellation": "free", "tax_status": "included",
        "room_type": "Standard double room",
    }
    assert compare_offers(base, dict(base)) == {"status": "comparable", "reasons": []}
    assert compare_offers(base, {**base, "meal_plan": "room_only"}) == {
        "status": "partially_comparable", "reasons": ["meal"]}
    assert compare_offers(base, {**base, "meal_plan": "room_only", "cancellation": "unknown"}) == {
        "status": "partially_comparable", "reasons": ["meal", "unknown_cancellation"]}
    assert compare_offers(base, {**base, "cancellation": "unknown"}) == {
        "status": "unknown", "reasons": ["unknown_cancellation"]}
    assert compare_offers(base, {**base, "tax_status": "unknown"}) == {
        "status": "unknown", "reasons": ["unknown_taxes"]}
    assert compare_offers(base, {**base, "check_in": "2027-06-02"}) == {
        "status": "unknown", "reasons": ["different_check_in"]}
    assert compare_offers({**base, "canonical_hotel_id": "hotel-a"},
                          {**base, "canonical_hotel_id": "hotel-b"}) == {
        "status": "unknown", "reasons": ["different_hotels"]}
