from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import re

import pytest

from aviasales_collector.database import MonitorSettings, SQLiteStore, SCHEMA_VERSION


def _offer(destination, route, price, departure="2026-10-05"):
    return {
        "origin": "MOW",
        "destination": destination,
        "destination_route": route,
        "price": price,
        "old_price": None,
        "currency": "RUB",
        "departure_date": departure,
        "departure_date_raw": "5 окт",
        "return_date": None,
        "return_date_raw": None,
        "trip_duration_days": None,
        "flight_duration": "4 ч в пути",
        "airline": ["Pobeda"],
        "airports": [{"code": "LED", "name": "Пулково"}] if route.startswith("LED") else [{"code": "VKO", "name": "Внуково"}],
        "stops": 0,
        "source_url": f"https://www.aviasales.ru/search/{route}",
        "warnings": [],
    }


def _scan(offers, *, complete=True, trip_type="OW"):
    if trip_type == "RT":
        for offer in offers:
            offer["source_url"] = re.sub(r"(/search/[A-Z]{3}\d{4}[A-Z]{3})(\d+)$", r"\g<1>20101", offer["source_url"])
    return {
        "origin": "MOW",
        "trip_type": trip_type,
        "duration_min_days": 3 if trip_type == "RT" else None,
        "duration_max_days": 11 if trip_type == "RT" else None,
        "complete": complete,
        "cards_found": len(offers),
        "dom_hot_ticket_count": len(offers),
        "unique_destinations": len({offer.get("destination") for offer in offers}),
        "offers": offers,
        "warnings": [] if complete else ["collector timeout"],
        "elapsed_seconds": 12.5,
    }


def test_settings_round_trip_and_upsert_are_scoped_by_user_and_chat(tmp_path):
    database_path = tmp_path / "monitor.sqlite3"
    with SQLiteStore(database_path) as store:
        store.save_settings(MonitorSettings(100, -200, "mow", "ow", 5))
        store.save_settings(MonitorSettings(101, -200, "led", "rt", 10, 3, 8))
    with SQLiteStore(database_path) as store:
        first = store.get_settings(100, -200)
        second = store.get_settings(101, -200)
        assert first == MonitorSettings(100, -200, "MOW", "OW", 5)
        assert second == MonitorSettings(101, -200, "LED", "RT", 10, 3, 8)

        store.save_settings(MonitorSettings(100, -200, "LED", "RT", 15, 4, 12, enabled=False))
        updated = store.get_settings(100, -200)
        assert updated == MonitorSettings(100, -200, "LED", "RT", 15, 4, 12, enabled=False)
        assert store.get_settings(999, -200) is None


