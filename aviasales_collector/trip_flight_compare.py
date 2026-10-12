"""Compare a saved Aviasales offer with a public Trip.com flight result.

This is a small flight action that deliberately reuses the existing Trip.com
Playwright executor, persistent context, and profile lock used by hotel search.
It never opens a booking or checkout page.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .flight_routes import city_code
from .hotels.sources.tripcom import (
    DEFAULT_TRIP_PROFILE,
    TRIP_EXECUTOR,
    TRIP_PROFILE_LOCK,
    _discard_tripcom_reusable_context,
    _tripcom_new_page_with_recovery,
    _tripcom_reusable_context,
)
from .hotels.sources.tripcom import log as tripcom_log


TRIP_FLIGHT_HOME = "https://ru.trip.com/flights/?locale=ru-RU&curr=RUB"
TRIP_SEARCH_BOX_SELECTOR = "#searchBox-content, .searchBox-content"
TRIP_CITY_SUGGESTIONS_SELECTOR = "#m-flight-poi-list, .m-flight-poi-list"
TRIP_RESULT_SELECTOR = "#J_resultList [aria-label^='Вылет рейса:']"
# Virtualized Trip.com cards can disappear while the list is refreshing.  A
# long per-card text timeout makes a ready sorted result look stalled for tens
# of seconds; a short bounded probe lets us skip that transient node and keep
# parsing the next visible fare.
TRIP_CARD_TEXT_TIMEOUT_MS = 1200
TRIP_EMPTY_RESULTS_PATTERN = re.compile(
    r"(?:Найдено\s+0\s+рейсов|Доступных\s+рейсов\s+не\s+найдено|"
    r"рейсов\s+не\s+найдено|рейсы\s+не\s+найдены|"
    r"no\s+(?:available\s+)?flights|0\s+flights)",
    re.IGNORECASE,
)
TRIP_ACCESS_LIMIT_PATTERN = re.compile(
    r"captcha|verify you are human|access challenge|доступ ограничен",
    re.IGNORECASE,
)
_CITY_INPUT_TEST_IDS = {
    "Аэропорт или город вылета": "search_city_from0",
    "Аэропорт или город прилета": "search_city_to0",
}
_RU_MONTHS = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_MONTH_INDEX = {name: index for index, name in enumerate(_RU_MONTHS) if name}


class TripFlightCompareError(RuntimeError):
    """Safe, user-displayable failure from a Trip.com comparison."""

    def __init__(self, reason: str, *, status_code: int = 503, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.retryable = retryable


def _iso_date(value: Any, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise TripFlightCompareError(f"Не удалось определить дату {field} билета.", status_code=422) from exc


def build_offer_context(offer: dict[str, Any]) -> dict[str, Any]:
    """Validate saved source-of-truth data and derive exact leg stop counts."""
    identity = offer.get("route_identity")
    if not isinstance(identity, dict) or identity.get("route_valid") is not True:
        raise TripFlightCompareError("Для этого билета не подтверждён маршрут.", status_code=422)
    trip_type = str(offer.get("trip_type") or "").upper()
    if trip_type not in {"OW", "RT"} or identity.get("trip_type") != trip_type:
        raise TripFlightCompareError("Не удалось подтвердить тип поездки.", status_code=422)

    origin = str(identity.get("requested_origin") or offer.get("origin") or "").upper()
    destination = str(identity.get("requested_destination") or "").upper()
    actual_origin = str(identity.get("actual_origin") or "").upper()
    actual_destination = str(identity.get("actual_destination") or "").upper()
    if not all((origin, destination, actual_origin, actual_destination)):
        raise TripFlightCompareError("Для этого билета не указано полное направление.", status_code=422)
    if city_code(actual_origin) != city_code(origin) or city_code(actual_destination) != city_code(destination):
        raise TripFlightCompareError("Фактический маршрут билета не совпадает с направлением поиска.", status_code=422)

    outbound = identity.get("outbound_segments")
    returning = identity.get("return_segments")
    if not isinstance(outbound, list) or not outbound:
        raise TripFlightCompareError("В билете не найдено исходящее плечо.", status_code=422)
    if trip_type == "RT" and (not isinstance(returning, list) or not returning):
        raise TripFlightCompareError("В билете не найдено обратное плечо.", status_code=422)
    stops = [len(outbound) - 1]
    if trip_type == "RT":
        stops.append(len(returning) - 1)

    currency = str(offer.get("currency") or "").upper()
    if currency != "RUB":
        raise TripFlightCompareError("Сравнение сейчас доступно только для билетов в рублях.", status_code=422)

    departure = _iso_date(offer.get("departure_date"), "вылета")
    return_date = _iso_date(offer.get("return_date"), "возвращения") if trip_type == "RT" else None
    if return_date and return_date < departure:
        raise TripFlightCompareError("Дата возвращения раньше даты вылета.", status_code=422)

    return {
        "origin": origin,
        "destination": destination,
        "origin_city": " ".join(str(offer.get("origin_city") or origin).split()),
        "destination_city": " ".join(str(offer.get("destination") or destination).split()),
        "trip_type": trip_type,
        "departure_date": departure,
        "return_date": return_date,
        "stops": stops,
        "currency": currency,
        "passengers": 1,
    }


def _parse_stops(label: str, card_text: str) -> int | None:
    text = f"{label} {card_text}".casefold()
    if "прямой рейс" in text or "без пересадок" in text:
        return 0
    match = re.search(r"\b(\d+)\s+пересадк", text)
    if match:
        return int(match.group(1))
    word_counts = {"одной": 1, "одну": 1, "одна": 1, "две": 2, "двух": 2,
                   "три": 3, "трех": 3, "трёх": 3, "четыре": 4, "четырех": 4,
                   "четырёх": 4}
    match = re.search(r"\b(одной|одну|одна|две|двух|три|трех|трёх|четыре|четырех|четырёх)\s+пересадк", text)
    if match:
        return word_counts[match.group(1)]
    # The current Trip.com flight-card label says "включая пересадку
    # длительностью …" without a numeric count. Its stop line separately
    # renders the city and connection duration, so this means one stop.
    if "включая пересадку" in text or "пересадку длительностью" in text:
        return 1
    return None


def _parse_price(label: str, trip_type: str) -> int | None:
    if trip_type == "RT":
        match = re.search(r"Туда\s+и\s+обратно\s*[—–-]\s*цена\s*:\s*([\d\s\u00a0]+)\s*₽", label, re.IGNORECASE)
    else:
        match = re.search(r"(?:В\s+одну\s+сторону\s*[—–-]\s*цена|Цена)\s*:\s*([\d\s\u00a0]+)\s*₽", label, re.IGNORECASE)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _airport_codes(card_text: str, expected_stops: int | None) -> list[str]:
    codes = re.findall(r"(?m)^\s*([A-Z]{3})\s*$", card_text)
    # Some Trip.com cards render code and terminal together (for example
    # ``SVO\nD``), so keep a token-based fallback and take only the itinerary
    # width indicated by the displayed stop count.
    if expected_stops is not None and len(codes) < expected_stops + 2:
        codes = re.findall(r"(?<![A-Z])([A-Z]{3})(?![A-Z])", card_text)
    if expected_stops is not None and len(codes) >= expected_stops + 2:
        codes = codes[:expected_stops + 2]
    return codes


def _parse_trip_result(label: str, card_text: str, trip_type: str) -> dict[str, Any] | None:
    stops = _parse_stops(label, card_text)
    amount = _parse_price(label, trip_type)
    timestamps = re.findall(r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?\b", label)
    if amount is None or stops is None or not timestamps:
        return None
    codes = _airport_codes(card_text, stops)
    if len(codes) < 2:
        return None
    chain_complete = len(codes) == stops + 2
    if len(codes) > stops + 2:
        return None
    stopover_cities = re.findall(
        r"\d+(?:[.,]\d+)?\s*(?:ч|час(?:а|ов)?)(?:\s*\d+\s*мин)?\s*\(([^()]+)\)",
        card_text,
        re.IGNORECASE,
    )
    description = re.search(r"продолжительностью\s+([^.]*)", label, re.IGNORECASE)
    airline = next((line.strip() for line in card_text.splitlines()
                    if line.strip() and not re.fullmatch(r"[A-Z]{3}", line.strip())
                    and not re.search(r"\d{1,2}:\d{2}|₽|пересадк|прямой рейс", line, re.IGNORECASE)
                    and len(line.strip()) <= 80), None)
    return {
        "price": amount,
        "currency": "RUB",
        "departure_date": timestamps[0][0],
        "departure_time": timestamps[0][1],
        "arrival_date": timestamps[1][0] if len(timestamps) > 1 else None,
        "arrival_time": timestamps[1][1] if len(timestamps) > 1 else None,
        "airport_chain": codes,
        "airport_chain_complete": chain_complete,
        "stops": stops,
        "stopover_cities": [city.strip() for city in stopover_cities],
        "airline": airline,
        "duration": description.group(1).strip() if description else None,
        "label": label,
    }


def _safe_tripcom_flight_url(value: Any) -> str | None:
    """Allow only HTTPS Trip.com flight pages to leave the comparison API."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or not (hostname == "trip.com" or hostname.endswith(".trip.com"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or not parsed.path.casefold().startswith("/flights")
        ):
            return None
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return None


