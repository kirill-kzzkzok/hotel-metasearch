from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .price_alerts import analyze_deal, materially_better_price
from .flight_routes import flight_identity, search_route, city_code
from .monitoring_logs import format_price_alert, log_monitoring


SCHEMA_VERSION = 17
ORIGIN_RE = re.compile(r"^[A-Z]{3}$")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorSettings:
    user_id: int
    chat_id: int
    origin: str
    trip_type: str
    interval_minutes: int
    duration_min_days: int | None = None
    duration_max_days: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        origin = self.origin.upper()
        trip_type = self.trip_type.upper()
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "trip_type", trip_type)
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if not ORIGIN_RE.fullmatch(origin):
            raise ValueError("origin must be a three-letter route code")
        if trip_type not in {"OW", "RT"}:
            raise ValueError("trip_type must be OW or RT")
        if self.interval_minutes < 1:
            raise ValueError("interval_minutes must be at least 1")
        if trip_type == "OW":
            if self.duration_min_days is not None or self.duration_max_days is not None:
                raise ValueError("one-way settings must not specify round-trip duration")
        else:
            low = 1 if self.duration_min_days is None else self.duration_min_days
            high = 30 if self.duration_max_days is None else self.duration_max_days
            if not 1 <= low <= high <= 30:
                raise ValueError("RT duration must satisfy 1 <= min <= max <= 30")
            object.__setattr__(self, "duration_min_days", low)
            object.__setattr__(self, "duration_max_days", high)


@dataclass(frozen=True)
class ScanRecord:
    scan_id: int
    complete: bool
    new_destinations: tuple[dict[str, Any], ...]
    price_changes: tuple[dict[str, Any], ...] = ()
    partial: bool = False
    outdated_offers: int = 0
    disappeared_destinations: tuple[str, ...] = ()
    price_findings: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Destination:
    destination_id: int
    user_id: int
    chat_id: int
    origin: str
    destination: str
    destination_route: str | None
    first_seen: str
    last_seen: str
    active: bool
    last_known_price: int | None
    last_known_currency: str | None
    archived: bool = False


@dataclass(frozen=True)
class ScanSummary:
    scan_id: int
    origin: str
    trip_type: str
    started_at: str
    finished_at: str
    complete: bool
    cards_found: int
    dom_hot_ticket_count: int
    unique_destinations: int
    warnings: Any
    scan_kind: str = "regular"
    partial: bool = False


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return _now_utc()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_or_none(value: Any) -> str | None:
    return None if value is None else _json(value)


def _destination_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    return " ".join(normalized.split()).casefold()


def _sqlite_migration_destination_key(name: str) -> str:
    """Match the v12→v13 SQLite lower(trim(...)) key, which is ASCII-only."""
    return "".join(char.lower() if char.isascii() else char for char in name.strip(" "))


def destination_key(name: str) -> str:
    """Return the stable comparison key for a visible destination name."""
    return _destination_key(name)