def test_enabled_settings_can_be_listed_and_paused_without_deleting_configuration(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        enabled = MonitorSettings(100, -200, "MOW", "OW", 5)
        paused = MonitorSettings(101, -200, "LED", "RT", 10, 3, 8, enabled=False)
        store.save_settings(enabled)
        store.save_settings(paused)

        assert store.list_enabled_settings() == [enabled]
        assert store.set_settings_enabled(100, -200, False)
        assert store.list_enabled_settings() == []
        assert store.get_settings(100, -200) == MonitorSettings(100, -200, "MOW", "OW", 5, enabled=False)
        assert store.set_settings_enabled(100, -200, True)
        assert store.list_enabled_settings() == [enabled]
        assert not store.set_settings_enabled(999, -200, True)


def test_settings_are_kept_separately_for_multiple_origins(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        mow = MonitorSettings(100, -200, "MOW", "RT", 15, 4, 11)
        led = MonitorSettings(100, -200, "LED", "OW", 30)
        store.save_settings(mow)
        store.save_settings(led)

        assert store.list_settings(100, -200) == [led, mow]
        assert store.get_settings(100, -200, "MOW") == mow
        assert store.get_settings(100, -200, "LED") == led
        assert {item.origin for item in store.list_enabled_settings()} == {"MOW", "LED"}


@pytest.mark.parametrize(
    "settings",
    [
        {"trip_type": "OW", "origin": "MOW", "interval_minutes": 0},
        {"trip_type": "RT", "origin": "MOW", "interval_minutes": 5, "duration_min_days": 9, "duration_max_days": 3},
        {"trip_type": "OW", "origin": "Москва", "interval_minutes": 5},
    ],
)
def test_invalid_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        MonitorSettings(user_id=1, chat_id=1, **settings)


def test_complete_scans_keep_destination_history_price_snapshots_and_activation(tmp_path):
    at_1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    at_2 = datetime(2026, 9, 10, tzinfo=timezone.utc)
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        first = store.record_scan(
            100,
            200,
            _scan(
                [
                    _offer("Тбилиси", "MOW0109TBS1", 4200),
                    _offer("Стамбул", "MOW0109IST1", 8100),
                ]
            ),
            started_at=at_1,
            finished_at=at_1,
        )
        assert len(first.new_destinations) == 2
        istanbul_id = next(
            item["destination_id"]
            for item in first.new_destinations
            if item["destination"] == "Стамбул"
        )
        assert store.set_destination_active(100, 200, istanbul_id, True)
        assert not store.set_destination_active(999, 999, istanbul_id, False)

        second = store.record_scan(
            100,
            200,
            _scan(
                [
                    _offer("ТБИЛИСИ", "MOW1009TBS1", 3900),
                    _offer("Барселона", "MOW1009BCN1", 12000),
                ]
            ),
            started_at=at_2,
            finished_at=at_2,
        )

        assert [item["destination"] for item in second.new_destinations] == ["Барселона"]
        assert second.outdated_offers == 2
        assert second.disappeared_destinations == ("Стамбул",)
        destinations = {item.destination: item for item in store.list_destinations(100, 200, "MOW")}
        assert set(destinations) == {"ТБИЛИСИ", "Стамбул", "Барселона"}
        assert destinations["ТБИЛИСИ"].first_seen == "2026-09-01T00:00:00Z"
        assert destinations["ТБИЛИСИ"].last_seen == "2026-09-10T00:00:00Z"
        assert destinations["ТБИЛИСИ"].last_known_price == 3900
        assert destinations["Стамбул"].last_seen == "2026-09-01T00:00:00Z"
        assert destinations["Стамбул"].active is True
        assert destinations["Барселона"].active is False

        offers = store.get_scan_offers(100, 200, second.scan_id)
        assert len(offers) == 2
        assert offers[0]["flight_duration"] == "4 ч в пути"
        assert offers[0]["airline"] == ["Pobeda"]
        assert offers[0]["airports"] == [{"code": "VKO", "name": "Внуково"}]
        assert offers[0]["stops"] == 0
        assert store.get_scan_offers(999, 999, second.scan_id) == []


def test_current_destinations_follow_latest_successful_regular_scan(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        first = store.record_scan(
            100, 200,
            _scan([_offer("Занзибар", "MOW0109ZNZ1", 50_000)]),
            finished_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        zanzibar_id = first.new_destinations[0]["destination_id"]
        assert store.set_destination_active(100, 200, zanzibar_id, True)

        latest = store.record_scan(
            100, 200,
            _scan([_offer("Тбилиси", "MOW0209TBS1", 4_200)]),
            finished_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        current = store.list_destinations(100, 200, "MOW", available_only=True)
        assert [item.destination for item in current] == ["Тбилиси"]
        assert {item.destination for item in store.list_destinations(100, 200, "MOW")} == {
            "Занзибар", "Тбилиси"
        }

        store.record_scan(
            100, 200,
            _scan([_offer("Занзибар", "MOW0309ZNZ1", 48_000)], complete=False),
            finished_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
        assert [item.destination for item in store.list_destinations(
            100, 200, "MOW", available_only=True
        )] == ["Тбилиси"]

        store.record_scan(
            100, 200,
            _scan([_offer("Занзибар", "MOW0409ZNZ1", 49_000)]),
            finished_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        )
        current = store.list_destinations(100, 200, "MOW", available_only=True)
        assert [item.destination for item in current] == ["Занзибар"]
        assert current[0].destination_id == zanzibar_id
        assert current[0].active is True


def test_tracked_price_changes_include_offer_metadata(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        first = store.record_scan(
            100,
            200,
            _scan([_offer("Тбилиси", "MOW0109TBS1", 10000)]),
        )
        destination_id = first.new_destinations[0]["destination_id"]
        assert store.set_destination_active(100, 200, destination_id, True)

        changed = store.record_scan(
            100,
            200,
            _scan([
                _offer("Тбилиси", "MOW0109TBS1", 9000),
                {
                    **_offer("Тбилиси", "MOW0109TBS2", 8000),
                    "departure_date_raw": "12 окт",
                    "stops": [1, 0],
                    "transfers": ["Стамбул"],
                    "transfer_durations": ["2 ч 30 мин"],
                    "flight_duration": ["8 ч", "5 ч"],
                },
            ]),
            track_price_changes=True,
        )
        assert len(changed.price_changes) == 1
        assert changed.price_changes[0]["previous_price"] == 10000
        assert changed.price_changes[0]["price"] == 8000
        assert changed.price_changes[0]["change_percent"] == -20.0
        assert changed.price_changes[0]["departure_date_raw"] == "12 окт"
        assert changed.price_changes[0]["transfers"] == ["Стамбул"]
        assert changed.price_changes[0]["transfer_durations"] == ["2 ч 30 мин"]
        assert store.list_destinations(100, 200, "MOW")[0].last_known_price == 8000

        unchanged = store.record_scan(
            100,
            200,
            _scan([_offer("Тбилиси", "MOW0109TBS1", 8000)]),
            track_price_changes=True,
        )
        assert unchanged.price_changes == ()


def test_inactive_destination_price_drop_is_still_reported(tmp_path):
    with SQLiteStore(tmp_path / "inactive-price-change.sqlite3") as store:
        store.record_scan(100, 200, _scan([_offer("Калининград", "MOW0109KGD1", 7100)]))
        destination = store.list_destinations(100, 200, "MOW")[0]
        assert destination.active is False

        changed = store.record_scan(
            100, 200, _scan([_offer("Калининград", "MOW0109KGD1", 6200)]),
            track_price_changes=True,
        )

        assert len(changed.price_changes) == 1
        assert changed.price_changes[0]["previous_price"] == 7100
        assert changed.price_changes[0]["price"] == 6200


def test_price_change_percentage_is_not_compared_across_currencies(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        first = store.record_scan(100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 10000)]))
        store.set_destination_active(100, 200, first.new_destinations[0]["destination_id"], True)
        eur_offer = {**_offer("Тбилиси", "MOW0109TBS2", 8000), "currency": "EUR"}

        changed = store.record_scan(
            100, 200, _scan([eur_offer]), track_price_changes=True
        )

        assert changed.price_changes[0]["change_percent"] is None


def test_price_drop_alert_claim_is_durable_and_allows_a_new_price_transition(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    change = {
        "origin": "MOW", "destination": "Нячанг", "previous_price": 50000,
        "price": 40000, "currency": "RUB",
    }
    with SQLiteStore(path) as store:
        assert store.claim_price_drop_alert(100, 200, change)
        assert not store.claim_price_drop_alert(100, 200, change)

    with SQLiteStore(path) as reopened:
        assert not reopened.claim_price_drop_alert(100, 200, change)
        assert reopened.claim_price_drop_alert(100, 200, {**change, "previous_price": 60000})


def test_active_kaliningrad_finding_catches_gradual_historical_drop(tmp_path):
    with SQLiteStore(tmp_path / "deals.sqlite3") as store:
        initial_scan = _scan([_offer("Калининград", "LED0109KGD1", 7100)], trip_type="RT")
        initial_scan["origin"] = "LED"
        first = store.record_scan(
            100, 200, initial_scan,
            track_price_changes=True,
        )
        destination_id = first.new_destinations[0]["destination_id"]
        assert store.set_destination_active(100, 200, destination_id, True)
        for price in (6400, 5400):
            scan = _scan([_offer("Калининград", "LED0109KGD1", price)], trip_type="RT")
            scan["origin"] = "LED"
            outcome = store.record_scan(
                100, 200, scan,
                track_price_changes=True,
            )
        assert len(outcome.price_findings) == 1
        finding = outcome.price_findings[0]
        assert finding["price"] == 5400
        assert finding["previous_price"] == 6400
        assert finding["deal_metrics"]["reference_drop_amount"] == 1350
        assert finding["deal_metrics"]["previous_change_amount"] == -1000
        row = store.connection.execute(
            "SELECT status FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()
        assert row["status"] == "pending"


def test_rt_price_history_is_scoped_to_exact_duration_context(tmp_path):
    with SQLiteStore(tmp_path / "rt-context.sqlite3") as store:
        def save(price, *, trip_type="RT", context=(3, 11)):
            result = _scan([_offer("Контекстный город", "MOW0109TST1", price)], trip_type=trip_type)
            if context is None:
                result.pop("duration_min_days", None)
                result.pop("duration_max_days", None)
            elif trip_type == "RT":
                result["duration_min_days"], result["duration_max_days"] = context
            return store.record_scan(100, 200, result, track_price_changes=True)

        # Same visible route and currency, but three distinct contexts.
        save(50_000, trip_type="OW")
        save(20_000, context=None)  # legacy/unknown RT context
        prices = [8500, 9500, 9800, 10000, 10000, 10500, 11000, 10000]
        for price in prices:
            save(price, context=(3, 11))

        same_context = save(8500, context=(3, 11)).price_findings
        assert same_context and same_context[0]["deal_metrics"]["observation_count"] == 8
        assert same_context[0]["deal_metrics"]["reference_price"] == 10000

        different_context = save(30000, context=(7, 10)).price_findings
        assert not different_context
        assert store.connection.execute(
            """SELECT COUNT(*) FROM price_observations
                WHERE trip_type = 'RT' AND duration_min_days = 7 AND duration_max_days = 10"""
        ).fetchone()[0] == 1
        assert store.connection.execute(
            """SELECT COUNT(*) FROM price_observations
                WHERE trip_type = 'RT' AND duration_min_days IS NULL AND duration_max_days IS NULL"""
        ).fetchone()[0] == 1

        for price in prices:
            save(price, context=(7, 10))
        other_context = save(8500, context=(7, 10)).price_findings
        assert other_context and other_context[0]["deal_metrics"]["observation_count"] == 9
        assert store.connection.execute(
            """SELECT COUNT(*) FROM price_observations
                WHERE trip_type = 'OW' AND duration_min_days IS NULL AND duration_max_days IS NULL"""
        ).fetchone()[0] == 1


@pytest.mark.parametrize(("threshold", "expected_finding"), [(5, True), (12, True), (20, False)])
def test_scan_service_threshold_reaches_database_finding_evaluator(tmp_path, threshold, expected_finding):
    import asyncio
    from aviasales_collector.scan_service import scan_and_persist

    with SQLiteStore(tmp_path / f"threshold-{threshold}.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 200, "MOW", "OW", 30))
        for price in [8500, 9500, 9800, 10000, 10000, 10500, 11000, 10000]:
            store.record_scan(100, 200, _scan([_offer("Порог", "MOW0109TST1", price)]))

        def collector(**_kwargs):
            return _scan([_offer("Порог", "MOW0109TST1", 8500)])

        outcome = asyncio.run(scan_and_persist(
            store, 100, 200, collector_fn=collector,
            price_drop_threshold_percent=threshold,
        ))
        assert bool(outcome.stored_scan.price_findings) is expected_finding


def test_price_alert_uses_prior_observation_not_inserted_current_scan(tmp_path):
    with SQLiteStore(tmp_path / "previous-observation.sqlite3") as store:
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        outcomes = []
        for index, price in enumerate((10000, 9000, 8000, 7000)):
            scan = _scan([_offer("Тестовый город", "MOW0109TST1", price)])
            finished = start + timedelta(hours=index * 8)
            outcomes.append(store.record_scan(
                100, 200, scan, started_at=finished, finished_at=finished,
                track_price_changes=True,
            ))
            if index == 0:
                # Simulate the v12→v13 migration's SQLite lower(), which did
                # not casefold Cyrillic route names.
                store.connection.execute(
                    "UPDATE price_observations SET destination_key = ? WHERE scan_id = ?",
                    ("Тестовый город", outcomes[0].scan_id),
                )
                store.connection.commit()

        rows = store.connection.execute(
            "SELECT price, scan_id FROM price_observations ORDER BY observed_at, id"
        ).fetchall()
        assert [row["price"] for row in rows] == [10000, 9000, 8000, 7000]
        assert len({row["scan_id"] for row in rows}) == 4

        finding = outcomes[-1].price_findings[0]
        metrics = finding["deal_metrics"]
        assert metrics["previous_price"] == 8000
        assert finding["price"] == 7000
        assert metrics["reference_drop_amount"] == 2000
        assert metrics["reference_drop_percent"] == 22.22
        assert metrics["previous_change_amount"] == -1000
        assert metrics["previous_change_percent"] == -12.5
        assert metrics["reference_price"] == 9000
        assert metrics["observation_count"] == 3
        assert finding["notification_queued"] is True
        assert store.connection.execute(
            "SELECT status FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()["status"] == "pending"


def test_price_alert_rebound_reports_previous_low_but_does_not_find_a_drop(tmp_path):
    with SQLiteStore(tmp_path / "rebound-previous.sqlite3") as store:
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        outcomes = []
        for index, price in enumerate((10000, 7000, 9000)):
            outcomes.append(store.record_scan(
                100, 200, _scan([_offer("Тестовый город", "MOW0109TST1", price)]),
                started_at=start + timedelta(hours=index * 8),
                finished_at=start + timedelta(hours=index * 8),
                track_price_changes=True,
            ))
        assert outcomes[-1].price_findings == ()
        from aviasales_collector.price_alerts import analyze_deal

        metrics = analyze_deal(9000, "RUB", [
            {"price": 10000, "currency": "RUB"},
            {"price": 7000, "currency": "RUB"},
        ])
        assert metrics["previous_price"] == 7000
        assert metrics["historical_low"] == 7000
        assert metrics["reference_drop_amount"] == -500
        assert metrics["reference_drop_percent"] == -5.88
        assert metrics["previous_change_amount"] == 2000
        assert metrics["finding"] is False
        history = store.connection.execute(
            "SELECT price FROM price_observations ORDER BY observed_at, id"
        ).fetchall()
        assert [row["price"] for row in history] == [10000, 7000, 9000]


def test_large_single_drop_creates_finding_for_inactive_route(tmp_path):
    with SQLiteStore(tmp_path / "single-drop.sqlite3") as store:
        first = store.record_scan(100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 30431)]))
        destination_id = first.new_destinations[0]["destination_id"]
        assert store.list_destinations(100, 200, "MOW")[0].active is False
        outcome = store.record_scan(
            100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 11940)]),
            track_price_changes=True,
        )
        assert len(outcome.price_findings) == 1
        finding = outcome.price_findings[0]
        assert finding["previous_price"] == 30431
        assert finding["deal_metrics"]["reference_drop_amount"] == 18491
        assert finding["deal_metrics"]["reference_drop_percent"] == 60.76
        assert finding["deal_metrics"]["finding"] is True
        assert finding["notification_queued"] is True
        assert store.list_destinations(100, 200, "MOW")[0].destination_id == destination_id
        rebound = store.record_scan(
            100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 30432)]),
            track_price_changes=True,
        )
        assert rebound.price_findings == ()
        assert store.connection.execute("SELECT COUNT(*) FROM price_findings").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0] == 1


