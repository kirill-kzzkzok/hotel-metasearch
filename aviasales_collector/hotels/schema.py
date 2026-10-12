"""Hotel tables and backwards-compatible SQLite schema initialization."""

from __future__ import annotations

import json
import threading
from contextlib import closing
from pathlib import Path

from .models import utc_now
from .runtime import _profiled_sqlite_connect


_HOTEL_STORE_SCHEMA_LOCK = threading.RLock()
_INITIALIZED_HOTEL_STORE_IDENTITIES: set[tuple[str, int, int]] = set()


def _hotel_store_identity(path: str | Path) -> tuple[str, int, int]:
    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return str(resolved), -1, -1
    return str(resolved), int(stat.st_dev), int(stat.st_ino)


def init_hotel_store(path: str | Path) -> None:
    """Initialize or migrate a store once per on-disk database identity.

    This function is called from read and write paths. Re-running its DDL and
    ALTER checks for each rate result made every persistence operation contend
    for SQLite's schema write lock. The file identity changes if a database is
    replaced at the same path, so a newly-created store is still initialized.
    """
    with _HOTEL_STORE_SCHEMA_LOCK:
        identity = _hotel_store_identity(path)
        if identity in _INITIALIZED_HOTEL_STORE_IDENTITIES:
            return
        _initialize_hotel_store_uncached(path)
        _INITIALIZED_HOTEL_STORE_IDENTITIES.add(_hotel_store_identity(path))


