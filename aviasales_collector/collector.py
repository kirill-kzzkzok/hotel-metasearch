from __future__ import annotations

import re
import os
import time
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .parser import normalize_card_data
from .flight_routes import flight_identity, search_route, city_code


BASE_URL = "https://www.aviasales.ru/hottickets/{origin}"
CARD_SELECTOR = '[data-test-id="service-destination"]'
HOT_TICKET_SELECTOR = '[data-test-id="hot-ticket"]'
MORE_TICKETS_SELECTOR = '[data-test-id="more-tickets-button"]'
TRIP_SELECTOR = '[data-test-id="hot-tickets-trip-type-chip-button"]'
POLL_INTERVAL_MS = 700
STABLE_POLLS = 4
MAX_SCROLL_PASSES = 80
STABLE_BOTTOM_PASSES = 5
SCROLL_SETTLE_MS = 400
_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()
try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    # Moscow has been UTC+3 without DST; this keeps minimal installs usable.
    MOSCOW_TZ = timezone(timedelta(hours=3))


def _launch_context(playwright, origin: str, headless: bool):
    raw_profile = os.environ.get("AVIASALES_BROWSER_DATA_DIR", "").strip()
    if raw_profile:
        profile = Path(raw_profile) / origin.lower()
        profile.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1365, "height": 900},
        )
        return None, context
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1365, "height": 900},
    )
    return browser, context


class CollectorError(RuntimeError):
    pass


def _profile_lock(origin: str) -> threading.Lock | None:
    """Serialize persistent-context launches for one origin profile."""
    raw_profile = os.environ.get("AVIASALES_BROWSER_DATA_DIR", "").strip()
    if not raw_profile:
        return None
    key = str((Path(raw_profile) / origin.lower()).resolve())
    with _PROFILE_LOCKS_GUARD:
        return _PROFILE_LOCKS.setdefault(key, threading.Lock())


def _trip_label(trip_type: str) -> str:
    return "Туда‑обратно" if trip_type == "RT" else "В одну сторону"


def _dismiss_site_prompts(page: Page) -> None:
    consent = page.get_by_role("button", name="Да без проблем", exact=True)
    if consent.count() and consent.first.is_visible():
        consent.first.click(timeout=4_000)
    stay = page.get_by_role("button", name="Остаться на сайте", exact=True)
    if stay.count() and stay.first.is_visible():
        stay.first.click(timeout=4_000)


def _wait_for_cards_stable(
    page: Page,
    trip_type: str,
    duration_min: int = 1,
    duration_max: int = 30,
    timeout_ms: int = 35_000,
    stable_polls: int = STABLE_POLLS,
    poll_interval_ms: int = POLL_INTERVAL_MS,
) -> None:
    if stable_polls < 1 or poll_interval_ms < 1:
        raise ValueError("stable_polls and poll_interval_ms must be positive")
    deadline = time.monotonic() + timeout_ms / 1000
    previous_signature = None
    stable = 0
    last_count = 0
    expected_label = _trip_label(trip_type)
    while time.monotonic() < deadline:
        try:
            label = page.locator(TRIP_SELECTOR).inner_text(timeout=2_000).strip()
            cards = page.locator(CARD_SELECTOR)
            count = cards.count()
            last_count = count
            snapshot = cards.evaluate_all(
                    """(nodes, range) => {
                        const samples = nodes.slice(0, 5).map(card => card.innerText);
                        const rtEvidence = nodes.length > 0 && nodes.every(card => {
                            const text = card.innerText.replace(/[\\u00a0\\u202f]/g, ' ');
                            const match = text.match(/на\\s+(\\d+)\\s+д(?:ень|ня|ней)/i);
                            return card.querySelectorAll('[data-test-id=date]').length >= 2
                                && match !== null
                                && Number(match[1]) >= range.min
                                && Number(match[1]) <= range.max;
                        });
                        return {samples, rtEvidence};
                    }""",
                    {"min": duration_min, "max": duration_max},
                )
            if isinstance(snapshot, dict):
                samples = snapshot["samples"]
                rt_evidence = snapshot["rtEvidence"]
            else:
                # Lightweight test doubles and older wrappers may only expose
                # the boolean RT check; retain the compatible fallback.
                samples = [cards.nth(i).inner_text(timeout=2_000) for i in range(min(count, 5))]
                rt_evidence = cards.evaluate_all(
                    "(nodes, range) => { const values = nodes.map(card => { "
                    "const match = card.innerText.replace(/[\\u00a0\\u202f]/g, ' ').match(/на\\s+(\\d+)\\s+д(?:ень|ня|ней)/i); "
                    "return match ? Number(match[1]) : null; }); "
                    "return nodes.length > 0 && nodes.every((card, i) => "
                    "card.querySelectorAll('[data-test-id=date]').length >= 2 && "
                    "values[i] !== null && values[i] >= range.min && values[i] <= range.max); }",
                    {"min": duration_min, "max": duration_max},
                )
            mode_ready = expected_label in label and (trip_type == "OW" or rt_evidence)
            signature = (label, count, tuple(samples))
            if mode_ready and count > 0 and signature == previous_signature:
                stable += 1
            elif mode_ready and count > 0:
                stable = 1
            else:
                stable = 0
            previous_signature = signature
            if stable >= stable_polls:
                return
        except (PlaywrightTimeoutError, PlaywrightError):
            stable = 0
        page.wait_for_timeout(poll_interval_ms)

    raise CollectorError(
        f"results did not stabilize for {trip_type}; visible card count at timeout: {last_count}"
    )