def test_archived_destination_price_drop_keeps_finding_without_telegram_outbox(tmp_path):
    with SQLiteStore(tmp_path / "archived-price-alert.sqlite3") as store:
        baseline = store.record_scan(100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 10000)]))
        destination_id = baseline.new_destinations[0]["destination_id"]
        assert store.set_destination_archived(100, 200, destination_id, True)

        outcome = store.record_scan(
            100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 7000)]),
            track_price_changes=True,
        )

        assert len(outcome.price_findings) == 1
        finding = outcome.price_findings[0]
        assert finding["deal_metrics"]["finding"] is True
        assert finding["notification_queued"] is False
        assert store.connection.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0] == 0
        assert store.claim_pending_price_notifications(100, 200) == []


def test_archiving_destination_cancels_queued_telegram_price_alert(tmp_path):
    with SQLiteStore(tmp_path / "archive-pending-alert.sqlite3") as store:
        baseline = store.record_scan(100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 10000)]))
        destination_id = baseline.new_destinations[0]["destination_id"]
        finding = store.record_scan(
            100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 7000)]),
            track_price_changes=True,
        ).price_findings[0]
        assert finding["notification_queued"] is True

        assert store.set_destination_archived(100, 200, destination_id, True)

        outbox = store.connection.execute(
            "SELECT status, last_error FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()
        assert outbox["status"] == "failed"
        assert "archived" in outbox["last_error"]
        assert store.claim_pending_price_notifications(100, 200) == []


def test_claim_cleans_pending_alerts_for_destination_archived_outside_store(tmp_path):
    with SQLiteStore(tmp_path / "archive-pending-external.sqlite3") as store:
        baseline = store.record_scan(100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 10000)]))
        destination_id = baseline.new_destinations[0]["destination_id"]
        finding = store.record_scan(
            100, 200, _scan([_offer("Мумбаи", "MOW0109BOM1", 7000)]),
            track_price_changes=True,
        ).price_findings[0]
        assert finding["notification_queued"] is True
        store.connection.execute("UPDATE destinations SET archived = 1 WHERE id = ?", (destination_id,))
        store.connection.commit()

        assert store.claim_pending_price_notifications(100, 200) == []
        outbox = store.connection.execute(
            "SELECT status, last_error FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()
        assert outbox["status"] == "failed"
        assert "archived" in outbox["last_error"]


@pytest.mark.parametrize("active", [False, True])
def test_same_price_drop_finds_and_queues_independent_of_active_flag(tmp_path, active):
    with SQLiteStore(tmp_path / f"active-{active}.sqlite3") as store:
        first = store.record_scan(100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 10000)]))
        destination_id = first.new_destinations[0]["destination_id"]
        if active:
            assert store.set_destination_active(100, 200, destination_id, True)

        outcome = store.record_scan(
            100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 7000)]),
            track_price_changes=True,
        )

        assert len(outcome.price_findings) == 1
        finding = outcome.price_findings[0]
        assert finding["notification_queued"] is True
        assert finding["price"] == 7000
        assert finding["deal_metrics"]["reference_drop_percent"] == 30.0
        outbox = store.connection.execute(
            "SELECT status FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()
        assert outbox["status"] == "pending"


def test_deal_events_deduplicate_repeats_and_rearm_after_recovery(tmp_path):
    with SQLiteStore(tmp_path / "rearm.sqlite3") as store:
        findings = []
        for price in (10000, 8000, 8000, 8000, 10000, 7000):
            outcome = store.record_scan(
                100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", price)]),
                track_price_changes=True,
            )
            findings.extend(item for item in outcome.price_findings if item["notification_queued"])
        # The second move is a large step from the prior scan but only a
        # modest change against the rolling reference; previous-only score
        # evidence must not generate another finding.
        assert [item["price"] for item in findings] == [8000]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM telegram_notification_outbox"
        ).fetchone()[0] == 1


