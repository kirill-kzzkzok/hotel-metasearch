from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse
from .flight_routes import flight_identity

from .dates import (
    MONTHS,
    DateNormalization,
    normalize_departure_date,
    normalize_return_date,
    parse_day_month,
    parse_trip_duration_days,
)


PRICE_RE = re.compile(r"([\d\s\u00a0\u202f.,]+)")
STOPS_RE = re.compile(r"(\d+)\s+пересад", re.IGNORECASE)
DATE_TEXT_RE = re.compile(r"(?<!\d)\d{1,2}\s+[A-Za-zА-Яа-яЁё]+", re.IGNORECASE)
DAY_ONLY_RE = re.compile(r"^\d{1,2}$")
MONTH_DAY_RE = re.compile(r"^([A-Za-zА-Яа-яЁё]+)\s+(\d{1,2})$")
AIRPORT_LINE_RE = re.compile(r"^([A-Z]{3})\s+(.+)$")
AIRPORT_REVERSE_LINE_RE = re.compile(r"^(.+?)\s*[\[(]?([A-Z]{3})[\])]?$")
TRANSFER_LINE_RE = re.compile(
    r"^\s*(?P<hours>\d+(?:[.,]\d+)?)\s*ч(?:\s*(?P<minutes>\d+)\s*мин)?\s+(?P<city>.+?)\s*$",
    re.IGNORECASE,
)
CLOCK_TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
NEXT_DAY_RE = re.compile(r"[\s\u200a-\u200f\u2060\ufeff]*\+\s*(?P<days>\d{1,2})\b")


def _arrival_day_offset(
    card_text: str,
    arrival_match: re.Match[str],
    next_time_start: int | None,
    base_date: str | None,
) -> int | None:
    marker = NEXT_DAY_RE.match(card_text[arrival_match.end():arrival_match.end() + 24])
    if marker:
        return int(marker.group("days"))
    if not base_date:
        return None

    tail_end = next_time_start if next_time_start is not None else arrival_match.end() + 80
    explicit_date = DATE_TEXT_RE.search(card_text[arrival_match.end():tail_end])
    parsed = parse_day_month(explicit_date.group(0)) if explicit_date else None
    if parsed is None:
        return None

    base = date.fromisoformat(base_date)
    day, month = parsed
    candidates = []
    for year in (base.year - 1, base.year, base.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    offset = (min(candidates, key=lambda candidate: abs((candidate - base).days)) - base).days
    return offset if 1 <= offset <= 2 else None


def parse_price(text: str) -> tuple[int | None, str | None]:
    currency_by_symbol = {
        "₽": "RUB",
        "₸": "KZT",
        "€": "EUR",
        "$": "USD",
        "£": "GBP",
        "₺": "TRY",
        "֏": "AMD",
        "₾": "GEL",
    }
    symbol = next((symbol for symbol in currency_by_symbol if symbol in text), None)
    match = PRICE_RE.search(text)
    if not match:
        return None, currency_by_symbol.get(symbol) if symbol else None
    digits = re.sub(r"\D", "", match.group(1))
    amount = int(digits) if digits else None
    return amount, currency_by_symbol.get(symbol) if symbol else None


def parse_stops(text: str) -> int | None:
    normalized = text.lower()
    if "прямой" in normalized or "без пересад" in normalized:
        return 0
    match = STOPS_RE.search(normalized)
    return int(match.group(1)) if match else None


def route_from_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    path = urlparse(source_url).path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


def _safe_ticket_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        trusted_host = (
            host == "aviasales.ru" or host.endswith(".aviasales.ru")
            or host == "aviasales.com" or host.endswith(".aviasales.com")
        )
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or not trusted_host):
            return None
    except ValueError:
        return None
    return value


