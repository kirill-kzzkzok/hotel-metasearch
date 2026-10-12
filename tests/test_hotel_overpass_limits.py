from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from aviasales_collector.hotels.geo import geometry


def test_public_overpass_requests_are_serialized(monkeypatch):
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_request(_url, _data, _headers, _stage, _max_bytes, _deadline):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return b"{}"

    monkeypatch.setattr(geometry, "_overpass_post_impl", fake_request)
    monkeypatch.setattr(geometry, "_overpass_reserve_request_start", lambda *_args: None)

    def request(index):
        return geometry._overpass_post(
            geometry.OVERPASS_PRIMARY_URL, b"data", {}, f"probe_{index}", 1024,
            deadline_seconds=1.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, range(2)))

    assert results == [b"{}", b"{}"]
    assert max_active == 1


def _reset_overpass_cooldowns(monkeypatch):
    monkeypatch.setattr(geometry, "_OVERPASS_ENDPOINT_COOLDOWNS", {})
    monkeypatch.setattr(geometry, "_OVERPASS_ENDPOINT_FAILURE_STREAKS", {})
    monkeypatch.setattr(geometry, "_OVERPASS_NEXT_START_AT", 0.0)
    monkeypatch.setattr(geometry, "_OVERPASS_COOLDOWN_UNTIL", 0.0)
    monkeypatch.setattr(geometry, "_OVERPASS_MIN_START_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(geometry, "_overpass_reserve_request_start", lambda *_args: None)


def test_504_falls_back_and_skips_primary_during_cooldown(monkeypatch):
    _reset_overpass_cooldowns(monkeypatch)
    calls = []

    def fake_request(url, _data, _headers, _stage, _max_bytes, _deadline):
        calls.append(url)
        if url == geometry.OVERPASS_PRIMARY_URL:
            request = httpx.Request("POST", url)
            response = httpx.Response(504, request=request)
            raise httpx.HTTPStatusError("dispatcher overloaded", request=request, response=response)
        return b"fallback payload"

    monkeypatch.setattr(geometry, "_overpass_post_impl", fake_request)

    result = geometry._overpass_post(
        geometry.OVERPASS_PRIMARY_URL, b"data", {}, "probe", 1024,
    )
    assert result == b"fallback payload"
    assert calls == [geometry.OVERPASS_PRIMARY_URL, geometry.OVERPASS_FALLBACK_URL]
    assert geometry._overpass_endpoint_cooldown_remaining(geometry.OVERPASS_PRIMARY_URL) > 0

    # A later tile request should use the healthy fallback directly rather
    # than re-sending a request to the instance that just returned HTTP 504.
    result = geometry._overpass_post(
        geometry.OVERPASS_PRIMARY_URL, b"data", {}, "probe_next_tile", 1024,
    )
    assert result == b"fallback payload"
    assert calls == [geometry.OVERPASS_PRIMARY_URL,
                     geometry.OVERPASS_FALLBACK_URL, geometry.OVERPASS_FALLBACK_URL]


def test_all_overpass_instances_cooling_down_fail_fast_without_retry(monkeypatch):
    _reset_overpass_cooldowns(monkeypatch)
    calls = []

    def overloaded(url, _data, _headers, _stage, _max_bytes, _deadline):
        calls.append(url)
        request = httpx.Request("POST", url)
        response = httpx.Response(504, request=request)
        raise httpx.HTTPStatusError("dispatcher overloaded", request=request, response=response)

    monkeypatch.setattr(geometry, "_overpass_post_impl", overloaded)

    with pytest.raises(httpx.HTTPStatusError):
        geometry._overpass_post(geometry.OVERPASS_PRIMARY_URL, b"data", {}, "probe", 1024)
    assert calls == [geometry.OVERPASS_PRIMARY_URL, geometry.OVERPASS_FALLBACK_URL]

    with pytest.raises(TimeoutError, match="cooling down"):
        geometry._overpass_post(geometry.OVERPASS_PRIMARY_URL, b"data", {}, "probe_next_tile", 1024)
    assert calls == [geometry.OVERPASS_PRIMARY_URL, geometry.OVERPASS_FALLBACK_URL]
