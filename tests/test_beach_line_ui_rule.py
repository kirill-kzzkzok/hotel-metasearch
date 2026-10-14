"""UI rule (user directive): a city without beaches must not show «до пляжа» at all.

Bangkok live smoke 2026-10-14: cards rendered "🏖 — до пляжа" for an inland city
(is_beach_destination=0). Backend already sends location.beach_relevant=false /
beach_status="not_applicable" (geo/core.py); hotels.js:1566 rendered the line
unconditionally. Fix: render the beach line only when the beach dimension is
applicable for the hotel's city.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "aviasales_collector" / "web_assets" / "hotels.js"
SOURCE = JS.read_text(encoding="utf-8")


def test_hotel_card_beach_line_is_conditional():
    card_block = re.search(
        r'const beachRaw = hotel\.location\?\.distance_to_beach_km;.*?'
        r'addText\(context, "beach-line".*?\);',
        SOURCE, re.S).group(0)
    assert "beachApplicable" in card_block, "card must gate the beach line on applicability"
    assert 'hotel.location?.beach_relevant !== false' in card_block
    assert 'hotel.location?.beach_status !== "not_applicable"' in card_block
    # The render itself must sit inside the applicability guard, not before it.
    guard = re.search(r'const beachApplicable.*?if \(beachApplicable\) \{', card_block, re.S)
    render = re.search(r'addText\(context, "beach-line"', card_block)
    assert guard and render and guard.end() < render.start()


def test_map_popup_beach_line_is_conditional():
    popup_block = re.search(
        r'const beachDistanceValue = hotel\.location\?\.distance_to_beach_km;.*?'
        r'addText\(facts, "map-popup-distance", `\$\{formatDistance\(beachDistance\)\} до пляжа`\);',
        SOURCE, re.S).group(0)
    assert "beachApplicable" in popup_block
    guard = re.search(r'const beachApplicable.*?if \(beachApplicable && hasDistance', popup_block, re.S)
    render = re.search(r'addText\(facts, "map-popup-distance"', popup_block)
    assert guard and render and guard.end() < render.start()


def test_non_beach_city_payload_marks_not_applicable():
    """Backend contract: beach_relevant=false produces beach_status not_applicable."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from aviasales_collector.hotels.geo.core import _location_summary
    records = [{"cached_beach_relevant": False, "cached_beach_status": "not_found"}]
    location = _location_summary({"name": "x"}, records, None)
    assert location["beach_relevant"] is False
    assert location["beach_status"] == "not_applicable"
    assert location["beach_line"] is None or location["distance_to_beach_km"] is None


def test_beach_city_payload_keeps_distance():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from aviasales_collector.hotels.geo.core import _location_summary
    records = [{
        "cached_beach_relevant": True,
        "cached_beach_distance_meters": 845.0,
        "cached_beach_name": "Platja de la Mar Bella",
        "cached_beach_source": "openstreetmap_overpass_geometry",
        "cached_beach_status": "ok",
        "cached_beach_geometry_available": True,
        "cache_key": "k",
    }]
    location = _location_summary({"name": "x"}, records, None)
    assert location["beach_relevant"] is True
    assert location["beach_status"] == "available"
    assert location["distance_to_beach_km"] == 0.845