def _set_slider_value(page: Page, handle_index: int, target: int) -> None:
    handles = page.locator(".rc-slider [role=slider]")
    handle = handles.nth(handle_index)
    minimum = int(handle.get_attribute("aria-valuemin"))
    maximum = int(handle.get_attribute("aria-valuemax"))
    if not minimum <= target <= maximum:
        raise CollectorError(f"duration value {target} outside slider range {minimum}..{maximum}")

    track = page.locator(".rc-slider").first
    box = track.bounding_box()
    handle_box = handle.bounding_box()
    if box is None or handle_box is None or maximum == minimum:
        raise CollectorError("duration slider geometry is unavailable")
    current = int(handle.get_attribute("aria-valuenow"))
    if current == target:
        return

    target_x = box["x"] + (target - minimum) / (maximum - minimum) * box["width"]
    center_y = handle_box["y"] + handle_box["height"] / 2
    page.mouse.move(handle_box["x"] + handle_box["width"] / 2, center_y)
    page.mouse.down()
    page.mouse.move(target_x, center_y, steps=12)
    page.mouse.up()
    actual = int(handle.get_attribute("aria-valuenow"))
    if actual != target:
        raise CollectorError(f"duration slider handle {handle_index} ended at {actual}, expected {target}")


def _apply_trip_settings(
    page: Page,
    trip_type: str,
    duration_min: int,
    duration_max: int,
    *,
    wait_for_stable: bool = True,
) -> None:
    _dismiss_site_prompts(page)
    page.locator(TRIP_SELECTOR).click(timeout=8_000)
    label = "round_trip" if trip_type == "RT" else "one_way"
    page.locator(f'[data-test-id="{label}"]').click(timeout=5_000)

    if trip_type == "RT":
        page.wait_for_function(
            "() => document.querySelectorAll('.rc-slider [role=slider]').length === 2",
            timeout=5_000,
        )
        _set_slider_value(page, 0, duration_min)
        _set_slider_value(page, 1, duration_max)
        values = [
            int(page.locator(".rc-slider [role=slider]").nth(i).get_attribute("aria-valuenow"))
            for i in range(2)
        ]
        if values != [duration_min, duration_max]:
            raise CollectorError(f"duration slider applied {values}, expected {[duration_min, duration_max]}")

    # Current page builds apply the radio choice immediately; older variants expose a final button.
    apply_button = page.get_by_text("Выбрать", exact=True)
    if apply_button.count() and apply_button.first.is_visible():
        apply_button.first.click(timeout=5_000)
    else:
        page.locator(TRIP_SELECTOR).click(timeout=5_000)
    page.wait_for_function(
        "(expected) => document.querySelector('[data-test-id=hot-tickets-trip-type-chip-button]')?.innerText.includes(expected)",
        arg=_trip_label(trip_type),
        timeout=8_000,
    )
    if wait_for_stable:
        _wait_for_cards_stable(page, trip_type, duration_min, duration_max)


def _find_destination_card(page: Page, destination: str):
    cards = page.locator(CARD_SELECTOR)
    for index in range(cards.count()):
        title = cards.nth(index).locator('[data-test-id="city-title"]')
        if title.count() and title.first.inner_text().strip().casefold() == destination.casefold():
            return cards.nth(index)
    return None


def _wait_for_destination_card(page: Page, destination: str, timeout_ms: int = 20_000):
    deadline = time.monotonic() + timeout_ms / 1000
    stable_bottom_passes = 0
    previous_state = None
    while time.monotonic() < deadline:
        selected = _find_destination_card(page, destination)
        if selected is not None:
            return selected
        state = page.evaluate(
            """destination => {
                const cards = [...document.querySelectorAll('[data-test-id="service-destination"]')];
                const found = cards.some(card => card.querySelector('[data-test-id="city-title"]')
                    ?.innerText.trim().toLocaleLowerCase() === destination.toLocaleLowerCase());
                let node = cards[0]?.parentElement;
                let scrollNode = null;
                while (node && node !== document.body) {
                    const style = getComputedStyle(node);
                    if (node.scrollHeight > node.clientHeight + 8 && /(auto|scroll)/.test(style.overflowY)) {
                        scrollNode = node;
                    }
                    node = node.parentElement;
                }
                const target = scrollNode || document.scrollingElement;
                const before = target.scrollTop;
                const max = Math.max(0, target.scrollHeight - target.clientHeight);
                target.scrollBy({top: Math.max(300, target.clientHeight * 0.8), behavior: 'instant'});
                return {found, top: target.scrollTop, max, count: cards.length, moved: target.scrollTop > before};
            }""",
            destination,
        )
        if isinstance(state, dict):
            signature = (state.get("top"), state.get("max"), state.get("count"))
            if not state.get("moved") or signature == previous_state:
                stable_bottom_passes += 1
            else:
                stable_bottom_passes = 0
            previous_state = signature
            if stable_bottom_passes >= 4:
                break
        page.wait_for_timeout(350)
    raise CollectorError(f"destination card not found: {destination}")