def test_notification_outbox_claim_is_single_winner_and_failure_keeps_finding(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    with SQLiteStore(path) as first:
        first.record_scan(100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 10000)]))
        finding = first.record_scan(
            100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 8000)]),
            track_price_changes=True,
        ).price_findings[0]
        with SQLiteStore(path) as second:
            claimed = first.claim_pending_price_notifications(100, 200)
            assert len(claimed) == 1
            assert claimed[0]["finding_id"] == finding["finding_id"]
            assert second.claim_pending_price_notifications(100, 200) == []
            first.mark_price_notification_failed(claimed[0]["notification_id"], "temporary Telegram outage")
            outbox = first.connection.execute(
                "SELECT status, attempts, last_error FROM telegram_notification_outbox"
            ).fetchone()
            assert outbox["status"] == "retryable"
            assert outbox["attempts"] == 1
            assert "temporary Telegram outage" in outbox["last_error"]
            assert first.connection.execute("SELECT COUNT(*) FROM price_findings").fetchone()[0] == 1


def test_schema_12_migration_backfills_valid_price_baseline(tmp_path):
    path = tmp_path / "baseline-migration.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 10000)]))
        destination_id = saved.new_destinations[0]["destination_id"]
        store.set_destination_active(100, 200, destination_id, True)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE telegram_notification_outbox")
    connection.execute("DROP TABLE price_findings")
    connection.execute("DROP TABLE deal_alert_state")
    connection.execute("DROP TABLE price_observations")
    connection.execute("PRAGMA user_version = 12")
    connection.commit()
    connection.close()

    with SQLiteStore(path) as migrated:
        row = migrated.connection.execute(
            "SELECT origin, destination, price, currency FROM price_observations"
        ).fetchone()
        assert tuple(row) == ("MOW", "Тбилиси", 10000, "RUB")
        assert migrated.connection.execute("SELECT COUNT(*) FROM scan_history").fetchone()[0] == 1
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
        assert migrated.connection.execute("SELECT COUNT(*) FROM price_findings").fetchone()[0] == 0
        assert migrated.connection.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0] == 0
        destination = migrated.list_destinations(100, 200, "MOW")[0]
        assert (destination.destination_route, destination.active, destination.last_known_price) == (
            "MOW0109TBS1", True, 10000
        )

    # Reopening at v13 is a no-op; baseline rows and schema objects are not duplicated.
    with SQLiteStore(path) as reopened:
        assert reopened.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert reopened.connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0] == 1
        assert reopened.connection.execute("SELECT COUNT(*) FROM scan_history").fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'telegram_notification_outbox'"
        ).fetchone()[0] == 1


