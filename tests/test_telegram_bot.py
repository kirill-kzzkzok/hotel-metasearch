from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from dataclasses import replace
import os
import subprocess
import sys

import pytest
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

from aviasales_collector.database import MonitorSettings, SQLiteStore
from telethon.errors import RPCError
from aviasales_collector.deep_scan import DeepScanOutcome
from aviasales_collector.scheduler import ScanDisposition
from aviasales_collector.scan_service import ScanOutcome, scan_and_persist
from aviasales_collector import telegram_bot as telegram_bot_module
from aviasales_collector.telegram_bot import (
    BotInstanceLock,
    TelegramBot,
    TelegramBotConfig,
    create_telegram_client,
)


def _env(secret="0123456789abcdef0123456789abcdef"):
    return {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "api-hash",
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_ALLOWED_USER_IDS": "822906320",
        "TG_WS_PROXY_SECRET": secret,
    }


def test_bot_config_requires_mtproxy_secret_and_normalizes_raw_secret():
    config = TelegramBotConfig.from_env(_env())

    assert config.proxy_secret == "dd0123456789abcdef0123456789abcdef"
    assert config.allowed_user_ids == frozenset({822906320})
    assert config.proxy_host == "127.0.0.1"
    assert config.proxy_port == 1443
    assert config.allowed_chat_ids == frozenset()
    assert config.deep_scan_interval_minutes == 360
    assert config.price_drop_threshold_percent == 12.0

    prefixed = TelegramBotConfig.from_env(_env("DD0123456789ABCDEF0123456789ABCDEF"))
    assert prefixed.proxy_secret == config.proxy_secret


def test_bot_config_reads_explicit_group_allowlist():
    config = TelegramBotConfig.from_env({**_env(), "TELEGRAM_ALLOWED_CHAT_IDS": "-100123,-200"})

    assert config.allowed_chat_ids == frozenset({-100123, -200})


def test_bot_config_reads_deep_scan_controls():
    config = TelegramBotConfig.from_env(
        {
            **_env(),
            "PRICE_DROP_THRESHOLD_PERCENT": "12.5",
            "DEEP_SCAN_MAX_DESTINATIONS": "4",
            "DEEP_SCAN_RETRIES": "2",
            "DEEP_SCAN_RETRY_DELAY_SECONDS": "1.5",
            "DEEP_SCAN_INTERVAL_MINUTES": "180",
        }
    )

    assert config.price_drop_threshold_percent == 12.5
    assert config.deep_scan_max_destinations == 4
    assert config.deep_scan_retries == 2
    assert config.deep_scan_retry_delay_seconds == 1.5
    assert config.deep_scan_interval_minutes == 180


def test_bot_instance_lock_rejects_live_owner(tmp_path):
    path = tmp_path / "bot.lock"
    first = BotInstanceLock(path)
    first.acquire()
    second = BotInstanceLock(path)
    with pytest.raises(RuntimeError):
        second.acquire()
    first.release()


def test_bot_instance_lock_rejects_owner_in_another_process(tmp_path):
    path = tmp_path / "bot.lock"
    code = (
        "from aviasales_collector.telegram_bot import BotInstanceLock; "
        "import sys; lock=BotInstanceLock(sys.argv[1]); lock.acquire(); "
        "print('READY', flush=True); sys.stdin.readline(); lock.release()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(RuntimeError):
            BotInstanceLock(path).acquire()
    finally:
        if process.stdin is not None:
            process.stdin.write("\n")
            process.stdin.flush()
        process.wait(timeout=10)
    assert process.returncode == 0
    assert not path.exists()


def test_bot_instance_lock_removes_stale_owner(tmp_path):
    path = tmp_path / "bot.lock"
    path.write_text("999999999", encoding="ascii")
    second = BotInstanceLock(path)
    second.acquire()
    assert path.read_text(encoding="ascii")
    second.release()


def test_bot_instance_lock_removes_corrupt_lock_without_recursion(tmp_path):
    path = tmp_path / "bot.lock"
    path.write_text("not-a-pid", encoding="ascii")
    lock = BotInstanceLock(path)
    lock.acquire()
    assert path.read_text(encoding="ascii") == str(os.getpid())
    lock.release()


def test_bot_instance_lock_release_removes_owned_file(tmp_path):
    path = tmp_path / "bot.lock"
    lock = BotInstanceLock(path)
    lock.acquire()
    assert path.exists()
    lock.release()
    assert not path.exists()


def test_home_lists_each_departure_origin(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, -200, "MOW", "RT", 15, 4, 11))
        store.save_settings(MonitorSettings(100, -200, "LED", "OW", 30))
        bot = TelegramBot(client=object(), store=store, allowed_user_ids=frozenset({100}))

        text = bot._home_text(100, -200)

        assert "Вылет: MOW · Туда-обратно" in text
        assert "Длительность: 4–11 дней" in text
        assert "Вылет: LED · Туда" in text
        assert "Интервал: 30 мин." in text


def test_home_shows_active_destination_count_per_origin(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, -200, "MOW", "OW", 5))
        saved = store.record_scan(
            100,
            -200,
            {
                "origin": "MOW",
                "trip_type": "OW",
                "complete": True,
                "cards_found": 2,
                "dom_hot_ticket_count": 2,
                "unique_destinations": 2,
                "offers": [
                    {"destination": "Тбилиси", "price": 4200, "currency": "RUB"},
                    {"destination": "Стамбул", "price": 8100, "currency": "RUB"},
                ],
                "warnings": [],
            },
        )
        store.set_destination_active(100, -200, saved.new_destinations[0]["destination_id"], True)
        bot = TelegramBot(client=object(), store=store, allowed_user_ids=frozenset({100}))

        assert "Активных направлений: 1" in bot._home_text(100, -200)


