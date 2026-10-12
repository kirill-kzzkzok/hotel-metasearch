from __future__ import annotations

import asyncio
import pytest

from aviasales_collector import deep_scan as deep_scan_module
from aviasales_collector.database import MonitorSettings, SQLiteStore
from aviasales_collector.deep_scan import deep_scan_and_persist


def _offer(destination: str, price: int) -> dict:
    return {
        "origin": "MOW",
        "destination": destination,
        "destination_route": "MOW0109TBS1",
        "price": price,
        "old_price": None,
        "currency": "RUB",
        "departure_date": "2026-10-05",
        "departure_date_raw": "5 окт",
        "return_date": None,
        "return_date_raw": None,
        "trip_duration_days": None,
        "flight_duration": "4 ч в пути",
        "airline": ["Pobeda"],
        "airports": [{"code": "VKO", "name": "Внуково"}],
        "stops": 0,
        "source_url": "https://www.aviasales.ru/search/MOW0109TBS1",
        "warnings": [],
    }


def _result(offers: list[dict]) -> dict:
    return {
        "origin": "MOW",
        "trip_type": "OW",
        "complete": True,
        "cards_found": len(offers),
        "dom_hot_ticket_count": len(offers),
        "unique_destinations": len(offers),
        "offers": offers,
        "warnings": [],
    }


