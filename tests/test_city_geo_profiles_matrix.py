"""Curated city geo profiles cover the mission §6 city matrix.

The 2026-10-15 Paris live smoke rendered «🏖 — до пляжа» in an inland city:
`city_geo_profiles.json` had only 8 curated cities, Paris was unprofiled, so
`beach_relevant` stayed null and the JS `!== false` gate let the line through.
Two fixes exist: the JS gate now requires explicit beach_relevant === true,
and the curated profile file must cover every city of the §6 matrix so
beach/metro cities keep the line while inland cities hide it. This test
guards the data file: all matrix cities present, slugs match what the live
SravniOtel suggest endpoint returns, and no accidental typo keys (an
"is_bech_destination" typo silently degrades to False).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "aviasales_collector" / "data" / "city_geo_profiles.json"

# (city_slug, country_slug, is_beach_destination) — mission §6 matrix.
MATRIX = {
    ("barcelona", "spain", True),
    ("dubai", "united-arab-emirates", True),
    ("rio-de-janeiro", "brazil", True),
    ("london", "united-kingdom", False),
    ("paris", "france", False),
    ("tokyo", "japan", False),
    ("cancun", "mexico", True),
    ("antalya", "turkey", True),
    ("brugge", "belgium", False),
    ("suzdal", "russia", False),
    ("cusco", "peru", False),
}


def _load():
    data = json.loads(PROFILES.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, "profiles file must be a non-empty list"
    return data


def test_matrix_cities_are_profiled():
    profiles = _load()
    indexed = {(p.get("city_slug"), p.get("country_slug")): p for p in profiles}
    missing = [key for key in MATRIX if key[:2] not in indexed]
    assert not missing, f"§6 matrix cities without a geo profile: {missing}"


def test_matrix_flags_match_expected():
    profiles = _load()
    indexed = {(p.get("city_slug"), p.get("country_slug")): p for p in profiles}
    for city_slug, country_slug, expected in MATRIX:
        profile = indexed[(city_slug, country_slug)]
        assert profile.get("is_beach_destination") is expected, (
            f"{city_slug}: is_beach_destination must be {expected}")


def test_profile_records_are_wellformed():
    for profile in _load():
        slug = str(profile.get("city_slug") or "")
        country = str(profile.get("country_slug") or "")
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), f"bad city_slug: {slug!r}"
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", country), f"bad country_slug: {country!r}"
        # A typo like "is_bech_destination" would silently evaluate to False
        # (missing key) and hide the beach line for a real beach city.
        assert set(profile) == {"city_slug", "country_slug", "is_beach_destination", "evidence"}, (
            f"unexpected keys in {slug}: {sorted(profile)}")
        assert isinstance(profile.get("is_beach_destination"), bool)
        assert str(profile.get("evidence") or "").strip()


def test_unique_profile_pairs():
    pairs = [(p.get("city_slug"), p.get("country_slug")) for p in _load()]
    assert len(pairs) == len(set(pairs)), "duplicate (city_slug, country_slug) entries"


def test_paris_profile_marks_city_inland():
    """Regression: the Paris live smoke showed «до пляжа» for an unprofiled
    inland city; Paris must now be explicitly non-beach."""
    profiles = _load()
    paris = next(p for p in profiles if p["city_slug"] == "paris")
    assert paris["is_beach_destination"] is False
