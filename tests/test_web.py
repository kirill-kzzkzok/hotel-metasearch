import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
from fastapi import HTTPException
import pytest

from aviasales_collector.database import SQLiteStore
from aviasales_collector.web import _airports_by_leg, create_app


def _scan(origin: str, name: str, price: int, kind: str = "regular"):
    return {
        "origin": origin,
        "trip_type": "OW",
        "complete": True,
        "cards_found": 1,
        "dom_hot_ticket_count": 1,
        "unique_destinations": 1,
        "offers": [{
            "origin": origin,
            "destination": name,
            "destination_route": "opaque-token-is-not-a-route",
            "price": price,
            "old_price": price + 100,
            "currency": "RUB",
            "departure_date_raw": "5 окт",
            "airline": ["Carrier A", "Carrier B"],
            "airline_logos": [{"name": "Carrier A", "url": "https://pics.avs.io/logo.png"}],
            "airports": [{"code": "LED", "name": "Пулково"}, {"code": "PVG", "name": "Пудун"}],
            "stops": 1,
            "flight_times": ["08:10", "11:45"],
            "flight_duration": "10 ч",
            "transfers": ["Чэнду"],
            "transfer_durations": ["4 ч"],
            "source_url": "https://www.aviasales.ru/ticket",
            "warnings": ["selector fallback"],
        }],
        "warnings": [],
        "scan_kind": kind,
    }