def _ensure_deep_page(playwright, origin: str, headless: bool, browser, context, page):
    try:
        context_closed = context is None or context.is_closed()
    except Exception:
        context_closed = True
    try:
        page_closed = page is None or page.is_closed()
    except Exception:
        page_closed = True
    if not context_closed and not page_closed:
        return browser, context, page
    if not context_closed and page_closed:
        page = context.new_page()
        page.set_default_timeout(8_000)
        page.set_default_navigation_timeout(45_000)
        return browser, context, page
    for resource in (context, browser):
        if resource is not None:
            try:
                resource.close()
            except Exception:
                pass
    browser, context = _launch_context(playwright, origin, headless)
    page = context.new_page()
    page.set_default_timeout(8_000)
    page.set_default_navigation_timeout(45_000)
    return browser, context, page


def _wait_for_more_tickets(page: Page, selected, destination: str, timeout_ms: int = 1_200):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        more = selected.locator(MORE_TICKETS_SELECTOR)
        if more.count() and more.first.is_visible():
            return more.first
        # Keep compatibility with older Aviasales markup while preferring its stable test id.
        more = selected.get_by_text("Больше билетов", exact=True)
        if more.count() and more.first.is_visible():
            return more.first
        page.wait_for_timeout(100)
    return None


def _collect_summary_card_offers(
    selected, origin: str, trip_type: str, destination: str
) -> tuple[list[dict], int, bool, list[str]]:
    """Use the offers already visible on a destination card when there is no expand control."""
    raw_cards = selected.evaluate(
        """card => {
            const carriers = scope => [...scope.querySelectorAll('img, [aria-label], [data-test-id*=\"airline\" i], [class*=\"airline\" i]')]
                .map(item => {
                    const name = [item.getAttribute('alt'), item.getAttribute('title'), item.getAttribute('aria-label'),
                        item.closest('[aria-label]')?.getAttribute('aria-label'), item.closest('[title]')?.getAttribute('title')]
                        .map(value => (value || '').trim()).find(Boolean);
                    const image = item.matches('img') ? item : item.querySelector('img');
                    return name ? {name, url: image ? (image.currentSrc || image.src || image.getAttribute('data-src')) : null} : null;
                }).filter(Boolean);
            return [...card.querySelectorAll('[data-test-id=hot-ticket]')].map(node => {
            let scope = node;
            for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
                const ticketLinks = parent.querySelectorAll('[data-test-id=hot-ticket]').length;
                const legCount = [...parent.querySelectorAll('[data-test-id=text]')].filter(item => /в пути/i.test(item.innerText)).length;
                if (ticketLinks !== 1 || legCount > 2) break;
                scope = parent;
            }
            const legs = [...scope.querySelectorAll('[data-test-id=text]')]
                .filter(item => /в пути/i.test(item.innerText));
            return {
                card_text: scope.innerText,
                destination: card.querySelector('[data-test-id=city-title]')?.innerText ?? null,
                source_url: node.href ?? null,
                price_texts: [...node.querySelectorAll('[data-test-id=price]')].map(item => item.innerText),
                text_items: [...scope.querySelectorAll('[data-test-id=text]')].map(item => item.innerText),
                date_items: [...scope.querySelectorAll('[data-test-id=month], [data-test-id=date]')].map(item => item.innerText),
                airline_by_leg: legs.map(leg => {
                    let parent = leg.parentElement;
                    while (parent && parent !== scope) {
                        const count = [...parent.querySelectorAll('[data-test-id=text]')]
                            .filter(item => /в пути/i.test(item.innerText)).length;
                        const names = carriers(parent).map(item => item.name);
                        if (count === 1 && names.length) return names;
                        parent = parent.parentElement;
                    }
                    return [];
                }),
                airlines: carriers(scope).map(item => item.name),
                airline_logos: carriers(scope).filter(item => item.url)
            };
        });
        }"""
    )
    today = datetime.now(MOSCOW_TZ).date()
    offers = []
    for data in raw_cards:
        data["destination"] = destination
        offers.append(normalize_card_data(data, origin, trip_type, today))
    return offers, len(raw_cards), True, []


