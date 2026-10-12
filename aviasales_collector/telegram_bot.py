from __future__ import annotations

import asyncio
import ctypes
import hashlib
import html
import inspect
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.errors import RPCError
from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

from .dates import parse_day_month
from .database import MonitorSettings, SQLiteStore
from .deep_scan import DeepScanConfigurationError, deep_scan_and_persist
from .flight_routes import airport_legs
from .scheduler import DeepScanScheduler, MonitorScheduler, ScanDisposition
from .scan_service import ScanConfigurationError, scan_and_persist
from .web import run_server
from .monitoring_logs import (
    MonitoringFormatter,
    configure_utf8_console,
    format_trip_type,
    log_monitoring,
    log_monitoring_exception,
    summarize_warnings,
)


log = logging.getLogger(__name__)
ORIGIN_RE = re.compile(r"^[A-Za-z]{3}$")
DURATION_RE = re.compile(r"^\s*(\d{1,2})\s*[,; -]\s*(\d{1,2})\s*$")
DESTINATIONS_PER_PAGE = 20
NEW_DESTINATION_BUTTONS = 4
AUTO_NOTIFICATION_DESTINATIONS = 30
PRICE_DROP_THRESHOLD_PERCENT = 12.0
HISTORY_SCAN_LIMIT = 8
DETAILS_DESTINATIONS_PER_PAGE = 4
DETAILS_OFFERS_PER_DESTINATION = 2