def extract_date_texts(text_items: list[str]) -> list[str]:
    dates: list[str] = []
    normalized_items = [
        text.replace("\u00a0", " ").replace("\u202f", " ").strip()
        for text in text_items
    ]
    for index, text in enumerate(normalized_items):
        if DATE_TEXT_RE.search(text) and parse_day_month(text) is not None and text not in dates:
            dates.append(text)
            continue
        reverse = MONTH_DAY_RE.fullmatch(text)
        if reverse and reverse.group(1).lower().rstrip(".") in MONTHS:
            value = f"{reverse.group(2)} {reverse.group(1).lower().rstrip('.') }"
            if value not in dates:
                dates.append(value)
            continue
        month = text.lower().rstrip(".")
        if month in MONTHS and index + 1 < len(normalized_items) and DAY_ONLY_RE.fullmatch(normalized_items[index + 1]):
            value = f"{normalized_items[index + 1]} {month}"
            if value not in dates:
                dates.append(value)
    return dates


def extract_airports(card_text: str) -> list[dict[str, str]] | None:
    airports: list[dict[str, str]] = []
    lines = [
        re.sub(r"[\u200a-\u200f\u2060\ufeff]", "", line)
        .replace("\u00a0", " ").replace("\u202f", " ").strip(" \t·|—–-")
        for line in card_text.splitlines()
    ]
    lines = [line for line in lines if line]

    def is_airport_name(value: str) -> bool:
        value = value.strip(" \t·|—–-()[]")
        if not value or re.fullmatch(r"[A-Z]{3}", value):
            return False
        if CLOCK_TIME_RE.fullmatch(value) or DATE_TEXT_RE.search(value):
            return False
        lowered = value.casefold()
        if AIRPORT_LINE_RE.fullmatch(value) or re.match(r"\d+(?:[.,]\d+)?\s*(?:ч|час|мин|дн|день)\b", lowered):
            return False
        return not any(token in lowered for token in ("в пути", "пересад", "прямой", "туда", "обратно", "₽", "$", "€", "£"))

    for index, line in enumerate(lines):
        forward = AIRPORT_LINE_RE.fullmatch(line)
        if forward and is_airport_name(forward.group(2)):
            airports.append({"code": forward.group(1), "name": forward.group(2).strip(" \t·|—–-()[]")})
            continue
        reverse = AIRPORT_REVERSE_LINE_RE.fullmatch(line)
        if reverse and not re.fullmatch(r"[A-Z]{3}", reverse.group(1).strip()) and is_airport_name(reverse.group(1)):
            airports.append({"code": reverse.group(2).upper(), "name": reverse.group(1).strip(" \t·|—–-()[]")})
            continue
        if re.fullmatch(r"[A-Z]{3}", line):
            adjacent_name = next(
                (candidate for candidate in (lines[index - 1] if index else "", lines[index + 1] if index + 1 < len(lines) else "")
                 if is_airport_name(candidate)),
                None,
            )
            if adjacent_name:
                airports.append({"code": line, "name": adjacent_name.strip(" \t·|—–-()[]")})
    return airports or None


def extract_transfers(card_text: str) -> list[str] | None:
    """Extract rendered transfer city names such as ``4 ч Чэнду``."""
    transfers: list[str] = []
    for line in card_text.splitlines():
        normalized = line.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200a", "")
        normalized = normalized.replace("\u2060", "").strip()
        if "в пути" in normalized.lower() or "пересад" in normalized.lower():
            continue
        match = TRANSFER_LINE_RE.fullmatch(normalized)
        if not match:
            continue
        city = re.sub(r"\s+", " ", match.group("city")).strip(" -/—")
        if city:
            transfers.append(city)
    return transfers or None


def extract_transfer_durations(card_text: str) -> list[str] | None:
    durations: list[str] = []
    for line in card_text.splitlines():
        normalized = line.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200a", "")
        normalized = normalized.replace("\u2060", "").strip()
        if "в пути" in normalized.lower() or "пересад" in normalized.lower():
            continue
        match = TRANSFER_LINE_RE.fullmatch(normalized)
        if not match:
            continue
        city = re.sub(r"\s+", " ", match.group("city")).strip(" -/—")
        if not city:
            continue
        hours = match.group("hours").replace(",", ".")
        minutes = match.group("minutes")
        duration = f"{hours} ч" + (f" {minutes} мин" if minutes else "")
        durations.append(duration)
    return durations or None


