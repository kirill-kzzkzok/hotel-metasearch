"""Hotel search SQLite read model and persisted source audit."""

from __future__ import annotations

import collections
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..hotel_matching import normalize_hotel_name
from .geo.city import _read_city_geo_profile
from .geo.core import (_coordinate_pair, _coordinate_source_metadata,
                       _haversine_m, _hotel_beach_cache_key,
                       _hotel_coordinate_cache_key,
                       _is_enrichment_coordinate_source)
from .geo.geometry import (_overpass_legacy_tile_key,
                           _overpass_nearby_tile_key, _overpass_tile_key)
from .normalization import (_normalize_hotel_stars, _normalize_rating_fields,
                            extract_amenities, normalize_beach_line)
from .runtime import (_profiled_sqlite_connect, _safe_json_list,
                      _safe_json_loads, log_search_timeline)
from .schema import init_hotel_store


class _ReadModelProfile:
    def __init__(self) -> None:
        self.query_count = 0
        self.rows_fetched = 0
        self.execute_ms = 0.0
        self.fetch_ms = 0.0
        self.queries_by_table: collections.Counter[str] = collections.Counter()
        self.rows_by_table: collections.Counter[str] = collections.Counter()
        self.slowest: list[dict[str, Any]] = []

    @staticmethod
    def _table(sql: str) -> str:
        match = re.search(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+([\w\"]+)", sql, re.I)
        return match.group(1).strip('"') if match else "other"

    def start_query(self, sql: str, elapsed_ms: float) -> dict[str, Any]:
        table = self._table(sql)
        normalized = " ".join(sql.split())[:220]
        item = {"table": table, "sql": normalized, "execute_ms": elapsed_ms,
                "fetch_ms": 0.0, "rows": 0}
        self.query_count += 1
        self.execute_ms += elapsed_ms
        self.queries_by_table[table] += 1
        self.slowest.append(item)
        self.slowest.sort(key=lambda query: query["execute_ms"] + query["fetch_ms"], reverse=True)
        del self.slowest[10:]
        return item

    def add_fetch(self, item: dict[str, Any], elapsed_ms: float, rows: int) -> None:
        item["fetch_ms"] += elapsed_ms
        item["rows"] += rows
        self.fetch_ms += elapsed_ms
        self.rows_fetched += rows
        self.rows_by_table[item["table"]] += rows


class _ReadModelProfiledCursor:
    def __init__(self, cursor: sqlite3.Cursor, profile: _ReadModelProfile,
                 query: dict[str, Any]) -> None:
        self._cursor = cursor
        self._profile = profile
        self._query = query

    def fetchone(self):
        started = time.perf_counter()
        row = self._cursor.fetchone()
        self._profile.add_fetch(self._query, (time.perf_counter() - started) * 1000,
                                int(row is not None))
        return row

    def fetchall(self):
        started = time.perf_counter()
        rows = self._cursor.fetchall()
        self._profile.add_fetch(self._query, (time.perf_counter() - started) * 1000, len(rows))
        return rows

    def fetchmany(self, size: int | None = None):
        started = time.perf_counter()
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        self._profile.add_fetch(self._query, (time.perf_counter() - started) * 1000, len(rows))
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        started = time.perf_counter()
        try:
            row = next(self._cursor)
        except StopIteration:
            raise
        self._profile.add_fetch(self._query, (time.perf_counter() - started) * 1000, 1)
        return row

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _ReadModelProfiledConnection:
    def __init__(self, connection: sqlite3.Connection, profile: _ReadModelProfile) -> None:
        self._connection = connection
        self._profile = profile

    def execute(self, sql: str, parameters=()):
        started = time.perf_counter()
        cursor = self._connection.execute(sql, parameters)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return _ReadModelProfiledCursor(cursor, self._profile,
                                        self._profile.start_query(sql, elapsed_ms))

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)


def _iter_source_key_rows(
    connection: _ReadModelProfiledConnection,
    select_sql: str,
    base_params: tuple[Any, ...],
    source_keys: list[tuple[str, str]],
):
    """Fetch source-owned rows in bounded SQLite parameter batches."""
    unique_keys = sorted(set(source_keys))
    for offset in range(0, len(unique_keys), 300):
        batch = unique_keys[offset:offset + 300]
        placeholders = ",".join("(?,?)" for _ in batch)
        params = [*base_params, *(value for key in batch for value in key)]
        yield from connection.execute(
            f"{select_sql} AND (source,source_hotel_id) IN ({placeholders})", params,
        )


def _fetch_lookup_rows(
    connection: _ReadModelProfiledConnection,
    table: str,
    key_column: str,
    columns: str,
    keys: set[str],
) -> dict[str, tuple[Any, ...]]:
    """Read cache rows by stable key without one query per hotel."""
    rows: dict[str, tuple[Any, ...]] = {}
    ordered_keys = sorted(keys)
    for offset in range(0, len(ordered_keys), 400):
        batch = ordered_keys[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"SELECT {key_column},{columns} FROM {table} "
            f"WHERE {key_column} IN ({placeholders})", batch,
        ):
            rows[str(row[key_column])] = tuple(row)[1:]
    return rows


def _sravni_collection_audit(connection: sqlite3.Connection, search_id: str) -> dict[str, Any]:
    """Derive cursor and raw-card diagnostics from persisted provider pages."""
    rows = connection.execute(
        "SELECT page_index,raw_json FROM hotel_raw_page "
        "WHERE search_id=? AND source='sravniotel' ORDER BY page_index",
        (search_id,),
    ).fetchall()
    pages = []
    seen_ids: set[str] = set()
    repeated_requested: list[str] = []
    requested_tokens: list[str | None] = []
    totals: list[int | None] = []
    for row in rows:
        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            raw = {}
        cards = raw.get("hotels") if isinstance(raw.get("hotels"), list) else []
        ids = {
            str(card.get("id", card.get("source_hotel_id")))
            for card in cards
            if isinstance(card, dict) and card.get("id", card.get("source_hotel_id")) is not None
        }
        requested = raw.get("requestedPageToken")
        requested_tokens.append(requested)
        totals.append(raw.get("totalHotels") if isinstance(raw.get("totalHotels"), int) else None)
        pages.append({
            "page": row["page_index"], "cards": len(cards), "unique_ids": len(ids),
            "requested_cursor": requested, "next_cursor": raw.get("nextPageToken"),
            "reported_total": raw.get("totalHotels"),
        })
        seen_ids.update(ids)
    for token, count in collections.Counter(
        token for token in requested_tokens if token is not None
    ).items():
        if count > 1:
            repeated_requested.append(token)
    empty_followed = [
        pages[index]["page"] for index in range(len(pages) - 1)
        if pages[index]["cards"] == 0 and pages[index + 1]["cards"] > 0
    ]
    premature_end = [
        page["page"] for page in pages[:-1] if not page["next_cursor"]
    ]
    final_next = pages[-1]["next_cursor"] if pages else None
    stop_reasons = [
        str(raw.get("_paginationStopReason"))
        for row in rows
        for raw in [_safe_json_loads(row["raw_json"], {})]
        if isinstance(raw, dict) and raw.get("_paginationStopReason")
    ]
    exhausted = bool(pages) and not final_next and not stop_reasons
    complete = exhausted and not repeated_requested and not premature_end
    if complete:
        reason = "next_page_token_exhausted"
    elif stop_reasons:
        reason = stop_reasons[-1]
    elif repeated_requested:
        reason = "repeated_page_token"
    elif premature_end:
        reason = "premature_empty_cursor"
    elif not pages:
        reason = "no_pages"
    else:
        reason = "next_page_token_available"
    return {
        "pages": len(pages), "raw_cards": sum(page["cards"] for page in pages),
        "unique_cards": len(seen_ids), "final_next_page_token": final_next,
        "next_page_token_exhausted": exhausted, "collection_complete": complete,
        "collection_reason": reason, "repeated_requested_tokens": repeated_requested,
        "pagination_stop_reasons": stop_reasons,
        "premature_end_pages": premature_end, "empty_pages": sum(page["cards"] == 0 for page in pages),
        "empty_pages_followed_by_cards": empty_followed,
        "reported_totals": sorted({total for total in totals if total is not None}),
        "reported_total_changed": len({total for total in totals if total is not None}) > 1,
        "first_pages": pages[:5], "last_pages": pages[-5:],
    }

def _read_search(
    path: str | Path, search_id: str, *, source_hotel_ids: set[str] | None = None,
    source_identity_keys: set[tuple[str, str]] | None = None,
    matching_only: bool = False,
) -> dict[str, Any] | None:
    read_started = time.perf_counter()
    profile_ms: dict[str, float] = {
        "setup_ms": 0.0, "connection_open_ms": 0.0, "hotel_fetch_ms": 0.0, "identity_map_ms": 0.0,
        "hotel_build_ms": 0.0, "coordinate_queries_ms": 0.0,
        "offers_rates_ms": 0.0, "normalization_ms": 0.0, "audit_ms": 0.0,
    }
    db_profile = _ReadModelProfile()
    # Existing SQLite sessions predate resolution metadata; migrate lazily so
    # rereads and diagnostic tools see the same schema as new searches.
    stage_started = time.perf_counter()
    init_hotel_store(path)
    profile_ms["schema_init_ms"] = (time.perf_counter() - stage_started) * 1000
    connection_started = time.perf_counter()
    raw_connection = _profiled_sqlite_connect(str(path), timeout=10)
    profile_ms["connection_open_ms"] = (time.perf_counter() - connection_started) * 1000
    with closing(raw_connection) as raw_connection:
        connection = _ReadModelProfiledConnection(raw_connection, db_profile)
        connection.row_factory = sqlite3.Row
        stage_started = time.perf_counter()
        session = connection.execute("SELECT * FROM hotel_search_session WHERE id=?", (search_id,)).fetchone()
        if session is None:
            return None
        query = _safe_json_loads(session["query_json"], {})
        if not isinstance(query, dict):
            query = {}
        beach_profile = None
        if not matching_only:
            beach_profile = _read_city_geo_profile(
                connection, query.get("city_name") or "", query.get("country"),
                query.get("city_slug"), query.get("country_slug"),
            )
        hotel_sql = "SELECT * FROM hotel_observation WHERE search_id=?"
        hotel_params: list[Any] = [search_id]
        if source_hotel_ids is not None and source_identity_keys is not None:
            raise ValueError("source_hotel_ids and source_identity_keys are mutually exclusive")
        if source_hotel_ids is not None:
            if not source_hotel_ids:
                hotel_sql += " AND 1=0"
            else:
                hotel_sql += " AND source='trip_com' AND source_hotel_id IN (" + ",".join(
                    "?" for _ in source_hotel_ids
                ) + ")"
                hotel_params.extend(sorted(source_hotel_ids))
        hotel_sql += " ORDER BY id"
        profile_ms["setup_ms"] = (time.perf_counter() - stage_started) * 1000
        stage_started = time.perf_counter()
        if source_identity_keys is None:
            hotels = connection.execute(hotel_sql, hotel_params).fetchall()
        else:
            selected_keys = sorted(source_identity_keys)
            hotels = []
            for offset in range(0, len(selected_keys), 300):
                batch = selected_keys[offset:offset + 300]
                placeholders = ",".join("(?,?)" for _ in batch)
                key_params = [value for key in batch for value in key]
                hotels.extend(connection.execute(
                    hotel_sql.replace(" ORDER BY id", "")
                    + f" AND (source,source_hotel_id) IN ({placeholders}) ORDER BY id",
                    [*hotel_params, *key_params],
                ).fetchall())
            hotels.sort(key=lambda row: row["id"])
        profile_ms["hotel_fetch_ms"] = (time.perf_counter() - stage_started) * 1000
        hotel_rows_by_source = dict(collections.Counter(str(row["source"]) for row in hotels))

        if matching_only:
            # Pagination needs immutable matching evidence for the whole search,
            # not fully hydrated cards. Building the public read model here
            # reread Geo caches, offers, rates, and provider details on every
            # cursor page, even though matching consumes only source evidence.
            stage_started = time.perf_counter()
            source_keys = [(str(row["source"]), str(row["source_hotel_id"])) for row in hotels]
            identities: dict[tuple[str, str], str | None] = {}
            for offset in range(0, len(source_keys), 300):
                batch = source_keys[offset:offset + 300]
                placeholders = ",".join("(?,?)" for _ in batch)
                params = [value for key in batch for value in key]
                for identity in connection.execute(
                    "SELECT source,source_hotel_id,canonical_hotel_id FROM hotel_source_identity "
                    f"WHERE (source,source_hotel_id) IN ({placeholders})",
                    params,
                ):
                    identities[(str(identity["source"]), str(identity["source_hotel_id"]))] = (
                        str(identity["canonical_hotel_id"])
                        if identity["canonical_hotel_id"] is not None else None
                    )
            profile_ms["identity_map_ms"] = (time.perf_counter() - stage_started) * 1000

            stage_started = time.perf_counter()
            matching_hotels = []
            for row in hotels:
                raw_hotel = _safe_json_loads(row["raw_json"], {})
                if not isinstance(raw_hotel, dict):
                    raw_hotel = {}
                matching_coordinates = _coordinate_pair(raw_hotel.get("coordinates"))
                if matching_coordinates and _is_enrichment_coordinate_source(
                    raw_hotel.get("coordinate_source")
                ):
                    matching_coordinates = None
                stars = _normalize_hotel_stars(row["stars"])
                images = _safe_json_list(row["images_json"])
                matching_hotels.append({
                    "source": str(row["source"]),
                    "source_hotel_id": str(row["source_hotel_id"]),
                    "canonical_hotel_id": identities.get(
                        (str(row["source"]), str(row["source_hotel_id"]))
                    ),
                    "name": row["name"],
                    "address": row["address"],
                    "stars": stars,
                    "phone": raw_hotel.get("phone"),
                    "website": raw_hotel.get("website"),
                    "city": query.get("city_name"),
                    "country": raw_hotel.get("country"),
                    "chain": raw_hotel.get("chain"),
                    "coordinates": matching_coordinates,
                    "images": images,
                    "nearby_places": _safe_json_list(row["nearby_places_json"]),
                    "_match_evidence": {
                        "address": row["address"],
                        "stars": stars,
                        "coordinates": matching_coordinates,
                        "images": images,
                    },
                })
            profile_ms["hotel_build_ms"] = (time.perf_counter() - stage_started) * 1000
            log_search_timeline(
                search_id, "read_model_built",
                read_model_ms=round((time.perf_counter() - read_started) * 1000, 1),
                hotel_rows=len(matching_hotels), offers=0, scoped=source_hotel_ids is not None,
                source_identity_filter=(
                    len(source_identity_keys) if source_identity_keys is not None else None
                ), matching_only=True, matching_records_only=True,
                phases_ms={key: round(value, 1) for key, value in profile_ms.items()},
                db_query_count=db_profile.query_count, db_rows_fetched=db_profile.rows_fetched,
                db_execute_ms=round(db_profile.execute_ms, 1), db_fetch_ms=round(db_profile.fetch_ms, 1),
                db_queries_by_table_json=json.dumps(db_profile.queries_by_table, sort_keys=True,
                                                    separators=(",", ":")),
                db_rows_by_table_json=json.dumps(db_profile.rows_by_table, sort_keys=True,
                                                 separators=(",", ":")),
                hotel_rows_by_source_json=json.dumps(hotel_rows_by_source, sort_keys=True,
                                                     separators=(",", ":")),
            )
            return {"search_id": search_id, "query": query, "hotels": matching_hotels}

        result_hotels = []
        stage_started = time.perf_counter()
        identities = {
            (row["source"], row["source_hotel_id"]): row["canonical_hotel_id"]
            for row in connection.execute("SELECT source,source_hotel_id,canonical_hotel_id FROM hotel_source_identity")
        }
        profile_ms["identity_map_ms"] = (time.perf_counter() - stage_started) * 1000

        # The page endpoint rereads the same search after every Sravni cursor
        # page.  Keep the read semantics identical, but load the per-hotel
        # observations in bounded batches instead of issuing one SQLite query
        # for every row below.  Under concurrent Geo/rate writes those N+1
        # lookups were the dominant source of the application-time-budget
        # cutoff.
        source_keys = [(str(row["source"]), str(row["source_hotel_id"])) for row in hotels]

        def source_rows(select_sql: str, params: tuple[Any, ...] = ()):
            keys = sorted(set(source_keys))
            for offset in range(0, len(keys), 300):
                batch = keys[offset:offset + 300]
                key_placeholders = ",".join("(?,?)" for _ in batch)
                key_params = [value for key in batch for value in key]
                yield from connection.execute(
                    f"{select_sql} AND (source,source_hotel_id) IN ({key_placeholders})",
                    [*params, *key_params],
                )

        coordinate_observations: dict[tuple[str, str], sqlite3.Row] = {}
        if source_keys:
            for item in source_rows(
                "SELECT source,source_hotel_id,latitude,longitude,coordinate_source,"
                "coordinate_method,coordinate_confidence FROM hotel_coordinate_observation "
                "WHERE search_id=?", (search_id,),
            ):
                coordinate_observations[(str(item["source"]), str(item["source_hotel_id"]))] = item
        resolution_by_key: dict[str, tuple[Any, ...]] = {}
        identity_keys = {
            key for key in (
                str(identities.get(identity) or "")
                for identity in source_keys
            ) if key
        }
        identity_keys.update(f"{source}:{source_hotel_id}" for source, source_hotel_id in source_keys)
        if identity_keys:
            ordered_identity_keys = sorted(identity_keys)
            for offset in range(0, len(ordered_identity_keys), 400):
                batch = ordered_identity_keys[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                for item in connection.execute(
                    "SELECT hotel_key,status,reason,coordinate_method,coordinate_confidence,"
                    "attempts_json,retrieved_at FROM hotel_coordinate_resolution "
                    f"WHERE search_id=? AND hotel_key IN ({placeholders}) ORDER BY hotel_key",
                    [search_id, *batch],
                ):
                    resolution_by_key.setdefault(str(item["hotel_key"]), tuple(item)[1:])
        offers_by_key: dict[tuple[str, str], list[sqlite3.Row]] = collections.defaultdict(list)
        if not matching_only:
            for item in source_rows(
                "SELECT * FROM hotel_offer_observation WHERE search_id=?", (search_id,),
            ):
                offers_by_key[(str(item["source"]), str(item["source_hotel_id"]))].append(item)
        for rows_for_hotel in offers_by_key.values():
            rows_for_hotel.sort(key=lambda item: item["id"])
        provider_metadata_by_key: dict[tuple[str, str], list[sqlite3.Row]] = collections.defaultdict(list)
        for item in source_rows(
            "SELECT source,source_hotel_id,provider,metadata_json FROM hotel_provider_metadata "
            "WHERE search_id=?", (search_id,),
        ):
            provider_metadata_by_key[(str(item["source"]), str(item["source_hotel_id"]))].append(item)
        for rows_for_hotel in provider_metadata_by_key.values():
            rows_for_hotel.sort(key=lambda item: item["provider"] or "")
        latest_rate_fetch_by_key: dict[tuple[str, str], sqlite3.Row] = {}
        for item in source_rows(
            "SELECT id,source,source_hotel_id,status,http_status,latency_ms,rates_seen,observed_at,"
            "property_details_json FROM hotel_room_rate_fetch WHERE search_id=? AND check_in=? AND check_out=? "
            "AND adults=? AND children_count=?",
            (search_id, query.get("check_in"), query.get("check_out"), query.get("adults"),
             query.get("children_count", 0)),
        ):
            key = (str(item["source"]), str(item["source_hotel_id"]))
            current = latest_rate_fetch_by_key.get(key)
            if current is None or (item["observed_at"], item["id"]) > (current["observed_at"], current["id"]):
                latest_rate_fetch_by_key[key] = item
        rate_rows_by_key: dict[tuple[str, str, str], list[sqlite3.Row]] = collections.defaultdict(list)
        latest_fetch_keys = [
            (source, hotel_id, str(item["observed_at"]))
            for (source, hotel_id), item in latest_rate_fetch_by_key.items()
            if item["status"] == "ok"
        ]
        for offset in range(0, 0 if matching_only else len(latest_fetch_keys), 250):
            batch = latest_fetch_keys[offset:offset + 250]
            if not batch:
                continue
            key_placeholders = ",".join("(?,?,?)" for _ in batch)
            key_params = [value for key in batch for value in key]
            for item in connection.execute(
                """SELECT r.* FROM hotel_room_rate_observation r
                   WHERE r.search_id=? AND r.check_in=? AND r.check_out=?
                     AND r.adults=? AND r.children_count=?
                     AND (r.source,r.source_hotel_id,r.observed_at) IN ("""
                f"{key_placeholders}) AND r.observed_at=("
                "SELECT MAX(r2.observed_at) FROM hotel_room_rate_observation r2 "
                "WHERE r2.search_id=r.search_id AND r2.source=r.source "
                "AND r2.source_hotel_id=r.source_hotel_id "
                "AND r2.provider_rate_id=r.provider_rate_id "
                "AND r2.check_in=r.check_in AND r2.check_out=r.check_out "
                "AND r2.adults=r.adults AND r2.children_count=r.children_count)",
                [search_id, query.get("check_in"), query.get("check_out"), query.get("adults"),
                 query.get("children_count", 0), *key_params],
            ):
                key = (str(item["source"]), str(item["source_hotel_id"]), str(item["observed_at"]))
                rate_rows_by_key[key].append(item)

        raw_hotels_by_key = {
            (str(row["source"]), str(row["source_hotel_id"])): _safe_json_loads(row["raw_json"], {})
            for row in hotels
        }
        coordinate_cache_keys_by_hotel: dict[tuple[str, str], tuple[str, str]] = {}
        coordinate_cache_keys: set[str] = set()
        for row, key in zip(hotels, source_keys):
            raw_hotel = raw_hotels_by_key.get(key)
            if not isinstance(raw_hotel, dict):
                raw_hotel = {}
            provider_coordinates = _coordinate_pair(raw_hotel.get("coordinates"))
            if provider_coordinates and _is_enrichment_coordinate_source(raw_hotel.get("coordinate_source")):
                provider_coordinates = None
            persisted = coordinate_observations.get(key)
            persisted_coordinates = _coordinate_pair({"lat": persisted[2], "lng": persisted[3]}) \
                if persisted else None
            if provider_coordinates or persisted_coordinates:
                continue
            keys = (
                _hotel_coordinate_cache_key(
                    row["name"], query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"),
                    row["address"] or raw_hotel.get("address"),
                    query.get("city_slug"), query.get("country_slug"), key[0], key[1],
                ),
                _hotel_coordinate_cache_key(
                    row["name"], query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"),
                    row["address"] or raw_hotel.get("address"),
                    query.get("city_slug"), query.get("country_slug"),
                ),
            )
            coordinate_cache_keys_by_hotel[key] = keys
            coordinate_cache_keys.update(keys)
        coordinate_cache_by_key = _fetch_lookup_rows(
            connection, "hotel_coordinate_cache", "cache_key",
            "latitude,longitude,source,status", coordinate_cache_keys,
        )

        beach_keys_by_hotel: dict[tuple[str, str], tuple[str, str]] = {}
        geo_keys_by_hotel: dict[tuple[str, str], tuple[str, str, str]] = {}
        beach_keys: set[str] = set()
        geo_keys: set[str] = set()
        for row, key in zip(hotels, source_keys):
            raw_hotel = raw_hotels_by_key.get(key)
            if not isinstance(raw_hotel, dict):
                raw_hotel = {}
            provider_coordinates = _coordinate_pair(raw_hotel.get("coordinates"))
            geocoded_coordinates = None
            if provider_coordinates and _is_enrichment_coordinate_source(raw_hotel.get("coordinate_source")):
                geocoded_coordinates, provider_coordinates = provider_coordinates, None
            if not provider_coordinates:
                persisted = coordinate_observations.get(key)
                if persisted:
                    geocoded_coordinates = _coordinate_pair({"lat": persisted[2], "lng": persisted[3]})
                if not geocoded_coordinates:
                    for cache_key in coordinate_cache_keys_by_hotel.get(key, ()):
                        cached = coordinate_cache_by_key.get(cache_key)
                        if cached and cached[3] == "ok":
                            geocoded_coordinates = _coordinate_pair({"lat": cached[0], "lng": cached[1]})
                            if geocoded_coordinates:
                                break
            coordinate_for_cache = provider_coordinates or geocoded_coordinates
            if not coordinate_for_cache:
                continue
            hotel_beach_keys = (
                _hotel_beach_cache_key(
                    coordinate_for_cache, query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"),
                ),
                _hotel_beach_cache_key(
                    coordinate_for_cache, query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"), "osm-beach-geometry-v14",
                ),
                _hotel_beach_cache_key(
                    coordinate_for_cache, query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"), "osm-beach-geometry-v11",
                ),
            )
            beach_keys_by_hotel[key] = hotel_beach_keys
            beach_keys.update(hotel_beach_keys)
            hotel_geo_keys = (
                _overpass_nearby_tile_key(
                    coordinate_for_cache["lat"], coordinate_for_cache["lng"])[0],
                _overpass_tile_key(
                    coordinate_for_cache["lat"], coordinate_for_cache["lng"])[0],
                _overpass_legacy_tile_key(
                    coordinate_for_cache["lat"], coordinate_for_cache["lng"]),
            )
            geo_keys_by_hotel[key] = hotel_geo_keys
            geo_keys.update(hotel_geo_keys)
        beach_cache_by_key = _fetch_lookup_rows(
            connection, "hotel_beach_cache", "cache_key",
            "distance_meters,beach_name,source,status,retrieved_at,beach_osm_type,"
            "beach_osm_id,beach_method,beach_confidence", beach_keys,
        )
        geo_cache_by_key = _fetch_lookup_rows(
            connection, "hotel_geo_feature_cache", "tile_key",
            "features_json,source,status,retrieved_at", geo_keys,
        )
        hotel_build_started = time.perf_counter()
        for row in hotels:
            row_started = time.perf_counter()
            raw_hotel = _safe_json_loads(row["raw_json"], {})
            if not isinstance(raw_hotel, dict):
                raw_hotel = {}
            rating = row["rating"]
            if (not isinstance(rating, (int, float)) or isinstance(rating, bool)
                    or not 0 < rating <= 10):
                rating = None
            coordinate_started = time.perf_counter()
            raw_coordinate_source = raw_hotel.get("coordinate_source")
            provider_coordinates = _coordinate_pair(raw_hotel.get("coordinates"))
            geocoded_coordinates = None
            geocoded_coordinate_source = None
            geocoded_coordinate_method = None
            geocoded_coordinate_confidence = None
            if provider_coordinates and _is_enrichment_coordinate_source(raw_coordinate_source):
                geocoded_coordinates = provider_coordinates
                geocoded_coordinate_source = raw_coordinate_source
                provider_coordinates = None
            coordinate_source = None if geocoded_coordinates else raw_coordinate_source
            if not provider_coordinates:
                persisted_coordinate = coordinate_observations.get(
                    (str(row["source"]), str(row["source_hotel_id"])))
                if persisted_coordinate:
                    geocoded_coordinates = _coordinate_pair(
                        {"lat": persisted_coordinate[2], "lng": persisted_coordinate[3]})
                    geocoded_coordinate_source = persisted_coordinate[4]
                    geocoded_coordinate_method = persisted_coordinate[5]
                    geocoded_coordinate_confidence = persisted_coordinate[6]
            matching_coordinates = provider_coordinates
            resolution_keys = [
                str(identities.get((row["source"], row["source_hotel_id"])) or ""),
                f"{row['source']}:{row['source_hotel_id']}",
            ]
            resolution = next((resolution_by_key[key] for key in sorted(resolution_keys)
                               if key in resolution_by_key), None)
            if not provider_coordinates and not geocoded_coordinates:
                cache_key = _hotel_coordinate_cache_key(
                    row["name"], query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"),
                    row["address"] or raw_hotel.get("address"),
                    query.get("city_slug"), query.get("country_slug"),
                    row["source"], row["source_hotel_id"],
                )
                legacy_cache_key = _hotel_coordinate_cache_key(
                    row["name"], query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"),
                    row["address"] or raw_hotel.get("address"),
                    query.get("city_slug"), query.get("country_slug"),
                )
                cached_coordinate = next((coordinate_cache_by_key.get(key)
                                          for key in (cache_key, legacy_cache_key)
                                          if coordinate_cache_by_key.get(key)
                                          and coordinate_cache_by_key[key][3] == "ok"), None)
                if cached_coordinate:
                    geocoded_coordinates = _coordinate_pair(
                        {"lat": cached_coordinate[0], "lng": cached_coordinate[1]})
                    geocoded_coordinate_source = cached_coordinate[2] if geocoded_coordinates else None
            coordinate_for_beach = provider_coordinates or geocoded_coordinates
            cached_beach = None
            if coordinate_for_beach:
                beach_key = _hotel_beach_cache_key(
                    coordinate_for_beach, query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"),
                )
                previous_beach_key = _hotel_beach_cache_key(
                    coordinate_for_beach, query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"), "osm-beach-geometry-v14")
                legacy_beach_key = _hotel_beach_cache_key(
                    coordinate_for_beach, query.get("city_name") or "",
                    raw_hotel.get("country") or query.get("country"), "osm-beach-geometry-v11")
                cached_beach = next((beach_cache_by_key.get(key) for key in (
                    beach_key, previous_beach_key, legacy_beach_key)
                                     if beach_cache_by_key.get(key) is not None
                                     and beach_cache_by_key[key][2] != "openstreetmap_photon_geometry"
                                     and beach_cache_by_key[key][7] != "nearest_osm_beach_point"), None)
                if cached_beach:
                    try:
                        beach_age = datetime.now(timezone.utc) - datetime.fromisoformat(
                            cached_beach[4]).astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        beach_age = timedelta.max
                    beach_ttl = (timedelta(days=30)
                                 if cached_beach[3] == "ok"
                                 else timedelta(days=1)
                                 if cached_beach[3] == "not_found"
                                 else timedelta(minutes=1))
                    if beach_age >= beach_ttl:
                        cached_beach = None
            cached_geo_places: dict[str, dict[str, Any]] = {}
            cached_geo_status = None
            cached_geo_source = None
            if coordinate_for_beach:
                geo_tile_key, _geo_lat, _geo_lon = _overpass_nearby_tile_key(
                    coordinate_for_beach["lat"], coordinate_for_beach["lng"])
                legacy_geo_tile_key, _legacy_lat, _legacy_lon = _overpass_tile_key(
                    coordinate_for_beach["lat"], coordinate_for_beach["lng"])
                pre_v4_geo_tile_key = _overpass_legacy_tile_key(
                    coordinate_for_beach["lat"], coordinate_for_beach["lng"])
                cached_geo = next((geo_cache_by_key.get(key) for key in (
                    geo_tile_key, legacy_geo_tile_key, pre_v4_geo_tile_key)
                    if geo_cache_by_key.get(key) is not None), None)
                if cached_geo:
                    try:
                        geo_age = datetime.now(timezone.utc) - datetime.fromisoformat(
                            cached_geo[3]).astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        geo_age = timedelta.max
                    geo_ttl = (timedelta(days=30) if cached_geo[2] == "ok" else
                               timedelta(days=1) if cached_geo[2] == "not_found" else
                               timedelta(minutes=1))
                    if geo_age < geo_ttl:
                        cached_geo_status, cached_geo_source = cached_geo[2], cached_geo[1]
                        for feature in _safe_json_loads(cached_geo[0] or "[]", []) if cached_geo[2] == "ok" else []:
                            point = _coordinate_pair({"lat": feature.get("latitude"),
                                                      "lng": feature.get("longitude")})
                            if not point:
                                continue
                            distance = _haversine_m(coordinate_for_beach["lat"], coordinate_for_beach["lng"],
                                                    point["lat"], point["lng"])
                            category = feature.get("type")
                            if category not in {"metro", "airport", "landmark", "restaurant", "shopping"}:
                                continue
                            candidate = {**feature, "distance_meters": round(distance, 1),
                                         "distance_km": round(distance / 1000, 3),
                                         "method": "haversine_osm_feature"}
                            if category not in cached_geo_places or distance < cached_geo_places[category]["distance_meters"]:
                                cached_geo_places[category] = candidate
            profile_ms["coordinate_queries_ms"] += (time.perf_counter() - coordinate_started) * 1000
            rates_started = time.perf_counter()
            hotel_key = (str(row["source"]), str(row["source_hotel_id"]))
            offers = offers_by_key.get(hotel_key, [])
            normalized_offers = []
            for offer in offers:
                normalization_started = time.perf_counter()
                payload = _safe_json_loads(offer["raw_json"], {})
                if not isinstance(payload, dict):
                    payload = {}
                labels = _safe_json_loads(offer["labels_json"], [])
                if not isinstance(labels, list):
                    labels = []
                tax_status = "included" if "taxes_included" in labels else payload.get("tax_status", "unknown")
                cancellation = (
                    "free" if "free_cancellation" in labels else
                    "non_refundable" if "non_refundable" in labels else
                    payload.get("cancellation", "unknown")
                )
                normalized_offers.append({
                    **payload,
                    "provider": offer["provider"], "amount": offer["amount"],
                    "source_price_value": offer["source_price_value"], "currency": offer["currency"],
                    "deeplink": offer["deeplink"], "labels": labels,
                    "tax_status": tax_status,
                    "availability": "unknown",
                    "cancellation": cancellation,
                    "observed_at": offer["observed_at"],
                })
                profile_ms["normalization_ms"] += (time.perf_counter() - normalization_started) * 1000
            latest_rate_fetch = latest_rate_fetch_by_key.get(hotel_key)
            rate_rows = rate_rows_by_key.get((*hotel_key, str(latest_rate_fetch["observed_at"])), []) \
                if latest_rate_fetch and latest_rate_fetch["status"] == "ok" else []
            rate_rows = sorted(rate_rows, key=lambda item: (
                item["room_group_index"] if item["room_group_index"] is not None else -1,
                str(item["provider_rate_id"]),
            ))
            room_rates = [payload for rate_row in rate_rows
                          for payload in [_safe_json_loads(rate_row["raw_json"], None)]
                          if isinstance(payload, dict)]
            provider_detail_rows = provider_metadata_by_key.get(hotel_key, [])
            provider_details = [payload for detail in provider_detail_rows
                                for payload in [_safe_json_loads(detail["metadata_json"], None)]
                                if isinstance(payload, dict)]
            room_options_by_id: dict[str, dict[str, Any]] = {}
            for rate in room_rates:
                room_identity = str(rate.get("provider_room_id") or
                                    (f"group-{rate['room_group_index']}" if rate.get("room_group_index") is not None
                                     else f"rate-{rate.get('provider_rate_id')}"))
                room_option = room_options_by_id.setdefault(room_identity, {
                    "room_id": rate.get("provider_room_id"),
                    "room_group_index": rate.get("room_group_index"),
                    "room_type": rate.get("room_type"),
                    "amenities": rate.get("room_amenities"),
                    "rates": [],
                })
                room_option["rates"].append(rate)
            property_details = _safe_json_loads(latest_rate_fetch["property_details_json"], None) \
                if latest_rate_fetch and latest_rate_fetch["property_details_json"] else None
            property_details = property_details if isinstance(property_details, dict) else None
            detail_coordinates = _coordinate_pair(property_details.get("coordinates")) if property_details else None
            if not provider_coordinates and detail_coordinates:
                provider_coordinates = detail_coordinates
                coordinate_source = "trip_com_detail"
            # Older persisted searches may contain provider sentinels such as
            # stars=0. Sanitize every persistence source on read so stale rows
            # cannot turn an unknown classification into a known value.
            stars = _normalize_hotel_stars(row["stars"])
            if stars is None:
                stars = _normalize_hotel_stars(
                    property_details.get("stars") if property_details else raw_hotel.get("stars"))
            rating_scale = row["rating_scale"] if row["rating_scale"] in (5, 10) else (
                10 if rating is not None else None)
            rating_source = row["rating_source"] or (row["source"] if rating is not None else None)
            rating_original = row["rating_original"] if row["rating_original"] is not None else rating
            rating_fields = _normalize_rating_fields(rating_original, rating_scale, rating_source)
            if rating_fields["rating_original"] is None and property_details:
                rating_fields = _normalize_rating_fields(
                    property_details.get("rating_original", property_details.get("rating")),
                    property_details.get("rating_scale"),
                    property_details.get("rating_source") or "trip_com",
                )
            rating = rating_fields["rating_original"]
            reviews_count = row["reviews_count"] if row["reviews_count"] is not None else (
                property_details.get("reviews_count") if property_details else None)
            persisted_amenities = raw_hotel.get("amenities")
            if not isinstance(persisted_amenities, list):
                persisted_amenities = extract_amenities(raw_hotel)
            else:
                persisted_amenities = list(persisted_amenities)
            if property_details:
                persisted_amenities = list(dict.fromkeys([
                    *persisted_amenities, *property_details.get("amenities", []),
                ]))
            images = _safe_json_list(row["images_json"])
            matching_images = list(images)
            if property_details and property_details.get("images"):
                image_keys = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in images}
                for image_url in property_details["images"]:
                    image = {"url": image_url, "role": "hotel_photo", "source": "trip_com"}
                    key = json.dumps(image, sort_keys=True, ensure_ascii=False)
                    if key not in image_keys:
                        images.append(image)
                        image_keys.add(key)
            result_hotels.append({
                "source": row["source"], "source_hotel_id": row["source_hotel_id"],
                "canonical_hotel_id": identities.get((row["source"], row["source_hotel_id"])),
                "_match_evidence": {
                    # These values are read directly from the immutable source
                    # observation. Later Geo/rate enrichment may fill public
                    # fields below, but must not alter cross-source matching.
                    "address": row["address"],
                    "stars": _normalize_hotel_stars(row["stars"]),
                    "coordinates": matching_coordinates,
                    "images": matching_images,
                },
                "name": row["name"], "normalized_name": normalize_hotel_name(row["name"]),
                "address": row["address"] or (property_details.get("address") if property_details else None),
                "stars": stars, "rating": rating, **rating_fields,
                "reviews_count": reviews_count, "coordinates": provider_coordinates,
                "coordinate_source": coordinate_source,
                "geocoded_coordinates": geocoded_coordinates,
                "geocoded_coordinate_source": geocoded_coordinate_source,
                "coordinate_method": (
                    resolution[2] if resolution and resolution[2] else
                    geocoded_coordinate_method if geocoded_coordinates else
                    _coordinate_source_metadata(coordinate_source)[0]
                    if provider_coordinates else None
                ),
                "coordinate_confidence": (
                    resolution[3] if resolution and resolution[3] else
                    geocoded_coordinate_confidence if geocoded_coordinates else
                    _coordinate_source_metadata(coordinate_source)[1]
                    if provider_coordinates else None
                ),
                "coordinate_status": (
                    resolution[0] if resolution else
                    "PROVIDER_DETAIL" if provider_coordinates and
                    _coordinate_source_metadata(coordinate_source)[0] == "hotel_detail_structured_data" else
                    "PROVIDER_COORDINATES" if provider_coordinates else
                    "PROVIDER_DETAIL" if geocoded_coordinates and geocoded_coordinate_source and
                    _coordinate_source_metadata(geocoded_coordinate_source)[0] == "hotel_detail_structured_data" else
                    "GEOCODER" if geocoded_coordinates else "PENDING"
                ),
                "coordinate_reason": resolution[1] if resolution else None,
                "coordinate_attempts": _safe_json_list(resolution[4]) if resolution and resolution[4] else [],
                "coordinate_resolution_retrieved_at": resolution[5] if resolution else None,
                "cached_beach_distance_meters": cached_beach[0] if cached_beach and cached_beach[3] == "ok" else None,
                "cached_beach_name": cached_beach[1] if cached_beach and cached_beach[3] == "ok" else None,
                "cached_beach_source": cached_beach[2] if cached_beach and cached_beach[3] == "ok" else None,
                "cached_beach_status": ("unknown" if cached_beach and cached_beach[7] == "nearest_osm_beach_point"
                                         else cached_beach[3] if cached_beach else None),
                "cached_beach_osm_type": cached_beach[5] if cached_beach else None,
                "cached_beach_osm_id": cached_beach[6] if cached_beach else None,
                "cached_beach_method": cached_beach[7] if cached_beach else None,
                "cached_beach_confidence": cached_beach[8] if cached_beach else None,
                "cached_geo_places": cached_geo_places,
                "cached_geo_status": cached_geo_status,
                "cached_geo_source": cached_geo_source,
                "cached_beach_relevant": beach_profile.is_beach_destination if beach_profile else None,
                "cached_beach_geometry_available": (
                    True if cached_beach and cached_beach[3] == "ok"
                    and cached_beach[2] != "openstreetmap_photon_geometry" else
                    False if cached_beach and cached_beach[3] == "not_found" else None
                ),
                "phone": raw_hotel.get("phone"), "website": raw_hotel.get("website"),
                "chain": raw_hotel.get("chain"), "city": query.get("city_name"),
                "country": raw_hotel.get("country"),
                "beach_access": raw_hotel.get("beach_access") or raw_hotel.get("beachAccess") or "unknown",
                "beach_line": normalize_beach_line(raw_hotel.get("beach_line") or raw_hotel.get("beachLine")
                                                    or raw_hotel.get("beachfront")),
                "beach_line_evidence": raw_hotel.get("beach_line") or raw_hotel.get("beachLine")
                    or raw_hotel.get("beachfront"),
                "beach_line_source": "source" if normalize_beach_line(
                    raw_hotel.get("beach_line") or raw_hotel.get("beachLine") or raw_hotel.get("beachfront")
                ) != "unknown" else None,
                "raw_payload": raw_hotel,
                "review_quotes": raw_hotel.get("review_quotes") or [],
                "review_source": raw_hotel.get("review_source"),
                "images": images,
                "nearby_places": _safe_json_list(row["nearby_places_json"]),
                "amenities": persisted_amenities,
                "room_options": list(room_options_by_id.values()), "room_rates": room_rates,
                "property_details": property_details,
                "review_url": property_details.get("review_url") if property_details else None,
                "room_rate_enrichment_status": latest_rate_fetch["status"] if latest_rate_fetch else None,
                "room_rate_fetch": ({key: latest_rate_fetch[key] for key in (
                    "status", "http_status", "latency_ms", "rates_seen", "observed_at",
                    "property_details_json",
                )} if latest_rate_fetch else None),
                "provider_details": provider_details,
                "observed_at": row["observed_at"],
                "offers": normalized_offers,
            })
            profile_ms["offers_rates_ms"] += (time.perf_counter() - rates_started) * 1000
            profile_ms["hotel_build_ms"] += (time.perf_counter() - row_started) * 1000
        stage_started = time.perf_counter()
        runs = connection.execute(
            "SELECT * FROM hotel_source_run WHERE search_id=? ORDER BY id", (search_id,)
        ).fetchall()
        collection_audits = {}
        for audit_row in connection.execute(
            "SELECT source,raw_json FROM hotel_raw_page WHERE search_id=? ORDER BY page_index DESC",
            (search_id,),
        ):
            if audit_row["source"] in collection_audits:
                continue
            try:
                raw_payload = json.loads(audit_row["raw_json"])
                raw_audit = raw_payload.get("collectionAudit")
            except (TypeError, ValueError):
                raw_payload = {}
                raw_audit = None
            if raw_audit is not None:
                if audit_row["source"] == "trip_com" and isinstance(raw_audit, dict):
                    # Older persisted Trip.com audits predate the explicit
                    # card counters. Derive them from the saved raw page so
                    # search_audit remains exact across rereads.
                    raw_audit = dict(raw_audit)
                    cards = raw_payload.get("hotels") if isinstance(raw_payload.get("hotels"), list) else []
                    ids = {
                        str(card.get("id", card.get("source_hotel_id")))
                        for card in cards
                        if isinstance(card, dict) and card.get("id", card.get("source_hotel_id")) is not None
                    }
                    raw_audit.setdefault("raw_cards", len(cards))
                    raw_audit.setdefault("unique_cards", len(ids))
                collection_audits[audit_row["source"]] = raw_audit
            elif audit_row["source"] == "trip_com":
                cards = raw_payload.get("hotels") if isinstance(raw_payload.get("hotels"), list) else []
                ids = {
                    str(card.get("id", card.get("source_hotel_id")))
                    for card in cards
                    if isinstance(card, dict) and card.get("id", card.get("source_hotel_id")) is not None
                }
                collection_audits[audit_row["source"]] = {
                    "pages": 1, "raw_cards": len(cards), "unique_cards": len(ids),
                    "collection_complete": None, "collection_reason": "not_recorded",
                }
        sravni_audit = _sravni_collection_audit(connection, search_id)
        collection_audits["sravniotel"] = sravni_audit
        provider_page = connection.execute(
            "SELECT raw_json FROM hotel_raw_page WHERE search_id=? AND source='sravniotel' ORDER BY page_index LIMIT 1",
            (search_id,),
        ).fetchone()
        provider_directory = []
        if provider_page:
            raw_page = _safe_json_loads(provider_page["raw_json"], {})
            if not isinstance(raw_page, dict):
                raw_page = {}
            provider_directory = raw_page.get("providers", []) if isinstance(raw_page.get("providers"), list) else []
        profile_ms["audit_ms"] = (time.perf_counter() - stage_started) * 1000
        result = {
            "search_id": search_id, "query": query, "status": session["status"],
            "total_hotels": session["total_hotels"], "has_more": bool(session["next_page_token"]),
            "next_page_token": session["next_page_token"], "hotels": result_hotels,
            "providers": provider_directory,
            "sources": [{"source": run["source"], "status": run["status"],
                         "http_status": run["http_status"],
                         "latency_ms": run["latency_ms"], "pages": run["pages"],
                         "cache_state": run["cache_state"],
                         "reported_total": run["reported_total"],
                         "collection_complete": (
                             (collection_audits[run["source"]].get("collection_complete")
                              if run["source"] == "sravniotel" else None)
                             if ("collection_complete" not in run.keys()
                                 or run["collection_complete"] is None)
                             else bool(run["collection_complete"])
                         ),
                        "collection_reason": (
                             (collection_audits[run["source"]].get("collection_reason")
                              if run["source"] == "sravniotel" else None)
                             if "collection_reason" not in run.keys() or run["collection_reason"] is None
                             else run["collection_reason"]
                         ),
                         "collection_audit": collection_audits.get(run["source"]),
                         "hotel_count": run["hotel_count"], "offer_count": run["offer_count"],
                         "error_class": run["error_class"], "retrieved_at": run["observed_at"]}
                        for run in runs],
            "updated_at": session["finished_at"] or session["started_at"],
        }
        object_counts = {
            "provider_hotels": len(result_hotels),
            "trip_hotels": hotel_rows_by_source.get("trip_com", 0),
            "sravni_hotels": hotel_rows_by_source.get("sravniotel", 0),
            "offers": sum(len(hotel.get("offers", [])) for hotel in result_hotels),
            "room_rates": sum(len(hotel.get("room_rates", [])) for hotel in result_hotels),
            "room_options": sum(len(hotel.get("room_options", [])) for hotel in result_hotels),
            "provider_details": sum(len(hotel.get("provider_details", [])) for hotel in result_hotels),
        }
        log_search_timeline(
            search_id, "read_model_built",
            read_model_ms=round((time.perf_counter() - read_started) * 1000, 1),
            hotel_rows=len(result_hotels),
            offers=object_counts["offers"],
            scoped=source_hotel_ids is not None,
            source_identity_filter=(len(source_identity_keys) if source_identity_keys is not None else None),
            matching_only=matching_only,
            phases_ms={key: round(value, 1) for key, value in profile_ms.items()},
            db_query_count=db_profile.query_count,
            db_rows_fetched=db_profile.rows_fetched,
            db_execute_ms=round(db_profile.execute_ms, 1),
            db_fetch_ms=round(db_profile.fetch_ms, 1),
            db_queries_by_table_json=json.dumps(db_profile.queries_by_table, sort_keys=True,
                                                separators=(",", ":")),
            db_rows_by_table_json=json.dumps(db_profile.rows_by_table, sort_keys=True,
                                             separators=(",", ":")),
            db_slowest_queries_json=json.dumps(db_profile.slowest, ensure_ascii=False,
                                               separators=(",", ":")),
            hotel_rows_by_source_json=json.dumps(hotel_rows_by_source, sort_keys=True,
                                                 separators=(",", ":")),
            object_counts_json=json.dumps(object_counts, sort_keys=True, separators=(",", ":")),
            python_other_ms=round(max(0.0, (time.perf_counter() - read_started)
                                      * 1000 - profile_ms["connection_open_ms"]
                                      - profile_ms["schema_init_ms"] - db_profile.execute_ms
                                      - db_profile.fetch_ms - profile_ms["normalization_ms"]), 1),
            serialization_ms="measured_at_api_boundary",
        )
        return result