class _TelethonClusterWarningFilter(logging.Filter):
    """Hide a known noisy Telethon update-sync warning.

    MTProto can briefly report a stale update cluster after reconnecting. The
    library retries that synchronization itself; this warning is unrelated to
    flight scans and otherwise repeats every update poll.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "telethon.client.updates"
            and "MTPROTO_CLUSTER_INVALID" in record.getMessage()
        )


@dataclass(frozen=True)
class TelegramBotConfig:
    api_id: int
    api_hash: str
    bot_token: str
    allowed_user_ids: frozenset[int]
    proxy_host: str
    proxy_port: int
    proxy_secret: str
    allowed_chat_ids: frozenset[int] = frozenset()
    database_path: str = "hot-tickets.sqlite3"
    session_name: str = "hot-tickets-bot"
    price_drop_threshold_percent: float = PRICE_DROP_THRESHOLD_PERCENT
    deep_scan_max_destinations: int | None = None
    deep_scan_retries: int = 0
    deep_scan_retry_delay_seconds: float = 3.0
    deep_scan_interval_minutes: int = 360
    lock_path: str = ".hot-tickets-bot.lock"
    browser_data_dir: str | None = ".aviasales-browser"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TelegramBotConfig:
        values = os.environ if env is None else env
        missing = [
            key
            for key in (
                "TELEGRAM_API_ID",
                "TELEGRAM_API_HASH",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_ALLOWED_USER_IDS",
                "TG_WS_PROXY_SECRET",
            )
            if not values.get(key, "").strip()
        ]
        if missing:
            raise ValueError(f"missing required environment variables: {', '.join(missing)}")

        try:
            api_id = int(values["TELEGRAM_API_ID"])
            proxy_port = int(values.get("TG_WS_PROXY_PORT", "1443"))
        except ValueError as exc:
            raise ValueError("TELEGRAM_API_ID and TG_WS_PROXY_PORT must be integers") from exc
        if api_id <= 0 or not 1 <= proxy_port <= 65535:
            raise ValueError("TELEGRAM_API_ID or TG_WS_PROXY_PORT is outside its valid range")

        proxy_secret = values["TG_WS_PROXY_SECRET"].strip().lower()
        if re.fullmatch(r"[0-9a-f]{32}", proxy_secret):
            proxy_secret = "dd" + proxy_secret
        if not re.fullmatch(r"dd[0-9a-f]{32}", proxy_secret):
            raise ValueError("TG_WS_PROXY_SECRET must be dd-prefixed 16-byte hex (or its 32-hex raw secret)")

        try:
            allowed_user_ids = frozenset(
                int(part.strip())
                for part in values["TELEGRAM_ALLOWED_USER_IDS"].split(",")
                if part.strip()
            )
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must be comma-separated positive integers") from exc
        if not allowed_user_ids or any(user_id <= 0 for user_id in allowed_user_ids):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain at least one positive user ID")

        try:
            allowed_chat_ids = frozenset(
                int(part.strip())
                for part in str(values.get("TELEGRAM_ALLOWED_CHAT_IDS", "")).split(",")
                if part.strip()
            )
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must be comma-separated chat IDs") from exc
        if 0 in allowed_chat_ids:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS cannot contain zero")

        proxy_host = values.get("TG_WS_PROXY_HOST", "127.0.0.1").strip()
        database_path = values.get("HOT_TICKETS_DB_PATH", "hot-tickets.sqlite3").strip()
        session_name = values.get("TELETHON_SESSION", "hot-tickets-bot").strip()
        lock_path = values.get("HOT_TICKETS_LOCK_PATH", ".hot-tickets-bot.lock").strip()
        browser_data_dir = values.get("AVIASALES_BROWSER_DATA_DIR", ".aviasales-browser").strip() or None
        if not proxy_host or not database_path or not session_name or not lock_path:
            raise ValueError("proxy host, database path, session name, and lock path must not be empty")

        try:
            threshold = float(values.get("PRICE_DROP_THRESHOLD_PERCENT", str(PRICE_DROP_THRESHOLD_PERCENT)))
            max_destinations_raw = values.get("DEEP_SCAN_MAX_DESTINATIONS", "").strip()
            max_destinations = int(max_destinations_raw) if max_destinations_raw else None
            deep_retries = int(values.get("DEEP_SCAN_RETRIES", "0"))
            deep_retry_delay = float(values.get("DEEP_SCAN_RETRY_DELAY_SECONDS", "3"))
            deep_interval_raw = values.get("DEEP_SCAN_INTERVAL_MINUTES", "360").strip()
            deep_interval = int(deep_interval_raw) if deep_interval_raw else 360
        except ValueError as exc:
            raise ValueError(
                "PRICE_DROP_THRESHOLD_PERCENT, DEEP_SCAN_MAX_DESTINATIONS, "
                "DEEP_SCAN_RETRIES, DEEP_SCAN_RETRY_DELAY_SECONDS, and "
                "DEEP_SCAN_INTERVAL_MINUTES must be numeric"
            ) from exc
        if threshold <= 0:
            raise ValueError("PRICE_DROP_THRESHOLD_PERCENT must be positive")
        if max_destinations is not None and max_destinations < 1:
            raise ValueError("DEEP_SCAN_MAX_DESTINATIONS must be positive when set")
        if deep_retries < 0 or deep_retry_delay < 0:
            raise ValueError("deep scan retries and retry delay cannot be negative")
        if deep_interval < 1:
            raise ValueError("DEEP_SCAN_INTERVAL_MINUTES must be positive")

        return cls(
            api_id=api_id,
            api_hash=values["TELEGRAM_API_HASH"].strip(),
            bot_token=values["TELEGRAM_BOT_TOKEN"].strip(),
            allowed_user_ids=allowed_user_ids,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_secret=proxy_secret,
            allowed_chat_ids=allowed_chat_ids,
            database_path=database_path,
            session_name=session_name,
            price_drop_threshold_percent=threshold,
            deep_scan_max_destinations=max_destinations,
            deep_scan_retries=deep_retries,
            deep_scan_retry_delay_seconds=deep_retry_delay,
            deep_scan_interval_minutes=deep_interval,
            lock_path=lock_path,
            browser_data_dir=browser_data_dir,
        )


class BotInstanceLock:
    """Small cross-platform singleton guard for the bot process."""

    _owned_paths: set[str] = set()
    _owned_paths_guard = threading.Lock()

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.acquired = False
        self._owner_pid: int | None = None
        self._guard_handle = None
        self._windows_mutex = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve()).casefold()
        with self._owned_paths_guard:
            if key in self._owned_paths:
                raise RuntimeError(f"another hot-tickets bot instance is already running: {self.path}")
        try:
            if os.name == "nt":
                mutex_name = "Local\\HotTicketsBot-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                kernel32.CreateMutexW.restype = wintypes.HANDLE
                kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel32.WaitForSingleObject.restype = wintypes.DWORD
                kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                kernel32.ReleaseMutex.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                handle = kernel32.CreateMutexW(None, False, mutex_name)
                if not handle:
                    raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
                result = kernel32.WaitForSingleObject(handle, 0)
                if result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                    kernel32.CloseHandle(handle)
                    if result == 0x102:  # WAIT_TIMEOUT
                        raise RuntimeError(f"another hot-tickets bot instance is already running: {self.path}")
                    raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
                self._guard_handle = handle
                self._windows_mutex = True
            else:
                import fcntl

                guard_path = self.path.with_name(self.path.name + ".guard")
                self._guard_handle = open(guard_path, "a+b")
                try:
                    fcntl.flock(self._guard_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    self._guard_handle.close()
                    self._guard_handle = None
                    raise RuntimeError(
                        f"another hot-tickets bot instance is already running: {self.path}"
                    ) from exc

            with self._owned_paths_guard:
                if key in self._owned_paths:
                    raise RuntimeError(f"another hot-tickets bot instance is already running: {self.path}")
                self._owned_paths.add(key)
            with self.path.open("w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            self._owner_pid = os.getpid()
            self.acquired = True
        except Exception:
            with self._owned_paths_guard:
                self._owned_paths.discard(key)
            self._release_guard()
            raise

    def _release_guard(self) -> None:
        if self._guard_handle is None:
            return
        if self._windows_mutex:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.ReleaseMutex(self._guard_handle)
            kernel32.CloseHandle(self._guard_handle)
        else:
            import fcntl

            fcntl.flock(self._guard_handle.fileno(), fcntl.LOCK_UN)
            self._guard_handle.close()
        self._guard_handle = None
        self._windows_mutex = False

    def release(self) -> None:
        if self.acquired:
            key = str(self.path.resolve()).casefold()
            try:
                self.path.unlink(missing_ok=True)
            finally:
                with self._owned_paths_guard:
                    self._owned_paths.discard(key)
                self.acquired = False
                self._owner_pid = None
                self._release_guard()
        else:
            self._release_guard()

    def __enter__(self) -> BotInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def create_telegram_client(config: TelegramBotConfig) -> TelegramClient:
    return TelegramClient(
        config.session_name,
        config.api_id,
        config.api_hash,
        connection=ConnectionTcpMTProxyRandomizedIntermediate,
        proxy=(config.proxy_host, config.proxy_port, config.proxy_secret),
    )


def _callback(text: str) -> bytes:
    return text.encode("ascii")


def _short(text: str, maximum: int = 42) -> str:
    text = " ".join(text.split())
    if len(text) > maximum:
        return text[: maximum - 1] + "…"
    return text


def _format_price(price: int | None, currency: str | None) -> str:
    if not isinstance(price, int):
        return ""
    symbols = {"RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£", "TRY": "₺"}
    suffix = symbols.get(currency or "", currency or "")
    return f" · {price:,}".replace(",", " ") + (f" {suffix}" if suffix else "")


def _format_alert_price(price: int | None, currency: str | None) -> str:
    symbols = {"RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£", "TRY": "₺"}
    amount = f"{round(price):,}".replace(",", " ") if isinstance(price, (int, float)) else "цена не указана"
    return f"{amount} {symbols.get(currency or '', currency or '')}".strip()


def _safe_aviasales_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and (host == "aviasales.ru" or host.endswith(".aviasales.ru"))
        ):
            return value
    except ValueError:
        pass
    return None


def _alert_date(raw, normalized) -> str | None:
    if isinstance(normalized, str):
        try:
            parsed = datetime.fromisoformat(normalized[:10])
            return parsed.strftime("%d.%m")
        except ValueError:
            pass
    if isinstance(raw, str):
        parsed = parse_day_month(raw)
        if parsed:
            day, month = parsed
            return f"{day:02d}.{month:02d}"
    return None


def _alert_leg(change: dict, leg_index: int, origin: str, destination: str) -> tuple[str, str]:
    raw_stops = change.get("stops")
    stops = raw_stops if isinstance(raw_stops, list) else [raw_stops]
    if len(stops) == 1 and leg_index == 1:
        stops = [stops[0], None]
    stop_count = stops[leg_index] if leg_index < len(stops) else None
    stop_count = int(stop_count) if isinstance(stop_count, (int, float)) and stop_count >= 0 else None
    cities = change.get("transfers")
    cities = cities if isinstance(cities, list) else [cities] if cities else []
    durations = change.get("transfer_durations")
    durations = durations if isinstance(durations, list) else [durations] if durations else []

    # The parser keeps the actual airport chain separately from the city labels.
    # Use it to annotate endpoints and connections without inventing codes when
    # the itinerary did not provide them.
    airport_chains = airport_legs(change.get("airports"), stops, "RT" if len(stops) > 1 else "OW")
    airport_chain = airport_chains[leg_index] if leg_index < len(airport_chains) else []
    airport_codes = [
        str(point.get("code") or "").upper()
        if re.fullmatch(r"[A-Za-z]{3}", str(point.get("code") or "")) else ""
        for point in airport_chain
    ]

    def with_code(label: str, code: str | None) -> str:
        safe_label = html.escape(str(label), quote=False)
        if code:
            return f"{safe_label} ({html.escape(code, quote=False)})"
        return safe_label

    if len(stops) > 1 and all(isinstance(value, int) and value >= 0 for value in stops):
        city_start = sum(stops[:leg_index])
        leg_cities = cities[city_start:city_start + stops[leg_index]]
        leg_durations = durations[city_start:city_start + stops[leg_index]]
    elif leg_index == 0:
        leg_cities = cities[:stop_count] if stop_count is not None else cities
        leg_durations = durations[:len(leg_cities)]
    else:
        leg_cities, leg_durations = [], []

    route = [with_code(origin, airport_codes[0] if airport_codes else None)]
    for index, city in enumerate(leg_cities):
        if city:
            point = with_code(city, airport_codes[index + 1] if index + 1 < len(airport_codes) - 1 else None)
            duration = leg_durations[index] if index < len(leg_durations) else None
            if duration:
                point += f" ({html.escape(str(duration), quote=False)})"
            route.append(point)
    route.append(with_code(destination, airport_codes[-1] if airport_codes else None))

    times = change.get("flight_times")
    times = times if isinstance(times, list) else []
    outbound_stops = stops[0] if stops and isinstance(stops[0], int) else None
    if leg_index == 0:
        time_index = 0
    elif outbound_stops is not None:
        time_index = 2 * (outbound_stops + 1)
    else:
        time_index = 2 if len(times) >= 4 else -1
    departure_time = times[time_index] if 0 <= time_index < len(times) else None
    duration_values = change.get("flight_duration")
    if isinstance(duration_values, list):
        flight_duration = duration_values[leg_index] if leg_index < len(duration_values) else None
    else:
        flight_duration = duration_values if leg_index == 0 else None
    route_text = " → ".join(route)
    if flight_duration:
        duration_text = re.sub(r"\s*в\s+пути\s*$", "", str(flight_duration), flags=re.IGNORECASE)
        route_text += f" · {html.escape(duration_text, quote=False)} в пути"
    return route_text, html.escape(str(departure_time), quote=False) if departure_time else ""


def _accepts_keyword(function, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


_DELIVERY_MARKER_SEPARATOR = "\u2063"


def _encode_delivery_marker(delivery_key: str) -> str:
    """Keep an invisible, searchable marker in Telegram text for crash recovery."""
    return _DELIVERY_MARKER_SEPARATOR.join(delivery_key)


class TelegramBot:
    def __init__(
        self,
        client: TelegramClient,
        store: SQLiteStore,
        allowed_user_ids: frozenset[int],
        *,
        allowed_chat_ids: frozenset[int] = frozenset(),
        price_drop_threshold_percent: float = PRICE_DROP_THRESHOLD_PERCENT,
        deep_scan_max_destinations: int | None = None,
        deep_scan_retries: int = 0,
        deep_scan_retry_delay_seconds: float = 3.0,
        deep_scan_interval_minutes: int = 360,
    ):
        self.client = client
        self.store = store
        self.allowed_user_ids = allowed_user_ids
        self.allowed_chat_ids = allowed_chat_ids
        self.dialogs: dict[tuple[int, int], dict] = {}
        self.active_scans: set[tuple[int, int, str]] = set()
        self.scheduled_scan_lock = asyncio.Lock()
        self.scan_in_progress = False
        self.price_drop_threshold_percent = price_drop_threshold_percent
        self.deep_scan_max_destinations = deep_scan_max_destinations
        self.deep_scan_retries = deep_scan_retries
        self.deep_scan_retry_delay_seconds = deep_scan_retry_delay_seconds
        self.deep_scan_interval_minutes = deep_scan_interval_minutes

    @staticmethod
    def _scan_key(owner: tuple[int, int], origin: str) -> tuple[int, int, str]:
        return owner[0], owner[1], origin.upper()

    def _owner_allowed(self, user_id: int, chat_id: int) -> bool:
        return user_id in self.allowed_user_ids and (
            chat_id == user_id or chat_id in self.allowed_chat_ids
        )

    async def _edit_status(self, status, text: str, buttons=None) -> bool:
        """Best-effort update for a progress message.

        A Telegram edit can fail independently from the scan (for example
        after a deleted message or a transient connection error). The scan
        state must still be released and the original exception must not be
        replaced by a second failing edit in an error handler.
        """
        if status is None:
            return False
        try:
            await status.edit(text, buttons=buttons)
        except Exception as exc:
            log_monitoring(log, logging.WARNING, "⚠️ Telegram · не удалось обновить сообщение о ходе проверки")
            log.debug(
                "[DEBUG][TELEGRAM] Status update failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return False
        return True

    def register_handlers(self) -> None:
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_callback, events.CallbackQuery())

    def _owner(self, event) -> tuple[int, int] | None:
        user_id = event.sender_id
        chat_id = event.chat_id
        if user_id is None or chat_id is None or not self._owner_allowed(user_id, chat_id):
            return None
        return user_id, chat_id

    def _home_buttons(self, has_settings: bool, enabled: bool = True) -> list[list[Button]]:
        if not has_settings:
            return [[Button.inline("Настроить мониторинг", _callback("menu:settings"))]]
        return [
            [Button.inline("Настройки", _callback("menu:settings")), Button.inline("Направления", _callback("menu:destinations:0"))],
            [Button.inline("Детали последнего скана", _callback("menu:details"))],
            [Button.inline("Запустить скан", _callback("menu:scan"))],
            [Button.inline("Глубокий скан активных", _callback("menu:deep"))],
            [Button.inline("История сканов", _callback("menu:history"))],
            [Button.inline("Выключить мониторинг" if enabled else "Включить мониторинг", _callback("monitor:toggle"))],
        ]

    def _home_text(self, user_id: int, chat_id: int) -> str:
        settings_list = self.store.list_settings(user_id, chat_id)
        if not settings_list:
            return "Мониторинг Hot Tickets\nНастройки ещё не заданы."
        lines = ["Мониторинг Hot Tickets"]
        for settings in settings_list:
            trip = "Туда" if settings.trip_type == "OW" else "Туда-обратно"
            lines.append(f"\nВылет: {settings.origin} · {trip}")
            if settings.trip_type == "RT":
                lines.append(f"Длительность: {settings.duration_min_days}–{settings.duration_max_days} дней")
            active_count = len(self.store.list_active_destinations(user_id, chat_id, settings.origin))
            lines.append(f"Активных направлений: {active_count}")
            lines.append(f"Интервал: {settings.interval_minutes} мин. · {'включён' if settings.enabled else 'выключен'}")
        return "\n".join(lines)

    async def _on_message(self, event) -> None:
        owner = self._owner(event)
        if owner is None:
            return
        user_id, chat_id = owner
        text = (event.raw_text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower() if text.startswith("/") else ""

        if command in {"/start", "/menu"}:
            settings = self.store.list_settings(user_id, chat_id)
            enabled = any(item.enabled for item in settings)
            await event.respond(
                self._home_text(user_id, chat_id),
                buttons=self._home_buttons(bool(settings), enabled),
            )
            return
        if command == "/settings":
            await self._begin_settings(event, owner)
            return
        if command == "/destinations":
            await self._show_destinations(event, owner, 0)
            return
        if command == "/details":
            await self._show_details(event, owner, 0)
            return
        if command == "/scan":
            await self._run_scan(event, owner)
            return
        if command == "/deep":
            await self._run_deep_scan(event, owner)
            return
        if command == "/history":
            await self._show_history(event, owner)
            return
        if command == "/cancel":
            self.dialogs.pop(owner, None)
            settings = self.store.list_settings(*owner)
            await event.respond("Настройка отменена.", buttons=self._home_buttons(bool(settings), any(item.enabled for item in settings)))
            return
        if command:
            return

        dialog = self.dialogs.get(owner)
        if dialog is None:
            return
        await self._consume_settings_input(event, owner, text, dialog)

    async def _begin_settings(self, event, owner: tuple[int, int], edit: bool = False) -> None:
        self.dialogs[owner] = {"step": "origin"}
        text = "Введите трёхбуквенный код города вылета, например MOW или LED."
        buttons = [[Button.inline("Отмена", _callback("setup:cancel"))]]
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def _consume_settings_input(self, event, owner: tuple[int, int], text: str, dialog: dict) -> None:
        if dialog["step"] == "origin":
            if not ORIGIN_RE.fullmatch(text):
                await event.respond("Нужен код из трёх латинских букв, например MOW.")
                return
            dialog["origin"] = text.upper()
            dialog["step"] = "trip"
            await event.respond(
                f"Вылет: {dialog['origin']}\nВыберите тип поездки:",
                buttons=[
                    [Button.inline("Туда", _callback("setup:trip:OW")), Button.inline("Туда-обратно", _callback("setup:trip:RT"))],
                    [Button.inline("Отмена", _callback("setup:cancel"))],
                ],
            )
            return
        if dialog["step"] == "duration":
            match = DURATION_RE.fullmatch(text)
            if not match:
                await event.respond("Введите два числа через запятую, например 3,10.")
                return
            low, high = (int(match.group(1)), int(match.group(2)))
            if not 1 <= low <= high <= 30:
                await event.respond("Диапазон должен удовлетворять 1 ≤ минимум ≤ максимум ≤ 30.")
                return
            dialog["duration_min_days"] = low
            dialog["duration_max_days"] = high
            await self._ask_interval(event, dialog)
            return
        if dialog["step"] == "interval":
            if not text.isdigit() or not 30 <= int(text) <= 1440:
                await event.respond("Интервал должен быть целым числом минут от 30 до 1440.")
                return
            await self._save_settings(event, owner, dialog, int(text))

    async def _ask_interval(self, event, dialog: dict, edit: bool = False) -> None:
        dialog["step"] = "interval"
        text = "Выберите интервал мониторинга или задайте число минут (по умолчанию 90):"
        buttons = [
            [Button.inline(f"{minutes} мин", _callback(f"setup:interval:{minutes}")) for minutes in (90, 180, 360)],
            [Button.inline("Другое значение", _callback("setup:interval:custom"))],
            [Button.inline("Отмена", _callback("setup:cancel"))],
        ]
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def _save_settings(
        self, event, owner: tuple[int, int], dialog: dict, interval: int, edit: bool = False
    ) -> None:
        settings = MonitorSettings(
            user_id=owner[0],
            chat_id=owner[1],
            origin=dialog["origin"],
            trip_type=dialog["trip_type"],
            interval_minutes=interval,
            duration_min_days=dialog.get("duration_min_days"),
            duration_max_days=dialog.get("duration_max_days"),
        )
        self.store.save_settings(settings)
        self.dialogs.pop(owner, None)
        text = "Настройки сохранены. Автоматический мониторинг включён; первый скан запустится скоро."
        buttons = self._home_buttons(True)
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def _show_destinations(
        self,
        event,
        owner: tuple[int, int],
        page: int,
        edit: bool = False,
        origin: str | None = None,
    ) -> None:
        settings_list = self.store.list_settings(*owner)
        settings = (
            self.store.get_settings(*owner, origin)
            if origin
            else (self.store.get_settings(*owner) if len(settings_list) == 1 else None)
        )
        if settings is None and len(settings_list) > 1:
            text = "Выберите город вылета:"
            buttons = [
                [Button.inline(item.origin, _callback(f"menu:destinations:{item.origin}:0"))]
                for item in settings_list
            ]
            buttons.append([Button.inline("В меню", _callback("menu:home"))])
        elif settings is None:
            text = "Сначала задайте город вылета в настройках."
            buttons = self._home_buttons(False)
        else:
            destinations = self.store.list_destinations(
                *owner, settings.origin, available_only=True
            )
            if not destinations:
                text = f"{settings.origin} · направлений пока нет. Запустите скан."
                buttons = [
                    [Button.inline("Запустить скан", _callback("menu:scan"))],
                    [Button.inline("В меню", _callback("menu:home"))],
                ]
            else:
                pages = (len(destinations) + DESTINATIONS_PER_PAGE - 1) // DESTINATIONS_PER_PAGE
                page = min(max(page, 0), pages - 1)
                visible = destinations[page * DESTINATIONS_PER_PAGE : (page + 1) * DESTINATIONS_PER_PAGE]
                text = f"{settings.origin} · направления · {page + 1}/{pages}\nНажмите, чтобы изменить статус."
                buttons = []
                for offset in range(0, len(visible), 2):
                    row = visible[offset : offset + 2]
                    buttons.append(
                        [
                            Button.inline(
                                f"{'🟢' if item.active else '🔴'} {_short(item.destination, 24)}"
                                f"{_format_price(item.last_known_price, item.last_known_currency)}",
                                _callback(
                                    f"dest:{item.destination_id}:{0 if item.active else 1}:{page}:{settings.origin}"
                                ),
                            )
                            for item in row
                        ]
                    )
                navigation = []
                if page > 0:
                    navigation.append(
                        Button.inline("‹", _callback(f"menu:destinations:{settings.origin}:{page - 1}"))
                    )
                if page + 1 < pages:
                    navigation.append(
                        Button.inline("›", _callback(f"menu:destinations:{settings.origin}:{page + 1}"))
                    )
                if navigation:
                    buttons.append(navigation)
                buttons.append([Button.inline("В меню", _callback("menu:home"))])
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    @staticmethod
    def _format_offer_detail(offer: dict) -> str:
        price = _format_price(offer.get("price"), offer.get("currency"))
        price = price.strip(" ·") if price else "цена неизвестна"
        departure = offer.get("departure_date_raw") or offer.get("departure_date") or "дата неизвестна"
        returning = offer.get("return_date_raw") or offer.get("return_date")
        dates = f"{departure} → {returning}" if returning else departure

        airlines = offer.get("airline") or []
        if isinstance(airlines, str):
            airlines = [airlines]
        airline_text = ", ".join(str(item) for item in airlines[:2]) or "авиакомпания не указана"

        stops = offer.get("stops")
        airports = offer.get("airports") or []
        def stop_text(value) -> str:
            if value == 0:
                return "прямой"
            if isinstance(value, int):
                return "1 пересадка" if value == 1 else f"{value} пересадки"
            return "пересадки не указаны"

        if isinstance(stops, list):
            labels = [stop_text(value) for value in stops[:2]]
            if labels and len(set(labels)) == 1:
                route_text = labels[0]
            elif labels:
                route_text = "туда: " + labels[0]
                if len(labels) > 1:
                    route_text += " · обратно: " + labels[1]
            else:
                route_text = "пересадки не указаны"
        elif stops == 0:
            route_text = "прямой"
        elif isinstance(stops, int):
            transfer_names = []
            if isinstance(airports, list) and len(airports) > 2:
                for airport in airports[1:-1][:2]:
                    if isinstance(airport, dict):
                        code = airport.get("code")
                        name = airport.get("name")
                        transfer_names.append(" ".join(str(item) for item in (code, name) if item))
            transfer_text = f" ({', '.join(transfer_names)})" if transfer_names else ""
            route_text = stop_text(stops) + transfer_text
        else:
            route_text = "пересадки не указаны"

        transfers = offer.get("transfers") or []
        if isinstance(transfers, str):
            transfers = [transfers]
        transfer_names = [str(item) for item in transfers if str(item).strip()]
        if transfer_names:
            route_text += " · через " + ", ".join(transfer_names[:2])

        duration = offer.get("flight_duration")
        if isinstance(duration, list):
            compact_durations = []
            for item in duration[:2]:
                cleaned = re.sub(r"[\u200a-\u200d\u2060]", "", str(item))
                cleaned = re.sub(r"\s+в пути", "", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s*/\s*прямой", "", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s*/\s*\d+\s+пересад\w*", "", cleaned, flags=re.IGNORECASE)
                compact_durations.append(_short(cleaned.strip(), 18))
            duration = " / ".join(compact_durations)
        elif duration:
            duration = re.sub(r"\s+в пути", "", str(duration), flags=re.IGNORECASE)
            duration = re.sub(r"\s*/\s*прямой", "", duration, flags=re.IGNORECASE).strip()
            duration = re.sub(r"\s*/\s*\d+\s+пересад\w*", "", duration, flags=re.IGNORECASE).strip()
        suffix = f" · {duration}" if duration else ""
        return f"{dates} · {airline_text} · {route_text}{suffix}\n{price}"

    async def _show_details(
        self,
        event,
        owner: tuple[int, int],
        page: int,
        edit: bool = False,
        origin: str | None = None,
    ) -> None:
        settings_list = self.store.list_settings(*owner)
        settings = (
            self.store.get_settings(*owner, origin)
            if origin
            else (self.store.get_settings(*owner) if len(settings_list) == 1 else None)
        )
        if settings is None and len(settings_list) > 1:
            text = "Выберите город вылета для деталей:"
            buttons = [
                [Button.inline(item.origin, _callback(f"menu:details:{item.origin}:0"))]
                for item in settings_list
            ]
            buttons.append([Button.inline("В меню", _callback("menu:home"))])
        elif settings is None:
            text = "Сначала задайте город вылета в настройках."
            buttons = self._home_buttons(False)
        else:
            offers = self.store.get_latest_scan_offers(*owner, settings.origin)
            grouped: dict[str, list[dict]] = {}
            for offer in offers:
                destination = offer.get("destination")
                if isinstance(destination, str) and destination.strip():
                    grouped.setdefault(" ".join(destination.split()), []).append(offer)
            groups = sorted(grouped.items(), key=lambda item: item[0].casefold())
            if not groups:
                text = f"{settings.origin} · подробных результатов пока нет. Запустите deep scan."
                buttons = [
                    [Button.inline("Запустить deep scan", _callback(f"menu:deep:{settings.origin}"))],
                    [Button.inline("В меню", _callback("menu:home"))],
                ]
            else:
                pages = (len(groups) + DETAILS_DESTINATIONS_PER_PAGE - 1) // DETAILS_DESTINATIONS_PER_PAGE
                page = min(max(page, 0), pages - 1)
                visible = groups[
                    page * DETAILS_DESTINATIONS_PER_PAGE : (page + 1) * DETAILS_DESTINATIONS_PER_PAGE
                ]
                lines = [f"{settings.origin} · детали последнего скана · {page + 1}/{pages}", ""]
                for destination, destination_offers in visible:
                    ordered = sorted(
                        destination_offers,
                        key=lambda item: item.get("price") if isinstance(item.get("price"), int) else 10**18,
                    )[:DETAILS_OFFERS_PER_DESTINATION]
                    lines.append(f"{_short(destination, 34)} · {len(destination_offers)} вариантов")
                    for offer in ordered:
                        lines.append(f"  {self._format_offer_detail(offer)}")
                    lines.append("")
                text = "\n".join(lines).rstrip()
                buttons = []
                navigation = []
                if page > 0:
                    navigation.append(Button.inline("‹", _callback(f"menu:details:{settings.origin}:{page - 1}")))
                if page + 1 < pages:
                    navigation.append(Button.inline("›", _callback(f"menu:details:{settings.origin}:{page + 1}")))
                if navigation:
                    buttons.append(navigation)
                buttons.extend(
                    [
                        [Button.inline("Направления", _callback(f"menu:destinations:{settings.origin}:0"))],
                        [Button.inline("В меню", _callback("menu:home"))],
                    ]
                )
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def _show_history(self, event, owner: tuple[int, int], edit: bool = False) -> None:
        scans = self.store.list_scans(*owner, limit=HISTORY_SCAN_LIMIT)
        if not scans:
            text = "История сканов пока пуста."
        else:
            lines = ["История последних сканов", ""]
            for scan in scans:
                state = "OK" if scan.complete else "ошибка"
                lines.append(
                    f"#{scan.scan_id} · {scan.finished_at[:16].replace('T', ' ')} · {state} · "
                    f"{scan.cards_found} карточек · {scan.unique_destinations} направлений"
                )
                if scan.warnings:
                    lines.append(f"  предупреждений: {len(scan.warnings)}")
            text = "\n".join(lines)
        buttons = [
            [Button.inline("Направления", _callback("menu:destinations:0"))],
            [Button.inline("В меню", _callback("menu:home"))],
        ]
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def _run_scan(self, event, owner: tuple[int, int], edit: bool = False) -> None:
        settings_list = [item for item in self.store.list_settings(*owner) if item.enabled]
        if not settings_list:
            if edit:
                await event.answer()
                await event.edit("Сначала настройте мониторинг или включите его снова.")
            else:
                await event.respond("Сначала настройте мониторинг или включите его снова.")
            return
        keys = {self._scan_key(owner, item.origin) for item in settings_list}
        if self.active_scans.intersection(keys):
            if edit:
                await event.answer("Скан уже выполняется.")
            else:
                await event.respond("Сейчас уже выполняется другой скан.")
            return

        self.scan_in_progress = True
        self.active_scans.update(keys)
        status = None
        active_origin = None
        try:
            for settings in settings_list:
                active_origin = settings.origin
                log_monitoring(
                    log,
                    logging.INFO,
                    "🔎 %s → %s · начинаю проверку",
                    settings.origin,
                    format_trip_type(settings.trip_type),
                )
            if edit:
                await event.answer()
                status = await event.edit("Сканирую Aviasales. Обычно это занимает несколько десятков секунд…")
            else:
                status = await event.respond("Сканирую Aviasales. Обычно это занимает несколько десятков секунд…")
            summaries = []
            incomplete = False
            latest = self.store.get_settings(*owner)
            for settings in settings_list:
                scan_options = {} if len(settings_list) == 1 and latest is not None and latest.origin == settings.origin else {"origin": settings.origin}
                outcome = await scan_and_persist(
                    self.store, *owner, price_drop_threshold_percent=self.price_drop_threshold_percent,
                    **scan_options,
                )
                result = outcome.collector_result
                await self._deliver_pending_price_notifications(owner)
                if result.get("complete"):
                    new_count = len(outcome.stored_scan.new_destinations)
                    log_monitoring(
                        log,
                        logging.INFO,
                        "✅ %s · проверка завершена · %s направлений · новых направлений: %s",
                        settings.origin,
                        result.get("unique_destinations", 0),
                        new_count,
                    )
                    change_text = (
                        f"новых: {new_count}"
                        if new_count
                        else "ничего нового"
                    )
                    summaries.append(
                        f"{settings.origin}: {result.get('unique_destinations', 0)} направлений · {change_text}"
                    )
                else:
                    incomplete = True
                    warnings = result.get("warnings", [])
                    log_monitoring(
                        log,
                        logging.WARNING,
                        "⚠️ %s · проверка не завершена · %s",
                        settings.origin,
                        summarize_warnings(warnings),
                    )
                    log.debug("[DEBUG][SCAN] Incomplete origin=%s warnings=%r", settings.origin, warnings)
                    summaries.append(f"{settings.origin}: не завершён")
            if incomplete:
                text = "Скан завершён частично:\n" + "\n".join(summaries) + "\nПодробнее: /destinations"
                buttons = [[Button.inline("Повторить скан", _callback("menu:scan"))]]
            else:
                text = "Проверка проведена:\n" + "\n".join(summaries) + "\nПодробнее: /destinations"
                buttons = []
            buttons.append([Button.inline("Направления", _callback("menu:destinations:0"))])
            buttons.append([Button.inline("В меню", _callback("menu:home"))])
            await self._edit_status(status, text, buttons=buttons)
        except ScanConfigurationError:
            settings = self.store.list_settings(*owner)
            if status is not None:
                await self._edit_status(
                    status,
                    "Сначала настройте мониторинг или включите его снова.",
                    buttons=self._home_buttons(bool(settings), any(item.enabled for item in settings)),
                )
        except Exception as exc:
            log_monitoring_exception(
                log,
                message="❌ %s · ошибка проверки: %s",
                error=exc,
                debug_context="SCAN",
                args=(active_origin or "Скан",),
            )
            if status is not None:
                await self._edit_status(status, "Не удалось выполнить скан. Подробности записаны в журнал приложения.")
        finally:
            self.active_scans.difference_update(keys)
            self.scan_in_progress = bool(self.active_scans)

    async def _run_deep_scan(
        self,
        event,
        owner: tuple[int, int],
        edit: bool = False,
        origin: str | None = None,
    ) -> None:
        settings_list = [item for item in self.store.list_settings(*owner) if item.enabled]
        if not settings_list:
            if edit:
                await event.answer()
                await event.edit("Сначала настройте мониторинг или включите его снова.")
            else:
                await event.respond("Сначала настройте мониторинг или включите его снова.")
            return
        if origin is None and len(settings_list) > 1:
            buttons = [
                [Button.inline(item.origin, _callback(f"menu:deep:{item.origin}"))]
                for item in settings_list
            ]
            buttons.append([Button.inline("В меню", _callback("menu:home"))])
            if edit:
                await event.answer()
                await event.edit("Выберите город для deep scan:", buttons=buttons)
            else:
                await event.respond("Выберите город для deep scan:", buttons=buttons)
            return
        settings = self.store.get_settings(*owner, origin or settings_list[0].origin)
        if settings is None or not settings.enabled:
            if edit:
                await event.answer("Настройка не найдена.", alert=True)
            else:
                await event.respond("Настройка не найдена.")
            return
        key = self._scan_key(owner, settings.origin)
        if key in self.active_scans:
            if edit:
                await event.answer("Скан уже выполняется.")
            else:
                await event.respond("Сейчас уже выполняется другой скан.")
            return

        self.scan_in_progress = True
        self.active_scans.add(key)
        status = None
        progress_futures = []
        try:
            log_monitoring(
                log,
                logging.INFO,
                "🔎 %s → %s · начинаю глубокую проверку",
                settings.origin,
                format_trip_type(settings.trip_type),
            )
            if edit:
                await event.answer()
                status = await event.edit("Глубоко проверяю активные направления…")
            else:
                status = await event.respond("Глубоко проверяю активные направления…")

            loop = asyncio.get_running_loop()

            def report_progress(update: dict) -> None:
                if status is None or loop.is_closed():
                    return
                text = self._deep_progress_text(settings.origin, update)
                try:
                    future = asyncio.run_coroutine_threadsafe(self._edit_status(status, text), loop)
                except RuntimeError:
                    return
                progress_futures.append(future)

            deep_options = {}
            if self.deep_scan_max_destinations is not None:
                deep_options["max_destinations"] = self.deep_scan_max_destinations
            if self.deep_scan_retries:
                deep_options["retries"] = self.deep_scan_retries
            if self.deep_scan_retry_delay_seconds != 3.0:
                deep_options["retry_delay_seconds"] = self.deep_scan_retry_delay_seconds
            if len(settings_list) > 1:
                deep_options["origin"] = settings.origin
            if _accepts_keyword(deep_scan_and_persist, "progress_callback"):
                deep_options["progress_callback"] = report_progress
            deep_options["price_drop_threshold_percent"] = self.price_drop_threshold_percent
            outcome = await deep_scan_and_persist(self.store, *owner, **deep_options)
            for future in progress_futures:
                try:
                    await asyncio.wrap_future(future)
                except Exception as exc:
                    log_monitoring(
                        log,
                        logging.WARNING,
                        "⚠️ %s · не удалось обновить ход глубокой проверки",
                        settings.origin,
                    )
                    log.debug(
                        "[DEBUG][DEEP-SCAN] Progress update failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            result = outcome.collector_result
            scanned = int(result.get("destinations_scanned") or 0)
            requested = int(result.get("destinations_requested") or len(outcome.active_destinations))
            offers_count = int(result.get("cards_found") or 0)
            if not result.get("complete"):
                warnings = result.get("warnings", [])
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ %s · глубокая проверка не завершена · проверено %s из %s · %s",
                    settings.origin,
                    scanned,
                    requested,
                    summarize_warnings(warnings),
                )
                log.debug("[DEBUG][DEEP-SCAN] Incomplete origin=%s warnings=%r", settings.origin, warnings)
                text = (
                    "Глубокая проверка не завершена.\n"
                    f"Проверено направлений: {scanned}/{requested}.\n"
                    "Каталог цен не изменён."
                )
                buttons = [[Button.inline("Повторить deep scan", _callback("menu:deep"))]]
            else:
                drops = [item for item in outcome.stored_scan.price_findings if item.get("notification_queued")]
                text = (
                    f"Проверка проведена: {settings.origin}.\n"
                    f"Проверено направлений: {scanned}/{requested}.\n"
                    f"Результатов: {offers_count}.\n"
                    f"Изменений цен: {len(outcome.stored_scan.price_changes)}."
                )
                if result.get("partial"):
                    text += "\nПроверка частичная: отдельные направления временно недоступны."
                if outcome.skipped_destinations:
                    text += (
                        f"\nПропущено направлений: {len(outcome.skipped_destinations)} "
                        "из-за установленного лимита."
                    )
                sent_notifications = await self._deliver_pending_price_notifications(owner)
                if drops:
                    text += (
                        f"\nСущественных снижений: {len(drops)}. "
                        f"Уведомлений отправлено сейчас: {sent_notifications}."
                    )
                buttons = [[Button.inline("Детали результатов", _callback(f"menu:details:{settings.origin}:0"))]]
                log_monitoring(
                    log,
                    logging.INFO,
                    "✅ %s · глубокая проверка завершена · проверено %s направлений · "
                    "%s результатов · изменений цены: %s · Telegram-уведомлений в очереди: %s",
                    settings.origin,
                    scanned,
                    offers_count,
                    len(outcome.stored_scan.price_changes),
                    len(drops),
                )
            buttons.extend(
                [
                    [Button.inline("Направления", _callback("menu:destinations:0"))],
                    [Button.inline("В меню", _callback("menu:home"))],
                ]
            )
            await self._edit_status(status, text, buttons=buttons)
        except DeepScanConfigurationError as exc:
            if status is not None:
                await self._edit_status(
                    status,
                    str(exc).capitalize() + ".",
                    buttons=self._home_buttons(True, True),
                )
        except Exception as exc:
            log_monitoring_exception(
                log,
                message="❌ %s · ошибка глубокой проверки: %s",
                error=exc,
                debug_context="DEEP-SCAN",
                args=(settings.origin,),
            )
            if status is not None:
                await self._edit_status(
                    status,
                    "Не удалось выполнить глубокий скан. Подробности записаны в журнал приложения.",
                )
        finally:
            self.active_scans.discard(key)
            self.scan_in_progress = bool(self.active_scans)

    @staticmethod
    def _deep_progress_text(origin: str, update: dict) -> str:
        current = int(update.get("current") or 0)
        total = int(update.get("total") or 0)
        destination = update.get("destination") or "направление"
        offers = int(update.get("offers") or 0)
        if update.get("status") == "started":
            return (
                f"Проверяю {origin}: {current}/{total}\n"
                f"Направление: {destination}\n"
                f"Собрано ранее: {offers} · загружаю варианты…"
            )
        if update.get("status") == "failed":
            return (
                f"Проверено {origin}: {current}/{total}\n"
                f"Не удалось: {destination}\n"
                f"Результатов всего: {offers}"
            )
        if update.get("status") == "retrying":
            return (
                f"Проверяю {origin}: {current}/{total}\n"
                f"Повторяю направление: {destination}\n"
                f"Результатов всего: {offers}"
            )
        return (
            f"Проверено {origin}: {current}/{total}\n"
            f"Последнее: {destination}\n"
            f"Результатов всего: {offers}"
        )

    async def run_scheduled_scan(self, settings: MonitorSettings) -> bool | ScanDisposition:
        owner = (settings.user_id, settings.chat_id)
        if not self._owner_allowed(*owner):
            log_monitoring(log, logging.WARNING, "⚠️ Планировщик пропустил проверку: чат не разрешён")
            return False
        key = self._scan_key(owner, settings.origin)
        if key in self.active_scans:
            log.debug("[SCHEDULER] Defer overlapping scan user=%s chat=%s origin=%s", *owner, settings.origin)
            return ScanDisposition.DEFERRED

        await self.scheduled_scan_lock.acquire()
        if key in self.active_scans:
            self.scheduled_scan_lock.release()
            log.debug("[SCHEDULER] Defer active manual scan user=%s chat=%s origin=%s", *owner, settings.origin)
            return ScanDisposition.DEFERRED

        started_at = asyncio.get_running_loop().time()
        scan_status = "failed"
        cards_found = 0
        self.scan_in_progress = True
        self.active_scans.add(key)
        try:
            log_monitoring(
                log,
                logging.INFO,
                "🔎 %s → %s · начинаю проверку",
                settings.origin,
                format_trip_type(settings.trip_type),
            )
            latest = self.store.get_settings(*owner)
            scan_options = {} if latest is not None and latest.origin == settings.origin else {"origin": settings.origin}
            outcome = await scan_and_persist(
                self.store, *owner, price_drop_threshold_percent=self.price_drop_threshold_percent,
                **scan_options,
            )
            result = outcome.collector_result
            cards_found = int(result.get("cards_found") or 0)
            if not result.get("complete"):
                scan_status = "incomplete"
                warnings = result.get("warnings", [])
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ %s · проверка не завершена · %s",
                    settings.origin,
                    summarize_warnings(warnings),
                )
                log.debug("[DEBUG][SCAN] Incomplete scheduled origin=%s warnings=%r", settings.origin, warnings)
                return False

            scan_status = "complete"
            log_monitoring(
                log,
                logging.INFO,
                "✅ %s · проверка завершена · %s направлений · новых направлений: %s",
                settings.origin,
                result.get("unique_destinations", 0),
                len(outcome.stored_scan.new_destinations),
            )
            await self._deliver_pending_price_notifications(owner)
        except ScanConfigurationError:
            log_monitoring(log, logging.INFO, "⏸️ %s · проверка пропущена: мониторинг отключён", settings.origin)
            return False
        except Exception as exc:
            log_monitoring_exception(
                log,
                message="❌ %s · ошибка проверки: %s",
                error=exc,
                debug_context="SCAN",
                args=(settings.origin,),
            )
            return False
        finally:
            self.active_scans.discard(key)
            self.scan_in_progress = bool(self.active_scans)
            elapsed = asyncio.get_running_loop().time() - started_at
            if scan_status != "complete":
                log.debug(
                    "[DEBUG][SCAN] Finished scheduled origin=%s status=%s cards=%s elapsed=%.1fs",
                    settings.origin, scan_status, cards_found, elapsed,
                )
            self.scheduled_scan_lock.release()
        return True

    async def run_scheduled_deep_scan(self, settings: MonitorSettings) -> bool | ScanDisposition:
        owner = (settings.user_id, settings.chat_id)
        if not self._owner_allowed(*owner):
            log_monitoring(log, logging.WARNING, "⚠️ Планировщик пропустил глубокую проверку: чат не разрешён")
            return False
        key = self._scan_key(owner, settings.origin)
        if key in self.active_scans:
            log.debug("[DEEP-SCHEDULER] Defer overlapping scan origin=%s", settings.origin)
            return ScanDisposition.DEFERRED

        await self.scheduled_scan_lock.acquire()
        if key in self.active_scans:
            self.scheduled_scan_lock.release()
            log.debug("[DEEP-SCHEDULER] Defer active manual scan origin=%s", settings.origin)
            return ScanDisposition.DEFERRED

        started_at = asyncio.get_running_loop().time()
        scan_status = "failed"
        offers_found = 0
        self.scan_in_progress = True
        self.active_scans.add(key)
        try:
            log_monitoring(log, logging.INFO, "🔎 %s · начинаю глубокую проверку", settings.origin)
            deep_options = {
                "origin": settings.origin,
                "max_destinations": self.deep_scan_max_destinations,
                "retries": self.deep_scan_retries,
                "retry_delay_seconds": self.deep_scan_retry_delay_seconds,
                "price_drop_threshold_percent": self.price_drop_threshold_percent,
            }
            outcome = await deep_scan_and_persist(self.store, *owner, **deep_options)
            result = outcome.collector_result
            offers_found = int(result.get("cards_found") or 0)
            if not result.get("complete"):
                scan_status = "incomplete"
                warnings = result.get("warnings", [])
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ %s · глубокая проверка не завершена · %s",
                    settings.origin,
                    summarize_warnings(warnings),
                )
                log.debug("[DEBUG][DEEP-SCAN] Incomplete origin=%s warnings=%r", settings.origin, warnings)
                return False
            if not result.get("batch_complete", True):
                scan_status = "partial"
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ %s · глубокая проверка неполная · проверено %s из %s · будет повтор",
                    settings.origin,
                    result.get("destinations_scanned", 0),
                    len(outcome.active_destinations),
                )
                return False
            drops = [item for item in outcome.stored_scan.price_findings if item.get("notification_queued")]
            scan_status = "complete"
            log_monitoring(
                log,
                logging.INFO,
                "✅ %s · глубокая проверка завершена · %s результатов · изменений цены: %s · "
                "Telegram-уведомлений в очереди: %s",
                settings.origin,
                offers_found,
                len(outcome.stored_scan.price_changes),
                len(drops),
            )
            await self._deliver_pending_price_notifications(owner)
        except DeepScanConfigurationError as exc:
            log_monitoring(log, logging.INFO, "⏸️ %s · глубокая проверка пропущена", settings.origin)
            log.debug("[DEBUG][DEEP-SCHEDULER] Skipped origin=%s reason=%s", settings.origin, exc)
            return False
        except Exception as exc:
            log_monitoring_exception(
                log,
                message="❌ %s · ошибка глубокой проверки: %s",
                error=exc,
                debug_context="DEEP-SCHEDULER",
                args=(settings.origin,),
            )
            return False
        finally:
            self.active_scans.discard(key)
            self.scan_in_progress = bool(self.active_scans)
            elapsed = asyncio.get_running_loop().time() - started_at
            if scan_status != "complete":
                log.debug(
                    "[DEBUG][DEEP-SCHEDULER] Finished origin=%s status=%s offers=%s elapsed=%.1fs",
                    settings.origin,
                    scan_status,
                    offers_found,
                    elapsed,
                )
            self.scheduled_scan_lock.release()
        return True

    async def start_web_deep_scan(self, origin: str) -> bool:
        for user_id in self.allowed_user_ids:
            settings = self.store.get_settings(user_id, user_id, origin)
            if settings is None or not settings.enabled:
                continue
            key = self._scan_key((user_id, user_id), settings.origin)
            if key in self.active_scans:
                return False
            asyncio.create_task(self.run_scheduled_deep_scan(settings), name=f"web-deep-{settings.origin}")
            return True
        return False

    async def _deliver_pending_price_notifications(self, owner: tuple[int, int]) -> int:
        claimed = self.store.claim_pending_price_notifications(*owner, limit=AUTO_NOTIFICATION_DESTINATIONS)
        sent = 0
        for item in claimed:
            notification_id = int(item["notification_id"])
            route = f"{item.get('origin') or '?'} → {item.get('destination') or '?'}"
            try:
                response = await self._send_price_alert(owner, item)
                message_id = getattr(response, "id", None)
                if isinstance(message_id, int):
                    self.store.mark_price_notification_sent(notification_id, message_id)
                    log_monitoring(log, logging.INFO, "📨 Telegram · уведомление доставлено · %s", route)
                    log.debug(
                        "[DEBUG][TELEGRAM-OUTBOX] Sent notification=%s finding=%s message=%s",
                        notification_id, item.get("finding_id"), message_id,
                    )
                    sent += 1
                else:
                    self.store.mark_price_notification_uncertain(
                        notification_id, "Telegram send returned without a message id; outcome is uncertain"
                    )
                    log_monitoring(
                        log,
                        logging.WARNING,
                        "⚠️ Telegram · подтверждение доставки не получено · %s",
                        route,
                    )
                    log.debug(
                        "[DEBUG][TELEGRAM-OUTBOX] Uncertain notification=%s; no automatic resend",
                        notification_id,
                    )
            except RPCError as exc:
                # A Telegram RPC error is an explicit server rejection; it is
                # safe to retry using the bounded backoff state.
                self.store.mark_price_notification_failed(notification_id, f"{type(exc).__name__}: {exc}")
                log_monitoring_exception(
                    log,
                    message="❌ Telegram отклонил уведомление · %s · %s",
                    error=exc,
                    debug_context="TELEGRAM-OUTBOX",
                    args=(route,),
                )
            except Exception as exc:
                self.store.mark_price_notification_uncertain(notification_id, f"{type(exc).__name__}: {exc}")
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ Telegram · результат отправки неизвестен · %s",
                    route,
                )
                log.debug(
                    "[DEBUG][TELEGRAM-OUTBOX] Delivery outcome uncertain notification=%s attempt=%s",
                    notification_id, item.get("attempt"), exc_info=(type(exc), exc, exc.__traceback__),
                )
        await self._reconcile_uncertain_price_notifications(owner)
        return sent

    async def _reconcile_uncertain_price_notifications(self, owner: tuple[int, int]) -> None:
        uncertain = self.store.list_uncertain_price_notifications(*owner)
        if not uncertain:
            return
        get_messages = getattr(self.client, "get_messages", None)
        if not callable(get_messages):
            return
        try:
            messages = await get_messages(owner[1], limit=500)
        except Exception as exc:
            log_monitoring_exception(
                log,
                message="⚠️ Telegram · не удалось сверить доставку: %s",
                error=exc,
                debug_context="TELEGRAM-OUTBOX",
            )
            return
        messages = messages if isinstance(messages, (list, tuple)) else [messages]
        for item in uncertain:
            delivery_key = str(item.get("delivery_key") or "")
            if not delivery_key:
                continue
            for message in messages:
                body = str(getattr(message, "message", None) or getattr(message, "raw_text", None) or "")
                if delivery_key in body.replace(_DELIVERY_MARKER_SEPARATOR, ""):
                    message_id = getattr(message, "id", None)
                    self.store.mark_price_notification_sent(
                        int(item["notification_id"]), message_id if isinstance(message_id, int) else None
                    )
                    log_monitoring(
                        log,
                        logging.INFO,
                        "📨 Telegram · доставка подтверждена по истории сообщений",
                    )
                    log.debug(
                        "[DEBUG][TELEGRAM-OUTBOX] Reconciled notification=%s message=%s",
                        item["notification_id"], message_id,
                    )
                    break

    async def retry_notification_outbox(self, stop_event: asyncio.Event) -> None:
        """Retry durable Telegram notifications after startup and transient errors."""
        while not stop_event.is_set():
            try:
                for owner in self.store.pending_price_notification_owners():
                    if self._owner_allowed(*owner):
                        await self._deliver_pending_price_notifications(owner)
            except Exception as exc:
                log_monitoring_exception(
                    log,
                    message="⚠️ Telegram · ошибка обработки очереди уведомлений: %s",
                    error=exc,
                    debug_context="TELEGRAM-OUTBOX",
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def _send_price_alert(self, owner: tuple[int, int], change: dict):
        origin = str(change.get("origin") or "").upper()
        origin_name = self.store.get_origin_label(*owner, origin) if origin else None
        origin_name = origin_name or {"MOW": "Москва", "LED": "Санкт-Петербург"}.get(origin, origin or "Город вылета")
        destination_name = str(change.get("destination") or "Направление")
        destination = html.escape(destination_name, quote=False)
        currency = change.get("currency") or change.get("previous_currency")
        current = _format_alert_price(change.get("price"), currency)
        reference_price = change.get("reference_price")
        if not isinstance(reference_price, (int, float)):
            reference_price = change.get("previous_price")
        previous = _format_alert_price(reference_price, change.get("previous_currency") or currency)
        change_percent = change.get("reference_drop_percent")
        if not isinstance(change_percent, (int, float)):
            change_percent = abs(change.get("change_percent")) if isinstance(change.get("change_percent"), (int, float)) else None
        percent_text = f"−{round(abs(change_percent))}%" if isinstance(change_percent, (int, float)) else ""
        details = [
            f"🔥 <b>{html.escape(origin_name, quote=False)} → {destination} — {current}</b>",
            f"<s>{previous}</s> <b>{percent_text}</b>".strip(),
        ]
        departure = _alert_date(change.get("departure_date_raw"), change.get("departure_date"))
        outbound_route, _ = _alert_leg(change, 0, origin_name, destination_name)
        outbound_label = f"✈️ <b>Туда:</b> {departure or 'дата не указана'}"
        details.extend((outbound_label, outbound_route))
        is_round_trip = change.get("return_date") or change.get("return_date_raw") or (
            isinstance(change.get("stops"), list) and len(change["stops"]) > 1
        )
        if is_round_trip:
            return_date = _alert_date(change.get("return_date_raw"), change.get("return_date"))
            return_route, _ = _alert_leg(change, 1, destination_name, origin_name)
            return_label = f"↩️ <b>Обратно:</b> {return_date or 'дата не указана'}"
            details.extend((return_label, return_route))
        source_url = _safe_aviasales_url(change.get("source_url"))
        if source_url:
            details.append(f'👉 <a href="{html.escape(source_url, quote=True)}"><b>Открыть билет</b></a>')
        notification_id = int(change["notification_id"])
        delivery_key = str(change.get("delivery_key") or f"HT-{notification_id}")
        details.append(html.escape(_encode_delivery_marker(delivery_key)))
        message_text = "\n".join(details)
        # Persist the reconciliation marker before making the irreversible API
        # call. If the process dies after Telegram ACK, the lease recovery path
        # can search chat history and mark this exact notification sent.
        self.store.prepare_price_notification(notification_id, delivery_key, message_text)
        return await self.client.send_message(
            owner[1], message_text, parse_mode="html", link_preview=False
        )

    async def _on_callback(self, event) -> None:
        owner = self._owner(event)
        if owner is None:
            if event.sender_id is not None:
                await event.answer("Нет доступа.", alert=True)
            return
        if event.data is None:
            return
        try:
            parts = event.data.decode("ascii").split(":")
        except (UnicodeDecodeError, AttributeError):
            await event.answer("Неизвестное действие.", alert=True)
            return

        if parts == ["menu", "home"]:
            await event.answer()
            settings = self.store.get_settings(*owner)
            await event.edit(
                self._home_text(*owner),
                buttons=self._home_buttons(settings is not None, settings.enabled if settings else False),
            )
            return
        if parts == ["menu", "settings"]:
            await event.answer()
            await self._begin_settings(event, owner, edit=True)
            return
        if parts == ["menu", "details"]:
            await event.answer()
            await self._show_details(event, owner, 0, edit=True)
            return
        if len(parts) == 4 and parts[:2] == ["menu", "details"]:
            origin, page_text = parts[2].upper(), parts[3]
            if not page_text.isdigit():
                await event.answer("Некорректная страница.", alert=True)
                return
            await event.answer()
            await self._show_details(event, owner, int(page_text), edit=True, origin=origin)
            return
        if len(parts) in {3, 4} and parts[:2] == ["menu", "destinations"]:
            origin = None
            page_text = parts[2]
            if len(parts) == 4:
                origin, page_text = parts[2].upper(), parts[3]
            if not page_text.isdigit():
                await event.answer("Некорректная страница.", alert=True)
                return
            await event.answer()
            await self._show_destinations(event, owner, int(page_text), edit=True, origin=origin)
            return
        if parts == ["menu", "scan"]:
            await self._run_scan(event, owner, edit=True)
            return
        if parts == ["menu", "deep"]:
            await self._run_deep_scan(event, owner, edit=True)
            return
        if len(parts) == 3 and parts[:2] == ["menu", "deep"]:
            await self._run_deep_scan(event, owner, edit=True, origin=parts[2].upper())
            return
        if parts == ["menu", "history"]:
            await event.answer()
            await self._show_history(event, owner, edit=True)
            return
        if parts == ["monitor", "toggle"]:
            settings = self.store.list_settings(*owner)
            if not settings:
                await event.answer("Сначала настройте мониторинг.", alert=True)
                return
            enabled = not any(item.enabled for item in settings)
            self.store.set_settings_enabled(*owner, enabled)
            await event.answer("Мониторинг включён." if enabled else "Мониторинг выключен.")
            await event.edit(self._home_text(*owner), buttons=self._home_buttons(True, enabled))
            return
        if parts == ["setup", "cancel"]:
            self.dialogs.pop(owner, None)
            await event.answer("Настройка отменена.")
            settings = self.store.get_settings(*owner)
            await event.edit(
                self._home_text(*owner),
                buttons=self._home_buttons(settings is not None, settings.enabled if settings else False),
            )
            return
        if len(parts) == 3 and parts[:2] == ["setup", "trip"]:
            dialog = self.dialogs.get(owner)
            if dialog is None or dialog.get("step") != "trip" or parts[2] not in {"OW", "RT"}:
                await event.answer("Этот шаг настройки уже не активен.", alert=True)
                return
            dialog["trip_type"] = parts[2]
            if parts[2] == "RT":
                dialog["step"] = "duration"
                await event.answer()
                await event.edit(
                    "Введите минимальную и максимальную длительность поездки в днях, например 3,10.",
                    buttons=[[Button.inline("Отмена", _callback("setup:cancel"))]],
                )
            else:
                await event.answer()
                await self._ask_interval(event, dialog, edit=True)
            return
        if len(parts) == 3 and parts[:2] == ["setup", "interval"]:
            dialog = self.dialogs.get(owner)
            if dialog is None or dialog.get("step") != "interval":
                await event.answer("Этот шаг настройки уже не активен.", alert=True)
                return
            if parts[2] == "custom":
                dialog["step"] = "interval"
                await event.answer()
                await event.edit(
                    "Введите интервал целым числом минут от 30 до 1440.",
                    buttons=[[Button.inline("Отмена", _callback("setup:cancel"))]],
                )
                return
            if not parts[2].isdigit() or not 30 <= int(parts[2]) <= 1440:
                await event.answer("Некорректный интервал.", alert=True)
                return
            await event.answer("Настройки сохранены.")
            await self._save_settings(event, owner, dialog, int(parts[2]), edit=True)
            return
        if len(parts) in {4, 5} and parts[0] == "dest":
            try:
                destination_id, active, page = int(parts[1]), int(parts[2]), int(parts[3])
                if active not in {0, 1} or page < 0:
                    raise ValueError
            except ValueError:
                await event.answer("Некорректное действие.", alert=True)
                return
            changed = self.store.set_destination_active(*owner, destination_id, bool(active))
            await event.answer("Статус обновлён." if changed else "Направление не найдено.")
            origin = parts[4].upper() if len(parts) == 5 else None
            await self._show_destinations(event, owner, page, edit=True, origin=origin)
            return

        await event.answer("Неизвестное действие.", alert=True)


async def _run() -> None:
    configure_utf8_console()
    logging_kwargs = dict(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if sys.stderr is None:
        logging.basicConfig(handlers=[logging.NullHandler()], **logging_kwargs)
    else:
        logging.basicConfig(**logging_kwargs)
    monitoring_formatter = MonitoringFormatter(
        "[%(asctime)s] %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(monitoring_formatter)
    load_dotenv()
    config = TelegramBotConfig.from_env()
    log_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HotTicketsBot"
    log_root.mkdir(parents=True, exist_ok=True)
    default_log_path = log_root / "bot-runtime.log"
    log_path = Path(os.environ.get("HOT_TICKETS_LOG_FILE", str(default_log_path))).resolve()
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(monitoring_formatter)
    logging.getLogger().addHandler(file_handler)
    logging.getLogger("telethon.client.updates").addFilter(_TelethonClusterWarningFilter())
    lock = BotInstanceLock(config.lock_path)
    lock.acquire()
    try:
        if config.browser_data_dir:
            os.environ.setdefault("AVIASALES_BROWSER_DATA_DIR", config.browser_data_dir)
        client = create_telegram_client(config)
        store = SQLiteStore(config.database_path)
    except Exception:
        lock.release()
        raise
    stop_scheduler = asyncio.Event()
    bot = None
    scheduler_task = None
    outbox_task = None
    stop_outbox = asyncio.Event()
    web_task = None
    stop_deep_scheduler = asyncio.Event()
    deep_scheduler = None
    deep_scheduler_task = None
    try:
        bot = TelegramBot(
            client,
            store,
            config.allowed_user_ids,
            allowed_chat_ids=config.allowed_chat_ids,
            price_drop_threshold_percent=config.price_drop_threshold_percent,
            deep_scan_max_destinations=config.deep_scan_max_destinations,
            deep_scan_retries=config.deep_scan_retries,
            deep_scan_retry_delay_seconds=config.deep_scan_retry_delay_seconds,
            deep_scan_interval_minutes=config.deep_scan_interval_minutes,
        )
        bot.register_handlers()
        web_task = asyncio.create_task(
            _run_web_service(config.database_path, config.allowed_user_ids, bot.start_web_deep_scan),
            name="local-results-web",
        )
        scheduler = MonitorScheduler(store, bot.run_scheduled_scan)
        deep_scheduler = DeepScanScheduler(
            store,
            bot.run_scheduled_deep_scan,
            interval_minutes=config.deep_scan_interval_minutes,
        )
        await client.start(bot_token=config.bot_token)
        identity = await client.get_me()
        log.info("Bot connected as @%s using local MTProto proxy %s:%d", identity.username, config.proxy_host, config.proxy_port)
        scheduler_task = asyncio.create_task(scheduler.run(stop_scheduler), name="monitor-scheduler")
        outbox_task = asyncio.create_task(
            bot.retry_notification_outbox(stop_outbox), name="telegram-notification-outbox"
        )
        if deep_scheduler is not None:
            deep_scheduler_task = asyncio.create_task(
                deep_scheduler.run(stop_deep_scheduler), name="deep-scan-scheduler"
            )
        await client.run_until_disconnected()
    finally:
        stop_scheduler.set()
        stop_deep_scheduler.set()
        stop_outbox.set()
        if web_task is not None:
            web_task.cancel()
        try:
            if scheduler_task is not None:
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_monitoring_exception(
                        log,
                        message="❌ Планировщик проверок остановился с ошибкой: %s",
                        error=exc,
                        debug_context="SCHEDULER",
                    )
            if deep_scheduler_task is not None:
                try:
                    await deep_scheduler_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_monitoring_exception(
                        log,
                        message="❌ Планировщик глубоких проверок остановился с ошибкой: %s",
                        error=exc,
                        debug_context="DEEP-SCHEDULER",
                    )
            if outbox_task is not None:
                try:
                    await outbox_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_monitoring_exception(
                        log,
                        message="❌ Обработка очереди Telegram-уведомлений остановилась с ошибкой: %s",
                        error=exc,
                        debug_context="TELEGRAM-OUTBOX",
                    )
        finally:
            try:
                if web_task is not None:
                    try:
                        await web_task
                    except asyncio.CancelledError:
                        pass
                await client.disconnect()
            finally:
                try:
                    store.close()
                finally:
                    lock.release()
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


async def _run_web_service(
    database_path: str,
    allowed_user_ids: frozenset[int],
    on_deep_scan=None,
) -> None:
    try:
        log.info("Starting local results page at http://127.0.0.1:8765")
        await run_server(database_path, allowed_user_ids, on_deep_scan)
    except asyncio.CancelledError:
        raise
    except BaseException:
        log.exception("Local results website could not start; Telegram bot will continue")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
