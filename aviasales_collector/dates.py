from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta


MONTHS = {
    "янв": 1, "января": 1,
    "фев": 2, "февраля": 2,
    "мар": 3, "марта": 3,
    "апр": 4, "апреля": 4,
    "май": 5, "мая": 5,
    "июн": 6, "июня": 6,
    "июл": 7, "июля": 7,
    "авг": 8, "августа": 8,
    "сен": 9, "сент": 9, "сентября": 9,
    "окт": 10, "октября": 10,
    "ноя": 11, "нояб": 11, "ноября": 11,
    "дек": 12, "декабря": 12,
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+([A-Za-zА-Яа-яЁё]+)", re.IGNORECASE)
TRIP_DAYS_RE = re.compile(r"на\s+(\d+)\s+д(?:ень|ня|ней)", re.IGNORECASE)


@dataclass(frozen=True)
class DateNormalization:
    value: str | None
    raw: str | None
    warning: str | None = None


def parse_day_month(raw: str) -> tuple[int, int] | None:
    match = DATE_RE.search(raw.replace("\u00a0", " ").replace("\u202f", " "))
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower().rstrip("."))
    day = int(match.group(1))
    if month is None:
        return None
    return day, month


def normalize_departure_date(raw: str | None, today: date) -> DateNormalization:
    if not raw:
        return DateNormalization(None, raw, "departure date is not present in the card")
    parsed = parse_day_month(raw)
    if parsed is None:
        return DateNormalization(None, raw, f"unrecognized departure date: {raw!r}")

    day, month = parsed
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return DateNormalization(None, raw, f"invalid departure day/month: {raw!r}")

    if candidate < today:
        if (today - candidate).days > 30:
            next_occurrence = date(today.year + 1, month, day)
            return DateNormalization(
                next_occurrence.isoformat(),
                raw,
                f"{raw!r} has passed in the current year; assigned next year's occurrence",
            )
        return DateNormalization(
            None,
            raw,
            f"departure date {raw!r} is before today; year is ambiguous without an explicit year",
        )
    return DateNormalization(candidate.isoformat(), raw)


def normalize_return_date(
    raw: str | None,
    departure_iso: str | None,
    today: date,
    trip_duration_days: int | None = None,
) -> DateNormalization:
    if not raw:
        return DateNormalization(None, raw, "return date is not present in the card")
    parsed = parse_day_month(raw)
    if parsed is None:
        return DateNormalization(None, raw, f"unrecognized return date: {raw!r}")
    if departure_iso is None:
        return DateNormalization(None, raw, "return year cannot be inferred without a normalized departure date")

    departure = date.fromisoformat(departure_iso)
    day, month = parsed
    if trip_duration_days is not None:
        expected = departure + timedelta(days=trip_duration_days)
        if (expected.day, expected.month) == (day, month):
            if expected < today:
                return DateNormalization(None, raw, f"return date {raw!r} is before today")
            return DateNormalization(expected.isoformat(), raw)
        return DateNormalization(
            None,
            raw,
            f"visible trip duration ({trip_duration_days} days) does not match return date {raw!r}",
        )

    candidates: list[date] = []
    for year in {departure.year, departure.year + 1}:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if departure <= candidate <= departure + timedelta(days=365):
            candidates.append(candidate)

    if len(candidates) != 1:
        return DateNormalization(None, raw, f"return year is ambiguous for {raw!r}")
    result = candidates[0]
    if result < today:
        return DateNormalization(None, raw, f"return date {raw!r} is before today; year is ambiguous")
    return DateNormalization(result.isoformat(), raw)


def parse_trip_duration_days(text: str) -> int | None:
    match = TRIP_DAYS_RE.search(text.replace("\u00a0", " ").replace("\u202f", " "))
    return int(match.group(1)) if match else None
