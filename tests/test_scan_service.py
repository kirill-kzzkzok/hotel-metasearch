from __future__ import annotations

import asyncio
import threading
import time

import pytest

from aviasales_collector.database import MonitorSettings, SQLiteStore
from aviasales_collector.scan_service import ScanConfigurationError, scan_and_persist


def _result(complete=True):
    offers = []
    if complete:
        offers = [
            {
                "origin": "MOW",
                "destination": "Тбилиси",
                "destination_route": "MOW0510TBS1",
                "price": 4200,
                "old_price": 5100,
                "currency": "RUB",
                "departure_date": "2026-10-05",
                "departure_date_raw": "5 окт",
                "return_date": None,
                "return_date_raw": None,
                "trip_duration_days": None,
                "flight_duration": "2 ч 40 мин в пути",
                "airline": ["Pobeda"],
                "airports": [{"code": "VKO", "name": "Внуково"}],
                "stops": 0,
                "source_url": "https://www.aviasales.ru/search/MOW0510TBS1",
                "warnings": [],
            }
        ]
    return {
        "origin": "MOW",
        "trip_type": "OW",
        "complete": complete,
        "cards_found": len(offers),
        "dom_hot_ticket_count": len(offers),
        "unique_destinations": len(offers),
        "offers": offers,
        "warnings": [] if complete else ["collector timeout"],
        "elapsed_seconds": 0.1,
    }


def test_scan_service_loads_settings_runs_collector_off_loop_and_persists_result(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "mow", "ow", 5))
        main_thread_id = threading.get_ident()
        calls = []

        def fake_collector(**kwargs):
            calls.append((threading.get_ident(), kwargs))
            time.sleep(0.05)
            return _result()

        async def scenario():
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            outcome = await scan_and_persist(
                store, 100, 200, collector_fn=fake_collector
            )
            assert heartbeat.done()
            return outcome

        outcome = asyncio.run(scenario())

        assert len(calls) == 1
        worker_thread_id, collector_kwargs = calls[0]
        assert collector_kwargs == {
            "origin": "MOW",
            "trip_type": "OW",
            "duration_min": 1,
            "duration_max": 30,
        }
        assert worker_thread_id != main_thread_id
        assert outcome.settings.origin == "MOW"
        assert outcome.stored_scan.complete is True
        assert len(outcome.stored_scan.new_destinations) == 1
        saved_offers = store.get_scan_offers(100, 200, outcome.stored_scan.scan_id)
        assert saved_offers[0]["destination"] == "Тбилиси"
        assert saved_offers[0]["old_price"] == 5100


def test_incomplete_collector_result_is_recorded_without_offers_or_destinations(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        outcome = asyncio.run(
            scan_and_persist(store, 100, 200, collector_fn=lambda **_kwargs: _result(False))
        )

        assert outcome.stored_scan.complete is False
        assert outcome.stored_scan.new_destinations == ()
        assert store.get_scan_offers(100, 200, outcome.stored_scan.scan_id) == []
        assert store.list_destinations(100, 200, "MOW") == []
        assert store.list_scans(100, 200)[0].warnings == ["collector timeout"]


@pytest.mark.parametrize("saved_settings", [None, MonitorSettings(100, 200, "MOW", "OW", 5, enabled=False)])
def test_scan_service_rejects_missing_or_disabled_configuration(tmp_path, saved_settings):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        if saved_settings is not None:
            store.save_settings(saved_settings)
        called = False

        def fake_collector(**_kwargs):
            nonlocal called
            called = True
            return _result()

        with pytest.raises(ScanConfigurationError):
            asyncio.run(scan_and_persist(store, 100, 200, collector_fn=fake_collector))
        assert called is False