def extract_repeat_registration_by_leg(card_text: str, leg_count: int) -> list[bool] | None:
    """Mark flight legs whose transfer text includes a repeat-registration notice."""
    if leg_count < 1 or not card_text:
        return None
    marks = [False] * leg_count
    current_leg = -1
    for raw_line in card_text.splitlines():
        line = re.sub(r"[\u200a-\u200f\u2060\ufeff]", "", raw_line)
        line = line.replace("\u00a0", " ").replace("\u202f", " ").strip().casefold()
        if "в пути" in line:
            current_leg = min(current_leg + 1, leg_count - 1)
        if "повторная регистрация" in line:
            marks[max(current_leg, 0)] = True
    return marks if any(marks) else None


def normalize_card_data(data: dict, origin: str, trip_type: str, today: date) -> dict:
    warnings: list[str] = []
    card_text = data.get("card_text", "")
    destination = (data.get("destination") or "").strip() or None
    if not destination:
        warnings.append("destination name selector missing")

    source_url = _safe_ticket_url(data.get("source_url"))
    if not source_url:
        warnings.append("ticket link missing")

    price_texts = data.get("price_texts", [])
    price, currency = parse_price(price_texts[0]) if price_texts else (None, None)
    old_price, old_currency = parse_price(price_texts[1]) if len(price_texts) > 1 else (None, None)
    if price is None:
        warnings.append("current price could not be parsed")
    if currency is None:
        warnings.append("currency could not be identified")
    if old_currency and currency and old_currency != currency:
        warnings.append("comparison price currency differs from current price currency")

    text_items = data.get("text_items", [])
    date_texts = extract_date_texts(data.get("date_items") or text_items)

    departure_raw = date_texts[0] if date_texts else None
    departure_result = normalize_departure_date(departure_raw, today)
    if departure_result.warning:
        warnings.append(departure_result.warning)

    return_raw = date_texts[1] if trip_type == "RT" and len(date_texts) > 1 else None
    duration_days = parse_trip_duration_days(card_text) if trip_type == "RT" else None
    if trip_type == "RT":
        return_result = normalize_return_date(return_raw, departure_result.value, today, duration_days)
        if return_result.warning:
            warnings.append(return_result.warning)
    else:
        return_result = DateNormalization(None, None)

    if trip_type == "RT" and duration_days is None:
        warnings.append("round-trip duration label is missing")

    flight_texts = [
        text.replace("\u00a0", " ").replace("\u202f", " ").strip()
        for text in text_items
        if "в пути" in text.lower()
    ]
    clean_flight_durations = [
        re.split(r"\s+в пути\b", re.sub(r"[\u200a-\u200d\u2060]", "", text), maxsplit=1, flags=re.IGNORECASE)[0].strip()
        for text in flight_texts
    ]
    flight_duration: str | list[str] | None
    if trip_type == "RT":
        flight_duration = clean_flight_durations or None
    else:
        flight_duration = clean_flight_durations[0] if clean_flight_durations else None

    stops_per_leg = [parse_stops(text) for text in flight_texts]
    if not stops_per_leg or all(value is None for value in stops_per_leg):
        card_stop = parse_stops(card_text)
        if card_stop is not None:
            stops_per_leg = [card_stop]
    stops: int | list[int | None] | None
    if trip_type == "RT":
        stops = stops_per_leg or None
    else:
        stops = stops_per_leg[0] if stops_per_leg else None

    airlines: list[str] = []
    airline_by_leg = []
    for leg_airlines in data.get("airline_by_leg", []):
        if not isinstance(leg_airlines, list):
            airline_by_leg.append([])
            continue
        names = []
        for alt in leg_airlines:
            name = str(alt or "").strip()
            if name and name not in names:
                names.append(name)
            if name and name not in airlines:
                airlines.append(name)
        airline_by_leg.append(names)
    for alt in data.get("airlines", []):
        name = str(alt or "").strip()
        if name and name not in airlines:
            airlines.append(name)
    airline_logos = []
    for logo in data.get("airline_logos", []):
        if not isinstance(logo, dict):
            continue
        name = str(logo.get("name") or "").strip()
        source = str(logo.get("url") or "").strip()
        host = (urlparse(source).hostname or "").lower()
        trusted_host = host == "avs.io" or host.endswith(".avs.io") or host == "aviasales.com" or host.endswith(".aviasales.com")
        if name and name in airlines and source.startswith("https://") and trusted_host:
            item = {"name": name, "url": source}
            if item not in airline_logos:
                airline_logos.append(item)

    normalized_card_text = re.sub(r"[\u200a-\u200f\u2060\ufeff]", "", card_text)
    time_matches = list(CLOCK_TIME_RE.finditer(normalized_card_text))
    flight_times = [match.group(0) for match in time_matches]
    arrival_matches = time_matches[1::2]
    arrival_bases: list[str | None]
    if trip_type == "OW":
        arrival_bases = [departure_result.value] * len(arrival_matches)
    elif len(arrival_matches) == 2:
        arrival_bases = [departure_result.value, return_result.value]
    elif len(stops_per_leg) >= 2 and all(isinstance(value, int) for value in stops_per_leg[:2]):
        arrival_bases = [
            departure_result.value
        ] * (stops_per_leg[0] + 1) + [
            return_result.value
        ] * (stops_per_leg[1] + 1)
        arrival_bases = (arrival_bases + [None] * len(arrival_matches))[:len(arrival_matches)]
    else:
        arrival_bases = [departure_result.value] + [None] * max(0, len(arrival_matches) - 2)
        if len(arrival_matches) > 1:
            arrival_bases.append(return_result.value)
    arrival_day_offsets = []
    for arrival_index, time_index in enumerate(range(1, len(time_matches), 2)):
        next_time_start = (
            time_matches[time_index + 2].start()
            if time_index + 2 < len(time_matches)
            else None
        )
        arrival_day_offsets.append(
            _arrival_day_offset(
                normalized_card_text,
                time_matches[time_index],
                next_time_start,
                arrival_bases[arrival_index] if arrival_index < len(arrival_bases) else None,
            )
        )
    # Keep partially captured segment times; the UI can still show the leg endpoints.
    if len(flight_times) < 2:
        flight_times = []
        arrival_day_offsets = []

    offer = {
        "origin": origin,
        "destination": destination,
        "destination_route": route_from_url(source_url),
        "price": price,
        "old_price": old_price,
        "currency": currency,
        "departure_date": departure_result.value,
        "departure_date_raw": departure_result.raw,
        "return_date": return_result.value,
        "return_date_raw": return_result.raw,
        "trip_duration_days": duration_days,
        "flight_duration": flight_duration,
        "flight_times": flight_times or None,
        "arrival_day_offsets": arrival_day_offsets or None,
        "airline": airlines or None,
        "airline_by_leg": airline_by_leg or None,
        "airline_logos": airline_logos or None,
        "airports": extract_airports(card_text),
        "transfers": extract_transfers(card_text),
        "transfer_durations": extract_transfer_durations(card_text),
        "repeat_registration_by_leg": extract_repeat_registration_by_leg(
            card_text, 2 if trip_type == "RT" else 1
        ),
        "stops": stops,
        "source_url": source_url,
        "warnings": warnings,
    }
    offer["route_identity"] = flight_identity(
        offer, trip_type, origin, data.get("requested_destination"), data.get("search_context")
    )
    return offer
