"""Regression: hotels.travel.ru moved to /api/v1 with snake_case pagination.

The old /api/public/hotels endpoints 404. The v1 search response renamed
totalHotels -> total_hotels and nextPageToken -> next_page_token; downstream
cache/persistence/read-model still consume the historical camelCase keys, so
SravniOtelSource must normalize them after fetching. Suggest requires a
Cyrillic transliteration for Da Nang; the Latin query falls back through
travelpayouts aliases (existing behavior, kept unchanged).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from aviasales_collector.hotels.models import SourceResult
from aviasales_collector.hotels.sources import sravni


def test_sravni_base_points_at_live_v1_api():
    assert sravni.SRAVNI_BASE == "https://hotels.travel.ru/api/v1"


def _v1_search_payload() -> dict:
    return {
        "total_hotels": 736,
        "next_page_token": "2",
        "providers": [{"code": "ostrovok", "display_name": "Островок"}],
        "hotels": [{
            "id": 7059410,
            "name": "Nhat Minh Hotel and Apartment",
            "address": "VN, Da Nang, Lot D1, Tran Bach Dang Street",
            "stars": 4,
            "rating": 9.77,
            "reviews_count": 4,
            "location": {"lat": 16.049635, "lon": 108.24776},
            "offers": [{
                "provider": "ostrovok",
                "price": {"currency": "RUB", "value": 497300},
                "deeplink": "https://ostrovok.ru/rooms/example",
                "labels": ["breakfast_included"],
            }],
        }],
    }


def test_v1_snake_case_pagination_is_normalized_to_camel_case(monkeypatch):
    captured = {}

    async def fake_run_in_executor(_executor, fn, *args, **kwargs):
        captured["url"] = args[0] if args else kwargs.get("url")
        return _v1_search_payload(), 200, 12

    monkeypatch.setattr(sravni.asyncio, "get_running_loop",
                        lambda: type("Loop", (), {"run_in_executor": staticmethod(fake_run_in_executor)})())

    query = {"geo_id": "2658853", "check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0, "limit": 5}
    result = asyncio.run(sravni.SRAVNIOTEL_SOURCE.search(query))

    assert captured["url"].startswith("https://hotels.travel.ru/api/v1/search?")
    assert result.status == "ok"
    assert result.total_hotels == 736
    assert result.next_page_token == "2"
    assert result.hotels and result.hotels[0]["name"] == "Nhat Minh Hotel and Apartment"


def test_v1_pagination_stop_without_next_page_token(monkeypatch):
    payload = _v1_search_payload()
    payload["next_page_token"] = None

    async def fake_run_in_executor(_executor, fn, *args, **kwargs):
        return payload, 200, 12

    monkeypatch.setattr(sravni.asyncio, "get_running_loop",
                        lambda: type("Loop", (), {"run_in_executor": staticmethod(fake_run_in_executor)})())

    query = {"geo_id": "2658853", "check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0, "limit": 5}
    result = asyncio.run(sravni.SRAVNIOTEL_SOURCE.search(query))

    assert result.status == "ok"
    assert result.next_page_token is None
    assert result.total_hotels == 736


def test_v1_response_without_hotels_list_is_invalid(monkeypatch):
    async def fake_run_in_executor(_executor, fn, *args, **kwargs):
        return {"total_hotels": 0, "next_page_token": None}, 200, 12

    monkeypatch.setattr(sravni.asyncio, "get_running_loop",
                        lambda: type("Loop", (), {"run_in_executor": staticmethod(fake_run_in_executor)})())

    query = {"geo_id": "2658853", "check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0, "limit": 5}
    result = asyncio.run(sravni.SRAVNIOTEL_SOURCE.search(query))

    assert result.status == "invalid_response"


def test_camel_case_contract_is_not_double_normalized(monkeypatch):
    """A payload that already uses camelCase keeps its own values."""
    payload = {
        "totalHotels": 42,
        "nextPageToken": "9",
        "hotels": [{
            "id": 1, "name": "Camel Case Hotel", "offers": [],
        }],
    }

    async def fake_run_in_executor(_executor, fn, *args, **kwargs):
        return payload, 200, 12

    monkeypatch.setattr(sravni.asyncio, "get_running_loop",
                        lambda: type("Loop", (), {"run_in_executor": staticmethod(fake_run_in_executor)})())

    query = {"geo_id": "2658853", "check_in": "2026-11-14", "check_out": "2026-11-17",
             "adults": 2, "children_count": 0, "limit": 5}
    result = asyncio.run(sravni.SRAVNIOTEL_SOURCE.search(query))

    assert result.status == "ok"
    assert result.total_hotels == 42
    assert result.next_page_token == "9"
