from __future__ import annotations

import asyncio
import logging
import json
from functools import lru_cache
from contextlib import asynccontextmanager, closing
from importlib.resources import files
from pathlib import Path
import sqlite3
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse

from .database import MonitorSettings, SQLiteStore
from .flight_routes import city_code
from .hotels.api import build_router
from .hotels.schema import init_hotel_store
from .hotels.sources.tripcom import TRIP_EXECUTOR, shutdown_tripcom_browser
from .trip_flight_compare import TripFlightCompareError, build_offer_context, compare_trip_offer

log = logging.getLogger(__name__)
ASSETS = files("aviasales_collector").joinpath("web_assets")
ORIGIN_PATTERN = r"^[A-Z]{3}$"
CITY_AUTOCOMPLETE_URL = "https://autocomplete.travelpayouts.com/places2"
BROWSER_ORIGINS = frozenset({"http://127.0.0.1:8765", "http://localhost:8765"})
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
MAX_CITY_RESPONSE_BYTES = 1_048_576
DEEP_PROGRESS_RE = re.compile(r"deep scan completed partially:\s*(\d+)\s*/\s*(\d+)\s*destinations", re.IGNORECASE)
DEEP_LIMIT_RE = re.compile(r"deep scan limited to\s+(\d+)\s+active destinations;\s+(\d+)\s+skipped", re.IGNORECASE)
DEEP_FAILURE_RE = re.compile(r"^(.+?):\s*(?:CollectorError|TimeoutError|Error):", re.IGNORECASE)


