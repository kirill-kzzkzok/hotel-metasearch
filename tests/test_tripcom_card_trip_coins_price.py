"""Trip.com card offers must ignore Trip Coins bonus amounts.

Live Cusco/Dubai sessions 2026-10-15: result cards show a loyalty bonus line
('Получите 179,22 ₽ в виде Trip Coins' / 'Get 20 Trip Coins') AFTER the real
price lines. The card parser takes the LAST 'N ₽' in the text as the offer
price, so the bonus became the offer and cards displayed 10-179 RUB
'prices' (user-visible defect). Both card parsers
(_tripcom_card_to_raw and _tripcom_card_snapshot_to_raw) now strip the
Trip Coins phrases from the text before the money regex runs.
"""
from aviasales_collector.hotels.sources.tripcom import (
    _tripcom_card_snapshot_to_raw,
)


def _snapshot(text):
    return {
        "id": "3910170", "text": text, "name": "Inkas Garden Apartment",
        "links": [], "labels": [], "images": [],
    }


def _offer_values(raw):
    return [(offer.get("price") or {}).get("value") for offer in (raw.get("offers") or [])]


def test_trip_coins_ru_phrase_is_not_the_price():
    raw = _tripcom_card_snapshot_to_raw(_snapshot(
        "Inkas Garden Apartment\nПолучите 10 ₽ в виде Trip Coins\n1 000 ₽\nИтого: 3 000 ₽"))
    assert _offer_values(raw) == [300000]


def test_trip_coins_in_single_line_text():
    raw = _tripcom_card_snapshot_to_raw(_snapshot(
        "Inkas Garden Apartment | Получите 10 ₽ в виде Trip Coins | 1 000 ₽ | Итого: 3 000 ₽"))
    assert _offer_values(raw) == [300000]


def test_trip_coins_english_phrase_is_not_the_price():
    raw = _tripcom_card_snapshot_to_raw(_snapshot("Hotel X\nGet 20 Trip Coins\n1 500 ₽"))
    assert _offer_values(raw) == [150000]


def test_card_without_coins_keeps_the_total():
    raw = _tripcom_card_snapshot_to_raw(_snapshot(
        "Inkas Garden Apartment\n2 985 ₽\nИтого: 8 955 ₽"))
    assert _offer_values(raw) == [895500]
