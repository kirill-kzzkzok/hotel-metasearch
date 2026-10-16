from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from aviasales_collector import web
from aviasales_collector import trip_flight_compare
from aviasales_collector.database import SQLiteStore
from aviasales_collector.trip_flight_compare import (
    TRIP_CITY_SUGGESTIONS_SELECTOR,
    TRIP_SEARCH_BOX_SELECTOR,
    _parse_trip_result,
    _search_form,
    _select_city_all_airports,
    _sort_cheapest,
    _safe_tripcom_flight_url,
    _tripcom_flight_url,
    build_offer_context,
    match_one_way,
    match_round_trip,
    TripFlightCompareError,
)


def _context(*, trip_type="RT", stops=(0, 0)):
    outbound = [{"origin": "SVO", "destination": "KGD"}]
    returning = [{"origin": "KGD", "destination": "VKO"}]
    if stops[0]:
        outbound = [{"origin": "SVO", "destination": "LED"}, {"origin": "LED", "destination": "KGD"}]
    if trip_type == "RT" and len(stops) > 1 and stops[1]:
        returning = [{"origin": "KGD", "destination": "IST"}, {"origin": "IST", "destination": "VKO"}]
    identity = {
        "requested_origin": "MOW", "requested_destination": "KGD",
        "actual_origin": "SVO", "actual_destination": "KGD",
        "route_valid": True, "trip_type": trip_type,
        "outbound_segments": outbound,
        "return_segments": returning if trip_type == "RT" else [],
    }
    offer = {
        "origin": "MOW", "origin_city": "Москва", "destination": "Калининград",
        "price": 9431, "currency": "RUB", "trip_type": trip_type,
        "departure_date": "2026-10-07", "return_date": "2026-10-14" if trip_type == "RT" else None,
        "route_identity": identity,
    }
    return build_offer_context(offer)


def _flight(price, date, chain, stops):
    return {
        "price": price, "currency": "RUB", "departure_date": date,
        "airport_chain": chain, "stops": stops,
    }


def test_trip_card_parser_reads_full_round_trip_fare_and_direct_itinerary():
    label = (
        "Вылет рейса: Международный аэропорт Шереметьево, в 2026-10-07 08:55:00. "
        "Прилет рейса: Аэропорт Калининград, в 2026-10-07 10:35:00. "
        "Это прямой рейс продолжительностью 2 ч 40 мин. Туда и обратно — цена: 12 036 ₽"
    )
    body = "Смартавиа\n08:55\nSVO\nD\n2 ч 40 мин\nБез пересадок\n10:35\nKGD\n12 036 ₽"
    parsed = _parse_trip_result(label, body, "RT")
    assert parsed is not None
    assert parsed["price"] == 12036
    assert parsed["currency"] == "RUB"
    assert parsed["departure_date"] == "2026-10-07"
    assert parsed["airport_chain"] == ["SVO", "KGD"]
    assert parsed["stops"] == 0


def test_trip_card_parser_reads_one_stop_chain():
    label = (
        "Вылет рейса: Аэропорт Калининград, в 2026-10-14 18:10:00. "
        "Прилет рейса: Международный аэропорт Внуково, в 2026-10-15 01:50:00. "
        "Это рейс с одной пересадкой продолжительностью 7 ч 40 мин. "
        "Туда и обратно — цена: 16 543 ₽"
    )
    body = "Smartavia\n18:10\nKGD\nSAW\n7 ч 40 мин\n1 пересадка\n01:50\nVKO\n16 543 ₽"
    parsed = _parse_trip_result(label, body, "RT")
    assert parsed is not None
    assert parsed["stops"] == 1
    assert parsed["airport_chain"] == ["KGD", "SAW", "VKO"]


def test_trip_card_parser_reads_current_dom_one_stop_without_transfer_airport_code():
    label = (
        "Вылет рейса: Международный аэропорт Внуково A, в 2026-11-09 00:35:00. "
        "Прилет рейса: Аэропорт Анталья D, в 2026-11-09 08:40:00. "
        "Продолжительность перелета: 2 ч 30 мин, включая пересадку длительностью "
        "Стамбул в г. ${2}\nТуда и обратно — цена: 34 306 ₽"
    )
    body = "Turkish Airlines\n00:35\nVKO\nA\n8 ч 5 мин\n2 ч 30 мин (Стамбул)\n08:40\nAYT\nD\n34 306 ₽"

    parsed = _parse_trip_result(label, body, "RT")

    assert parsed is not None
    assert parsed["departure_date"] == "2026-11-09"
    assert parsed["airport_chain"] == ["VKO", "AYT"]
    assert parsed["airport_chain_complete"] is False
    assert parsed["stops"] == 1
    assert parsed["stopover_cities"] == ["Стамбул"]


def test_result_cards_skip_refreshing_virtualized_card_with_bounded_text_timeout(monkeypatch):
    label = (
        "Вылет рейса: Международный аэропорт Шереметьево, в 2026-10-07 08:55:00. "
        "Прилет рейса: Аэропорт Калининград, в 2026-10-07 10:35:00. "
        "Это прямой рейс продолжительностью 2 ч 40 мин. Туда и обратно — цена: 12 036 ₽"
    )
    body = "Смартавиа\n08:55\nSVO\n2 ч 40 мин\nБез пересадок\n10:35\nKGD\n12 036 ₽"

    class Card:
        def inner_text(self, *, timeout):
            assert timeout == trip_flight_compare.TRIP_CARD_TEXT_TIMEOUT_MS
            raise TimeoutError("virtualized card was replaced")

    class Label:
        def __init__(self):
            self.timeouts = []

        def get_attribute(self, _name):
            return label

        def locator(self, _selector):
            return Card()

        def inner_text(self, *, timeout):
            self.timeouts.append(timeout)
            return body

    class Labels:
        def all(self):
            return [Label()]

    class Page:
        url = "https://ru.trip.com/flights/list"

        def locator(self, _selector):
            return Labels()

    monkeypatch.setattr(trip_flight_compare, "_tripcom_flight_url", lambda *_args: "https://ru.trip.com/flights/list")
    cards = trip_flight_compare._result_cards(Page(), "RT")
    assert cards[0][0]["price"] == 12036