def test_results_page_and_api_are_scoped_and_paginated(tmp_path):
    path = tmp_path / "web.sqlite3"
    with SQLiteStore(path) as store:
        first = store.record_scan(10, -10, _scan("MOW", "Тбилиси", 4000))
        assert store.set_destination_active(10, -10, first.new_destinations[0]["destination_id"], True)
        store.record_scan(10, -10, _scan("MOW", "Тбилиси", 3900))
        store.record_scan(10, -10, _scan("MOW", "Тбилиси", 3800))
        store.record_scan(10, -10, _scan("LED", "Сеул", 7000))
        store.record_scan(10, -10, _scan("LED", "Шанхай", 8000), scan_kind="deep")
        store.record_scan(99, -99, _scan("MOW", "Private city", 1000))

    async def verify_api():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            index = await client.get("/")
            assert index.status_code == 200
            assert "Санкт-Петербург" in index.text
            assert index.headers["content-security-policy"].startswith("default-src 'self'")
            assert index.headers["x-content-type-options"] == "nosniff"
            assert index.headers["x-frame-options"] == "DENY"
            assert index.headers["cache-control"] == "no-store"
            latest_page = (await client.get("/api/scans?origin=MOW&kind=all&limit=1&offset=0")).json()
            assert latest_page["total"] == 1
            assert latest_page["checks_recorded"] == 5
            assert latest_page["scans"][0]["offers"][0]["price"] == 3800
            assert latest_page["scans"][0]["last_checked_at"] == latest_page["scans"][0]["finished_at"]
            assert latest_page["scans"][0]["offers"][0]["previous_price"] == 3900
            assert latest_page["scans"][0]["offers"][0]["previous_currency"] == "RUB"
            latest_offer_id = latest_page["scans"][0]["offers"][0]["offer_id"]
            price_history = (await client.get(
                f"/api/price-history?origin=MOW&offer_id={latest_offer_id}"
            )).json()
            assert [point["price"] for point in price_history["points"]] == [4000, 3900, 3800]
            assert (await client.get("/api/price-history?origin=LED&offer_id="
                                     f"{latest_offer_id}")).status_code == 404
            older_page = (await client.get("/api/scans?origin=MOW&kind=all&view=history&limit=1&offset=1")).json()
            oldest_page = (await client.get("/api/scans?origin=MOW&kind=all&view=history&limit=1&offset=2")).json()
            assert older_page["total"] == 3
            assert older_page["scans"][0]["scan_id"] != oldest_page["scans"][0]["scan_id"]
            filtered = (await client.get("/api/scans?origin=MOW&kind=all&view=history&q=тбилиси&active_only=true")).json()
            assert filtered["total"] == 3
            assert all(offer["active"] for scan in filtered["scans"] for offer in scan["offers"])
            visible = (await client.get("/api/scans?origin=LED&kind=deep")).json()
            assert visible["scans"][0]["offers"][0]["transfers"] == ["Чэнду"]
            assert [point["code"] for point in visible["scans"][0]["offers"][0]["airports_by_leg"][0]] == ["LED", "PVG"]
            assert visible["scans"][0]["scan_kind"] == "deep"
            assert visible["scans"][0]["offers"][0]["flight_times"] == ["08:10", "11:45"]
            assert visible["scans"][0]["offers"][0]["airline_logos"][0]["name"] == "Carrier A"
            assert visible["scans"][0]["offers"][0]["transfer_durations"] == ["4 ч"]
            assert (await client.get("/api/scans?origin=MOW&kind=deep")).json()["total"] == 0
            deep_history = (await client.get("/api/scans?origin=LED&kind=deep&view=history&limit=1")).json()
            assert deep_history["total"] == 1
            assert deep_history["scans"][0]["scan_kind"] == "deep"
            destinations = (await client.get("/api/destinations?origin=MOW")).json()["destinations"]
            assert destinations[0]["destination"] == "Тбилиси"
            assert destinations[0]["last_known_price"] == 3800
            destination_id = destinations[0]["destination_id"]
            archive_headers = {"Origin": "http://127.0.0.1:8765"}
            archived = await client.post(
                f"/api/destinations/archive?origin=MOW&destination_id={destination_id}",
                headers=archive_headers,
                json={"archived": True},
            )
            assert archived.status_code == 200
            assert archived.json()["archived"] is True
            assert (await client.get("/api/destinations?origin=MOW")).json()["destinations"] == []
            archive_items = (await client.get("/api/destinations?origin=MOW&archived=true")).json()["destinations"]
            assert archive_items[0]["destination"] == "Тбилиси"
            regular_after_archive = (await client.get("/api/scans?origin=MOW&kind=regular")).json()
            assert all(not scan["offers"] for scan in regular_after_archive["scans"])
            missing_origin_header = await client.post(
                f"/api/destinations/archive?origin=MOW&destination_id={destination_id}",
                json={"archived": False},
            )
            assert missing_origin_header.status_code == 403
            restored = await client.post(
                f"/api/destinations/archive?origin=MOW&destination_id={destination_id}",
                headers=archive_headers,
                json={"archived": False},
            )
            assert restored.status_code == 200
            wrong_origin = await client.post(
                f"/api/destinations/archive?origin=LED&destination_id={destination_id}",
                headers=archive_headers,
                json={"archived": True},
            )
            assert wrong_origin.status_code == 404
            notifications = (await client.get("/api/notifications?origin=LED")).json()["items"]
            assert notifications[0]["new_destinations"] == ["Шанхай"]
            assert notifications[0]["id"] > 0
            assert notifications[0]["outdated_offers"] == 0
            assert notifications[0]["disappeared_destinations"] == []
            all_scans = (await client.get("/api/scans?origin=ALL&kind=all")).json()
            assert all_scans["total"] == 3
            assert {scan["origin"] for scan in all_scans["scans"]} == {"MOW", "LED"}
            assert all(
                offer["origin"] == scan["origin"]
                for scan in all_scans["scans"] for offer in scan["offers"]
            )
            hotels_page = await client.get("/hotels")
            assert hotels_page.status_code == 200
            assert 'id="load-more" class="load-more-sentinel" hidden' in hotels_page.text
            assert 'id="load-more" class="load-more-sentinel" style=' not in hotels_page.text
            all_destinations = (await client.get("/api/destinations?origin=ALL")).json()["destinations"]
            assert {item["origin"] for item in all_destinations} == {"MOW", "LED"}
            all_notifications = (await client.get("/api/notifications?origin=ALL")).json()["items"]
            assert {item["origin"] for item in all_notifications} == {"MOW", "LED"}
            restarted_app = create_app(path, frozenset({10}))
            restarted_transport = httpx.ASGITransport(app=restarted_app)
            async with httpx.AsyncClient(transport=restarted_transport, base_url="http://local") as restarted_client:
                persisted = (await restarted_client.get("/api/scans?origin=ALL&kind=all")).json()
                assert persisted["checks_recorded"] == 5
            assert (await client.get("/api/destinations?origin=XXXX")).status_code == 422
            assert (await client.get("/api/scans?origin=XXXX&kind=regular")).status_code == 422
            assert (await client.get("/assets/app.js")).status_code == 200

    asyncio.run(verify_api())