def _wait_for_hot_tickets(page: Page, destination: str, timeout_ms: int = 30_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.locator(HOT_TICKET_SELECTOR).count():
            return
        page.wait_for_timeout(250)
    raise CollectorError(f"destination page has no ticket cards for {destination}")


def _scroll_collect(page: Page, origin: str, trip_type: str) -> tuple[list[dict], int, bool, list[str]]:
    seen: OrderedDict[str, dict] = OrderedDict()
    warnings: list[str] = []
    stable_bottom_passes = 0
    last_dom_count = -1
    today = datetime.now(MOSCOW_TZ).date()
    scroll_complete = False

    for _ in range(MAX_SCROLL_PASSES):
        cards = page.locator(CARD_SELECTOR)
        dom_count = cards.count()
        if dom_count == 0:
            raise CollectorError("critical card selector matched no cards after results stabilized")

        before_unique = len(seen)
        raw_cards = cards.evaluate_all(
            "cards => cards.map(card => ({"
            "card_text: card.innerText,"
            "destination: card.querySelector('[data-test-id=city-title]')?.innerText ?? null,"
            "source_url: card.querySelector('[data-test-id=hot-ticket]')?.href ?? null,"
            "price_texts: [...card.querySelectorAll('[data-test-id=price]')].map(node => node.innerText),"
            "text_items: [...card.querySelectorAll('[data-test-id=text]')].map(node => node.innerText),"
            "date_items: [...card.querySelectorAll('[data-test-id=month], [data-test-id=date]')].map(node => node.innerText),"
            "airline_by_leg: (() => { const carrierNames = scope => [...scope.querySelectorAll('img, [aria-label], [data-test-id*=\"airline\" i], [class*=\"airline\" i]')].map(item => [item.getAttribute('alt'), item.getAttribute('title'), item.getAttribute('aria-label'), item.closest('[aria-label]')?.getAttribute('aria-label'), item.closest('[title]')?.getAttribute('title')].map(value => (value || '').trim()).find(Boolean)).filter(Boolean); const legs = [...card.querySelectorAll('[data-test-id=text]')].filter(node => /в пути/i.test(node.innerText)); return legs.map(node => { let scope = node.parentElement; while (scope && scope !== card) { const count = [...scope.querySelectorAll('[data-test-id=text]')].filter(item => /в пути/i.test(item.innerText)).length; const names = carrierNames(scope); if (count === 1 && names.length) return names; scope = scope.parentElement; } return []; }); })(),"
            "airlines: [...card.querySelectorAll('img, [aria-label], [data-test-id*=\"airline\" i], [class*=\"airline\" i]')].map(item => [item.getAttribute('alt'), item.getAttribute('title'), item.getAttribute('aria-label'), item.closest('[aria-label]')?.getAttribute('aria-label'), item.closest('[title]')?.getAttribute('title')].map(value => (value || '').trim()).find(Boolean)).filter(Boolean),"
            "airline_logos: [...card.querySelectorAll('img')].map(item => ({name: [item.getAttribute('alt'), item.getAttribute('title'), item.getAttribute('aria-label'), item.closest('[aria-label]')?.getAttribute('aria-label'), item.closest('[title]')?.getAttribute('title')].map(value => (value || '').trim()).find(Boolean), url: item.currentSrc || item.src || item.getAttribute('data-src')})).filter(item => item.name)"
            "}))"
        )
        for data in raw_cards:
            route = data.get("source_url")
            fallback = "|".join(
                str(value or "")
                for value in (origin, data.get("destination"), data.get("price_texts"), data.get("text_items"))
            )
            key = route or fallback
            if key not in seen:
                seen[key] = normalize_card_data(data, origin, trip_type, today)

        metrics = _scroll_metrics(page, CARD_SELECTOR)
        at_bottom = metrics["top"] + metrics["viewport"] >= metrics["height"] - 3
        no_new_cards = len(seen) == before_unique and dom_count == last_dom_count
        if at_bottom and no_new_cards:
            stable_bottom_passes += 1
        else:
            stable_bottom_passes = 0
        if stable_bottom_passes >= STABLE_BOTTOM_PASSES:
            scroll_complete = True
            break

        last_dom_count = dom_count
        _scroll_down(page, CARD_SELECTOR, max(550, int(metrics["viewport"] * 0.85)))
        page.wait_for_timeout(SCROLL_SETTLE_MS)

    if not scroll_complete:
        warnings.append(f"scroll did not reach stable bottom after {MAX_SCROLL_PASSES} passes")

    final_dom_count = page.locator(CARD_SELECTOR).count()
    final_ticket_count = page.locator(f'{CARD_SELECTOR} {HOT_TICKET_SELECTOR}').count()
    if final_dom_count != final_ticket_count:
        warnings.append(
            f"card/ticket selector count mismatch: {final_dom_count} cards vs {final_ticket_count} ticket links"
        )
    return list(seen.values()), final_ticket_count, scroll_complete, warnings


def _scroll_collect_destination(
    page: Page, origin: str, trip_type: str, destination: str,
    requested_destination: str | None = None,
) -> tuple[list[dict], int, bool, list[str]]:
    """Collect offers from the destination page opened by the visible UI button."""
    seen: OrderedDict[str, dict] = OrderedDict()
    warnings: list[str] = []
    stable_bottom_passes = 0
    last_dom_count = -1
    today = datetime.now(MOSCOW_TZ).date()
    scroll_complete = False

    for _ in range(MAX_SCROLL_PASSES):
        tickets = page.locator(HOT_TICKET_SELECTOR)
        dom_count = tickets.count()
        if dom_count == 0:
            raise CollectorError(f"destination page has no ticket cards for {destination}")

        before_unique = len(seen)
        raw_cards = tickets.evaluate_all(
            """nodes => nodes.map(node => {
                let card = node;
                for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
                    const ticketLinks = parent.querySelectorAll('[data-test-id=hot-ticket]').length;
                    const legCount = [...parent.querySelectorAll('[data-test-id=text]')].filter(item => /в пути/i.test(item.innerText)).length;
                    if (ticketLinks !== 1 || legCount > 2) break;
                    card = parent;
                }
                const carrierNames = scope => [...scope.querySelectorAll('img, [aria-label], [data-test-id*=\"airline\" i], [class*=\"airline\" i]')]
                    .map(item => [item.getAttribute('alt'), item.getAttribute('title'), item.getAttribute('aria-label'), item.closest('[aria-label]')?.getAttribute('aria-label'), item.closest('[title]')?.getAttribute('title')].map(value => (value || '').trim()).find(Boolean)).filter(Boolean);
                return {
                    card_text: card?.innerText ?? node.innerText,
                    destination: node.closest('[data-test-id=service-destination]')?.querySelector('[data-test-id=city-title]')?.innerText
                        ?? document.querySelector('[data-test-id=route-destination]')?.innerText ?? null,
                    source_url: node.href ?? null,
                    price_texts: [...node.querySelectorAll('[data-test-id=price]')].map(item => item.innerText),
                    text_items: [...(card ?? node).querySelectorAll('[data-test-id=text]')].map(item => item.innerText),
                    date_items: [...(card ?? node).querySelectorAll('[data-test-id=month], [data-test-id=date]')].map(item => item.innerText),
                    airline_by_leg: (() => {
                        const root = card ?? node;
                        const legs = [...root.querySelectorAll('[data-test-id=text]')].filter(item => /в пути/i.test(item.innerText));
                        return legs.map(leg => {
                            let scope = leg.parentElement;
                            while (scope && scope !== root) {
                                const count = [...scope.querySelectorAll('[data-test-id=text]')].filter(item => /в пути/i.test(item.innerText)).length;
                                const names = carrierNames(scope);
                                if (count === 1 && names.length) return names;
                                scope = scope.parentElement;
                            }
                            return [];
                        });
                    })(),
                    airlines: carrierNames(card),
                    airline_logos: [...card.querySelectorAll('img')].map(item => ({name: [item.getAttribute('alt'), item.getAttribute('title'), item.getAttribute('aria-label'), item.closest('[aria-label]')?.getAttribute('aria-label'), item.closest('[title]')?.getAttribute('title')].map(value => (value || '').trim()).find(Boolean), url: item.currentSrc || item.src || item.getAttribute('data-src')})).filter(item => item.name)
                };
            })"""
        )
        for data in raw_cards:
            # The expanded view retains the global destination cards in the DOM.
            # Never relabel their tickets with the requested destination.
            if data.get("destination") and data["destination"].strip().casefold() != destination.casefold():
                continue
            if requested_destination:
                raw_route = search_route(data.get("source_url"))
                if raw_route is None or city_code(raw_route["destination"]) != city_code(requested_destination) or city_code(raw_route["origin"]) != city_code(origin):
                    continue
            data["destination"] = destination
            key = data.get("source_url") or "|".join(
                str(value or "")
                for value in (origin, destination, data.get("price_texts"), data.get("text_items"))
            )
            if key not in seen:
                offer = normalize_card_data(data, origin, trip_type, today)
                identity = flight_identity(offer, trip_type, origin, requested_destination,
                                           f"{origin}|{requested_destination or destination}|{trip_type}")
                if not identity["route_valid"] or (requested_destination and identity['validation'] != 'verified'):
                    warnings.append(f"rejected mismatched itinerary: {identity['actual_origin']}->{identity['actual_destination']}")
                    continue
                offer["route_identity"] = identity
                seen[key] = offer

        metrics = _scroll_metrics(page, HOT_TICKET_SELECTOR)
        at_bottom = metrics["top"] + metrics["viewport"] >= metrics["height"] - 3
        no_new_cards = len(seen) == before_unique and dom_count == last_dom_count
        if at_bottom and no_new_cards:
            stable_bottom_passes += 1
        else:
            stable_bottom_passes = 0
        if stable_bottom_passes >= STABLE_BOTTOM_PASSES:
            scroll_complete = True
            break

        last_dom_count = dom_count
        _scroll_down(page, HOT_TICKET_SELECTOR, max(550, int(metrics["viewport"] * 0.85)))
        page.wait_for_timeout(SCROLL_SETTLE_MS)

    if not scroll_complete:
        warnings.append(f"destination scroll did not reach stable bottom after {MAX_SCROLL_PASSES} passes")
    final_count = page.locator(HOT_TICKET_SELECTOR).count()
    if requested_destination is None and final_count > len(seen):
        warnings.append(f"destination DOM reports {final_count} tickets but only {len(seen)} were collected")
        # A complete deep scan must not claim success while the final DOM
        # still contains ticket links that were never parsed.
        scroll_complete = False
    if not seen:
        scroll_complete = False
        warnings.append(f"no matching itineraries for {origin}->{requested_destination or destination}")
    return list(seen.values()), len(seen) if requested_destination else final_count, scroll_complete, warnings


def _scroll_metrics(page: Page, selector: str) -> dict[str, int]:
    return page.evaluate(
        """selector => {
            const root = document.scrollingElement || document.documentElement;
            let scroller = null;
            for (let node = document.querySelector(selector); node && node !== document.body;
                 node = node.parentElement) {
                const overflow = getComputedStyle(node).overflowY;
                if ((overflow === 'auto' || overflow === 'scroll')
                    && node.scrollHeight > node.clientHeight + 3) {
                    scroller = node;
                    break;
                }
            }
            const isRoot = !scroller;
            scroller = scroller || root;
            return {
                top: isRoot ? window.scrollY : scroller.scrollTop,
                height: isRoot
                    ? Math.max(root.scrollHeight, document.documentElement.scrollHeight)
                    : scroller.scrollHeight,
                viewport: isRoot ? window.innerHeight : scroller.clientHeight
            };
        }""",
        selector,
    )


def _scroll_down(page: Page, selector: str, amount: int) -> None:
    page.evaluate(
        """({selector, amount}) => {
            const root = document.scrollingElement || document.documentElement;
            let scroller = null;
            for (let node = document.querySelector(selector); node && node !== document.body;
                 node = node.parentElement) {
                const overflow = getComputedStyle(node).overflowY;
                if ((overflow === 'auto' || overflow === 'scroll')
                    && node.scrollHeight > node.clientHeight + 3) {
                    scroller = node;
                    break;
                }
            }
            if (scroller) scroller.scrollBy(0, amount);
            else window.scrollBy(0, amount);
        }""",
        {"selector": selector, "amount": amount},
    )


def _capture_failure(page: Page, origin: str, trip_type: str) -> str | None:
    try:
        directory = Path("artifacts")
        directory.mkdir(exist_ok=True)
        path = directory / f"collector-error-{origin.lower()}-{trip_type.lower()}.png"
        page.screenshot(path=str(path), full_page=True, timeout=8_000)
        return str(path)
    except Exception:
        return None


def _emit_deep_progress(callback: Callable[[dict[str, Any]], None] | None, update: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(update)
    except Exception:
        # Progress reporting must never turn a collected result into a failed scan.
        pass


def _is_expected_hot_tickets_url(value: str, origin: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and (parsed.hostname or "").lower() in {"aviasales.ru", "www.aviasales.ru"}
            and parsed.path.rstrip("/") == f"/hottickets/{origin.lower()}"
        )
    except ValueError:
        return False


def _collect_destination_page(
    page: Page,
    origin: str,
    trip_type: str,
    duration_min: int,
    duration_max: int,
    destination: str,
    refresh_before_scan: bool,
) -> tuple[list[dict], int, bool, list[str]]:
    page.goto(BASE_URL.format(origin=origin.lower()), wait_until="domcontentloaded")
    if not _is_expected_hot_tickets_url(page.url, origin):
        raise CollectorError(f"unexpected Hot Tickets URL: {page.url}")
    page.locator(TRIP_SELECTOR).wait_for(state="visible", timeout=30_000)
    _dismiss_site_prompts(page)
    _wait_for_cards_stable(
        page,
        "OW",
        timeout_ms=25_000,
        stable_polls=2,
        poll_interval_ms=400,
    )
    _apply_trip_settings(
        page,
        trip_type,
        duration_min,
        duration_max,
        wait_for_stable=trip_type == "RT",
    )
    if refresh_before_scan:
        page.reload(wait_until="domcontentloaded", timeout=45_000)
        if not _is_expected_hot_tickets_url(page.url, origin):
            raise CollectorError(f"unexpected Hot Tickets URL after refresh: {page.url}")
        page.locator(TRIP_SELECTOR).wait_for(state="visible", timeout=30_000)
        _dismiss_site_prompts(page)
        _apply_trip_settings(page, trip_type, duration_min, duration_max)

    selected = _wait_for_destination_card(page, destination)
    summary_url = selected.locator(HOT_TICKET_SELECTOR).first.get_attribute("href")
    requested_route = search_route(summary_url)
    more = _wait_for_more_tickets(page, selected, destination)
    if more is None:
        return _collect_summary_card_offers(selected, origin, trip_type, destination)
    more.click(timeout=8_000)
    _wait_for_hot_tickets(page, destination)
    if requested_route is None:
        raise CollectorError(f"cannot establish requested route for {destination}")
    return _scroll_collect_destination(page, origin, trip_type, destination, requested_route["destination"])


def _result_shell(origin: str, trip_type: str, duration_min: int, duration_max: int) -> dict:
    return {
        "origin": origin,
        "trip_type": trip_type,
        "duration_min_days": duration_min if trip_type == "RT" else None,
        "duration_max_days": duration_max if trip_type == "RT" else None,
        "url": BASE_URL.format(origin=origin.lower()),
        "complete": False,
        "cards_found": 0,
        "dom_hot_ticket_count": 0,
        "unique_destinations": 0,
        "duration_filter_verified": None,
        "offers": [],
        "warnings": [],
        "elapsed_seconds": 0.0,
    }


def collect(
    origin: str,
    trip_type: str,
    duration_min: int = 1,
    duration_max: int = 30,
    headless: bool = True,
    refresh_before_scan: bool = True,
) -> dict:
    origin = origin.upper()
    trip_type = trip_type.upper()
    result = _result_shell(origin, trip_type, duration_min, duration_max)
    started = time.monotonic()
    browser: Browser | None = None
    page: Page | None = None

    if not re.fullmatch(r"[A-Z]{3}", origin):
        result["warnings"].append("origin must be a three-letter IATA-style route code")
        return result
    if trip_type not in {"OW", "RT"}:
        result["warnings"].append("trip_type must be OW or RT")
        return result
    if not 1 <= duration_min <= duration_max <= 30:
        result["warnings"].append("RT duration must satisfy 1 <= min <= max <= 30")
        return result

    profile_lock = _profile_lock(origin)
    try:
        if profile_lock is not None:
            profile_lock.acquire()
        with sync_playwright() as playwright:
            browser, context = _launch_context(playwright, origin, headless)
            page = context.new_page()
            page.set_default_timeout(8_000)
            page.set_default_navigation_timeout(45_000)
            try:
                page.goto(result["url"], wait_until="domcontentloaded")
                if not _is_expected_hot_tickets_url(page.url, origin):
                    raise CollectorError(f"unexpected Hot Tickets URL: {page.url}")
                page.locator(TRIP_SELECTOR).wait_for(state="visible", timeout=30_000)
                _dismiss_site_prompts(page)
                _wait_for_cards_stable(page, "OW")
                _apply_trip_settings(page, trip_type, duration_min, duration_max)

                if refresh_before_scan:
                    page.reload(wait_until="domcontentloaded", timeout=45_000)
                    if not _is_expected_hot_tickets_url(page.url, origin):
                        raise CollectorError(f"unexpected Hot Tickets URL after refresh: {page.url}")
                    page.locator(TRIP_SELECTOR).wait_for(state="visible", timeout=30_000)
                    _dismiss_site_prompts(page)
                    reset_label = page.locator(TRIP_SELECTOR).inner_text().strip()
                    if reset_label != _trip_label("OW"):
                        result["warnings"].append(f"refresh default was {reset_label!r}, expected OW reset")
                    _apply_trip_settings(page, trip_type, duration_min, duration_max)

                result["url"] = page.url
                if not _is_expected_hot_tickets_url(page.url, origin):
                    raise CollectorError(f"unexpected Hot Tickets URL after settings: {page.url}")
                result["offers"], result["dom_hot_ticket_count"], scroll_complete, scroll_warnings = _scroll_collect(
                    page, origin, trip_type
                )
                result["warnings"].extend(scroll_warnings)
                result["cards_found"] = len(result["offers"])
                result["unique_destinations"] = len({offer["destination"] for offer in result["offers"] if offer["destination"]})
                if trip_type == "RT":
                    duration_values = [offer["trip_duration_days"] for offer in result["offers"]]
                    result["duration_filter_verified"] = bool(duration_values) and all(
                        value is not None and duration_min <= value <= duration_max for value in duration_values
                    )
                    if not result["duration_filter_verified"]:
                        result["warnings"].append(
                            f"RT duration values do not all fit the selected {duration_min}..{duration_max} day range"
                        )

                quality_fields = ("destination", "price", "currency", "source_url", "departure_date_raw")
                if trip_type == "RT":
                    quality_fields += ("return_date_raw", "trip_duration_days")
                core_valid = sum(all(offer.get(field) is not None for field in quality_fields) for offer in result["offers"])
                quality_ratio = core_valid / len(result["offers"]) if result["offers"] else 0
                if result["cards_found"] == 0:
                    result["warnings"].append("no offers were collected; empty result is not treated as success")
                if quality_ratio < 0.9:
                    result["warnings"].append(
                        f"core parser fields present on {core_valid}/{len(result['offers'])} cards; minimum required is 90%"
                    )
                if result["dom_hot_ticket_count"] != result["cards_found"]:
                    result["warnings"].append(
                        f"collected {result['cards_found']} unique cards but final DOM has {result['dom_hot_ticket_count']} hot-ticket links"
                    )

                parse_warning_counts: dict[str, int] = {}
                for offer in result["offers"]:
                    for warning in offer["warnings"]:
                        parse_warning_counts[warning] = parse_warning_counts.get(warning, 0) + 1
                result["parse_warning_counts"] = parse_warning_counts
                result["complete"] = bool(
                    scroll_complete
                    and result["cards_found"] > 0
                    and result["dom_hot_ticket_count"] == result["cards_found"]
                    and quality_ratio >= 0.9
                    and (trip_type != "RT" or result["duration_filter_verified"])
                )
            except Exception as exc:
                result["warnings"].append(f"{type(exc).__name__}: {exc}")
                artifact = _capture_failure(page, origin, trip_type)
                if artifact:
                    result["warnings"].append(f"debug screenshot: {artifact}")
            finally:
                try:
                    context.close()
                except Exception as exc:
                    result["warnings"].append(f"browser context close failed: {type(exc).__name__}: {exc}")
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as exc:
                        result["warnings"].append(f"browser close failed: {type(exc).__name__}: {exc}")
                browser = None
    except Exception as exc:
        result["warnings"].append(f"{type(exc).__name__}: {exc}")
        if page is not None:
            artifact = _capture_failure(page, origin, trip_type)
            if artifact:
                result["warnings"].append(f"debug screenshot: {artifact}")
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    finally:
        if profile_lock is not None and profile_lock.locked():
            profile_lock.release()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def collect_deep(
    origin: str,
    trip_type: str,
    destinations: list[str] | tuple[str, ...],
    duration_min: int = 1,
    duration_max: int = 30,
    headless: bool = True,
    refresh_before_scan: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Open each destination through the visible More tickets UI and collect its DOM."""
    origin = origin.upper()
    trip_type = trip_type.upper()
    result = _result_shell(origin, trip_type, duration_min, duration_max)
    result.update(
        {
            "deep_scan": True,
            "destinations_requested": len(destinations),
            "destinations_scanned": 0,
        }
    )
    started = time.monotonic()
    browser: Browser | None = None
    page: Page | None = None

    if not re.fullmatch(r"[A-Z]{3}", origin):
        result["warnings"].append("origin must be a three-letter route code")
        return result
    if trip_type not in {"OW", "RT"}:
        result["warnings"].append("trip_type must be OW or RT")
        return result
    if not destinations:
        result["warnings"].append("at least one active destination is required")
        return result
    if not 1 <= duration_min <= duration_max <= 30:
        result["warnings"].append("RT duration must satisfy 1 <= min <= max <= 30")
        return result

    all_offers: list[dict] = []
    destination_successes = 0
    profile_lock = _profile_lock(origin)
    try:
        if profile_lock is not None:
            profile_lock.acquire()
        with sync_playwright() as playwright:
            browser, context = _launch_context(playwright, origin, headless)
            page = context.new_page()
            page.set_default_timeout(8_000)
            page.set_default_navigation_timeout(45_000)
            try:
                for destination_index, destination in enumerate(destinations, start=1):
                    destination = " ".join(destination.split())
                    _emit_deep_progress(
                        progress_callback,
                        {
                            "status": "started",
                            "destination": destination,
                            "current": destination_index,
                            "total": len(destinations),
                            "offers": len(all_offers),
                        },
                    )
                    last_error = None
                    for destination_attempt in range(2):
                        try:
                            browser, context, page = _ensure_deep_page(
                                playwright, origin, headless, browser, context, page
                            )
                            offers, dom_count, scroll_complete, warnings = _collect_destination_page(
                                page,
                                origin,
                                trip_type,
                                duration_min,
                                duration_max,
                                destination,
                                refresh_before_scan,
                            )
                            result["warnings"].extend(
                                f"{destination}: {warning}" for warning in warnings
                            )
                            if not scroll_complete:
                                raise CollectorError(f"destination offers incomplete: {destination}")
                            all_offers.extend(offers)
                            destination_successes += 1
                            _emit_deep_progress(
                                progress_callback,
                                {
                                    "status": "completed",
                                    "destination": destination,
                                    "current": destination_index,
                                    "total": len(destinations),
                                    "offers": len(all_offers),
                                },
                            )
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                            if destination_attempt == 0:
                                _emit_deep_progress(
                                    progress_callback,
                                    {
                                        "status": "retrying",
                                        "destination": destination,
                                        "current": destination_index,
                                        "total": len(destinations),
                                        "offers": len(all_offers),
                                    },
                                )
                                continue
                    if last_error is not None:
                        result["warnings"].append(
                            f"{destination}: {type(last_error).__name__}: {last_error}"
                        )
                        _emit_deep_progress(
                            progress_callback,
                            {
                                "status": "failed",
                                "destination": destination,
                                "current": destination_index,
                                "total": len(destinations),
                                "offers": len(all_offers),
                                "warning": str(last_error),
                            },
                        )

                result["offers"] = all_offers
                result["cards_found"] = len(all_offers)
                result["dom_hot_ticket_count"] = len(all_offers)
                result["unique_destinations"] = len(
                    {offer.get("destination") for offer in all_offers if offer.get("destination")}
                )
                result["destinations_scanned"] = destination_successes
                result["partial"] = destination_successes < len(destinations)
                if result["partial"] and destination_successes:
                    result["warnings"].append(
                        f"deep scan completed partially: {destination_successes}/{len(destinations)} destinations"
                    )
                if trip_type == "RT":
                    duration_values = [offer.get("trip_duration_days") for offer in all_offers]
                    result["duration_filter_verified"] = bool(duration_values) and all(
                        value is not None and duration_min <= value <= duration_max
                        for value in duration_values
                    )
                    if not result["duration_filter_verified"]:
                        result["warnings"].append(
                            f"deep RT duration values do not all fit selected {duration_min}..{duration_max} day range"
                        )
                core_fields = ("destination", "price", "currency", "source_url", "departure_date_raw")
                if trip_type == "RT":
                    core_fields += ("return_date_raw", "trip_duration_days")
                valid = sum(
                    all(offer.get(field) is not None for field in core_fields)
                    for offer in all_offers
                )
                ratio = valid / len(all_offers) if all_offers else 0
                if ratio < 0.9:
                    result["warnings"].append(
                        f"core parser fields present on {valid}/{len(all_offers)} deep cards; minimum required is 90%"
                    )
                result["complete"] = bool(
                    destination_successes > 0
                    and all_offers
                    and ratio >= 0.9
                    and (trip_type != "RT" or result["duration_filter_verified"])
                )
            except Exception as exc:
                result["warnings"].append(f"{type(exc).__name__}: {exc}")
                artifact = _capture_failure(page, origin, trip_type)
                if artifact:
                    result["warnings"].append(f"debug screenshot: {artifact}")
            finally:
                try:
                    context.close()
                except Exception as exc:
                    result["warnings"].append(f"browser context close failed: {type(exc).__name__}: {exc}")
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as exc:
                        result["warnings"].append(f"browser close failed: {type(exc).__name__}: {exc}")
                browser = None
    except Exception as exc:
        result["warnings"].append(f"{type(exc).__name__}: {exc}")
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    finally:
        if profile_lock is not None and profile_lock.locked():
            profile_lock.release()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result
