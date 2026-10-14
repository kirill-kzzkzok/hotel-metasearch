from __future__ import annotations

from aviasales_collector.hotels.sources import tripcom


def test_empty_trip_inventory_shell_is_incomplete():
    hotels = [{"id": str(index)} for index in range(5)]
    assert tripcom._tripcom_empty_inventory_shell(hotels, 4979, {}) is True
    assert tripcom._tripcom_empty_inventory_shell(
        [{"id": str(index)} for index in range(12)],
        4979,
        {"inventory_requests": [
            {"http_status": 200, "response_bytes": 434,
             "response_has_data": False},
            {"http_status": 200, "response_bytes": 434,
             "response_has_data": False},
        ]},
    ) is True


def test_trip_inventory_payload_proves_shell_can_continue():
    hotels = [{"id": str(index)} for index in range(12)]
    assert tripcom._tripcom_empty_inventory_shell(
        hotels,
        4979,
        {"inventory_requests": [
            {"http_status": 200, "response_bytes": 45178,
             "response_has_data": True},
        ]},
    ) is False


def test_empty_inventory_requests_without_reported_total_are_incomplete():
    """Recovery must not promote a ResponseStatus-only retry to ``ok``."""
    hotels = [{"id": str(index)} for index in range(12)]
    audit = {
        "inventory_requests": [
            {"http_status": 200, "response_bytes": 433,
             "response_top_level_keys": ["ResponseStatus"],
             "response_has_data": False},
        ],
    }

    assert tripcom._tripcom_empty_inventory_shell(hotels, None, audit) is True
    assert tripcom._tripcom_empty_inventory_shell(hotels, len(hotels), audit) is True

    # This is the status transition performed by the recovery path after the
    # shell check.  It must remain partial even though the DOM has cards and
    # the scroll loop reached stable_end.
    collection_complete = True
    collection_reason = "stable_end"
    if tripcom._tripcom_empty_inventory_shell(hotels, None, audit):
        collection_complete = False
        collection_reason = "inventory_response_empty"
    status = "ok" if hotels and collection_complete else "partial" if hotels else "parse_error"
    assert status == "partial"
    assert collection_complete is False
    assert collection_reason == "inventory_response_empty"


def test_small_dom_shell_without_total_or_inventory_is_incomplete():
    """A five-card shell with no source evidence is not a complete search."""
    hotels = [{"id": str(index)} for index in range(5)]
    assert tripcom._tripcom_empty_inventory_shell(hotels, None, {}) is True


def test_valid_inventory_then_empty_response_preserves_terminal_completion():
    """A real inventory payload keeps the existing natural-end behavior."""
    hotels = [{"id": str(index)} for index in range(20)]
    audit = {
        "inventory_requests": [
            {"http_status": 200, "response_bytes": 45178,
             "response_has_data": True},
            {"http_status": 200, "response_bytes": 433,
             "response_top_level_keys": ["ResponseStatus"],
             "response_has_data": False},
        ],
    }

    assert tripcom._tripcom_empty_inventory_shell(hotels, 20, audit) is False


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _Cards:
    def __init__(self, page):
        self.page = page

    def count(self):
        return self.page.card_count


class _ResultPanel:
    def __init__(self, page):
        self.page = page

    def evaluate(self, _script):
        return dict(self.page.state)


class _Page:
    url = "https://ru.trip.com/hotels/listpage/?city=220"

    def __init__(self, clock):
        self.clock = clock
        self.mouse = _Mouse()
        self.scroll_target_selector = None
        self.card_count = 5
        self.state = {
            "top": 500, "height": 1000, "client": 500,
            "window_top": 500, "window_height": 1000, "window_client": 500,
            "loading": False, "mutations": 0, "continuation": None,
        }

    def evaluate(self, _script):
        if "const rects = Array.from(list.querySelectorAll('.hotel-card, .list-item'))" in _script:
            self.scroll_target_selector = ".hotel-list"
            return {"x": 700, "y": 400}
        # Match the collector's ~214 ms scroll-and-settle interval.
        self.clock.now += 0.214
        return {"elapsed_ms": 214, "loading": False}

    def wait_for_timeout(self, milliseconds):
        self.clock.now += milliseconds / 1000

    def apply_delayed_page(self):
        self.card_count = 17
        self.state.update({
            "top": 2000, "height": 2500,
            "window_top": 2000, "window_height": 2500,
            "mutations": 1,
        })


class _Mouse:
    def __init__(self):
        self.moves = []
        self.wheels = []

    def move(self, x, y):
        self.moves.append((x, y))

    def wheel(self, x, y):
        self.wheels.append((x, y))


