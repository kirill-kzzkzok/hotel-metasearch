"""Regression: «Личный номер» filter semantics (mission spec §9).

Private = a separate space for the user's group. A bed in a dorm, a hostel
bed-space, a capsule or any shared room is NOT private. A rate name or the
presence of a bed proves nothing. Unknown stays unknown — never displayed
as a confirmed private room. Coverage: private / shared / unknown across
English and Russian room naming, plus the pipeline filter behavior.
"""
from __future__ import annotations

import pytest

from aviasales_collector.hotels.normalization import classify_room_privacy


@pytest.mark.parametrize("room_type,bed_type,expected", [
    ("Double Room", "double bed", "private"),
    ("Standard Twin Room", None, "private"),
    ("Deluxe Двухместный номер", None, "private"),
    ("Private room with private bathroom", None, "private"),
    ("Семейный номер", None, "private"),
    ("Bed in 6-bed dormitory", None, "shared"),
    ("Койко-место в общей комнате", None, "shared"),
    ("Capsule pod", None, "shared"),
    ("Shared room, female dormitory", None, "shared"),
    ("Hostel bed space", None, "shared"),
    (None, None, "unknown"),
    ("", "", "unknown"),
    ("Superior", None, "unknown"),  # bare adjective proves nothing
    (None, "1 double bed", "unknown"),  # bed alone proves nothing
])
def test_room_privacy_classification(room_type, bed_type, expected):
    assert classify_room_privacy(room_type, bed_type) == expected


def test_private_mode_filters_shared_and_unknown_but_keeps_private():
    from aviasales_collector.hotels.normalization import classify_room_privacy  # noqa: F401

    def _project(result):
        """Run the exact room-mode projection from pipeline._finalize_search."""
        room_mode = result.get("query", {}).get("room_mode", "private")
        all_count = len(result["hotels"])
        if room_mode == "private":
            filtered = []
            for hotel in result["hotels"]:
                offers = [offer for offer in hotel.get("offers", [])
                          if offer.get("room_privacy") == "private"]
                if offers:
                    filtered.append({**hotel, "offers": offers})
            result["hotels"] = filtered
        result["room_audit"] = {
            "mode": room_mode,
            "eligible_hotels": len(result["hotels"]),
            "all_hotels": all_count,
        }
        return result

    result = {
        "query": {"room_mode": "private"},
        "hotels": [
            {"name": "A", "offers": [
                {"amount": 100, "room_privacy": "private"},
                {"amount": 90, "room_privacy": "shared"},
            ]},
            {"name": "B", "offers": [{"amount": 80, "room_privacy": "shared"}]},
            {"name": "C", "offers": [{"amount": 70, "room_privacy": "unknown"}]},
        ],
    }
    projected = _project(result)
    assert projected["query"]["room_mode"] == "private"
    assert [hotel["name"] for hotel in projected["hotels"]] == ["A"]
    assert projected["hotels"][0]["offers"] == [{"amount": 100, "room_privacy": "private"}]
    audit = projected["room_audit"]
    assert audit["mode"] == "private"
    assert audit["all_hotels"] == 3 and audit["eligible_hotels"] == 1


def test_all_mode_keeps_every_offer():
    def _project(result):
        room_mode = result.get("query", {}).get("room_mode", "private")
        if room_mode == "private":
            filtered = []
            for hotel in result["hotels"]:
                offers = [offer for offer in hotel.get("offers", [])
                          if offer.get("room_privacy") == "private"]
                if offers:
                    filtered.append({**hotel, "offers": offers})
            result["hotels"] = filtered
        return result

    result = {
        "query": {"room_mode": "all"},
        "hotels": [
            {"name": "A", "offers": [
                {"amount": 100, "room_privacy": "private"},
                {"amount": 90, "room_privacy": "shared"},
            ]},
        ],
    }
    projected = _project(result)
    assert projected["query"]["room_mode"] == "all"
    assert len(projected["hotels"][0]["offers"]) == 2


def test_pipeline_projection_predicate_is_live():
    """The projection above must match the production implementation."""
    import inspect
    import aviasales_collector.hotels.pipeline as pipeline

    source = inspect.getsource(pipeline._finalize_search)
    assert 'offer.get("room_privacy") == "private"' in source, (
        "_finalize_search must keep filtering private-mode offers on exact privacy match"
    )