def _tripcom_flight_url(page: Any, card: Any = None) -> str | None:
    """Prefer a flight-card link, falling back to the current exact-date results."""
    page_url = _safe_tripcom_flight_url(getattr(page, "url", None))
    if card is not None:
        try:
            links = card.locator("a[href]")
            for index in range(min(links.count(), 12)):
                href = links.nth(index).get_attribute("href")
                if href:
                    candidate = _safe_tripcom_flight_url(urljoin(getattr(page, "url", ""), href))
                    if candidate:
                        return candidate
        except Exception:
            # Trip.com currently renders many fare cards without a direct anchor.
            pass
    return page_url


def _matches_leg(result: dict[str, Any], origin: str, destination: str,
                 departure_date: str, max_stops: int) -> bool:
    chain = result.get("airport_chain")
    chain_complete = result.get("airport_chain_complete", True)
    actual_stops = result.get("stops")
    return (
        result.get("departure_date") == departure_date
        and isinstance(actual_stops, int)
        and 0 <= actual_stops <= max_stops
        and isinstance(chain, list)
        and len(chain) >= 2
        and (not chain_complete or len(chain) == actual_stops + 2)
        and city_code(chain[0]) == city_code(origin)
        and city_code(chain[-1]) == city_code(destination)
        and result.get("currency") == "RUB"
        and isinstance(result.get("price"), int)
        and result["price"] > 0
    )


def match_round_trip(outbound: dict[str, Any], returning: dict[str, Any],
                     context: dict[str, Any]) -> bool:
    """Require both actual Trip.com legs to match this city-level search.

    Airport choices within one city are intentionally accepted: the live search
    selects Trip.com's explicit ``Все аэропорты`` suggestion. Each live leg may
    have the same or fewer stops than the saved offer, while direction and leg
    departure dates remain exact.
    """
    if context["trip_type"] != "RT":
        return False
    out_stops, back_stops = context["stops"]
    return (
        _matches_leg(outbound, context["origin"], context["destination"],
                     context["departure_date"], out_stops)
        and _matches_leg(returning, context["destination"], context["origin"],
                         context["return_date"], back_stops)
        and outbound.get("currency") == returning.get("currency")
    )


def match_one_way(result: dict[str, Any], context: dict[str, Any]) -> bool:
    if context["trip_type"] != "OW":
        return False
    return _matches_leg(result, context["origin"], context["destination"],
                        context["departure_date"], context["stops"][0])