def test_delayed_trip_page_request_prevents_false_stable_end(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)
    cards = _Cards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        if clock.now >= 2.0:
            page.apply_delayed_page()
        pending = int(1.5 <= clock.now < 2.0)
        return {
            "pending_requests": pending,
            "pending_all_requests": pending,
            "pending_request_paths": ["/restapi/soa2/34951/fetchHotelList"] if pending else [],
            "pending_inventory_paths": ["/restapi/soa2/34951/fetchHotelList"] if pending else [],
        }

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=2.5,
    )

    assert complete is True
    assert reason is None
    assert audit["collection_reason"] == "stable_end"
    assert len(collected) == 17
    assert audit["completion_conditions"]["stable_quiet_elapsed_ms"] >= 2500
    assert page.scroll_target_selector == ".hotel-list"
    assert page.mouse.moves
    assert page.mouse.wheels


def test_delayed_trip_page_without_pending_signal_gets_collection_grace(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)
    cards = _Cards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        # The browser has not exposed a pending inventory request yet, but the
        # page appends the delayed continuation after its own grace period.
        if clock.now >= 5.0:
            page.apply_delayed_page()
        return {
            "pending_requests": 0,
            "pending_all_requests": 0,
            "pending_request_paths": [],
            "pending_inventory_paths": [],
        }

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=80,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=8.0,
    )

    assert complete is True
    assert reason is None
    assert audit["collection_reason"] == "stable_end"
    assert len(collected) == 17
    assert audit["completion_conditions"]["stable_quiet_elapsed_ms"] >= 8000


def test_inventory_wait_does_not_abort_before_next_wheel(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)
    page.state.update({"top": 100, "window_top": 100})
    cards = _Cards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        if len(page.mouse.wheels) >= 2:
            page.apply_delayed_page()
        pending = int(len(page.mouse.wheels) == 1 and clock.now < 1.0)
        return {
            "pending_requests": pending,
            "pending_all_requests": pending,
            "pending_request_paths": ["/restapi/soa2/34951/fetchHotelList"] if pending else [],
            "pending_inventory_paths": ["/restapi/soa2/34951/fetchHotelList"] if pending else [],
        }

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=0.5,
    )

    assert complete is True
    assert reason is None
    assert audit["collection_reason"] == "stable_end"
    assert len(collected) == 17
    assert len(page.mouse.wheels) >= 2


def test_single_no_progress_wheel_retries_while_not_at_bottom(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)

    class _TransientWheelPage(_Page):
        def __init__(self, clock):
            super().__init__(clock)
            self.state.update({"top": 100, "height": 5000,
                               "window_top": 100, "window_height": 5000})

        def evaluate(self, script):
            if "const rects = Array.from(list.querySelectorAll('.hotel-card, .list-item'))" in script:
                self.scroll_target_selector = ".hotel-list"
                return {"x": 700, "y": 400}
            self.clock.now += 0.214
            # First wheel is lost while Trip settles a just-completed virtual
            # list update; the next real wheel advances and exposes the page.
            if len(self.mouse.wheels) >= 2:
                self.apply_delayed_page()
            return {"elapsed_ms": 214, "loading": False}

    page = _TransientWheelPage(clock)
    cards = _Cards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        return {"pending_requests": 0, "pending_all_requests": 0,
                "pending_request_paths": [], "pending_inventory_paths": []}

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=0.5,
    )

    assert complete is True
    assert reason is None
    assert audit["collection_reason"] == "stable_end"
    assert audit["no_progress_retries"] == 1
    assert len(page.mouse.wheels) >= 2
    assert len(collected) == 17


def test_five_cards_at_bottom_get_bounded_wheel_probe(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)
    page.state.update({"top": 500, "height": 1000,
                       "window_top": 500, "window_height": 1000})
    cards = _Cards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        if len(page.mouse.wheels) >= 2:
            page.apply_delayed_page()
        return {"pending_requests": 0, "pending_all_requests": 0,
                "pending_request_paths": [], "pending_inventory_paths": []}

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=0.5,
    )

    assert complete is True
    assert reason is None
    assert audit["collection_reason"] == "stable_end"
    assert audit["bottom_probe_wheels"] == 1
    assert len(collected) == 17