def test_result_cards_read_visible_fares_in_one_dom_pass(monkeypatch):
    label = (
        "Вылет рейса: Международный аэропорт Шереметьево, в 2026-10-07 08:55:00. "
        "Прилет рейса: Аэропорт Калининград, в 2026-10-07 10:35:00. "
        "Это прямой рейс продолжительностью 2 ч 40 мин. В одну сторону — цена: 6 100 ₽"
    )
    body = "Смартавиа\n08:55\nSVO\n2 ч 40 мин\nБез пересадок\n10:35\nKGD\n6 100 ₽"

    class CardLocator:
        def __init__(self, selector):
            self.selector = selector

        def locator(self, selector):
            assert "ancestor-or-self" in selector
            return self

    class Label:
        def get_attribute(self, name):
            assert name == "aria-label"
            return label

        def locator(self, selector):
            return CardLocator(selector)

    class Labels:
        def __init__(self):
            self.dom_passes = 0

        def all(self):
            return [Label()]

        def evaluate_all(self, _script, limit):
            self.dom_passes += 1
            assert limit == 24
            return [{"index": 0, "label": label, "text": body}]

    labels = Labels()

    class Page:
        def locator(self, selector):
            assert selector == trip_flight_compare.TRIP_RESULT_SELECTOR
            return labels

    def unexpected_url_lookup(*_args):
        raise AssertionError("URL lookup should be deferred until a fare is selected")

    monkeypatch.setattr(trip_flight_compare, "_tripcom_flight_url", unexpected_url_lookup)

    cards = trip_flight_compare._result_cards(Page(), "OW")

    assert len(cards) == 1
    assert cards[0][0]["price"] == 6100
    assert cards[0][0]["airport_chain"] == ["SVO", "KGD"]
    assert labels.dom_passes == 1


def test_tripcom_flight_link_is_limited_to_https_tripcom_flight_pages():
    assert _safe_tripcom_flight_url(
        "https://ru.trip.com/flights/ShowFareNext?flight=abc#fare"
    ) == "https://ru.trip.com/flights/ShowFareNext?flight=abc"
    assert _safe_tripcom_flight_url("https://evil.example/flights/search") is None
    assert _safe_tripcom_flight_url("javascript:alert(1)") is None
    assert _safe_tripcom_flight_url("https://user@ru.trip.com/flights/search") is None
    assert _safe_tripcom_flight_url("https://ru.trip.com:444/flights/search") is None


def test_tripcom_flight_link_uses_a_card_link_or_exact_results_page():
    class Link:
        def __init__(self, href):
            self.href = href

        def get_attribute(self, _name):
            return self.href

    class Links:
        def count(self):
            return 1

        def nth(self, _index):
            return Link("/flights/ShowFareNext?fare=cheapest")

    class Card:
        def locator(self, _selector):
            return Links()

    class Page:
        url = "https://ru.trip.com/flights/list?date=2026-10-07"

    assert _tripcom_flight_url(Page(), Card()) == (
        "https://ru.trip.com/flights/ShowFareNext?fare=cheapest"
    )
    assert _tripcom_flight_url(Page()) == Page.url


@pytest.mark.parametrize(
    ("result_count", "body", "expected"),
    [
        (1, "Найдено 48 рейсов", True),
        (0, "Найдено 0 рейсов\nДоступных рейсов не найдено", False),
    ],
)
def test_wait_for_trip_results_accepts_fares_or_explicit_empty_state(result_count, body, expected):
    class Locator:
        def __init__(self, visible=False, text=""):
            self.visible = visible
            self.text = text
            self.first = self

        def or_(self, other):
            return Locator(self.visible or other.visible)

        def wait_for(self, **_kwargs):
            if not self.visible:
                raise TimeoutError("not visible")

        def count(self):
            return result_count if self.visible else 0

        def inner_text(self, **_kwargs):
            return self.text

    class Page:
        def locator(self, selector):
            if selector == "body":
                return Locator(text=body)
            return Locator(visible=result_count > 0)

        def get_by_text(self, _pattern):
            return Locator(visible=expected is False)

    assert trip_flight_compare._wait_for_trip_result_list(Page(), timeout_ms=100) is expected


