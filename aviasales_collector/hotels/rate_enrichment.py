"""Trip.com room-rate enrichment use case and persistence orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .models import HotelSearchOperationError, utc_now
from .persistence import (_sanitize_enrichment_error_message,
                          persist_tripcom_room_rates)
from .read_model import _read_search
from .runtime import (_ACTIVE_SEARCH_TIMELINE_ID, _profiled_sqlite_connect, log,
                      log_search_timeline)
from .sources.tripcom import (DEFAULT_TRIP_PROFILE, TRIP_EXECUTOR,
                              TRIP_PROFILE_LOCK, shutdown_tripcom_browser)
from .sources.tripcom_rates import collect_tripcom_room_rates
from .sources.tripcom_rate_fetch import collect_tripcom_room_rates_via_fetch


def _tripcom_rate_fast_path_enabled() -> bool:
    """Fast path is the default outside tests; opt out with TRIPCOM_RATE_FAST_FETCH=0.

    Exposed as a module-level function so tests can substitute it directly:
    pytest re-creates the PYTEST_CURRENT_TEST env var on every test phase,
    which makes deleting it from a fixture ineffective.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return os.getenv("TRIPCOM_RATE_FAST_FETCH", "1") == "1"


def _persist_rate_task_lifecycle(database_path: str | Path, search_id: str,
                                 tasks: list[dict[str, Any]], query: dict[str, Any],
                                 status: str) -> None:
    """Persist one accounting row per scheduled rate task before browser work.

    Rows used to appear only at terminal, so an interrupted pool (client
    disconnect, server restart, context crash) silently lost every unfinished
    task and no endpoint could answer "expected vs completed". Writing
    queued/running rows up front keeps the accounting observable without
    changing what a terminal row means.
    """
    if not tasks:
        return
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.executemany(
            """INSERT INTO hotel_room_rate_fetch
            (search_id,source,source_hotel_id,check_in,check_out,adults,children_count,status,
             http_status,latency_ms,rates_seen,observed_at,operation,attempt,retry_count)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(search_id, "trip_com", str(task["source_hotel_id"]), query["check_in"],
              query["check_out"], query["adults"], query.get("children_count", 0), status,
              None, None, 0, utc_now(), task.get("operation", "tripcom_detail_rate_inventory"),
              int(task.get("attempt", 1)), int(task.get("retry_count", 0)))
             for task in tasks],
        )
        connection.commit()


def _tripcom_source_city_id(database_path: str | Path, search_id: str,
                            query_city_id: Any) -> str | None:
    """Read Trip.com's own city id; Sravni geo ids are a different namespace."""
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        row = connection.execute(
            "SELECT raw_json FROM hotel_raw_page WHERE search_id=? AND source='trip_com' "
            "ORDER BY page_index DESC,id DESC LIMIT 1",
            (search_id,),
        ).fetchone()
    if row:
        try:
            raw_page = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            raw_page = None
        source_city_id = raw_page.get("sourceCityId") if isinstance(raw_page, dict) else None
        if str(source_city_id or "").isdigit():
            return str(source_city_id)
    fallback = str(query_city_id or "")
    return fallback if fallback.isdigit() else None


async def enrich_tripcom_room_rates(
    database_path: str | Path, search_id: str, result: dict[str, Any],
    payload: dict[str, Any], wanted: set[str] | None,
) -> dict[str, Any]:
    enrichment_started = time.perf_counter()
    # Canonical cards may contain Trip.com as a nested source record after
    # matching. Enrichment must still address that record by its provider
    # id; restricting this list to top-level source=trip_com silently
    # skipped matched cards.
    trip_records: dict[str, dict[str, Any]] = {}
    for hotel in result["hotels"]:
        for record in hotel.get("source_records") or [hotel]:
            if record.get("source") == "trip_com" and str(record.get("source_hotel_id", "")).isdigit():
                trip_records.setdefault(str(record["source_hotel_id"]), record)
    trip_hotels = [record for hotel_id, record in trip_records.items()
                   if wanted is None or hotel_id in wanted]
    available = set(trip_records)
    if wanted is not None and not wanted.issubset(available):
        raise HotelSearchOperationError("invalid_payload", "One or more hotel_ids are not in this search")
    query = result["query"]
    trip_city_id = await asyncio.to_thread(
        _tripcom_source_city_id, database_path, search_id, query.get("geo_id"),
    )
    hotel_names = {str(hotel["source_hotel_id"]): hotel.get("name") for hotel in trip_hotels}
    all_tasks = [{"source_hotel_id": hotel["source_hotel_id"], "city_id": trip_city_id,
                  "check_in": query["check_in"], "check_out": query["check_out"],
                  "adults": query["adults"], "children_count": query.get("children_count", 0)}
                 for hotel in trip_hotels]
    if not all_tasks:
        return {"search_id": search_id, "status": "no_tripcom_hotels", "hotels": []}
    log_search_timeline(search_id, "rate_tasks_discovered", hotels=len(all_tasks),
                        requested=len(wanted) if wanted is not None else "all",
                        concurrency=payload.get("concurrency", 5))
    # Room-rate fetches persist terminal outcomes (blocked, timeout,
    # no-rates, and successful inventories). Reuse the newest terminal
    # row for this exact booking context so a rerender, coordinate refresh,
    # or a second client request cannot create another identical source
    # request or another duplicate history row.
    terminal_statuses = frozenset({
        "ok", "blocked", "no_rates", "rate_inventory_absent", "rate_inventory_timeout", "body_timeout",
        "navigation_error", "invalid_response", "http_error", "error", "not_started",
    })
    retryable_rate_statuses = frozenset({
        "navigation_error", "rate_inventory_timeout", "body_timeout", "invalid_response", "http_error", "error",
    })
    max_rate_retries = 1
    task_ids = [str(task["source_hotel_id"]) for task in all_tasks]
    placeholders = ",".join("?" for _ in task_ids)
    latest_fetch: dict[str, tuple[str, int | None, int | None, int, str | None,
                                  str | None, str | None, int, int]] = {}
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        rows = connection.execute(
            "SELECT source_hotel_id,status,http_status,latency_ms,rates_seen,operation,error_type,"
            "error_message,attempt,retry_count FROM hotel_room_rate_fetch "
            "WHERE search_id=? AND source=? AND check_in=? AND check_out=? AND adults=? "
            f"AND children_count=? AND source_hotel_id IN ({placeholders}) "
            "ORDER BY observed_at DESC,id DESC",
            [search_id, "trip_com", query["check_in"], query["check_out"], query["adults"],
             query.get("children_count", 0), *task_ids],
        ).fetchall()
    for row in rows:
        hotel_id = str(row[0])
        if hotel_id not in latest_fetch:
            latest_fetch[hotel_id] = (str(row[1]), row[2], row[3], int(row[4] or 0),
                                      row[5], row[6], row[7], int(row[8] or 0), int(row[9] or 0))
    tasks = []
    for task in all_tasks:
        latest = latest_fetch.get(str(task["source_hotel_id"]))
        if latest is None or latest[0] not in terminal_statuses:
            tasks.append(task)
        elif latest[0] in retryable_rate_statuses and latest[8] < max_rate_retries:
            # Failed detail navigations get at most one explicit retry.
            # Protection responses and not-started hotels remain terminal.
            tasks.append({**task, "attempt": latest[7] + 1, "retry_count": latest[8] + 1})
    scheduled_ids = {str(task["source_hotel_id"]) for task in tasks}
    reused_outcomes = {
        hotel_id: {"status": details[0], "rate_status": details[1], "latency_ms": details[2],
                   "rates_seen": details[3], "operation": details[4], "error_type": details[5],
                   "error_message": details[6], "attempt": details[7],
                   "retry_count": details[8], "reused": True}
        for hotel_id, details in latest_fetch.items()
        if details[0] in terminal_statuses
        and hotel_id not in scheduled_ids
    }
    if not tasks:
        # Preserve the existing busy-browser contract for an overlapping
        # request, even when this request would otherwise be served from
        # the terminal cache without opening Chromium.
        if not TRIP_PROFILE_LOCK.acquire(blocking=False):
            raise HotelSearchOperationError("browser_busy", "Trip.com browser is busy; retry this enrichment later")
        TRIP_PROFILE_LOCK.release()
        refreshed = _read_search(database_path, search_id)
        refreshed_by_id: dict[str, dict[str, Any]] = {}
        for hotel in refreshed["hotels"]:
            for record in hotel.get("source_records") or [hotel]:
                if record.get("source") == "trip_com":
                    refreshed_by_id[str(record.get("source_hotel_id"))] = record
        return {
            "search_id": search_id,
            "status": "blocked" if any(item["status"] == "blocked" for item in reused_outcomes.values()) else "ok",
            "concurrency": payload.get("concurrency", 5),
            "hotels": [{"source_hotel_id": hotel_id,
                        "room_options": refreshed_by_id.get(hotel_id, {}).get("room_options", []),
                        "room_rates": refreshed_by_id.get(hotel_id, {}).get("room_rates", []),
                        "property_details": refreshed_by_id.get(hotel_id, {}).get("property_details"),
                        "enrichment": outcome}
                       for hotel_id, outcome in reused_outcomes.items()],
        }
    log_search_timeline(search_id, "rate_queue_prepared", scheduled=len(tasks),
                        reused=len(reused_outcomes), concurrency=payload.get("concurrency", 5))
    profile_setting = os.getenv("TRIP_COM_BROWSER_DATA_DIR")
    profile = Path(profile_setting).expanduser().resolve() if profile_setting else DEFAULT_TRIP_PROFILE
    if not TRIP_PROFILE_LOCK.acquire(blocking=False):
        raise HotelSearchOperationError("browser_busy", "Trip.com browser is busy; retry this enrichment later")
    persistence_lock = asyncio.Lock()
    timeline_token = _ACTIVE_SEARCH_TIMELINE_ID.set(search_id)
    # Outcomes that already reached the database through on_result. If the
    # fast path crashes mid-run, these partial results are restored into the
    # response instead of being lost, and the detail-page fallback skips
    # them so the same hotel cannot be collected and persisted twice.
    persisted_outcomes: dict[str, dict[str, Any]] = {}

    async def persist_completed_rate_result(index: int, outcome: dict[str, Any]) -> None:
        persist_started = time.perf_counter()
        task = tasks[index]
        persisted_outcomes[str(task["source_hotel_id"])] = outcome
        outcome["attempt"] = int(task.get("attempt") or outcome.get("attempt") or 1)
        outcome["retry_count"] = int(task.get("retry_count") or outcome.get("retry_count") or 0)
        outcome["error_message"] = _sanitize_enrichment_error_message(outcome.get("error_message"))
        outcome.setdefault("operation", "tripcom_detail_rate_inventory")
        outcome.setdefault("attempt", 1)
        outcome.setdefault("retry_count", 0)
        inventory = outcome.get("inventory")
        if not isinstance(inventory, dict):
            inventory = {
                "source_hotel_id": task["source_hotel_id"],
                "check_in": task["check_in"], "check_out": task["check_out"],
                "adults": task["adults"], "children_count": task.get("children_count", 0),
                "rates": [],
            }
        property_details = outcome.get("property_details")
        # Keep SQLite writes serialized while allowing the Playwright pages
        # to continue loading independently. The search API can expose each
        # terminal hotel immediately instead of waiting for the whole pool.
        async with persistence_lock:
            outcome["persistence"] = await asyncio.to_thread(
                persist_tripcom_room_rates,
                database_path, search_id, inventory,
                status=outcome.get("status"),
                http_status=outcome.get("rate_status"),
                latency_ms=outcome.get("latency_ms"),
                property_details=property_details,
                operation=outcome.get("operation", "tripcom_detail_rate_inventory"),
                error_type=outcome.get("error_type"),
                error_message=outcome.get("error_message"),
                attempt=outcome.get("attempt", 1),
                retry_count=outcome.get("retry_count", 0),
            )
        log_search_timeline(search_id, "rate_task_terminal", hotel_id=task["source_hotel_id"],
                            name=hotel_names.get(str(task["source_hotel_id"])), status=outcome.get("status"),
                            error_type=outcome.get("error_type"),
                            persist_ms=round((time.perf_counter() - persist_started) * 1000, 1),
                            task_elapsed_ms=outcome.get("latency_ms"),
                            attempt=outcome.get("attempt", 1), retry_count=outcome.get("retry_count", 0))
        log.info("[ENRICH][hotel=%s] operation=%s status=%s error_type=%s attempt=%s retry_count=%s",
                 task["source_hotel_id"], outcome.get("operation", "tripcom_detail_rate_inventory"),
                 outcome.get("status"), outcome.get("error_type"), outcome.get("attempt", 1),
                 outcome.get("retry_count", 0))
    try:
        # The synchronous discovery browser and Playwright's async room-rate
        # browser cannot hold the same persistent user-data directory at
        # once. Close the reusable context on its owning executor thread;
        # cookies and login state remain in the persistent profile and the
        # next discovery recreates Chromium from that same profile.
        shutdown_started = time.perf_counter()
        await asyncio.get_running_loop().run_in_executor(TRIP_EXECUTOR, shutdown_tripcom_browser)
        log_search_timeline(search_id, "rate_browser_profile_released",
                            elapsed_ms=round((time.perf_counter() - shutdown_started) * 1000, 1))
        log.info("[TRIP_BROWSER_START] stage=room_rate_enrichment profile_released=true")
        # Accounting rows appear before browser work so an interrupted pool is
        # observable: expected/created are countable even if no task reaches
        # terminal. The "queued" status is non-terminal and excluded from reuse.
        # Written only after the profile lock is held: a request that loses
        # the browser-busy race must not leave queued rows newer than the
        # winner's terminal rows.
        await asyncio.to_thread(
            _persist_rate_task_lifecycle, database_path, search_id, tasks, query, "queued",
        )
        pool_started = time.perf_counter()
        log_search_timeline(search_id, "rate_pool_started", workers=min(int(payload.get("concurrency", 5)), len(tasks)),
                            scheduled=len(tasks))
        # Fast path first: the site's own rate endpoint fetched in-page, one
        # domain landing + N fetches, no per-hotel detail navigation. The
        # detail-page pool runs for hotels the fast path never reached
        # (template capture failure, fast-path exception, not_started stubs);
        # an explicit protection response (spider flag / HTTP 401/403/429/430)
        # stops the source for this request instead of repeating it through
        # detail pages. Hotels already collected keep their results.
        # pytest sets PYTEST_CURRENT_TEST; a real browser + real network calls
        # must never run from unit tests (see _tripcom_rate_fast_path_enabled).
        fast_path_enabled = _tripcom_rate_fast_path_enabled()
        enriched: list[dict[str, Any]] = []
        protected = False
        if fast_path_enabled:
            try:
                enriched = await collect_tripcom_room_rates_via_fetch(
                    tasks, profile, concurrency=payload.get("concurrency", 5),
                    on_result=persist_completed_rate_result,
                )
                # The fast path answers one item per task and pads hotels it
                # never reached with not_started stubs, so the fallback
                # decision is made on statuses, not on the result length. A
                # blocked item means the source answered with protection:
                # retrying the same source through detail pages would hit the
                # same protection and only burn the profile's quota.
                protected = any(item.get("status") == "blocked"
                                for item in enriched if isinstance(item, dict))
            except Exception as exc:
                log.warning("[RATE_FETCH] in_page_fetch_failed error=%s detail=%s; falling back to detail pages",
                            type(exc).__name__, str(exc)[:300])
                # Restore outcomes the crashed run already persisted instead
                # of dropping them; the fallback below then skips these
                # hotels so they are not collected (and persisted) twice.
                enriched = [persisted_outcomes.get(str(task["source_hotel_id"]))
                            or {"status": "not_started"} for task in tasks]
        if not fast_path_enabled:
            remaining = list(tasks)
        elif protected:
            remaining = []
            log.info("[RATE_FETCH] fallback_to_detail_pages skipped reason=protection")
        else:
            remaining = [task for index, task in enumerate(tasks)
                         if index >= len(enriched)
                         or not isinstance(enriched[index], dict)
                         or str(enriched[index].get("status") or "") == "not_started"]
        if remaining:
            # Attempts are indexed by the full task order inside persistence;
            # translate the subset-relative on_result index of the legacy pool
            # back to the task index so a partial fallback cannot persist an
            # outcome under the wrong hotel.
            absolute_position = {str(task.get("source_hotel_id") or ""): index
                                 for index, task in enumerate(tasks)}

            async def persist_remaining_result(index: int, outcome: dict[str, Any]) -> None:
                hotel_id = str(remaining[index].get("source_hotel_id") or "") if index < len(remaining) else ""
                await persist_completed_rate_result(absolute_position.get(hotel_id, index), outcome)

            log.info("[RATE_FETCH] fallback_to_detail_pages remaining=%d", len(remaining))
            detail_results = await collect_tripcom_room_rates(
                remaining, profile, concurrency=payload.get("concurrency", 5),
                on_result=persist_remaining_result,
            )
            # Positional merge: the legacy pool contract returns one item per
            # input hotel and its outcomes may omit source_hotel_id, so map
            # each remaining task back to its slot in the task order.
            merged: list[dict[str, Any] | None] = list(enriched)
            if len(merged) < len(tasks):
                merged.extend([None] * (len(tasks) - len(merged)))
            for task, item in zip(remaining, detail_results, strict=False):
                slot = absolute_position.get(str(task.get("source_hotel_id") or ""))
                if slot is not None:
                    merged[slot] = item
            enriched = [item if item is not None else {"status": "not_started"}
                        for item in merged]
        status_counts: dict[str, int] = {}
        for item in enriched:
            key = str(item.get("status") or "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        def percentile(field: str, fraction: float) -> float | None:
            values = sorted(float(item[field]) for item in enriched if item.get(field) is not None)
            if not values:
                return None
            position = (len(values) - 1) * fraction
            low = int(position)
            high = min(low + 1, len(values) - 1)
            return round(values[low] + (values[high] - values[low]) * (position - low), 1)

        rate_percentiles = {
            field: {f"p{int(fraction * 100)}": percentile(field, fraction)
                    for fraction in (0.5, 0.9, 0.95, 0.99)}
            for field in ("queue_wait_ms", "latency_ms", "navigation_latency_ms",
                          "execution_ms", "inventory_wait_ms", "room_details_ms", "normalization_ms")
        }
        for field in ("queue_wait_ms", "latency_ms", "navigation_latency_ms",
                      "execution_ms", "inventory_wait_ms", "room_details_ms", "normalization_ms"):
            vals = [float(item[field]) for item in enriched if item.get(field) is not None]
            rate_percentiles[field]["max"] = round(max(vals), 1) if vals else None
        log_search_timeline(search_id, "rate_pool_terminal", elapsed_ms=round((time.perf_counter() - pool_started) * 1000, 1),
                            terminal=len(enriched), statuses=status_counts,
                            distributions=rate_percentiles)
    finally:
        _ACTIVE_SEARCH_TIMELINE_ID.reset(timeline_token)
        TRIP_PROFILE_LOCK.release()
    for outcome in enriched:
        outcome.pop("inventory", None)
        outcome.pop("property_details", None)
    refreshed = _read_search(database_path, search_id)
    refreshed_by_id: dict[str, dict[str, Any]] = {}
    for hotel in refreshed["hotels"]:
        for record in hotel.get("source_records") or [hotel]:
            if record.get("source") == "trip_com":
                refreshed_by_id[str(record.get("source_hotel_id"))] = record
    outcomes_by_id = {str(task["source_hotel_id"]): item for task, item in zip(tasks, enriched, strict=True)}
    outcomes_by_id.update(reused_outcomes)
    return {
        "search_id": search_id,
        "status": "blocked" if any(item.get("status") == "blocked" for item in outcomes_by_id.values())
            else "ok" if all(item.get("status") in {"ok", "no_rates", "rate_inventory_absent"}
                             for item in outcomes_by_id.values())
            else "partial",
        "concurrency": payload.get("concurrency", 5),
        "hotels": [{"source_hotel_id": hotel_id,
                    "room_options": refreshed_by_id.get(hotel_id, {}).get("room_options", []),
                    "room_rates": refreshed_by_id.get(hotel_id, {}).get("room_rates", []),
                    "property_details": refreshed_by_id.get(hotel_id, {}).get("property_details"),
                    "enrichment": outcome}
                   for hotel_id, outcome in outcomes_by_id.items()],
    }
