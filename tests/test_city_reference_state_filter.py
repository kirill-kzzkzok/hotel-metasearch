"""City reference resolution: state boundary points must not become centers.

2026-10-15 Cusco live defect: Nominatim ranks the Cusco STATE boundary
(addresstype=state, -12.5/-72.5) above the city place for some queries. The
pre-fix Nominatim fallback had no addresstype filter (Photon candidates were
already constrained to osm_value city/town/municipality), so the state point
was persisted as the city center - every hotel showed ~125-130 km to the
center while the true median was 1.9 km.

Two layers fix this:
1. providers.py: the Nominatim fallback only accepts place-level candidates
   (city/town/municipality/village/borough/suburb) - parity with Photon.
2. city.py: cached nominatim_place_point rows that look like coarse area
   centroids (half-degree rounded coordinates) are treated as stale and
   re-resolved instead of being served forever.
"""
import json

import pytest

from aviasales_collector.hotels.geo.city import _read_city_reference
from aviasales_collector.hotel_search import init_hotel_store
from aviasales_collector.hotels.schema import _profiled_sqlite_connect


def _row(connection, lat, lon, method="nominatim_place_point"):
    connection.execute(
        """INSERT OR REPLACE INTO hotel_city_reference
        (city_slug,country_slug,name,center_lat,center_lon,source,retrieved_at,method)
        VALUES ('cusco','peru','Cusco',?,?, 'nominatim_city_place', '2026-10-15T00:00:00+00:00', ?)""",
        (lat, lon, method),
    )
    connection.commit()


def test_cached_state_centroid_is_treated_as_stale(tmp_path):
    db = tmp_path / "geo.sqlite3"
    init_hotel_store(db)
    with _profiled_sqlite_connect(str(db), timeout=10) as connection:
        _row(connection, -12.5, -72.5)
        assert _read_city_reference(
            connection, {"city_slug": "cusco", "country_slug": "peru"}) is None


def test_cached_real_city_point_is_served(tmp_path):
    db = tmp_path / "geo.sqlite3"
    init_hotel_store(db)
    with _profiled_sqlite_connect(str(db), timeout=10) as connection:
        _row(connection, -13.5170887, -71.9785356)
        reference = _read_city_reference(
            connection, {"city_slug": "cusco", "country_slug": "peru"})
    assert reference is not None
    assert reference.center_lat == pytest.approx(-13.5170887)
    assert reference.center_lon == pytest.approx(-71.9785356)


def test_rounded_but_place_points_from_other_methods_are_kept(tmp_path):
    """A photon-sourced point is never the state-centroid artifact; the stale
    guard must only affect the nominatim fallback rows it was introduced for."""
    db = tmp_path / "geo.sqlite3"
    init_hotel_store(db)
    with _profiled_sqlite_connect(str(db), timeout=10) as connection:
        _row(connection, -12.5, -72.5, method="photon_place_point")
        reference = _read_city_reference(
            connection, {"city_slug": "cusco", "country_slug": "peru"})
    assert reference is not None
