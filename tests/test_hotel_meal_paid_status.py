from __future__ import annotations

import pytest

from aviasales_collector.hotels.normalization import _offer_meal_facts


@pytest.mark.parametrize(("raw", "plan", "status", "included", "available"), [
    ("Breakfast included", "breakfast", "included", True, True),
    ("Room only", "room_only", "not_included", False, None),
    ("No meals", "room_only", "not_included", False, None),
    ("Half board", "half_board", "included", True, True),
    ("Full board", "full_board", "included", True, True),
    ("All inclusive", "all_inclusive", "included", True, True),
    ("Breakfast available for an additional charge", "unknown", "paid", False, True),
    ("Breakfast available", "unknown", "unknown", None, True),
    ("Breakfast not included", "room_only", "not_included", False, True),
    ("Breakfast", "unknown", "unknown", None, True),
    ("Отличный завтрак за 802,86 ₽ (по желанию)", "unknown", "paid", False, True),
])
def test_meal_status_keeps_included_paid_not_included_and_unknown_distinct(
    raw, plan, status, included, available,
):
    facts = _offer_meal_facts({"meal": raw}, "trip_com_rate_inventory")
    assert facts["meal_plan"] == plan
    assert facts["meal_status"] == status
    assert facts["meal_included"] is included
    assert facts["meal_available"] is available


@pytest.mark.parametrize(("label", "plan", "status", "included"), [
    ("breakfast_included", "breakfast", "included", True),
    ("room_only", "room_only", "not_included", False),
    ("half_board", "half_board", "included", True),
    ("full_board", "full_board", "included", True),
    ("all_inclusive", "all_inclusive", "included", True),
    ("breakfast_available", "unknown", "unknown", None),
    ("breakfast_extra", "unknown", "paid", False),
    ("meal_unknown", "unknown", "unknown", None),
])
def test_meal_label_codes_preserve_unknown_and_paid_states(label, plan, status, included):
    facts = _offer_meal_facts({"labels": [label]}, "trip_com_rate_inventory")
    assert facts["meal_plan"] == plan
    assert facts["meal_status"] == status
    assert facts["meal_included"] is included


def test_paid_breakfast_keeps_real_surcharge_and_currency():
    facts = _offer_meal_facts(
        {"meal": "Отличный завтрак за 802,86 ₽ (по желанию)"},
        "trip_com_rate_inventory",
    )
    assert facts["meal_available"] is True
    assert facts["meal_extra_amount"] == "802.86"
    assert facts["meal_extra_currency"] == "RUB"

@pytest.mark.parametrize(("raw", "plan", "included", "available"), [
    ("Breakfast included", "breakfast", True, True),
    ("Room only", "room_only", False, None),
    ("No meals", "room_only", False, None),
    ("Half board", "half_board", True, True),
    ("Full board", "full_board", True, True),
    ("All inclusive", "all_inclusive", True, True),
    ("Breakfast available for an additional charge", "unknown", False, True),
    ("Breakfast available", "unknown", None, True),
    ("Breakfast not included", "room_only", False, True),
    ("Breakfast", "unknown", None, True),
])
def test_rate_name_meal_terms_do_not_turn_unknown_into_not_included(raw, plan, included, available):
    facts = _offer_meal_facts({"rate_name": raw}, "trip_com_rate_inventory")
    assert facts["meal_plan"] == plan
    assert facts["meal_included"] is included
    assert facts["meal_available"] is available
