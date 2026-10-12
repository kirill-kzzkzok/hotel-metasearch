"""Hotel search source result and geography contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..hotel_models import CanonicalHotel, Offer, ProviderHotel


@dataclass
class CityReference:
    """Validated reference point used for calculated center distances.

    The reference is data, not control flow: callers can persist one per
    city/country using :func:`register_city_reference`.  If no reference is
    available, center distance remains ``None`` instead of using a hotel or a
    guessed city centroid.
    """

    city_slug: str
    country_slug: str
    name: str
    center_lat: float
    center_lon: float
    source: str
    retrieved_at: str
    method: str = "registered_reference"


@dataclass
class CityGeoProfile:
    city_slug: str
    country_slug: str
    is_beach_destination: bool
    source: str
    retrieved_at: str
    evidence: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourceError(Exception):
    def __init__(self, kind: str, message: str, *, http_status: int | None = None,
                 latency_ms: int | None = None, upstream_timeout: bool = False):
        self.kind = kind
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.upstream_timeout = upstream_timeout
        super().__init__(message)


class HotelSearchOperationError(Exception):
    """A search use-case error translated to an HTTP response by the API."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass
class SourceResult:
    source: str
    status: str
    hotels: tuple[dict[str, Any], ...] = ()
    raw_page: dict[str, Any] | None = None
    total_hotels: int | None = None
    next_page_token: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    error_class: str | None = None
    reason: str | None = None
    cache_state: str = "miss"
    observed_at: str | None = None
    reported_total: int | None = None
    collection_complete: bool | None = None
    collection_reason: str | None = None
    collection_audit: dict[str, Any] | None = None