def test_v14_migration_preserves_rt_history_and_marks_inflight_outbox_uncertain(tmp_path):
    path = tmp_path / "v14-to-v15.sqlite3"
    with SQLiteStore(path) as store:
        store.record_scan(100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 10000)]))
        store.record_scan(
            100, 200, _scan([_offer("Тбилиси", "MOW0109TBS1", 8000)]),
            track_price_changes=True,
        )
        finding_id = store.connection.execute(
            "SELECT finding_id FROM telegram_notification_outbox"
        ).fetchone()[0]

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE scan_history DROP COLUMN duration_max_days")
    connection.execute("ALTER TABLE scan_history DROP COLUMN duration_min_days")
    connection.execute("ALTER TABLE price_observations DROP COLUMN duration_max_days")
    connection.execute("ALTER TABLE price_observations DROP COLUMN duration_min_days")
    connection.execute("DROP INDEX telegram_outbox_due")
    connection.execute("ALTER TABLE telegram_notification_outbox RENAME TO outbox_v15")
    connection.execute("""CREATE TABLE telegram_notification_outbox (
        id INTEGER PRIMARY KEY,
        finding_id INTEGER NOT NULL UNIQUE REFERENCES price_findings(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','sending','sent','retryable','failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        next_attempt_at TEXT NOT NULL, lease_until TEXT, last_error TEXT,
        created_at TEXT NOT NULL, sent_at TEXT
    )""")
    connection.execute("""INSERT INTO telegram_notification_outbox (
        id,finding_id,status,attempts,next_attempt_at,lease_until,last_error,created_at,sent_at
    ) SELECT id,finding_id,'sending',1,'2000-01-01T00:00:00Z',lease_until,NULL,created_at,NULL
    FROM outbox_v15""")
    connection.execute("DROP TABLE outbox_v15")
    connection.execute("DROP TABLE deep_scan_rotation")
    connection.execute("PRAGMA user_version = 14")
    connection.commit()
    connection.close()

    with SQLiteStore(path) as migrated:
        row = migrated.connection.execute(
            "SELECT finding_id,status,message_id FROM telegram_notification_outbox"
        ).fetchone()
        assert tuple(row) == (finding_id, "uncertain", None)
        assert migrated.connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0] == 2
        assert migrated.connection.execute(
            "SELECT COUNT(*) FROM price_observations WHERE duration_min_days IS NULL"
        ).fetchone()[0] == 2
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_incomplete_scan_is_recorded_but_does_not_change_destination_inventory(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        original = datetime(2026, 9, 1, tzinfo=timezone.utc)
        later = datetime(2026, 9, 2, tzinfo=timezone.utc)
        seeded = store.record_scan(
            1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]), finished_at=original
        )
        destination_id = seeded.new_destinations[0]["destination_id"]

        failed = store.record_scan(
            1,
            2,
            _scan([_offer("Новый город", "MOW0209NEW1", 5000)], complete=False),
            finished_at=later,
        )

        assert failed.complete is False
        assert failed.outdated_offers == 0
        assert failed.new_destinations == ()
        destinations = store.list_destinations(1, 2, "MOW")
        assert len(destinations) == 1
        assert destinations[0].destination_id == destination_id
        assert destinations[0].last_seen == "2026-09-01T00:00:00Z"
        assert store.get_scan_offers(1, 2, failed.scan_id) == []

        history = store.list_scans(1, 2, limit=10)
        assert [scan.complete for scan in history] == [False, True]
        assert history[0].warnings == ["collector timeout"]
        assert history[1].cards_found == 1
        assert history[0].scan_kind == "regular"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM price_observations WHERE scan_id = ?", (failed.scan_id,)
        ).fetchone()[0] == 0