def test_deep_scan_uses_active_destinations_and_tracks_price(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        initial = store.record_scan(100, 200, _result([_offer("Тбилиси", 10000), _offer("Стамбул", 9000)]))
        tbilisi_id = next(item["destination_id"] for item in initial.new_destinations if item["destination"] == "Тбилиси")
        assert store.set_destination_active(100, 200, tbilisi_id, True)

        async def run():
            return await deep_scan_and_persist(
                store,
                100,
                200,
                collector_fn=lambda **_kwargs: _result([_offer("Тбилиси", 8000), _offer("Стамбул", 1000)]),
            )

        outcome = asyncio.run(run())
        assert [offer["destination"] for offer in outcome.collector_result["offers"]] == ["Тбилиси"]
        assert len(outcome.stored_scan.price_changes) == 1
        assert outcome.price_drops[0]["change_percent"] == -20.0


def test_deep_scan_limits_destinations_and_retries_incomplete_result(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        seed = store.record_scan(
            100,
            200,
            _result([_offer("Тбилиси", 10000), _offer("Стамбул", 9000)]),
        )
        for item in seed.new_destinations:
            assert store.set_destination_active(100, 200, item["destination_id"], True)

        calls = []

        def collector(**_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return {**_result([]), "complete": False, "warnings": ["timeout"]}
            return _result([_offer("Тбилиси", 8000)])

        async def run():
            return await deep_scan_and_persist(
                store,
                100,
                200,
                collector_fn=collector,
                max_destinations=1,
                retries=1,
                retry_delay_seconds=0,
            )

        outcome = asyncio.run(run())
        assert len(calls) == 2
        assert len(outcome.active_destinations) == 1
        assert len(outcome.skipped_destinations) == 1
        assert "limited to 1" in outcome.collector_result["warnings"][-1]


def test_deep_scan_reports_progress_and_keeps_successful_destinations_when_one_fails(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        seed = store.record_scan(
            100,
            200,
            _result([_offer("Тбилиси", 10000), _offer("Стамбул", 9000)]),
        )
        tbs = next(item for item in store.list_destinations(100, 200, "MOW") if item.destination == "Тбилиси")
        for item in seed.new_destinations:
            assert store.set_destination_active(100, 200, item["destination_id"], True)

        progress = []

        def collector(**kwargs):
            callback = kwargs["progress_callback"]
            callback({"status": "started", "destination": "Тбилиси", "current": 1, "total": 2, "offers": 0})
            callback({"status": "completed", "destination": "Тбилиси", "current": 1, "total": 2, "offers": 1})
            callback({"status": "failed", "destination": "Стамбул", "current": 2, "total": 2, "offers": 1})
            progress.extend(("started", "completed", "failed"))
            return {
                **_result([_offer("Тбилиси", 8000)]),
                "destinations_requested": 2,
                "destinations_scanned": 1,
                "partial": True,
            }

        # Exercise the production collector callback contract through the
        # worker-thread path used by deep_scan_and_persist.
        monkeypatch.setattr(deep_scan_module, "collect_deep", collector)

        async def run():
            return await deep_scan_and_persist(store, 100, 200, progress_callback=lambda update: progress.append(update))

        outcome = asyncio.run(run())

        assert outcome.collector_result["complete"] is True
        assert outcome.collector_result["partial"] is True
        assert outcome.collector_result["destinations_scanned"] == 1
        assert outcome.stored_scan.complete is True
        assert outcome.stored_scan.partial is True
        assert store.list_scans(100, 200, limit=1)[0].partial is True
        assert len(outcome.stored_scan.price_changes) == 1
        current_tbs = next(item for item in store.list_destinations(100, 200, "MOW") if item.destination == "Тбилиси")
        assert current_tbs.last_known_price == 10000
        assert current_tbs.last_seen == tbs.last_seen
        saved_offer = store.get_scan_offers(100, 200, outcome.stored_scan.scan_id)[0]
        assert saved_offer["price"] == 8000
        assert saved_offer["destination_id"] == tbs.destination_id
        assert progress[0]["status"] == "started"


def test_deep_scan_does_not_accept_only_unselected_destinations(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        seeded = store.record_scan(100, 200, _result([_offer("Тбилиси", 4200)]))
        store.set_destination_active(100, 200, seeded.new_destinations[0]["destination_id"], True)

        async def run():
            return await deep_scan_and_persist(
                store,
                100,
                200,
                collector_fn=lambda **_kwargs: _result([_offer("Стамбул", 5000)]),
            )

        outcome = asyncio.run(run())

        assert outcome.collector_result["complete"] is False
        assert outcome.stored_scan.complete is False
        assert store.list_destinations(100, 200, "MOW")[0].last_known_price == 4200


def test_deep_scan_derives_partial_count_when_collector_omits_progress_fields(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        seeded = store.record_scan(
            100,
            200,
            _result([_offer("Тбилиси", 4200), _offer("Стамбул", 5200)]),
        )
        for item in seeded.new_destinations:
            store.set_destination_active(100, 200, item["destination_id"], True)

        async def run():
            return await deep_scan_and_persist(
                store,
                100,
                200,
                collector_fn=lambda **_kwargs: _result([_offer("Тбилиси", 4000)]),
            )

        outcome = asyncio.run(run())

        assert outcome.collector_result["destinations_scanned"] == 1
        assert outcome.collector_result["partial"] is True


def test_partial_deep_scan_keeps_successful_cards_without_changing_catalog(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 5))
        seeded = store.record_scan(100, 200, _result([_offer("Тбилиси", 4200)]))
        store.set_destination_active(100, 200, seeded.new_destinations[0]["destination_id"], True)
        partial = {**_result([_offer("Тбилиси", 3900)]), "complete": False, "warnings": ["one destination timed out"]}

        async def run():
            return await deep_scan_and_persist(
                store, 100, 200, collector_fn=lambda **_kwargs: partial
            )

        outcome = asyncio.run(run())

        assert outcome.stored_scan.complete is False
        assert outcome.stored_scan.new_destinations == ()
        assert store.get_scan_offers(100, 200, outcome.stored_scan.scan_id)[0]["price"] == 3900
        assert store.list_destinations(100, 200, "MOW")[0].last_known_price == 4200
        assert store.list_scans(100, 200, limit=1)[0].scan_kind == "deep"


@pytest.mark.parametrize(("total", "batch_size"), [(10, 3), (50, 10), (100, 17)])
def test_persistent_rotation_eventually_scans_every_active_destination(tmp_path, total, batch_size):
    with SQLiteStore(tmp_path / "deep-rotation.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 30))
        names = [f"City-{index:03d}" for index in range(total)]
        seeded = store.record_scan(100, 200, _result([_offer(name, 20_000) for name in names]))
        for item in seeded.new_destinations:
            store.set_destination_active(100, 200, item["destination_id"], True)

        observed = []

        def collector(**kwargs):
            destinations = kwargs["destinations"]
            observed.extend(destinations)
            callback = kwargs["progress_callback"]
            for index, destination in enumerate(destinations, start=1):
                callback({"status": "completed", "destination": destination})
            return {
                **_result([_offer(name, 19_000) for name in destinations]),
                "destinations_scanned": len(destinations),
            }

        async def run_cycle():
            return await deep_scan_and_persist(
                store, 100, 200, collector_fn=collector, max_destinations=batch_size
            )

        cycles = [asyncio.run(run_cycle()) for _ in range((total + batch_size - 1) // batch_size)]
        assert all(cycle.collector_result["batch_complete"] for cycle in cycles)
        assert all(cycle.collector_result["partial"] for cycle in cycles)
        assert len(observed) == total
        assert set(observed) == set(names)
        assert len(set(observed)) == total

        # Remove/pause one existing destination and add/resume another while
        # the persistent cursor is between pages; selection still advances.
        paused = next(item for item in store.list_destinations(100, 200, "MOW") if item.destination == names[0])
        store.set_destination_active(100, 200, paused.destination_id, False)
        archived = next(item for item in store.list_destinations(100, 200, "MOW") if item.destination == names[1])
        store.set_destination_archived(100, 200, archived.destination_id, True)
        added = store.record_scan(100, 200, _result([_offer("City-new", 20_000)]))
        store.set_destination_active(100, 200, added.new_destinations[0]["destination_id"], True)
        resumed = store.set_destination_active(100, 200, paused.destination_id, True)
        assert resumed
        store.set_destination_archived(100, 200, archived.destination_id, False)

        for _ in range(total + 1):
            asyncio.run(run_cycle())
            if "City-new" in observed[total:]:
                break
        assert "City-new" in observed[total:]


def test_failed_deep_destination_is_retried_first_and_partial_is_not_full(tmp_path):
    with SQLiteStore(tmp_path / "deep-rotation-retry.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 30))
        names = [f"City-{index:02d}" for index in range(20)]
        seeded = store.record_scan(100, 200, _result([_offer(name, 20_000) for name in names]))
        for item in seeded.new_destinations:
            store.set_destination_active(100, 200, item["destination_id"], True)

        first_names = []

        def fail_first(**kwargs):
            selected = kwargs["destinations"]
            first_names.extend(selected)
            failed = selected[0]
            callback = kwargs["progress_callback"]
            for index, name in enumerate(selected, start=1):
                callback({"status": "failed" if name == failed else "completed", "destination": name})
            successful = [name for name in selected if name != failed]
            return {
                **_result([_offer(name, 19_000) for name in successful]),
                "destinations_scanned": len(successful),
                "partial": True,
            }

        first = asyncio.run(deep_scan_and_persist(
            store, 100, 200, collector_fn=fail_first, max_destinations=10
        ))
        assert first.collector_result["partial"] is True
        assert first.collector_result["batch_complete"] is False
        latest = store.list_scans(100, 200, limit=1)[0]
        assert latest.complete is True and latest.partial is True
        eligible_last_success = store.latest_scan_finished_at(100, 200, "MOW", "deep")
        assert eligible_last_success is None

        next_selected = []

        def succeed(**kwargs):
            selected = kwargs["destinations"]
            next_selected.extend(selected)
            for name in selected:
                kwargs["progress_callback"]({"status": "completed", "destination": name})
            return {**_result([_offer(name, 19_000) for name in selected]),
                    "destinations_scanned": len(selected)}

        asyncio.run(deep_scan_and_persist(
            store, 100, 200, collector_fn=succeed, max_destinations=10
        ))
        assert next_selected[0] == first_names[0]
