"""Hotel search source adapter interface."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import SourceResult


class HotelSource(Protocol):
    source: str

    async def search(self, query: dict[str, Any], page_token: str | None = None) -> SourceResult: ...