def test_completed_dynamic_refresh_at_bottom_gets_fetch_hotel_list_probe(monkeypatch):
    """A refresh request is not proof the inventory cursor is exhausted."""
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)
    page.card_count = 12
    page.state.update({"top": 500, "height": 1000,
                       "window_top": 500, "window_height": 1000})
    cards = _Cards(page)
    panel = _ResultPanel(page)
    saw_dynamic_refresh = False

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        nonlocal saw_dynamic_refresh
        if len(page.mouse.wheels) == 1:
            saw_dynamic_refresh = True
            return {
                "pending_requests": 0,
                "pending_all_requests": 0,
                "pending_request_paths": [],
                "pending_inventory_paths": ["/restapi/soa2/34951/fetchDynamicRefreshList"],
            }
        if len(page.mouse.wheels) >= 2:
            page.apply_delayed_page()
        return {"pending_requests": 0, "pending_all_requests": 0,
                "pending_request_paths": [], "pending_inventory_paths": []}

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=0.5,
    )

    assert saw_dynamic_refresh
    assert complete is True
    assert reason is None
    assert audit["collection_reason"] == "stable_end"
    assert audit["bottom_probe_wheels"] >= 1
    assert len(collected) == 17


def test_discard_trip_context_stops_playwright_dispatcher(monkeypatch):
    class _Resource:
        def __init__(self):
            self.closed = False
            self.stopped = False

        def close(self):
            self.closed = True

        def stop(self):
            self.stopped = True

    context = _Resource()
    manager = _Resource()
    monkeypatch.setattr(tripcom, "_TRIP_SESSION_CONTEXT", context)
    monkeypatch.setattr(tripcom, "_TRIP_SESSION_MANAGER", manager)
    monkeypatch.setattr(tripcom, "_TRIP_SESSION_PROFILE", tripcom.DEFAULT_TRIP_PROFILE)
    monkeypatch.setitem(tripcom._TRIP_CITY_ROUTES, "dubai", ("220", "/hotels/listpage/?city=220"))

    tripcom._discard_tripcom_reusable_context()

    assert context.closed is True
    assert manager.stopped is True
    assert tripcom._TRIP_SESSION_CONTEXT is None
    assert tripcom._TRIP_SESSION_MANAGER is None
    assert "dubai" not in tripcom._TRIP_CITY_ROUTES


def test_delayed_trip_page_requires_passive_pagination_hold(monkeypatch):
    """A debounced continuation must be given an idle interval at bottom."""
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)

    class _PassiveCards(_Cards):
        def __init__(self, page):
            super().__init__(page)
            self.passive_hold_seen = False

    cards = _PassiveCards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        return {
            "pending_requests": 0,
            "pending_all_requests": 0,
            "pending_request_paths": [],
            "pending_inventory_paths": [],
        }

    original_wait = page.wait_for_timeout

    def wait_for_timeout(milliseconds):
        original_wait(milliseconds)
        if milliseconds >= 1000:
            cards.passive_hold_seen = True
            page.apply_delayed_page()

    page.wait_for_timeout = wait_for_timeout
    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=20, after_wait=source_state,
        stable_end_quiet_seconds=1.0,
    )

    assert cards.passive_hold_seen is True
    assert complete is True
    assert reason is None
    assert audit["pagination_idle_waits"] >= 1
    assert len(collected) == 17


def test_bounded_collection_budget_covers_concurrent_inventory_page_cadence(monkeypatch):
    """A 150 s concurrent run reached 119 and kept receiving inventory pages."""
    clock = _Clock()
    monkeypatch.setattr(tripcom.time, "monotonic", clock.monotonic)
    page = _Page(clock)
    page.card_count = 5
    cards = _Cards(page)
    panel = _ResultPanel(page)

    def capture_cards(target):
        for index in range(page.card_count):
            target[str(index)] = {"id": str(index)}

    def source_state():
        # Thirteen inventory continuations at 16.5 s each approximate the
        # verified concurrent run's 119 cards after 151 s, then continue to
        # the configured cap without any artificial card-count shortcut.
        continuations = len(page.mouse.wheels)
        clock.now += 16.5
        page.card_count = min(150, 5 + 12 * continuations)
        page.state.update({
            "height": 1000 + page.card_count * 100,
            "window_height": 1000 + page.card_count * 100,
            "mutations": continuations,
        })
        return {
            "pending_requests": 0,
            "pending_all_requests": 0,
            "pending_request_paths": [],
            "pending_inventory_paths": [],
        }

    collected, complete, reason, audit = tripcom._tripcom_collect_visible_cards(
        page, cards, panel, capture_cards, max_scrolls=40,
        max_duration_seconds=tripcom.TRIPCOM_COLLECTION_TIMEOUT_SECONDS,
        max_cards=150, after_wait=source_state,
    )

    assert tripcom.TRIPCOM_COLLECTION_TIMEOUT_SECONDS >= 220
    assert complete is True
    assert reason == "card_limit_reached"
    assert audit["elapsed_ms"] >= 200_000
    assert len(collected) == 150
