from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .collector import collect_deep
from .database import (
    Destination,
    MonitorSettings,
    SQLiteStore,
    ScanRecord,
    destination_key,
)
from .monitoring_logs import log_monitoring


log = logging.getLogger(__name__)


class DeepScanConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeepScanOutcome:
    settings: MonitorSettings
    active_destinations: tuple[Destination, ...]
    collector_result: dict[str, Any]
    stored_scan: ScanRecord
    skipped_destinations: tuple[Destination, ...] = ()

    @property
    def price_drops(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            change
            for change in self.stored_scan.price_changes
            if isinstance(change.get("change_percent"), (int, float))
            and change["change_percent"] < 0
        )


def _target_result(result: dict[str, Any], active: tuple[Destination, ...]) -> dict[str, Any]:
    active_keys = {destination_key(item.destination) for item in active}
    offers = [
        offer
        for offer in result.get("offers", [])
        if isinstance(offer.get("destination"), str)
        and destination_key(offer["destination"]) in active_keys
    ]
    targeted = dict(result)
    targeted["offers"] = offers
    targeted["cards_found"] = len(offers)
    targeted["dom_hot_ticket_count"] = len(offers)
    targeted["unique_destinations"] = len(
        {destination_key(offer["destination"]) for offer in offers if offer.get("destination")}
    )
    targeted["deep_scan"] = True
    # A structurally complete collector response can still contain only
    # destinations outside the active set. After filtering, that must not
    # update the catalog or price baselines.
    if not offers:
        targeted["complete"] = False
        warnings = targeted.get("warnings")
        if not isinstance(warnings, list):
            warnings = [] if warnings is None else [str(warnings)]
        warnings.append("deep scan returned no results for the selected active destinations")
        targeted["warnings"] = warnings
    return targeted