def test_destinations_buttons_show_last_known_price(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, -200, "MOW", "OW", 5))
        store.record_scan(
            100,
            -200,
            {
                "origin": "MOW",
                "trip_type": "OW",
                "complete": True,
                "cards_found": 1,
                "dom_hot_ticket_count": 1,
                "unique_destinations": 1,
                "offers": [{"destination": "Тбилиси", "price": 12345, "currency": "RUB"}],
                "warnings": [],
            },
        )
        bot = TelegramBot(client=object(), store=store, allowed_user_ids=frozenset({100}))

        class _DestinationEvent:
            def __init__(self):
                self.messages = []

            async def respond(self, text, buttons=None):
                self.messages.append((text, buttons))

        event = _DestinationEvent()
        asyncio.run(bot._show_destinations(event, (100, -200), 0))

        button = event.messages[0][1][0][0]
        assert "· 12 345 ₽" in button.text


def test_details_screen_compactly_shows_top_offer_fields(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, -200, "MOW", "OW", 5))
        store.record_scan(
            100,
            -200,
            {
                "origin": "MOW",
                "trip_type": "OW",
                "complete": True,
                "cards_found": 1,
                "dom_hot_ticket_count": 1,
                "unique_destinations": 1,
                "offers": [
                    {
                        "destination": "Тбилиси",
                        "price": 12345,
                        "currency": "RUB",
                        "departure_date_raw": "5 окт",
                        "airline": ["Победа"],
                        "stops": 1,
                        "airports": [
                            {"code": "VKO", "name": "Внуково"},
                            {"code": "IST", "name": "Стамбул"},
                            {"code": "TBS", "name": "Тбилиси"},
                        ],
                        "flight_duration": "6 ч в пути",
                    }
                ],
                "warnings": [],
            },
        )
        bot = TelegramBot(client=object(), store=store, allowed_user_ids=frozenset({100}))

        class _DetailsEvent:
            def __init__(self):
                self.messages = []

            async def respond(self, text, buttons=None):
                self.messages.append((text, buttons))

        event = _DetailsEvent()
        asyncio.run(bot._show_details(event, (100, -200), 0))

        text = event.messages[0][0]
        assert "12 345 ₽" in text
        assert "Победа" in text
        assert "1 пересадка (IST Стамбул)" in text
        assert "5 окт" in text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda values: values.pop("TG_WS_PROXY_SECRET"),
        lambda values: values.pop("TELEGRAM_ALLOWED_USER_IDS"),
        lambda values: values.update(TELEGRAM_ALLOWED_USER_IDS="nope"),
        lambda values: values.update(TELEGRAM_ALLOWED_CHAT_IDS="not-a-chat"),
        lambda values: values.update(TELEGRAM_ALLOWED_CHAT_IDS="0"),
        lambda values: values.update(TG_WS_PROXY_SECRET="ee" + "0" * 32),
        lambda values: values.update(TG_WS_PROXY_PORT="70000"),
    ],
)
def test_bot_config_rejects_missing_or_incompatible_proxy_settings(mutate):
    values = _env()
    mutate(values)
    with pytest.raises(ValueError):
        TelegramBotConfig.from_env(values)


def test_telethon_client_is_configured_for_tg_ws_proxy_mtproxy(tmp_path):
    config = TelegramBotConfig.from_env(
        {
            **_env("dd0123456789abcdef0123456789abcdef"),
            "TELETHON_SESSION": str(tmp_path / "bot"),
        }
    )

    client = create_telegram_client(config)

    assert client._connection is ConnectionTcpMTProxyRandomizedIntermediate
    assert client._proxy == ("127.0.0.1", 1443, "dd0123456789abcdef0123456789abcdef")


class _CallbackEvent:
    def __init__(self, sender_id, chat_id, data):
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))


def test_owner_blocks_unapproved_group_chats_but_allows_private_and_allowlisted_chats():
    client = _MessageClient()
    bot = TelegramBot(client, object(), frozenset({100}))

    assert bot._owner(_CallbackEvent(100, 100, b"menu:home")) == (100, 100)
    assert bot._owner(_CallbackEvent(100, -200, b"menu:home")) is None

    group_bot = TelegramBot(_MessageClient(), object(), frozenset({100}), allowed_chat_ids=frozenset({-200}))
    assert group_bot._owner(_CallbackEvent(100, -200, b"menu:home")) == (100, -200)
    assert group_bot._owner(_CallbackEvent(101, -200, b"menu:home")) is None

    settings = MonitorSettings(100, -200, "MOW", "OW", 5)
    assert asyncio.run(bot.run_scheduled_scan(settings)) is False
    assert asyncio.run(bot.run_scheduled_deep_scan(settings)) is False
    assert client.messages == []


class _MessageClient:
    def __init__(self):
        self.messages = []
        self.history = []
        self.send_kwargs = []

    async def send_message(self, chat_id, text, buttons=None, **kwargs):
        self.messages.append((chat_id, text, buttons))
        self.send_kwargs.append(kwargs)
        message = SimpleNamespace(id=len(self.messages), message=text, raw_text=text)
        self.history.append(message)
        return message

    async def get_messages(self, chat_id, *, limit):
        return self.history[-limit:]