def test_cheapest_sort_recognizes_yo_and_waits_for_tripcom_to_apply_it():
    calls = []

    class Control:
        first = None

        def __init__(self):
            self.selected = "false"
            self.first = self

        def count(self):
            return 1

        def get_attribute(self, name):
            return self.selected if name == "aria-selected" else None

        def click(self):
            calls.append("click")
            self.selected = "true"

    control = Control()

    class Page:
        def get_by_role(self, role, *, name):
            calls.append(("find", role, name.pattern))
            return control if role == "tab" else EmptyControl()

        def get_by_text(self, _pattern):
            return EmptyControl()

        def wait_for_function(self, script, **kwargs):
            calls.append(("wait", script, kwargs))
            assert control.selected == "true"

        def locator(self, selector):
            if selector == "body":
                return BodyLocator()
            return ResultLocator()

    class BodyLocator:
        def inner_text(self, **_kwargs):
            return "Найдено 48 рейсов"

    class ResultLocator:
        def count(self):
            return 1

    class EmptyControl:
        first = None

        def count(self):
            return 0

    _sort_cheapest(Page())

    assert calls[0] == ("find", "tab", "Самые деш[её]вые")
    assert "click" in calls
    wait_call = next(call for call in calls if isinstance(call, tuple) and call[0] == "wait")
    assert "node.getAttribute('aria-selected') === 'true'" in wait_call[1]
    assert calls.index("click") < calls.index(wait_call)
    # 2026-10-14 DOM: the sort click clears the list before re-rendering, so
    # the sort must also wait for fare cards to re-appear before returning.
    card_wait = next(
        call for call in calls
        if isinstance(call, tuple) and call[0] == "wait"
        and call[2].get("arg") == trip_flight_compare.TRIP_RESULT_SELECTOR
    )
    assert "document.querySelector(selector)" in card_wait[1]
    assert calls.index(wait_call) < calls.index(card_wait)


def test_cheapest_sort_fails_clearly_when_tripcom_does_not_show_it():
    class EmptyControl:
        first = None

        def count(self):
            return 0

    class Page:
        def get_by_role(self, *_args, **_kwargs):
            return EmptyControl()

        def get_by_text(self, *_args, **_kwargs):
            return EmptyControl()

    with pytest.raises(TripFlightCompareError, match="не показал сортировку «Самые дешёвые»"):
        _sort_cheapest(Page())


def test_initial_trip_search_recognizes_current_zero_flights_page_without_url_change():
    body = (
        "Нет доступных фильтров\nВылеты в Монастир\nНайдено 0 рейсов\n"
        "Ближайшие маршруты\nСанкт-ПетербургТунис\nОт 58 031 ₽"
    )

    class Locator:
        def inner_text(self, **_kwargs):
            return body

        def count(self):
            return 0

    class Page:
        def wait_for_function(self, _script, *, arg, timeout):
            assert arg == trip_flight_compare.TRIP_RESULT_SELECTOR
            assert timeout == 100

        def locator(self, _selector):
            return Locator()

    assert trip_flight_compare._wait_for_initial_trip_results(Page(), timeout_ms=100) is False


def test_initial_trip_search_marks_unrecognized_timeout_as_retryable():
    class Locator:
        def inner_text(self, **_kwargs):
            return "Trip.com is still loading"

        def count(self):
            return 0

    class Page:
        def wait_for_function(self, *_args, **_kwargs):
            raise TimeoutError("result state did not appear")

        def locator(self, _selector):
            return Locator()

    with pytest.raises(TripFlightCompareError, match="не загрузил список рейсов") as error:
        trip_flight_compare._wait_for_initial_trip_results(Page(), timeout_ms=100)
    assert error.value.retryable is True


def test_initial_trip_search_reports_access_limit_separately():
    class Locator:
        def inner_text(self, **_kwargs):
            return "Доступ ограничен. Verify you are human."

        def count(self):
            return 0

    class Page:
        def wait_for_function(self, *_args, **_kwargs):
            raise TimeoutError("access challenge")

        def locator(self, _selector):
            return Locator()

    with pytest.raises(TripFlightCompareError, match="Trip.com ограничил обычный поиск"):
        trip_flight_compare._wait_for_initial_trip_results(Page(), timeout_ms=100)


def test_round_trip_matching_accepts_any_airport_in_selected_all_airports_city():
    context = _context()
    outbound = _flight(12036, "2026-10-07", ["DME", "KGD"], 0)
    returning = _flight(12036, "2026-10-14", ["KGD", "SVO"], 0)
    assert match_round_trip(outbound, returning, context)