def test_failed_scan_write_rolls_back_history_destinations_and_offers(tmp_path):
    broken_offer = _offer("Сочи", "MOW0509AER1", 3500)
    broken_offer["airports"] = [object()]
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        with pytest.raises(TypeError):
            store.record_scan(1, 2, _scan([broken_offer]))

        assert store.list_scans(1, 2) == []
        assert store.list_destinations(1, 2, "MOW") == []


def test_latest_scan_offers_are_scoped_to_origin(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.record_scan(1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]))
        led_offer = _offer("Тбилиси", "LED0109TBS1", 4100)
        led_offer["origin"] = "LED"
        led_scan = _scan([led_offer])
        led_scan["origin"] = "LED"
        store.record_scan(1, 2, led_scan)

        offers = store.get_latest_scan_offers(1, 2, "LED")

        assert len(offers) == 1
        assert offers[0]["price"] == 4100
        assert store.get_latest_scan_offers(1, 2, "MOW")[0]["price"] == 4200


def test_offer_transfer_cities_are_persisted_and_loaded(tmp_path):
    offer = _offer("Шанхай", "MOW0109PVG1", 26120)
    offer["transfers"] = ["Чэнду"]
    offer["transfer_durations"] = ["4 ч"]
    offer["flight_times"] = ["08:00", "14:00"]
    offer["airline_by_leg"] = [["Carrier A"], ["Carrier B"]]
    offer["arrival_day_offsets"] = [1]
    offer["repeat_registration_by_leg"] = [True, False]
    offer["airline_logos"] = [{"name": "Sichuan Airlines", "url": "https://pics.avs.io/200/200/3U.png"}]
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        saved = store.record_scan(1, 2, _scan([offer]))
        loaded = store.get_scan_offers(1, 2, saved.scan_id)

        assert loaded[0]["transfers"] == ["Чэнду"]
        assert loaded[0]["transfer_durations"] == ["4 ч"]
        assert loaded[0]["flight_times"] == ["08:00", "14:00"]
        assert loaded[0]["airline_by_leg"] == [["Carrier A"], ["Carrier B"]]
        assert loaded[0]["arrival_day_offsets"] == [1]
        assert loaded[0]["repeat_registration_by_leg"] == [True, False]
        assert loaded[0]["airline_logos"][0]["name"] == "Sichuan Airlines"