class _UnconfirmedMessageClient(_MessageClient):
    async def send_message(self, chat_id, text, buttons=None, **kwargs):
        self.messages.append((chat_id, text, buttons))
        return None


class _TimeoutMessageClient(_MessageClient):
    async def send_message(self, chat_id, text, buttons=None, **kwargs):
        raise asyncio.TimeoutError("response timed out after request transmission")


class _FailingMessageClient(_MessageClient):
    def __init__(self, error_code=400):
        super().__init__()
        self.error_code = error_code

    async def send_message(self, chat_id, text, buttons=None, **kwargs):
        raise RPCError(None, "TELEGRAM_SEND_FAILURE", self.error_code)


@pytest.mark.parametrize("error_code", [400, 500])
def test_telegram_failure_leaves_finding_retryable(tmp_path, error_code):
    with SQLiteStore(tmp_path / "notification-retry.sqlite3") as store:
        def save(price, track):
            return store.record_scan(
                100, 100,
                {
                    "origin": "MOW", "trip_type": "OW", "complete": True,
                    "cards_found": 1, "dom_hot_ticket_count": 1,
                    "unique_destinations": 1, "warnings": [],
                    "offers": [{
                        "destination": "Тбилиси", "price": price, "currency": "RUB",
                        "source_url": "https://www.aviasales.ru/search/test",
                    }],
                },
                track_price_changes=track,
            )

        save(10000, False)
        save(8000, True)
        bot = TelegramBot(_FailingMessageClient(error_code), store, frozenset({100}))
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 0
        row = store.connection.execute(
            "SELECT n.status, n.attempts, f.id finding_id FROM telegram_notification_outbox n "
            "JOIN price_findings f ON f.id = n.finding_id"
        ).fetchone()
        assert row["status"] == "retryable"
        assert row["attempts"] == 1
        assert row["finding_id"] is not None
        store.connection.execute(
            "UPDATE telegram_notification_outbox SET next_attempt_at = '2000-01-01T00:00:00Z'"
        )
        store.connection.commit()

        retry_client = _MessageClient()
        retry_bot = TelegramBot(retry_client, store, frozenset({100}))
        assert asyncio.run(retry_bot._deliver_pending_price_notifications((100, 100))) == 1
        assert len(retry_client.messages) == 1
        assert store.connection.execute("SELECT COUNT(*) FROM price_findings").fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT status FROM telegram_notification_outbox"
        ).fetchone()["status"] == "sent"
        assert store.connection.execute(
            "SELECT message_id FROM telegram_notification_outbox"
        ).fetchone()[0] == 1


def test_telegram_empty_response_does_not_mark_outbox_sent(tmp_path):
    with SQLiteStore(tmp_path / "notification-no-ack.sqlite3") as store:
        def save(price):
            return store.record_scan(
                100, 100,
                {
                    "origin": "MOW", "trip_type": "OW", "complete": True,
                    "cards_found": 1, "dom_hot_ticket_count": 1,
                    "unique_destinations": 1, "warnings": [],
                    "offers": [{"destination": "Тбилиси", "price": price, "currency": "RUB"}],
                },
                track_price_changes=True,
            )

        save(10000)
        assert save(8000).price_findings
        bot = TelegramBot(_UnconfirmedMessageClient(), store, frozenset({100}))
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 0
        row = store.connection.execute(
            "SELECT status, attempts, last_error FROM telegram_notification_outbox"
        ).fetchone()
        assert row["status"] == "uncertain"
        assert row["attempts"] == 1
        assert "uncertain" in row["last_error"]


def test_timeout_does_not_trigger_blind_telegram_resend(tmp_path):
    with SQLiteStore(tmp_path / "notification-timeout.sqlite3") as store:
        for price in (10000, 8000):
            store.record_scan(100, 100, {
                "origin": "MOW", "trip_type": "OW", "complete": True,
                "cards_found": 1, "dom_hot_ticket_count": 1, "unique_destinations": 1,
                "offers": [{"destination": "Тбилиси", "price": price, "currency": "RUB"}],
            }, track_price_changes=True)
        client = _TimeoutMessageClient()
        bot = TelegramBot(client, store, frozenset({100}))
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 0
        row = store.connection.execute("SELECT status, attempts FROM telegram_notification_outbox").fetchone()
        assert tuple(row) == ("uncertain", 1)
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 0
        assert store.connection.execute(
            "SELECT attempts FROM telegram_notification_outbox"
        ).fetchone()[0] == 1