def test_trip_search_selects_all_airports_for_both_city_fields():
    class Field:
        def __init__(self, page, name):
            self.page = page
            self.name = name

        def filter(self, **_kwargs):
            return self

        def count(self):
            return int(self.page.active_field == self.name)

        @property
        def last(self):
            return self

        def fill(self, value, **_kwargs):
            self.page.typed[self.name] = value

        def click(self):
            self.page.active_field = self.name

    class ClearCity:
        def __init__(self, page, name):
            self.page = page
            self.name = name
            self.first = self

        def count(self):
            return int(bool(self.page.city_groups[self.name]))

        def click(self):
            self.page.city_groups[self.name] = ""

    class CityGroup:
        def __init__(self, page, name):
            self.page = page
            self.name = name
            self.first = self

        def count(self):
            return int(self.name in self.page.city_groups)

        def click(self):
            self.page.active_field = self.name

        def get_by_role(self, role, name, exact=False):
            assert role == "button" and name == "\u0423\u0434\u0430\u043b\u0438\u0442\u044c" and exact
            return ClearCity(self.page, self.name)

    class Suggestion:
        def __init__(self, page):
            self.page = page

        def wait_for(self, **kwargs):
            assert kwargs == {"state": "visible", "timeout": 15000}

        def inner_text(self):
            return f"{self.page.last_city} · \u0412\u0441\u0435 \u0430\u044d\u0440\u043e\u043f\u043e\u0440\u0442\u044b"

        def click(self):
            self.page.selected.append(self.inner_text())
            self.page.city_groups[self.page.active_field] = self.inner_text()

    class Suggestions:
        def __init__(self, page):
            self.page = page
            self.first = Suggestion(page)

        def get_by_role(self, role, name):
            assert role == "button"
            assert name.search("\u0412\u0441\u0435 \u0430\u044d\u0440\u043e\u043f\u043e\u0440\u0442\u044b")
            return self

    class Box:
        def __init__(self, page):
            self.page = page

        def get_by_role(self, role, name):
            if role == "combobox":
                return Field(self.page, name)
            assert role == "group"
            city_name = next(key for key in self.page.city_groups if name.search(key))
            return CityGroup(self.page, city_name)

    class Page:
        def __init__(self):
            self.typed = {}
            self.selected = []
            self.city_groups = {
                "\u0410\u044d\u0440\u043e\u043f\u043e\u0440\u0442 \u0438\u043b\u0438 \u0433\u043e\u0440\u043e\u0434 \u0432\u044b\u043b\u0435\u0442\u0430": "\u0421\u0442\u0430\u0440\u044b\u0439 \u0433\u043e\u0440\u043e\u0434",
                "\u0410\u044d\u0440\u043e\u043f\u043e\u0440\u0442 \u0438\u043b\u0438 \u0433\u043e\u0440\u043e\u0434 \u043f\u0440\u0438\u043b\u0435\u0442\u0430": "\u041f\u0440\u0435\u0436\u043d\u0438\u0439 \u0433\u043e\u0440\u043e\u0434",
            }
            self.active_field = None
            self.box = Box(self)
            self.suggestions = Suggestions(self)

        @property
        def last_city(self):
            return next(reversed(self.typed.values()))

        def locator(self, selector):
            if selector == TRIP_SEARCH_BOX_SELECTOR:
                return self.box
            assert selector == TRIP_CITY_SUGGESTIONS_SELECTOR
            return self.suggestions

    page = Page()
    origin = "\u0410\u044d\u0440\u043e\u043f\u043e\u0440\u0442 \u0438\u043b\u0438 \u0433\u043e\u0440\u043e\u0434 \u0432\u044b\u043b\u0435\u0442\u0430"
    destination = "\u0410\u044d\u0440\u043e\u043f\u043e\u0440\u0442 \u0438\u043b\u0438 \u0433\u043e\u0440\u043e\u0434 \u043f\u0440\u0438\u043b\u0435\u0442\u0430"
    _select_city_all_airports(page, origin, "\u041c\u043e\u0441\u043a\u0432\u0430")
    _select_city_all_airports(page, destination, "\u041a\u0430\u043b\u0438\u043d\u0438\u043d\u0433\u0440\u0430\u0434")

    assert page.typed == {origin: "\u041c\u043e\u0441\u043a\u0432\u0430", destination: "\u041a\u0430\u043b\u0438\u043d\u0438\u043d\u0433\u0440\u0430\u0434"}
    assert page.selected == [
        "\u041c\u043e\u0441\u043a\u0432\u0430 · \u0412\u0441\u0435 \u0430\u044d\u0440\u043e\u043f\u043e\u0440\u0442\u044b",
        "\u041a\u0430\u043b\u0438\u043d\u0438\u043d\u0433\u0440\u0430\u0434 · \u0412\u0441\u0435 \u0430\u044d\u0440\u043e\u043f\u043e\u0440\u0442\u044b",
    ]
    assert all("\u0412\u0441\u0435 \u0430\u044d\u0440\u043e\u043f\u043e\u0440\u0442\u044b" in value for value in page.city_groups.values())

def test_trip_search_discards_reusable_browser_context_after_automation_failure(monkeypatch):
    discarded = []

    class BrokenPage:
        def goto(self, *_args, **_kwargs):
            raise RuntimeError("browser target closed")

        def title(self):
            raise RuntimeError("browser target closed")

        def locator(self, _selector):
            return self

        def inner_text(self):
            raise RuntimeError("browser target closed")

        def count(self):
            return 0

    monkeypatch.setattr(
        trip_flight_compare,
        "_discard_tripcom_reusable_context",
        lambda: discarded.append(True),
    )
    with pytest.raises(TripFlightCompareError, match="Не удалось выполнить поиск"):
        _search_form(BrokenPage(), _context())

    assert discarded == [True]


def test_trip_search_sets_one_adult_and_uses_visible_tripcom_passenger_control():
    class PassengerControl:
        def __init__(self, value):
            self.value = value
            self.visible = True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    class PassengerToggle:
        def __init__(self, visible, counts):
            self.visible = visible
            self.counts = counts
            self.clicks = 0

        def click(self):
            self.clicks += 1

        def inner_text(self):
            adults, children, infants = (control.value for control in self.counts)
            if (adults, children, infants) == ("1", "0", "0"):
                return "1 взрослый, Эконом"
            return f"{adults} взрослых, {children} детей, {infants} младенцев, Эконом"

    class Locator:
        def __init__(self, items):
            self.items = items

        def filter(self, *, visible):
            return Locator([item for item in self.items if item.visible is visible])

        def count(self):
            return len(self.items)

        @property
        def first(self):
            return self.items[0]

    adults = PassengerControl("2")
    children = PassengerControl("1")
    infants = PassengerControl("0")
    counts = (adults, children, infants)
    hidden = PassengerToggle(False, counts)
    visible = PassengerToggle(True, counts)

    class Box:
        def get_by_role(self, role, *, name):
            assert role == "button"
            return Locator([hidden, visible])

    class Page:
        def get_by_role(self, role, *, name):
            assert role == "spinbutton"
            pattern = name.pattern
            control = adults if "взросл" in pattern else children if "дет" in pattern else infants
            return Locator([control])

    trip_flight_compare._set_one_adult_in_economy(Page(), Box())

    assert (adults.value, children.value, infants.value) == ("1", "0", "0")
    assert hidden.clicks == 0
    assert visible.clicks == 2


