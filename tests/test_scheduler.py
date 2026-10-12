from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aviasales_collector.database import MonitorSettings, SQLiteStore
from aviasales_collector.scheduler import DeepScanScheduler, MonitorScheduler, ScanDisposition


def test_scheduler_scans_enabled_monitors_and_applies_changes_without_restart(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        first = MonitorSettings(100, 200, "MOW", "OW", 5)
        paused = MonitorSettings(101, 201, "LED", "RT", 10, 3, 8, enabled=False)
        store.save_settings(first)
        store.save_settings(paused)
        now = [100.0]
        calls = []

        async def scan(settings):
            calls.append((settings.user_id, settings.origin))

        scheduler = MonitorScheduler(store, scan, clock=lambda: now[0])

        async def scenario():
            await scheduler.run_once()
            await scheduler.run_once()
            now[0] += 300
            await scheduler.run_once()
            store.set_settings_enabled(100, 200, False)
            store.set_settings_enabled(101, 201, True)
            now[0] += 1
            await scheduler.run_once()

        asyncio.run(scenario())

        assert calls == [(100, "MOW"), (100, "MOW"), (101, "LED")]


def test_scheduler_retries_after_callback_failure_without_stopping(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 1))
        now = [0.0]
        attempts = []

        async def scan(_settings):
            attempts.append(now[0])
            if len(attempts) == 1:
                raise RuntimeError("transient failure")

        scheduler = MonitorScheduler(store, scan, clock=lambda: now[0])

        async def scenario():
            await scheduler.run_once()
            now[0] = 59
            await scheduler.run_once()
            now[0] = 60
            await scheduler.run_once()

        asyncio.run(scenario())

        assert attempts == [0, 60]


def test_scheduler_keeps_two_origins_for_one_chat(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        store.save_settings(MonitorSettings(100, 200, "LED", "OW", 5))
        calls = []

        async def scan(settings):
            calls.append(settings.origin)

        scheduler = MonitorScheduler(store, scan, clock=lambda: 0.0)
        asyncio.run(scheduler.run_once())

        assert sorted(calls) == ["LED", "MOW"]


def test_scheduler_retries_explicit_incomplete_callback_before_full_interval(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 10))
        now = [0.0]
        calls = []

        async def scan(_settings):
            calls.append(now[0])
            return len(calls) > 1

        scheduler = MonitorScheduler(store, scan, clock=lambda: now[0])

        async def scenario():
            await scheduler.run_once()
            now[0] = 59
            await scheduler.run_once()
            now[0] = 60
            await scheduler.run_once()

        asyncio.run(scenario())
        assert calls == [0.0, 60]


def test_scheduler_defers_overlap_without_counting_it_as_a_failure(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 10))
        now = [0.0]
        calls = []

        async def scan(_settings):
            calls.append(now[0])
            return ScanDisposition.DEFERRED if len(calls) == 1 else True

        scheduler = MonitorScheduler(store, scan, clock=lambda: now[0])

        async def scenario():
            await scheduler.run_once()
            now[0] = 29
            await scheduler.run_once()
            assert calls == [0.0]
            now[0] = 30
            await scheduler.run_once()

        asyncio.run(scenario())
        assert calls == [0.0, 30]


def test_scheduler_run_survives_a_transient_run_once_crash(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        scheduler = MonitorScheduler(store, lambda _settings: None, poll_interval_seconds=0.001)
        stop = asyncio.Event()
        calls = []

        async def run_once():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("temporary repository failure")
            stop.set()

        scheduler.run_once = run_once
        asyncio.run(scheduler.run(stop))

        assert len(calls) == 2


def test_deep_scheduler_visits_enabled_origins_on_its_own_cadence(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        store.save_settings(MonitorSettings(100, 200, "LED", "OW", 5))
        now = [0.0]
        calls = []

        async def scan(settings):
            calls.append(settings.origin)

        scheduler = DeepScanScheduler(store, scan, interval_minutes=60, clock=lambda: now[0])

        async def scenario():
            await scheduler.run_once()
            assert sorted(calls) == ["LED", "MOW"]
            calls.clear()
            now[0] = 3599
            await scheduler.run_once()
            assert calls == []
            now[0] = 3600
            await scheduler.run_once()

        asyncio.run(scenario())
        assert sorted(calls) == ["LED", "MOW"]


def test_deep_scheduler_resumes_interval_from_persisted_check_time(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, 200, "MOW", "OW", 30)
        store.save_settings(settings)
        wall_now = [datetime(2026, 9, 24, 12, tzinfo=timezone.utc).timestamp()]
        last_finished = datetime.fromtimestamp(wall_now[0], timezone.utc) - timedelta(minutes=20)
        store.record_scan(
            100, 200,
            {
                "origin": "MOW", "trip_type": "OW", "complete": True,
                "cards_found": 1, "unique_destinations": 1,
                "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
                "warnings": [],
            },
            scan_kind="deep", finished_at=last_finished,
        )
        monotonic_now = [100.0]
        calls = []

        async def scan(item):
            calls.append(item.origin)

        scheduler = DeepScanScheduler(
            store, scan, interval_minutes=30,
            clock=lambda: monotonic_now[0], wall_clock=lambda: wall_now[0],
        )

        async def scenario():
            await scheduler.run_once()
            assert calls == []
            monotonic_now[0] += 10 * 60
            wall_now[0] += 10 * 60
            await scheduler.run_once()

        asyncio.run(scenario())
        assert calls == ["MOW"]


def test_deep_scheduler_defers_overlap_without_failure_backoff(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "LED", "OW", 30))
        now = [0.0]
        calls = []

        async def scan(_settings):
            calls.append(now[0])
            return ScanDisposition.DEFERRED if len(calls) == 1 else True

        scheduler = DeepScanScheduler(
            store, scan, interval_minutes=360, clock=lambda: now[0]
        )

        async def scenario():
            await scheduler.run_once()
            now[0] = 29
            await scheduler.run_once()
            assert calls == [0.0]
            now[0] = 30
            await scheduler.run_once()

        asyncio.run(scenario())
        assert calls == [0.0, 30]