def test_crash_after_telegram_ack_reconciles_by_durable_delivery_key(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "notification-crash-after-ack.sqlite3") as store:
        for price in (10000, 8000):
            store.record_scan(100, 100, {
                "origin": "MOW", "trip_type": "OW", "complete": True,
                "cards_found": 1, "dom_hot_ticket_count": 1, "unique_destinations": 1,
                "offers": [{"destination": "Тбилиси", "price": price, "currency": "RUB"}],
            }, track_price_changes=True)
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}))
        real_mark_sent = store.mark_price_notification_sent

        def crash_before_db_commit(*_args, **_kwargs):
            raise SystemExit("simulated process crash after Telegram accepted the send")

        monkeypatch.setattr(store, "mark_price_notification_sent", crash_before_db_commit)
        with pytest.raises(SystemExit, match="simulated process crash"):
            asyncio.run(bot._deliver_pending_price_notifications((100, 100)))
        assert len(client.messages) == 1
        claimed = store.connection.execute(
            "SELECT status, delivery_key, message_text FROM telegram_notification_outbox"
        ).fetchone()
        assert claimed["status"] == "sending"
        assert claimed["delivery_key"] not in claimed["message_text"]
        assert claimed["delivery_key"] in claimed["message_text"].replace("\u2063", "")

        # On restart an expired in-flight lease is moved to uncertain, not
        # resent. Chat history contains the stable marker and resolves sent.
        store.connection.execute(
            "UPDATE telegram_notification_outbox SET lease_until = '2000-01-01T00:00:00Z'"
        )
        store.connection.commit()
        monkeypatch.setattr(store, "mark_price_notification_sent", real_mark_sent)
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 0
        row = store.connection.execute(
            "SELECT status, message_id FROM telegram_notification_outbox"
        ).fetchone()
        assert tuple(row) == ("sent", 1)
        assert len(client.messages) == 1


def test_inactive_route_price_finding_is_delivered_to_telegram(tmp_path):
    with SQLiteStore(tmp_path / "inactive-route-notification.sqlite3") as store:
        def save(price):
            return store.record_scan(
                100, 100,
                {
                    "origin": "MOW", "trip_type": "OW", "complete": True,
                    "cards_found": 1, "dom_hot_ticket_count": 1,
                    "unique_destinations": 1, "warnings": [],
                    "offers": [{
                        "destination": "Мумбаи", "price": price, "currency": "RUB",
                        "source_url": "https://www.aviasales.ru/search/MOWBOM",
                    }],
                },
                track_price_changes=True,
            )

        save(30431)
        destination = store.list_destinations(100, 100, "MOW")[0]
        assert destination.active is False
        finding = save(11940).price_findings[0]
        assert finding["notification_queued"] is True

        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}))
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 1
        assert len(client.messages) == 1
        assert "Мумбаи" in client.messages[0][1]
        assert store.connection.execute(
            "SELECT status FROM telegram_notification_outbox"
        ).fetchone()["status"] == "sent"


def test_kaliningrad_7109_6431_5457_runs_full_scan_to_telegram_pipeline(tmp_path):
    with SQLiteStore(tmp_path / "kaliningrad-application-pipeline.sqlite3") as store:
        settings = MonitorSettings(100, 100, "LED", "RT", 30, 3, 11)
        store.save_settings(settings)
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}))

        for price in (7109, 6431, 5457):
            def collector_fn(origin, trip_type, duration_min, duration_max, *, _price=price):
                return {
                    "origin": origin, "trip_type": trip_type, "complete": True,
                    "cards_found": 1, "dom_hot_ticket_count": 1,
                    "unique_destinations": 1, "warnings": [],
                    "offers": [{
                        "destination": "Калининград", "destination_route": "LED2909KGD06101",
                        "price": _price, "currency": "RUB",
                        "departure_date": "2026-10-05", "departure_date_raw": "5 окт",
                        "return_date": "2026-10-12", "return_date_raw": "12 окт",
                        "source_url": "https://www.aviasales.ru/search/LED2909KGD06101",
                        "stops": 0, "transfers": [], "flight_duration": ["2 ч", "2 ч"],
                    }],
                }

            outcome = asyncio.run(scan_and_persist(
                store, 100, 100, collector_fn=collector_fn, origin="LED"
            ))
            if price == 7109:
                destination = store.list_destinations(100, 100, "LED")[0]
                assert store.set_destination_active(100, 100, destination.destination_id, True)

        assert len(outcome.stored_scan.price_changes) == 1
        assert outcome.stored_scan.price_changes[0]["change_percent"] == -15.15
        assert len(outcome.stored_scan.price_findings) == 1
        finding = outcome.stored_scan.price_findings[0]
        assert finding["notification_queued"] is True
        assert finding["deal_metrics"]["finding"] is True
        assert store.connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0] == 3
        queued = store.connection.execute(
            "SELECT status FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()
        assert queued["status"] == "pending"

        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 1
        assert len(client.messages) == 1
        assert "Калининград" in client.messages[0][1]
        assert store.connection.execute(
            "SELECT status FROM telegram_notification_outbox WHERE finding_id = ?",
            (finding["finding_id"],),
        ).fetchone()["status"] == "sent"


@pytest.mark.parametrize("active", [False, True])
def test_regular_application_pipeline_sends_same_deal_for_both_active_states(tmp_path, active):
    with SQLiteStore(tmp_path / f"regular-application-active-{active}.sqlite3") as store:
        store.save_settings(MonitorSettings(100, 100, "MOW", "OW", 30))
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}))

        def collector_for(price):
            def collector_fn(origin, trip_type, duration_min, duration_max):
                return {
                    "origin": origin, "trip_type": trip_type, "complete": True,
                    "cards_found": 1, "dom_hot_ticket_count": 1,
                    "unique_destinations": 1, "warnings": [],
                    "offers": [{
                        "destination": "TEST", "destination_route": "MOW0110TST1",
                        "price": price, "currency": "RUB",
                        "departure_date": "2026-10-01", "departure_date_raw": "1 окт",
                        "source_url": "https://www.aviasales.ru/search/MOW0110TST1",
                    }],
                }
            return collector_fn

        asyncio.run(scan_and_persist(store, 100, 100, collector_fn=collector_for(10000)))
        destination = store.list_destinations(100, 100, "MOW")[0]
        assert destination.active is False
        if active:
            assert store.set_destination_active(100, 100, destination.destination_id, True)

        outcome = asyncio.run(scan_and_persist(store, 100, 100, collector_fn=collector_for(7000)))
        assert len(outcome.stored_scan.price_findings) == 1
        assert outcome.stored_scan.price_findings[0]["notification_queued"] is True
        assert asyncio.run(bot._deliver_pending_price_notifications((100, 100))) == 1
        assert len(client.messages) == 1
        assert "TEST" in client.messages[0][1]


