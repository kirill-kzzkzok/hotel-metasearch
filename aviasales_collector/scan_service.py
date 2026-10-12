from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from .collector import collect
from .database import MonitorSettings, SQLiteStore, ScanRecord


class ScanConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanOutcome:
    settings: MonitorSettings
    collector_result: dict[str, Any]
    stored_scan: ScanRecord


async def scan_and_persist(
    store: SQLiteStore,
    user_id: int,
    chat_id: int,
    *,
    collector_fn: Callable[..., dict[str, Any]] | None = None,
    origin: str | None = None,
    price_drop_threshold_percent: float = 12.0,
) -> ScanOutcome:
    """Run one saved monitor configuration without blocking the calling event loop."""
    settings = store.get_settings(user_id, chat_id, origin)
    if settings is None:
        raise ScanConfigurationError("monitor settings are not configured for this user and chat")
    if not settings.enabled:
        raise ScanConfigurationError("monitor settings are disabled")

    run_collector = collector_fn or collect
    result = await asyncio.to_thread(
        run_collector,
        origin=settings.origin,
        trip_type=settings.trip_type,
        duration_min=settings.duration_min_days or 1,
        duration_max=settings.duration_max_days or 30,
    )
    result["duration_min_days"] = settings.duration_min_days
    result["duration_max_days"] = settings.duration_max_days
    stored_scan = store.record_scan(
        user_id, chat_id, result, track_price_changes=True, scan_kind="regular",
        price_drop_threshold_percent=price_drop_threshold_percent,
    )
    return ScanOutcome(settings=settings, collector_result=result, stored_scan=stored_scan)
