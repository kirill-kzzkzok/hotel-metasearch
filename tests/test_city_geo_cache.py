"""Regression: confirmed city geo_ids persist and survive suggest outages.

A city resolution used to depend on the live Sravni suggest endpoint for
every search. When the endpoint failed, the city degraded to trip-only
with geo_id=None even though the same city had been confirmed hours
earlier (observed 2026-10-11..13: Da Nang flipped between confirmed
geo_id=2658853 and unresolved across sessions). suggest_cities now
persists confirmed city items into hotel_city_geo_cache and consults
the cache when the live endpoint and the alias path both fail.
"""
from __future__ import annotations

import asyncio

import pytest

from aviasales_collector.hotels import city_resolution
from aviasales_collector.hotels.city_resolution import suggest_cities
from aviasales_collector.hotels.models import SourceError


def test_confirmed_city_is_persisted_and_served_from_cache(tmp_path, monkeypatch):
    database = tmp_path / "city-cache.sqlite3"
    monkeypatch.setenv("HOT_TICKETS_DB_PATH", str(database))
    monkeypatch.setattr(city_resolution, "_travelpayouts_city_aliases",
                        lambda _q: [])

    confirmed = {"geo_id": "2658853", "name": "Дананг", "country": "Вьетнам",
                 "city_slug": "da-nang", "country_slug": "vietnam"}

    class FakeSource:
        fail = False

        async def suggest(self, query):
            if self.fail:
                raise SourceError("unavailable", "endpoint down")
            return [dict(confirmed)], 200, 10

    monkeypatch.setattr(city_resolution, "SRAVNIOTEL_SOURCE", FakeSource())
    monkeypatch.setattr(city_resolution, "SravniOtelSource", FakeSource)

    found, _status, _ms = asyncio.run(suggest_cities("Дананг"))
    assert found and found[0]["geo_id"] == "2658853"
    assert database.exists()

    # Endpoint goes down entirely: the cache must still resolve the city.
    FakeSource.fail = True
    found, _status, _ms = asyncio.run(suggest_cities("Дананг"))
    assert found and found[0]["geo_id"] == "2658853"


def test_unconfirmed_city_still_degrades_to_trip_only(tmp_path, monkeypatch):
    database = tmp_path / "city-cache.sqlite3"
    monkeypatch.setenv("HOT_TICKETS_DB_PATH", str(database))
    monkeypatch.setattr(city_resolution, "_travelpayouts_city_aliases",
                        lambda _q: [{"name": "Da Nang", "country": "Вьетнам", "iata_code": "DAD"}])

    class FakeSource:
        async def suggest(self, query):
            raise SourceError("unavailable", "endpoint down")

    monkeypatch.setattr(city_resolution, "SRAVNIOTEL_SOURCE", FakeSource())
    monkeypatch.setattr(city_resolution, "SravniOtelSource", FakeSource)

    found, _status, _ms = asyncio.run(suggest_cities("Da Nang"))
    # The alias suggest also fails, and nothing was ever confirmed for it.
    assert found == [{"geo_id": None, "name": "Da Nang", "country": "Вьетнам",
                      "city_slug": None, "country_slug": None, "iata_code": "DAD",
                      "resolution_source": "travelpayouts"}]


def test_cache_miss_for_unknown_city_raises_unavailable(tmp_path, monkeypatch):
    database = tmp_path / "city-cache.sqlite3"
    monkeypatch.setenv("HOT_TICKETS_DB_PATH", str(database))
    monkeypatch.setattr(city_resolution, "_travelpayouts_city_aliases",
                        lambda _q: [])

    class FakeSource:
        async def suggest(self, query):
            raise SourceError("unavailable", "endpoint down")

    monkeypatch.setattr(city_resolution, "SRAVNIOTEL_SOURCE", FakeSource())
    monkeypatch.setattr(city_resolution, "SravniOtelSource", FakeSource)

    with pytest.raises(SourceError):
        asyncio.run(suggest_cities("Теперьхертоинск"))