class _Status:
    def __init__(self):
        self.edits = []

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))


class _FailingStatus(_Status):
    async def edit(self, text, buttons=None):
        raise RuntimeError("status message was deleted")


class _RunEvent:
    def __init__(self):
        self.status = _Status()

    async def respond(self, _text, buttons=None):
        return self.status


class _FailingRunEvent(_RunEvent):
    def __init__(self):
        self.status = _FailingStatus()


def test_destination_callbacks_are_owner_scoped_idempotent_and_refresh_inline_message(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, -200, "MOW", "OW", 5))
        saved = store.record_scan(
            100,
            -200,
            {
                "origin": "MOW",
                "trip_type": "OW",
                "complete": True,
                "cards_found": 1,
                "dom_hot_ticket_count": 1,
                "unique_destinations": 1,
                "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
                "warnings": [],
            },
        )
        destination_id = saved.new_destinations[0]["destination_id"]
        bot = TelegramBot(
            client=object(), store=store, allowed_user_ids=frozenset({100}),
            allowed_chat_ids=frozenset({-200}),
        )
        payload = f"dest:{destination_id}:1:0".encode("ascii")

        foreign_event = _CallbackEvent(999, -200, payload)
        asyncio.run(bot._on_callback(foreign_event))
        assert store.list_destinations(100, -200, "MOW")[0].active is False
        assert "Нет доступа" in foreign_event.answers[0][0]

        owner_event = _CallbackEvent(100, -200, payload)
        asyncio.run(bot._on_callback(owner_event))
        asyncio.run(bot._on_callback(_CallbackEvent(100, -200, payload)))

        destination = store.list_destinations(100, -200, "MOW")[0]
        assert destination.active is True
        assert len(owner_event.edits) == 1
        assert "направления" in owner_event.edits[0][0]


def test_scheduled_scan_does_not_push_new_destinations_or_refresh_changes(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}), allowed_chat_ids=frozenset({-200}))
        result = {
            "origin": "MOW",
            "trip_type": "OW",
            "complete": True,
            "cards_found": 2,
            "unique_destinations": 2,
            "offers": [
                {"destination": "Тбилиси", "price": 4200, "currency": "RUB"},
                {"destination": "Стамбул", "price": 8100, "currency": "RUB"},
            ],
            "warnings": [],
        }

        async def fake_scan_and_persist(_store, user_id, chat_id, **_kwargs):
            saved = replace(
                _store.record_scan(user_id, chat_id, result),
                outdated_offers=3,
                disappeared_destinations=("Санкт-Петербург",),
            )
            return ScanOutcome(settings, result, saved)

        monkeypatch.setattr(telegram_bot_module, "scan_and_persist", fake_scan_and_persist)

        asyncio.run(bot.run_scheduled_scan(settings))

        assert client.messages == []
        assert bot.scan_in_progress is False


def test_web_deep_scan_uses_owner_settings_and_rejects_overlap(tmp_path, monkeypatch):
    async def verify():
        with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
            settings = MonitorSettings(100, 100, "MOW", "OW", 5)
            store.save_settings(settings)
            bot = TelegramBot(_MessageClient(), store, frozenset({100}))
            started = []

            async def fake_deep_scan(selected):
                started.append(selected.origin)

            monkeypatch.setattr(bot, "run_scheduled_deep_scan", fake_deep_scan)
            assert await bot.start_web_deep_scan("MOW") is True
            await asyncio.sleep(0)
            assert started == ["MOW"]
            bot.active_scans.add(bot._scan_key((100, 100), "MOW"))
            assert await bot.start_web_deep_scan("MOW") is False
            assert await bot.start_web_deep_scan("LED") is False

    asyncio.run(verify())


