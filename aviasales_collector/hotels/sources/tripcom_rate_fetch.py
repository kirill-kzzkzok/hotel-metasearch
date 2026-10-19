"""Fetch Trip.com room-rate inventories via the site's own rate endpoint,
issued in-page from a single domain landing page - no per-hotel detail
navigation. A template capture failure returns the empty sentinel so the
caller can fall back to the detail-page pool; an explicit protection
response (spider flag / HTTP 401/403/429/430) instead stops the run and is
reported per hotel instead of repeating it through detail pages.

Verified live 2026-10-14: 10/10 real hotels returned full inventories
(2-242 room groups each) at ~1-4s per hotel versus 15-20s per detail page,
zero protection responses. The endpoint, POST body shape, and payload
contract are the ones the detail page itself uses; nothing is invented.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .tripcom_rates import (
    TRIPCOM_RATE_ENDPOINT_PATH,
    _classify_tripcom_rate_response,
    _utc_timestamp,
)


TRIPCOM_RATE_FETCH_TEMPLATE_TIMEOUT_SECONDS = 20.0
TRIPCOM_RATE_FETCH_PER_HOTEL_TIMEOUT_SECONDS = 15.0
# 2026-10-18: 0.7 -> 0.35. With 4 parallel tabs the global request-schedule
# lock serializes fetch+pause, so the effective request rate stays as gentle
# as the old single-tab cadence while latency overlaps across tabs; measured
# pool span was 454s for 100 ok outcomes (4.5s/hotel) - the pause dominated.
TRIPCOM_RATE_FETCH_INTER_REQUEST_DELAY_SECONDS = 0.35
TRIPCOM_RATE_FETCH_MAX_PAYLOAD_BYTES = 32 * 1024 * 1024

_RATE_FETCH_URL = "https://ru.trip.com" + TRIPCOM_RATE_ENDPOINT_PATH

_COMMENT_JS = """async (hotelId) => {
  const body = {hotelId: Number(hotelId), sceneTypes: ['CommentList'],
    commentFilterOptions: {pageIndex: 1, pageSize: 10, repeatComment: 1, orderBy: '0'},
    head: {platform: 'PC', cver: '0', bu: 'IBU', group: 'trip', locale: 'ru-RU',
           currency: 'RUB', pageId: '10320668148', extension: []}};
  try {
    const r = await fetch('https://ru.trip.com/restapi/soa2/34308/getHotelCommentInfo', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify(body), credentials: 'include'});
    const t = await r.text();
    return {status: r.status, payloadJson: t.slice(0, 400000)};
  } catch (e) { return {status: 0, payloadJson: ''}; }
}"""

_FETCH_JS = """async (task) => {
  const body = JSON.parse(JSON.stringify(window.__rateTemplate));
  body.search.hotelId = Number(task.hotelId);
  // 2026-10-15 audit: the template is captured once per batch (with
  // hotels[0] and children=0); every task must overwrite the occupancy and
  // date fields too, otherwise rates are requested for the wrong guest
  // composition while persistence records the task's composition.
  body.search.checkIn = task.checkIn;
  body.search.checkOut = task.checkOut;
  body.search.adult = task.adult;
  if (task.childInfoItems !== undefined) {
    body.search.childInfoItems = JSON.parse(JSON.stringify(task.childInfoItems));
  }
  const resp = await fetch(__RATE_FETCH_URL__, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
    credentials: 'include',
  });
  const status = resp.status;
  let payload = null;
  try { payload = await resp.json(); } catch (e) {}
  if (payload && typeof payload === 'object') {
    const data = payload.data;
    return {status, spider: (data && data.htlSpiderActionErrorCode) || payload.htlSpiderActionErrorCode || null,
            payloadJson: JSON.stringify(payload)};
  }
  return {status, spider: null, payloadJson: null};
}"""

_FETCH_JS = _FETCH_JS.replace("__RATE_FETCH_URL__", json.dumps(_RATE_FETCH_URL))


async def _capture_rate_template(page: Any, hotel: dict[str, Any]) -> dict[str, Any] | None:
    """Capture the site's own rate request body from one detail navigation.

    The captured body is a faithful template (session-bound head fields,
    extras); per-hotel fields are overwritten per request. Returns None when
    the page never issues the request (protection or absence).
    """
    captured: list[str] = []
    page.on("request", lambda req: captured.append(req.post_data or "")
            if TRIPCOM_RATE_ENDPOINT_PATH in req.url else None)
    hotel_id = str(hotel.get("source_hotel_id") or "")
    # B05: cityId must be a Trip-resolved namespace id. The hardcoded 1356
    # used to leak into every capture without a real mapping.
    city_id = str(hotel.get("city_id") or "")
    if not hotel_id or not city_id.isdigit():
        return None
    # Capture with the task's own occupancy so the template's childInfoItems
    # block matches the guest composition; per-task fields are overwritten on
    # every fetch anyway, this keeps the captured head faithful.
    children_count = int(hotel.get("children_count", 0) or 0)
    url = ("https://ru.trip.com/hotels/detail/?" + "&".join([
        f"hotelId={hotel_id}", f"cityId={city_id}",
        f"checkIn={hotel.get('check_in')}", f"checkOut={hotel.get('check_out')}",
        f"adult={hotel.get('adults', 2)}", f"children={children_count}",
        "locale=ru-RU", "crn=1",
    ]))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        deadline = time.monotonic() + 20
        while not captured and time.monotonic() < deadline:
            await page.wait_for_timeout(300)
    except Exception:
        return None
    if not captured:
        return None
    try:
        return json.loads(captured[0])
    except (TypeError, ValueError):
        return None


async def collect_tripcom_room_rates_via_fetch(
        hotels: list[dict[str, Any]], profile: str | Path,
        concurrency: int = 5,
        on_result: Any | None = None) -> list[dict[str, Any]]:
    """Collect rate inventories through the site's own rate endpoint in-page.

    One persistent context, one domain landing page, sequential in-page POST
    fetches with a small inter-request delay. This replaces per-hotel detail
    navigation (15-20s each) with a ~1-4s fetch while using the identical
    public endpoint and payload the detail page uses. A template capture
    failure returns the empty sentinel (the caller keeps unreached hotels
    queued - per-hotel detail pages are forbidden, 2026-10-15 directive); a
    protection signal (spider flag or HTTP
    401/403/429/430) instead stops the run and the blocked hotel is reported
    immediately. The result always carries one item per input hotel (hotels
    never attempted are not_started); already-collected hotels keep their
    results.
    """
    if not hotels:
        return []
    from playwright.async_api import async_playwright

    results: list[dict[str, Any] | None] = [None] * len(hotels)
    search_ids = {str(h.get("search_id") or "") for h in hotels if h.get("search_id")}
    search_id = next(iter(search_ids)) if len(search_ids) == 1 else ""

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(Path(profile).expanduser().resolve()), headless=False, locale="ru-RU",
            viewport={"width": 1440, "height": 1000},
            service_workers="block",
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://ru.trip.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            template = await _capture_rate_template(page, hotels[0])
            if template is None:
                return []  # sentinel: unreached hotels stay queued (no detail fallback)
            # The detail page keeps navigating client-side after goto; its
            # execution context can be destroyed mid-evaluate ("Execution
            # context was destroyed, most likely because of a navigation" -
            # the observed server-runtime failure). Land on the stable home
            # page before installing the template and issuing fetches.
            await page.goto("https://ru.trip.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            await page.evaluate("tpl => { window.__rateTemplate = tpl; }", template)

            # 2026-10-15: parallel tabs. The sequential loop (one fetch, then
            # a fixed pause) needed ~3.2s per hotel - 150 hotels = 8 minutes.
            # Workers share ONE context (one browser, one profile); a global
            # request-schedule lock keeps the inter-request pause so the
            # request rate stays as gentle as before, latency is simply
            # overlapped with other tabs' waiting.
            _REQUEST_SCHEDULE_LOCK = asyncio.Lock()
            effective_concurrency = max(1, min(int(concurrency or 1), 4, len(hotels)))
            worker_pages = [page]
            for _ in range(effective_concurrency - 1):
                try:
                    worker_pages.append(await context.new_page())
                    await worker_pages[-1].goto("https://ru.trip.com/", wait_until="domcontentloaded", timeout=45000)
                    await worker_pages[-1].evaluate("tpl => { window.__rateTemplate = tpl; }", template)
                except Exception:
                    break

            async def _fetch_one(worker_page, index, hotel):
                hotel_id = str(hotel.get("source_hotel_id") or "")
                if _protection_signalled.is_set():
                    return index, {"source_hotel_id": hotel_id, "status": "not_started",
                                   "latency_ms": 0}
                started = time.perf_counter()
                try:
                    fetch_started = time.perf_counter()
                    # Dates/occupancy are per-task fields on the fetch body
                    # (the captured template belongs to hotels[0]); the
                    # template is now captured with the task's children=N so
                    # its childInfoItems block is the site's own rendering of
                    # this guest composition. Ages are only overwritten when
                    # the app actually knows them - inventing ages would
                    # fabricate the guest composition.
                    fetch_task: dict[str, Any] = {
                        "hotelId": hotel_id,
                        "checkIn": str(hotel.get("check_in") or "").replace("-", ""),
                        "checkOut": str(hotel.get("check_out") or "").replace("-", ""),
                        "adult": int(hotel.get("adults", 2) or 2),
                    }
                    children_ages = [age for age in (hotel.get("children_ages") or [])
                                     if isinstance(age, int) and 0 <= age <= 17]
                    if children_ages:
                        fetch_task["childInfoItems"] = [{"age": age} for age in children_ages]
                    async with _REQUEST_SCHEDULE_LOCK:
                        outcome = await asyncio.wait_for(
                            worker_page.evaluate(_FETCH_JS, fetch_task),
                            timeout=TRIPCOM_RATE_FETCH_PER_HOTEL_TIMEOUT_SECONDS,
                        )
                        await worker_page.wait_for_timeout(
                            int(TRIPCOM_RATE_FETCH_INTER_REQUEST_DELAY_SECONDS * 1000))
                except asyncio.TimeoutError:
                    return index, {
                        "source_hotel_id": hotel_id,
                        "status": "rate_inventory_timeout",
                        "operation": "tripcom_detail_rate_inventory",
                        "error_type": "rate_response_timeout",
                        "error_message": "In-page rate fetch exceeded its bounded deadline",
                        "attempt": 1, "retry_count": 0,
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                    }
                return index, outcome

            _protection_signalled = asyncio.Event()
            tasks_pool = [asyncio.create_task(_fetch_one(worker_pages[task_index % len(worker_pages)], index, hotel))
                          for task_index, (index, hotel) in enumerate(enumerate(hotels))]
            outcomes_by_index: dict[int, dict[str, Any]] = {}
            for coro in asyncio.as_completed(tasks_pool):
                try:
                    index, outcome = await coro
                except asyncio.CancelledError:
                    # A protection signal cancelled the remaining fetches;
                    # cancelled tasks keep their not_started stubs - do not
                    # let the cancellation kill the whole collector.
                    continue
                outcomes_by_index[index] = outcome
                hotel = hotels[index]
                hotel_id = str(hotel.get("source_hotel_id") or "")
                started = time.perf_counter()
                if _protection_signalled.is_set():
                    for pending in tasks_pool:
                        pending.cancel()
                # Classify + persist in completion order (results keep the
                # per-hotel slot so the caller response stays positional).
                # A timeout stub is already terminal; everything else is the
                # raw page.evaluate outcome ({status, payloadJson, spider})
                # and must be classified below.
                if outcome.get("status") == "rate_inventory_timeout":
                    item = outcome
                else:
                    status = outcome.get("status")
                    spider = outcome.get("spider")
                    payload_json = outcome.get("payloadJson")
                    payload = None
                    if payload_json and len(payload_json) <= TRIPCOM_RATE_FETCH_MAX_PAYLOAD_BYTES:
                        try:
                            payload = json.loads(payload_json)
                        except (TypeError, ValueError):
                            payload = None
                    if status in {401, 403, 429, 430} or spider is not None:
                        # Protection signal: the blocked outcome is reported
                        # (and persisted) for this hotel; remaining hotels
                        # keep their not_started stubs and the caller must not
                        # retry the same source through detail pages.
                        import logging
                        logging.getLogger(__name__).warning(
                            "[RATE_FETCH] protection signal hotel=%s status=%s spider=%s "
                            "payload_head=%s", hotel_id, status, spider,
                            (payload_json or "")[:300])
                        item = {
                            "source_hotel_id": hotel_id,
                            "status": "blocked",
                            "rate_status": status,
                            "operation": "tripcom_detail_rate_inventory",
                            "error_type": "robots_or_provider_protection",
                            "error_message": "Protection response on the rate endpoint",
                            "attempt": 1, "retry_count": 0,
                            "latency_ms": round((time.perf_counter() - started) * 1000),
                        }
                        _protection_signalled.set()
                        for pending in tasks_pool:
                            pending.cancel()
                    else:
                        diagnostics = {
                            "hotel_id": hotel_id,
                            "rate_fetch_mode": "in_page_fetch",
                            "rate_response_status": status,
                        }
                        status_out, inventory, error_type, error_message = _classify_tripcom_rate_response(
                            status, payload, body_wait_error=None,
                            response_body_timeout=None,
                            source_hotel_id=hotel_id,
                            check_in=str(hotel.get("check_in")), check_out=str(hotel.get("check_out")),
                            adults=int(hotel.get("adults", 2)),
                            children_count=int(hotel.get("children_count", 0) or 0),
                        )
                        item = {
                            "source_hotel_id": hotel_id,
                            "status": status_out,
                            "operation": "tripcom_detail_rate_inventory",
                            "attempt": 1, "retry_count": 0,
                            "rate_status": status,
                            "latency_ms": round((time.perf_counter() - started) * 1000),
                        }
                        if inventory is not None:
                            item["inventory"] = inventory
                            diagnostics["rate_count"] = len(inventory.get("rates", []))
                        if error_type:
                            item["error_type"] = error_type
                        if error_message:
                            item["error_message"] = error_message
                        item["diagnostics"] = diagnostics
                results[index] = item
                # 2026-10-17: review enrichment via the site's own comment
                # endpoint (in-page XHR, same as the rate fetch - no detail
                # pages). One request per hotel, only for successful rates.
                if item.get("status") == "ok":
                    comment_page = worker_pages[index % len(worker_pages)]
                    try:
                        comment_out = await asyncio.wait_for(
                            comment_page.evaluate(_COMMENT_JS, hotel_id), timeout=20)
                        comment_payload = None
                        if comment_out and comment_out.get("status") == 200 and comment_out.get("payloadJson"):
                            comment_payload = json.loads(comment_out["payloadJson"])
                        comment_data = (comment_payload or {}).get("data")                             if isinstance(comment_payload, dict) else None
                        if isinstance(comment_data, dict):
                            rating = comment_data.get("commentRating") or {}
                            subscores = []
                            for key, label in (("ratingLocation", "Местоположение"),
                                               ("ratingFacility", "Удобства"),
                                               ("ratingService", "Сервис"),
                                               ("ratingRoom", "Чистота")):
                                value = rating.get(key)
                                if isinstance(value, (int, float)) and 0 < value <= 10:
                                    subscores.append({"name": label, "score": float(value)})
                            review_tags = [
                                {"name": str(tag.get("name") or ""), "count": int(tag.get("commentCount") or 0)}
                                for tag in (comment_data.get("commentTagList") or [])
                                if isinstance(tag, dict) and tag.get("name")]
                            item["review_info"] = {
                                "rating_subscores": subscores or None,
                                "review_level": rating.get("commentLevel"),
                                "reviews_total": comment_data.get("totalCount"),
                                "review_tags": review_tags[:8] or None,
                            }
                    except Exception:
                        pass  # Review enrichment is best-effort; rates stay intact.
                if on_result is not None:
                    if item.get("status") not in {"ok", "no_rates", "rate_inventory_absent"}:
                        item.setdefault("error_type", item.get("reason") or item.get("status") or "error")
                        item.setdefault("error_message", item.get("reason") or item.get("status") or "Enrichment failed")
                    await on_result(index, item)
        finally:
            try:
                await context.close()
            except Exception:
                pass
    return [item if item is not None else {"status": "not_started"} for item in results]
