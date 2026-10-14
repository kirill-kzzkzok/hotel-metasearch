"""Regression: Sravni reviews button gets a real source page URL (spec §10).

Root cause: the SravniOtel list contract returns canonical_url (a site-relative
hotel page path, e.g. /vietnam/cities/da-nang/7-seven-sea-hotel) in raw JSON,
but normalization dropped it, so the frontend reviewLink() found no candidate
for Sravni-only cards and the reviews button never appeared. The live page
carries the hotel reviews (verified: HTTP 200, review markers present).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import aviasales_collector.hotels.normalization as normalization
from aviasales_collector.hotels.normalization import _normalize_source_hotel_url, normalize_hotel

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_source_hotel_url_absolute_passes_through():
    assert _normalize_source_hotel_url("https://example.com/hotel") == "https://example.com/hotel"


def test_source_hotel_url_site_relative_is_absolutized():
    assert (_normalize_source_hotel_url("/vietnam/cities/da-nang/7-seven-sea-hotel")
            == "https://hotels.travel.ru/vietnam/cities/da-nang/7-seven-sea-hotel")


def test_source_hotel_url_garbage_is_rejected():
    assert _normalize_source_hotel_url(None) is None
    assert _normalize_source_hotel_url("") is None
    assert _normalize_source_hotel_url("javascript:alert(1)") is None
    assert _normalize_source_hotel_url("relative/path") is None


def test_normalize_hotel_exposes_source_hotel_url():
    item = {
        "id": 42, "name": "Seven Sea Hotel", "stars": 3, "rating": 8.7,
        "reviews_count": 13, "offers": [], "canonical_url": "/vietnam/cities/da-nang/7-seven-sea-hotel",
    }
    hotel = normalize_hotel(item)
    assert hotel["source_hotel_url"] == "https://hotels.travel.ru/vietnam/cities/da-nang/7-seven-sea-hotel"


def test_normalize_hotel_without_canonical_url_has_none():
    item = {"id": 42, "name": "Hotel", "offers": []}
    hotel = normalize_hotel(item)
    assert hotel["source_hotel_url"] is None


def test_frontend_review_link_uses_source_hotel_url():
    """hotels.js reviewLink must consider source_hotel_url candidates."""
    source = (PROJECT_ROOT / "aviasales_collector/web_assets/hotels.js").read_text(encoding="utf-8")
    assert "hotel.source_hotel_url" in source, (
        "reviewLink must try the source hotel page URL for Sravni-only cards"
    )
    assert "record?.source_hotel_url" in source and "detail?.source_hotel_url" in source
