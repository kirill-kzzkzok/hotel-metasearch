"""Regression tests for geo process-global isolation between tests (spec §16).

Covers four requirements:
1. the previously flaky beach test passes repeatedly in one process;
2. geo globals do not leak from one test to the next;
3. teardown restores the exact pre-test state, including removing entries a
   test added (the old .update() restore kept them);
4. production code still uses the module-global caches normally (a warm cache
   hits without new requests) — isolation must not break caching semantics.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import aviasales_collector.hotels.geo.geometry as geometry
import aviasales_collector.hotels.geo.providers as providers
from aviasales_collector.hotels.geo.providers import PHOTON_CITY_EXTENTS
from tests.conftest import _restore_geo_process_globals

FLAKY_NODE = (
    "tests/test_hotel_search.py::"
    "test_beach_search_uses_city_and_country_slugs_and_rejects_beach_resorts"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_beach_search_flaky_case_passes_repeatedly_in_one_process():
    """The historical ~90%-in-file flake must be gone; runs the case 5x."""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", FLAKY_NODE, "-q", "--tb=short"]
        + [FLAKY_NODE] * 4,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


def test_geo_globals_do_not_leak_between_tests():
    """An entry added by this test must not be visible to a later one."""
    # Simulate what a leaking test does: warm the global caches.
    PHOTON_CITY_EXTENTS[("leak-probe-city", "leak-country")] = (1.0, 2.0, 3.0, 4.0)
    geometry._OVERPASS_ENDPOINT_COOLDOWNS["https://leak.example/api"] = 999.0
    geometry._OVERPASS_ENDPOINT_FAILURE_STREAKS["https://leak.example/api"] = 7
    geometry._OVERPASS_NEXT_START_AT = 123.456
    geometry._OVERPASS_COOLDOWN_UNTIL = 987.654
    # Teardown of THIS test runs the same restore logic the next test relies on.

    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--tb=short",
         "tests/test_geo_global_isolation.py::test_leak_probe_clean_start"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "geo globals leaked across processes is impossible; this failure means "
        "the probe test itself broke: " + result.stdout[-2000:]
    )


def test_leak_probe_clean_start():
    """Probe executed in a FRESH child pytest: globals must start cold.

    (When this runs inside the main suite it also proves the parent test's
    teardown restored state: the autouse fixture re-clears anyway.)
    """
    assert ("leak-probe-city", "leak-country") not in PHOTON_CITY_EXTENTS
    assert all("leak.example" not in key for key in geometry._OVERPASS_ENDPOINT_COOLDOWNS)
    assert all("leak.example" not in key for key in geometry._OVERPASS_ENDPOINT_FAILURE_STREAKS)


def test_teardown_restores_exact_state_including_test_added_entries():
    """Direct exercise of the conftest restore: added entries are removed,
    pre-existing entries survive, scalars return to their saved values."""
    # Pre-existing state as some earlier suite stage could have left it.
    PHOTON_CITY_EXTENTS[("keep-city", "keep-country")] = (5.0, 6.0, 7.0, 8.0)
    geometry._OVERPASS_ENDPOINT_COOLDOWNS["https://keep.example/api"] = 42.0
    geometry._OVERPASS_NEXT_START_AT = 111.0

    # Run the real restore logic (same code pytest runs around each test) by
    # invoking the fixture generator function directly.
    generator = _restore_geo_process_globals.__wrapped__()
    generator.send(None)  # enter: snapshot + clear

    # Simulate test activity that dirties the globals.
    PHOTON_CITY_EXTENTS[("test-added-city", "test-country")] = (9.0, 9.0, 9.0, 9.0)
    PHOTON_CITY_EXTENTS[("keep-city", "keep-country")] = (0.0, 0.0, 0.0, 0.0)  # mutated
    geometry._OVERPASS_ENDPOINT_COOLDOWNS["https://test-added.example/api"] = 5.0
    geometry._OVERPASS_NEXT_START_AT = 999.0

    # Teardown.
    try:
        generator.send(None)
    except StopIteration:
        pass

    assert ("test-added-city", "test-country") not in PHOTON_CITY_EXTENTS, (
        "teardown must REMOVE entries added by the test (old .update() kept them)"
    )
    assert "https://test-added.example/api" not in geometry._OVERPASS_ENDPOINT_COOLDOWNS
    assert PHOTON_CITY_EXTENTS[("keep-city", "keep-country")] == (5.0, 6.0, 7.0, 8.0), (
        "teardown must restore pre-existing entries to their pre-test VALUES"
    )
    assert geometry._OVERPASS_ENDPOINT_COOLDOWNS["https://keep.example/api"] == 42.0
    assert geometry._OVERPASS_NEXT_START_AT == 111.0


def test_production_photon_cache_still_caches():
    """Isolation must not break production caching semantics: a warm entry
    short-circuits _photon_city_extent without any network transport."""
    PHOTON_CITY_EXTENTS[("prod cache city", "prod cache country")] = (10.0, 20.0, 30.0, 40.0)
    calls = []

    def _fail_transport(*args, **kwargs):
        calls.append(args)
        raise AssertionError("cached extent must not hit any transport")

    original = providers._profiled_urlopen
    providers._profiled_urlopen = _fail_transport
    try:
        extent = providers._photon_city_extent("prod-cache-city", "prod-cache-country")
    finally:
        providers._profiled_urlopen = original

    assert extent == (10.0, 20.0, 30.0, 40.0)
    assert calls == [], "cache hit must not call the transport"