def test_all_origin_search_returns_matching_destination_for_every_origin(tmp_path):
    path = tmp_path / "all-origins.sqlite3"
    destination = "\u041d\u044f\u0447\u0430\u043d\u0433"
    with SQLiteStore(path) as store:
        store.record_scan(10, -10, _scan("MOW", destination, 36_911))
        store.record_scan(10, -10, _scan("LED", destination, 82_797))

    async def verify_api():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(
                "/api/scans",
                params={"origin": "ALL", "kind": "all", "q": destination},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 2
            assert {
                (scan["origin"], offer["destination"])
                for scan in data["scans"] for offer in scan["offers"]
            } == {("MOW", destination), ("LED", destination)}

    asyncio.run(verify_api())


def test_destinations_api_hides_routes_missing_from_latest_complete_scan(tmp_path):
    path = tmp_path / "current-destinations.sqlite3"
    with SQLiteStore(path) as store:
        store.record_scan(10, -10, _scan("MOW", "Zanzibar", 50_000))
        store.record_scan(10, -10, _scan("MOW", "Tbilisi", 4_200))

    async def verify_api():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get("/api/destinations?origin=MOW")
            assert response.status_code == 200
            assert [item["destination"] for item in response.json()["destinations"]] == ["Tbilisi"]

    asyncio.run(verify_api())


def test_destination_active_api_updates_shared_tracking_state(tmp_path):
    path = tmp_path / "active-destination.sqlite3"
    with SQLiteStore(path) as store:
        saved = store.record_scan(10, -10, _scan("MOW", "Тбилиси", 4_200))
        destination_id = saved.new_destinations[0]["destination_id"]

    async def verify_api():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        headers = {"Origin": "http://127.0.0.1:8765"}
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            url = f"/api/destinations/active?origin=MOW&destination_id={destination_id}"
            assert (await client.post(url, headers=headers, json={"active": True})).json() == {
                "destination_id": destination_id, "active": True,
            }
            assert (await client.get("/api/destinations?origin=MOW")).json()["destinations"][0]["active"]
            assert (await client.post(url, headers=headers, json={"active": "yes"})).status_code == 422
            assert (await client.post(
                f"/api/destinations/active?origin=LED&destination_id={destination_id}",
                headers=headers, json={"active": False},
            )).status_code == 404

    asyncio.run(verify_api())


def test_round_trip_airport_codes_survive_mismatched_stop_counts():
    airports = [
        {"code": "SVO", "name": "Шереметьево"},
        {"code": "AYT", "name": "Анталья"},
        {"code": "AYT", "name": "Анталья"},
        {"code": "VKO", "name": "Внуково"},
    ]
    split = _airports_by_leg(airports, [0, 1], "RT")
    assert [[point["code"] for point in leg] for leg in split] == [
        ["SVO", "AYT"], ["AYT", "VKO"]
    ]
    three_points = _airports_by_leg(airports[:2] + airports[-1:], [1, 1], "RT")
    assert [[point["code"] for point in leg] for leg in three_points] == [
        ["SVO", "AYT"], ["AYT", "VKO"]
    ]
    malformed = _airports_by_leg([{"code": "116"}, *airports[:2]], [1, 1], "RT")
    assert [[point["code"] for point in leg] for leg in malformed] == [
        ["", "SVO"], ["SVO", "AYT"]
    ]


def test_verified_ticket_itinerary_overrides_noisy_airport_list():
    noisy_airports = [
        {"code": "CAT", "name": "S"},
        {"code": "VKO", "name": "Внуково"},
        {"code": "ALA", "name": "Алматы"},
        {"code": "CAT", "name": "S"},
        {"code": "ALA", "name": "Алматы"},
        {"code": "VKO", "name": "Внуково"},
    ]
    identity = {
        "route_valid": True,
        "outbound_segments": [{"origin": "VKO", "destination": "ALA"}],
        "return_segments": [{"origin": "ALA", "destination": "VKO"}],
    }
    split = _airports_by_leg(noisy_airports, [0, 0], "RT", identity)
    assert [[point["code"] for point in leg] for leg in split] == [
        ["VKO", "ALA"], ["ALA", "VKO"]
    ]


def test_archive_hides_destination_from_regular_and_deep_history(tmp_path):
    path = tmp_path / "archive-deep.sqlite3"
    with SQLiteStore(path) as store:
        regular = store.record_scan(10, -10, _scan("MOW", "Махачкала", 4500))
        destination_id = regular.new_destinations[0]["destination_id"]
        store.record_scan(10, -10, _scan("MOW", "Махачкала", 4400, kind="deep"), scan_kind="deep")

    async def verify_archive():
        app = create_app(path, frozenset({10}))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            archived = await client.post(
                f"/api/destinations/archive?origin=MOW&destination_id={destination_id}",
                headers={"Origin": "http://localhost:8765"},
                json={"archived": True},
            )
            assert archived.status_code == 200
            for kind in ("regular", "deep"):
                result = (await client.get(f"/api/scans?origin=MOW&kind={kind}")).json()
                assert result["total"] == 1
                assert result["scans"][0]["offers"] == []

    asyncio.run(verify_archive())


def test_archive_endpoint_closes_queued_price_alert(tmp_path):
    path = tmp_path / "archive-queued-alert.sqlite3"
    with SQLiteStore(path) as store:
        baseline = store.record_scan(10, -10, _scan("MOW", "Махачкала", 10000))
        destination_id = baseline.new_destinations[0]["destination_id"]
        finding = store.record_scan(
            10, -10, _scan("MOW", "Махачкала", 7000), track_price_changes=True
        ).price_findings[0]
        assert finding["notification_queued"] is True

    async def verify_archive():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            archived = await client.post(
                f"/api/destinations/archive?origin=MOW&destination_id={destination_id}",
                headers={"Origin": "http://localhost:8765"},
                json={"archived": True},
            )
            assert archived.status_code == 200

    asyncio.run(verify_archive())
    with SQLiteStore(path) as store:
        outbox = store.connection.execute(
            "SELECT status, last_error FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()
        assert outbox["status"] == "failed"
        assert "archived" in outbox["last_error"]


def test_current_deep_results_include_partial_cards_and_ignore_empty_failed_passes(tmp_path):
    path = tmp_path / "partial-deep-current.sqlite3"
    partial = _scan("MOW", "Тбилиси", 3900, kind="deep")
    partial["complete"] = False
    partial["partial"] = True
    partial["warnings"] = ["one destination timed out"]
    failed = _scan("MOW", "Тбилиси", 3800, kind="deep")
    failed["complete"] = False
    failed["partial"] = True
    failed["offers"] = []
    with SQLiteStore(path) as store:
        saved = store.record_scan(10, -10, partial, scan_kind="deep")
        store.record_scan(10, -10, failed, scan_kind="deep")

    async def verify_current_deep():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            result = (await client.get("/api/scans?origin=MOW&kind=deep")).json()
            assert result["total"] == 1
            assert result["scans"][0]["scan_id"] == saved.scan_id
            assert result["scans"][0]["partial"] is True
            assert result["scans"][0]["offers"][0]["price"] == 3900

    asyncio.run(verify_current_deep())


def test_notifications_omit_scans_older_than_three_days_without_deleting_history(tmp_path):
    path = tmp_path / "notification-age.sqlite3"
    now = datetime.now(timezone.utc)
    with SQLiteStore(path) as store:
        old = store.record_scan(
            10, -10, _scan("MOW", "Старое направление", 4200),
            started_at=now - timedelta(days=4), finished_at=now - timedelta(days=4),
        )
        three_day_window = store.record_scan(
            10, -10, _scan("MOW", "Направление за два дня", 4150),
            started_at=now - timedelta(days=2), finished_at=now - timedelta(days=2),
        )
        recent = store.record_scan(
            10, -10, _scan("MOW", "Новое направление", 4100),
            started_at=now - timedelta(hours=1), finished_at=now - timedelta(minutes=30),
        )

    async def verify_notifications():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get("/api/notifications?origin=MOW")
            assert [item["id"] for item in response.json()["items"]] == [recent.scan_id, three_day_window.scan_id]
        with SQLiteStore(path) as store:
            assert {item.scan_id for item in store.list_scans(10, -10)} == {
                old.scan_id, three_day_window.scan_id, recent.scan_id,
            }

    asyncio.run(verify_notifications())


def test_deep_notifications_explain_progress_and_failed_destination_names(tmp_path):
    path = tmp_path / "deep-notification-progress.sqlite3"
    partial = _scan("MOW", "Бангкок", 35_000, kind="deep")
    partial["partial"] = True
    partial["warnings"] = [
        "Фукуок: CollectorError: destination card not found: Фукуок",
        "Маэ: CollectorError: destination card not found: Маэ",
        "deep scan completed partially: 7/9 destinations",
    ]
    with SQLiteStore(path) as store:
        store.record_scan(10, -10, partial, scan_kind="deep")

    async def verify():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            item = (await client.get("/api/notifications?origin=MOW")).json()["items"][0]
            assert item["deep_progress"] == {"scanned": 7, "requested": 9}
            assert item["deep_issues"] == [
                {"destination": "Фукуок", "reason": "карточка не найдена"},
                {"destination": "Маэ", "reason": "карточка не найдена"},
            ]

    asyncio.run(verify())


def test_price_history_tracks_route_minimum_across_departure_dates_and_scan_kinds(tmp_path):
    path = tmp_path / "route-price-history.sqlite3"
    with SQLiteStore(path) as store:
        for kind, price, departure in (
            ("regular", 40_000, "2026-10-10"),
            ("regular", 37_000, "2026-10-17"),
            ("deep", 36_000, "2026-10-24"),
        ):
            scan = _scan("LED", "Бангкок", price, kind=kind)
            scan["offers"][0]["departure_date"] = departure
            store.record_scan(10, -10, scan, scan_kind=kind)
        latest_id = store.list_scans(10, -10, limit=1)[0].scan_id
        latest_offer_id = store.get_scan_offers(10, -10, latest_id)[0]["offer_id"]

    async def verify():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(f"/api/price-history?origin=LED&offer_id={latest_offer_id}")
            assert [point["price"] for point in response.json()["points"]] == [40_000, 37_000, 36_000]

    asyncio.run(verify())


def test_rt_price_history_chart_includes_all_duration_contexts_and_legacy_scans(tmp_path):
    path = tmp_path / "rt-context-price-history.sqlite3"
    with SQLiteStore(path) as store:
        for context, prices in (((None, None), (50000,)), ((3, 11), (40000, 37000)), ((7, 10), (29000, 27000))):
            for price in prices:
                scan = _scan("LED", "Бангкок", price)
                scan["trip_type"] = "RT"
                if context != (None, None):
                    scan["duration_min_days"], scan["duration_max_days"] = context
                scan["offers"][0]["return_date"] = "2026-10-20"
                store.record_scan(10, -10, scan)
        latest = store.list_scans(10, -10, limit=1)[0]
        latest_offer_id = store.get_scan_offers(10, -10, latest.scan_id)[0]["offer_id"]

    async def verify():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(f"/api/price-history?origin=LED&offer_id={latest_offer_id}")
            assert [point["price"] for point in response.json()["points"]] == [50000, 40000, 37000, 29000, 27000]

    asyncio.run(verify())


def test_notifications_include_disappeared_destinations_only_for_complete_regular_scans(tmp_path):
    path = tmp_path / "disappeared-notifications.sqlite3"
    with SQLiteStore(path) as store:
        store.record_scan(10, -10, {
            **_scan("MOW", "Стамбул", 8000),
            "offers": [
                _scan("MOW", "Стамбул", 8000)["offers"][0],
                _scan("MOW", "Тбилиси", 4000)["offers"][0],
            ],
            "unique_destinations": 2,
        })
        current = store.record_scan(10, -10, _scan("MOW", "Тбилиси", 3900))
        assert current.disappeared_destinations == ("Стамбул",)
        store.record_scan(10, -10, _scan("MOW", "Дубай", 9000, kind="deep"), scan_kind="deep")
        partial = _scan("MOW", "Париж", 12000)
        partial["partial"] = True
        store.record_scan(10, -10, partial)

    async def verify():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get("/api/notifications?origin=MOW")
            items = response.json()["items"]
            assert items[0]["scan_kind"] == "regular"
            assert items[0]["disappeared_destinations"] == []
            assert items[0]["partial"] is True
            assert items[1]["scan_kind"] == "deep"
            assert items[1]["disappeared_destinations"] == []
            assert items[2]["disappeared_destinations"] == ["Стамбул"]

    asyncio.run(verify())


def test_notifications_show_returned_destinations_but_hide_archived_ones(tmp_path):
    path = tmp_path / "returned-notifications.sqlite3"
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def timestamp(value):
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    def scan_with_destinations(*names):
        offers = [
            _scan("MOW", name, 5000 + index)["offers"][0]
            for index, name in enumerate(names)
        ]
        result = _scan("MOW", names[0], 5000)
        result["offers"] = offers
        result["unique_destinations"] = len(offers)
        result["cards_found"] = len(offers)
        result["dom_hot_ticket_count"] = len(offers)
        return result

    with SQLiteStore(path) as store:
        first = store.record_scan(
            10, -10, scan_with_destinations("Направление A", "Направление B", "Направление C"),
            started_at=timestamp(now - timedelta(hours=3)),
            finished_at=timestamp(now - timedelta(hours=3) + timedelta(minutes=1)),
        )
        archived_id = next(
            item["destination_id"] for item in first.new_destinations
            if item["destination"] == "Направление A"
        )
        store.record_scan(
            10, -10, scan_with_destinations("Направление B"),
            started_at=timestamp(now - timedelta(hours=2)),
            finished_at=timestamp(now - timedelta(hours=2) + timedelta(minutes=1)),
        )
        assert store.set_destination_archived(10, -10, archived_id, True)
        current = store.record_scan(
            10, -10,
            scan_with_destinations("Направление A", "Направление B", "Направление C", "Направление D"),
            started_at=timestamp(now - timedelta(hours=1)),
            finished_at=timestamp(now - timedelta(hours=1) + timedelta(minutes=1)),
        )
        assert [item["destination"] for item in current.new_destinations] == ["Направление D"]

    async def verify():
        transport = httpx.ASGITransport(app=create_app(path, frozenset({10})))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get("/api/notifications?origin=MOW")
            item = response.json()["items"][0]
            assert item["new_destinations"] == ["Направление D"]
            assert item["returned_destinations"] == ["Направление C"]
            assert "Направление A" not in item["returned_destinations"]

    asyncio.run(verify())


def test_web_deep_scan_action_checks_origin_and_reports_conflicts(tmp_path):
    calls = []

    async def on_deep_scan(origin):
        calls.append(origin)
        return origin == "MOW"

    async def verify_action():
        app = create_app(tmp_path / "unused.sqlite3", frozenset({10}), on_deep_scan)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            started = await client.post(
                "/api/deep-scan?origin=MOW", headers={"Origin": "http://127.0.0.1:8765"}
            )
            assert started.status_code == 200
            assert started.json() == {"origin": "MOW", "started": True}
            missing_origin = await client.post("/api/deep-scan?origin=MOW")
            assert missing_origin.status_code == 403
            conflict = await client.post(
                "/api/deep-scan?origin=LED", headers={"Origin": "http://127.0.0.1:8765"}
            )
            assert conflict.status_code == 409
            forbidden = await client.post(
                "/api/deep-scan?origin=MOW", headers={"Origin": "http://example.com"}
            )
            assert forbidden.status_code == 403
            assert calls == ["MOW", "LED"]

    asyncio.run(verify_action())


def test_web_city_search_saves_monitor_settings_for_scheduler(tmp_path, monkeypatch):
    from aviasales_collector import web

    monkeypatch.setattr(
        web, "_search_cities",
        lambda term: ({"code": "MSQ", "name": "Минск", "country": "Беларусь"},),
    )
    path = tmp_path / "city-settings.sqlite3"
    with SQLiteStore(path):
        pass

    async def verify_settings():
        app = create_app(path, frozenset({10}))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            suggestions = await client.get("/api/cities?q=Мин")
            assert suggestions.json()["cities"][0]["code"] == "MSQ"
            saved = await client.post(
                "/api/settings",
                headers={"Origin": "http://127.0.0.1:8765"},
                json={
                    "origin": "MSQ", "city_name": "Минск", "trip_type": "RT",
                    "duration_min_days": 4, "duration_max_days": 12,
                    "interval_minutes": 30,
                },
            )
            assert saved.status_code == 200
            invalid_interval = await client.post(
                "/api/settings",
                headers={"Origin": "http://127.0.0.1:8765"},
                json={
                    "origin": "MSQ", "city_name": "Минск", "trip_type": "OW",
                    "interval_minutes": 1441,
                },
            )
            assert invalid_interval.status_code == 422
            too_short_interval = await client.post(
                "/api/settings",
                headers={"Origin": "http://127.0.0.1:8765"},
                json={
                    "origin": "MSQ", "city_name": "Минск", "trip_type": "OW",
                    "interval_minutes": 15,
                },
            )
            assert too_short_interval.status_code == 422
            missing_origin = await client.post(
                "/api/settings",
                json={
                    "origin": "MSQ", "city_name": "Минск", "trip_type": "OW",
                    "interval_minutes": 30,
                },
            )
            assert missing_origin.status_code == 403
            configured = (await client.get("/api/settings")).json()["origins"]
            minsk = next(item for item in configured if item["origin"] == "MSQ")
            assert minsk["city_name"] == "Минск"
            assert minsk["interval_minutes"] == 30
            assert (await client.get("/api/scans?origin=MSQ&kind=all")).status_code == 200

    asyncio.run(verify_settings())
    with SQLiteStore(path) as store:
        saved = store.get_settings(10, 10, "MSQ")
        assert saved is not None and saved.trip_type == "RT"
        assert saved.duration_min_days == 4 and saved.duration_max_days == 12


def test_city_lookup_rejects_oversized_upstream_response(monkeypatch):
    from aviasales_collector import web

    class OversizedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, limit):
            return b" " * limit

    monkeypatch.setattr(web, "urlopen", lambda *_args, **_kwargs: OversizedResponse())
    web._search_cities.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            web._search_cities("oversized response test")
        assert error.value.status_code == 502
    finally:
        web._search_cities.cache_clear()


def test_city_lookup_skips_malformed_items_and_non_ascii_codes(monkeypatch):
    from aviasales_collector import web

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            payload = [
                None,
                "malformed",
                {"type": "city", "code": "MSQ", "name": "Минск", "country_name": "Беларусь"},
                {"type": "city", "code": "СПБ", "name": "Некорректный код"},
            ]
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr(web, "urlopen", lambda *_args, **_kwargs: Response())
    web._search_cities.cache_clear()
    try:
        assert web._search_cities("malformed upstream test") == (
            {"code": "MSQ", "name": "Минск", "country": "Беларусь"},
        )
    finally:
        web._search_cities.cache_clear()


def test_card_renderer_uses_only_saved_route_fields_and_safe_links():
    from pathlib import Path

    assets = Path(__file__).parents[1] / "aviasales_collector" / "web_assets"
    app_js = (assets / "app.js").read_text(encoding="utf-8")
    assert "destination_route" not in app_js
    assert 'url.protocol === "https:"' in app_js
    assert "offer.airports_by_leg" in app_js
    assert "old_price" not in app_js
    assert "transfers" in app_js
    assert "price-history-dialog" in app_js
    assert "data-ticket-url" in app_js
    page = (assets / "index.html").read_text(encoding="utf-8")
    assert "История сканов" not in page
    assert "price-history-dialog" in page
    assert 'repeat(2, minmax(0,1fr))' in (assets / "style.css").read_text(encoding="utf-8")


def test_trip_compare_modal_shows_a_safe_link_to_the_found_tripcom_fare():
    from pathlib import Path

    assets = Path(__file__).parents[1] / "aviasales_collector" / "web_assets"
    app_js = (assets / "app.js").read_text(encoding="utf-8")
    styles = (assets / "style.css").read_text(encoding="utf-8")

    assert 'function safeTripcomFlightUrl(value)' in app_js
    assert 'host === "trip.com" || host.endsWith(".trip.com")' in app_js
    assert 'trip?.tripcom_url' in app_js
    assert 'target="_blank" rel="noopener noreferrer"' in app_js
    assert "Открыть найденный тариф на Trip.com" in app_js
    assert ".trip-compare-link" in styles
    assert "tripCompareActiveOfferId" in app_js
    assert "tripCompareStates" in app_js
    assert "content.dataset.offerId !== requestOfferId" in app_js


def test_dashboard_uses_one_card_feed_with_deep_variants_and_compact_origin_headers():
    from pathlib import Path

    assets = Path(__file__).parents[1] / "aviasales_collector" / "web_assets"
    page = (assets / "index.html").read_text(encoding="utf-8")
    app_js = (assets / "app.js").read_text(encoding="utf-8")

    assert "Все карточки" not in page
    assert "Обычные" not in page
    assert "Глубокие" not in page
    assert "state.kind" not in app_js
    assert "Больше вариантов" in app_js
    assert 'id="deep-sort-order"' in page
    assert 'value="price" selected' in page
    assert 'value="duration"' in page
    assert 'document.querySelector("#deep-sort-order").value = "price"' in app_js
    assert 'sort === "duration" ? leftDays : leftPrice' in app_js
    assert "offer.active && deepOffer" in app_js
    assert "Последняя проверка:" in app_js
    assert "Всего проверок:" in app_js
    assert "Проверка завершена" not in app_js
    assert "Обычный ·" not in app_js


def test_airline_logos_have_fallbacks_for_hainan_and_etihad():
    from pathlib import Path

    app_js = (
        Path(__file__).parents[1] / "aviasales_collector" / "web_assets" / "app.js"
    ).read_text(encoding="utf-8")

    assert '["hainan airlines", "HU"]' in app_js
    assert '["etihad airways", "EY"]' in app_js
    assert '["EK", "Emirates"]' in app_js
    assert 'safeUrl(airlineLogoUrl(name), true)' in app_js
    assert 'https://img.avs.io/pics/al_square/${iata}@avif?rs=fit:120:120' in app_js
    assert 'url.searchParams.get("t")' in app_js
    assert 'airlinesFromTicketSource(offer.source_url)' in app_js
    assert 'matchAll(/(?<![A-Z0-9])([A-Z0-9]{2})(?=\\d{10})/g)' in app_js
    assert 'https://img.avs.io/pics/al_square/${iata}@avif?rs=fit:120:120' in app_js


def test_repeat_registration_marks_the_transfer_line_in_red():
    from pathlib import Path

    assets = Path(__file__).parents[1] / "aviasales_collector" / "web_assets"
    app_js = (assets / "app.js").read_text(encoding="utf-8")
    styles = (assets / "style.css").read_text(encoding="utf-8")

    assert "offer.repeat_registration_by_leg" in app_js
    assert 'route-details--repeat-registration' in app_js
    assert ".route-details--repeat-registration { color:" in styles


def test_deep_card_price_is_merged_into_main_card_and_scan_button_is_right_aligned():
    from pathlib import Path

    assets = Path(__file__).parents[1] / "aviasales_collector" / "web_assets"
    app_js = (assets / "app.js").read_text(encoding="utf-8")
    styles = (assets / "style.css").read_text(encoding="utf-8")

    assert "deepPrice < regularPrice" in app_js
    assert "const deepIsCurrent = Boolean(deep)" in app_js
    assert "sameCurrencyDeepOffer = deepIsCurrent && deepOffer" in app_js
    assert "offer.active && deepOffer" in app_js
    assert 'state.origin === "ALL" ? `<strong class="scan-origin">' in app_js
    assert ".deep-scan-row { justify-content: flex-start; }" in styles
    assert ".arrival-day { padding-left: 4px; }" in styles


def test_notification_read_state_is_shared_per_origin_across_all_and_city_tabs():
    from pathlib import Path

    app_js = (
        Path(__file__).parents[1] / "aviasales_collector" / "web_assets" / "app.js"
    ).read_text(encoding="utf-8")

    assert "function notificationReadKey(origin)" in app_js
    assert "function notificationReadId(origin)" in app_js
    assert "items.filter(item => Number(item.id) > notificationReadId(item.origin))" in app_js
    assert 'data-origin="${esc(item.origin)}"' in app_js
    assert 'localStorage.setItem(notificationReadKey(origin), String(latest))' in app_js
    assert 'if (state.origin === "ALL" && latestByOrigin.size)' in app_js
