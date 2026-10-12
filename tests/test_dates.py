from datetime import date

from aviasales_collector.dates import (
    normalize_departure_date,
    normalize_return_date,
    parse_day_month,
    parse_trip_duration_days,
)


def test_parse_russian_date_and_duration():
    assert parse_day_month("23\u00a0сен, ср") == (23, 9)
    assert parse_trip_duration_days("Анталья\nна 8 дней") == 8


def test_departure_date_uses_current_year_for_future_date():
    result = normalize_departure_date("5 окт, пн", date(2026, 9, 22))
    assert result.value == "2026-10-05"
    assert result.warning is None


def test_departure_date_before_today_is_not_guessed():
    result = normalize_departure_date("21 сен, пн", date(2026, 9, 22))
    assert result.value is None
    assert result.raw == "21 сен, пн"
    assert "ambiguous" in result.warning


def test_departure_date_after_new_year_is_assigned_with_warning():
    result = normalize_departure_date("3 янв, вс", date(2026, 12, 28))
    assert result.value == "2027-01-03"
    assert result.raw == "3 янв, вс"
    assert "next year's occurrence" in result.warning


def test_return_date_rolls_into_next_year_only_relative_to_departure():
    result = normalize_return_date("3 янв, вс", "2026-12-28", date(2026, 9, 22), 6)
    assert result.value == "2027-01-03"
    assert result.warning is None


def test_trip_duration_resolves_return_date_near_year_boundary():
    result = normalize_return_date("24 сен", "2026-09-23", date(2026, 9, 22), 1)
    assert result.value == "2026-09-24"
    assert result.warning is None


def test_return_date_duration_mismatch_is_not_guessed():
    result = normalize_return_date("25 сен", "2026-09-23", date(2026, 9, 22), 1)
    assert result.value is None
    assert "does not match" in result.warning


def test_return_date_stays_null_when_departure_is_ambiguous():
    result = normalize_return_date("3 янв, вс", None, date(2026, 12, 28))
    assert result.value is None
    assert "without a normalized departure" in result.warning
