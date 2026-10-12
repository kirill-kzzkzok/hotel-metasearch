from __future__ import annotations

from datetime import date

import pytest

from aviasales_collector import collector


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value


@pytest.mark.parametrize("url", [
    "https://www.aviasales.ru/hottickets/mow",
    "https://aviasales.ru/hottickets/mow?foo=bar",
    "https://www.aviasales.ru:443/hottickets/mow",
])
def test_collector_accepts_only_expected_ticket_origin_urls(url):
    assert collector._is_expected_hot_tickets_url(url, "MOW")


@pytest.mark.parametrize("url", [
    "https://aviasales.ru.attacker.example/hottickets/mow",
    "https://aviasales.ru@attacker.example/hottickets/mow",
    "http://www.aviasales.ru/hottickets/mow",
    "https://www.aviasales.ru:444/hottickets/mow",
    "https://www.aviasales.ru/hottickets/led",
])
def test_collector_rejects_untrusted_or_wrong_ticket_origin_urls(url):
    assert not collector._is_expected_hot_tickets_url(url, "MOW")


class _TextLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text


class _StablePage:
    def __init__(self, trip_label, count=2, rt_ready=True, expected_range=(1, 30)):
        self.trip_label = trip_label
        self.card_count = count
        self.rt_ready = rt_ready
        self.clock = _Clock()
        self.expected_range = expected_range

    def locator(self, selector):
        if selector == collector.TRIP_SELECTOR:
            return _TextLocator(self.trip_label)
        return self

    def count(self):
        return self.card_count

    def nth(self, _index):
        return _TextLocator("Анталья\n12 345 ₽\n5 окт")

    def evaluate_all(self, _expression, arg):
        assert arg == {"min": self.expected_range[0], "max": self.expected_range[1]}
        return self.rt_ready

    def wait_for_timeout(self, milliseconds):
        self.clock.value += milliseconds / 1000