def test_trip_search_retries_transient_form_failure_in_a_fresh_context(monkeypatch, tmp_path):
    from playwright import sync_api

    contexts = []

    class Page:
        def __init__(self):
            self.closed = False
            self.url = "https://ru.trip.com/flights/list?departure=2026-10-07"

        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            self.closed = True

    class BrowserContext:
        def new_page(self):
            page = Page()
            pages.append(page)
            return page

    pages = []

    def new_context(_profile, _factory):
        context = BrowserContext()
        contexts.append(context)
        return None, context, False

    attempts = []
    result_order = []

    def search_form(_page, _context):
        attempts.append(True)
        if len(attempts) == 1:
            raise TripFlightCompareError("Форма временно не загрузилась.", retryable=True)

    result = _flight(26606, "2026-11-16", ["SVO", "AYT"], 0)
    cheapest = _flight(5700, "2026-10-07", ["DME", "KGD"], 0)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", new_context)
    monkeypatch.setattr(trip_flight_compare, "_search_form", search_form)
    monkeypatch.setattr(trip_flight_compare, "_load_more_results", lambda *_args: result_order.append("expand"))
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: result_order.append("sort_cheapest"))
    monkeypatch.setattr(trip_flight_compare, "_result_cards", lambda *_args: [(result, None), (cheapest, None)])

    actual = trip_flight_compare._tripcom_search(_context(trip_type="OW", stops=(0,)), tmp_path / "profile")

    assert actual["price"] == 5700
    assert actual["outbound"] is cheapest
    assert actual["tripcom_url"] == "https://ru.trip.com/flights/list?departure=2026-10-07"
    assert len(attempts) == 2
    assert len(contexts) == 2
    assert len(pages) == 2
    assert all(page.closed for page in pages)
    assert result_order == ["sort_cheapest"]


def test_trip_search_discards_context_before_a_manual_retry(monkeypatch, tmp_path):
    from playwright import sync_api

    class Page:
        closed = False

        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            self.closed = True

    page = Page()
    discarded = []
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", lambda *_args: (None, object(), False))
    monkeypatch.setattr(
        trip_flight_compare, "_tripcom_new_page_with_recovery",
        lambda context, reused, profile, factory: (page, context, reused),
    )
    monkeypatch.setattr(
        trip_flight_compare, "_search_form",
        lambda *_args: (_ for _ in ()).throw(TripFlightCompareError("passenger setup failed")),
    )
    monkeypatch.setattr(
        trip_flight_compare, "_discard_tripcom_reusable_context",
        lambda: discarded.append(True),
    )

    with pytest.raises(TripFlightCompareError, match="passenger setup failed"):
        trip_flight_compare._tripcom_search(_context(), tmp_path / "profile")

    assert discarded == [True]
    assert page.closed


def test_trip_search_retries_browser_start_failure_in_a_fresh_context(monkeypatch, tmp_path, caplog):
    from playwright import sync_api

    pages = []

    class Page:
        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            pass

    class BrowserContext:
        def new_page(self):
            page = Page()
            pages.append(page)
            return page

    starts = 0
    discarded = []

    def new_context(_profile, _factory):
        nonlocal starts
        starts += 1
        if starts == 1:
            raise RuntimeError("transient browser transport failure")
        return None, BrowserContext(), False

    result = _flight(6100, "2026-10-07", ["SVO", "KGD"], 0)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", new_context)
    monkeypatch.setattr(trip_flight_compare, "_discard_tripcom_reusable_context",
                        lambda: discarded.append(True))
    monkeypatch.setattr(trip_flight_compare, "_search_form", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_load_more_results", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_result_cards", lambda *_args: [(result, None)])

    actual = trip_flight_compare._tripcom_search(_context(trip_type="OW", stops=(0,)), tmp_path / "profile")

    assert actual["price"] == 6100
    assert starts == 2
    assert len(pages) == 1
    assert discarded == [True, True]
    assert "transient browser transport failure" in caplog.text
    assert str(tmp_path / "profile") not in caplog.text


def test_trip_search_reports_browser_start_failure_after_one_fresh_retry(monkeypatch, tmp_path, caplog):
    from playwright import sync_api

    starts = 0

    def new_context(_profile, _factory):
        nonlocal starts
        starts += 1
        raise RuntimeError("browser profile is temporarily unavailable")

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", new_context)
    monkeypatch.setattr(trip_flight_compare, "_discard_tripcom_reusable_context", lambda: None)

    with pytest.raises(TripFlightCompareError, match="Браузер Trip.com не запустился"):
        trip_flight_compare._tripcom_search(_context(trip_type="OW", stops=(0,)), tmp_path / "profile")

    assert starts == 2
    assert "stage=browser_start" in caplog.text
    assert "browser profile is temporarily unavailable" in caplog.text


