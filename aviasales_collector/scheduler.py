from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable

from .database import MonitorSettings, SQLiteStore
from .monitoring_logs import log_monitoring, log_monitoring_exception, scheduler_clock_time


log = logging.getLogger(__name__)


class ScanDisposition(Enum):
    DEFERRED = "deferred"


ScanCallback = Callable[[MonitorSettings], Awaitable[None | bool | ScanDisposition]]
_DEFERRED_RETRY_SECONDS = 30


@dataclass
class _ScheduledMonitor:
    settings: MonitorSettings
    next_run: float
    failures: int = 0


class MonitorScheduler:
    """Sequential in-process scheduler for enabled monitor configurations."""

    def __init__(
        self,
        store: SQLiteStore,
        scan_callback: ScanCallback,
        *,
        poll_interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.store = store
        self.scan_callback = scan_callback
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock
        self._scheduled: dict[tuple[int, int, str], _ScheduledMonitor] = {}

    async def run_once(self) -> None:
        now = self.clock()
        current = {
            (settings.user_id, settings.chat_id, settings.origin): settings
            for settings in self.store.list_enabled_settings()
        }

        for owner in self._scheduled.keys() - current.keys():
            self._scheduled.pop(owner, None)

        for owner, settings in current.items():
            existing = self._scheduled.get(owner)
            if existing is None or existing.settings != settings:
                self._scheduled[owner] = _ScheduledMonitor(settings=settings, next_run=now)

        due = sorted(
            (
                (owner, scheduled)
                for owner, scheduled in self._scheduled.items()
                if scheduled.next_run <= now
            ),
            key=lambda item: (item[1].next_run, item[0]),
        )
        for owner, scheduled in due:
            log.debug(
                "[SCHEDULER] Scan due user=%s chat=%s origin=%s interval=%sm",
                owner[0],
                owner[1],
                scheduled.settings.origin,
                scheduled.settings.interval_minutes,
            )
            try:
                success = await self.scan_callback(scheduled.settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_monitoring_exception(
                    log,
                    message="❌ %s · ошибка проверки: %s",
                    error=exc,
                    debug_context="SCHEDULER",
                    args=(scheduled.settings.origin,),
                )
                success = False

            current_schedule = self._scheduled.get(owner)
            if current_schedule is scheduled:
                if success is ScanDisposition.DEFERRED:
                    current_schedule.next_run = self.clock() + max(
                        self.poll_interval_seconds, _DEFERRED_RETRY_SECONDS
                    )
                    continue
                if success is False:
                    current_schedule.failures += 1
                    retry_delay = min(
                        scheduled.settings.interval_minutes * 60,
                        60 * (2 ** min(current_schedule.failures - 1, 4)),
                    )
                    current_schedule.next_run = self.clock() + retry_delay
                    retry_at = scheduler_clock_time(current_schedule.next_run, self.clock())
                    log_monitoring(
                        log,
                        logging.WARNING,
                        "⚠️ %s · проверка не завершилась · повтор в %s",
                        scheduled.settings.origin,
                        retry_at,
                    )
                    continue
                current_schedule.failures = 0
                current_schedule.next_run = self.clock() + scheduled.settings.interval_minutes * 60
                next_clock = scheduler_clock_time(current_schedule.next_run, self.clock())
                log_monitoring(
                    log,
                    logging.INFO,
                    "⏰ %s · следующая проверка в %s",
                    scheduled.settings.origin,
                    next_clock,
                )

    async def run(self, stop_event: asyncio.Event) -> None:
        log_monitoring(log, logging.INFO, "⏰ Планировщик проверок запущен")
        try:
            while not stop_event.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A repository or callback failure must not silently kill
                    # the long-lived scheduler task and leave the bot alive
                    # without any future scans.
                    log_monitoring_exception(
                        log,
                        message="❌ Планировщик проверки остановился с ошибкой: %s",
                        error=exc,
                        debug_context="SCHEDULER",
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            log_monitoring(log, logging.INFO, "⏰ Планировщик проверок остановлен")


class DeepScanScheduler:
    """Optional low-frequency scheduler for deep scans."""

    def __init__(
        self,
        store: SQLiteStore,
        scan_callback: ScanCallback,
        *,
        interval_minutes: int,
        poll_interval_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        if interval_minutes < 1:
            raise ValueError("deep scan interval must be at least one minute")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.store = store
        self.scan_callback = scan_callback
        self.interval_minutes = interval_minutes
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock
        self.wall_clock = wall_clock
        self._next_run: dict[tuple[int, int, str], float] = {}
        self._failures: dict[tuple[int, int, str], int] = {}

    async def run_once(self) -> None:
        now = self.clock()
        current = {
            (settings.user_id, settings.chat_id, settings.origin): settings
            for settings in self.store.list_enabled_settings()
        }
        for key in set(self._next_run) - set(current):
            self._next_run.pop(key, None)
            self._failures.pop(key, None)
        for key, settings in current.items():
            if key in self._next_run:
                continue
            last_finished = self.store.latest_scan_finished_at(*key, "deep")
            if last_finished:
                try:
                    finished_at = datetime.fromisoformat(last_finished.replace("Z", "+00:00"))
                    elapsed = max(0.0, self.wall_clock() - finished_at.timestamp())
                    delay = max(0.0, self.interval_minutes * 60 - elapsed)
                except ValueError:
                    log_monitoring(
                        log,
                        logging.WARNING,
                        "⚠️ %s · время прошлой глубокой проверки не распознано; проверка назначена сейчас",
                        settings.origin,
                    )
                    delay = 0.0
                self._next_run[key] = now + delay
            else:
                self._next_run[key] = now

        due = sorted(key for key, next_run in self._next_run.items() if next_run <= now)
        for key in due:
            settings = current[key]
            log.debug(
                "[DEEP-SCHEDULER] Scan due origin=%s user=%s chat=%s",
                settings.origin,
                settings.user_id,
                settings.chat_id,
            )
            try:
                success = await self.scan_callback(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_monitoring_exception(
                    log,
                    message="❌ %s · ошибка глубокой проверки: %s",
                    error=exc,
                    debug_context="DEEP-SCHEDULER",
                    args=(settings.origin,),
                )
                success = False
            if success is ScanDisposition.DEFERRED:
                self._next_run[key] = self.clock() + max(
                    self.poll_interval_seconds, _DEFERRED_RETRY_SECONDS
                )
                continue
            if success is False:
                self._failures[key] = self._failures.get(key, 0) + 1
                retry_delay = min(self.interval_minutes * 60, 300 * (2 ** min(self._failures[key] - 1, 3)))
                self._next_run[key] = self.clock() + retry_delay
                retry_at = scheduler_clock_time(self._next_run[key], self.clock())
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ %s · глубокая проверка не завершилась · повтор в %s",
                    settings.origin,
                    retry_at,
                )
                continue
            self._failures[key] = 0
            self._next_run[key] = self.clock() + self.interval_minutes * 60
            next_clock = scheduler_clock_time(self._next_run[key], self.clock())
            log_monitoring(
                log,
                logging.INFO,
                "⏰ %s · следующая глубокая проверка в %s",
                settings.origin,
                next_clock,
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        log_monitoring(log, logging.INFO, "⏰ Планировщик глубоких проверок запущен")
        try:
            while not stop_event.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_monitoring_exception(
                        log,
                        message="❌ Планировщик глубоких проверок остановился с ошибкой: %s",
                        error=exc,
                        debug_context="DEEP-SCHEDULER",
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            log_monitoring(log, logging.INFO, "⏰ Планировщик глубоких проверок остановлен")
