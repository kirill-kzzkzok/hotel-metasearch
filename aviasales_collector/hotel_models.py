"""Small public data contracts for the provider/canonical hotel pipeline.

The collector still uses dictionaries at its boundaries for backwards
compatibility with the API, but these contracts document the three distinct
entities and make conversion/validation available to callers and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class Offer:
    canonical_hotel_id: str | None = None
    provider: str | None = None
    provider_hotel_id: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    adults: int | None = None
    children_count: int | None = None
    rooms: int | None = None
    room_type: str | None = None
    bed_type: str | None = None
    meal_plan: str = "unknown"
    meal_included: bool | None = None
    meal_available: bool | None = None
    meal_source: str | None = None
    meal_confidence: str = "none"
    meal_raw_text: str | None = None
    cancellation: str = "unknown"
    payment_method: str = "unknown"
    amount: Decimal | str | None = None
    currency: str | None = None
    taxes: Any = None
    fees: Any = None
    tax_status: str = "unknown"
    deeplink: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderHotel:
    provider: str
    provider_hotel_id: str
    name: str
    address: str | None = None
    city: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    stars: float | None = None
    rating: float | None = None
    reviews_count: int | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    deeplink: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalHotel:
    canonical_id: str
    name: str
    normalized_name: str
    country: str | None = None
    city: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    stars: float | None = None
    canonical_rating: float | None = None
    canonical_review_count: int | None = None
    offers: list[Offer] = field(default_factory=list)