def test_notification_failure_does_not_retry_completed_scan(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        bot = TelegramBot(_FailingMessageClient(), store, frozenset({100}), allowed_chat_ids=frozenset({-200}))
        result = {
            "origin": "MOW",
            "trip_type": "OW",
            "complete": True,
            "cards_found": 1,
            "dom_hot_ticket_count": 1,
            "unique_destinations": 1,
            "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
            "warnings": [],
        }

        async def fake_scan_and_persist(_store, user_id, chat_id, **_kwargs):
            saved = _store.record_scan(user_id, chat_id, result)
            return ScanOutcome(settings, result, saved)

        monkeypatch.setattr(telegram_bot_module, "scan_and_persist", fake_scan_and_persist)

        assert asyncio.run(bot.run_scheduled_scan(settings)) is True
        assert bot.active_scans == set()


def test_manual_scan_releases_lock_when_status_update_fails(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        bot = TelegramBot(_MessageClient(), store, frozenset({100}))
        result = {
            "origin": "MOW",
            "trip_type": "OW",
            "complete": True,
            "cards_found": 1,
            "dom_hot_ticket_count": 1,
            "unique_destinations": 1,
            "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
            "warnings": [],
        }

        async def fake_scan_and_persist(_store, user_id, chat_id, **_kwargs):
            saved = _store.record_scan(user_id, chat_id, result)
            return ScanOutcome(settings, result, saved)

        monkeypatch.setattr(telegram_bot_module, "scan_and_persist", fake_scan_and_persist)
        asyncio.run(bot._run_scan(_FailingRunEvent(), (100, -200)))

        assert bot.active_scans == set()
        assert bot.scan_in_progress is False


def test_website_start_failure_does_not_escape_into_bot_startup(monkeypatch):
    async def fail_server(*_args):
        raise SystemExit(1)

    monkeypatch.setattr(telegram_bot_module, "run_server", fail_server)
    asyncio.run(telegram_bot_module._run_web_service("unused.sqlite3", frozenset({100})))


def test_scheduled_incomplete_scan_does_not_send_notification(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}), allowed_chat_ids=frozenset({-200}))
        result = {
            "origin": "MOW",
            "trip_type": "OW",
            "complete": False,
            "cards_found": 0,
            "unique_destinations": 0,
            "offers": [],
            "warnings": ["page timeout"],
        }

        async def fake_scan_and_persist(_store, user_id, chat_id, **_kwargs):
            saved = _store.record_scan(user_id, chat_id, result)
            return ScanOutcome(settings, result, saved)

        monkeypatch.setattr(telegram_bot_module, "scan_and_persist", fake_scan_and_persist)

        asyncio.run(bot.run_scheduled_scan(settings))

        assert client.messages == []
        assert store.list_destinations(100, -200, "MOW") == []


def test_monitor_toggle_updates_saved_state_and_home_buttons(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        store.save_settings(MonitorSettings(100, -200, "MOW", "OW", 5))
        bot = TelegramBot(
            client=object(), store=store, allowed_user_ids=frozenset({100}),
            allowed_chat_ids=frozenset({-200}),
        )

        first = _CallbackEvent(100, -200, b"monitor:toggle")
        asyncio.run(bot._on_callback(first))
        assert store.get_settings(100, -200).enabled is False
        assert "выключен" in first.edits[0][0]
        assert first.answers == [("Мониторинг выключен.", False)]

        second = _CallbackEvent(100, -200, b"monitor:toggle")
        asyncio.run(bot._on_callback(second))
        assert store.get_settings(100, -200).enabled is True
        assert "включён" in second.edits[0][0]


def test_deep_scan_notifies_only_significant_price_drop(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        seed = store.record_scan(
            100,
            -200,
            {
                "origin": "MOW",
                "trip_type": "OW",
                "complete": True,
                "cards_found": 1,
                "dom_hot_ticket_count": 1,
                "unique_destinations": 1,
            "offers": [{"destination": "Тбилиси", "price": 10000, "currency": "RUB"}],
                "warnings": [],
            },
        )
        destination_id = seed.new_destinations[0]["destination_id"]
        store.set_destination_active(100, -200, destination_id, True)
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}))
        result = {
            "origin": "MOW",
            "trip_type": "OW",
            "complete": True,
            "cards_found": 1,
            "dom_hot_ticket_count": 1,
            "unique_destinations": 1,
            "offers": [{
                "destination": "Тбилиси", "price": 8000, "currency": "RUB",
                "departure_date_raw": "12 окт", "source_url": "https://www.aviasales.ru/search/test",
                "stops": [1], "transfers": ["Стамбул"], "transfer_durations": ["2 ч"],
                "airports": [
                    {"code": "VKO", "name": "Внуково"},
                    {"code": "IST", "name": "Стамбул"},
                    {"code": "TBS", "name": "Тбилиси"},
                ],
                "flight_duration": ["8 ч"],
            }],
            "warnings": [],
        }

        async def fake_deep_scan(_store, user_id, chat_id, **_kwargs):
            saved = _store.record_scan(user_id, chat_id, result, track_price_changes=True)
            active = tuple(_store.list_active_destinations(user_id, chat_id, "MOW"))
            return DeepScanOutcome(settings, active, result, saved)

        monkeypatch.setattr(telegram_bot_module, "deep_scan_and_persist", fake_deep_scan)
        event = _RunEvent()
        asyncio.run(bot._run_deep_scan(event, (100, -200)))

        assert len(client.messages) == 1
        assert "🔥 <b>Москва → Тбилиси — 8 000 ₽</b>" in client.messages[0][1]
        assert "<s>10 000 ₽</s> <b>−20%</b>" in client.messages[0][1]
        assert "✈️ <b>Туда:</b> 12.10" in client.messages[0][1]
        assert "Москва (VKO) → Стамбул (IST) (2 ч) → Тбилиси (TBS)" in client.messages[0][1]
        assert '<a href="https://www.aviasales.ru/search/test"><b>Открыть билет</b></a>' in client.messages[0][1]
        assert "Существенных снижений: 1" in event.status.edits[0][0]