class SQLiteStore:
    """Small synchronous repository for collector scans and per-chat state."""

    def __init__(self, database_path: str | Path):
        self.connection = sqlite3.connect(str(database_path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than supported schema {SCHEMA_VERSION}")
        if version == 1:
            primary_key = [
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(monitor_settings)").fetchall()
                if row["pk"]
            ]
            if primary_key == ["user_id", "chat_id"]:
                self.connection.executescript(
                    """
                    ALTER TABLE monitor_settings RENAME TO monitor_settings_v1;
                    CREATE TABLE monitor_settings (
                        user_id INTEGER NOT NULL CHECK (user_id > 0),
                        chat_id INTEGER NOT NULL,
                        origin TEXT NOT NULL CHECK (length(origin) = 3),
                        trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                        duration_min_days INTEGER,
                        duration_max_days INTEGER,
                        interval_minutes INTEGER NOT NULL CHECK (interval_minutes >= 1),
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, chat_id, origin),
                        CHECK (
                            (trip_type = 'OW' AND duration_min_days IS NULL AND duration_max_days IS NULL)
                            OR
                            (trip_type = 'RT' AND duration_min_days BETWEEN 1 AND 30
                             AND duration_max_days BETWEEN duration_min_days AND 30)
                        )
                    );
                    INSERT INTO monitor_settings (
                        user_id, chat_id, origin, trip_type, duration_min_days,
                        duration_max_days, interval_minutes, enabled, created_at, updated_at
                    ) SELECT user_id, chat_id, origin, trip_type, duration_min_days,
                        duration_max_days, interval_minutes, enabled, created_at, updated_at
                    FROM monitor_settings_v1;
                    DROP TABLE monitor_settings_v1;
                    """
                )
                version = 2
        if version == 2:
            offers_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'offers'"
            ).fetchone()
            if offers_table is not None:
                columns = {
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(offers)").fetchall()
                }
                if "transfers_json" not in columns:
                    self.connection.execute("ALTER TABLE offers ADD COLUMN transfers_json TEXT")
            self.connection.execute("PRAGMA user_version = 3")
            version = 3
        if version == 3:
            history_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scan_history'"
            ).fetchone()
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(scan_history)").fetchall()
            } if history_table is not None else set()
            if history_table is not None and "scan_kind" not in columns:
                self.connection.execute(
                    "ALTER TABLE scan_history ADD COLUMN scan_kind TEXT NOT NULL DEFAULT 'regular' "
                    "CHECK (scan_kind IN ('regular', 'deep'))"
                )
            self.connection.execute("PRAGMA user_version = 4")
            version = 4
        if version == 4:
            history_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scan_history'"
            ).fetchone()
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(scan_history)").fetchall()
            } if history_table is not None else set()
            if history_table is not None and "partial" not in columns:
                self.connection.execute(
                    "ALTER TABLE scan_history ADD COLUMN partial INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (partial IN (0, 1))"
                )
            self.connection.execute("PRAGMA user_version = 5")
            version = 5
        if version == 5:
            offers_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'offers'"
            ).fetchone()
            if offers_table is not None:
                columns = {
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(offers)").fetchall()
                }
                for column in ("flight_times_json", "airline_logos_json", "transfer_durations_json"):
                    if column not in columns:
                        self.connection.execute(f"ALTER TABLE offers ADD COLUMN {column} TEXT")
            self.connection.execute("PRAGMA user_version = 6")
            version = 6
        if version == 6:
            history_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scan_history'"
            ).fetchone()
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(scan_history)").fetchall()
            } if history_table is not None else set()
            if history_table is not None and "outdated_offers" not in columns:
                self.connection.execute(
                    "ALTER TABLE scan_history ADD COLUMN outdated_offers INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (outdated_offers >= 0)"
                )
            self.connection.execute("PRAGMA user_version = 7")
            version = 7
        if version == 7:
            offers_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'offers'"
            ).fetchone()
            if offers_table is not None:
                columns = {
                    row["name"] for row in self.connection.execute("PRAGMA table_info(offers)").fetchall()
                }
                for column in ("airline_by_leg_json", "arrival_day_offsets_json"):
                    if column not in columns:
                        self.connection.execute(f"ALTER TABLE offers ADD COLUMN {column} TEXT")
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS origin_labels (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    origin TEXT NOT NULL CHECK (length(origin) = 3),
                    city_name TEXT NOT NULL,
                    PRIMARY KEY (user_id, chat_id, origin)
                )"""
            )
            self.connection.execute("PRAGMA user_version = 8")
            version = 8
        if version == 8:
            destination_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'destinations'"
            ).fetchone()
            if destination_table is not None:
                columns = {
                    row["name"] for row in self.connection.execute("PRAGMA table_info(destinations)").fetchall()
                }
                if "archived" not in columns:
                    self.connection.execute(
                        "ALTER TABLE destinations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (archived IN (0, 1))"
                    )
            self.connection.execute("PRAGMA user_version = 9")
            version = 9
        if version == 9:
            settings_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'monitor_settings'"
            ).fetchone()
            if settings_table is not None:
                self.connection.execute(
                    "UPDATE monitor_settings SET interval_minutes = 30, updated_at = ? "
                    "WHERE interval_minutes < 30",
                    (_now_utc(),),
                )
            self.connection.execute("PRAGMA user_version = 10")
            version = 10
        if version == 10:
            history_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scan_history'"
            ).fetchone()
            columns = {
                row["name"] for row in self.connection.execute("PRAGMA table_info(scan_history)").fetchall()
            } if history_table is not None else set()
            if history_table is not None and "disappeared_destinations_json" not in columns:
                self.connection.execute(
                    "ALTER TABLE scan_history ADD COLUMN disappeared_destinations_json TEXT NOT NULL DEFAULT '[]'"
                )
            self.connection.execute("PRAGMA user_version = 11")
            version = 11
        if version == 11:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS price_drop_alerts (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    previous_price INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, chat_id, origin, destination_key,
                        previous_price, price, currency)
                )"""
            )
            self.connection.execute("PRAGMA user_version = 12")
            version = 12
        if version == 12:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    scan_id INTEGER NOT NULL REFERENCES scan_history(id) ON DELETE CASCADE,
                    origin TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                    currency TEXT NOT NULL,
                    price INTEGER NOT NULL CHECK (price > 0),
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE (scan_id, destination_key, trip_type, currency)
                );
                CREATE INDEX IF NOT EXISTS price_observations_baseline
                    ON price_observations (user_id, chat_id, origin, destination_key,
                        trip_type, currency, observed_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS deal_alert_state (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                    currency TEXT NOT NULL,
                    armed INTEGER NOT NULL DEFAULT 1 CHECK (armed IN (0, 1)),
                    last_alert_price INTEGER,
                    last_alert_at TEXT,
                    PRIMARY KEY (user_id, chat_id, origin, destination_key, trip_type, currency)
                );
                CREATE TABLE IF NOT EXISTS price_findings (
                    id INTEGER PRIMARY KEY,
                    observation_id INTEGER NOT NULL UNIQUE REFERENCES price_observations(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    current_price INTEGER NOT NULL,
                    reference_price REAL,
                    historical_low INTEGER,
                    previous_price INTEGER,
                    drop_amount REAL,
                    drop_percent REAL,
                    saving_amount REAL NOT NULL,
                    observation_count INTEGER NOT NULL,
                    baseline_confidence REAL NOT NULL,
                    deal_score REAL NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_notification_outbox (
                    id INTEGER PRIMARY KEY,
                    finding_id INTEGER NOT NULL UNIQUE REFERENCES price_findings(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sending', 'sent', 'retryable', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_at TEXT NOT NULL,
                    lease_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS telegram_outbox_due
                    ON telegram_notification_outbox (status, next_attempt_at, lease_until);
                """
            )
            # Seed the baseline from already-saved successful scans. This is
            # read-only with respect to scan history and makes the first new
            # observation useful immediately after upgrade.
            history_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scan_history'"
            ).fetchone()
            if history_table is not None:
                self.connection.execute(
                    """INSERT OR IGNORE INTO price_observations (
                        user_id, chat_id, scan_id, origin, destination_key, destination,
                        trip_type, currency, price, observed_at, payload_json
                    ) SELECT h.user_id, h.chat_id, h.id, h.origin,
                        lower(trim(o.destination)), trim(o.destination), h.trip_type,
                        o.currency, MIN(o.price), h.finished_at, '{}'
                      FROM scan_history h JOIN offers o ON o.scan_id = h.id
                      WHERE o.destination IS NOT NULL AND trim(o.destination) <> ''
                        AND o.price > 0 AND o.currency IS NOT NULL AND trim(o.currency) <> ''
                        AND (h.scan_kind = 'deep' OR (h.complete = 1 AND h.partial = 0))
                      GROUP BY h.user_id, h.chat_id, h.id, h.origin,
                        lower(trim(o.destination)), trim(o.destination), h.trip_type, o.currency"""
                )
            self.connection.execute("PRAGMA user_version = 13")
            version = 13
        if version == 13:
            offers_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'offers'"
            ).fetchone()
            if offers_table is not None:
                columns = {
                    row["name"] for row in self.connection.execute("PRAGMA table_info(offers)").fetchall()
                }
                if "repeat_registration_by_leg_json" not in columns:
                    self.connection.execute(
                        "ALTER TABLE offers ADD COLUMN repeat_registration_by_leg_json TEXT"
                    )
            self.connection.execute("PRAGMA user_version = 14")
            version = 14
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitor_settings (
                user_id INTEGER NOT NULL CHECK (user_id > 0),
                chat_id INTEGER NOT NULL,
                origin TEXT NOT NULL CHECK (length(origin) = 3),
                trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                duration_min_days INTEGER,
                duration_max_days INTEGER,
                interval_minutes INTEGER NOT NULL CHECK (interval_minutes >= 1),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, chat_id, origin),
                CHECK (
                    (trip_type = 'OW' AND duration_min_days IS NULL AND duration_max_days IS NULL)
                    OR
                    (trip_type = 'RT' AND duration_min_days BETWEEN 1 AND 30
                     AND duration_max_days BETWEEN duration_min_days AND 30)
                )
            );

            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                origin TEXT NOT NULL CHECK (length(origin) = 3),
                trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                cards_found INTEGER NOT NULL CHECK (cards_found >= 0),
                dom_hot_ticket_count INTEGER NOT NULL CHECK (dom_hot_ticket_count >= 0),
                unique_destinations INTEGER NOT NULL CHECK (unique_destinations >= 0),
                warnings_json TEXT NOT NULL,
                scan_kind TEXT NOT NULL DEFAULT 'regular' CHECK (scan_kind IN ('regular', 'deep')),
                partial INTEGER NOT NULL DEFAULT 0 CHECK (partial IN (0, 1)),
                outdated_offers INTEGER NOT NULL DEFAULT 0 CHECK (outdated_offers >= 0),
                disappeared_destinations_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS scan_history_owner_time
                ON scan_history (user_id, chat_id, finished_at DESC);

            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                origin TEXT NOT NULL CHECK (length(origin) = 3),
                destination_key TEXT NOT NULL,
                destination TEXT NOT NULL,
                destination_route TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
                archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                last_known_price INTEGER,
                last_known_currency TEXT,
                UNIQUE (user_id, chat_id, origin, destination_key)
            );

            CREATE INDEX IF NOT EXISTS destinations_owner_origin_recent
                ON destinations (user_id, chat_id, origin, first_seen DESC);

            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY,
                scan_id INTEGER NOT NULL REFERENCES scan_history(id) ON DELETE CASCADE,
                offer_index INTEGER NOT NULL,
                destination_id INTEGER REFERENCES destinations(id),
                destination TEXT,
                destination_route TEXT,
                price INTEGER,
                old_price INTEGER,
                currency TEXT,
                departure_date TEXT,
                departure_date_raw TEXT,
                return_date TEXT,
                return_date_raw TEXT,
                trip_duration_days INTEGER,
                flight_duration_json TEXT,
                airline_json TEXT,
                airports_json TEXT,
                stops_json TEXT,
                transfers_json TEXT,
                flight_times_json TEXT,
                airline_logos_json TEXT,
                transfer_durations_json TEXT,
                airline_by_leg_json TEXT,
                arrival_day_offsets_json TEXT,
                repeat_registration_by_leg_json TEXT,
                source_url TEXT,
                warnings_json TEXT NOT NULL,
                UNIQUE (scan_id, offer_index)
            );

            CREATE INDEX IF NOT EXISTS offers_destination_scan
                ON offers (destination_id, scan_id DESC);

            CREATE TABLE IF NOT EXISTS origin_labels (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                origin TEXT NOT NULL CHECK (length(origin) = 3),
                city_name TEXT NOT NULL,
                PRIMARY KEY (user_id, chat_id, origin)
            );

            CREATE TABLE IF NOT EXISTS price_drop_alerts (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                origin TEXT NOT NULL,
                destination_key TEXT NOT NULL,
                previous_price INTEGER NOT NULL,
                price INTEGER NOT NULL,
                currency TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (user_id, chat_id, origin, destination_key,
                    previous_price, price, currency)
            );

            CREATE TABLE IF NOT EXISTS price_observations (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                scan_id INTEGER NOT NULL REFERENCES scan_history(id) ON DELETE CASCADE,
                origin TEXT NOT NULL, destination_key TEXT NOT NULL, destination TEXT NOT NULL,
                trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                currency TEXT NOT NULL, price INTEGER NOT NULL CHECK (price > 0),
                observed_at TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (scan_id, destination_key, trip_type, currency)
            );
            CREATE INDEX IF NOT EXISTS price_observations_baseline
                ON price_observations (user_id, chat_id, origin, destination_key,
                    trip_type, currency, observed_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS deal_alert_state (
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, origin TEXT NOT NULL,
                destination_key TEXT NOT NULL, trip_type TEXT NOT NULL CHECK (trip_type IN ('OW', 'RT')),
                currency TEXT NOT NULL, armed INTEGER NOT NULL DEFAULT 1 CHECK (armed IN (0, 1)),
                last_alert_price INTEGER, last_alert_at TEXT,
                PRIMARY KEY (user_id, chat_id, origin, destination_key, trip_type, currency)
            );
            CREATE TABLE IF NOT EXISTS price_findings (
                id INTEGER PRIMARY KEY,
                observation_id INTEGER NOT NULL UNIQUE REFERENCES price_observations(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, origin TEXT NOT NULL,
                destination_key TEXT NOT NULL, destination TEXT NOT NULL, currency TEXT NOT NULL,
                current_price INTEGER NOT NULL, reference_price REAL, historical_low INTEGER,
                previous_price INTEGER, drop_amount REAL, drop_percent REAL,
                saving_amount REAL NOT NULL, observation_count INTEGER NOT NULL,
                baseline_confidence REAL NOT NULL, deal_score REAL NOT NULL, reason TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_notification_outbox (
                id INTEGER PRIMARY KEY,
                finding_id INTEGER NOT NULL UNIQUE REFERENCES price_findings(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'retryable', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                next_attempt_at TEXT NOT NULL, lease_until TEXT, last_error TEXT,
                created_at TEXT NOT NULL, sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS telegram_outbox_due
                ON telegram_notification_outbox (status, next_attempt_at, lease_until);

            """
        )
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # A fresh database is bootstrapped by the schema script above;
            # mark that legacy baseline once, then apply the normal upgrade.
            self.connection.execute("PRAGMA user_version = 14")
            version = 14
        if version == 14:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                for table in ("scan_history", "price_observations"):
                    columns = {
                        row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")
                    }
                    for column in ("duration_min_days", "duration_max_days"):
                        if column not in columns:
                            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")

                outbox_columns = {
                    row["name"] for row in self.connection.execute(
                        "PRAGMA table_info(telegram_notification_outbox)"
                    )
                }
                if "message_id" not in outbox_columns:
                    self.connection.execute(
                        "ALTER TABLE telegram_notification_outbox RENAME TO telegram_notification_outbox_v14"
                    )
                    self.connection.execute("DROP INDEX IF EXISTS telegram_outbox_due")
                    self.connection.execute(
                        """CREATE TABLE telegram_notification_outbox (
                            id INTEGER PRIMARY KEY,
                            finding_id INTEGER NOT NULL UNIQUE REFERENCES price_findings(id) ON DELETE CASCADE,
                            status TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'sending', 'sent', 'retryable', 'failed', 'uncertain')),
                            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                            next_attempt_at TEXT NOT NULL, lease_until TEXT, last_error TEXT,
                            created_at TEXT NOT NULL, sent_at TEXT,
                            message_id INTEGER, delivery_key TEXT, message_text TEXT
                        )"""
                    )
                    self.connection.execute(
                        """INSERT INTO telegram_notification_outbox (
                            id, finding_id, status, attempts, next_attempt_at, lease_until,
                            last_error, created_at, sent_at
                        ) SELECT id, finding_id,
                            CASE WHEN status = 'sending' THEN 'uncertain' ELSE status END,
                            attempts, next_attempt_at, lease_until,
                            last_error, created_at, sent_at FROM telegram_notification_outbox_v14"""
                    )
                    self.connection.execute("DROP TABLE telegram_notification_outbox_v14")
                self.connection.execute(
                    """CREATE TABLE IF NOT EXISTS deep_scan_rotation (
                        user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, origin TEXT NOT NULL,
                        cursor_destination_key TEXT, updated_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, chat_id, origin)
                    )"""
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS telegram_outbox_due "
                    "ON telegram_notification_outbox (status, next_attempt_at, lease_until)"
                )
                self.connection.execute("PRAGMA user_version = 15")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        self.connection.commit()

        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 15:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                for table, column, sql_type in (
                    ('offers','route_identity_json','TEXT'),
                    ('offers','route_valid','INTEGER NOT NULL DEFAULT 1'),
                    ('price_observations','route_valid','INTEGER NOT NULL DEFAULT 1'),
                ):
                    columns = {r['name'] for r in self.connection.execute(f'PRAGMA table_info({table})')}
                    if column not in columns:
                        self.connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {sql_type}')
                self._validate_legacy_flight_routes()
                self.connection.execute("PRAGMA user_version = 16")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 16:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                # The application's default monitor cadence is now 90 minutes.
                # Preserve any custom cadence while upgrading the former default.
                self.connection.execute(
                    "UPDATE monitor_settings SET interval_minutes = 90, updated_at = ? "
                    "WHERE interval_minutes = 30",
                    (_now_utc(),),
                )
                self.connection.execute("PRAGMA user_version = 17")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def _known_destination_routes(self) -> dict[tuple[int, int, str, str], str]:
        """Use original summary cards, never relabelled deep results, as requested context."""
        votes: dict[tuple[int, int, str, str], dict[str, int]] = {}
        for row in self.connection.execute(
            """SELECT h.user_id,h.chat_id,h.origin,o.destination,o.source_url
                FROM offers o JOIN scan_history h ON h.id=o.scan_id
                WHERE h.scan_kind='regular' AND o.route_valid=1"""
        ):
            route = search_route(row["source_url"])
            if route and city_code(route["origin"]) == city_code(row["origin"]):
                key = (row["user_id"], row["chat_id"], row["origin"], _destination_key(row["destination"]))
                counts = votes.setdefault(key, {})
                code = city_code(route["destination"])
                counts[code] = counts.get(code, 0) + 1
        return {key: max(counts, key=counts.get) for key, counts in votes.items()}

    def _validate_legacy_flight_routes(self) -> None:
        routes = self._known_destination_routes()
        affected: set[tuple[int, int, str, str]] = set()
        for row in self.connection.execute(
            "SELECT o.*,h.user_id,h.chat_id,h.origin,h.trip_type FROM offers o JOIN scan_history h ON h.id=o.scan_id"
        ).fetchall():
            offer = dict(row)
            offer["airports"] = json.loads(row["airports_json"] or "null")
            offer["stops"] = json.loads(row["stops_json"] or "null")
            key = (row["user_id"], row["chat_id"], row["origin"], _destination_key(row["destination"]))
            identity = flight_identity(offer, row["trip_type"], row["origin"], routes.get(key), f"scan:{row['scan_id']}")
            # Legacy rows with opaque fixture/manual links lack evidence; preserve them.
            valid = identity["route_valid"] if search_route(row["source_url"]) else True
            self.connection.execute("UPDATE offers SET route_identity_json=?,route_valid=? WHERE id=?",
                                    (_json(identity), int(valid), row["id"]))
            if not valid:
                affected.add(key)
        for row in self.connection.execute("SELECT * FROM price_observations").fetchall():
            offer = json.loads(row["payload_json"])
            key = (row["user_id"], row["chat_id"], row["origin"], _destination_key(row["destination"]))
            identity = flight_identity(offer, row["trip_type"], row["origin"], routes.get(key))
            valid = identity['route_valid'] if search_route(offer.get('source_url')) else True
            self.connection.execute('UPDATE price_observations SET route_valid=? WHERE id=?', (int(valid),row['id']))
            if not valid:
                affected.add(key)
        self.connection.execute("""UPDATE telegram_notification_outbox SET status='failed',lease_until=NULL,
            last_error='quarantined: itinerary does not match requested route'
            WHERE status IN ('pending','retryable','sending','uncertain') AND finding_id IN (
                SELECT f.id FROM price_findings f JOIN price_observations p ON p.id=f.observation_id WHERE p.route_valid=0)""")
        for user, chat, origin, key in affected:
            # Re-arm a state derived from the corrupt cheap price; keep raw findings and prices.
            self.connection.execute("""UPDATE deal_alert_state SET armed=1,last_alert_price=NULL
                WHERE user_id=? AND chat_id=? AND origin=? AND
                    (destination_key=? OR destination_key LIKE ?)""", (user,chat,origin,key,key+'|RT|%'))
            latest = self.connection.execute("""SELECT o.price,o.currency FROM offers o
                JOIN scan_history h ON h.id=o.scan_id JOIN destinations d ON d.id=o.destination_id
                WHERE d.user_id=? AND d.chat_id=? AND d.origin=? AND d.destination_key=? AND o.route_valid=1
                ORDER BY h.finished_at DESC,h.id DESC,o.price LIMIT 1""", (user,chat,origin,key)).fetchone()
            self.connection.execute("""UPDATE destinations SET last_known_price=?,last_known_currency=?
                WHERE user_id=? AND chat_id=? AND origin=? AND destination_key=?""",
                (latest['price'] if latest else None,latest['currency'] if latest else None,user,chat,origin,key))

    def save_settings(self, settings: MonitorSettings) -> None:
        now = _now_utc()
        self.connection.execute(
            """
            INSERT INTO monitor_settings (
                user_id, chat_id, origin, trip_type, duration_min_days,
                duration_max_days, interval_minutes, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id, origin) DO UPDATE SET
                origin = excluded.origin,
                trip_type = excluded.trip_type,
                duration_min_days = excluded.duration_min_days,
                duration_max_days = excluded.duration_max_days,
                interval_minutes = excluded.interval_minutes,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                settings.user_id,
                settings.chat_id,
                settings.origin,
                settings.trip_type,
                settings.duration_min_days,
                settings.duration_max_days,
                settings.interval_minutes,
                int(settings.enabled),
                now,
                now,
            ),
        )
        self.connection.commit()

    def save_origin_label(self, user_id: int, chat_id: int, origin: str, city_name: str) -> None:
        if not ORIGIN_RE.fullmatch(origin.upper()):
            raise ValueError("origin must be a three-letter route code")
        name = " ".join(city_name.split())
        if not name or len(name) > 100:
            raise ValueError("city name must contain 1 to 100 characters")
        self.connection.execute(
            """INSERT INTO origin_labels (user_id, chat_id, origin, city_name)
            VALUES (?, ?, ?, ?) ON CONFLICT(user_id, chat_id, origin)
            DO UPDATE SET city_name = excluded.city_name""",
            (user_id, chat_id, origin.upper(), name),
        )
        self.connection.commit()

    def get_origin_label(self, user_id: int, chat_id: int, origin: str) -> str | None:
        row = self.connection.execute(
            "SELECT city_name FROM origin_labels WHERE user_id = ? AND chat_id = ? AND origin = ?",
            (user_id, chat_id, origin.upper()),
        ).fetchone()
        return str(row["city_name"]) if row is not None else None

    def claim_price_drop_alert(self, user_id: int, chat_id: int, change: Mapping[str, Any]) -> bool:
        origin = str(change.get("origin") or "").upper()
        destination = str(change.get("destination") or "").strip()
        previous_price = change.get("previous_price")
        price = change.get("price")
        currency = str(change.get("currency") or change.get("previous_currency") or "").upper()
        if not ORIGIN_RE.fullmatch(origin) or not destination or not currency:
            return False
        if not isinstance(previous_price, int) or not isinstance(price, int):
            return False
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO price_drop_alerts (
                user_id, chat_id, origin, destination_key, previous_price, price, currency, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, origin, _destination_key(destination), previous_price, price, currency, _now_utc()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def release_price_drop_alert(self, user_id: int, chat_id: int, change: Mapping[str, Any]) -> None:
        origin = str(change.get("origin") or "").upper()
        destination = str(change.get("destination") or "").strip()
        previous_price = change.get("previous_price")
        price = change.get("price")
        currency = str(change.get("currency") or change.get("previous_currency") or "").upper()
        if not destination or not isinstance(previous_price, int) or not isinstance(price, int):
            return
        self.connection.execute(
            """DELETE FROM price_drop_alerts WHERE user_id = ? AND chat_id = ? AND origin = ?
                AND destination_key = ? AND previous_price = ? AND price = ? AND currency = ?""",
            (user_id, chat_id, origin, _destination_key(destination), previous_price, price, currency),
        )
        self.connection.commit()

    def pending_price_notification_owners(self) -> list[tuple[int, int]]:
        rows = self.connection.execute(
            """SELECT DISTINCT f.user_id, f.chat_id FROM telegram_notification_outbox n
                JOIN price_findings f ON f.id = n.finding_id
                WHERE (n.status IN ('pending', 'retryable', 'sending') AND n.attempts < 5)
                    OR n.status = 'uncertain'
                ORDER BY f.user_id, f.chat_id"""
        ).fetchall()
        return [(int(row["user_id"]), int(row["chat_id"])) for row in rows]

    def claim_pending_price_notifications(
        self, user_id: int, chat_id: int, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        now_text = _timestamp(now)
        lease_text = _timestamp(now + timedelta(minutes=2))
        claimed: list[dict[str, Any]] = []
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            # Outbox rows created before a destination was archived can be
            # left pending when archive state was changed outside SQLiteStore
            # (for example by the local results page). Close those rows so
            # they do not remain in the queue forever.
            self.connection.execute(
                """UPDATE telegram_notification_outbox SET status = 'failed', lease_until = NULL,
                    last_error = 'destination archived before notification delivery'
                    WHERE status IN ('pending', 'retryable') AND finding_id IN (
                        SELECT f.id FROM price_findings f JOIN destinations d
                            ON d.user_id = f.user_id AND d.chat_id = f.chat_id
                            AND d.origin = f.origin AND d.destination_key = f.destination_key
                        WHERE d.archived = 1 AND f.user_id = ? AND f.chat_id = ?
                    )""",
                (user_id, chat_id),
            )
            # Once a send has been prepared, an expired lease is an unknown
            # remote outcome, not permission to blindly send the same alert.
            self.connection.execute(
                """UPDATE telegram_notification_outbox
                    SET status = CASE WHEN message_text IS NULL THEN 'retryable' ELSE 'uncertain' END,
                        lease_until = NULL,
                        last_error = COALESCE(last_error, 'worker lease expired before delivery was recorded')
                    WHERE status = 'sending' AND lease_until <= ?""",
                (now_text,),
            )
            rows = self.connection.execute(
                """SELECT n.id AS notification_id, n.finding_id, n.attempts, f.payload_json
                    FROM telegram_notification_outbox n JOIN price_findings f ON f.id = n.finding_id
                    WHERE f.user_id = ? AND f.chat_id = ? AND n.attempts < 5
                        AND EXISTS (
                            SELECT 1 FROM destinations d WHERE d.user_id = f.user_id
                                AND d.chat_id = f.chat_id AND d.origin = f.origin
                                AND d.destination_key = f.destination_key AND d.archived = 0
                        )
                        AND n.status IN ('pending', 'retryable') AND n.next_attempt_at <= ?
                    ORDER BY n.created_at, n.id LIMIT ?""",
                (user_id, chat_id, now_text, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                cursor = self.connection.execute(
                    """UPDATE telegram_notification_outbox
                        SET status = 'sending', attempts = attempts + 1, lease_until = ?
                        WHERE id = ? AND attempts = ?""",
                    (lease_text, row["notification_id"], row["attempts"]),
                )
                if cursor.rowcount != 1:
                    continue
                payload = json.loads(row["payload_json"])
                payload["notification_id"] = int(row["notification_id"])
                payload["finding_id"] = int(row["finding_id"])
                payload["attempt"] = int(row["attempts"]) + 1
                payload["delivery_key"] = f"HT-{int(row['notification_id'])}"
                claimed.append(payload)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return claimed

    def prepare_price_notification(self, notification_id: int, delivery_key: str, message_text: str) -> None:
        cursor = self.connection.execute(
            """UPDATE telegram_notification_outbox SET delivery_key = ?, message_text = ?
                WHERE id = ? AND status = 'sending'""",
            (delivery_key, message_text, notification_id),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise RuntimeError("notification is no longer in sending state")
        self.connection.commit()

    def mark_price_notification_sent(self, notification_id: int, message_id: int | None = None) -> None:
        now = _now_utc()
        self.connection.execute(
            """UPDATE telegram_notification_outbox SET status = 'sent', sent_at = ?,
                message_id = COALESCE(?, message_id), lease_until = NULL, last_error = NULL
                WHERE id = ? AND status IN ('sending', 'uncertain')""",
            (now, message_id, notification_id),
        )
        self.connection.commit()

    def mark_price_notification_uncertain(self, notification_id: int, error: str) -> None:
        self.connection.execute(
            """UPDATE telegram_notification_outbox SET status = 'uncertain', lease_until = NULL,
                last_error = ? WHERE id = ? AND status = 'sending'""",
            (str(error)[:1000], notification_id),
        )
        self.connection.commit()

    def list_uncertain_price_notifications(self, user_id: int, chat_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT n.id AS notification_id, n.finding_id, n.delivery_key, n.message_text
                FROM telegram_notification_outbox n JOIN price_findings f ON f.id = n.finding_id
                WHERE f.user_id = ? AND f.chat_id = ? AND n.status = 'uncertain'
                ORDER BY n.created_at, n.id""",
            (user_id, chat_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_uncertain_price_notification(
        self, notification_id: int, *, delivered: bool, message_id: int | None = None
    ) -> None:
        if delivered:
            self.mark_price_notification_sent(notification_id, message_id)
            return
        self.connection.execute(
            """UPDATE telegram_notification_outbox SET status = 'pending', next_attempt_at = ?,
                lease_until = NULL, last_error = 'operator confirmed message absent'
                WHERE id = ? AND status = 'uncertain'""",
            (_now_utc(), notification_id),
        )
        self.connection.commit()

    def mark_price_notification_failed(self, notification_id: int, error: str) -> None:
        row = self.connection.execute(
            "SELECT attempts FROM telegram_notification_outbox WHERE id = ? AND status = 'sending'",
            (notification_id,),
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"])
        delays = (30, 120, 600, 1800)
        status = "retryable" if attempts < 5 else "failed"
        next_at = _timestamp(datetime.now(timezone.utc) + timedelta(seconds=delays[min(attempts - 1, len(delays) - 1)]))
        self.connection.execute(
            """UPDATE telegram_notification_outbox SET status = ?, next_attempt_at = ?,
                lease_until = NULL, last_error = ? WHERE id = ? AND status = 'sending'""",
            (status, next_at, str(error)[:1000], notification_id),
        )
        self.connection.commit()

    def get_settings(self, user_id: int, chat_id: int, origin: str | None = None) -> MonitorSettings | None:
        if origin is None:
            row = self.connection.execute(
                "SELECT * FROM monitor_settings WHERE user_id = ? AND chat_id = "
                "? ORDER BY updated_at DESC, origin LIMIT 1",
                (user_id, chat_id),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM monitor_settings WHERE user_id = ? AND chat_id = ? AND origin = ?",
                (user_id, chat_id, origin.upper()),
            ).fetchone()
        if row is None:
            return None
        return MonitorSettings(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            origin=row["origin"],
            trip_type=row["trip_type"],
            duration_min_days=row["duration_min_days"],
            duration_max_days=row["duration_max_days"],
            interval_minutes=row["interval_minutes"],
            enabled=bool(row["enabled"]),
        )

    def list_settings(self, user_id: int, chat_id: int) -> list[MonitorSettings]:
        rows = self.connection.execute(
            "SELECT * FROM monitor_settings WHERE user_id = ? AND chat_id = ? ORDER BY updated_at DESC, origin",
            (user_id, chat_id),
        ).fetchall()
        return [
            MonitorSettings(
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                origin=row["origin"],
                trip_type=row["trip_type"],
                duration_min_days=row["duration_min_days"],
                duration_max_days=row["duration_max_days"],
                interval_minutes=row["interval_minutes"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def list_enabled_settings(self) -> list[MonitorSettings]:
        rows = self.connection.execute(
            "SELECT * FROM monitor_settings WHERE enabled = 1 ORDER BY user_id, chat_id, origin"
        ).fetchall()
        return [
            MonitorSettings(
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                origin=row["origin"],
                trip_type=row["trip_type"],
                duration_min_days=row["duration_min_days"],
                duration_max_days=row["duration_max_days"],
                interval_minutes=row["interval_minutes"],
                enabled=True,
            )
            for row in rows
        ]

    def set_settings_enabled(
        self, user_id: int, chat_id: int, enabled: bool, origin: str | None = None
    ) -> bool:
        if origin is None:
            cursor = self.connection.execute(
                "UPDATE monitor_settings SET enabled = ?, updated_at = ? WHERE user_id = ? AND chat_id = ?",
                (int(enabled), _now_utc(), user_id, chat_id),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE monitor_settings SET enabled = ?, updated_at = ? "
                "WHERE user_id = ? AND chat_id = ? AND origin = ?",
                (int(enabled), _now_utc(), user_id, chat_id, origin.upper()),
            )
        self.connection.commit()
        return cursor.rowcount > 0

    def record_scan(
        self,
        user_id: int,
        chat_id: int,
        result: Mapping[str, Any],
        *,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
        track_price_changes: bool = False,
        scan_kind: str = "regular",
        price_drop_threshold_percent: float = 12.0,
    ) -> ScanRecord:
        origin = str(result.get("origin", "")).upper()
        trip_type = str(result.get("trip_type", "")).upper()
        if not ORIGIN_RE.fullmatch(origin):
            raise ValueError("scan origin must be a three-letter route code")
        if trip_type not in {"OW", "RT"}:
            raise ValueError("scan trip_type must be OW or RT")
        duration_min = result.get("duration_min_days") if trip_type == "RT" else None
        duration_max = result.get("duration_max_days") if trip_type == "RT" else None
        if trip_type == "RT" and not (
            isinstance(duration_min, int) and isinstance(duration_max, int)
            and 1 <= duration_min <= duration_max <= 30
        ):
            # Old/manual RT records without captured settings remain unknown;
            # they are retained, but cannot contaminate an exact-context baseline.
            duration_min = duration_max = None
        if scan_kind not in {"regular", "deep"}:
            raise ValueError("scan_kind must be regular or deep")
        result_offers = result.get("offers", [])
        if not isinstance(result_offers, list):
            raise ValueError("scan offers must be a list")

        known_routes = self._known_destination_routes()
        validated_offers = []
        for raw_offer in result_offers:
            offer = dict(raw_offer)
            expected = known_routes.get((user_id,chat_id,origin,_destination_key(str(offer.get('destination') or ''))))
            collected_identity = offer.get('route_identity') or {}
            if expected is None:
                expected = collected_identity.get('requested_destination')
            identity = flight_identity(offer, trip_type, origin, expected,
                                       f"{origin}|{trip_type}|{duration_min}|{duration_max}")
            if search_route(offer.get('source_url')) and not identity['route_valid']:
                log_monitoring(
                    log,
                    logging.WARNING,
                    "⚠️ %s → %s · маршрут в результатах не совпал с запросом",
                    origin,
                    expected or "неизвестное направление",
                )
                log.debug(
                    "[DEBUG][ROUTE-REJECT] requested=%s->%s actual=%s->%s destination=%s",
                    origin, expected, identity["actual_origin"], identity["actual_destination"],
                    offer.get("destination"),
                )
                continue
            offer['route_identity'] = identity
            validated_offers.append(offer)
        result_offers = validated_offers

        best_offer_by_destination: dict[str, Mapping[str, Any]] = {}
        best_offer_by_destination_currency: dict[tuple[str, str], Mapping[str, Any]] = {}
        for offer in result_offers:
            destination_name = offer.get("destination")
            price = offer.get("price")
            if not isinstance(destination_name, str) or not destination_name.strip() or not isinstance(price, int):
                continue
            key = _destination_key(destination_name)
            currency = str(offer.get("currency") or "").upper()
            if price > 0 and currency:
                currency_key = (key, currency)
                currency_previous = best_offer_by_destination_currency.get(currency_key)
                if currency_previous is None or price < int(currency_previous["price"]):
                    best_offer_by_destination_currency[currency_key] = offer
            previous = best_offer_by_destination.get(key)
            if previous is None:
                best_offer_by_destination[key] = offer
                continue
            previous_price = previous.get("price")
            if isinstance(previous_price, int) and price < previous_price:
                best_offer_by_destination[key] = offer

        complete = result.get("complete") is True and (bool(result_offers) or not result.get('offers'))
        catalog_complete = complete and result.get("partial") is not True
        finished = _timestamp(finished_at)
        if started_at is None and result.get("elapsed_seconds") is not None:
            started_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            started = _timestamp(
                datetime.fromtimestamp(
                    started_dt.timestamp() - float(result["elapsed_seconds"]), timezone.utc
                )
            )
        else:
            started = _timestamp(started_at if started_at is not None else finished_at)

        warnings = result.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        if result.get("parse_warning_counts"):
            warnings_payload = {"warnings": warnings, "parse_warning_counts": result["parse_warning_counts"]}
        else:
            warnings_payload = warnings

        new_destinations: list[dict[str, Any]] = []
        disappeared_destinations: list[str] = []
        price_changes: list[dict[str, Any]] = []
        tracked_price_keys: set[str] = set()
        outdated_offers = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if complete and result.get("partial") is not True and scan_kind == "regular":
                previous_scan = self.connection.execute(
                    """
                    SELECT id FROM scan_history
                    WHERE user_id = ? AND chat_id = ? AND origin = ?
                        AND scan_kind = 'regular' AND complete = 1 AND partial = 0
                    ORDER BY finished_at DESC, id DESC LIMIT 1
                    """,
                    (user_id, chat_id, origin),
                ).fetchone()
                if previous_scan is not None:
                    current_signatures = {
                        (
                            _destination_key(str(offer.get("destination") or "")),
                            offer.get("price"), offer.get("currency"),
                            offer.get("departure_date_raw") or offer.get("departure_date"),
                            offer.get("return_date_raw") or offer.get("return_date"),
                            offer.get("source_url"),
                            _json_or_none(offer.get("airline")),
                            _json_or_none(offer.get("airports")),
                            _json_or_none(offer.get("stops")),
                            _json_or_none(offer.get("transfers")),
                        )
                        for offer in result_offers
                        if offer.get("destination")
                    }
                    previous_offers = self.connection.execute(
                        """
                        SELECT destination, price, currency, departure_date_raw, departure_date,
                            return_date_raw, return_date, source_url, airline_json, airports_json,
                            stops_json, transfers_json
                        FROM offers WHERE scan_id = ?
                        """,
                        (previous_scan["id"],),
                    ).fetchall()
                    previous_destinations = {
                        _destination_key(str(row["destination"] or "")): str(row["destination"])
                        for row in previous_offers
                        if row["destination"]
                    }
                    current_destinations = {
                        _destination_key(str(offer.get("destination") or ""))
                        for offer in result_offers
                        if offer.get("destination")
                    }
                    disappeared_destinations = sorted(
                        (
                            name for key, name in previous_destinations.items()
                            if key not in current_destinations
                        ),
                        key=str.casefold,
                    )
                    outdated_offers = sum(
                        (
                            _destination_key(str(row["destination"] or "")),
                            row["price"], row["currency"],
                            row["departure_date_raw"] or row["departure_date"],
                            row["return_date_raw"] or row["return_date"],
                            row["source_url"], row["airline_json"], row["airports_json"],
                            row["stops_json"], row["transfers_json"],
                        ) not in current_signatures
                        for row in previous_offers
                    )
            cursor = self.connection.execute(
                """
                INSERT INTO scan_history (
                    user_id, chat_id, origin, trip_type, started_at, finished_at,
                    complete, cards_found, dom_hot_ticket_count, unique_destinations, warnings_json,
                    scan_kind, partial, outdated_offers, disappeared_destinations_json,
                    duration_min_days, duration_max_days
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    chat_id,
                    origin,
                    trip_type,
                    started,
                    finished,
                    int(complete),
                    int(result.get("cards_found", 0)),
                    int(result.get("dom_hot_ticket_count", 0)),
                    int(result.get("unique_destinations", 0)),
                    _json(warnings_payload),
                    scan_kind,
                    int(result.get("partial") is True),
                    outdated_offers,
                    _json(disappeared_destinations),
                    duration_min,
                    duration_max,
                ),
            )
            scan_id = int(cursor.lastrowid)

            offers_to_store = result_offers if complete or scan_kind == "deep" else []
            for index, offer in enumerate(offers_to_store):
                destination_name = offer.get("destination")
                destination_id = None
                if isinstance(destination_name, str):
                    destination_name = " ".join(destination_name.split()) or None
                if complete and isinstance(destination_name, str) and destination_name.strip():
                    key = _destination_key(destination_name)
                    existing = self.connection.execute(
                        """
                        SELECT id, last_known_price, last_known_currency, archived FROM destinations
                        WHERE user_id = ? AND chat_id = ? AND origin = ? AND destination_key = ?
                        """,
                        (user_id, chat_id, origin, key),
                    ).fetchone()
                    route = offer.get("destination_route")
                    best_offer = best_offer_by_destination.get(key, offer)
                    previous_price = existing["last_known_price"] if existing else None
                    previous_currency = existing["last_known_currency"] if existing else None
                    current_price = best_offer.get("price")
                    current_currency = best_offer.get("currency")
                    if (
                        track_price_changes
                        and existing is not None
                        and key not in tracked_price_keys
                        and isinstance(previous_price, int)
                        and isinstance(current_price, int)
                        and previous_price != current_price
                    ):
                        tracked_price_keys.add(key)
                        same_currency = previous_currency and current_currency and previous_currency == current_currency
                        change_percent = (
                            round((current_price - previous_price) / previous_price * 100, 2)
                            if previous_price and same_currency else None
                        )
                        price_changes.append(
                            {
                                "destination_id": int(existing["id"]),
                                "origin": origin,
                                "destination": destination_name,
                                "previous_price": previous_price,
                                "price": current_price,
                                "previous_currency": previous_currency,
                                "currency": current_currency,
                                "change_percent": change_percent,
                                "source_url": best_offer.get("source_url"),
                                "departure_date": best_offer.get("departure_date"),
                                "departure_date_raw": best_offer.get("departure_date_raw") or best_offer.get("departure_date"),
                                "return_date": best_offer.get("return_date"),
                                "return_date_raw": best_offer.get("return_date_raw") or best_offer.get("return_date"),
                                "trip_duration_days": best_offer.get("trip_duration_days"),
                                "flight_duration": best_offer.get("flight_duration"),
                                "flight_times": best_offer.get("flight_times"),
                                "stops": best_offer.get("stops"),
                                "transfers": best_offer.get("transfers"),
                                "transfer_durations": best_offer.get("transfer_durations"),
                            }
                        )
                    if catalog_complete:
                        self.connection.execute(
                            """
                            INSERT INTO destinations (
                                user_id, chat_id, origin, destination_key, destination,
                                destination_route, first_seen, last_seen, last_known_price,
                                last_known_currency
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(user_id, chat_id, origin, destination_key) DO UPDATE SET
                                destination = excluded.destination,
                                destination_route = COALESCE(excluded.destination_route, destinations.destination_route),
                                last_seen = excluded.last_seen,
                                last_known_price = COALESCE(excluded.last_known_price, destinations.last_known_price),
                                last_known_currency = COALESCE(excluded.last_known_currency, destinations.last_known_currency)
                            """,
                            (
                                user_id,
                                chat_id,
                                origin,
                                key,
                                destination_name,
                                route,
                                finished,
                                finished,
                                current_price,
                                current_currency,
                            ),
                        )
                        destination_id = self.connection.execute(
                            """
                            SELECT id FROM destinations
                            WHERE user_id = ? AND chat_id = ? AND origin = ? AND destination_key = ?
                            """,
                            (user_id, chat_id, origin, key),
                        ).fetchone()["id"]
                        if existing is None:
                            new_destinations.append(
                                {
                                    "destination_id": destination_id,
                                    "origin": origin,
                                    "destination": destination_name,
                                    "destination_route": route,
                                    "first_seen": finished,
                                }
                            )
                    elif scan_kind == "deep" and existing is not None:
                        destination_id = int(existing["id"])
                elif scan_kind == "deep" and isinstance(destination_name, str) and destination_name:
                    row = self.connection.execute(
                        """
                        SELECT id FROM destinations
                        WHERE user_id = ? AND chat_id = ? AND origin = ? AND destination_key = ?
                        """,
                        (user_id, chat_id, origin, _destination_key(destination_name)),
                    ).fetchone()
                    destination_id = int(row["id"]) if row is not None else None

                self.connection.execute(
                    """
                    INSERT INTO offers (
                        scan_id, offer_index, destination_id, destination, destination_route,
                        price, old_price, currency, departure_date, departure_date_raw,
                        return_date, return_date_raw, trip_duration_days,
                        flight_duration_json, airline_json, airports_json, stops_json,
                transfers_json, flight_times_json, airline_logos_json,
                transfer_durations_json, airline_by_leg_json, arrival_day_offsets_json,
                repeat_registration_by_leg_json,
                        source_url, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        index,
                        destination_id,
                        destination_name,
                        offer.get("destination_route"),
                        offer.get("price"),
                        offer.get("old_price"),
                        offer.get("currency"),
                        offer.get("departure_date"),
                        offer.get("departure_date_raw"),
                        offer.get("return_date"),
                        offer.get("return_date_raw"),
                        offer.get("trip_duration_days"),
                        _json_or_none(offer.get("flight_duration")),
                        _json_or_none(offer.get("airline")),
                        _json_or_none(offer.get("airports")),
                        _json_or_none(offer.get("stops")),
                        _json_or_none(offer.get("transfers")),
                        _json_or_none(offer.get("flight_times")),
                        _json_or_none(offer.get("airline_logos")),
                        _json_or_none(offer.get("transfer_durations")),
                        _json_or_none(offer.get("airline_by_leg")),
                        _json_or_none(offer.get("arrival_day_offsets")),
                        _json_or_none(offer.get("repeat_registration_by_leg")),
                        offer.get("source_url"),
                        _json(offer.get("warnings") or []),
                    ),
                )
                self.connection.execute(
                    "UPDATE offers SET route_identity_json=? WHERE scan_id=? AND offer_index=?",
                    (_json(offer.get('route_identity')), scan_id, index),
                )

            findings: list[dict[str, Any]] = []
            human_price_logs: list[str] = []
            if complete and (scan_kind == "deep" or result.get("partial") is not True):
                for (key, _currency_key), offer in best_offer_by_destination_currency.items():
                    price = offer.get("price")
                    currency = str(offer.get("currency") or "").upper()
                    destination = str(offer.get("destination") or "").strip()
                    if not destination or not isinstance(price, int) or price <= 0 or not currency:
                        continue
                    route_key = _destination_key(destination)
                    history_keys = tuple(dict.fromkeys((
                        route_key, _sqlite_migration_destination_key(destination),
                    )))
                    history_key_slots = ", ".join("?" for _ in history_keys)
                    self.connection.execute(
                        """INSERT OR IGNORE INTO price_observations (
                            user_id, chat_id, scan_id, origin, destination_key, destination,
                            trip_type, currency, price, observed_at, payload_json,
                            duration_min_days, duration_max_days
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, chat_id, scan_id, origin, route_key, destination,
                         trip_type, currency, price, finished, _json(offer), duration_min, duration_max),
                    )
                    observation = self.connection.execute(
                        "SELECT id FROM price_observations WHERE scan_id = ? AND destination_key = ? AND trip_type = ? AND currency = ?",
                        (scan_id, route_key, trip_type, currency),
                    ).fetchone()
                    if not track_price_changes or observation is None:
                        continue
                    observation_id = int(observation["id"])
                    history_rows = self.connection.execute(
                        f"""SELECT price, currency, observed_at FROM (
                            SELECT scan_id, MIN(price) AS price, currency, MIN(observed_at) AS observed_at
                            FROM price_observations
                            WHERE user_id = ? AND chat_id = ? AND origin = ?
                                AND destination_key IN ({history_key_slots})
                                AND trip_type = ? AND currency = ? AND scan_id <> ? AND route_valid=1
                                AND ((trip_type = 'OW' AND duration_min_days IS NULL AND duration_max_days IS NULL)
                                    OR (trip_type = 'RT' AND ? IS NOT NULL AND duration_min_days = ?
                                        AND duration_max_days = ?))
                            GROUP BY scan_id, currency
                        ) WHERE price > 0
                            ORDER BY observed_at DESC, scan_id DESC LIMIT 30""",
                        (user_id, chat_id, origin, *history_keys, trip_type, currency, scan_id,
                         duration_min, duration_min, duration_max),
                    ).fetchall()
                    history = [dict(row) for row in reversed(history_rows)]
                    summary = self.connection.execute(
                        f"""WITH per_scan AS (
                            SELECT scan_id, MIN(price) AS price, MIN(observed_at) AS observed_at
                            FROM price_observations WHERE user_id = ? AND chat_id = ?
                                AND origin = ? AND destination_key IN ({history_key_slots})
                                AND trip_type = ? AND currency = ? AND scan_id <> ? AND route_valid=1
                                AND ((trip_type = 'OW' AND duration_min_days IS NULL AND duration_max_days IS NULL)
                                    OR (trip_type = 'RT' AND ? IS NOT NULL AND duration_min_days = ?
                                        AND duration_max_days = ?))
                            GROUP BY scan_id
                        )
                        SELECT COUNT(*) AS observation_count, MIN(price) AS historical_low,
                            COALESCE(SUM(CASE WHEN price > ? THEN 1 ELSE 0 END), 0) AS higher_count,
                            COUNT(DISTINCT price) AS distinct_count,
                            MIN(observed_at) AS first_observed_at,
                            MAX(observed_at) AS last_observed_at FROM per_scan""",
                        (user_id, chat_id, origin, *history_keys, trip_type, currency, scan_id,
                         duration_min, duration_min, duration_max, price),
                    ).fetchone()
                    analysis = analyze_deal(
                        price, currency, history, historical_summary=dict(summary),
                        minimum_reference_drop_percent=price_drop_threshold_percent,
                    )
                    alert_state_key = route_key if trip_type == "OW" else (
                        f"{route_key}|RT|{duration_min}|{duration_max}"
                        if duration_min is not None and duration_max is not None else f"{route_key}|RT|UNKNOWN"
                    )
                    state = self.connection.execute(
                        """SELECT armed, last_alert_price FROM deal_alert_state
                            WHERE user_id = ? AND chat_id = ? AND origin = ?
                                AND destination_key = ? AND trip_type = ? AND currency = ?""",
                        (user_id, chat_id, origin, alert_state_key, trip_type, currency),
                    ).fetchone()
                    armed = True if state is None else bool(state["armed"])
                    last_alert_price = state["last_alert_price"] if state is not None else None
                    if not armed and isinstance(last_alert_price, int):
                        recovery_level = max(
                            last_alert_price * 1.20,
                            (analysis["reference_price"] or last_alert_price) * 0.90,
                        )
                        if price >= recovery_level:
                            armed = True
                            self.connection.execute(
                                """UPDATE deal_alert_state SET armed = 1
                                    WHERE user_id = ? AND chat_id = ? AND origin = ?
                                        AND destination_key = ? AND trip_type = ? AND currency = ?""",
                                (user_id, chat_id, origin, alert_state_key, trip_type, currency),
                            )
                    notification_allowed = bool(analysis["finding"])
                    notification_reason = analysis["reason"]
                    if notification_allowed and existing is not None and bool(existing["archived"]):
                        notification_allowed = False
                        notification_reason = "destination_archived"
                    if notification_allowed and not armed and not materially_better_price(price, last_alert_price, currency):
                        notification_allowed = False
                        notification_reason = "ongoing_event_deduplicated"
                    human_line = format_price_alert(
                        origin=origin,
                        destination=destination,
                        current_price=price,
                        previous_price=analysis["previous_price"],
                        previous_change_amount=analysis["previous_change_amount"],
                        previous_change_percent=analysis["previous_change_percent"],
                        historical_low=analysis["historical_low"],
                        observation_count=analysis["observation_count"],
                        reason=analysis["reason"],
                        currency=currency,
                    )
                    log.debug(
                        "[DEBUG][PRICE-ALERT] route=%s-%s current=%s previous=%s "
                        "reference=%s median=%s historical_low=%s "
                        "reference_drop_amount=%s reference_drop_percent=%s "
                        "previous_change_amount=%s previous_change_percent=%s "
                        "observations=%s confidence=%s score=%s finding=%s "
                        "notification_queued=%s reason=%s notification_reason=%s threshold=%s",
                        origin, destination, price, analysis["previous_price"],
                        analysis["reference_price"], analysis["reference_price"], analysis["historical_low"],
                        analysis["reference_drop_amount"], analysis["reference_drop_percent"],
                        analysis["previous_change_amount"], analysis["previous_change_percent"],
                        analysis["observation_count"], analysis["baseline_confidence"],
                        analysis["deal_score"], analysis["finding"], notification_allowed,
                        analysis["reason"], notification_reason, analysis["minimum_reference_drop_percent"],
                    )
                    if not analysis["finding"]:
                        human_price_logs.append(human_line)
                        continue
                    reference = analysis["reference_price"]
                    payload = dict(offer)
                    payload.update({
                        "origin": origin,
                        "destination": destination,
                        "reference_price": reference,
                        "reference_drop_amount": analysis["reference_drop_amount"],
                        "reference_drop_percent": analysis["reference_drop_percent"],
                        "previous_price": analysis["previous_price"],
                        "previous_change_amount": analysis["previous_change_amount"],
                        "previous_change_percent": analysis["previous_change_percent"],
                        "previous_currency": currency,
                        "currency": currency,
                        "change_percent": -analysis["reference_drop_percent"],
                        "deal_metrics": analysis,
                    })
                    cursor = self.connection.execute(
                        """INSERT OR IGNORE INTO price_findings (
                            observation_id, user_id, chat_id, origin, destination_key,
                            destination, currency, current_price, reference_price,
                            historical_low, previous_price, drop_amount, drop_percent,
                            saving_amount, observation_count, baseline_confidence,
                            deal_score, reason, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (observation_id, user_id, chat_id, origin, route_key, destination,
                         currency, price, analysis["reference_price"], analysis["historical_low"],
                         analysis["previous_price"], analysis["drop_amount"], analysis["drop_percent"],
                         analysis["saving_amount"], analysis["observation_count"],
                         analysis["baseline_confidence"], analysis["deal_score"], analysis["reason"],
                         _json(payload), finished),
                    )
                    if cursor.rowcount != 1:
                        human_price_logs.append(human_line)
                        continue
                    finding_id = int(cursor.lastrowid)
                    if notification_allowed:
                        self.connection.execute(
                            """INSERT INTO telegram_notification_outbox
                                (finding_id, status, attempts, next_attempt_at, created_at)
                                VALUES (?, 'pending', 0, ?, ?)""",
                            (finding_id, finished, finished),
                        )
                        self.connection.execute(
                            """INSERT INTO deal_alert_state (
                                user_id, chat_id, origin, destination_key, trip_type, currency,
                                armed, last_alert_price, last_alert_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                            ON CONFLICT(user_id, chat_id, origin, destination_key, trip_type, currency)
                            DO UPDATE SET armed = 0, last_alert_price = excluded.last_alert_price,
                                last_alert_at = excluded.last_alert_at""",
                            (user_id, chat_id, origin, alert_state_key, trip_type, currency, price, finished),
                        )
                    findings.append({"finding_id": finding_id, "notification_queued": notification_allowed, **payload})
                    human_suffix = " · ценовой сигнал сохранён"
                    if notification_allowed:
                        human_suffix += " · Telegram-уведомление в очереди"
                    human_price_logs.append(human_line + human_suffix)
                    log.debug(
                        "[DEBUG][PRICE-FINDING] Created finding=%s notification_queued=%s "
                        "route=%s-%s price=%s score=%s reason=%s",
                        finding_id, notification_allowed,
                        origin, destination, price, analysis["deal_score"], notification_reason,
                    )

            self.connection.commit()
            for human_line in human_price_logs:
                log_monitoring(log, logging.INFO, human_line)
        except Exception:
            self.connection.rollback()
            raise
        return ScanRecord(
            scan_id=scan_id,
            complete=complete,
            new_destinations=tuple(new_destinations),
            price_changes=tuple(price_changes),
            partial=result.get("partial") is True,
            outdated_offers=outdated_offers,
            disappeared_destinations=tuple(disappeared_destinations),
            price_findings=tuple(findings),
        )

    def set_destination_active(
        self, user_id: int, chat_id: int, destination_id: int, active: bool
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE destinations SET active = ?
            WHERE id = ? AND user_id = ? AND chat_id = ?
            """,
            (int(active), destination_id, user_id, chat_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def set_destination_archived(
        self, user_id: int, chat_id: int, destination_id: int, archived: bool
    ) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "UPDATE destinations SET archived = ? WHERE id = ? AND user_id = ? AND chat_id = ?",
                (int(archived), destination_id, user_id, chat_id),
            )
            updated = cursor.rowcount == 1
            if updated and archived:
                self.connection.execute(
                    """UPDATE telegram_notification_outbox SET status = 'failed', lease_until = NULL,
                        last_error = 'destination archived before notification delivery'
                        WHERE status IN ('pending', 'retryable') AND finding_id IN (
                            SELECT f.id FROM price_findings f JOIN destinations d
                                ON d.user_id = f.user_id AND d.chat_id = f.chat_id
                                AND d.origin = f.origin AND d.destination_key = f.destination_key
                            WHERE d.id = ? AND f.user_id = ? AND f.chat_id = ?
                        )""",
                    (destination_id, user_id, chat_id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return updated

    def list_active_destinations(self, user_id: int, chat_id: int, origin: str) -> list[Destination]:
        return self.list_destinations(
            user_id, chat_id, origin, active_only=True, available_only=True
        )

    def list_destinations(
        self,
        user_id: int,
        chat_id: int,
        origin: str,
        *,
        active_only: bool = False,
        include_archived: bool = False,
        available_only: bool = False,
    ) -> list[Destination]:
        active_clause = " AND active = 1" if active_only else ""
        archived_clause = "" if include_archived else " AND archived = 0"
        available_clause = "" if not available_only else """
            AND EXISTS (
                SELECT 1
                FROM scan_history current_scan
                JOIN offers current_offer ON current_offer.scan_id = current_scan.id
                WHERE current_scan.id = (
                    SELECT latest.id FROM scan_history latest
                    WHERE latest.user_id = destinations.user_id
                        AND latest.chat_id = destinations.chat_id
                        AND latest.origin = destinations.origin
                        AND latest.scan_kind = 'regular'
                        AND latest.complete = 1 AND latest.partial = 0
                    ORDER BY latest.finished_at DESC, latest.id DESC LIMIT 1
                )
                    AND current_offer.destination_id = destinations.id
            )
        """
        rows = self.connection.execute(
            f"""
            SELECT * FROM destinations
            WHERE user_id = ? AND chat_id = ? AND origin = ?
            {active_clause}{archived_clause}{available_clause}
            ORDER BY first_seen DESC, id DESC
            """,
            (user_id, chat_id, origin.upper()),
        ).fetchall()
        return [
            Destination(
                destination_id=row["id"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                origin=row["origin"],
                destination=row["destination"],
                destination_route=row["destination_route"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                active=bool(row["active"]),
                last_known_price=row["last_known_price"],
                last_known_currency=row["last_known_currency"],
                archived=bool(row["archived"]),
            )
            for row in rows
        ]

    def list_scans(self, user_id: int, chat_id: int, limit: int = 20) -> list[ScanSummary]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = self.connection.execute(
            """
            SELECT * FROM scan_history
            WHERE user_id = ? AND chat_id = ?
            ORDER BY finished_at DESC, id DESC LIMIT ?
            """,
            (user_id, chat_id, limit),
        ).fetchall()
        return [
            ScanSummary(
                scan_id=row["id"],
                origin=row["origin"],
                trip_type=row["trip_type"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                complete=bool(row["complete"]),
                cards_found=row["cards_found"],
                dom_hot_ticket_count=row["dom_hot_ticket_count"],
                unique_destinations=row["unique_destinations"],
                warnings=json.loads(row["warnings_json"]),
                scan_kind=row["scan_kind"],
                partial=bool(row["partial"]),
            )
            for row in rows
        ]

    def latest_scan_finished_at(
        self, user_id: int, chat_id: int, origin: str, scan_kind: str
    ) -> str | None:
        if scan_kind not in {"regular", "deep"}:
            raise ValueError("scan_kind must be regular or deep")
        row = self.connection.execute(
            """
            SELECT finished_at FROM scan_history
            WHERE user_id = ? AND chat_id = ? AND origin = ? AND scan_kind = ?
                AND complete = 1 AND partial = 0
            ORDER BY finished_at DESC, id DESC LIMIT 1
            """,
            (user_id, chat_id, origin.upper(), scan_kind),
        ).fetchone()
        return str(row["finished_at"]) if row is not None else None

    def get_deep_scan_cursor(self, user_id: int, chat_id: int, origin: str) -> str | None:
        row = self.connection.execute(
            "SELECT cursor_destination_key FROM deep_scan_rotation "
            "WHERE user_id = ? AND chat_id = ? AND origin = ?",
            (user_id, chat_id, origin.upper()),
        ).fetchone()
        return str(row["cursor_destination_key"]) if row and row["cursor_destination_key"] else None

    def set_deep_scan_cursor(self, user_id: int, chat_id: int, origin: str, destination: str | None) -> None:
        key = _destination_key(destination) if destination else None
        self.connection.execute(
            """INSERT INTO deep_scan_rotation (user_id, chat_id, origin, cursor_destination_key, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id, origin) DO UPDATE SET
                    cursor_destination_key = excluded.cursor_destination_key,
                    updated_at = excluded.updated_at""",
            (user_id, chat_id, origin.upper(), key, _now_utc()),
        )
        self.connection.commit()

    def get_scan_offers(self, user_id: int, chat_id: int, scan_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT offers.*, scan_history.origin AS origin FROM offers
            JOIN scan_history ON scan_history.id = offers.scan_id
            WHERE scan_history.user_id = ? AND scan_history.chat_id = ? AND scan_history.id = ? AND offers.route_valid=1
            ORDER BY offers.offer_index
            """,
            (user_id, chat_id, scan_id),
        ).fetchall()
        json_fields = {
            "flight_duration_json": "flight_duration",
            "airline_json": "airline",
            "airports_json": "airports",
            "stops_json": "stops",
            "transfers_json": "transfers",
            "flight_times_json": "flight_times",
            "airline_logos_json": "airline_logos",
            "transfer_durations_json": "transfer_durations",
            "airline_by_leg_json": "airline_by_leg",
            "arrival_day_offsets_json": "arrival_day_offsets",
            "repeat_registration_by_leg_json": "repeat_registration_by_leg",
            "route_identity_json": "route_identity",
        }
        output = []
        for row in rows:
            item = {
                key: row[key]
                for key in (
                    "origin",
                    "destination",
                    "destination_route",
                    "price",
                    "old_price",
                    "currency",
                    "departure_date",
                    "departure_date_raw",
                    "return_date",
                    "return_date_raw",
                    "trip_duration_days",
                    "source_url",
                )
            }
            item["offer_id"] = row["id"]
            item["destination_id"] = row["destination_id"]
            item["warnings"] = json.loads(row["warnings_json"])
            for db_key, public_key in json_fields.items():
                item[public_key] = json.loads(row[db_key]) if row[db_key] is not None else None
            output.append(item)
        return output

    def get_latest_scan_offers(
        self, user_id: int, chat_id: int, origin: str, *, complete_only: bool = True
    ) -> list[dict[str, Any]]:
        """Return offers from the latest stored scan for one origin."""
        condition = "AND complete = 1" if complete_only else ""
        row = self.connection.execute(
            f"""
            SELECT id FROM scan_history
            WHERE user_id = ? AND chat_id = ? AND origin = ? {condition}
            ORDER BY finished_at DESC, id DESC LIMIT 1
            """,
            (user_id, chat_id, origin.upper()),
        ).fetchone()
        if row is None:
            return []
        return self.get_scan_offers(user_id, chat_id, int(row["id"]))
