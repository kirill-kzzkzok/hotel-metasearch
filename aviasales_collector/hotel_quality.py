from __future__ import annotations

from typing import Any


MIN_QUALITY_REVIEWS = 3


def _rating_records(hotel: dict[str, Any]) -> list[dict[str, Any]]:
    records = hotel.get("source_records")
    return records if isinstance(records, list) else [hotel]


def quality_signal(hotel: dict[str, Any]) -> str | None:
    records = _rating_records(hotel)
    has_rating = lambda record: (
        isinstance(record.get("rating"), (int, float))
        and not isinstance(record.get("rating"), bool)
        and isinstance(record.get("rating_scale"), (int, float))
        and 0 < record["rating"] <= record["rating_scale"]
    )
    if not any(has_rating(record) for record in records):
        return "no_rating"
    if not any(
        has_rating(record)
        and isinstance(record.get("reviews_count"), int)
        and not isinstance(record.get("reviews_count"), bool)
        and record["reviews_count"] >= MIN_QUALITY_REVIEWS
        for record in records
    ):
        return "low_reviews"
    return None


def assess_hotel_quality(hotels: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept = []
    signals = {"no_rating": 0, "low_reviews": 0, "no_offer": 0}
    for hotel in hotels:
        if not hotel.get("offers"):
            signals["no_offer"] += 1
            continue
        reason = quality_signal(hotel)
        if reason is not None:
            signals[reason] += 1
        kept.append(hotel)
    return kept, signals