async def deep_scan_and_persist(
    store: SQLiteStore,
    user_id: int,
    chat_id: int,
    *,
    collector_fn: Callable[..., dict[str, Any]] | None = None,
    max_destinations: int | None = None,
    retries: int = 0,
    retry_delay_seconds: float = 3.0,
    origin: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    price_drop_threshold_percent: float = 12.0,
) -> DeepScanOutcome:
    """Run a DOM collector pass and retain only currently active destinations.

    This uses the proven public Hot Tickets collector and clicks the visible
    "More tickets" control. It never calls Aviasales endpoints directly.
    """
    settings = store.get_settings(user_id, chat_id, origin)
    if settings is None:
        raise DeepScanConfigurationError("monitor settings are not configured")
    if not settings.enabled:
        raise DeepScanConfigurationError("monitor settings are disabled")

    active = tuple(store.list_active_destinations(user_id, chat_id, settings.origin))
    if not active:
        raise DeepScanConfigurationError("no active destinations to deep scan")
    if max_destinations is not None and max_destinations < 1:
        raise DeepScanConfigurationError("deep scan destination limit must be positive")
    if retries < 0:
        raise DeepScanConfigurationError("deep scan retries cannot be negative")
    if retry_delay_seconds < 0:
        raise DeepScanConfigurationError("deep scan retry delay cannot be negative")

    cursor = store.get_deep_scan_cursor(user_id, chat_id, settings.origin)
    start_index = next(
        ((index + 1) % len(active) for index, item in enumerate(active)
         if destination_key(item.destination) == cursor),
        0,
    )
    rotated = active[start_index:] + active[:start_index]
    # Do not wrap a capped batch around the end of the order: that would scan
    # already-seen destinations twice before a full round reaches its last
    # items (e.g. 10 routes in batches of 3 would repeat 2 routes).
    batch_limit = max_destinations if max_destinations is not None else len(active)
    selected = rotated[:min(batch_limit, len(active) - start_index)]
    skipped_keys = {destination_key(item.destination) for item in active} - {
        destination_key(item.destination) for item in selected
    }
    skipped = tuple(item for item in active if destination_key(item.destination) in skipped_keys)
    if skipped:
        log_monitoring(
            log,
            logging.INFO,
            "🔎 %s · глубокая проверка: в этой пачке %s направлений, отложено %s",
            settings.origin,
            len(selected),
            len(skipped),
        )

    result: dict[str, Any] = {}
    destination_status: dict[str, str] = {}

    def record_progress(update: dict[str, Any]) -> None:
        if isinstance(update, dict) and isinstance(update.get("destination"), str):
            destination_status[destination_key(update["destination"])] = str(update.get("status") or "")
        if progress_callback is not None:
            progress_callback(update)

    for attempt in range(retries + 1):
        if collector_fn is None:
            result = await asyncio.to_thread(
                collect_deep,
                origin=settings.origin,
                trip_type=settings.trip_type,
                destinations=[item.destination for item in selected],
                duration_min=settings.duration_min_days or 1,
                duration_max=settings.duration_max_days or 30,
                progress_callback=record_progress,
            )
        else:
            kwargs = {
                "origin": settings.origin,
                "trip_type": settings.trip_type,
                "duration_min": settings.duration_min_days or 1,
                "duration_max": settings.duration_max_days or 30,
            }
            try:
                parameters = inspect.signature(collector_fn).parameters
                accepts_kwargs = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
                )
                if "destinations" in parameters or accepts_kwargs:
                    kwargs["destinations"] = [item.destination for item in selected]
                if "progress_callback" in parameters or accepts_kwargs:
                    kwargs["progress_callback"] = record_progress
            except (TypeError, ValueError):
                pass
            result = await asyncio.to_thread(collector_fn, **kwargs)
        if result.get("complete") or attempt >= retries:
            break
        log_monitoring(
            log,
            logging.WARNING,
            "⚠️ %s · глубокая проверка не завершена · повтор через %.1f с",
            settings.origin,
            retry_delay_seconds,
        )
        log.debug(
            "[DEBUG][DEEP-SCAN] Incomplete attempt %s/%s origin=%s",
            attempt + 1,
            retries + 1,
            settings.origin,
        )
        if retry_delay_seconds:
            await asyncio.sleep(retry_delay_seconds)

    targeted = _target_result(result, selected)
    targeted["destinations_requested"] = len(active)
    scanned_raw = result.get("destinations_scanned")
    if scanned_raw is None:
        scanned = len(
            {
                destination_key(offer["destination"])
                for offer in targeted.get("offers", [])
                if isinstance(offer.get("destination"), str) and offer.get("destination")
            }
        )
    else:
        try:
            scanned = int(scanned_raw)
        except (TypeError, ValueError):
            scanned = 0
    targeted["destinations_scanned"] = max(0, min(len(selected), scanned))
    successful_keys = {
        key for key, status in destination_status.items() if status == "completed"
    }
    if not destination_status:
        successful_keys = {
            destination_key(offer["destination"])
            for offer in targeted.get("offers", [])
            if isinstance(offer.get("destination"), str)
        }
    batch_complete = bool(result.get("complete")) and (
        targeted["destinations_scanned"] >= len(selected)
    )
    targeted["batch_complete"] = batch_complete
    targeted["partial"] = targeted["destinations_scanned"] < len(active)
    warnings = targeted.get("warnings")
    if not isinstance(warnings, list):
        warnings = [] if warnings is None else [str(warnings)]
    warnings = [
        warning for warning in warnings
        if not str(warning).casefold().startswith("deep scan completed partially:")
    ]
    if targeted["partial"]:
        warnings.append(
            f"deep scan completed partially: {targeted['destinations_scanned']}/{len(active)} destinations"
        )
    targeted["warnings"] = warnings
    if skipped:
        warnings.append(
            f"deep scan limited to {len(selected)} active destinations; {len(skipped)} skipped"
        )
        targeted["warnings"] = warnings
    targeted["duration_min_days"] = settings.duration_min_days
    targeted["duration_max_days"] = settings.duration_max_days
    stored_scan = store.record_scan(
        user_id,
        chat_id,
        targeted,
        track_price_changes=True,
        scan_kind="deep",
        price_drop_threshold_percent=price_drop_threshold_percent,
    )
    failed_selected = [
        item for item in selected if destination_key(item.destination) not in successful_keys
    ]
    if failed_selected:
        failed_key = destination_key(failed_selected[0].destination)
        failed_index = next(i for i, item in enumerate(active) if destination_key(item.destination) == failed_key)
        cursor_destination = active[failed_index - 1].destination
    elif selected:
        cursor_destination = selected[-1].destination
    else:
        cursor_destination = cursor
    store.set_deep_scan_cursor(user_id, chat_id, settings.origin, cursor_destination)
    return DeepScanOutcome(
        settings=settings,
        active_destinations=selected,
        collector_result=targeted,
        stored_scan=stored_scan,
        skipped_destinations=skipped,
    )