def test_schema_v2_without_offers_table_migrates_cleanly(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    with SQLiteStore(path) as store:
        columns = {
            row["name"]
            for row in store.connection.execute("PRAGMA table_info(offers)").fetchall()
        }
        assert "transfers_json" in columns
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_scan_kind_is_saved_separately_by_origin(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        regular = store.record_scan(1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]))
        deep_result = _scan([_offer("Стамбул", "LED0109IST1", 8100)])
        deep_result["origin"] = "LED"
        deep_result["offers"][0]["origin"] = "LED"
        deep = store.record_scan(1, 2, deep_result, scan_kind="deep")
        summaries = store.list_scans(1, 2)

        assert [(item.origin, item.scan_kind) for item in summaries] == [("LED", "deep"), ("MOW", "regular")]
        assert store.get_scan_offers(1, 2, regular.scan_id)[0]["origin"] == "MOW"
        assert store.get_scan_offers(1, 2, deep.scan_id)[0]["origin"] == "LED"


def test_archived_destination_is_hidden_from_default_list_and_can_be_restored(tmp_path):
    with SQLiteStore(tmp_path / "archive.sqlite3") as store:
        saved = store.record_scan(1, 2, _scan([_offer("Махачкала", "MOW0109MCX1", 4200)]))
        destination_id = saved.new_destinations[0]["destination_id"]

        assert store.set_destination_archived(1, 2, destination_id, True)
        assert store.list_destinations(1, 2, "MOW") == []
        archived = store.list_destinations(1, 2, "MOW", include_archived=True)
        assert len(archived) == 1
        assert archived[0].archived is True
        assert store.set_destination_archived(1, 2, destination_id, False)
        restored = store.list_destinations(1, 2, "MOW")
        assert len(restored) == 1
        assert restored[0].archived is False