def _date_label_pattern(value: str) -> re.Pattern[str]:
    target = date.fromisoformat(value)
    return re.compile(rf"\b{target.day}\s+{_RU_MONTHS[target.month]}\s+{target.year}\s+г\.", re.IGNORECASE)


def _select_calendar_date(page: Any, value: str) -> None:
    target = date.fromisoformat(value)
    label = _date_label_pattern(value)
    for _ in range(14):
        day = page.get_by_role("checkbox", name=label)
        if day.count():
            if not day.first.is_enabled():
                raise TripFlightCompareError("Trip.com не разрешает выбрать дату этого билета.", status_code=422)
            day.first.click()
            return

        calendar_text = page.locator(TRIP_SEARCH_BOX_SELECTOR).inner_text()
        visible_months = re.findall(r"([а-яё]+),\s*(\d{4})\s*г\.", calendar_text, re.IGNORECASE)
        visible = next((( _MONTH_INDEX.get(name.casefold()), int(year))
                        for name, year in visible_months if name.casefold() in _MONTH_INDEX), None)
        if visible is None:
            raise TripFlightCompareError("Не удалось прочитать календарь Trip.com.")
        target_month = (target.year, target.month)
        current_month = (visible[1], visible[0])
        direction = "Перейти к следующему месяцу" if target_month > current_month else "Перейти к предыдущему месяцу"
        nav = page.get_by_role("button", name=direction)
        if not nav.count() or not nav.first.is_enabled():
            raise TripFlightCompareError("Trip.com не показал дату этого билета.", status_code=422)
        nav.first.click()
    raise TripFlightCompareError("Trip.com не показал дату этого билета.", status_code=422)


def _select_city_all_airports(page: Any, field_name: str, city: str) -> None:
    box = page.locator(TRIP_SEARCH_BOX_SELECTOR)
    city_group = box.get_by_role("group", name=re.compile(re.escape(field_name)))
    if city_group.count():
        # Trip.com restores the last search into the form. Remove that chip so
        # a new comparison cannot accidentally retain an extra city.
        clear_city = city_group.first.get_by_role("button", name="Удалить", exact=True)
        if clear_city.count():
            clear_city.first.click()
        city_group = box.get_by_role("group", name=re.compile(re.escape(field_name)))
        if city_group.count():
            city_group.first.click()

    field = box.get_by_role("combobox", name=field_name).filter(visible=True)
    if not field.count() and field_name in _CITY_INPUT_TEST_IDS:
        # The empty city group drops the accessible name in Trip.com's current
        # form, so target its stable test id after focusing the group.
        field = page.get_by_test_id(_CITY_INPUT_TEST_IDS[field_name]).filter(visible=True)
    if not field.count():
        raise TripFlightCompareError("Не найдено поле выбора города Trip.com.")
    field.last.fill(city, timeout=10000)
    suggestions = page.locator(TRIP_CITY_SUGGESTIONS_SELECTOR).get_by_role(
        "button", name=re.compile(r"Все аэропорты", re.IGNORECASE)
    )
    suggestions.first.wait_for(state="visible", timeout=15000)
    chosen_text = suggestions.first.inner_text()
    if city.casefold() not in chosen_text.casefold() or "все аэропорты" not in chosen_text.casefold():
        raise TripFlightCompareError(f"Trip.com не подтвердил город «{city}» со всеми аэропортами.")
    suggestions.first.click()


def _set_one_adult_in_economy(page: Any, box: Any) -> None:
    passengers = box.get_by_role(
        "button", name=re.compile(r"взросл.*Эконом", re.IGNORECASE)
    ).filter(visible=True)
    if not passengers.count():
        raise TripFlightCompareError("Не удалось подтвердить цену Trip.com для одного взрослого в эконом-классе.")

    passenger_toggle = passengers.first
    passenger_toggle.click()
    for name, expected in ((r"взросл", "1"), (r"дет", "0"), (r"младен", "0")):
        count_control = page.get_by_role(
            "spinbutton", name=re.compile(name, re.IGNORECASE)
        ).filter(visible=True)
        if not count_control.count():
            if name == r"взросл":
                raise TripFlightCompareError("Trip.com не показал настройки пассажиров для одного взрослого.")
            continue
        control = count_control.first
        current = control.input_value()
        if current != expected:
            tripcom_log.info(
                "[TRIP_FLIGHT_COMPARE] correcting_passenger_count type=%s from=%s to=%s",
                name, current, expected,
            )
            control.fill(expected)
        if control.input_value() != expected:
            raise TripFlightCompareError("Не удалось установить на Trip.com нужное число пассажиров.")
    passenger_label = passenger_toggle.inner_text()
    passenger_toggle.click()
    if not re.search(r"\b1\s+взросл", passenger_label, re.IGNORECASE):
        raise TripFlightCompareError(
            "Не удалось установить на Trip.com одного взрослого в эконом-классе."
        )