def test_trip_search_retries_when_results_page_closes_after_search(monkeypatch, tmp_path, caplog):
    from playwright import sync_api

    class TargetClosedError(Exception):
        pass

    pages = []
    contexts = []

    class Page:
        def __init__(self, attempt):
            self.attempt = attempt
            self.closed = False

        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            self.closed = True

    class BrowserContext:
        def __init__(self, attempt):
            self.attempt = attempt

        def new_page(self):
            page = Page(self.attempt)
            pages.append(page)
            return page

    starts = 0

    def new_context(_profile, _factory):
        nonlocal starts
        starts += 1
        context = BrowserContext(starts)
        contexts.append(context)
        return None, context, False

    result = _flight(6100, "2026-10-07", ["SVO", "KGD"], 0)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", new_context)
    monkeypatch.setattr(trip_flight_compare, "_search_form", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_flight_url", lambda *_args: "https://ru.trip.com/flights/list")
    def result_cards(page, *_args):
        if page.attempt == 1:
            raise TargetClosedError("Target page, context or browser has been closed")
        return [(result, None)]

    monkeypatch.setattr(trip_flight_compare, "_result_cards", result_cards)

    actual = trip_flight_compare._tripcom_search(_context(trip_type="OW", stops=(0,)), tmp_path / "profile")

    assert actual["price"] == 6100
    assert starts == 2
    assert len(contexts) == 2
    assert all(page.closed for page in pages)
    assert "reason=target_closed" in caplog.text


def test_trip_search_retries_when_driver_connection_closes_while_parsing(monkeypatch, tmp_path, caplog):
    from playwright import sync_api

    pages = []

    class Page:
        def __init__(self, attempt):
            self.attempt = attempt
            self.closed = False

        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            self.closed = True

    class BrowserContext:
        def __init__(self, attempt):
            self.attempt = attempt

        def new_page(self):
            page = Page(self.attempt)
            pages.append(page)
            return page

    starts = 0

    def new_context(_profile, _factory):
        nonlocal starts
        starts += 1
        return None, BrowserContext(starts), False

    result = _flight(6100, "2026-10-07", ["SVO", "KGD"], 0)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", new_context)
    monkeypatch.setattr(trip_flight_compare, "_search_form", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: None)

    def result_cards(page, *_args):
        if page.attempt == 1:
            raise Exception("Locator.inner_text: Connection closed while reading from the driver")
        return [(result, None)]

    monkeypatch.setattr(trip_flight_compare, "_result_cards", result_cards)

    actual = trip_flight_compare._tripcom_search(_context(trip_type="OW", stops=(0,)), tmp_path / "profile")

    assert actual["price"] == 6100
    assert starts == 2
    assert len(pages) == 2
    assert all(page.closed for page in pages)
    assert "reason=target_closed" in caplog.text


def test_trip_search_returns_cheapest_visible_round_trip_card_when_action_is_missing(monkeypatch, tmp_path, caplog):
    from playwright import sync_api

    class EmptyLocator:
        def count(self):
            return 0

    class Card:
        def get_by_role(self, *_args, **_kwargs):
            return EmptyLocator()

    class Page:
        url = "https://ru.trip.com/flights/list?date=2026-11-16"

        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            pass

        def locator(self, _selector):
            return self

    page = Page()

    class BrowserContext:
        def new_page(self):
            return page

    context = BrowserContext()
    result = _flight(26606, "2026-11-16", ["SVO", "AYT"], 0)
    card = Card()

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context",
                        lambda *_args: (None, context, False))
    monkeypatch.setattr(trip_flight_compare, "_search_form", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_result_cards", lambda *_args: [(result, card)])
    monkeypatch.setattr(trip_flight_compare, "_tripcom_flight_url",
                        lambda *_args: "https://ru.trip.com/flights/list?date=2026-11-16")

    context_for_route = _context()
    context_for_route.update({
        "destination": "AYT", "departure_date": "2026-11-16",
        "return_date": "2026-11-22", "stops": [0, 1],
    })
    actual = trip_flight_compare._tripcom_search(context_for_route, tmp_path / "profile")

    assert actual["price"] == 26606
    assert actual["outbound"]["price"] == 26606
    assert actual["return"] is None
    assert actual["tripcom_url"].startswith("https://ru.trip.com/flights/")


def test_successful_trip_search_releases_context_before_next_offer(monkeypatch, tmp_path):
    """A second offer must start with a fresh Sync Playwright context."""
    from playwright import sync_api

    class Page:
        url = "https://ru.trip.com/flights/list?date=2026-11-16"

        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            pass

    class BrowserContext:
        def new_page(self):
            return Page()

    starts = []
    discarded = []

    def new_context(_profile, _factory):
        starts.append(True)
        return None, BrowserContext(), False

    result = _flight(6100, "2026-10-07", ["SVO", "KGD"], 0)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(trip_flight_compare, "_tripcom_reusable_context", new_context)
    monkeypatch.setattr(
        trip_flight_compare, "_discard_tripcom_reusable_context",
        lambda: discarded.append(True),
    )
    monkeypatch.setattr(trip_flight_compare, "_search_form", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_result_cards", lambda *_args: [(result, None)])

    context = _context(trip_type="OW", stops=(0,))
    first = trip_flight_compare._tripcom_search(context, tmp_path / "profile")
    second = trip_flight_compare._tripcom_search(context, tmp_path / "profile")

    assert first["price"] == second["price"] == 6100
    assert len(starts) == 2
    assert discarded == [True, True]


def test_compare_trip_offer_runs_sync_playwright_on_executor_thread(monkeypatch):
    caller_thread = threading.get_ident()
    observed = {}

    def fake_search(context, _profile):
        observed["context"] = context
        observed["thread"] = threading.get_ident()
        observed["thread_name"] = threading.current_thread().name
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return {"price": 12345}

    monkeypatch.setattr(trip_flight_compare, "_extract_route_context", lambda _offer: {"test": True})
    monkeypatch.setattr(trip_flight_compare, "_tripcom_search", fake_search)

    async def check():
        return await trip_flight_compare.compare_trip_offer({"offer_id": 1})

    assert asyncio.run(check()) == {"price": 12345}
    assert observed["context"] == {"test": True}
    assert observed["thread"] != caller_thread
    assert observed["thread_name"].startswith("tripcom-browser")


def test_real_sync_playwright_starts_inside_tripcom_executor_from_async_loop():
    from playwright.sync_api import sync_playwright
    from aviasales_collector.hotels.sources.tripcom import TRIP_EXECUTOR

    async def check():
        loop = asyncio.get_running_loop()

        def start_and_stop():
            with sync_playwright() as playwright:
                # Let the Playwright driver finish its initial connection task
                # before stopping it, so the test leaves no dangling loop task.
                time.sleep(0.05)
                return threading.current_thread().name, playwright.chromium.name

        return await loop.run_in_executor(TRIP_EXECUTOR, start_and_stop)

    thread_name, browser_name = asyncio.run(check())

    assert thread_name.startswith("tripcom-browser")
    assert browser_name == "chromium"


@pytest.mark.parametrize(
    ("include_second_outbound",),
    [(True,), (False,)],
)
def test_round_trip_search_returns_cheapest_eligible_sorted_card(
    monkeypatch, tmp_path, include_second_outbound,
):
    from playwright import sync_api

    context = _context(stops=(0, 1))
    first_outbound = _flight(20000, "2026-10-07", ["SVO", "KGD"], 0)
    second_outbound = _flight(25000, "2026-10-07", ["DME", "KGD"], 0)
    third_outbound = _flight(23000, "2026-10-08", ["SVO", "KGD"], 0)

    class Card:
        pass

    class Page:
        def set_default_timeout(self, _timeout):
            pass

        def on(self, *_args):
            pass

        def close(self):
            pass

        url = "https://ru.trip.com/flights/showfarefirst?dcity=led"

    page = Page()

    class BrowserContext:
        def new_page(self):
            return page

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: None)
    monkeypatch.setattr(
        trip_flight_compare, "_tripcom_reusable_context",
        lambda *_args: (None, BrowserContext(), False),
    )
    monkeypatch.setattr(
        trip_flight_compare, "_tripcom_new_page_with_recovery",
        lambda _context, _reused, _profile, _factory: (page, BrowserContext(), False),
    )
    monkeypatch.setattr(trip_flight_compare, "_search_form", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_load_more_results", lambda *_args: None)
    monkeypatch.setattr(trip_flight_compare, "_sort_cheapest", lambda *_args: None)

    outbounds = [(first_outbound, Card())]
    if include_second_outbound:
        outbounds.append((second_outbound, Card()))
    outbounds.append((third_outbound, Card()))
    monkeypatch.setattr(trip_flight_compare, "_result_cards", lambda *_args: outbounds)

    actual = trip_flight_compare._tripcom_search(context, tmp_path / "profile")

    # The cheapest eligible card (20000) wins; no return-leg page is visited.
    assert actual["price"] == 20000
    assert actual["outbound"]["price"] == first_outbound["price"]
    assert actual["outbound"]["airport_chain"] == first_outbound["airport_chain"]
    assert actual["return"] is None
    assert actual["trip_type"] == "RT"
    assert actual["tripcom_url"] == "https://ru.trip.com/flights/showfarefirst?dcity=led"


def test_round_trip_matching_accepts_same_or_better_stops_and_requires_exact_dates_routes():
    context = _context(stops=(0, 1))
    outbound = _flight(16543, "2026-10-07", ["SVO", "KGD"], 0)
    returning = _flight(16543, "2026-10-14", ["KGD", "SAW", "VKO"], 1)
    assert match_round_trip(outbound, returning, context)
    assert match_round_trip(outbound, _flight(16800, "2026-10-14", ["KGD", "SAW", "VKO"], 1), context)
    assert match_round_trip(outbound, _flight(16543, "2026-10-14", ["KGD", "VKO"], 0), context)
    assert not match_round_trip(outbound, _flight(16543, "2026-10-15", ["KGD", "SAW", "VKO"], 1), context)
    assert not match_round_trip(outbound, _flight(16543, "2026-10-14", ["KGD", "SAW", "IST", "VKO"], 2), context)
    assert not match_round_trip(outbound, _flight(16543, "2026-10-14", ["KGD", "SAW", "LED"], 1), context)
    wrong_currency = _flight(16543, "2026-10-14", ["KGD", "SAW", "VKO"], 1)
    wrong_currency["currency"] = "USD"
    assert not match_round_trip(outbound, wrong_currency, context)


def test_round_trip_matching_accepts_dom_stop_count_when_transfer_airport_is_not_shown():
    context = _context(stops=(0, 1))
    outbound = _flight(34000, "2026-10-07", ["SVO", "KGD"], 0)
    returning = _flight(34000, "2026-10-14", ["KGD", "VKO"], 1)
    returning["airport_chain_complete"] = False
    returning["stopover_cities"] = ["Стамбул"]

    assert match_round_trip(outbound, returning, context)
    returning["stops"] = 2
    assert not match_round_trip(outbound, returning, context)


def test_one_way_matching_rejects_return_and_wrong_stop_count():
    context = _context(trip_type="OW", stops=(0,))
    assert match_one_way(_flight(6000, "2026-10-07", ["DME", "KGD"], 0), context)
    assert not match_one_way(_flight(6000, "2026-10-08", ["DME", "KGD"], 0), context)
    assert not match_one_way(_flight(6000, "2026-10-07", ["DME", "LED", "KGD"], 1), context)


def test_one_way_matching_accepts_fewer_stops_than_saved_offer():
    context = _context(trip_type="OW", stops=(1,))
    assert match_one_way(_flight(6000, "2026-10-07", ["DME", "KGD"], 0), context)
    assert not match_one_way(_flight(6000, "2026-10-07", ["DME", "LED", "SVO", "KGD"], 2), context)


def _scan(offer):
    return {
        "origin": "LED", "trip_type": "RT", "duration_min_days": 3, "duration_max_days": 11,
        "complete": True, "cards_found": 1, "unique_destinations": 1,
        "offers": [offer],
    }


def _valid_offer():
    return {
        "origin": "LED", "destination": "Дубай", "price": 34747, "currency": "RUB",
        "departure_date": "2026-10-07", "return_date": "2026-10-14",
        "source_url": "https://www.aviasales.ru/search/LED0710DXB14101",
        "airports": [{"code": code} for code in ("LED", "DXB", "DXB", "LED")],
        "stops": [0, 0],
    }


def test_compare_endpoint_uses_only_owned_offer_id_and_shared_result(monkeypatch, tmp_path):
    path = tmp_path / "compare.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(10, -10, _scan(_valid_offer()))
        offer_id = store.get_scan_offers(10, -10, saved.scan_id)[0]["offer_id"]
        store.save_origin_label(10, -10, "LED", "Санкт-Петербург")
        private_scan = store.record_scan(99, -99, _scan(_valid_offer()))
        private_id = store.get_scan_offers(99, -99, private_scan.scan_id)[0]["offer_id"]

    async def fake_compare(offer):
        assert offer["offer_id"] == offer_id
        assert offer["route_identity"]["actual_destination"] == "DXB"
        return {
            "price": 31000, "currency": "RUB", "trip_type": "RT", "passengers": 1,
            "tripcom_url": "https://ru.trip.com/flights/ShowFareNext?fare=cheapest",
            "outbound": {"airport_chain": ["LED", "DXB"], "stops": 0},
            "return": {"airport_chain": ["DXB", "LED"], "stops": 0},
            "elapsed_seconds": 2.4,
        }

    monkeypatch.setattr(web, "compare_trip_offer", fake_compare)

    async def check():
        headers = {"Origin": "http://127.0.0.1:8765"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.create_app(path, frozenset({10}))),
            base_url="http://127.0.0.1:8765",
        ) as client:
            response = await client.post("/api/flights/compare-trip", headers=headers, json={"offer_id": offer_id})
            assert response.status_code == 200
            result = response.json()
            assert result["card_price"] == 34747
            assert result["trip"]["price"] == 31000
            assert result["trip"]["tripcom_url"] == "https://ru.trip.com/flights/ShowFareNext?fare=cheapest"
            assert result["passengers"] == 1
            assert (await client.post("/api/flights/compare-trip", headers=headers,
                                     json={"offer_id": offer_id, "url": "https://example.com"})).status_code == 422
            assert (await client.post("/api/flights/compare-trip", headers=headers,
                                     json={"offer_id": private_id})).status_code == 404
            assert (await client.post("/api/flights/compare-trip", json={"offer_id": offer_id})).status_code == 403

    asyncio.run(check())


def test_compare_endpoint_accepts_a_new_request_after_transient_failure(monkeypatch, tmp_path):
    path = tmp_path / "compare-retry.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(10, -10, _scan(_valid_offer()))
        offer_id = store.get_scan_offers(10, -10, saved.scan_id)[0]["offer_id"]
        store.save_origin_label(10, -10, "LED", "Санкт-Петербург")

    calls = 0

    async def fake_compare(_offer):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TripFlightCompareError("Форма Trip.com временно не загрузилась.", retryable=True)
        return {
            "price": 31000, "currency": "RUB", "trip_type": "RT", "passengers": 1,
            "outbound": {"airport_chain": ["LED", "DXB"], "stops": 0},
            "return": {"airport_chain": ["DXB", "LED"], "stops": 0},
            "elapsed_seconds": 2.4,
        }

    monkeypatch.setattr(web, "compare_trip_offer", fake_compare)

    async def check():
        headers = {"Origin": "http://127.0.0.1:8765"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.create_app(path, frozenset({10}))),
            base_url="http://127.0.0.1:8765",
        ) as client:
            failed = await client.post("/api/flights/compare-trip", headers=headers, json={"offer_id": offer_id})
            assert failed.status_code == 503
            assert failed.json()["detail"] == "Форма Trip.com временно не загрузилась."
            retried = await client.post("/api/flights/compare-trip", headers=headers, json={"offer_id": offer_id})
            assert retried.status_code == 200
            assert retried.json()["trip"]["price"] == 31000
            assert calls == 2

    asyncio.run(check())


def test_trip_comparison_reads_real_database_route_identity_read_only(tmp_path):
    path = tmp_path / "compare-read.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(10, -10, _scan(_valid_offer()))
        offer_id = store.get_scan_offers(10, -10, saved.scan_id)[0]["offer_id"]
        store.save_origin_label(10, -10, "LED", "Санкт-Петербург")
    offer = web._load_trip_compare_offer(path, (10,), offer_id)
    context = build_offer_context(offer)
    assert context["origin_city"] == "Санкт-Петербург"
    assert context["destination_city"] == "Дубай"
    assert context["stops"] == [0, 0]
    assert context["departure_date"] == "2026-10-07"
    assert context["return_date"] == "2026-10-14"