@lru_cache(maxsize=128)
def _search_cities(term: str) -> tuple[dict[str, str], ...]:
    params = urlencode({"term": term, "locale": "ru", "types[]": "city"})
    request = URLRequest(f"{CITY_AUTOCOMPLETE_URL}?{params}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read(MAX_CITY_RESPONSE_BYTES + 1)
            if len(body) > MAX_CITY_RESPONSE_BYTES:
                raise HTTPException(status_code=502, detail="City lookup returned an oversized response")
            payload = json.loads(body.decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail="City lookup is temporarily unavailable") from exc
    cities = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").upper()
        name = " ".join(str(item.get("name") or "").split())
        if item.get("type") == "city" and len(code) == 3 and code.isascii() and code.isalpha() and name:
            city = {"code": code, "name": name, "country": str(item.get("country_name") or "")}
            if city not in cities:
                cities.append(city)
        if len(cities) == 8:
            break
    return tuple(cities)


def _require_local_request(request: Request) -> None:
    request_origin = request.headers.get("origin")
    if request_origin not in BROWSER_ORIGINS:
        raise HTTPException(status_code=403, detail="Request origin is not allowed")


def _integer_field(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _airports_by_leg(
    airports: Any,
    stops: Any,
    trip_type: str,
    route_identity: Any = None,
) -> list[list[dict[str, Any]]]:
    """Return airport chains for each leg, preferring verified itinerary data."""
    identity = route_identity if isinstance(route_identity, dict) else {}
    if identity.get("route_valid") is True:
        segment_legs = [identity.get("outbound_segments"), identity.get("return_segments")]
        if trip_type != "RT":
            segment_legs = segment_legs[:1]
        if all(isinstance(segments, list) and segments for segments in segment_legs):
            known = {
                str(item.get("code") or "").strip().upper(): item
                for item in (airports if isinstance(airports, list) else [])
                if isinstance(item, dict) and item.get("code")
            }
            result: list[list[dict[str, Any]]] = []
            for segments in segment_legs:
                chain: list[dict[str, Any]] = []
                for segment in segments:
                    if not isinstance(segment, dict):
                        continue
                    origin = str(segment.get("origin") or "").strip().upper()
                    destination = str(segment.get("destination") or "").strip().upper()
                    if not origin or not destination:
                        continue
                    if not chain or chain[-1].get("code") != origin:
                        chain.append({**known.get(origin, {}), "code": origin})
                    chain.append({**known.get(destination, {}), "code": destination})
                if len(chain) >= 2:
                    result.append(chain)
            if len(result) == len(segment_legs):
                return result

    points = []
    for airport in airports if isinstance(airports, list) else []:
        if not isinstance(airport, dict):
            continue
        code = str(airport.get("code") or "").strip().upper()
        if len(code) != 3 or not code.isascii() or not code.isalpha():
            code = ""
        points.append({**airport, "code": code})

    if trip_type != "RT":
        return [points]
    if len(points) == 2:
        return [points, list(reversed(points))]

    stop_counts = stops if isinstance(stops, list) else [stops]
    if len(stop_counts) >= 2 and all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in stop_counts[:2]
    ):
        lengths = [value + 2 for value in stop_counts[:2]]
        if len(points) == sum(lengths):
            split = lengths[0]
            return [points[:split], points[split:]]

    if len(points) == 3:
        return [points[:2], points[1:]]
    if len(points) >= 4:
        return [points[:2], points[-2:]]
    return [[], []]


def _read_scans(
    database_path: str | Path,
    user_ids: tuple[int, ...],
    origin: str,
    scan_kind: str,
    query: str,
    active_only: bool,
    limit: int,
    offset: int,
    current_only: bool = True,
) -> tuple[int, list[dict[str, Any]]]:
    if not user_ids:
        return 0, []
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    placeholders = ",".join("?" for _ in user_ids)
    where = f"user_id IN ({placeholders})"
    params: list[Any] = [*user_ids]
    if origin != "ALL":
        where += " AND origin = ?"
        params.append(origin)
    if scan_kind != "all":
        where += " AND scan_kind = ?"
        params.append(scan_kind)
    if current_only:
        usable_scan = "((scan_history.scan_kind = 'deep' AND EXISTS (" \
            "SELECT 1 FROM offers usable_offer WHERE usable_offer.scan_id = scan_history.id)) " \
            "OR (scan_history.complete = 1 AND scan_history.partial = 0))"
        usable_newer_scan = "((newer.scan_kind = 'deep' AND EXISTS (" \
            "SELECT 1 FROM offers newer_offer WHERE newer_offer.scan_id = newer.id)) " \
            "OR (newer.complete = 1 AND newer.partial = 0))"
        where += f" AND {usable_scan} AND NOT EXISTS (" \
            "SELECT 1 FROM scan_history newer WHERE newer.user_id = scan_history.user_id " \
            "AND newer.chat_id = scan_history.chat_id AND newer.origin = scan_history.origin " \
            f"AND newer.scan_kind = scan_history.scan_kind AND {usable_newer_scan} " \
            "AND (newer.finished_at > scan_history.finished_at OR " \
            "(newer.finished_at = scan_history.finished_at AND newer.id > scan_history.id)))"
    if active_only:
        where += " AND EXISTS (SELECT 1 FROM offers active_offer " \
            "JOIN destinations active_destination ON active_destination.id = active_offer.destination_id " \
            "WHERE active_offer.scan_id = scan_history.id AND active_destination.active = 1 " \
            "AND active_destination.archived = 0)"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if query:
            candidates = connection.execute(
                f"SELECT * FROM scan_history WHERE {where} ORDER BY finished_at DESC, id DESC",
                params,
            ).fetchall()
            matching = []
            for row in candidates:
                matches = connection.execute(
                    """
                    SELECT offers.destination, destinations.active, destinations.archived
                    FROM offers
                    LEFT JOIN destinations ON destinations.id = offers.destination_id
                        AND destinations.user_id = ? AND destinations.chat_id = ?
                    WHERE offers.scan_id = ?
                    """,
                    (row["user_id"], row["chat_id"], row["id"]),
                ).fetchall()
                if any(
                    query.casefold() in str(item["destination"] or "").casefold()
                    and not bool(item["archived"])
                    and (not active_only or bool(item["active"]))
                    for item in matches
                ):
                    matching.append(row)
            total = len(matching)
            rows = matching[offset:offset + limit]
        else:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM scan_history WHERE {where}", params
            ).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM scan_history WHERE {where} "
                "ORDER BY finished_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        output = []
        for row in rows:
            offers = connection.execute(
                """
                SELECT offers.*, destinations.active AS destination_active,
                    destinations.archived AS destination_archived,
                    previous_offer.price AS previous_price,
                    previous_offer.currency AS previous_currency
                FROM offers
                LEFT JOIN destinations ON destinations.id = offers.destination_id
                    AND destinations.user_id = ? AND destinations.chat_id = ?
                LEFT JOIN offers previous_offer ON previous_offer.id = (
                    SELECT candidate.id
                    FROM offers candidate
                    JOIN scan_history previous_scan ON previous_scan.id = candidate.scan_id
                    WHERE candidate.destination_id = offers.destination_id
                        AND candidate.route_valid = 1
                        AND candidate.price IS NOT NULL
                        AND previous_scan.user_id = ? AND previous_scan.chat_id = ?
                        AND previous_scan.origin = ? AND previous_scan.scan_kind = ?
                        AND previous_scan.trip_type = ? AND previous_scan.complete = 1
                        AND (previous_scan.finished_at < ? OR
                            (previous_scan.finished_at = ? AND previous_scan.id < ?))
                    ORDER BY previous_scan.finished_at DESC, previous_scan.id DESC,
                        candidate.price ASC, candidate.id
                    LIMIT 1
                )
                WHERE offers.scan_id = ? AND offers.route_valid = 1 ORDER BY offers.offer_index
                """,
                (
                    row["user_id"], row["chat_id"], row["user_id"], row["chat_id"],
                    row["origin"], row["scan_kind"], row["trip_type"],
                    row["finished_at"], row["finished_at"], row["id"], row["id"],
                ),
            ).fetchall()
            items = []
            for offer in offers:
                if bool(offer["destination_archived"]):
                    continue
                if query and query.casefold() not in str(offer["destination"] or "").casefold():
                    continue
                is_active = bool(offer["destination_active"])
                if active_only and not is_active:
                    continue
                item = {
                    key: offer[key]
                    for key in (
                        "destination", "destination_route", "price", "old_price", "currency",
                        "departure_date", "departure_date_raw", "return_date", "return_date_raw",
                        "trip_duration_days", "source_url",
                    )
                }
                item["origin"] = row["origin"]
                item["offer_id"] = offer["id"]
                item["destination_id"] = offer["destination_id"]
                item["active"] = is_active
                item["previous_price"] = offer["previous_price"]
                item["previous_currency"] = offer["previous_currency"]
                item["warnings"] = json.loads(offer["warnings_json"])
                for db_key, public_key in (
                    ("flight_duration_json", "flight_duration"),
                    ("airline_json", "airline"),
                    ("airports_json", "airports"),
                    ("stops_json", "stops"),
                    ("transfers_json", "transfers"),
                    ("flight_times_json", "flight_times"),
                    ("airline_logos_json", "airline_logos"),
                    ("transfer_durations_json", "transfer_durations"),
                    ("airline_by_leg_json", "airline_by_leg"),
                    ("arrival_day_offsets_json", "arrival_day_offsets"),
                    ("repeat_registration_by_leg_json", "repeat_registration_by_leg"),
                    ("route_identity_json", "route_identity"),
                ):
                    item[public_key] = json.loads(offer[db_key]) if offer[db_key] is not None else None
                item["airports_by_leg"] = _airports_by_leg(
                    item["airports"], item["stops"], row["trip_type"], item.get("route_identity")
                )
                identity = item.get('route_identity') or {}
                item['route_group_key'] = json.dumps([
                    row['user_id'], row['chat_id'], row['origin'], row['trip_type'],
                    row['duration_min_days'], row['duration_max_days'],
                    city_code(identity.get('actual_destination')) or str(item['destination']).casefold(),
                ], ensure_ascii=False, separators=(',', ':'))
                items.append(item)
            output.append({
                "scan_id": row["id"],
                "origin": row["origin"],
                "trip_type": row["trip_type"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "last_checked_at": connection.execute(
                    "SELECT MAX(finished_at) FROM scan_history "
                    "WHERE user_id = ? AND chat_id = ? AND origin = ?",
                    (row["user_id"], row["chat_id"], row["origin"]),
                ).fetchone()[0],
                "complete": bool(row["complete"]),
                "partial": bool(row["partial"]),
                "cards_found": row["cards_found"],
                "unique_destinations": row["unique_destinations"],
                "warnings": json.loads(row["warnings_json"]),
                "scan_kind": row["scan_kind"],
                "offers": items,
            })
    return total, output


def _count_recorded_checks(database_path: str | Path, user_ids: tuple[int, ...]) -> int:
    if not user_ids:
        return 0
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    placeholders = ",".join("?" for _ in user_ids)
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return int(connection.execute(
            f"SELECT COUNT(*) FROM scan_history WHERE user_id IN ({placeholders})",
            user_ids,
        ).fetchone()[0])


def _load_trip_compare_offer(
    database_path: str | Path,
    trusted_ids: tuple[int, ...],
    offer_id: int,
) -> dict[str, Any] | None:
    """Load a visible user's saved flight offer; never trust route data from JS."""
    if not trusted_ids:
        return None
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    placeholders = ",".join("?" for _ in trusted_ids)
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"""SELECT o.id, o.destination, o.price, o.currency, o.departure_date,
                    o.return_date, o.route_identity_json, h.origin, h.trip_type,
                    h.user_id, h.chat_id, l.city_name AS origin_city
                FROM offers o
                JOIN scan_history h ON h.id = o.scan_id
                LEFT JOIN destinations d ON d.id = o.destination_id
                LEFT JOIN origin_labels l ON l.user_id = h.user_id
                    AND l.chat_id = h.chat_id AND l.origin = h.origin
                WHERE o.id = ? AND o.route_valid = 1
                    AND h.user_id IN ({placeholders})
                    AND (d.id IS NULL OR d.archived = 0)""",
            (offer_id, *trusted_ids),
        ).fetchone()
    if row is None:
        return None
    try:
        identity = json.loads(row["route_identity_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        identity = None
    return {
        "offer_id": row["id"],
        "origin": row["origin"],
        "origin_city": row["origin_city"] or {"MOW": "Москва", "LED": "Санкт-Петербург"}.get(row["origin"], row["origin"]),
        "trip_type": row["trip_type"],
        "destination": row["destination"],
        "price": row["price"],
        "currency": row["currency"],
        "departure_date": row["departure_date"],
        "return_date": row["return_date"],
        "route_identity": identity,
    }


def create_app(
    database_path: str | Path,
    allowed_user_ids: frozenset[int],
    on_deep_scan: Callable[[str], Awaitable[bool]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Playwright must be closed on the executor thread that created it;
        # otherwise Chromium can leave a stale profile lock after a restart.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(TRIP_EXECUTOR, shutdown_tripcom_browser)

    app = FastAPI(title="Aviasales ticket monitor", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    # A full search GET can reach ~7 MB of JSON (150 cards with source
    # records and rates); gzip cuts it to ~0.5 MB measured on a live
    # snapshot (93% reduction). Only applied when the client sends
    # Accept-Encoding: gzip, which fetch() always does.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    init_hotel_store(database_path)
    app.include_router(build_router(database_path))
    trusted_ids = tuple(sorted(allowed_user_ids))

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(ASSETS.joinpath("index.html")), media_type="text/html; charset=utf-8")

    @app.get("/assets/{asset_name}", include_in_schema=False)
    def asset(asset_name: str) -> FileResponse:
        if asset_name not in {"style.css", "app.js"}:
            raise HTTPException(status_code=404)
        media_type = "text/css" if asset_name.endswith(".css") else "text/javascript"
        return FileResponse(str(ASSETS.joinpath(asset_name)), media_type=media_type)

    @app.get("/api/scans")
    def scans(
        origin: str = Query(pattern=r"^(ALL|[A-Z]{3})$"),
        kind: str = Query(pattern="^(all|regular|deep)$"),
        q: str = Query(default="", max_length=80),
        active_only: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
        view: str = Query(default="current", pattern="^(current|history)$"),
    ) -> dict[str, Any]:
        try:
            query = " ".join(q.split())
            total, items = _read_scans(
                database_path, trusted_ids, origin, kind, query, active_only, limit, offset,
                current_only=view == "current",
            )
            return {
                "origin": origin,
                "kind": kind,
                "query": query,
                "active_only": active_only,
                "view": view,
                "total": total,
                "checks_recorded": _count_recorded_checks(database_path, trusted_ids),
                "scans": items,
            }
        except Exception:
            log.exception("Local results page could not read the scan database")
            raise HTTPException(status_code=503, detail="Scan database is temporarily unavailable") from None

    @app.get("/api/destinations")
    def destinations(
        origin: str = Query(pattern=r"^(ALL|[A-Z]{3})$"),
        archived: bool = Query(default=False),
    ) -> dict[str, Any]:
        if not trusted_ids:
            return {"origin": origin, "destinations": []}
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        placeholders = ",".join("?" for _ in trusted_ids)
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            origin_filter = "" if origin == "ALL" else " AND d.origin = ?"
            origin_params: tuple[Any, ...] = () if origin == "ALL" else (origin,)
            availability_filter = "" if archived else """
                AND EXISTS (
                    SELECT 1
                    FROM scan_history current_scan
                    JOIN offers current_offer ON current_offer.scan_id = current_scan.id
                    WHERE current_scan.id = (
                        SELECT latest.id FROM scan_history latest
                        WHERE latest.user_id = d.user_id AND latest.chat_id = d.chat_id
                            AND latest.origin = d.origin AND latest.scan_kind = 'regular'
                            AND latest.complete = 1 AND latest.partial = 0
                        ORDER BY latest.finished_at DESC, latest.id DESC LIMIT 1
                    )
                        AND current_offer.destination_id = d.id
                )
            """
            rows = connection.execute(
                f"""
                SELECT d.id AS destination_id, d.destination, d.active, d.archived,
                    d.last_known_price, d.last_known_currency, d.last_seen, d.origin
                FROM destinations d
                WHERE d.user_id IN ({placeholders}){origin_filter}
                    AND d.archived = ?
                    {availability_filter}
                ORDER BY d.active DESC, d.destination COLLATE NOCASE
                """,
                (*trusted_ids, *origin_params, int(archived)),
            ).fetchall()
        return {"origin": origin, "destinations": [dict(row) for row in rows]}

    @app.post("/api/destinations/archive")
    async def archive_destination(
        request: Request,
        destination_id: int = Query(ge=1),
        origin: str = Query(pattern=ORIGIN_PATTERN),
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _require_local_request(request)
        archived = payload.get("archived")
        if type(archived) is not bool:
            raise HTTPException(status_code=422, detail="archived must be a boolean")
        if not trusted_ids:
            raise HTTPException(status_code=404, detail="Destination not found")
        uri = Path(database_path).resolve().as_uri() + "?mode=rw"
        placeholders = ",".join("?" for _ in trusted_ids)
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                cursor = connection.execute(
                    f"UPDATE destinations SET archived = ? WHERE id = ? AND origin = ? "
                    f"AND user_id IN ({placeholders})",
                    (int(archived), destination_id, origin, *trusted_ids),
                )
                if cursor.rowcount == 1 and archived:
                    connection.execute(
                        f"""UPDATE telegram_notification_outbox SET status = 'failed',
                            lease_until = NULL,
                            last_error = 'destination archived before notification delivery'
                            WHERE status IN ('pending', 'retryable') AND finding_id IN (
                                SELECT f.id FROM price_findings f
                                JOIN destinations d ON d.user_id = f.user_id
                                    AND d.chat_id = f.chat_id AND d.origin = f.origin
                                    AND d.destination_key = f.destination_key
                                WHERE d.id = ? AND f.user_id IN ({placeholders})
                            )""",
                        (destination_id, *trusted_ids),
                    )
                connection.commit()
        except (sqlite3.Error, OSError) as exc:
            log.exception("Local results page could not update destination archive state")
            raise HTTPException(status_code=503, detail="Destination archive is temporarily unavailable") from exc
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Destination not found")
        return {"destination_id": destination_id, "archived": archived}

    @app.post("/api/destinations/active")
    async def set_destination_active(
        request: Request,
        destination_id: int = Query(ge=1),
        origin: str = Query(pattern=ORIGIN_PATTERN),
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _require_local_request(request)
        active = payload.get("active")
        if type(active) is not bool:
            raise HTTPException(status_code=422, detail="active must be a boolean")
        if not trusted_ids:
            raise HTTPException(status_code=404, detail="Destination not found")
        uri = Path(database_path).resolve().as_uri() + "?mode=rw"
        placeholders = ",".join("?" for _ in trusted_ids)
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                cursor = connection.execute(
                    f"UPDATE destinations SET active = ? WHERE id = ? AND origin = ? "
                    f"AND user_id IN ({placeholders})",
                    (int(active), destination_id, origin, *trusted_ids),
                )
                connection.commit()
        except (sqlite3.Error, OSError) as exc:
            log.exception("Local results page could not update destination activity")
            raise HTTPException(status_code=503, detail="Destination activity is temporarily unavailable") from exc
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Destination not found")
        return {"destination_id": destination_id, "active": active}

    @app.get("/api/notifications")
    def notifications(origin: str = Query(pattern=r"^(ALL|[A-Z]{3})$")) -> dict[str, Any]:
        if not trusted_ids:
            return {"origin": origin, "items": []}
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        placeholders = ",".join("?" for _ in trusted_ids)
        # Notifications are a short-lived activity feed; scan history itself is retained.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            origin_filter = "" if origin == "ALL" else " AND origin = ?"
            origin_params: tuple[Any, ...] = () if origin == "ALL" else (origin,)
            rows = connection.execute(
                f"SELECT id, user_id, chat_id, finished_at, scan_kind, complete, partial, unique_destinations, outdated_offers, warnings_json, "
                f"disappeared_destinations_json, origin "
                f"FROM scan_history WHERE user_id IN ({placeholders}){origin_filter} AND finished_at >= ? "
                "ORDER BY finished_at DESC, id DESC LIMIT 20",
                (*trusted_ids, *origin_params, cutoff),
            ).fetchall()
            items = []
            for row in rows:
                cities = connection.execute(
                    "SELECT DISTINCT d.destination FROM offers o "
                    "JOIN destinations d ON d.id = o.destination_id "
                    "WHERE o.scan_id = ? AND d.first_seen = ? "
                    "ORDER BY d.destination COLLATE NOCASE",
                    (row["id"], row["finished_at"]),
                ).fetchall()
                returned_destinations = []
                if row["scan_kind"] == "regular" and row["complete"] and not row["partial"]:
                    previous_scan = connection.execute(
                        """SELECT id FROM scan_history WHERE user_id = ? AND chat_id = ? AND origin = ?
                            AND scan_kind = 'regular' AND complete = 1 AND partial = 0
                            AND (finished_at < ? OR (finished_at = ? AND id < ?))
                            ORDER BY finished_at DESC, id DESC LIMIT 1""",
                        (row["user_id"], row["chat_id"], row["origin"], row["finished_at"],
                         row["finished_at"], row["id"]),
                    ).fetchone()
                    if previous_scan is not None:
                        returned_destinations = [item["destination"] for item in connection.execute(
                            """SELECT DISTINCT d.destination FROM offers o
                                JOIN destinations d ON d.id = o.destination_id
                                WHERE o.scan_id = ? AND d.archived = 0 AND d.first_seen < ?
                                    AND NOT EXISTS (
                                        SELECT 1 FROM offers previous_offer
                                        WHERE previous_offer.scan_id = ?
                                            AND previous_offer.destination_id = o.destination_id
                                    )
                                ORDER BY d.destination COLLATE NOCASE""",
                            (row["id"], row["finished_at"], previous_scan["id"]),
                        ).fetchall()]
                progress = None
                deep_issues = []
                if row["scan_kind"] == "deep":
                    warnings = json.loads(row["warnings_json"] or "[]")
                    for warning in warnings:
                        warning = str(warning)
                        match = DEEP_PROGRESS_RE.search(warning)
                        limited = DEEP_LIMIT_RE.search(warning)
                        if match:
                            progress = {"scanned": int(match.group(1)), "requested": int(match.group(2))}
                        elif limited:
                            scanned, skipped = int(limited.group(1)), int(limited.group(2))
                            progress = {"scanned": scanned, "requested": scanned + skipped}
                        failure = DEEP_FAILURE_RE.match(warning)
                        if failure and "deep scan completed partially:" not in warning.casefold():
                            reason = "карточка не найдена" if "destination card not found" in warning.casefold() else "ошибка проверки"
                            deep_issues.append({"destination": failure.group(1).strip(), "reason": reason})
                items.append({
                    "id": row["id"],
                    "origin": row["origin"],
                    "finished_at": row["finished_at"], "scan_kind": row["scan_kind"],
                    "complete": bool(row["complete"]), "partial": bool(row["partial"]),
                    "unique_destinations": row["unique_destinations"],
                    "outdated_offers": row["outdated_offers"],
                    "new_destinations": [city["destination"] for city in cities],
                    "returned_destinations": returned_destinations,
                    "disappeared_destinations": json.loads(row["disappeared_destinations_json"] or "[]"),
                    "deep_progress": progress,
                    "deep_issues": deep_issues,
                })
        return {"origin": origin, "items": items}

    @app.get("/api/price-history")
    def price_history(
        origin: str = Query(pattern=ORIGIN_PATTERN),
        offer_id: int = Query(ge=1),
    ) -> dict[str, Any]:
        if not trusted_ids:
            raise HTTPException(status_code=404, detail="Offer not found")
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        placeholders = ",".join("?" for _ in trusted_ids)
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            current = connection.execute(
                f"""SELECT o.destination, o.destination_id, o.currency,
                    h.user_id, h.chat_id, h.origin, h.trip_type
                    FROM offers o JOIN scan_history h ON h.id = o.scan_id
                    WHERE o.id = ? AND o.route_valid = 1 AND h.user_id IN ({placeholders}) AND h.origin = ?""",
                (offer_id, *trusted_ids, origin),
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="Offer not found")
            rows = connection.execute(
                """SELECT h.finished_at, MIN(o.price) AS price
                    FROM offers o JOIN scan_history h ON h.id = o.scan_id
                    WHERE h.user_id = ? AND h.chat_id = ? AND h.origin = ?
                        AND h.trip_type = ?
                        AND (o.destination_id = ? OR (? IS NULL AND o.destination = ?))
                        AND o.currency IS ? AND o.price IS NOT NULL
                        AND o.route_valid = 1
                        AND (h.scan_kind = 'deep' OR (h.complete = 1 AND h.partial = 0))
                    GROUP BY h.id
                    ORDER BY h.finished_at, h.id""",
                (current["user_id"], current["chat_id"], origin, current["trip_type"],
                 current["destination_id"], current["destination_id"], current["destination"],
                 current["currency"]),
            ).fetchall()
        return {
            "origin": origin,
            "destination": current["destination"],
            "currency": current["currency"],
            "points": [{"checked_at": row["finished_at"], "price": row["price"]} for row in rows],
        }

    @app.post("/api/flights/compare-trip")
    async def compare_trip_flight(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require_local_request(request)
        if set(payload) != {"offer_id"} or type(payload.get("offer_id")) is not int or payload["offer_id"] < 1:
            raise HTTPException(status_code=422, detail="Provide only a positive offer_id")
        try:
            offer = _load_trip_compare_offer(database_path, trusted_ids, payload["offer_id"])
        except (sqlite3.Error, OSError):
            log.exception("Local results page could not load the requested flight offer")
            raise HTTPException(status_code=503, detail="Flight data is temporarily unavailable") from None
        if offer is None:
            raise HTTPException(status_code=404, detail="Offer not found")
        try:
            context = build_offer_context(offer)
            result = await compare_trip_offer(offer)
        except TripFlightCompareError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None
        except Exception:
            log.exception("Trip.com flight comparison failed")
            raise HTTPException(status_code=503, detail="Не удалось получить цену Trip.com. Попробуйте позже.") from None
        return {
            "offer_id": offer["offer_id"],
            "card_price": offer["price"],
            "card_currency": offer["currency"],
            "origin": context["origin"],
            "destination": offer["destination"],
            "trip_type": context["trip_type"],
            "departure_date": context["departure_date"],
            "return_date": context["return_date"],
            "passengers": result["passengers"],
            "trip": result,
        }

    @app.get("/api/cities")
    def cities(q: str = Query(min_length=2, max_length=80)) -> dict[str, Any]:
        term = " ".join(q.split())
        if len(term) < 2:
            return {"cities": []}
        return {"cities": list(_search_cities(term))}

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        if not trusted_ids:
            return {"origins": []}
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        placeholders = ",".join("?" for _ in trusted_ids)
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""SELECT s.origin, s.trip_type, s.duration_min_days, s.duration_max_days,
                    s.interval_minutes, s.enabled, l.city_name
                    FROM monitor_settings s LEFT JOIN origin_labels l
                        ON l.user_id = s.user_id AND l.chat_id = s.chat_id AND l.origin = s.origin
                    WHERE s.user_id IN ({placeholders})
                    ORDER BY s.updated_at DESC, s.origin""",
                trusted_ids,
            ).fetchall()
        origins = []
        for row in rows:
            default_name = {"MOW": "Москва", "LED": "Санкт-Петербург"}.get(row["origin"], row["origin"])
            origins.append({
                "origin": row["origin"], "city_name": row["city_name"] or default_name,
                "trip_type": row["trip_type"], "duration_min_days": row["duration_min_days"],
                "duration_max_days": row["duration_max_days"],
                "interval_minutes": row["interval_minutes"], "enabled": bool(row["enabled"]),
            })
        return {"origins": origins}

    @app.post("/api/settings")
    def save_web_settings(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _require_local_request(request)
        if len(trusted_ids) != 1:
            raise HTTPException(status_code=409, detail="Web settings require exactly one allowed Telegram user")
        user_id = trusted_ids[0]
        origin = str(payload.get("origin") or "").upper()
        city_name = " ".join(str(payload.get("city_name") or "").split())
        trip_type = str(payload.get("trip_type") or "").upper()
        try:
            interval = _integer_field(payload, "interval_minutes", 0)
            if not 30 <= interval <= 1440:
                raise ValueError("interval_minutes must be between 30 and 1440")
            low = _integer_field(payload, "duration_min_days", 1) if trip_type == "RT" else None
            high = _integer_field(payload, "duration_max_days", 30) if trip_type == "RT" else None
            settings_value = MonitorSettings(
                user_id=user_id, chat_id=user_id, origin=origin, trip_type=trip_type,
                interval_minutes=interval, duration_min_days=low, duration_max_days=high,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not city_name or len(city_name) > 100:
            raise HTTPException(status_code=422, detail="Select a city from the suggestions")
        matches = _search_cities(city_name)
        if not any(city["code"] == settings_value.origin for city in matches):
            raise HTTPException(status_code=422, detail="Select a valid city from the suggestions")
        try:
            with SQLiteStore(database_path) as store:
                store.save_settings(settings_value)
                store.save_origin_label(user_id, user_id, origin, city_name)
        except (sqlite3.Error, OSError) as exc:
            log.exception("Local website could not save monitor settings")
            raise HTTPException(status_code=503, detail="Could not save monitoring settings") from exc
        return {"origin": origin, "city_name": city_name, "saved": True}

    @app.post("/api/deep-scan")
    async def start_deep_scan(
        request: Request,
        origin: str = Query(pattern=ORIGIN_PATTERN),
    ) -> dict[str, Any]:
        if on_deep_scan is None:
            raise HTTPException(status_code=503, detail="Deep scan is unavailable")
        _require_local_request(request)
        accepted = await on_deep_scan(origin)
        if not accepted:
            raise HTTPException(status_code=409, detail="Deep scan is already running or not configured")
        return {"origin": origin, "started": True}

    return app


async def run_server(
    database_path: str | Path,
    allowed_user_ids: frozenset[int],
    on_deep_scan: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Run the loopback-only UI; callers isolate failures from the Telegram bot."""
    import uvicorn

    config = uvicorn.Config(
        create_app(database_path, allowed_user_ids, on_deep_scan),
        host="127.0.0.1",
        port=8765,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    await server.serve()
