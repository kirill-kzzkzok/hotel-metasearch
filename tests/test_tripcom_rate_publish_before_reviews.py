"""B13 regression: a successful rate is published before optional reviews.

Review enrichment used to run inside the completion loop BEFORE the
on_result callback (up to a 20s comment wait per hotel), so genuine rates
stayed invisible behind a best-effort review request, and the measured
latency excluded the actual fetch because `started` was reset. Now the
rate is terminal and persisted immediately; reviews arrive as a second
best-effort update and can never delay or hide the rate.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.sources.tripcom_rate_fetch import (
    collect_tripcom_room_rates_via_fetch,
)


class _StubPage:
    """Minimal page double: rate fetch answers ok, comment hangs forever."""

    def __init__(self) -> None:
        self.gotos: list[str] = []

    def on(self, event: str, handler: Any) -> None:
        # The template capture listens for rate-endpoint requests; the stub
        # never navigates to a detail page, so no request is emitted and the
        # capture branch is bypassed by returning the template below.
        pass

    async def goto(self, url: str, **_kw: Any) -> None:
        self.gotos.append(url)

    async def wait_for_timeout(self, _ms: float) -> None:
        pass

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "getHotelCommentInfo" in script:
            # The optional review request stalls (bounded by its own timeout).
            await asyncio.sleep(60)
        if "fetch(" not in script or "getHotelCommentInfo" in script:
            # Template installation (tpl => {...}) returns undefined.
            return None
        # The in-page rate fetch: HTTP 200 with a minimal valid inventory.
        return {
            "status": 200,
            "spider": None,
            "payloadJson": (
                '{"data": {"saleRoomMap": {}, "roomList": []}}'
            ),
        }


class _StubContext:
    def __init__(self) -> None:
        self.pages: list[_StubPage] = [_StubPage()]
        self.closed = False

    async def new_page(self) -> _StubPage:
        page = _StubPage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _StubBrowser:
    def __init__(self) -> None:
        self.context = _StubContext()

    async def new_context(self, **_kw: Any) -> _StubContext:
        return self.context


@pytest.fixture
def capture_env(monkeypatch, tmp_path):
    """Isolated store + a browser factory that never opens real Chromium."""
    init_hotel_store(tmp_path / "b13.sqlite3")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "b13_active")
    created = {"browser": _StubBrowser()}

    async def _launch(**_kw: Any) -> _StubBrowser:
        return created["browser"]

    import aviasales_collector.hotels.sources.tripcom_rate_fetch as trf

    class _StubPlaywright:
        def __init__(self, browser):
            self._browser = browser

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def chromium(self):
            return self

        async def launch_persistent_context(self, *_a, **_kw):
            return self._browser.context

    stub_pw = _StubPlaywright(created["browser"])

    class _StubAsyncPlaywrightCM:
        async def __aenter__(self):
            return stub_pw

        async def __aexit__(self, *args):
            return False

    import playwright.async_api as _pw_api
    monkeypatch.setattr(_pw_api, "async_playwright", lambda: _StubAsyncPlaywrightCM())

    async def _stub_capture(page, hotel):
        # A valid captured template body (the site's own request shape).
        return {"search": {"hotelId": 1, "checkIn": "20261120", "checkOut": "20261123",
                           "adult": 2}, "head": {"platform": "PC"}}
    import aviasales_collector.hotels.sources.tripcom_rate_fetch as trf
    monkeypatch.setattr(trf, "_capture_rate_template", _stub_capture)
    return created


_HOTELS = [
    {"source_hotel_id": "9001", "city_id": "1356",
     "check_in": "2026-11-20", "check_out": "2026-11-23", "adults": 2},
]


@pytest.mark.asyncio
async def test_rate_published_before_stalled_reviews(tmp_path, capture_env):
    published: list[dict[str, Any]] = []

    async def on_result(index: int, outcome: dict[str, Any]) -> None:
        published.append(dict(outcome))

    results = await asyncio.wait_for(
        collect_tripcom_room_rates_via_fetch(
            _HOTELS, tmp_path / "profile-dir", concurrency=1, on_result=on_result,
        ),
        timeout=15,
    )
    # The rate must have been published even though the comment request
    # stalled for 60s inside the stub (bounded to 0.2s by the patched
    # timeout): the first callback for the hotel arrives without waiting.
    assert published, "rate was never published"
    first = published[0]
    assert first["source_hotel_id"] == "9001"
    assert first["status"] in {"ok", "no_rates"}
    # And the overall collector finished despite the stalled review request.
    assert len(results) == 1
    assert results[0]["status"] in {"ok", "no_rates"}