def _search_form(page: Any, context: dict[str, Any]) -> None:
    started = time.perf_counter()
    stage = "home_navigation"
    try:
        page.goto(TRIP_FLIGHT_HOME, wait_until="domcontentloaded", timeout=45000)
        stage = "form_ready"
        box = page.locator(TRIP_SEARCH_BOX_SELECTOR)
        box.wait_for(state="visible", timeout=25000)
        body = page.locator("body").inner_text()
        if re.search(r"captcha|verify you are human|access challenge|доступ ограничен", body, re.IGNORECASE):
            raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.")

        stage = "trip_type"
        trip_label = "Туда и обратно" if context["trip_type"] == "RT" else "В одну сторону"
        trip_mode = box.get_by_role("radio", name=trip_label)
        if not trip_mode.count():
            raise TripFlightCompareError("Не найден выбор типа поездки Trip.com.")
        if not trip_mode.first.is_checked():
            trip_mode.first.check()

        stage = "origin_city_all_airports"
        _select_city_all_airports(page, "Аэропорт или город вылета", context["origin_city"])
        stage = "destination_city_all_airports"
        _select_city_all_airports(page, "Аэропорт или город прилета", context["destination_city"])
        stage = "verify_all_airports"
        selected = box.inner_text().casefold()
        if selected.count("все аэропорты") != 2:
            raise TripFlightCompareError("Trip.com не подтвердил выбор «Все аэропорты» для обоих городов.")

        stage = "date_controls"
        date_inputs = box.locator("input[readonly]")
        if date_inputs.count() < (2 if context["trip_type"] == "RT" else 1):
            raise TripFlightCompareError("Не удалось установить точные даты поиска Trip.com.")
        date_inputs.nth(0).click()
        stage = "departure_date"
        _select_calendar_date(page, context["departure_date"])
        if context["trip_type"] == "RT":
            stage = "return_date"
            _select_calendar_date(page, context["return_date"])

        stage = "passenger_and_cabin"
        _set_one_adult_in_economy(page, box)

        stage = "submit_search"
        # The current Trip.com DOM renders the submit control outside
        # ``#searchBox-content`` as a generic text node rather than a named
        # button.  Prefer the visible page-level label, then keep the scoped
        # and role-based variants for older DOM versions.
        search = page.get_by_text("Найти", exact=True).filter(visible=True)
        if not search.count():
            search = box.get_by_text("Найти", exact=True).filter(visible=True)
        if not search.count():
            search = page.get_by_role("button", name=re.compile(r"^\s*Найти\s*$")).filter(visible=True)
        if not search.count():
            raise TripFlightCompareError("Не удалось запустить поиск Trip.com.")
        search.last.click()
        stage = "wait_outbound_results"
        if not _wait_for_initial_trip_results(page, timeout_ms=45000):
            tripcom_log.info("[TRIP_FLIGHT_COMPARE] stage=wait_outbound_results result=zero_flights")
            raise TripFlightCompareError("Trip.com не нашёл рейсы на выбранный маршрут и даты.")
        tripcom_log.info(
            "[TRIP_FLIGHT_COMPARE] stage=outbound_results_ready elapsed=%.2f",
            time.perf_counter() - started,
        )
    except TripFlightCompareError:
        raise
    except Exception as exc:
        page_state = "unknown"
        title = ""
        body_chars = 0
        challenge = False
        try:
            title = page.title()[:60]
            body = page.locator("body").inner_text(timeout=2000)
            body_chars = len(body)
            challenge = bool(re.search(
                r"captcha|verify you are human|access challenge|доступ ограничен", body, re.IGNORECASE
            ))
            page_state = "challenge" if challenge else ("empty" if not body.strip() else "no_search_form")
        except Exception:
            pass
        tripcom_log.warning("[TRIP_FLIGHT_COMPARE] form_stage=%s error_class=%s",
                            stage, type(exc).__name__)
        _discard_tripcom_reusable_context()
        if stage == "form_ready":
            tripcom_log.warning("[TRIP_FLIGHT_COMPARE] page_state=%s title=%r body_chars=%d",
                                page_state, title, body_chars)
        if challenge:
            raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.") from None
        retryable = stage == "form_ready"
        reason = (
            "Не удалось загрузить форму поиска Trip.com. Запустите поиск повторно."
            if retryable else "Не удалось выполнить поиск билета на Trip.com. Попробуйте позже."
        )
        raise TripFlightCompareError(reason, retryable=retryable) from None


