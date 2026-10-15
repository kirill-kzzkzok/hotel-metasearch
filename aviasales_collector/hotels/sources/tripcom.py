"""Existing Trip.com Playwright discovery and browser lifecycle, relocated without flow changes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import (parse_qs, unquote, urlencode, urlparse, urlsplit,
                          urlunsplit)

from ..models import SourceResult, utc_now
from ..normalization import classify_meal_plan, normalize_hotel
from ..runtime import log
from .base import HotelSource

TRIP_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tripcom-browser")

TRIP_PROFILE_LOCK = threading.Lock()

_TRIP_SESSION_LOCK = threading.Lock()

_TRIP_SESSION_MANAGER: Any | None = None

_TRIP_SESSION_CONTEXT: Any | None = None

_TRIP_SESSION_PROFILE: Path | None = None

_TRIP_CITY_ROUTES: dict[str, tuple[str, str]] = {}

DEFAULT_TRIP_PROFILE = Path(__file__).resolve().parents[3] / ".tripcom-browser"

TRIPCOM_MAX_COLLECTION_CARDS = 150

TRIPCOM_COLLECTION_CACHE_VERSION = "150-240s-stable-quiet-v6"

# Three isolated live runs reached 150 in 71–114 s. A concurrent production-
# like run was still making verified progress at the old 150 s cutoff (119
# cards, 23 successful inventory responses, last page added 11). A bounded
# 240 s deadline covers the measured concurrent cadence plus one delayed page
# without allowing a stalled inventory to run indefinitely.
TRIPCOM_COLLECTION_TIMEOUT_SECONDS = 240

TRIP_AUTOCOMPLETE_PATH = "/restapi/soa2/14975/homepageSuggest"

def _tripcom_reusable_context(profile: Path, sync_playwright_factory: Any) -> tuple[Any, Any, bool]:
    """Return the single reusable Trip.com browser context.

    The caller is serialized by ``TRIP_PROFILE_LOCK`` and runs on the single
    Trip.com executor thread.  Keeping the Playwright manager and persistent
    context alive removes Chromium/context startup from every search while a
    fresh page is still used for each operation.
    """
    global _TRIP_SESSION_MANAGER, _TRIP_SESSION_CONTEXT, _TRIP_SESSION_PROFILE
    with _TRIP_SESSION_LOCK:
        current = _TRIP_SESSION_CONTEXT
        if current is not None and _TRIP_SESSION_PROFILE == profile:
            try:
                if not current.is_closed():
                    return _TRIP_SESSION_MANAGER, current, True
            except Exception:
                pass
        if _TRIP_SESSION_CONTEXT is not None:
            try:
                _TRIP_SESSION_CONTEXT.close()
            except Exception:
                pass
        if _TRIP_SESSION_MANAGER is not None:
            try:
                _TRIP_SESSION_MANAGER.stop()
            except Exception:
                pass
        manager = sync_playwright_factory().start()
        try:
            context = manager.chromium.launch_persistent_context(
                str(profile), headless=False, locale="ru-RU", viewport={"width": 1440, "height": 1000}
            )
        except Exception:
            # A failed persistent launch still owns a Playwright driver process.
            # Stop it before a caller attempts a fresh-context recovery.
            try:
                manager.stop()
            except Exception:
                pass
            raise
        _TRIP_SESSION_MANAGER = manager
        _TRIP_SESSION_CONTEXT = context
        _TRIP_SESSION_PROFILE = profile
        return manager, context, False

def _discard_tripcom_reusable_context() -> None:
    """Forget a Playwright context whose channel is stale even if is_closed lies."""
    global _TRIP_SESSION_MANAGER, _TRIP_SESSION_CONTEXT, _TRIP_SESSION_PROFILE
    with _TRIP_SESSION_LOCK:
        context, manager = _TRIP_SESSION_CONTEXT, _TRIP_SESSION_MANAGER
        _TRIP_SESSION_CONTEXT = None
        _TRIP_SESSION_MANAGER = None
        _TRIP_SESSION_PROFILE = None
        _TRIP_CITY_ROUTES.clear()
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if manager is not None:
        # Playwright's Sync dispatcher owns a running asyncio loop on this
        # thread. Closing the Playwright channel alone leaves that loop alive;
        # a same-thread bounded retry then fails in sync_playwright().start().
        try:
            manager.stop()
        except Exception:
            pass

def _tripcom_new_page_with_recovery(context: Any, reused_browser: bool, profile: Path,
                                   sync_playwright_factory: Any) -> tuple[Any, Any, bool]:
    try:
        return context.new_page(), context, reused_browser
    except Exception as exc:
        error_text = str(exc).casefold()
        stale_context = (
            any(name in type(exc).__name__.casefold()
                for name in ("targetclosed", "browserclosed", "pagecrashed"))
            or any(marker in error_text for marker in (
                "target page, context or browser has been closed",
                "browser has been closed",
                "browser disconnected",
                "target closed",
                "target crashed",
                "page crashed",
            ))
        )
        if not reused_browser or not stale_context:
            raise
        log.warning("[TRIP_BROWSER_REUSE] stale_context_detected error_class=%s; restarting once",
                    type(exc).__name__)
        _discard_tripcom_reusable_context()
        _manager, context, reused_browser = _tripcom_reusable_context(profile, sync_playwright_factory)
        log.info("[TRIP_BROWSER_START] recovery=stale_reuse")
        return context.new_page(), context, reused_browser

def shutdown_tripcom_browser() -> None:
    """Close the reusable Playwright resources on application shutdown.

    This function is executed on ``TRIP_EXECUTOR`` so Playwright objects are
    closed on the same thread that created them.  It is intentionally
    idempotent and safe when no Trip.com search has run in this process.
    """
    global _TRIP_SESSION_MANAGER, _TRIP_SESSION_CONTEXT, _TRIP_SESSION_PROFILE
    with _TRIP_SESSION_LOCK:
        context, manager = _TRIP_SESSION_CONTEXT, _TRIP_SESSION_MANAGER
        _TRIP_SESSION_CONTEXT = None
        _TRIP_SESSION_MANAGER = None
        _TRIP_SESSION_PROFILE = None
        _TRIP_CITY_ROUTES.clear()
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if manager is not None:
        try:
            manager.stop()
        except Exception:
            pass

class TripComSource:
    source = "trip_com"

    async def search(self, query: dict[str, Any], page_token: str | None = None) -> SourceResult:
        profile_setting = os.getenv("TRIP_COM_BROWSER_DATA_DIR")
        profile = Path(profile_setting).expanduser().resolve() if profile_setting else DEFAULT_TRIP_PROFILE
        submitted_at = time.perf_counter()

        def run_search() -> SourceResult:
            queue_ms = round((time.perf_counter() - submitted_at) * 1000, 1)
            log.info("[TRIP_SEARCH] stage=executor_start queue_ms=%.1f city=%s",
                     queue_ms, query.get("city_name"))
            return _tripcom_browser_search(query, profile)

        return await asyncio.get_running_loop().run_in_executor(TRIP_EXECUTOR, run_search)

def _tripcom_collect_visible_cards(page: Any, cards: Any, result_panel: Any,
                                   capture_visible_cards: Any,
                                   *, max_scrolls: int = 300,
                                   max_duration_seconds: float = 300,
                                   max_cards: int | None = None,
                                   loading_timeout_seconds: float = 15,
                                   stable_end_quiet_seconds: float = 0.0,
                                   pagination_idle_wait_seconds: float = 1.6,
                                   after_wait: Any = None) -> tuple[dict[str, dict[str, Any]], bool, str | None, dict[str, Any]]:
    """Collect a virtualized result list until its scroll end is stable.

    The caller supplies card parsing so this routine can be regression-tested
    without starting a browser. A finite safety limit prevents a broken page
    from spinning forever. Reaching `max_cards` is a normal configured stop,
    distinct from an incomplete or failed upstream collection.
    """
    parsed_cards: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    audit = {
        "initial_unique_cards": 0,
        "raw_cards": 0,
        "unique_cards": 0,
        "scrolls": 0,
        "max_scrolls": max_scrolls,
        "max_duration_seconds": max_duration_seconds,
        "max_cards": max_cards,
        "loading_timeout_seconds": loading_timeout_seconds,
        "stable_end_quiet_seconds": stable_end_quiet_seconds,
        "pagination_idle_wait_seconds": pagination_idle_wait_seconds,
        "pagination_idle_waits": 0,
        "bottom_probe_wheels": 0,
        "scroll_input_mode": "mouse_wheel_over_hotel_list",
        "trace": trace,
    }
    started = time.monotonic()

    if max_cards is not None and max_cards < 1:
        raise ValueError("max_cards must be a positive integer or None")

    state_script = r"""element => {
      const doc = element.ownerDocument;
      const view = doc.defaultView;
      const root = doc.documentElement;
      const resultList = doc.querySelector('.hotel-list');
      const visible = node => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none'
          && style.visibility !== 'hidden';
      };
      if (!element.__hotelInventoryAudit) {
        const audit = {mutations: 0};
        audit.observer = new MutationObserver(records => { audit.mutations += records.length; });
        audit.observer.observe(element, {subtree: true, childList: true, attributes: true, characterData: true});
        Object.defineProperty(element, '__hotelInventoryAudit', {value: audit});
      }
      const actionPattern = /^(?:load more|show more|see more|more results|next|далее|следующая|следующ\s+страница|показать ещё|показать еще|загрузить ещё|загрузить еще|ещё результаты|еще результаты)$/i;
      const controls = Array.from(doc.querySelectorAll('button, a, [role="button"]'));
      const continuation = controls.find(node => {
        if (!visible(node)) return false;
        const label = (node.getAttribute('aria-label') || node.innerText || '').replace(/\s+/g, ' ').trim();
        return node.rel === 'next' || actionPattern.test(label);
      });
      const loadingRoot = resultList || root;
      const loading = Array.from(loadingRoot.querySelectorAll('[aria-busy="true"], [role="progressbar"], [class]'))
        .some(node => visible(node) && (/loading|spinner|skeleton|загрузк/i.test(String(node.className || ''))
          || node.getAttribute('aria-busy') === 'true' || node.getAttribute('role') === 'progressbar'));
      return {
        top: view.scrollY, height: root.scrollHeight, client: view.innerHeight,
        window_top: view.scrollY, window_height: root.scrollHeight,
        window_client: view.innerHeight,
        loading,
        mutations: element.__hotelInventoryAudit.mutations,
        scroll_target: resultList ? '.hotel-list' : null,
        continuation: continuation ? {
          label: (continuation.getAttribute('aria-label') || continuation.innerText || '').replace(/\s+/g, ' ').trim(),
          href: continuation.href || null, tag: continuation.tagName.toLowerCase(),
        } : null,
      };
    }"""
    click_continuation_script = r"""element => {
      const visible = node => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none'
          && style.visibility !== 'hidden';
      };
      const actionPattern = /^(?:load more|show more|see more|more results|next|далее|следующая|следующ\s+страница|показать ещё|показать еще|загрузить ещё|загрузить еще|ещё результаты|еще результаты)$/i;
      const node = Array.from(element.ownerDocument.querySelectorAll('button, a, [role="button"]')).find(item => {
        if (!visible(item)) return false;
        const label = (item.getAttribute('aria-label') || item.innerText || '').replace(/\s+/g, ' ').trim();
        return item.rel === 'next' || actionPattern.test(label);
      });
      if (!node) return null;
      const action = {
        label: (node.getAttribute('aria-label') || node.innerText || '').replace(/\s+/g, ' ').trim(),
        href: node.href || null, tag: node.tagName.toLowerCase(),
      };
      node.click();
      return action;
    }"""

    def update_card_counts() -> None:
        # Cards are keyed by provider hotel ID, so the collected raw page and
        # the unique-card count are the same after provider-side deduplication.
        audit["raw_cards"] = len(parsed_cards)
        audit["unique_cards"] = len(parsed_cards)

    def enforce_card_limit() -> bool:
        if max_cards is not None:
            for hotel_id in list(parsed_cards)[max_cards:]:
                del parsed_cards[hotel_id]
        update_card_counts()
        return max_cards is not None and len(parsed_cards) >= max_cards

    capture_visible_cards(parsed_cards)
    initial_limit_reached = enforce_card_limit()
    audit["initial_unique_cards"] = len(parsed_cards)
    previous_ids = set(parsed_cards)
    if initial_limit_reached:
        audit["collection_reason"] = "card_limit_reached"
        audit["collection_complete"] = True
        log.info("[TRIP] card limit reached: %d", max_cards)
        log.info("[TRIP] discovery stopped normally")
        audit["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return parsed_cards, True, "card_limit_reached", audit

    try:
        previous = result_panel.evaluate(state_script)
    except Exception as exc:
        update_card_counts()
        reason = "scroll_timeout" if "timeout" in str(exc).casefold() else "result_container_unavailable"
        audit["collection_reason"] = reason
        return parsed_cards, False, reason, audit

    stable_end = 0
    stable_end_since: float | None = None
    loading_stalled_since: float | None = None
    stalled_continuations = 0
    no_progress_streak = 0
    # Trip's result list advances from real wheel events over the hotel list.
    # Programmatic scrollTo changes the filter sidebar/window but does not
    # activate Trip's inventory continuation handler.
    passive_hold_pending = False
    bottom_probe_pending = False
    last_pending_requests = 0
    bottom_probe_wheels = 0
    # A first inventory response can be fetchDynamicRefreshList.  Trip only
    # schedules the real fetchHotelList continuation after another wheel at
    # the document bottom; treating the completed refresh as terminal loses
    # the remaining inventory (the observed 12-card -> 150-card split).
    inventory_request_seen = False
    continuation_consumed = False
    for scroll_index in range(max_scrolls):
        if time.monotonic() - started >= max_duration_seconds:
            audit["collection_reason"] = "scroll_timeout"
            audit["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            update_card_counts()
            return parsed_cards, False, "scroll_timeout", audit
        try:
            count_before = len(parsed_cards)
            before = previous
            visible_before = cards.count() if hasattr(cards, "count") else None
            page_url_before = getattr(page, "url", None)
            settle_result = None
            if passive_hold_pending and not bottom_probe_pending:
                hold_ms = max(0, int(round(pagination_idle_wait_seconds * 1000)))
                if hold_ms:
                    page.wait_for_timeout(hold_ms)
                settle_result = {"elapsed_ms": hold_ms, "passive_hold": True}
                audit["pagination_idle_waits"] += 1
                passive_hold_pending = False
            elif (last_pending_requests > 0 and hasattr(page, "evaluate")
                  and hasattr(page, "mouse")):
                wait_ms = 214
                page.wait_for_timeout(wait_ms)
                settle_result = {"elapsed_ms": wait_ms, "waiting_for_inventory": True}
            elif hasattr(page, "evaluate") and hasattr(page, "mouse"):
                wheel_target = page.evaluate(r"""() => {
                  const list = document.querySelector('.hotel-list');
                  if (!list) return null;
                  const view = list.ownerDocument.defaultView;
                  const rects = Array.from(list.querySelectorAll('.hotel-card, .list-item')).map(card => {
                    const rect = card.getBoundingClientRect();
                    return {x: rect.left, y: rect.top, right: rect.right, bottom: rect.bottom};
                  });
                  const rect = rects.find(item => item.right > 0 && item.x < view.innerWidth
                    && item.bottom > 0 && item.y < view.innerHeight);
                  return rect ? {x: Math.min(view.innerWidth - 1, Math.max(1, (rect.x + rect.right) / 2)),
                    y: Math.min(view.innerHeight - 1, Math.max(1, (rect.y + rect.bottom) / 2))}
                    : {x: view.innerWidth * 0.72, y: view.innerHeight * 0.5};
                }""")
                if not isinstance(wheel_target, dict) or "x" not in wheel_target or "y" not in wheel_target:
                    update_card_counts()
                    audit["collection_reason"] = "result_scroll_target_unavailable"
                    audit["scrolls"] = scroll_index
                    return parsed_cards, False, "result_scroll_target_unavailable", audit
                page.mouse.move(float(wheel_target["x"]), float(wheel_target["y"]))
                page.mouse.wheel(0, max(500, int(before.get("window_client", 0) * 0.85)))
                settle_result = page.evaluate(r"""() => new Promise(resolve => {
                  const list = document.querySelector('.hotel-list');
                  const root = document.documentElement;
                  const started = performance.now();
                  let quietTimer;
                  let done = false;
                  const observer = new MutationObserver(() => schedule());
                  const loading = () => !!(list && Array.from(list.querySelectorAll(
                    '[aria-busy="true"], [role="progressbar"], [class*="loading"], '
                    + '[class*="spinner"], [class*="skeleton"]'
                  )).some(node => {
                    const style = getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && (/loading|spinner|skeleton|загрузк/i.test(String(node.className || ''))
                        || node.getAttribute('aria-busy') === 'true'
                        || node.getAttribute('role') === 'progressbar');
                  }));
                  const finish = () => {
                    if (done) return;
                    done = true;
                    observer.disconnect();
                    resolve({elapsed_ms: Math.round(performance.now() - started), loading: loading()});
                  };
                  const schedule = () => {
                    clearTimeout(quietTimer);
                    if (performance.now() - started >= 100 && !loading()) {
                      quietTimer = setTimeout(finish, 100);
                    }
                  };
                  observer.observe(list || root, {subtree: true, childList: true, attributes: true,
                    attributeFilter: ['class', 'aria-busy', 'aria-label']});
                  setTimeout(schedule, 100);
                  setTimeout(finish, 900);
                })""")
            else:
                result_panel.evaluate(
                    "element => element.scrollBy(0, Math.max(500, element.clientHeight * 0.85))"
                )
                page.wait_for_timeout(450)
            # A bottom probe deliberately takes the normal wheel branch above
            # instead of the passive wait branch.
            bottom_probe_pending = False
            if after_wait is not None:
                external_state = after_wait() or {}
            else:
                external_state = {}
            pending_requests = external_state.get("pending_requests", 0) \
                if isinstance(external_state, dict) else 0
            pending_all_requests = external_state.get("pending_all_requests", pending_requests) \
                if isinstance(external_state, dict) else pending_requests
            pending_request_paths = external_state.get("pending_request_paths", []) \
                if isinstance(external_state, dict) else []
            pending_inventory_paths = external_state.get("pending_inventory_paths", []) \
                if isinstance(external_state, dict) else []
            if pending_inventory_paths:
                inventory_request_seen = True
            capture_visible_cards(parsed_cards)
            card_limit_reached = enforce_card_limit()
            after = result_panel.evaluate(state_script)
        except Exception as exc:
            update_card_counts()
            reason = "scroll_timeout" if "timeout" in str(exc).casefold() else "scroll_error"
            audit["scrolls"] = scroll_index + 1
            audit["collection_reason"] = reason
            audit["collection_error_class"] = type(exc).__name__
            audit["collection_error"] = str(exc)[:240]
            return parsed_cards, False, reason, audit

        count_after = len(parsed_cards)
        scroll_progress = (
            before.get("top") != after.get("top")
            or before.get("height") != after.get("height")
            or before.get("window_top") != after.get("window_top")
            or before.get("window_height") != after.get("window_height")
            or count_after != count_before
        )
        container_at_bottom = after.get("top", 0) + after.get("client", 0) >= after.get("height", 0) - 8
        window_metrics_available = all(
            after.get(field) is not None
            for field in ("window_top", "window_height", "window_client")
        )
        window_at_bottom = window_metrics_available and (
            after["window_top"] + after["window_client"] >= after["window_height"] - 8
        )
        # Trip's .filter-fixed-content is the filter sidebar, not the hotel
        # list's scroller.  The list expands in document flow; real wheel input
        # over a hotel card scrolls the document and activates Trip pagination.
        # The production result list advances the document window. Older
        # adapters and lightweight page doubles may only expose container
        # metrics; use those when window metrics are unavailable, but never
        # infer "bottom" from missing values alone.
        at_bottom = window_at_bottom if window_metrics_available else container_at_bottom
        continuation_action = None
        if not card_limit_reached and at_bottom and after.get("continuation"):
            try:
                continuation_action = result_panel.evaluate(click_continuation_script)
                if continuation_action:
                    continuation_consumed = True
                    page.wait_for_timeout(450)
                    if after_wait is not None:
                        external_state = after_wait() or {}
                        pending_requests = external_state.get("pending_requests", 0) \
                            if isinstance(external_state, dict) else 0
                        pending_all_requests = external_state.get("pending_all_requests", pending_requests) \
                            if isinstance(external_state, dict) else pending_requests
                        pending_request_paths = external_state.get("pending_request_paths", []) \
                            if isinstance(external_state, dict) else []
                        pending_inventory_paths = external_state.get("pending_inventory_paths", []) \
                            if isinstance(external_state, dict) else []
                        if pending_inventory_paths:
                            inventory_request_seen = True
                    capture_visible_cards(parsed_cards)
                    card_limit_reached = enforce_card_limit()
                    after = result_panel.evaluate(state_script)
                    count_after = len(parsed_cards)
                    container_at_bottom = after.get("top", 0) + after.get("client", 0) >= after.get("height", 0) - 8
                    window_at_bottom = (
                        after.get("window_top") is None or after.get("window_height") is None
                        or after.get("window_client") is None
                        or after.get("window_top", 0) + after.get("window_client", 0)
                        >= after.get("window_height", 0) - 8
                    )
                    at_bottom = container_at_bottom and window_at_bottom
            except Exception as exc:
                update_card_counts()
                reason = "continuation_timeout" if "timeout" in str(exc).casefold() else "continuation_error"
                audit["scrolls"] = scroll_index + 1
                audit["collection_reason"] = reason
                trace.append({"scroll": scroll_index + 1, "continuation_action": continuation_action,
                              "error": type(exc).__name__})
                return parsed_cards, False, reason, audit

        added_ids = sorted(set(parsed_cards) - previous_ids)
        trace_item = {
            "scroll": scroll_index + 1,
            "cards_before": count_before,
            "cards_after": count_after,
            "cards_added": count_after - count_before,
            "new_hotel_ids": added_ids,
            "visible_cards_before": visible_before,
            "visible_cards_after": cards.count() if hasattr(cards, "count") else None,
            "page_height_before": before.get("height"),
            "page_height_after": after.get("height"),
            "window_top_before": before.get("window_top"),
            "window_top_after": after.get("window_top"),
            "window_height": after.get("window_height"),
            "window_client": after.get("window_client"),
            "settle_wait_ms": settle_result.get("elapsed_ms") if isinstance(settle_result, dict) else None,
            "pending_requests_after": pending_requests,
            "pending_all_requests_after": pending_all_requests,
            "pending_request_paths_after": pending_request_paths,
            "pending_inventory_paths_after": pending_inventory_paths,
            "scroll_top_before": before.get("top"),
            "scroll_top_after": after.get("top"),
            "viewport": after.get("client"),
            "at_bottom": at_bottom,
            "scroll_progress": scroll_progress,
            "scroll_input": (
                "waiting_for_inventory" if isinstance(settle_result, dict)
                and settle_result.get("waiting_for_inventory") else
                "passive_hold" if isinstance(settle_result, dict)
                and settle_result.get("passive_hold") else "mouse_wheel"
            ),
            "loading_before": bool(before.get("loading")),
            "loading_after": bool(after.get("loading")),
            "continuation_before": before.get("continuation"),
            "continuation_after": after.get("continuation"),
            "continuation_action": continuation_action,
            "dom_mutations_before": before.get("mutations"),
            "dom_mutations_after": after.get("mutations"),
            "page_url_before": page_url_before,
            "page_url_after": getattr(page, "url", None),
        }
        trace.append(trace_item)
        audit["scrolls"] = scroll_index + 1
        audit["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        update_card_counts()
        if card_limit_reached:
            audit["collection_reason"] = "card_limit_reached"
            audit["collection_complete"] = True
            log.info("[TRIP] card limit reached: %d", max_cards)
            log.info("[TRIP] discovery stopped normally")
            return parsed_cards, True, "card_limit_reached", audit
        height_not_increased = after.get("height", 0) <= before.get("height", 0)
        window_height_not_increased = after.get("window_height", 0) <= before.get("window_height", 0)
        is_loading = bool(after.get("loading")) or pending_requests > 0
        has_continuation = bool(after.get("continuation"))
        content_progress = (
            bool(added_ids)
            or after.get("height", 0) > before.get("height", 0)
            or after.get("window_height", 0) > before.get("window_height", 0)
            or bool(continuation_action)
        )
        if is_loading:
            stable_end = 0
            if at_bottom and not content_progress:
                if loading_stalled_since is None:
                    loading_stalled_since = time.monotonic()
            else:
                loading_stalled_since = None
            if loading_stalled_since is not None and \
                    time.monotonic() - loading_stalled_since >= loading_timeout_seconds:
                audit["collection_reason"] = "loading_timeout"
                return parsed_cards, False, "loading_timeout", audit
        else:
            loading_stalled_since = None
        if continuation_action and count_after == count_before \
                and after.get("height") == before.get("height"):
            stalled_continuations += 1
        elif continuation_action:
            stalled_continuations = 0
        if stalled_continuations >= 2:
            audit["collection_reason"] = "continuation_no_progress"
            return parsed_cards, False, "continuation_no_progress", audit
        if at_bottom and not (added_ids) and height_not_increased and window_height_not_increased \
                and not is_loading and not has_continuation:
            # A bounded probe is only for the known five-card inventory shell.
            # An empty result or a page that has just consumed an explicit
            # continuation must follow the normal terminal stability path.
            if (0 < len(parsed_cards) and (len(parsed_cards) <= 5 or inventory_request_seen)
                    and not continuation_consumed
                    and not continuation_action and bottom_probe_wheels < 3):
                bottom_probe_wheels += 1
                audit["bottom_probe_wheels"] = bottom_probe_wheels
                stable_end = 0
                stable_end_since = None
                passive_hold_pending = False
                bottom_probe_pending = True
                last_pending_requests = pending_requests
                previous = after
                previous_ids = set(parsed_cards)
                continue
            if stable_end_since is None:
                stable_end_since = time.monotonic()
            stable_end += 1
            stable_quiet_elapsed = time.monotonic() - stable_end_since
            if stable_end >= 2 and stable_quiet_elapsed >= stable_end_quiet_seconds:
                audit["stable_end_scrolls"] = stable_end
                audit["collection_reason"] = "stable_end"
                audit["completion_conditions"] = {
                    "at_bottom": True, "container_at_bottom": container_at_bottom,
                    "window_at_bottom": window_at_bottom, "no_new_ids": True,
                    "height_not_increased": True, "window_height_not_increased": True,
                    "loading": False, "pending_requests": 0, "continuation": False,
                    "pending_all_requests": pending_all_requests,
                    "stable_quiet_elapsed_ms": round(stable_quiet_elapsed * 1000),
                }
                return parsed_cards, True, None, audit
            # Keep the page still before the next state check.  This is a
            # lifecycle guard for delayed/debounced inventory pagination, not
            # a second collection timeout: the normal card, DOM, and request
            # checks above still decide whether the search can finish.
            passive_hold_pending = True
        else:
            stable_end = 0
            stable_end_since = None
            passive_hold_pending = False
        waiting_for_inventory = isinstance(settle_result, dict) and bool(
            settle_result.get("waiting_for_inventory")
        )
        if not scroll_progress and not at_bottom and not is_loading and not has_continuation \
                and not waiting_for_inventory:
            # A real wheel can be lost while the virtualized list is settling
            # just after fetchHotelList completes.  The trace from a live
            # production-like run showed exactly one such event at 142/150:
            # the viewport was still above the bottom and the preceding
            # inventory request had just added cards.  Retry a few input
            # events before classifying the source as incomplete; this is a
            # bounded input retry, not an extended timeout.
            no_progress_streak += 1
            audit["no_progress_retries"] = no_progress_streak
            if no_progress_streak <= 3:
                last_pending_requests = pending_requests
                previous = after
                previous_ids = set(parsed_cards)
                continue
            audit["collection_reason"] = "scroll_no_progress"
            return parsed_cards, False, "scroll_no_progress", audit
        if scroll_progress:
            no_progress_streak = 0
        last_pending_requests = pending_requests
        previous = after
        previous_ids = set(parsed_cards)
    audit["collection_reason"] = "scroll_limit_reached"
    update_card_counts()
    return parsed_cards, False, "scroll_limit_reached", audit


def _tripcom_empty_inventory_shell(hotels: list[dict[str, Any]],
                                   reported_total: int | None,
                                   collection_audit: dict[str, Any]) -> bool:
    """Identify a small DOM shell whose continuation returned no inventory.

    Trip has been observed to render 5 or 12 cards while reporting thousands
    of hotels.  In that state its inventory endpoints may acknowledge with
    HTTP 200 and a ``ResponseStatus``-only body, or may not be requested at
    all.  Neither is evidence that pagination naturally ended.
    """
    if len(hotels) > 12:
        return False
    requests = collection_audit.get("inventory_requests")
    if not isinstance(requests, list) or not requests:
        # Without an inventory request, a reported total larger than the DOM
        # shell is the only evidence that pagination was expected.  Preserve
        # the older five-card recovery behavior for that case.  When the page
        # also omitted its total, completeness cannot be established from the
        # DOM shell alone; keep the result partial instead of claiming a
        # natural end.
        return reported_total is None or reported_total > len(hotels)
    payload_seen = False
    for item in requests:
        if not isinstance(item, dict):
            continue
        if item.get("response_has_data") is True:
            payload_seen = True
            break
        response_bytes = item.get("response_bytes")
        if isinstance(response_bytes, (int, float)) and response_bytes > 1000:
            payload_seen = True
            break
    if payload_seen:
        # At least one valid inventory response proves that the source did
        # deliver real inventory.  A later empty response can therefore be a
        # normal terminal page rather than the empty-shell recovery signature.
        return False
    # Requests were actually made, but none carried inventory data.  This is
    # incomplete even when the retry lost the source-reported total (the
    # recovery path seen in the live probe), or when the reported total happens
    # to equal the visible shell.  DOM cards and ResponseStatus-only HTTP 200
    # bodies are not proof of a complete search.
    return True

def _tripcom_hotel_route(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        return None
    results = [item for item in payload["result"] if isinstance(item, dict)]
    suggested_districts = {str(item.get("id")) for item in results
                           if item.get("resourceType") == "district" and item.get("id") is not None}
    for item in results:
        if item.get("resourceType") != "districthotel":
            continue
        if suggested_districts and str(item.get("id")) not in suggested_districts:
            continue
        route = item.get("url")
        if not isinstance(route, str):
            continue
        parsed = urlsplit(route)
        route_params = parse_qs(parsed.query)
        city_id = (route_params.get("city") or route_params.get("cityId") or [None])[0]
        if not parsed.scheme and not parsed.netloc \
                and parsed.path in {"/hotels/listpage/", "/hotels/list"} \
                and city_id and city_id.isdigit():
            return city_id, route
    return None

def _tripcom_browser_search(query: dict[str, Any], profile: Path, *,
                            _recovery_attempt: bool = False) -> SourceResult:
    if not TRIP_PROFILE_LOCK.acquire(blocking=False):
        log.warning("[TRIP_ERROR] stage=browser_lock error_class=browser_busy city=%s",
                    query.get("city_name"))
        return SourceResult("trip_com", "unavailable", reason="A Trip.com browser search is already running")
    current_stage = "browser_start"
    reused_browser = False
    lock_released_for_retry = False
    started = time.perf_counter()
    try:
        from playwright.sync_api import sync_playwright

        profile.mkdir(parents=True, exist_ok=True)
        response_statuses: list[tuple[int, str]] = []
        autocomplete_statuses: list[int] = []
        autocomplete_payloads: list[dict[str, Any]] = []
        pending_provider_requests: dict[int, Any] = {}
        inventory_request_audit: list[dict[str, Any]] = []
        requested_city = " ".join(str(query.get("city_name") or "").split()).casefold()
        timings: dict[str, float] = {}
        stage_started = time.perf_counter()
        log.info("[TRIP_BROWSER_START] city=%s profile=%s", query.get("city_name"), profile)
        _manager, context, reused_browser = _tripcom_reusable_context(profile, sync_playwright)
        timings["browser_launch_or_reuse_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
        log.info("[TRIP_BROWSER_%s] city=%s elapsed_ms=%.1f",
                 "REUSE" if reused_browser else "STARTED", query.get("city_name"),
                 timings["browser_launch_or_reuse_ms"])
        try:
                current_stage = "page_create"
                page, context, reused_browser = _tripcom_new_page_with_recovery(
                    context, reused_browser, profile, sync_playwright)
                timings["browser_launch_or_reuse_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
                # 2026-10-14: Trip.com deployed a service worker (sw.js?v=20261014)
                # whose cached state renders an empty results shell (filters, no
                # cards) on controlled pages. Unregistering the worker and
                # clearing web storage on a domain landing is the standard
                # "clear site data" recovery a user performs - but cookies are
                # deliberately preserved: they carry the authenticated Trip.com
                # session (cticket/login_uid) the user signs in manually.
                try:
                    page.goto("https://ru.trip.com/", wait_until="domcontentloaded", timeout=30000)
                    page.evaluate(
                        "async () => {"
                        " try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}"
                        " try { const regs = await navigator.serviceWorker.getRegistrations();"
                        " await Promise.all(regs.map(r => r.unregister())); } catch (e) {}"
                        "}"
                    )
                    log.info("[TRIP_SEARCH] stage=site_storage_reset city=%s",
                             query.get("city_name"))
                except Exception:
                    pass
                page.on("response", lambda response: response_statuses.append(
                    (response.status, urlparse(response.url).path)
                ) if response.status in (401, 403, 429, 430) else None)

                def track_provider_request(request: Any) -> None:
                    host = (urlsplit(request.url).hostname or "").casefold()
                    if (request.resource_type in {"xhr", "fetch"}
                            and (host == "trip.com" or host.endswith(".trip.com"))):
                        pending_provider_requests[id(request)] = request

                def finish_provider_request(request: Any) -> None:
                    pending_provider_requests.pop(id(request), None)

                def audit_inventory_request(request: Any, *, failed: bool = False) -> None:
                    path = urlsplit(request.url).path
                    if not path.endswith(("/fetchHotelList", "/fetchDynamicRefreshList")):
                        return
                    record: dict[str, Any] = {
                        "path": path, "method": request.method,
                        "request_bytes": len((request.post_data or "").encode("utf-8")),
                    }
                    try:
                        payload = json.loads(request.post_data or "{}")
                        if isinstance(payload, dict):
                            for key in ("pageIndex", "pageSize", "page", "limit", "offset", "cursor"):
                                if key in payload and isinstance(payload[key], (str, int, float, bool)):
                                    record[key] = payload[key]
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                    try:
                        response = request.response()
                        if response is not None:
                            record["http_status"] = response.status
                            length = response.headers.get("content-length")
                            if length and str(length).isdigit():
                                record["response_bytes"] = int(length)
                            # Trip can acknowledge an inventory request with
                            # HTTP 200 while returning only ResponseStatus and
                            # no ``data`` payload.  Keep this distinction in
                            # the audit so a source-side empty continuation is
                            # never mistaken for natural pagination exhaustion.
                            try:
                                payload = response.json()
                                record["response_top_level_keys"] = (
                                    list(payload.keys())[:20]
                                    if isinstance(payload, dict) else []
                                )
                                record["response_has_data"] = bool(
                                    isinstance(payload, dict) and payload.get("data")
                                )
                            except Exception:
                                record["response_has_data"] = None
                    except Exception as exc:
                        record["response_error"] = type(exc).__name__
                    if failed:
                        try:
                            record["failure"] = request.failure
                        except Exception:
                            record["failure"] = "unknown"
                    record["event"] = "request_failed" if failed else "request_finished"
                    inventory_request_audit.append(record)

                page.on("request", track_provider_request)
                page.on("requestfinished", lambda request: (
                    audit_inventory_request(request), finish_provider_request(request)
                ))
                page.on("requestfailed", lambda request: (
                    audit_inventory_request(request, failed=True), finish_provider_request(request)
                ))

                def capture_autocomplete(response: Any) -> None:
                    if urlparse(response.url).path != TRIP_AUTOCOMPLETE_PATH:
                        return
                    autocomplete_statuses.append(response.status)
                    if response.status != 200:
                        return
                    try:
                        request_body = json.loads(response.request.post_data or "{}")
                        request_city = " ".join(str(request_body.get("keyword") or "").split()).casefold()
                        if request_city == requested_city:
                            autocomplete_payloads.append(response.json())
                    except (json.JSONDecodeError, TypeError, ValueError):
                        return

                page.on("response", capture_autocomplete)
                cached_destination = _TRIP_CITY_ROUTES.get(requested_city)
                http_status = None
                if cached_destination is None:
                    current_stage = "navigation_home"
                    stage_started = time.perf_counter()
                    log.info("[TRIP_NAVIGATION] stage=home url=https://ru.trip.com/ city=%s",
                             query.get("city_name"))
                    response = page.goto("https://ru.trip.com/", wait_until="domcontentloaded", timeout=45000)
                    timings["home_navigation_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
                    city_input = page.locator(".gccpoi__TripSearchBox-input")
                    try:
                        city_input.wait_for(state="visible", timeout=10000)
                    except Exception:
                        pass
                    http_status = response.status if response else None
                    if http_status is not None and http_status >= 500:
                        return SourceResult("trip_com", "http_error", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="http_error",
                                            reason=f"Trip.com home page returned HTTP {http_status}")
                    blocked = next((status for status, _path in response_statuses if status in (403, 429, 430)), None)
                    if blocked:
                        log.warning("[TRIP_BLOCKED] stage=home_navigation http_status=%s", blocked)
                        return SourceResult("trip_com", "blocked", http_status=blocked,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="access_denied",
                                            reason=f"Trip.com returned HTTP {blocked}; search stopped without retry")
                    if any(status == 401 for status, _path in response_statuses):
                        return SourceResult("trip_com", "login_required", http_status=401,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="authentication_required",
                                            reason="Trip.com requires sign-in in the saved browser profile")
                    current_url = page.url.casefold()
                    body = page.locator("body").inner_text(timeout=5000)
                    if "/account/signin" in current_url or "/account/login" in current_url:
                        return SourceResult("trip_com", "login_required", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="authentication_required",
                                            reason="Trip.com redirected the normal browser flow to sign-in")
                    if re.search(r"captcha|verify that you are human|access denied|unusual traffic", body, re.I):
                        log.warning("[TRIP_BLOCKED] stage=home_navigation reason=access_challenge")
                        return SourceResult("trip_com", "blocked", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="challenge_or_access_denied",
                                            reason="Trip.com displayed an access challenge; search stopped")
                    if city_input.count() == 0:
                        return SourceResult("trip_com", "parse_error", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="autocomplete_input_missing",
                                            reason="Trip.com home page did not expose its normal destination input")
                    stage_started = time.perf_counter()
                    current_stage = "search_autocomplete"
                    log.info("[TRIP_SEARCH] stage=autocomplete city=%s", query.get("city_name"))
                    city_input.click()
                    city_input.press("Control+A")
                    city_input.press("Backspace")
                    city_input.type(str(query.get("city_name") or ""), delay=35)
                    autocomplete_deadline = time.monotonic() + 5
                    while not autocomplete_payloads and time.monotonic() < autocomplete_deadline:
                        page.wait_for_timeout(100)
                    timings["autocomplete_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
                    blocked = next((status for status, _path in response_statuses if status in (403, 429, 430)), None)
                    if blocked:
                        log.warning("[TRIP_BLOCKED] stage=autocomplete http_status=%s", blocked)
                        return SourceResult("trip_com", "blocked", http_status=blocked,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="access_denied",
                                            reason=f"Trip.com autocomplete returned HTTP {blocked}; search stopped")
                    failed_autocomplete_status = next((status for status in reversed(autocomplete_statuses)
                                                       if status >= 400), None)
                    if failed_autocomplete_status is not None:
                        return SourceResult("trip_com", "http_error", http_status=failed_autocomplete_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="http_error",
                                            reason=f"Trip.com autocomplete returned HTTP {failed_autocomplete_status}")
                    autocomplete = next((payload for payload in reversed(autocomplete_payloads)
                                         if payload.get("result")), None)
                    if autocomplete is None:
                        return SourceResult("trip_com", "unavailable", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="city_not_found",
                                            reason="Trip.com autocomplete returned no destination suggestions")
                    destination = _tripcom_hotel_route(autocomplete)
                    if destination is None:
                        return SourceResult("trip_com", "unavailable", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="hotel_city_not_found",
                                            reason="Trip.com autocomplete returned no hotel search route for this destination")
                    with _TRIP_SESSION_LOCK:
                        _TRIP_CITY_ROUTES[requested_city] = destination
                else:
                    destination = cached_destination
                    timings["route_cache"] = "hit"
                city_id, hotel_route = destination
                result_url = urlunsplit(("https", "ru.trip.com", urlsplit(hotel_route).path,
                                         urlencode({**{key: value[0] for key, value in parse_qs(
                                             urlsplit(hotel_route).query).items()},
                                             "checkin": query["check_in"], "checkout": query["check_out"],
                                             "crn": 1, "adult": query["adults"],
                                             "children": query.get("children_count", 0),
                                             "curr": "RUB", "locale": "ru-RU"}), ""))
                stage_started = time.perf_counter()
                current_stage = "navigation_results"
                log.info("[TRIP_NAVIGATION] stage=results url=%s", result_url)
                response = page.goto(result_url, wait_until="domcontentloaded", timeout=45000)
                timings["results_navigation_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
                try:
                    page.locator(".hotel-card, .hotel-list .list-item").first.wait_for(state="visible", timeout=7000)
                except Exception:
                    pass
                # 2026-10-14: Trip.com's listpage sometimes renders only the filter
                # shell on a direct URL load - no hotel cards and no inventory XHR -
                # until the page's own search form is submitted natively. Click the
                # page's own search button once when the shell stays empty; this is
                # a normal user gesture, not an anti-bot bypass.
                if page.locator(".hotel-card, .hotel-list .list-item").count() == 0:
                    log.info("[TRIP_SEARCH] stage=results_shell_nudge city=%s",
                             query.get("city_name"))
                    try:
                        nudge = page.locator(
                            '.btn-search, button:has-text("Найти"), button:has-text("Поиск")'
                        ).first
                        nudge.click(timeout=3000)
                        page.locator(".hotel-card, .hotel-list .list-item").first.wait_for(state="visible", timeout=15000)
                    except Exception:
                        pass
                page.wait_for_timeout(1000)
                http_status = response.status if response else http_status
                if http_status is not None and http_status >= 500:
                    return SourceResult("trip_com", "http_error", http_status=http_status,
                                        latency_ms=int((time.perf_counter() - started) * 1000),
                                        error_class="http_error",
                                        reason=f"Trip.com result page returned HTTP {http_status}")
                blocked = next((status for status, _path in response_statuses if status in (403, 429, 430)), None)
                if blocked:
                    log.warning("[TRIP_BLOCKED] stage=results_navigation http_status=%s", blocked)
                    return SourceResult("trip_com", "blocked", http_status=blocked,
                                        latency_ms=int((time.perf_counter() - started) * 1000),
                                        error_class="access_denied",
                                        reason=f"Trip.com returned HTTP {blocked}; search stopped without retry")
                current_url = page.url.casefold()
                body = page.locator("body").inner_text(timeout=5000)
                if re.search(r"captcha|verify that you are human|access denied|unusual traffic", body, re.I):
                    log.warning("[TRIP_BLOCKED] stage=results_navigation reason=access_challenge")
                    return SourceResult("trip_com", "blocked", http_status=http_status,
                                        latency_ms=int((time.perf_counter() - started) * 1000),
                                        error_class="challenge_or_access_denied",
                                        reason="Trip.com displayed an access challenge; search stopped")
                result_params = parse_qs(urlsplit(page.url).query)
                expected_nights = (date.fromisoformat(query["check_out"])
                                   - date.fromisoformat(query["check_in"])).days
                context_matches = (
                    result_params.get("checkin") == [query["check_in"]]
                    and result_params.get("checkout") == [query["check_out"]]
                    and result_params.get("adult") == [str(query["adults"])]
                    and re.search(rf"\b{expected_nights}\s*(?:ноч\w*|night\w*)", body, re.I)
                    and re.search(rf"\b{query['adults']}\s+(?:взросл\w*|adults?)", body, re.I)
                )
                if not context_matches:
                    return SourceResult("trip_com", "parse_error", http_status=http_status,
                                        latency_ms=int((time.perf_counter() - started) * 1000),
                                        error_class="search_context_mismatch",
                                        reason="Trip.com page did not confirm the requested dates and occupancy")
                # 2026-10-14: Trip.com redesigned the results page. Cards are now
                # .hotel-list > .list-item with the hotel name in a.hotelName
                # (hotelId= in its href); the legacy .hotel-card class no longer
                # exists. Accept BOTH layouts so an A/B rollback still works.
                cards = page.locator(".hotel-card, .hotel-list .list-item")
                if cards.count() == 0:
                    if "Вход/регистрация" in body:
                        return SourceResult("trip_com", "login_required", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="authentication_required",
                                            reason="Trip.com has no authenticated session in its browser profile")
                    if _tripcom_explicit_empty_results(body):
                        log.info("[TRIP_NO_RESULTS] stage=results city=%s", query.get("city_name"))
                        raw_page = {"sourceCityId": city_id, "totalHotels": 0,
                                    "sourceReportedTotal": 0, "nextPageToken": None,
                                    "providers": [{"code": "trip.com", "display_name": "Trip.com"}],
                                    "hotels": []}
                        return SourceResult("trip_com", "no_results", raw_page=raw_page,
                                            total_hotels=0, http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            reported_total=0)
                    return SourceResult("trip_com", "unavailable", http_status=http_status,
                                        latency_ms=int((time.perf_counter() - started) * 1000),
                                        error_class="incomplete_collection",
                                        collection_complete=False,
                                        collection_reason="result_container_unavailable",
                                        reason="Trip.com page loaded without hotel cards or a result container")
                parsed_cards: dict[str, dict[str, Any]] = {}
                card_parse_diagnostics = {"skipped": 0, "samples": []}

                def capture_visible_cards(target: dict[str, dict[str, Any]]) -> None:
                    nonlocal current_stage
                    current_stage = "parser"
                    snapshots = cards.evaluate_all("""els => els.map(card => ({
                      id: card.id || card.querySelector('[data-offline-hotelid]')?.getAttribute('data-offline-hotelid')
                          || null,
                      text: card.innerText || '',
                      name: card.querySelector('h2, h3, [class*=hotelName], [class*=hotel-name]')?.innerText || '',
                      links: Array.from(card.querySelectorAll('a[href]')).map(a => ({href: a.href,
                        text: (a.innerText || '').trim()})),
                      labels: Array.from(card.querySelectorAll('[aria-label], [title], img[alt]'))
                        .map(el => [el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt')]
                          .filter(Boolean).join(' ')),
                      images: Array.from(card.querySelectorAll('img')).map(img => ({
                        url: img.currentSrc || img.src, alt: img.alt || ''
                      })).filter(img => /^https:\\/\\//.test(img.url)
                        && /hotel overview picture|hotel room|hotel exterior|hotel lobby|hotel pool/i.test(img.alt))
                    }))""")
                    for snapshot in snapshots:
                        raw_hotel = _tripcom_card_snapshot_to_raw(snapshot)
                        if raw_hotel and raw_hotel.get("id") is not None:
                            target[str(raw_hotel["id"])] = raw_hotel
                        else:
                            card_parse_diagnostics["skipped"] += 1
                            if len(card_parse_diagnostics["samples"]) < 5:
                                card_parse_diagnostics["samples"].append({
                                    "id": snapshot.get("id"),
                                    "name": snapshot.get("name"),
                                    "reason": "missing_provider_id_or_name",
                                })

                def check_tripcom_state() -> dict[str, int]:
                    blocked = next((status for status, _path in response_statuses if status in (403, 429, 430)), None)
                    if blocked:
                        raise RuntimeError(f"Trip.com returned HTTP {blocked} while loading results")
                    challenge = page.evaluate(r"""() => {
                      const nodes = Array.from(document.querySelectorAll(
                        'h1, h2, [role="alert"], [id*="captcha" i], [class*="captcha" i], '
                        + 'iframe[src*="captcha" i], [data-testid*="challenge" i]'
                      ));
                      const text = [document.title, ...nodes.map(node => node.textContent || '')]
                        .join(' ').slice(0, 5000);
                      return /captcha|verify that you are human|access denied|unusual traffic/i.test(text);
                    }""")
                    if challenge:
                        raise RuntimeError("Trip.com displayed an access challenge while loading results")
                    pending_paths = [urlsplit(request.url).path
                                     for request in pending_provider_requests.values()]
                    inventory_paths = [path for path in pending_paths
                                       if path.endswith(("/fetchHotelList", "/fetchDynamicRefreshList"))]
                    return {"pending_requests": len(inventory_paths),
                            "pending_all_requests": len(pending_paths),
                            "pending_request_paths": sorted(set(pending_paths)),
                            "pending_inventory_paths": sorted(set(inventory_paths))}

                result_panel = page.locator(".hotel-list")
                if not result_panel.count():
                    log.warning("[TRIP_RESULTS] stage=container status=partial reason=hotel_list_unavailable")
                    return SourceResult(
                        "trip_com", "partial", http_status=http_status,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error_class="incomplete_collection", collection_complete=False,
                        collection_reason="hotel_list_unavailable",
                        reason="Trip.com hotel list was unavailable; collection is incomplete",
                    )
                stage_started = time.perf_counter()
                current_stage = "results_collection"
                log.info("[TRIP_SEARCH] stage=collect city=%s", query.get("city_name"))
                collection_card_limit = TRIPCOM_MAX_COLLECTION_CARDS
                log.info("[TRIP_SEARCH] stage=collect configured_card_limit=%d", collection_card_limit)
                parsed_cards, collection_complete, collection_reason, collection_audit = _tripcom_collect_visible_cards(
                    page, cards, result_panel, capture_visible_cards,
                    max_cards=collection_card_limit, after_wait=check_tripcom_state,
                    max_duration_seconds=TRIPCOM_COLLECTION_TIMEOUT_SECONDS,
                    # Trip can schedule its first inventory continuation after
                    # the initial DOM has settled without exposing a pending
                    # request. Keep a bounded grace window before declaring
                    # five initial cards to be the complete inventory.
                    stable_end_quiet_seconds=8.0,
                )
                current_stage = "results_collection"
                timings["collection_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
                collection_reason = collection_reason or collection_audit.get("collection_reason")
                collection_audit["card_parse_errors"] = card_parse_diagnostics
                collection_audit["inventory_requests"] = inventory_request_audit
                if not collection_complete:
                    challenge = page.evaluate(r"""() => {
                      const nodes = Array.from(document.querySelectorAll(
                        'h1, h2, [role="alert"], [id*="captcha" i], [class*="captcha" i], '
                        + 'iframe[src*="captcha" i], [data-testid*="challenge" i]'
                      ));
                      return /captcha|verify that you are human|access denied|unusual traffic/i
                        .test([document.title, ...nodes.map(node => node.textContent || '')].join(' ').slice(0, 5000));
                    }""")
                    blocked = next((status for status, _path in response_statuses if status in (403, 429, 430)), None)
                    if blocked:
                        log.warning("[TRIP_BLOCKED] stage=results_collection http_status=%s", blocked)
                        return SourceResult("trip_com", "blocked", http_status=blocked,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="access_denied",
                                            reason=f"Trip.com returned HTTP {blocked} while loading results; stopped")
                    if challenge:
                        log.warning("[TRIP_BLOCKED] stage=results_collection reason=access_challenge")
                        return SourceResult("trip_com", "blocked", http_status=http_status,
                                            latency_ms=int((time.perf_counter() - started) * 1000),
                                            error_class="challenge_or_access_denied",
                                            reason="Trip.com displayed an access challenge while loading results; stopped")
                hotels = list(parsed_cards.values())
                current_stage = "parser"
                log.info("[TRIP_RESULTS] stage=parsed city=%s count=%d complete=%s",
                         query.get("city_name"), len(hotels), collection_complete)
                count_label = page.locator(".hotel-count-tip")
                total_text = count_label.inner_text(timeout=2000) if count_label.count() else ""
                # The count widget can exist before its text is populated;
                # fall back to the already captured document body rather than
                # losing the source-reported inventory total.
                reported_total = (_tripcom_reported_total(total_text)
                                  or _tripcom_reported_total(body))
                if reported_total is None:
                    try:
                        reported_total = _tripcom_reported_total(
                            page.locator("body").inner_text(timeout=2000)
                        )
                    except Exception:
                        pass
                empty_inventory_shell = _tripcom_empty_inventory_shell(
                    hotels, reported_total, collection_audit,
                )
                collection_audit["empty_inventory_shell"] = empty_inventory_shell
                # Trip occasionally serves a small visual shell while its
                # inventory continuation is absent or acknowledged with an
                # empty ResponseStatus-only body.  A real search with a
                # non-empty inventory can recover on a fresh browser page;
                # retry once, bounded and only for this source signature.
                if not _recovery_attempt and collection_complete and empty_inventory_shell:
                    log.warning(
                        "[TRIP_RECOVERY] reason=empty_inventory_shell "
                        "cards=%d reported_total=%s; restarting browser page once",
                        len(hotels), reported_total,
                    )
                    _discard_tripcom_reusable_context()
                    lock_released_for_retry = True
                    TRIP_PROFILE_LOCK.release()
                    return _tripcom_browser_search(
                        query, profile, _recovery_attempt=True,
                    )
                if empty_inventory_shell:
                    # A 200/acknowledged request without inventory data is an
                    # upstream partial result, not a legitimate end of
                    # pagination.  Keep the visible cards but expose the
                    # incomplete source state to persistence and the API.
                    collection_complete = False
                    collection_reason = "inventory_response_empty"
                    log.warning(
                        "[TRIP_RESULTS] stage=partial reason=inventory_response_empty "
                        "cards=%d reported_total=%s",
                        len(hotels), reported_total,
                    )
                raw_page = {"sourceCityId": city_id, "totalHotels": len(hotels), "sourceReportedTotal": reported_total,
                            "collectionComplete": collection_complete,
                            "collectionReason": collection_reason,
                            "collectionAudit": collection_audit,
                            "nextPageToken": None,
                            "providers": [{"code": "trip.com", "display_name": "Trip.com"}],
                            "hotels": hotels}
                stage_started = time.perf_counter()
                normalized_hotels = tuple(item for hotel in hotels
                                          if (item := normalize_hotel(
                                              hotel, utc_now(), source="trip_com",
                                              query={**query, "rooms": query.get("rooms", 1)},
                                          )) is not None)
                timings["normalization_ms"] = round((time.perf_counter() - stage_started) * 1000, 1)
                collection_audit["timings_ms"] = timings
                collection_audit["browser_reused"] = reused_browser
                status = "ok" if hotels and collection_complete else "partial" if hotels else "parse_error"
                log.info("[TRIP_RESULTS] stage=normalized city=%s count=%d status=%s",
                         query.get("city_name"), len(normalized_hotels), status)
                log.info("[TRIP_TIMING] city=%s status=%s reused=%s timings=%s total_ms=%d",
                         query.get("city_name"),
                         status,
                         reused_browser, timings, int((time.perf_counter() - started) * 1000))
                if status == "parse_error":
                    log.error("[TRIP_ERROR] stage=parser error_class=card_parse count=0")
                return SourceResult("trip_com", status,
                                    normalized_hotels,
                                    raw_page, len(hotels), None, http_status,
                                    int((time.perf_counter() - started) * 1000),
                                    None if hotels else "card_parse",
                                    None if hotels else "Trip.com cards were present but their fields could not be parsed",
                                    reported_total=reported_total,
                                    collection_complete=collection_complete,
                                    collection_reason=collection_reason,
                                    collection_audit=collection_audit)
        finally:
            try:
                page.close()
            except Exception:
                pass
    except Exception as exc:
        exception_name = type(exc).__name__
        message = str(exc)[:300]
        if re.search(r"HTTP\s+(?:403|429|430)|access challenge", message, re.I):
            status, error_class = "blocked", "challenge_or_access_denied"
            log.warning("[TRIP_BLOCKED] stage=%s error_class=%s message=%s", current_stage, error_class, message)
        elif "timeout" in exception_name.casefold():
            status, error_class = "timeout", f"{current_stage}_timeout"
            log.warning("[TRIP_TIMEOUT] stage=%s error_class=%s message=%s", current_stage, error_class, message)
        elif current_stage == "parser":
            status, error_class = "error", "parser_error"
            log.exception("[TRIP_ERROR] stage=parser error_class=%s", exception_name)
        elif current_stage in {"browser_start", "page_create"}:
            status, error_class = "error", "browser_launch_error"
            log.exception("[TRIP_ERROR] stage=%s error_class=%s", current_stage, error_class)
        elif any(term in exception_name.casefold() for term in ("targetclosed", "browserclosed", "pagecrashed")):
            status, error_class = "error", "stale_browser_or_page"
            log.exception("[TRIP_ERROR] stage=%s error_class=%s", current_stage, error_class)
        else:
            status, error_class = "error", f"{current_stage}_error"
            log.exception("[TRIP_ERROR] stage=%s error_class=%s", current_stage, error_class)
        return SourceResult("trip_com", status, error_class=error_class, reason=message,
                            latency_ms=int((time.perf_counter() - started) * 1000))
    finally:
        if not lock_released_for_retry:
            TRIP_PROFILE_LOCK.release()

def _tripcom_card_to_raw(card: Any) -> dict[str, Any] | None:
    text = " ".join(card.inner_text(timeout=3000).split())
    lines = [" ".join(line.split()) for line in card.inner_text(timeout=3000).splitlines() if line.strip()]
    links = card.locator("a[href]").evaluate_all("els => els.map(a => ({href:a.href,text:(a.innerText||'').trim()}))")
    hotel_url = next((link["href"] for link in links if "hotelId=" in link["href"]), None)
    hotel_id = None
    if hotel_url:
        hotel_id = parse_qs(urlparse(hotel_url).query).get("hotelId", [None])[0]
    if not hotel_id:
        card_id = card.get_attribute("id")
        hotel_id = card_id if card_id and card_id.isdigit() else None
    name = None
    for selector in ("h2", "h3", "[class*=hotelName]", "[class*=hotel-name]"):
        candidate = card.locator(selector).first
        if candidate.count():
            name = " ".join(candidate.inner_text(timeout=1000).split())
            if name:
                break
    if not name:
        name = next((link["text"] for link in links if link["text"] and "hotelId=" in link["href"]), None)
    if not name or not hotel_id:
        return None
    rating_match = re.search(r"\b(\d{1,2}(?:[.,]\d+)?)\s*/\s*10\b", text)
    reviews_match = re.search(r"([\d\s,]+)\s*(?:reviews?|отзыв(?:а|ов)?)", text, re.I)
    stars = _tripcom_card_stars(card)
    images = card.locator("img").evaluate_all("""
        els => els.map(img => ({url: img.currentSrc || img.src, alt: img.alt || ''}))
          .filter(img => /^https:\\/\\//.test(img.url)
            && /hotel overview picture|hotel room|hotel exterior|hotel lobby|hotel pool/i.test(img.alt))
    """)
    images = [dict(image, role="hotel_photo", source="trip_com") for image in images]
    map_index = next((index for index, line in enumerate(lines) if "посмотреть на карте" in line.casefold()), -1)
    room_type = next((line for line in lines[max(0, map_index + 1):map_index + 7]
                      if re.search(r"номер|комнат|suite|room|dorm|кроват|bed", line, re.I)), None)
    bed_match = re.search(r"\(([^)]*(?:кроват|beds?)[^)]*)\)", room_type or "", re.I)
    location_line = next((line for line in lines if "рядом:" in line.casefold()), "")
    location_prefix = re.split(r"рядом:", location_line, maxsplit=1, flags=re.I)[0]
    location_prefix = re.sub(r'["“«][^"”»]*["”»]', "", location_prefix)
    location_prefix = re.sub(r"центр города|посмотреть на карте", "", location_prefix, flags=re.I).strip(" ,–-\t")
    district_match = re.search(r"Рядом:\s*([^\"“«\n]+)", text, re.I)
    review_quotes = _tripcom_card_review_quotes(text)
    money = re.findall(r"(?<!\d)(\d[\d\u00a0\u202f ,]*(?:[.,]\d{1,2})?)\s*(₽|RUB)", text, re.I)
    offers = []
    if money:
        amount_text = money[-1][0].replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
        try:
            amount_minor = int((Decimal(amount_text) * 100).quantize(Decimal("1")))
        except (InvalidOperation, ValueError):
            amount_minor = 0
        if amount_minor > 0:
            labels = []
            if "бесплатная отмена" in text.casefold():
                labels.append("free_cancellation")
            if "без возврата" in text.casefold() or "невозврат" in text.casefold():
                labels.append("non_refundable")
            meal_plan = classify_meal_plan(text=text)
            if meal_plan == "breakfast":
                labels.append("breakfast_included")
            if "оплата в отеле" in text.casefold() or "pay at hotel" in text.casefold():
                labels.append("pay_at_hotel")
            if "вкл. налоги" in text.casefold() or "включая налоги" in text.casefold():
                labels.append("taxes_included")
            offers.append({"provider": "trip.com", "price": {"currency": "RUB", "value": amount_minor},
                           "deeplink": hotel_url, "labels": labels,
                           "room_type": room_type, "bed_type": bed_match.group(1) if bed_match else None,
                           "meal_plan": meal_plan,
                           "payment_method": "pay_at_hotel" if "pay_at_hotel" in labels else "unknown"})
    coordinate_value = None
    for link in links:
        if not re.search(r"map|карте|location", link["text"], re.I):
            continue
        coordinate_match = re.search(
            r"(?:mapCoordinate|coordinate|latlng|center)=(-?\d{1,2}\.\d+)\s*[,|%2C]\s*(-?\d{1,3}\.\d+)",
            unquote(link["href"]), re.I,
        )
        if coordinate_match:
            coordinate_value = {"lat": float(coordinate_match.group(1)), "lng": float(coordinate_match.group(2))}
            break
    return {
        "id": hotel_id, "name": name,
        "rating": float(rating_match.group(1).replace(",", ".")) if rating_match else None,
        "reviews_count": int(re.sub(r"\D", "", reviews_match.group(1))) if reviews_match else None,
        "review_quotes": review_quotes, "review_source": "trip_com" if review_quotes else None,
        "stars": stars, "images": images, "coordinates": coordinate_value,
        "amenities": [],
        "nearby_places": ([{"name": location_prefix, "type": "district"}]
                          if location_prefix else ([{"name": district_match.group(1).strip(), "type": "landmark"}]
                                                   if district_match else [])),
        "offers": offers,
    }

def _tripcom_card_review_quotes(text: str) -> list[str]:
    found = re.findall(r'["“«]([^"”»]{4,120})["”»]', text)
    return list(dict.fromkeys(" ".join(value.split()) for value in found if value.strip()))[:4]

def _tripcom_card_snapshot_to_raw(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    text = " ".join(str(snapshot.get("text") or "").split())
    lines = [" ".join(line.split()) for line in str(snapshot.get("text") or "").splitlines() if line.strip()]
    links = snapshot.get("links") if isinstance(snapshot.get("links"), list) else []
    hotel_url = next((link.get("href") for link in links
                      if isinstance(link, dict) and "hotelId=" in str(link.get("href") or "")), None)
    hotel_id = parse_qs(urlparse(hotel_url).query).get("hotelId", [None])[0] if hotel_url else None
    if not hotel_id and str(snapshot.get("id") or "").isdigit():
        hotel_id = str(snapshot["id"])
    name = " ".join(str(snapshot.get("name") or "").split()) or next(
        (" ".join(str(link.get("text") or "").split()) for link in links
         if isinstance(link, dict) and link.get("text") and "hotelId=" in str(link.get("href") or "")), None)
    if not name or not hotel_id:
        return None

    rating_match = re.search(r"\b(\d{1,2}(?:[.,]\d+)?)\s*/\s*10\b", text)
    reviews_match = re.search(r"([\d\s,]+)\s*(?:reviews?|отзыв(?:а|ов)?)", text, re.I)
    reviews_digits = re.sub(r"\D", "", reviews_match.group(1)) if reviews_match else ""
    stars = None
    for label in snapshot.get("labels") or []:
        if not re.search(r"(?:рейтинг|rating|star|звезд|звёзд)", str(label), re.I):
            continue
        match = re.search(r"\b([1-5])\s*(?:из|out of|/)\s*5\b", str(label), re.I)
        if match:
            stars = int(match.group(1))
            break
    images = [dict(image, role="hotel_photo", source="trip_com")
              for image in (snapshot.get("images") or []) if isinstance(image, dict)]
    map_index = next((index for index, line in enumerate(lines) if "посмотреть на карте" in line.casefold()), -1)
    room_type = next((line for line in lines[max(0, map_index + 1):map_index + 7]
                      if re.search(r"номер|комнат|suite|room|dorm|кроват|bed", line, re.I)), None)
    bed_match = re.search(r"\(([^)]*(?:кроват|beds?)[^)]*)\)", room_type or "", re.I)
    location_line = next((line for line in lines if "рядом:" in line.casefold()), "")
    location_prefix = re.split(r"рядом:", location_line, maxsplit=1, flags=re.I)[0]
    location_prefix = re.sub(r'["“«][^"”»]*["”»]', "", location_prefix)
    location_prefix = re.sub(r"центр города|посмотреть на карте", "", location_prefix, flags=re.I).strip(" ,–-\t")
    district_match = re.search(r"Рядом:\s*([^\"“«\n]+)", text, re.I)
    review_quotes = _tripcom_card_review_quotes(text)
    money = re.findall(r"(?<!\d)(\d[\d\u00a0\u202f ,]*(?:[.,]\d{1,2})?)\s*(₽|RUB)", text, re.I)
    offers = []
    if money:
        amount_text = money[-1][0].replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
        try:
            amount_minor = int((Decimal(amount_text) * 100).quantize(Decimal("1")))
        except (InvalidOperation, ValueError):
            amount_minor = 0
        if amount_minor > 0:
            labels = []
            if "бесплатная отмена" in text.casefold():
                labels.append("free_cancellation")
            if "без возврата" in text.casefold() or "невозврат" in text.casefold():
                labels.append("non_refundable")
            meal_plan = classify_meal_plan(text=text)
            if meal_plan == "breakfast":
                labels.append("breakfast_included")
            if "оплата в отеле" in text.casefold() or "pay at hotel" in text.casefold():
                labels.append("pay_at_hotel")
            if "вкл. налоги" in text.casefold() or "включая налоги" in text.casefold():
                labels.append("taxes_included")
            offers.append({"provider": "trip.com", "price": {"currency": "RUB", "value": amount_minor},
                           "deeplink": hotel_url, "labels": labels,
                           "room_type": room_type, "bed_type": bed_match.group(1) if bed_match else None,
                           "meal_plan": meal_plan,
                           "payment_method": "pay_at_hotel" if "pay_at_hotel" in labels else "unknown"})
    coordinate_value = None
    for link in links:
        if not isinstance(link, dict) or not re.search(r"map|карте|location", str(link.get("text") or ""), re.I):
            continue
        coordinate_match = re.search(
            r"(?:mapCoordinate|coordinate|latlng|center)=(-?\d{1,2}\.\d+)\s*[,|%2C]\s*(-?\d{1,3}\.\d+)",
            unquote(str(link.get("href") or "")), re.I,
        )
        if coordinate_match:
            coordinate_value = {"lat": float(coordinate_match.group(1)), "lng": float(coordinate_match.group(2))}
            break
    return {
        "id": hotel_id, "name": name,
        "rating": float(rating_match.group(1).replace(",", ".")) if rating_match else None,
        "reviews_count": int(reviews_digits) if reviews_digits else None,
        "review_quotes": review_quotes, "review_source": "trip_com" if review_quotes else None,
        "stars": stars, "images": images, "coordinates": coordinate_value,
        "amenities": [],
        "nearby_places": ([{"name": location_prefix, "type": "district"}]
                          if location_prefix else ([{"name": district_match.group(1).strip(), "type": "landmark"}]
                                                   if district_match else [])),
        "offers": offers,
    }

def _tripcom_card_stars(card: Any) -> int | None:
    labels = card.locator("[aria-label], [title], img[alt]").evaluate_all(
        "els => els.map(el => [el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt')].filter(Boolean).join(' '))"
    )
    for label in labels:
        if not re.search(r"(?:рейтинг|rating|star|звезд|звёзд)", label, re.I):
            continue
        match = re.search(r"\b([1-5])\s*(?:из|out of|/)\s*5\b", label, re.I)
        if match:
            return int(match.group(1))
    return None

def _tripcom_reported_total(body: str) -> int | None:
    match = re.search(r"Найдено\s+([\d\s,\u00a0\u202f]+)\s+объект(?:а|ов)?\s+размещения", body, re.I)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None

def _tripcom_explicit_empty_results(body: str) -> bool:
    return bool(re.search(
        r"(?:no\s+(?:hotels?|properties|results|availability|rooms?)\b|"
        r"(?:не найдено|нет)\s+(?:отел\w*|вариант\w*|доступн\w*|номеров\b)|"
        r"подходящ\w*\s+вариант\w*\s+не\s+найден\w*)",
        body, re.I,
    ))

TRIP_COM_SOURCE: HotelSource = TripComSource()
