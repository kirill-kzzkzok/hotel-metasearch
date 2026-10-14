"""Test isolation: clear cross-test geo caches that leak state between tests.

The failing test test_beach_search_uses_city_and_country_slugs_and_rejects_beach_resorts
(expected 2 photon requests) intermittently got only 1: PHOTON_CITY_EXTENTS is a
module-global dict, so any earlier test that resolved 'da nang / vietnam' left a
warm entry, removed the first expected call, and IndexError'd calls[1]. The same
applies to module-global Overpass cooldown/pacing state left by transport tests.
These globals are caches of EXTERNAL state, not production data: clearing them
per-test makes each test see the same cold-start world.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_geo_process_caches():
    from aviasales_collector.hotels.geo import geometry, providers

    saved_extents = dict(providers.PHOTON_CITY_EXTENTS)
    providers.PHOTON_CITY_EXTENTS.clear()
    saved_cooldowns = dict(geometry._OVERPASS_ENDPOINT_COOLDOWNS)
    geometry._OVERPASS_ENDPOINT_COOLDOWNS.clear()
    saved_streaks = dict(geometry._OVERPASS_ENDPOINT_FAILURE_STREAKS)
    geometry._OVERPASS_ENDPOINT_FAILURE_STREAKS.clear()
    saved_next_start = geometry._OVERPASS_NEXT_START_AT
    saved_cooldown_until = geometry._OVERPASS_COOLDOWN_UNTIL
    geometry._OVERPASS_NEXT_START_AT = 0.0
    geometry._OVERPASS_COOLDOWN_UNTIL = 0.0
    yield
    providers.PHOTON_CITY_EXTENTS.update(saved_extents)
    geometry._OVERPASS_ENDPOINT_COOLDOWNS.update(saved_cooldowns)
    geometry._OVERPASS_ENDPOINT_FAILURE_STREAKS.update(saved_streaks)
    geometry._OVERPASS_NEXT_START_AT = saved_next_start
    geometry._OVERPASS_COOLDOWN_UNTIL = saved_cooldown_until
