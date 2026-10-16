"""Unit tests for SravniOtel hotel-page policy/amenity enrichment.

The list API carries no check-in/out policy times and no hotel amenities;
the public hotel pages do (server-rendered HTML). parse_sravni_hotel_page
extracts only public user-facing facts; enrich_sravni_hotel_pages fetches
pages over plain HTTP (no browser, no Trip.com detail navigation) and
persists them into hotel_provider_metadata.
"""
from __future__ import annotations

import json
import sqlite3

from aviasales_collector.hotels.sravni_detail import (
    parse_sravni_hotel_api,
    parse_sravni_hotel_page,
    enrich_sravni_hotel_pages,
    lookup_sravni_policy,
)


def _make_db(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE hotel_observation (id INTEGER PRIMARY KEY, search_id TEXT, source TEXT, "
        "source_hotel_id TEXT, name TEXT, raw_json TEXT)"
    )
    connection.execute(
        "CREATE TABLE hotel_provider_metadata (search_id TEXT, source TEXT, source_hotel_id TEXT, "
        "provider TEXT, metadata_json TEXT, retrieved_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE hotel_search_session (id TEXT PRIMARY KEY, query_json TEXT, status TEXT, "
        "total_hotels INTEGER, started_at TEXT, finished_at TEXT)"
    )
    connection.execute(
        "INSERT INTO hotel_search_session(id, query_json, status, total_hotels) VALUES ('s1', ?, 'success', 1)",
        (json.dumps({"check_in": "2026-11-20", "check_out": "2026-11-23", "adults": 2}),),
    )
    return connection


def test_parser_extracts_policy_times_and_amenities():
    html = """
    <html><body>
      <div>Великолепно 81 отзыв Заезд с 14:00 Выезд до 12:00</div>
      <p>В отеле предоставляется бесплатный Wi-Fi, работает круглосуточная стойка
      регистрации. Кондиционер, Трансфер, Парковка, бассейн.</p>
    </body></html>
    """
    facts = parse_sravni_hotel_page(html)
    assert facts["check_in_text"] == "Заезд с 14:00"
    assert facts["check_out_text"] == "Выезд до 12:00"
    for token in ("Wi-Fi", "Кондиционер", "Трансфер", "круглосуточная стойка", "бассейн"):
        assert token in facts["amenities_text"], facts["amenities_text"]


def test_parser_returns_none_for_page_without_policy():
    facts = parse_sravni_hotel_page("<html><body>Просто страница без данных</html>")
    assert facts["check_in_text"] is None
    assert facts["check_out_text"] is None
    assert facts["amenities_text"] == []


def test_enrich_skips_hotels_without_canonical_url(tmp_path, monkeypatch):
    database_path = tmp_path / "sravni.sqlite3"
    connection = _make_db(database_path)
    connection.execute(
        "INSERT INTO hotel_observation(search_id, source, source_hotel_id, name, raw_json) "
        "VALUES ('s1', 'sravniotel', '1', 'Hotel', ?)",
        (json.dumps({"canonical_url": None}),),
    )
    connection.commit()
    connection.close()

    def fail_fetch(path):
        raise AssertionError("must not fetch anything when no canonical_url")

    monkeypatch.setattr("aviasales_collector.hotels.sravni_detail._fetch_page", fail_fetch)
    result = enrich_sravni_hotel_pages(str(database_path), "s1")
    assert result["pending"] == 0
    assert result["enriched"] == 0


def test_enrich_persists_policy_and_lookup_reads_it_back(tmp_path, monkeypatch):
    database_path = tmp_path / "sravni.sqlite3"
    connection = _make_db(database_path)
    connection.execute(
        "INSERT INTO hotel_observation(search_id, source, source_hotel_id, name, raw_json) "
        "VALUES ('s1', 'sravniotel', '7', 'Hotel 7', ?)",
        (json.dumps({"canonical_url": "/russia/cities/x/hotel-7"}),),
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        "aviasales_collector.hotels.sravni_detail._fetch_hotel_api", lambda *args: None,
    )
    monkeypatch.setattr(
        "aviasales_collector.hotels.sravni_detail._fetch_page",
        lambda path: "<html>Заезд с 14:00 Выезд до 12:00 Wi-Fi Парковка</html>",
    )
    result = enrich_sravni_hotel_pages(str(database_path), "s1")
    assert result["enriched"] == 1

    persisted = lookup_sravni_policy(str(database_path), "s1", "7")
    assert persisted is not None
    assert persisted["check_in_text"] == "Заезд с 14:00"
    assert persisted["check_out_text"] == "Выезд до 12:00"
    assert "Wi-Fi" in persisted["amenities_text"]
    assert persisted["source"] == "sravniotel_hotel_page_html"

    # Idempotent: a second cycle has nothing pending.
    second = enrich_sravni_hotel_pages(str(database_path), "s1")
    assert second["pending"] == 0
    assert second["enriched"] == 0


def test_enrich_reports_failed_page_honestly(tmp_path, monkeypatch):
    database_path = tmp_path / "sravni.sqlite3"
    connection = _make_db(database_path)
    connection.execute(
        "INSERT INTO hotel_observation(search_id, source, source_hotel_id, name, raw_json) "
        "VALUES ('s1', 'sravniotel', '9', 'Hotel 9', ?)",
        (json.dumps({"canonical_url": "/russia/cities/x/hotel-9"}),),
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        "aviasales_collector.hotels.sravni_detail._fetch_page", lambda path: None,
    )
    result = enrich_sravni_hotel_pages(str(database_path), "s1")
    assert result["enriched"] == 0
    assert result["failed"] == 1
    assert lookup_sravni_policy(str(database_path), "s1", "9") is None


def test_parse_sravni_hotel_api_extracts_structured_fields():
    payload = {
        "hotel": {
            "check_in_time": "14:00:00",
            "check_out_time": "12:00:00",
            "amenity_groups": [
                {"name": "Самые популярные удобства", "amenities": [
                    {"name": "Бесплатный Wi-Fi"},
                    {"name": "Круглосуточная стойка регистрации"},
                ]},
                {"name": "Интернет", "amenities": [
                    {"name": "Бесплатный Wi-Fi"},  # дубль
                    {"name": "Wi-Fi в номере"},
                ]},
            ],
            "description": [{"title": "Расположение", "paragraphs": ["Отель в центре."]}],
            "policies": [{"title": "Питание", "paragraphs": ["Завтрак 830 ₽."]}],
        }
    }
    facts = parse_sravni_hotel_api(payload)
    assert facts["check_in_text"] == "Заезд с 14:00"
    assert facts["check_out_text"] == "Выезд до 12:00"
    assert facts["amenities_text"] == ["Бесплатный Wi-Fi", "Круглосуточная стойка регистрации", "Wi-Fi в номере"]
    assert facts["source"] == "sravniotel_hotel_api"
    assert "Отель в центре." in facts["description_text"]
    assert facts["policy_text"]


def test_parse_sravni_hotel_api_empty_hotel():
    facts = parse_sravni_hotel_api({"hotel": {}})
    assert facts["check_in_text"] is None
    assert facts["check_out_text"] is None
    assert facts["amenities_text"] == []