def test_rt_stability_requires_every_card_to_match_selected_duration_range(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(collector, "time", clock)
    page = _StablePage(collector._trip_label("RT"), rt_ready=False, expected_range=(3, 8))
    page.clock = clock

    with pytest.raises(collector.CollectorError, match="did not stabilize"):
        collector._wait_for_cards_stable(page, "RT", 3, 8, timeout_ms=2_000)


def test_one_way_stability_does_not_require_round_trip_evidence(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(collector, "time", clock)
    page = _StablePage(collector._trip_label("OW"), rt_ready=False)
    page.clock = clock

    collector._wait_for_cards_stable(page, "OW", timeout_ms=5_000)


class _Mouse:
    def __init__(self):
        self.wheels = 0

    def wheel(self, _x, _y):
        self.wheels += 1


class _CardLocator:
    def __init__(self, cards, ticket_count):
        self.cards = cards
        self.ticket_count = ticket_count

    def count(self):
        return self.ticket_count if "hot-ticket" in self.cards.selector else len(self.cards.raw_cards)

    def evaluate_all(self, _expression):
        return self.cards.raw_cards


class _CardCollection:
    def __init__(self, raw_cards, ticket_count):
        self.raw_cards = raw_cards
        self.ticket_count = ticket_count
        self.selector = collector.CARD_SELECTOR


class _ScrollPage:
    def __init__(self, raw_cards, ticket_count):
        self.cards = _CardCollection(raw_cards, ticket_count)
        self.mouse = _Mouse()
        self.scrolls = 0

    def locator(self, selector):
        if "hot-ticket" in selector:
            self.cards.selector = selector
        else:
            self.cards.selector = collector.CARD_SELECTOR
        return _CardLocator(self.cards, self.cards.ticket_count)

    def evaluate(self, expression, argument=None):
        if isinstance(argument, dict):
            assert "overflowY" in expression
            assert "scrollBy" in expression
            self.scrolls += 1
        return {"top": 0, "height": 900, "viewport": 900}

    def wait_for_timeout(self, _milliseconds):
        pass


class _Button:
    def __init__(self, visible=True):
        self.visible = visible
        self.first = self

    def count(self):
        return int(self.visible)

    def is_visible(self):
        return self.visible


class _MoreTicketsCard:
    def __init__(self, dynamic=None, legacy=None):
        self.dynamic = dynamic or _Button(False)
        self.legacy = legacy or _Button(False)

    def locator(self, selector):
        assert selector == collector.MORE_TICKETS_SELECTOR
        return self.dynamic

    def get_by_text(self, _text, exact=False):
        assert exact
        return self.legacy


class _WaitPage:
    def wait_for_timeout(self, _milliseconds):
        pass


def _raw_card(destination, route):
    return {
        "card_text": f"{destination}\n5 окт\nПрямой\n4 ч в пути",
        "destination": destination,
        "source_url": f"https://www.aviasales.ru/search/{route}",
        "price_texts": ["12 345 ₽"],
        "text_items": ["5 окт", "4 ч в пути / Прямой"],
        "date_items": ["5 окт"],
        "airlines": ["Pobeda"],
    }


def test_more_tickets_uses_stable_selector_for_dynamic_button_label():
    button = _Button()
    card = _MoreTicketsCard(dynamic=button)

    assert collector._wait_for_more_tickets(_WaitPage(), card, "Гонконг") is button


def test_more_tickets_keeps_compatibility_with_legacy_label():
    button = _Button()
    card = _MoreTicketsCard(legacy=button)

    assert collector._wait_for_more_tickets(_WaitPage(), card, "Гонконг") is button


def test_wait_for_destination_card_scrolls_lazy_loaded_destination(monkeypatch):
    class DestinationLocator:
        def __init__(self, found):
            self.found = found

        def count(self):
            return 1 if self.found else 0

        def nth(self, _index):
            class Card:
                def locator(self, _selector):
                    class Title:
                        def __init__(self):
                            self.first = self

                        def count(self):
                            return 1

                        def inner_text(self):
                            return "Нячанг"

                    return Title()

            return Card()

    class DestinationPage:
        def __init__(self):
            self.scroll_count = 0

        def locator(self, _selector):
            return DestinationLocator(self.scroll_count >= 2)

        def evaluate(self, _script, _destination):
            self.scroll_count += 1
            return {"top": self.scroll_count, "max": 10, "count": 1, "moved": True}

        def wait_for_timeout(self, _milliseconds):
            pass

    page = DestinationPage()
    selected = collector._wait_for_destination_card(page, "Нячанг", timeout_ms=1_000)

    assert selected is not None
    assert page.scroll_count == 2


def test_ensure_deep_page_reopens_closed_page_without_relaunching_context():
    class ReopenedPage:
        def set_default_timeout(self, _timeout):
            pass

        def set_default_navigation_timeout(self, _timeout):
            pass

    class Context:
        def __init__(self):
            self.created = 0

        def is_closed(self):
            return False

        def new_page(self):
            self.created += 1
            return ReopenedPage()

    class ClosedPage:
        def is_closed(self):
            return True

    context = Context()
    browser = object()
    new_browser, new_context, page = collector._ensure_deep_page(
        object(), "MOW", True, browser, context, ClosedPage()
    )

    assert new_browser is browser
    assert new_context is context
    assert context.created == 1
    assert isinstance(page, ReopenedPage)


def test_missing_more_button_collects_visible_summary_offers_or_successful_empty():
    class SummaryCard:
        def __init__(self, offers):
            self.offers = offers

        def evaluate(self, _expression):
            return self.offers

    offers, count, complete, warnings = collector._collect_summary_card_offers(
        SummaryCard([_raw_card("Гонконг", "MOW2809HKG1")]), "MOW", "OW", "Гонконг"
    )
    assert count == 1
    assert complete is True
    assert warnings == []
    assert offers[0]["destination"] == "Гонконг"

    offers, count, complete, warnings = collector._collect_summary_card_offers(
        SummaryCard([]), "MOW", "OW", "Гонконг"
    )
    assert offers == []
    assert count == 0
    assert complete is True
    assert warnings == []


def test_scroll_waits_for_stable_bottom_without_new_cards():
    cards = [_raw_card("Анталья", "MOW0510AYT1"), _raw_card("Стамбул", "MOW0510IST1")]
    page = _ScrollPage(cards, ticket_count=2)

    offers, dom_count, complete, warnings = collector._scroll_collect(page, "MOW", "OW")

    assert len(offers) == 2
    assert dom_count == 2
    assert complete is True
    assert warnings == []
    assert page.scrolls == collector.STABLE_BOTTOM_PASSES


def test_destination_scroll_uses_hot_ticket_links_and_stable_bottom():
    cards = [_raw_card("Анталья", "MOW0510AYT1"), _raw_card("Анталья", "MOW0610AYT2")]
    page = _ScrollPage(cards, ticket_count=2)

    offers, dom_count, complete, warnings = collector._scroll_collect_destination(
        page, "MOW", "OW", "Анталья"
    )

    assert len(offers) == 2
    assert all(offer["destination"] == "Анталья" for offer in offers)
    assert dom_count == 2
    assert complete is True
    assert warnings == []
    assert page.scrolls == collector.STABLE_BOTTOM_PASSES


def test_destination_scroll_collects_cards_revealed_by_scrolling_the_result_container():
    first = _raw_card("Нячанг", "MOW2909CXR07101")
    second = _raw_card("Нячанг", "MOW2909CXR04101")
    page = _ScrollPage([first], ticket_count=1)

    original_evaluate = page.evaluate

    def evaluate(expression, argument=None):
        result = original_evaluate(expression, argument)
        if isinstance(argument, dict) and page.scrolls == 1:
            page.cards.raw_cards.append(second)
            page.cards.ticket_count = 2
        return result

    page.evaluate = evaluate
    offers, dom_count, complete, warnings = collector._scroll_collect_destination(
        page, "MOW", "RT", "Нячанг"
    )

    assert len(offers) == 2
    assert dom_count == 2
    assert complete is True
    assert warnings == []


def test_destination_scroll_is_incomplete_when_dom_has_unparsed_ticket_links():
    cards = [_raw_card("Анталья", "MOW0510AYT1")]
    page = _ScrollPage(cards, ticket_count=2)

    offers, dom_count, complete, warnings = collector._scroll_collect_destination(
        page, "MOW", "OW", "Анталья"
    )

    assert len(offers) == 1
    assert dom_count == 2
    assert complete is False
    assert any("only 1 were collected" in warning for warning in warnings)


def test_destination_scroll_rejects_foreign_global_tickets_in_dubai_expansion():
    cards = []
    for code, destination in [('MOW', 'Москва'), ('DXB', 'Дубай'), ('KUF', 'Самара'), ('KZN', 'Казань')]:
        data = _raw_card(destination, f'LED3009{code}04101')
        data['card_text'] = f'на 4 дня\n30 сен\nLED Пулково\n{code} Аэропорт\n4 окт\n{code} Аэропорт\nLED Пулково'
        data['date_items'] = ['30 сен', '4 окт']
        cards.append(data)
    page = _ScrollPage(cards, ticket_count=4)
    offers, count, complete, warnings = collector._scroll_collect_destination(
        page, 'LED', 'RT', 'Дубай', 'DXB'
    )
    assert len(offers) == count == 1
    assert offers[0]['route_identity']['actual_destination'] == 'DXB'
    assert complete and not warnings


class _FakePage:
    def __init__(self):
        self.url = ""
        self.reload_count = 0

    def set_default_timeout(self, _milliseconds):
        pass

    def set_default_navigation_timeout(self, _milliseconds):
        pass

    def goto(self, url, **_kwargs):
        self.url = url

    def reload(self, **_kwargs):
        self.reload_count += 1

    def locator(self, _selector):
        return _VisibleTripChip()


class _VisibleTripChip:
    def wait_for(self, **_kwargs):
        pass

    def inner_text(self):
        return collector._trip_label("OW")


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()

    def new_page(self):
        return self.page

    def close(self):
        pass


class _FailingCloseContext(_FakeContext):
    def close(self):
        raise RuntimeError("context close failed")


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()

    def new_context(self, **_kwargs):
        return self.context

    def close(self):
        pass


class _FailingCloseBrowser(_FakeBrowser):
    def close(self):
        raise RuntimeError("browser close failed")


class _FakePlaywright:
    def __init__(self):
        self.chromium = self
        self.browser = _FakeBrowser()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def launch(self, **_kwargs):
        return self.browser


def _install_fake_collector_runtime(monkeypatch, scroll_result):
    playwright = _FakePlaywright()
    settings_applied = []
    monkeypatch.setattr(collector, "sync_playwright", lambda: playwright)
    monkeypatch.setattr(collector, "_dismiss_site_prompts", lambda _page: None)
    monkeypatch.setattr(collector, "_wait_for_cards_stable", lambda *_args: None)
    monkeypatch.setattr(
        collector,
        "_apply_trip_settings",
        lambda page, trip, low, high: settings_applied.append((page.reload_count, trip, low, high)),
    )
    monkeypatch.setattr(collector, "_scroll_collect", lambda *_args: scroll_result)
    return playwright, settings_applied


def _valid_rt_offer():
    return {
        "origin": "MOW",
        "destination": "Анталья",
        "price": 12345,
        "currency": "RUB",
        "source_url": "https://www.aviasales.ru/search/MOW0510AYT1",
        "departure_date_raw": "5 окт",
        "return_date_raw": "12 окт",
        "trip_duration_days": 7,
        "warnings": [],
    }


def test_rt_settings_are_reapplied_after_refresh_resets_to_one_way(monkeypatch):
    offer = _valid_rt_offer()
    _, settings_applied = _install_fake_collector_runtime(
        monkeypatch, ([offer], 1, True, [])
    )

    result = collector.collect("MOW", "RT", 3, 8)

    assert settings_applied == [(0, "RT", 3, 8), (1, "RT", 3, 8)]
    assert result["duration_filter_verified"] is True
    assert result["complete"] is True


def test_successful_scan_keeps_result_when_browser_cleanup_reports_errors(monkeypatch):
    offer = _valid_rt_offer()
    playwright = _FakePlaywright()
    playwright.browser.context = _FailingCloseContext()
    playwright.browser = _FailingCloseBrowser()
    playwright.browser.context = _FailingCloseContext()
    monkeypatch.setattr(collector, "sync_playwright", lambda: playwright)
    monkeypatch.setattr(collector, "_dismiss_site_prompts", lambda _page: None)
    monkeypatch.setattr(collector, "_wait_for_cards_stable", lambda *_args: None)
    monkeypatch.setattr(collector, "_apply_trip_settings", lambda *_args: None)
    monkeypatch.setattr(collector, "_scroll_collect", lambda *_args: ([offer], 1, True, []))

    result = collector.collect("MOW", "RT", 3, 8)

    assert result["complete"] is True
    assert any("context close failed" in warning for warning in result["warnings"])
    assert any("browser close failed" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("offers", "ticket_count", "expected_warning"),
    [
        ([], 0, "no offers were collected"),
        ([_valid_rt_offer()], 2, "final DOM has 2 hot-ticket links"),
    ],
)
def test_empty_or_dom_mismatched_scan_is_never_complete(monkeypatch, offers, ticket_count, expected_warning):
    _install_fake_collector_runtime(monkeypatch, (offers, ticket_count, True, []))

    result = collector.collect("MOW", "RT", 3, 8)

    assert result["complete"] is False
    assert any(expected_warning in warning for warning in result["warnings"])