def test_scheduled_deep_partial_batch_is_failed_and_not_saved_as_full_completion(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "scheduled-deep-partial.sqlite3") as store:
        settings = MonitorSettings(100, 100, "MOW", "OW", 30)
        store.save_settings(settings)
        result = {
            "origin": "MOW", "trip_type": "OW", "complete": True, "partial": True,
            "batch_complete": False, "destinations_scanned": 7,
            "cards_found": 1, "dom_hot_ticket_count": 1, "unique_destinations": 1,
            "offers": [{"destination": "Тбилиси", "price": 5000, "currency": "RUB"}],
            "warnings": ["one destination failed"],
        }
        saved = store.record_scan(100, 100, result, scan_kind="deep", track_price_changes=True)

        async def fake_deep_scan(_store, user_id, chat_id, **_kwargs):
            return DeepScanOutcome(settings, (), result, saved)

        monkeypatch.setattr(telegram_bot_module, "deep_scan_and_persist", fake_deep_scan)
        bot = TelegramBot(_MessageClient(), store, frozenset({100}))
        assert asyncio.run(bot.run_scheduled_deep_scan(settings)) is False
        assert store.latest_scan_finished_at(100, 100, "MOW", "deep") is None


def test_outbox_price_finding_is_deduplicated_and_formats_round_trip_legs(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        client = _MessageClient()
        bot = TelegramBot(client, store, frozenset({100}))
        offers = [{
            "destination": "Фукуок", "destination_route": "MOW0910PQC19101",
            "price": price, "old_price": 80307, "currency": "RUB",
            "departure_date": "2026-10-09", "departure_date_raw": "9 окт",
            "return_date": "2026-10-20", "return_date_raw": "20 окт",
            "flight_times": ["15:30", "08:00", "10:00", "13:00", "20:10", "23:00"],
            "stops": [1, 0], "transfers": ["Хошимин"], "transfer_durations": ["3 ч"],
            "airports": [
                {"code": "VKO", "name": "Внуково"},
                {"code": "SGN", "name": "Хошимин"},
                {"code": "PQC", "name": "Фукуок"},
                {"code": "PQC", "name": "Фукуок"},
                {"code": "SVO", "name": "Шереметьево"},
            ],
            "flight_duration": ["14 ч", "12 ч"],
            "source_url": "https://www.aviasales.ru/search/test",
        } for price in (80307, 66047)]
        for _ in range(2):
            store.record_scan(100, 100, {
                "origin": "MOW", "trip_type": "RT", "complete": True,
                "duration_min_days": 3, "duration_max_days": 11,
                "cards_found": 1, "dom_hot_ticket_count": 1, "unique_destinations": 1,
                "offers": offers[:1], "warnings": [],
            })
        stored = store.record_scan(100, 100, {
            "origin": "MOW", "trip_type": "RT", "complete": True,
            "duration_min_days": 3, "duration_max_days": 11,
            "cards_found": 1, "dom_hot_ticket_count": 1, "unique_destinations": 1,
            "offers": offers[1:], "warnings": [],
        }, track_price_changes=True)
        assert stored.price_findings[0]["notification_queued"] is True

        async def deliver_twice():
            return (
                await bot._deliver_pending_price_notifications((100, 100)),
                await bot._deliver_pending_price_notifications((100, 100)),
            )

        assert asyncio.run(deliver_twice()) == (1, 0)

        assert len(client.messages) == 1
        text = client.messages[0][1]
        assert "🔥 <b>Москва → Фукуок — 66 047 ₽</b>" in text
        assert "<s>80 307 ₽</s> <b>−18%</b>" in text
        assert "✈️ <b>Туда:</b> 09.10" in text
        assert "Москва (VKO) → Хошимин (SGN) (3 ч) → Фукуок (PQC) · 14 ч в пути" in text
        assert "↩️ <b>Обратно:</b> 20.10" in text
        assert "Фукуок (PQC) → Москва (SVO) · 12 ч в пути" in text
        assert "15:30" not in text
        assert "20:10" not in text
        assert "HT-" not in text
        assert '<a href="https://www.aviasales.ru/search/test"><b>Открыть билет</b></a>' in text
        assert client.send_kwargs[0]["link_preview"] is False


def test_alert_leg_adds_codes_to_direct_airport_endpoints():
    route, _ = telegram_bot_module._alert_leg(
        {"stops": [0], "airports": [{"code": "VKO"}, {"code": "DWC"}]},
        0,
        "Москва",
        "Дубай",
    )

    assert route == "Москва (VKO) → Дубай (DWC)"


def test_alert_link_rejects_non_aviasales_hosts():
    assert telegram_bot_module._safe_aviasales_url("https://www.aviasales.ru/ticket")
    assert telegram_bot_module._safe_aviasales_url("https://aviasales.ru/ticket")
    assert telegram_bot_module._safe_aviasales_url("https://aviasales.ru.example.com/ticket") is None
    assert telegram_bot_module._safe_aviasales_url("javascript:alert(1)") is None
    assert telegram_bot_module._safe_aviasales_url("https://aviasales.ru@attacker.example/ticket") is None
    assert telegram_bot_module._safe_aviasales_url("https://www.aviasales.ru:444/ticket") is None


def test_manual_deep_scan_uses_selected_origin_when_multiple_origins_exist(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        mow = MonitorSettings(100, -200, "MOW", "OW", 5)
        led = MonitorSettings(100, -200, "LED", "OW", 5)
        store.save_settings(mow)
        store.save_settings(led)
        for origin in ("MOW", "LED"):
            store.record_scan(
                100,
                -200,
                {
                    "origin": origin,
                    "trip_type": "OW",
                    "complete": True,
                    "cards_found": 1,
                    "dom_hot_ticket_count": 1,
                    "unique_destinations": 1,
                    "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
                    "warnings": [],
                },
            )
            destination = store.list_destinations(100, -200, origin)[0]
            store.set_destination_active(100, -200, destination.destination_id, True)

        called = []

        async def fake_deep_scan(_store, user_id, chat_id, **kwargs):
            called.append(kwargs)
            saved = _store.record_scan(
                user_id,
                chat_id,
                {
                    "origin": "LED",
                    "trip_type": "OW",
                    "complete": True,
                    "cards_found": 1,
                    "dom_hot_ticket_count": 1,
                    "unique_destinations": 1,
                    "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
                    "warnings": [],
                },
                track_price_changes=True,
            )
            return DeepScanOutcome(led, tuple(_store.list_active_destinations(user_id, chat_id, "LED")), {
                "complete": True,
                "cards_found": 1,
            }, saved)

        monkeypatch.setattr(telegram_bot_module, "deep_scan_and_persist", fake_deep_scan)
        bot = TelegramBot(_MessageClient(), store, frozenset({100}), allowed_chat_ids=frozenset({-200}))
        asyncio.run(bot._run_deep_scan(_RunEvent(), (100, -200), origin="LED"))

        assert called and called[0]["origin"] == "LED"
        assert bot.active_scans == set()


def test_scheduled_scan_skips_same_origin_when_already_active(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        bot = TelegramBot(_MessageClient(), store, frozenset({100}), allowed_chat_ids=frozenset({-200}))
        bot.active_scans.add(bot._scan_key((100, -200), "MOW"))

        assert asyncio.run(bot.run_scheduled_scan(settings)) is ScanDisposition.DEFERRED

        assert asyncio.run(bot.run_scheduled_deep_scan(settings)) is ScanDisposition.DEFERRED


def test_scheduled_regular_and_deep_scans_share_one_browser_slot(tmp_path, monkeypatch):
    async def verify():
        with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
            mow = MonitorSettings(100, 100, "MOW", "OW", 30)
            led = MonitorSettings(100, 100, "LED", "OW", 30)
            store.save_settings(mow)
            store.save_settings(led)
            bot = TelegramBot(_MessageClient(), store, frozenset({100}))
            active = 0
            peak_active = 0

            async def occupy_slot(_origin):
                nonlocal active, peak_active
                active += 1
                peak_active = max(peak_active, active)
                await asyncio.sleep(0.01)
                active -= 1

            async def fake_regular(_store, user_id, chat_id):
                await occupy_slot("regular")
                result = {
                    "origin": "MOW", "trip_type": "OW", "complete": True,
                    "cards_found": 0, "unique_destinations": 0, "offers": [], "warnings": [],
                }
                saved = _store.record_scan(user_id, chat_id, result)
                return ScanOutcome(mow, result, saved)

            async def fake_deep(_store, user_id, chat_id, **_kwargs):
                await occupy_slot("deep")
                result = {
                    "origin": "LED", "trip_type": "OW", "complete": True,
                    "cards_found": 0, "unique_destinations": 0, "offers": [], "warnings": [],
                }
                saved = _store.record_scan(user_id, chat_id, result, scan_kind="deep")
                return DeepScanOutcome(led, (), result, saved)

            monkeypatch.setattr(telegram_bot_module, "scan_and_persist", fake_regular)
            monkeypatch.setattr(telegram_bot_module, "deep_scan_and_persist", fake_deep)
            await asyncio.gather(
                bot.run_scheduled_scan(mow),
                bot.run_scheduled_deep_scan(led),
            )

            assert peak_active == 1
            assert bot.active_scans == set()
            assert bot.scan_in_progress is False

    asyncio.run(verify())


def test_history_command_lists_recent_scans(tmp_path):
    with SQLiteStore(tmp_path / "monitor.sqlite3") as store:
        settings = MonitorSettings(100, -200, "MOW", "OW", 5)
        store.save_settings(settings)
        store.record_scan(
            100,
            -200,
            {
                "origin": "MOW",
                "trip_type": "OW",
                "complete": True,
                "cards_found": 2,
                "dom_hot_ticket_count": 2,
                "unique_destinations": 1,
                "offers": [{"destination": "Тбилиси", "price": 4200, "currency": "RUB"}],
                "warnings": [],
            },
        )
        class _HistoryEvent:
            def __init__(self):
                self.messages = []

            async def respond(self, text, buttons=None):
                self.messages.append((text, buttons))

        event = _HistoryEvent()
        bot = TelegramBot(client=object(), store=store, allowed_user_ids=frozenset({100}))

        asyncio.run(bot._show_history(event, (100, -200)))

        assert "История последних сканов" in event.messages[0][0]
        assert "2 карточек" in event.messages[0][0]


def test_telethon_cluster_warning_filter_suppresses_only_transient_difference_warning():
    warning_filter = telegram_bot_module._TelethonClusterWarningFilter()
    cluster_warning = logging.LogRecord(
        "telethon.client.updates", logging.WARNING, "updates.py", 1,
        "Cannot get difference: RPCError 400: MTPROTO_CLUSTER_INVALID", (), None,
    )
    other_telethon_warning = logging.LogRecord(
        "telethon.client.updates", logging.WARNING, "updates.py", 1,
        "Cannot get difference: RPCError 400: AUTH_KEY_UNREGISTERED", (), None,
    )
    unrelated_warning = logging.LogRecord(
        "telethon.client", logging.WARNING, "client.py", 1,
        "MTPROTO_CLUSTER_INVALID", (), None,
    )

    assert warning_filter.filter(cluster_warning) is False
    assert warning_filter.filter(other_telethon_warning) is True
    assert warning_filter.filter(unrelated_warning) is True