def _initialize_hotel_store_uncached(path: str | Path) -> None:
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS hotel_search_session (
                id TEXT PRIMARY KEY, query_json TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL, total_hotels INTEGER,
                next_page_token TEXT
            );
            CREATE TABLE IF NOT EXISTS hotel_source_run (
                id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
                status TEXT NOT NULL, latency_ms INTEGER, pages INTEGER NOT NULL DEFAULT 0,
                hotel_count INTEGER NOT NULL DEFAULT 0, offer_count INTEGER NOT NULL DEFAULT 0,
                cache_state TEXT NOT NULL DEFAULT 'pending', reported_total INTEGER,
                error_class TEXT, observed_at TEXT NOT NULL, http_status INTEGER,
                collection_complete INTEGER, collection_reason TEXT,
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_raw_page (
                id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
                page_index INTEGER NOT NULL, raw_json TEXT NOT NULL, observed_at TEXT NOT NULL,
                UNIQUE(search_id, source, page_index),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_observation (
                id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
                source_hotel_id TEXT NOT NULL, name TEXT NOT NULL, address TEXT,
                stars REAL, rating REAL, rating_original REAL, rating_scale INTEGER,
                rating_normalized REAL, rating_source TEXT,
                reviews_count INTEGER, images_json TEXT NOT NULL,
                nearby_places_json TEXT NOT NULL, raw_json TEXT NOT NULL, observed_at TEXT NOT NULL,
                UNIQUE(search_id, source, source_hotel_id),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_offer_observation (
                id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
                source_hotel_id TEXT NOT NULL, dedupe_key TEXT, provider TEXT, amount TEXT,
                source_price_value INTEGER, currency TEXT, deeplink TEXT,
                labels_json TEXT NOT NULL, raw_json TEXT NOT NULL, observed_at TEXT NOT NULL,
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_room_rate_observation (
                id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
                source_hotel_id TEXT NOT NULL, provider_rate_id TEXT NOT NULL,
                check_in TEXT NOT NULL, check_out TEXT NOT NULL, adults INTEGER NOT NULL,
                children_count INTEGER NOT NULL DEFAULT 0, room_group_index INTEGER,
                raw_json TEXT NOT NULL, observed_at TEXT NOT NULL,
                UNIQUE(search_id,source,source_hotel_id,provider_rate_id,check_in,check_out,
                       adults,children_count,observed_at),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_room_rate_fetch (
                id INTEGER PRIMARY KEY, search_id TEXT NOT NULL, source TEXT NOT NULL,
                source_hotel_id TEXT NOT NULL, check_in TEXT NOT NULL, check_out TEXT NOT NULL,
                adults INTEGER NOT NULL, children_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, http_status INTEGER, latency_ms INTEGER,
                rates_seen INTEGER NOT NULL DEFAULT 0, observed_at TEXT NOT NULL,
                property_details_json TEXT, operation TEXT NOT NULL DEFAULT 'tripcom_detail_rate_inventory',
                error_type TEXT, error_message TEXT, attempt INTEGER NOT NULL DEFAULT 1,
                retry_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE INDEX IF NOT EXISTS hotel_observation_search_idx
                ON hotel_observation(search_id, source, name);
            CREATE INDEX IF NOT EXISTS hotel_offer_search_idx
                ON hotel_offer_observation(search_id, source, source_hotel_id, currency);
            CREATE INDEX IF NOT EXISTS hotel_room_rate_search_idx
                ON hotel_room_rate_observation(search_id,source,source_hotel_id,check_in,check_out,observed_at);
            CREATE INDEX IF NOT EXISTS hotel_room_rate_fetch_idx
                ON hotel_room_rate_fetch(search_id,source,source_hotel_id,check_in,check_out,adults,children_count,observed_at);
            CREATE TABLE IF NOT EXISTS canonical_hotel (
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL, normalized_name TEXT,
                country TEXT, city TEXT, address TEXT, latitude REAL, longitude REAL,
                coordinate_source TEXT, coordinate_method TEXT, coordinate_confidence TEXT,
                stars REAL, canonical_rating REAL, canonical_review_count INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS hotel_city_reference (
                city_slug TEXT NOT NULL, country_slug TEXT NOT NULL, name TEXT NOT NULL,
                center_lat REAL NOT NULL, center_lon REAL NOT NULL, source TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'registered_reference',
                PRIMARY KEY(city_slug, country_slug)
            );
            CREATE TABLE IF NOT EXISTS hotel_city_geo_profile (
                city_slug TEXT NOT NULL, country_slug TEXT NOT NULL,
                is_beach_destination INTEGER NOT NULL, source TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(city_slug, country_slug)
            );
            CREATE TABLE IF NOT EXISTS hotel_source_identity (
                source TEXT NOT NULL, source_hotel_id TEXT NOT NULL,
                canonical_hotel_id TEXT NOT NULL REFERENCES canonical_hotel(id),
                created_at TEXT NOT NULL, PRIMARY KEY(source, source_hotel_id)
            );
            CREATE TABLE IF NOT EXISTS hotel_match_candidate (
                search_id TEXT NOT NULL, left_source TEXT NOT NULL, left_id TEXT NOT NULL,
                right_source TEXT NOT NULL, right_id TEXT NOT NULL, score REAL NOT NULL,
                features_json TEXT NOT NULL, decision TEXT NOT NULL, matcher_version TEXT NOT NULL,
                PRIMARY KEY(search_id,left_source,left_id,right_source,right_id),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE INDEX IF NOT EXISTS hotel_match_review_idx
                ON hotel_match_candidate(decision, score DESC);
            CREATE TABLE IF NOT EXISTS hotel_source_cache (
                cache_key TEXT PRIMARY KEY, source TEXT NOT NULL, raw_json TEXT NOT NULL,
                http_status INTEGER, latency_ms INTEGER NOT NULL, observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hotel_coordinate_cache (
                cache_key TEXT PRIMARY KEY, latitude REAL, longitude REAL,
                source TEXT NOT NULL, retrieved_at TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hotel_coordinate_observation (
                search_id TEXT NOT NULL, source TEXT NOT NULL, source_hotel_id TEXT NOT NULL,
                latitude REAL NOT NULL, longitude REAL NOT NULL, coordinate_source TEXT NOT NULL,
                coordinate_method TEXT NOT NULL DEFAULT 'provider_coordinates',
                coordinate_confidence TEXT NOT NULL DEFAULT 'high',
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY(search_id, source, source_hotel_id),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_coordinate_resolution (
                search_id TEXT NOT NULL, hotel_key TEXT NOT NULL,
                canonical_hotel_id TEXT, status TEXT NOT NULL, reason TEXT,
                coordinate_source TEXT, coordinate_method TEXT, coordinate_confidence TEXT,
                latitude REAL, longitude REAL, attempts_json TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY(search_id, hotel_key),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE TABLE IF NOT EXISTS hotel_beach_cache (
                cache_key TEXT PRIMARY KEY, distance_meters REAL, beach_name TEXT,
                source TEXT NOT NULL, retrieved_at TEXT NOT NULL, status TEXT NOT NULL,
                beach_osm_type TEXT, beach_osm_id TEXT, beach_method TEXT, beach_confidence TEXT
            );
            CREATE TABLE IF NOT EXISTS hotel_beach_search_cache (
                tile_key TEXT PRIMARY KEY, features_json TEXT, source TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, status TEXT NOT NULL, diagnostic_json TEXT
            );
            CREATE TABLE IF NOT EXISTS hotel_beach_feature_cache (
                osm_type TEXT NOT NULL, osm_id TEXT NOT NULL, name TEXT, geometry_json TEXT,
                retrieved_at TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(osm_type,osm_id)
            );
            CREATE TABLE IF NOT EXISTS hotel_geo_feature_cache (
                tile_key TEXT PRIMARY KEY, features_json TEXT, source TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, status TEXT NOT NULL, diagnostic_json TEXT
            );
            CREATE TABLE IF NOT EXISTS hotel_provider_detail_cache (
                source TEXT NOT NULL, source_hotel_id TEXT NOT NULL, address TEXT,
                latitude REAL, longitude REAL, coordinate_verified INTEGER NOT NULL DEFAULT 0,
                detail_version TEXT NOT NULL DEFAULT '', metadata_json TEXT,
                status TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                PRIMARY KEY(source, source_hotel_id)
            );
            CREATE TABLE IF NOT EXISTS hotel_provider_metadata (
                search_id TEXT NOT NULL, source TEXT NOT NULL, source_hotel_id TEXT NOT NULL,
                provider TEXT NOT NULL, metadata_json TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                PRIMARY KEY(search_id,source,source_hotel_id,provider),
                FOREIGN KEY(search_id) REFERENCES hotel_search_session(id)
            );
            CREATE INDEX IF NOT EXISTS hotel_source_cache_expiry_idx ON hotel_source_cache(expires_at);
            """
        )
        hotel_observation_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(hotel_observation)")}
        for column, declaration in (
            ("rating_original", "REAL"), ("rating_scale", "INTEGER"),
            ("rating_normalized", "REAL"), ("rating_source", "TEXT"),
        ):
            if column not in hotel_observation_columns:
                connection.execute(f"ALTER TABLE hotel_observation ADD COLUMN {column} {declaration}")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(hotel_source_run)")}
        if "http_status" not in columns:
            connection.execute("ALTER TABLE hotel_source_run ADD COLUMN http_status INTEGER")
        if "cache_state" not in columns:
            connection.execute("ALTER TABLE hotel_source_run ADD COLUMN cache_state TEXT NOT NULL DEFAULT 'pending'")
        if "reported_total" not in columns:
            connection.execute("ALTER TABLE hotel_source_run ADD COLUMN reported_total INTEGER")
        if "collection_complete" not in columns:
            connection.execute("ALTER TABLE hotel_source_run ADD COLUMN collection_complete INTEGER")
        if "collection_reason" not in columns:
            connection.execute("ALTER TABLE hotel_source_run ADD COLUMN collection_reason TEXT")
        rate_fetch_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(hotel_room_rate_fetch)")}
        if "property_details_json" not in rate_fetch_columns:
            connection.execute("ALTER TABLE hotel_room_rate_fetch ADD COLUMN property_details_json TEXT")
        for column, declaration in (
            ("operation", "TEXT NOT NULL DEFAULT 'tripcom_detail_rate_inventory'"),
            ("error_type", "TEXT"),
            ("error_message", "TEXT"),
            ("attempt", "INTEGER NOT NULL DEFAULT 1"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in rate_fetch_columns:
                connection.execute(f"ALTER TABLE hotel_room_rate_fetch ADD COLUMN {column} {declaration}")
        coordinate_columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(hotel_coordinate_observation)")}
        for column, declaration in (
            ("coordinate_method", "TEXT NOT NULL DEFAULT 'provider_coordinates'"),
            ("coordinate_confidence", "TEXT NOT NULL DEFAULT 'high'"),
        ):
            if column not in coordinate_columns:
                connection.execute(
                    f"ALTER TABLE hotel_coordinate_observation ADD COLUMN {column} {declaration}")
        offer_columns = {row[1] for row in connection.execute("PRAGMA table_info(hotel_offer_observation)")}
        if "dedupe_key" not in offer_columns:
            connection.execute("ALTER TABLE hotel_offer_observation ADD COLUMN dedupe_key TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS hotel_offer_dedupe_idx "
            "ON hotel_offer_observation(search_id,source,source_hotel_id,dedupe_key)"
        )
        canonical_columns = {row[1] for row in connection.execute("PRAGMA table_info(canonical_hotel)")}
        for column, declaration in (
            ("normalized_name", "TEXT"), ("country", "TEXT"), ("city", "TEXT"),
            ("address", "TEXT"), ("latitude", "REAL"), ("longitude", "REAL"),
            ("coordinate_source", "TEXT"), ("coordinate_method", "TEXT"),
            ("coordinate_confidence", "TEXT"),
            ("stars", "REAL"), ("canonical_rating", "REAL"),
            ("canonical_review_count", "INTEGER"), ("updated_at", "TEXT"),
        ):
            if column not in canonical_columns:
                connection.execute(f"ALTER TABLE canonical_hotel ADD COLUMN {column} {declaration}")
        beach_columns = {row[1] for row in connection.execute("PRAGMA table_info(hotel_beach_cache)")}
        for column, declaration in (
            ("beach_osm_type", "TEXT"), ("beach_osm_id", "TEXT"),
            ("beach_method", "TEXT"), ("beach_confidence", "TEXT"),
        ):
            if column not in beach_columns:
                connection.execute(f"ALTER TABLE hotel_beach_cache ADD COLUMN {column} {declaration}")
        for table, column, declaration in (
            ("hotel_city_reference", "method", "TEXT NOT NULL DEFAULT 'registered_reference'"),
            ("hotel_city_geo_profile", "evidence", "TEXT NOT NULL DEFAULT ''"),
            ("hotel_provider_detail_cache", "metadata_json", "TEXT"),
        ):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        for table in ("hotel_beach_search_cache", "hotel_geo_feature_cache"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "diagnostic_json" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN diagnostic_json TEXT")
        profiles_path = Path(__file__).resolve().parent.parent / "data" / "city_geo_profiles.json"
        if profiles_path.exists():
            try:
                profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                profiles = []
            for profile in profiles if isinstance(profiles, list) else []:
                if not isinstance(profile, dict):
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO hotel_city_geo_profile "
                    "(city_slug,country_slug,is_beach_destination,source,retrieved_at,evidence) "
                    "VALUES (?,?,?,?,?,?)",
                    (profile.get("city_slug"), profile.get("country_slug"),
                     int(bool(profile.get("is_beach_destination"))), "curated_city_destination_profile",
                     utc_now(), str(profile.get("evidence") or "")),
                )
        connection.commit()
