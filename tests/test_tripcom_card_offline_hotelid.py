import json
from pathlib import Path

import pytest

from aviasales_collector.hotels.sources.tripcom import _tripcom_card_snapshot_to_raw


def _snapshot(**overrides):
    base = {
        "id": None,
        "text": ("Sample Hotel | Рядом: City CenterКарта | Отлично | 100 отзывов | 9,0 | "
                 "Стандартный номер | 2 односпальные кровати | 12 500 ₽"),
        "name": "Sample Hotel",
        "links": [],
        "labels": [],
        "images": [],
    }
    base.update(overrides)
    return base


def test_card_without_links_uses_data_offline_hotelid():
    """2026-10-15 Dubai live session: the A/B variant renders .list-item cards
    with no <a href> at all; the only provider id is data-offline-hotelid on
    an inner div. The snapshot id already carries it (capture script reads
    card.id || [data-offline-hotelid]); parsing must not drop these cards.
    """
    snapshot = _snapshot(id="135657734")
    raw = _tripcom_card_snapshot_to_raw(snapshot)
    assert raw is not None
    assert raw["id"] == "135657734"
    assert raw["name"] == "Sample Hotel"


def test_card_without_id_or_links_is_still_dropped():
    snapshot = _snapshot(id=None)
    assert _tripcom_card_snapshot_to_raw(snapshot) is None


def test_card_with_hotelid_link_still_preferred():
    snapshot = _snapshot(
        id=None,
        links=[{"href": "https://ru.trip.com/hotel_detail?hotelId=987654&checkIn=2026-11-14",
                "text": "Sample Hotel"}],
    )
    raw = _tripcom_card_snapshot_to_raw(snapshot)
    assert raw is not None
    assert raw["id"] == "987654"