def _result_cards(page: Any, trip_type: str, *, limit: int | None = 24) -> list[tuple[dict[str, Any], Any]]:
    """Parse the first sorted fare cards without walking a virtualized list forever.

    Trip.com keeps a large virtualized result list alive.  Reading every card,
    and then probing up to twelve links on every card, can close the page while
    the browser is still replacing result nodes.  The ``Самые дешёвые`` sort
    makes the first visible cards the useful candidates, so a bounded prefix is
    enough for a live comparison and keeps the result available promptly.
    """
    labels_locator = page.locator(TRIP_RESULT_SELECTOR)
    labels = labels_locator.all()
    cards: list[tuple[dict[str, Any], Any]] = []
    seen: set[str] = set()

    # Reading each virtualized card through Locator.inner_text() can block until
    # the node is replaced.  On a live result page, collect the visible label and
    # nearest fare-card text in one DOM pass instead.  The element handles are
    # still retained for clicking a selected fare; the slower locator path below
    # remains as a compatibility fallback for older Playwright versions and the
    # small test doubles used by the regression suite.
    snapshots: list[dict[str, Any]] | None = None
    try:
        snapshots = labels_locator.evaluate_all(
            """(elements, maxCount) => elements.slice(0, maxCount ?? elements.length).map((element, index) => {
                let node = element;
                let text = (element.innerText || '').trim();
                for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
                    const candidate = (node.innerText || '').trim();
                    if (/Выбрать этот тариф|Подробнее/i.test(candidate)) {
                        text = candidate;
                        break;
                    }
                }
                return {
                    index,
                    label: (element.getAttribute('aria-label') || '').trim(),
                    text,
                };
            })""",
            limit,
        )
    except Exception:
        snapshots = None

    if snapshots is not None:
        for snapshot in snapshots:
            index = snapshot.get("index")
            label = str(snapshot.get("label") or "").strip()
            if not label or label in seen or not isinstance(index, int) or index >= len(labels):
                continue
            seen.add(label)
            body = str(snapshot.get("text") or "").strip()
            result = _parse_trip_result(label, body, trip_type)
            if result is None:
                # A card can be replaced between the DOM pass and parsing.  Do
                # one bounded fallback probe for that individual card only.
                try:
                    body = labels[index].inner_text(timeout=TRIP_CARD_TEXT_TIMEOUT_MS)
                except Exception as exc:
                    if _is_target_closed_error(exc):
                        raise
                    continue
                result = _parse_trip_result(label, body, trip_type)
            if result is not None:
                card = labels[index].locator(
                    "xpath=ancestor-or-self::*[.//button[contains(normalize-space(.), 'Выбрать этот тариф') "
                    "or contains(normalize-space(.), 'Подробнее')]][1]"
                )
                cards.append((result, card))
        return cards

    for label_element in labels[:limit] if limit is not None else labels:
        label = (label_element.get_attribute("aria-label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        card = label_element.locator(
            "xpath=ancestor-or-self::*[.//button[contains(normalize-space(.), 'Выбрать этот тариф') "
            "or contains(normalize-space(.), 'Подробнее')]][1]"
        )
        try:
            body = card.inner_text(timeout=TRIP_CARD_TEXT_TIMEOUT_MS)
        except Exception as exc:
            # Playwright can report a dead driver as a generic Exception from
            # Locator.inner_text rather than TargetClosedError.  Preserve the
            # signal so the caller can discard the reusable context and retry
            # with a fresh browser instead of leaving the UI request pending.
            if _is_target_closed_error(exc):
                raise
            try:
                body = label_element.inner_text(timeout=TRIP_CARD_TEXT_TIMEOUT_MS)
                card = label_element
            except Exception as fallback_exc:
                if _is_target_closed_error(fallback_exc):
                    raise
                continue
        result = _parse_trip_result(label, body, trip_type)
        if result is not None:
            # Resolve the Trip.com URL only for the selected fare.  Probing up
            # to twelve links on every visible card adds avoidable latency and
            # can race with Trip.com's virtualized DOM.
            cards.append((result, card))
    return cards


def _load_more_results(page: Any, trip_type: str) -> None:
    for _ in range(5):
        current = len(_result_cards(page, trip_type))
        more = page.get_by_text("Показать еще результаты", exact=True)
        if not more.count() or not more.last.is_visible() or current >= 60:
            return
        more.last.click()
        try:
            page.wait_for_function(
                "([oldCount, selector]) => document.querySelectorAll(selector).length > oldCount",
                arg=[current, TRIP_RESULT_SELECTOR], timeout=12000,
            )
        except Exception:
            return


def _sort_cheapest(page: Any) -> None:
    pattern = re.compile(r"Самые деш[её]вые", re.IGNORECASE)
    candidates = (
        page.get_by_role("tab", name=pattern),
        page.get_by_role("button", name=pattern),
        page.get_by_role("link", name=pattern),
        page.get_by_text(pattern),
    )
    control = next((candidate.first for candidate in candidates if candidate.count()), None)
    if control is None:
        tripcom_log.warning("[TRIP_FLIGHT_COMPARE] cheapest_tab=not_available")
        raise TripFlightCompareError(
            "Trip.com не показал сортировку «Самые дешёвые», поэтому проверить минимальную цену нельзя."
        )

    selected = control.get_attribute("aria-selected") == "true"
    pressed = control.get_attribute("aria-pressed") == "true"
    if not (selected or pressed):
        control.click()
        # Clicking a Trip.com sort tab starts an asynchronous result refresh.
        # Waiting only for the already-present result list returns immediately
        # and can leave us pricing the previous (recommended) ordering.
        page.wait_for_function(
            """() => {
                const pattern = /самые деш[её]вые/i;
                const controls = [...document.querySelectorAll('[role="tab"], [role="button"], button, a')];
                return controls.some(node => {
                    const label = `${node.getAttribute('aria-label') || ''} ${node.innerText || ''}`;
                    if (!pattern.test(label)) return false;
                    const state = `${node.className || ''} ${node.parentElement?.className || ''}`;
                    return node.getAttribute('aria-selected') === 'true'
                        || node.getAttribute('aria-pressed') === 'true'
                        || /(?:^|\\s)(?:active|selected|current)(?:\\s|$)/i.test(state);
                });
            }""",
            timeout=15000,
        )
    tripcom_log.info("[TRIP_FLIGHT_COMPARE] cheapest_tab=selected")


def _wait_for_trip_result_list(page: Any, *, timeout_ms: int = 30000) -> bool:
    """Wait for either fare cards or Trip.com's explicit empty-results state."""
    results = page.locator(TRIP_RESULT_SELECTOR)
    empty_state = page.get_by_text(TRIP_EMPTY_RESULTS_PATTERN)
    try:
        results.first.or_(empty_state).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            pass
        if TRIP_ACCESS_LIMIT_PATTERN.search(body):
            raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.") from None
        if TRIP_EMPTY_RESULTS_PATTERN.search(body):
            return False
        # A result can have appeared while the combined locator was timing out
        # on older Playwright versions that do not support Locator.or_.
        if results.count():
            try:
                results.first.wait_for(state="visible", timeout=1000)
                return True
            except Exception:
                pass
        raise TripFlightCompareError(
            "Trip.com не загрузил список рейсов. Нажмите «Повторить» для нового поиска.",
            retryable=True,
        ) from None

    if results.count():
        return True
    body = page.locator("body").inner_text(timeout=2000)
    if TRIP_ACCESS_LIMIT_PATTERN.search(body):
        raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.")
    return not bool(TRIP_EMPTY_RESULTS_PATTERN.search(body))


def _wait_for_initial_trip_results(page: Any, *, timeout_ms: int = 45000) -> bool:
    """Wait for initial fare cards or Trip.com's explicit empty-results state.

    Trip.com can keep the browser on the search page when a route has no fares.
    Waiting for the ShowFareFirst URL first turns that valid answer into a
    misleading navigation timeout.
    """
    ready_script = """
        selector => {
            const body = document.body?.innerText || "";
            const empty = /Найдено\\s*0\\s*рейсов|Доступных\\s+рейсов\\s+не\\s+найдено|рейсов\\s+не\\s+найдено|рейсы\\s+не\\s+найдены|no\\s+(available\\s+)?flights|0\\s+flights/i.test(body);
            const blocked = /captcha|verify you are human|access challenge|доступ ограничен/i.test(body);
            return Boolean(document.querySelector(selector)) || empty || blocked;
        }
    """
    try:
        page.wait_for_function(ready_script, arg=TRIP_RESULT_SELECTOR, timeout=timeout_ms)
    except Exception:
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            pass
        if TRIP_ACCESS_LIMIT_PATTERN.search(body):
            raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.") from None
        if TRIP_EMPTY_RESULTS_PATTERN.search(body):
            return False
        try:
            if page.locator(TRIP_RESULT_SELECTOR).count():
                return True
        except Exception:
            pass
        raise TripFlightCompareError(
            "Trip.com не загрузил список рейсов. Нажмите «Повторить» для нового поиска.",
            retryable=True,
        ) from None

    body = page.locator("body").inner_text(timeout=2000)
    if TRIP_ACCESS_LIMIT_PATTERN.search(body):
        raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.")
    if TRIP_EMPTY_RESULTS_PATTERN.search(body):
        return False
    return bool(page.locator(TRIP_RESULT_SELECTOR).count())


def _extract_route_context(offer: dict[str, Any]) -> dict[str, Any]:
    context = build_offer_context(offer)
    if not context["origin_city"] or not context["destination_city"]:
        raise TripFlightCompareError("Не удалось определить названия городов.", status_code=422)
    return context


def _safe_tripcom_error_message(exc: Exception, profile: Path) -> str:
    """Keep useful browser diagnostics in logs without copying a local profile path."""
    message = " ".join(str(exc).split())
    try:
        profile_path = str(profile.resolve())
        if profile_path:
            message = re.sub(re.escape(profile_path), "<tripcom-profile>", message, flags=re.IGNORECASE)
    except (OSError, RuntimeError):
        pass
    return (message or type(exc).__name__)[:300]


def _is_target_closed_error(exc: BaseException) -> bool:
    """Return whether Playwright lost the Trip.com page/browser target."""
    error_name = type(exc).__name__.casefold()
    error_text = " ".join(str(exc).split()).casefold()
    return (
        any(marker in error_name for marker in ("targetclosed", "browserclosed", "pagecrashed"))
        or any(marker in error_text for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser disconnected",
            "target closed",
            "target crashed",
            "page crashed",
            # The Playwright sync driver uses a plain Exception for a broken
            # transport in some versions, e.g. while reading a locator.
            "connection closed while reading from the driver",
            "connection closed while reading from the websocket",
            "connection closed while reading from the transport",
        ))
    )