def test_schema_v8_adds_destination_archive_flag(tmp_path):
    path = tmp_path / "v8.sqlite3"
    with SQLiteStore(path) as store:
        store.connection.execute("ALTER TABLE destinations DROP COLUMN archived")
        store.connection.execute("PRAGMA user_version = 8")
        store.connection.commit()

    with SQLiteStore(path) as store:
        columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(destinations)")}
        assert "archived" in columns
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v9_upgrades_legacy_short_monitor_intervals_to_ninety_minutes(tmp_path):
    path = tmp_path / "v9-interval.sqlite3"
    with SQLiteStore(path) as store:
        store.save_settings(MonitorSettings(1, 2, "MOW", "OW", 15))
        store.save_settings(MonitorSettings(1, 2, "LED", "OW", 60))
        store.connection.execute("UPDATE monitor_settings SET interval_minutes = 15 WHERE origin = 'MOW'")
        store.connection.execute("PRAGMA user_version = 9")
        store.connection.commit()

    with SQLiteStore(path) as store:
        settings = {item.origin: item.interval_minutes for item in store.list_settings(1, 2)}
        assert settings == {"MOW": 90, "LED": 60}
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v10_adds_disappeared_destinations_snapshot(tmp_path):
    path = tmp_path / "v10-disappeared.sqlite3"
    with SQLiteStore(path) as store:
        store.connection.execute("ALTER TABLE scan_history DROP COLUMN disappeared_destinations_json")
        store.connection.execute("PRAGMA user_version = 10")
        store.connection.commit()

    with SQLiteStore(path) as store:
        columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(scan_history)")}
        assert "disappeared_destinations_json" in columns
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v16_moves_old_default_monitor_cadence_to_ninety_minutes(tmp_path):
    path = tmp_path / "v16-monitor-cadence.sqlite3"
    with SQLiteStore(path) as store:
        store.save_settings(MonitorSettings(1, 2, "MOW", "OW", 30))
        store.save_settings(MonitorSettings(1, 2, "LED", "OW", 60))
        store.connection.execute("PRAGMA user_version = 16")
        store.connection.commit()

    with SQLiteStore(path) as store:
        settings = {item.origin: item.interval_minutes for item in store.list_settings(1, 2)}
        assert settings == {"MOW": 90, "LED": 60}
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v4_adds_partial_flag_with_false_for_existing_scans(tmp_path):
    path = tmp_path / "v4.sqlite3"
    with SQLiteStore(path) as store:
        store.record_scan(1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]))
        store.connection.execute("ALTER TABLE scan_history DROP COLUMN partial")
        store.connection.execute("PRAGMA user_version = 4")
        store.connection.commit()

    with SQLiteStore(path) as store:
        summary = store.list_scans(1, 2)[0]
        assert summary.partial is False
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v3_marks_legacy_scans_regular_and_nonpartial(tmp_path):
    path = tmp_path / "v3.sqlite3"
    with SQLiteStore(path) as store:
        store.record_scan(1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]))
        store.connection.execute("ALTER TABLE scan_history DROP COLUMN partial")
        store.connection.execute("ALTER TABLE scan_history DROP COLUMN scan_kind")
        store.connection.execute("PRAGMA user_version = 3")
        store.connection.commit()

    with SQLiteStore(path) as store:
        summary = store.list_scans(1, 2)[0]
        assert summary.scan_kind == "regular"
        assert summary.partial is False
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v5_adds_optional_flight_display_fields(tmp_path):
    path = tmp_path / "v5.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]))
        for column in ("flight_times_json", "airline_logos_json", "transfer_durations_json"):
            store.connection.execute(f"ALTER TABLE offers DROP COLUMN {column}")
        store.connection.execute("PRAGMA user_version = 5")
        store.connection.commit()

    with SQLiteStore(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        offer = store.get_scan_offers(1, 2, saved.scan_id)[0]
        assert offer["flight_times"] is None
        assert offer["airline_logos"] is None
        assert offer["transfer_durations"] is None


def test_schema_v6_adds_persisted_outdated_offer_count(tmp_path):
    path = tmp_path / "v6.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(1, 2, _scan([_offer("Тбилиси", "MOW0109TBS1", 4200)]))
        store.connection.execute("ALTER TABLE scan_history DROP COLUMN outdated_offers")
        store.connection.execute("PRAGMA user_version = 6")
        store.connection.commit()

    with SQLiteStore(path) as store:
        row = store.connection.execute(
            "SELECT outdated_offers FROM scan_history WHERE id = ?", (saved.scan_id,)
        ).fetchone()
        assert row["outdated_offers"] == 0
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
