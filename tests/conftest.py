"""Test isolation: restore geo process-global state exactly around each test.

Root cause of the long-flaky beach test (IndexError at test_hotel_search.py:2292,
~90% failure within the file): module-global geo caches leaked between tests in
one pytest process. PHOTON_CITY_EXTENTS warmed by an earlier test removed the
first expected photon request, and leftover Overpass cooldown/pacing state
(_OverpassRequestDeferred subclasses TimeoutError, silently swallowed) removed
the overpass request. Per-test clearing makes every test see a cold-start world.

Exact restoration (mission spec §16): the previous .update(saved) restore kept
entries a test added. Teardown now replaces the whole mapping content from a
pre-test snapshot, so test-added entries are removed, and restores scalar
pacing state under try/finally semantics. Production cache behavior itself is
covered by tests/test_geo_global_isolation.py.
"""
from __future__ import annotations

import pytest

_GLOBAL_DICTS = (
    "aviasales_collector.hotels.geo.providers:PHOTON_CITY_EXTENTS",
    "aviasales_collector.hotels.geo.providers:GEOCODE_PROPERTY_GENERIC",
    "aviasales_collector.hotels.geo.providers:GEOCODE_REQUEST_LOCKS",
    "aviasales_collector.hotels.geo.providers:PROVIDER_DETAIL_LOCKS",
    "aviasales_collector.hotels.geo.geometry:GEO_FEATURE_TILE_LOCKS",
    "aviasales_collector.hotels.geo.geometry:_OVERPASS_ENDPOINT_COOLDOWNS",
    "aviasales_collector.hotels.geo.geometry:_OVERPASS_ENDPOINT_FAILURE_STREAKS",
)
_GLOBAL_SCALARS = (
    "aviasales_collector.hotels.geo.geometry:_OVERPASS_NEXT_START_AT",
    "aviasales_collector.hotels.geo.geometry:_OVERPASS_COOLDOWN_UNTIL",
)


def _resolve(dotted: str):
    module_name, attr = dotted.rsplit(":", 1)
    import importlib

    module = importlib.import_module(module_name)
    return module, attr, getattr(module, attr)


@pytest.fixture(autouse=True)
def _restore_geo_process_globals():
    saved: dict[str, tuple] = {}
    for dotted in _GLOBAL_DICTS:
        try:
            module, attr, value = _resolve(dotted)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        saved[dotted] = (module, attr, value.copy())
        value.clear()
    for dotted in _GLOBAL_SCALARS:
        try:
            module, attr, value = _resolve(dotted)
        except Exception:
            continue
        saved[dotted] = (module, attr, value)
        setattr(module, attr, 0.0)
    try:
        yield
    finally:
        for module, attr, snapshot in saved.values():
            current = getattr(module, attr)
            if isinstance(current, dict):
                current.clear()
                current.update(snapshot)
            else:
                setattr(module, attr, snapshot)
