from aviasales_collector.hotels.normalization import _offer_meal_facts, classify_meal_plan, normalize_hotel
from aviasales_collector.hotels.sources.tripcom_rates import normalize_tripcom_rate_inventory


def test_meal_status_distinguishes_included_not_included_and_unknown():
    cases = [
        ("Breakfast included", "breakfast", "included"),
        ("Room only", "room_only", "not_included"),
        ("Half board", "half_board", "included"),
        ("All inclusive", "all_inclusive", "included"),
        ("Meals not included", "room_only", "not_included"),
        (None, "unknown", "unknown"),
    ]
    for raw, expected_plan, expected_status in cases:
        facts = _offer_meal_facts({"meal": raw} if raw else {}, "test")
        assert facts["meal_plan"] == expected_plan
        assert facts["meal_status"] == expected_status

    russian_included = "\u0412\u043a\u043b\u044e\u0447\u0451\u043d \u0437\u0430\u0432\u0442\u0440\u0430\u043a"
    russian_room_only = "\u0411\u0435\u0437 \u043f\u0438\u0442\u0430\u043d\u0438\u044f"
    russian_half_board = "\u041f\u043e\u043b\u0443\u043f\u0430\u043d\u0441\u0438\u043e\u043d"
    russian_all_inclusive = "\u0412\u0441\u0451 \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u043e"
    assert classify_meal_plan(text=russian_included) == "breakfast"
    assert _offer_meal_facts({"meal": russian_included}, "test")["meal_status"] == "included"
    assert _offer_meal_facts({"meal": russian_room_only}, "test")["meal_status"] == "not_included"
    assert _offer_meal_facts({"meal": russian_half_board}, "test")["meal_plan"] == "half_board"
    assert _offer_meal_facts({"meal": russian_all_inclusive}, "test")["meal_plan"] == "all_inclusive"


def test_meal_combination_is_not_collapsed_to_breakfast():
    text = "\u0412\u043a\u043b\u044e\u0447\u0435\u043d\u044b \u0437\u0430\u0432\u0442\u0440\u0430\u043a \u0438 \u0443\u0436\u0438\u043d \u0434\u043b\u044f 2 \u0433\u043e\u0441\u0442\u0435\u0439"
    assert classify_meal_plan(text=text) == "breakfast_dinner"
    facts = _offer_meal_facts({"meal": text}, "trip_com_rate_inventory")
    assert facts["meal_plan"] == "breakfast_dinner"
    assert facts["meal_status"] == "included"


def test_optional_breakfast_is_paid_but_bare_availability_stays_unknown():
    paid = _offer_meal_facts({"meal": "Breakfast for 2,738 AED (optional)"}, "trip_com")
    assert paid["meal_plan"] == "unknown"
    assert paid["meal_status"] == "paid"
    assert paid["meal_included"] is False

    available = _offer_meal_facts({"meal": "Breakfast available"}, "trip_com")
    assert available["meal_plan"] == "unknown"
    assert available["meal_status"] == "unknown"
    assert available["meal_available"] is True


def test_trip_rate_inventory_uses_real_meal_info_title_fields():
    title = "\u0412\u043a\u043b\u044e\u0447\u0435\u043d\u044b \u0437\u0430\u0432\u0442\u0440\u0430\u043a \u0438 \u0443\u0436\u0438\u043d \u0434\u043b\u044f 2 \u0433\u043e\u0441\u0442\u0435\u0439"
    payload = {
        "data": {
            "saleRoomMap": {"rate-1": {
                "mealInfo": {"title": title},
                "saleRoomCategoryList": [{"type": "mealInfo", "title": title}],
                "priceInfo": {"price": 1000, "currency": "AED"},
                "cancelInfo": {"title": "Non-refundable"},
            }},
            "roomList": [],
            "physicRoomMap": {},
        }
    }
    inventory = normalize_tripcom_rate_inventory(
        payload, source_hotel_id="hotel-1", check_in="2026-10-20", check_out="2026-10-25", adults=2,
    )
    assert inventory["status"] == "ok"
    rate = inventory["rates"][0]
    assert rate["meal_raw_text"] == title
    assert rate["meal_plan"] == "breakfast_dinner"
    assert rate["meal_status"] == "included"
    assert rate["rate_labels"] == [title]


def test_sravni_offer_without_meal_label_stays_unknown():
    offer = normalize_hotel({
        "id": "hotel-1", "name": "Hotel",
        "offers": [{"price": {"value": 10000, "currency": "RUB"}, "labels": []}],
    }, source="sravniotel")["offers"][0]
    assert offer["meal_plan"] == "unknown"
    assert offer["meal_status"] == "unknown"
