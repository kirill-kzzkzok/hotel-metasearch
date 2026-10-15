"""Trip.com card rating: accept the live bare-score layout.

Live Cusco session 2026-10-15: 150/150 cards had reviews_count parsed but
0/150 had a rating - the redesigned card renders the score as a bare '9,5'
line next to the review count ('326 отзыва'), while the parser only
matched the legacy '9,5 / 10' shape. Cards showed 'Рейтинг —' until the
user noticed. Both card parsers now also accept a standalone 0-10 score
line adjacent to a review-count line, verified live: 144/150 cards carry
a rating after the fix (the rest genuinely have none).
"""
from aviasales_collector.hotels.sources.tripcom import (
    _tripcom_card_snapshot_to_raw,
)


def _snapshot(text):
    return {
        "id": "1", "text": text, "name": "Casa Cusco Guest House",
        "links": [], "labels": [], "images": [],
    }


def test_live_bare_score_next_to_reviews_is_the_rating():
    raw = _tripcom_card_snapshot_to_raw(_snapshot(
        "Casa Cusco Guest House\nРядом: Plaza de Armas\nЗамечательно\n326 отзыва\n9,5\n1 200 ₽\nИтого: 3 600 ₽"))
    assert raw["rating"] == 9.5
    assert raw["reviews_count"] == 326


def test_legacy_slash_ten_rating_still_works():
    raw = _tripcom_card_snapshot_to_raw(_snapshot(
        "Hotel Old\n9,5 / 10\n326 отзыва\n1 200 ₽"))
    assert raw["rating"] == 9.5


def test_scoreless_card_keeps_rating_none():
    raw = _tripcom_card_snapshot_to_raw(_snapshot("Hotel X\n2 985 ₽\nИтого: 8 955 ₽"))
    assert raw["rating"] is None


def test_reviews_without_score_do_not_become_a_rating():
    """'57 отзыва' alone must not be parsed as a score - only a standalone
    decimal line next to the review count qualifies."""
    raw = _tripcom_card_snapshot_to_raw(_snapshot(
        "Hotel Y\n57 отзыва\n1 200 ₽\nИтого: 3 600 ₽"))
    assert raw["rating"] is None
    assert raw["reviews_count"] == 57