def _tripcom_search_in_fresh_thread(
    context: dict[str, Any], profile: Path, *, retry_depth: int,
) -> dict[str, Any]:
    """Retry outside a broken Playwright sync-driver thread.

    A closed Chromium target can leave the sync driver's dispatcher fiber in a
    bad state until the owning Python thread exits.  Joining a new short-lived
    thread gives the next persistent-context launch a clean dispatcher without
    changing the public executor API.
    """
    result: dict[str, Any] | None = None
    error: BaseException | None = None

    def worker() -> None:
        nonlocal result, error
        try:
            result = _tripcom_search(context, profile, retry_depth=retry_depth)
        except BaseException as exc:  # propagate the original typed failure
            error = exc

    thread = threading.Thread(
        target=worker,
        name="tripcom-browser-retry",
        daemon=True,
    )
    thread.start()
    thread.join()
    if error is not None:
        raise error
    if result is None:
        raise TripFlightCompareError("Не удалось получить сопоставимую цену Trip.com.")
    return result


def _tripcom_search(
    context: dict[str, Any], profile: Path, *, retry_depth: int = 0,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        loop_running = False
    tripcom_log.info(
        "[TRIP_FLIGHT_COMPARE] stage=executor_context thread=%s loop_running=%s",
        threading.current_thread().name, loop_running,
    )
    if not TRIP_PROFILE_LOCK.acquire(blocking=False):
        raise TripFlightCompareError("Браузер Trip.com занят другим поиском. Повторите через минуту.")
    tripcom_log.info(
        "[TRIP_FLIGHT_COMPARE] stage=search_start type=%s origin=%s destination=%s departure=%s return=%s max_stops=%s",
        context["trip_type"], context["origin"], context["destination"],
        context["departure_date"], context["return_date"], context["stops"],
    )
    page = None
    lock_held = True
    stage = "browser_start"
    try:
        from playwright.sync_api import sync_playwright

        profile.mkdir(parents=True, exist_ok=True)
        def run_results() -> dict[str, Any]:
            nonlocal stage
            stage = "outbound_results"
            stage = "sort_outbound"
            sort_started = time.perf_counter()
            _sort_cheapest(page)
            tripcom_log.info(
                "[TRIP_FLIGHT_COMPARE] stage=sort_outbound_complete elapsed=%.2f",
                time.perf_counter() - sort_started,
            )
            # Trip.com puts the cheapest fares first after the sort control is
            # applied.  Clicking its lazy "show more" control can keep the
            # page in a loading state or close the target altogether; the
            # visible sorted cards are sufficient for this price comparison.
            tripcom_log.info("[TRIP_FLIGHT_COMPARE] stage=visible_sorted_results")
            stage = "parse_outbound"
            parse_started = time.perf_counter()
            cards = _result_cards(page, context["trip_type"])
            tripcom_log.info(
                "[TRIP_FLIGHT_COMPARE] stage=parse_outbound cards=%d elapsed=%.2f",
                len(cards), time.perf_counter() - parse_started,
            )
            if context["trip_type"] == "OW":
                matches = [(item, card) for item, card in cards if match_one_way(item, context)]
                tripcom_log.info("[TRIP_FLIGHT_COMPARE] stage=parse_outbound matching=%d", len(matches))
                if not matches:
                    raise TripFlightCompareError("Trip.com не нашёл сопоставимый билет на эти даты.")
                selected, selected_card = min(matches, key=lambda pair: pair[0]["price"])
                selected["tripcom_url"] = _tripcom_flight_url(page, selected_card)
                tripcom_log.info("[TRIP_FLIGHT_COMPARE] stage=complete price=%d", selected["price"])
                return {
                    "price": selected["price"], "currency": "RUB", "trip_type": "OW",
                    "passengers": 1, "outbound": selected, "return": None,
                    "search_airports": "all", "tripcom_url": selected["tripcom_url"],
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                }

            expected_out_stops = context["stops"][0]
            eligible = [(item, card) for item, card in cards
                        if _matches_leg(item, context["origin"], context["destination"],
                                        context["departure_date"], expected_out_stops)]
            eligible.sort(key=lambda pair: pair[0]["price"])
            tripcom_log.info("[TRIP_FLIGHT_COMPARE] stage=parse_outbound eligible=%d", len(eligible))
            if not eligible:
                tripcom_log.info(
                    "[TRIP_FLIGHT_COMPARE] stage=parse_outbound candidates=%s",
                    [
                        {
                            "price": item.get("price"),
                            "date": item.get("departure_date"),
                            "stops": item.get("stops"),
                            "chain": item.get("airport_chain"),
                        }
                        for item, _card in cards
                    ],
                )
            best_pair = None
            visible_fallback = None
            saw_empty_return_results = False
            saw_return_cards = False
            for index, (outbound, card) in enumerate(eligible):
                # Each outbound quote is the lowest combined fare Trip.com advertises
                # for that departure. Once it cannot beat a verified return fare,
                # later (more expensive) departures cannot improve the result.
                if best_pair is not None and outbound["price"] >= best_pair["price"]:
                    break
                stage = "select_outbound"
                button = card.get_by_role("button", name=re.compile(r"Выбрать этот тариф", re.IGNORECASE))
                if not button.count():
                    tripcom_log.warning(
                        "[TRIP_FLIGHT_COMPARE] stage=select_outbound result=missing_fare_button price=%d",
                        outbound["price"],
                    )
                    # Trip.com sometimes renders a complete RT fare card with
                    # the price and itinerary but omits the action button in
                    # the current DOM variant.  The user still has a real,
                    # sorted fare to open, so keep the cheapest visible card
                    # as a fallback instead of discarding its price.
                    if visible_fallback is None:
                        visible_fallback = (outbound, card)
                    continue
                tripcom_log.info(
                    "[TRIP_FLIGHT_COMPARE] stage=select_outbound price=%d stops=%d",
                    outbound["price"], outbound["stops"],
                )
                button.first.click()
                try:
                    stage = "return_results"
                    page.wait_for_url(re.compile(r"/flights/ShowFareNext", re.IGNORECASE), timeout=30000)
                except Exception:
                    _discard_tripcom_reusable_context()
                    raise TripFlightCompareError(
                        "Trip.com не открыл обратные рейсы. Нажмите «Повторить» для нового поиска.",
                        retryable=True,
                    ) from None

                if not _wait_for_trip_result_list(page, timeout_ms=30000):
                    saw_empty_return_results = True
                    tripcom_log.info(
                        "[TRIP_FLIGHT_COMPARE] stage=return_results result=zero_flights outbound_price=%d",
                        outbound["price"],
                    )
                    stage = "return_to_outbound_results"
                    page.go_back(wait_until="domcontentloaded", timeout=20000)
                    if not _wait_for_trip_result_list(page, timeout_ms=30000):
                        raise TripFlightCompareError("Trip.com не восстановил список вылетов для проверки других рейсов.")
                    continue

                stage = "sort_return_results"
                _sort_cheapest(page)
                tripcom_log.info("[TRIP_FLIGHT_COMPARE] stage=visible_sorted_return_results")
                stage = "parse_return_results"
                return_cards = _result_cards(page, "RT")
                saw_return_cards = saw_return_cards or bool(return_cards)
                matches = [(item, return_card) for item, return_card in return_cards
                           if match_round_trip(outbound, item, context)]
                tripcom_log.info(
                    "[TRIP_FLIGHT_COMPARE] stage=parse_return_results cards=%d matching=%d",
                    len(return_cards), len(matches),
                )
                if matches:
                    selected_return, selected_return_card = min(matches, key=lambda pair: pair[0]["price"])
                    fare_url = _tripcom_flight_url(page, selected_return_card)
                    selected_return["tripcom_url"] = fare_url
                    candidate = {
                        "price": selected_return["price"], "currency": "RUB", "trip_type": "RT",
                        "passengers": 1, "outbound": outbound, "return": selected_return,
                        "search_airports": "all", "tripcom_url": fare_url,
                        "elapsed_seconds": round(time.perf_counter() - started, 2),
                    }
                    if best_pair is None or candidate["price"] < best_pair["price"]:
                        best_pair = candidate
                        tripcom_log.info(
                            "[TRIP_FLIGHT_COMPARE] stage=best_pair price=%d outbound_stops=%d return_stops=%d",
                            candidate["price"], outbound["stops"], selected_return["stops"],
                        )

                next_quote = eligible[index + 1][0]["price"] if index + 1 < len(eligible) else None
                if best_pair is not None and (next_quote is None or next_quote >= best_pair["price"]):
                    break

                stage = "return_to_outbound_results"
                page.go_back(wait_until="domcontentloaded", timeout=20000)
                if not _wait_for_trip_result_list(page, timeout_ms=30000):
                    raise TripFlightCompareError("Trip.com не восстановил список вылетов для проверки других рейсов.")
            if best_pair is not None:
                best_pair["elapsed_seconds"] = round(time.perf_counter() - started, 2)
                tripcom_log.info(
                    "[TRIP_FLIGHT_COMPARE] stage=complete price=%d elapsed=%.2f",
                    best_pair["price"], best_pair["elapsed_seconds"],
                )
                return best_pair
            if saw_empty_return_results and not saw_return_cards:
                raise TripFlightCompareError(
                    "Trip.com показал 0 обратных рейсов на выбранную дату. Нажмите «Повторить» позже."
                )
            if visible_fallback is not None:
                selected, selected_card = visible_fallback
                selected = dict(selected)
                selected["tripcom_url"] = _tripcom_flight_url(page, selected_card)
                elapsed = round(time.perf_counter() - started, 2)
                tripcom_log.info(
                    "[TRIP_FLIGHT_COMPARE] stage=complete_visible_card price=%d elapsed=%.2f",
                    selected["price"], elapsed,
                )
                return {
                    "price": selected["price"], "currency": "RUB", "trip_type": "RT",
                    "passengers": 1, "outbound": selected, "return": None,
                    "search_airports": "all", "tripcom_url": selected["tripcom_url"],
                    "elapsed_seconds": elapsed,
                }
            raise TripFlightCompareError("Trip.com не нашёл пару рейсов с подходящими пересадками.")

        for form_attempt in range(2):
            if form_attempt:
                tripcom_log.warning("[TRIP_FLIGHT_COMPARE] retry=fresh_context reason=transient_form")
            try:
                _manager, browser_context, reused = _tripcom_reusable_context(profile, sync_playwright)
                page, browser_context, reused = _tripcom_new_page_with_recovery(
                    browser_context, reused, profile, sync_playwright
                )
                page.set_default_timeout(15000)
                statuses: list[int] = []
                page.on("response", lambda response: statuses.append(response.status)
                        if response.request.resource_type == "document" else None)
            except Exception as exc:
                if retry_depth == 0 and _is_target_closed_error(exc):
                    tripcom_log.warning(
                        "[TRIP_FLIGHT_COMPARE] stage=browser_start error_class=%s retry=fresh_thread reason=target_closed",
                        type(exc).__name__,
                    )
                    _discard_tripcom_reusable_context()
                    lock_held = False
                    TRIP_PROFILE_LOCK.release()
                    return _tripcom_search_in_fresh_thread(context, profile, retry_depth=1)
                if form_attempt:
                    raise
                tripcom_log.warning(
                    "[TRIP_FLIGHT_COMPARE] stage=browser_start error_class=%s error=%r retry=fresh_context",
                    type(exc).__name__, _safe_tripcom_error_message(exc, profile),
                )
                _discard_tripcom_reusable_context()
                continue
            stage = "search_form"
            try:
                _search_form(page, context)
            except TripFlightCompareError as exc:
                if any(status in {403, 430} for status in statuses):
                    raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.") from None
                if not exc.retryable or form_attempt:
                    raise
                try:
                    page.close()
                except Exception:
                    pass
                page = None
                _discard_tripcom_reusable_context()
                if retry_depth == 0:
                    lock_held = False
                    TRIP_PROFILE_LOCK.release()
                    return _tripcom_search_in_fresh_thread(context, profile, retry_depth=1)
                continue
            if any(status in {403, 430} for status in statuses):
                raise TripFlightCompareError("Trip.com ограничил обычный поиск. Повторите позже.")
            try:
                result = run_results()
                # A persistent Sync Playwright context can leave its driver
                # event loop active on the single Trip.com worker thread.
                # Reusing it for the next HTTP request then makes Sync API
                # calls fail with "inside the asyncio loop" before a new
                # search starts.  A completed comparison is cheap to repeat
                # and must release the context so the next offer gets a
                # genuinely fresh browser state.
                _discard_tripcom_reusable_context()
                return result
            except Exception as exc:
                if retry_depth == 0 and form_attempt == 0 and _is_target_closed_error(exc):
                    tripcom_log.warning(
                        "[TRIP_FLIGHT_COMPARE] stage=%s error_class=%s retry=fresh_context reason=target_closed",
                        stage, type(exc).__name__,
                    )
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = None
                    _discard_tripcom_reusable_context()
                    lock_held = False
                    TRIP_PROFILE_LOCK.release()
                    return _tripcom_search_in_fresh_thread(context, profile, retry_depth=1)
                raise

    except TripFlightCompareError as exc:
        tripcom_log.warning(
            "[TRIP_FLIGHT_COMPARE] stage=%s result=failed retryable=%s reason=%r",
            stage, exc.retryable, str(exc),
        )
        _discard_tripcom_reusable_context()
        raise
    except Exception as exc:
        tripcom_log.warning(
            "[TRIP_FLIGHT_COMPARE] stage=%s error_class=%s error=%r",
            stage, type(exc).__name__, _safe_tripcom_error_message(exc, profile),
        )
        _discard_tripcom_reusable_context()
        if stage == "browser_start":
            raise TripFlightCompareError(
                "Браузер Trip.com не запустился после повторной попытки. Нажмите «Повторить» для нового запуска."
            ) from None
        if _is_target_closed_error(exc):
            raise TripFlightCompareError(
                "Trip.com закрыл страницу результатов. Нажмите «Повторить» для нового поиска.",
                retryable=True,
            ) from None
        raise TripFlightCompareError("Не удалось получить сопоставимую цену Trip.com. Попробуйте позже.") from None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if lock_held:
            TRIP_PROFILE_LOCK.release()


async def compare_trip_offer(offer: dict[str, Any]) -> dict[str, Any]:
    """Run one isolated flight comparison on a disposable Playwright thread.

    Playwright's Sync API owns an asyncio dispatcher inside its worker thread.
    A persistent ``ThreadPoolExecutor`` can retain that dispatcher after a
    successful search, making the next request fail before the browser starts.
    A one-shot executor lets the thread (and its dispatcher) terminate before
    the next offer is handled.
    """
    context = _extract_route_context(offer)
    profile_setting = os.getenv("TRIP_COM_BROWSER_DATA_DIR")
    profile = Path(profile_setting).expanduser().resolve() if profile_setting else DEFAULT_TRIP_PROFILE
    loop = asyncio.get_running_loop()

    def run_once() -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tripcom-browser-request") as executor:
            return executor.submit(_tripcom_search, context, profile).result()

    return await loop.run_in_executor(None, run_once)
