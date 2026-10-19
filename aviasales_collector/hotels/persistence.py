"""Hotel SQLite write-side operations and diagnostics."""

from __future__ import annotations

import collections
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .models import SourceResult, utc_now
from .normalization import (_normalize_rating_fields, _offer_meal_facts,
                            normalize_hotel)
from .runtime import _profiled_sqlite_connect, _safe_json_loads, log
from .schema import init_hotel_store
from .sources.base import HotelSource


def _sanitize_enrichment_error_message(error: Any) -> str:
    message = " ".join(str(error or "").split())[:500]
    message = re.sub(
        r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
        r"\1=[redacted]", message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", message)
    message = re.sub(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*[^)\]}]+", r"\1=[redacted]", message)
    message = re.sub(
        r"(?i)([\w-]*(?:token|session|password|secret|api[_-]?key|cookie|authorization)[\w-]*\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\]}]+)",
        r"\1[redacted]", message,
    )
    def redact_url(match: re.Match[str]) -> str:
        parsed = urlsplit(match.group(0).rstrip("'\"),.;"))
        # Exception messages can contain signed URLs or basic-auth credentials.
        # Keep the useful endpoint path while dropping userinfo, query, and fragment.
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            host = f"{host}:{port}"
        safe = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        suffix = match.group(0)[len(match.group(0).rstrip("'\"),.;")):]
        return safe + suffix
    message = re.sub(r"https?://[^\s]+", redact_url, message)
    return message[:300]

def persist_tripcom_room_rates(path: str | Path, search_id: str,
                               inventory: dict[str, Any], *, status: str | None = None,
                               http_status: int | None = None, latency_ms: int | None = None,
                               property_details: dict[str, Any] | None = None,
                               operation: str = "tripcom_detail_rate_inventory",
                               error_type: str | None = None, error_message: str | None = None,
                               attempt: int = 1, retry_count: int = 0,
                               observed_at: str | None = None) -> dict[str, int]:
    """Persist sanitized rate variants only when the hotel and booking context match a saved search."""
    init_hotel_store(path)
    source_hotel_id = str(inventory.get("source_hotel_id") or "")
    rates = inventory.get("rates") if isinstance(inventory.get("rates"), list) else []
    meal_counts: collections.Counter[str] = collections.Counter()
    inserted = 0
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        session = connection.execute("SELECT query_json FROM hotel_search_session WHERE id=?", (search_id,)).fetchone()
        hotel = connection.execute(
            "SELECT 1 FROM hotel_observation WHERE search_id=? AND source='trip_com' AND source_hotel_id=?",
            (search_id, source_hotel_id),
        ).fetchone()
        if session is None or hotel is None:
            raise ValueError("rate inventory must match a persisted Trip.com hotel in the selected search")
        query = _safe_json_loads(session["query_json"], {})
        if not isinstance(query, dict):
            query = {}
        expected = (query.get("check_in"), query.get("check_out"), query.get("adults"),
                    query.get("children_count", 0))
        actual = (inventory.get("check_in"), inventory.get("check_out"), inventory.get("adults"),
                  inventory.get("children_count", 0))
        if actual != expected:
            raise ValueError("rate inventory booking context does not match the saved search")
        fetched_at = observed_at or max((str(rate.get("observed_at") or utc_now()) for rate in rates),
                                        default=utc_now())
        fetch_status = status or str(inventory.get("status") or ("ok" if rates else "no_rates"))
        # A queued/running accounting row may already exist for this task
        # (written before the browser pool started). Replace it with the
        # terminal outcome instead of accumulating a second row.
        connection.execute(
            """DELETE FROM hotel_room_rate_fetch
               WHERE search_id=? AND source='trip_com' AND source_hotel_id=?
                 AND check_in=? AND check_out=? AND adults=? AND children_count=?
                 AND attempt=? AND retry_count=? AND status IN ('queued','running')""",
            (search_id, source_hotel_id, inventory["check_in"], inventory["check_out"],
             inventory["adults"], inventory.get("children_count", 0),
             max(0, int(attempt)), max(0, int(retry_count))),
        )
        detail_rating = _normalize_rating_fields(
            property_details.get("rating_original", property_details.get("rating"))
            if isinstance(property_details, dict) else None,
            property_details.get("rating_scale") if isinstance(property_details, dict) else None,
            (property_details.get("rating_source") or "trip_com")
            if isinstance(property_details, dict) else None,
        )
        if detail_rating["rating_original"] is not None:
            connection.execute(
                "UPDATE hotel_observation SET rating=?,rating_original=?,rating_scale=?,"
                "rating_normalized=?,rating_source=? WHERE search_id=? AND source='trip_com' "
                "AND source_hotel_id=?",
                (detail_rating["rating_original"], detail_rating["rating_original"],
                 detail_rating["rating_scale"], detail_rating["rating_normalized"],
                 detail_rating["rating_source"], search_id, source_hotel_id),
            )
        connection.execute(
            """INSERT INTO hotel_room_rate_fetch
            (search_id,source,source_hotel_id,check_in,check_out,adults,children_count,status,
             http_status,latency_ms,rates_seen,observed_at,property_details_json,operation,
             error_type,error_message,attempt,retry_count)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (search_id, "trip_com", source_hotel_id, inventory["check_in"], inventory["check_out"],
             inventory["adults"], inventory.get("children_count", 0), fetch_status, http_status,
             latency_ms, len(rates), fetched_at,
             json.dumps(property_details, ensure_ascii=False, sort_keys=True) if property_details else None,
             operation, error_type, _sanitize_enrichment_error_message(error_message),
             max(0, int(attempt)), max(0, int(retry_count))),
        )
        # B13 (live Phase C evidence, 2026-10-19): the publish-before-reviews
        # flow re-persists the SAME terminal record (rate first, the
        # review-enriched item second). Replaying an identical terminal tuple
        # (same observed_at) must collapse to the newest row, not append a
        # duplicate - a live search accumulated 100 duplicated ok rows from
        # this. A genuinely new observation (different observed_at) still
        # appends as a separate history row.
        connection.execute(
            """DELETE FROM hotel_room_rate_fetch
               WHERE search_id=? AND source='trip_com' AND source_hotel_id=?
                 AND check_in=? AND check_out=? AND adults=? AND children_count=?
                 AND attempt=? AND retry_count=? AND observed_at=? AND status=?
                 AND id < (SELECT MAX(id) FROM hotel_room_rate_fetch
                           WHERE search_id=? AND source='trip_com' AND source_hotel_id=?
                             AND check_in=? AND check_out=? AND adults=? AND children_count=?
                             AND attempt=? AND retry_count=? AND observed_at=? AND status=?)""",
            (search_id, source_hotel_id, inventory["check_in"], inventory["check_out"],
             inventory["adults"], inventory.get("children_count", 0),
             max(0, int(attempt)), max(0, int(retry_count)), fetched_at, fetch_status,
             search_id, source_hotel_id, inventory["check_in"], inventory["check_out"],
             inventory["adults"], inventory.get("children_count", 0),
             max(0, int(attempt)), max(0, int(retry_count)), fetched_at, fetch_status),
        )
        for rate in rates:
            if not isinstance(rate, dict) or not rate.get("provider_rate_id"):
                continue
            meal_counts[str(rate.get("meal_plan") or "unknown")] += 1
            connection.execute(
                """INSERT OR IGNORE INTO hotel_room_rate_observation
                (search_id,source,source_hotel_id,provider_rate_id,check_in,check_out,adults,
                 children_count,room_group_index,raw_json,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, "trip_com", source_hotel_id, rate["provider_rate_id"],
                 inventory["check_in"], inventory["check_out"], inventory["adults"],
                 inventory.get("children_count", 0), rate.get("room_group_index"),
                 json.dumps(rate, ensure_ascii=False, sort_keys=True), rate.get("observed_at") or fetched_at),
            )
            inserted += connection.execute("SELECT changes()").fetchone()[0]
        connection.commit()
    return {"rates_seen": len(rates), "rates_persisted": inserted,
            **{f"meal_{key}": value for key, value in meal_counts.items()}}

def reprocess_saved_offer_meals(path: str | Path, search_id: str | None = None) -> dict[str, int]:
    """Re-run only meal normalization from persisted offer raw_payload, leaving raw pages untouched."""
    init_hotel_store(path)
    changed = 0
    counts: collections.Counter[str] = collections.Counter()
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        query = "SELECT id,source,raw_json FROM hotel_offer_observation"
        params: tuple[Any, ...] = ()
        if search_id is not None:
            query += " WHERE search_id=?"
            params = (search_id,)
        rows = connection.execute(query, params).fetchall()
        for row in rows:
            normalized = _safe_json_loads(row["raw_json"], None)
            if not isinstance(normalized, dict):
                continue
            raw_offer = normalized.get("raw_payload")
            if not isinstance(raw_offer, dict):
                continue
            facts = _offer_meal_facts(raw_offer, row["source"])
            normalized.update(facts)
            connection.execute("UPDATE hotel_offer_observation SET raw_json=? WHERE id=?",
                               (json.dumps(normalized, ensure_ascii=False), row["id"]))
            counts[facts["meal_plan"]] += 1
            changed += 1
        connection.commit()
    return {"reprocessed": changed, **dict(counts)}

def _persist_page(path: str | Path, search_id: str, hotels: list[dict[str, Any]],
                  raw_page: dict[str, Any], page_token: str | None, source: str = "sravniotel",
                  query: dict[str, Any] | None = None, observed_at: str | None = None) -> None:
    now = observed_at or utc_now()
    raw_page = dict(raw_page)
    raw_page["requestedPageToken"] = page_token
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        # Serialize page-index allocation and cursor updates.  Without an
        # immediate transaction two concurrent Load more calls could both see
        # the same MAX(page_index) and race on the unique page key.
        connection.execute("BEGIN IMMEDIATE")
        seen_tokens = {
            payload.get("requestedPageToken")
            for row in connection.execute(
                "SELECT raw_json FROM hotel_raw_page WHERE search_id=? AND source=?",
                (search_id, source),
            )
            for payload in [_safe_json_loads(row[0], {})]
            if isinstance(payload, dict)
        }
        # Two clients can read the same cursor before either request enters
        # this transaction. The first writer owns that page; the second must
        # be a no-op instead of persisting a duplicate page and offers.
        if page_token is not None and page_token in seen_tokens:
            connection.rollback()
            return
        next_token = raw_page.get("nextPageToken")
        # Sravni ends pagination with either JSON null or an empty string.
        # Store both forms as SQL NULL so lifecycle checks and API polling
        # agree that the cursor is exhausted.
        if isinstance(next_token, str) and not next_token.strip():
            next_token = None
        repeated_next_token = next_token is not None and (
            next_token == page_token or next_token in seen_tokens
        )
        if repeated_next_token:
            # Mark the broken cursor as an application stop. Clear it below so
            # clients cannot retry the same page forever; the audit stays incomplete.
            raw_page["_paginationStopReason"] = "repeated_next_page_token"
            raw_page["_providerNextPageToken"] = next_token
            raw_page["nextPageToken"] = None
        page_index = connection.execute(
            "SELECT COALESCE(MAX(page_index),0)+1 FROM hotel_raw_page WHERE search_id=? AND source=?",
            (search_id, source),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO hotel_raw_page(search_id,source,page_index,raw_json,observed_at) VALUES (?,?,?,?,?)",
            (search_id, source, page_index, json.dumps(raw_page, ensure_ascii=False), now),
        )
        new_hotel_count = 0
        new_offer_count = 0
        for raw_hotel in hotels:
            hotel = normalize_hotel(raw_hotel, now, source=source, query=query)
            if hotel is None:
                continue
            exists = connection.execute(
                "SELECT 1 FROM hotel_observation WHERE search_id=? AND source=? AND source_hotel_id=?",
                (search_id, source, hotel["source_hotel_id"]),
            ).fetchone()
            if not exists:
                new_hotel_count += 1
            canonical_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                          f"hotel-source:{source}:{hotel['source_hotel_id']}"))
            connection.execute(
                "INSERT OR IGNORE INTO canonical_hotel(id,display_name,created_at) VALUES (?,?,?)",
                (canonical_id, hotel["name"], now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO hotel_source_identity(source,source_hotel_id,canonical_hotel_id,created_at) "
                "VALUES (?,?,?,?)", (source, hotel["source_hotel_id"], canonical_id, now),
            )
            connection.execute(
                """INSERT INTO hotel_observation
                (search_id,source,source_hotel_id,name,address,stars,rating,rating_original,
                 rating_scale,rating_normalized,rating_source,reviews_count,images_json,
                 nearby_places_json,raw_json,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(search_id,source,source_hotel_id) DO UPDATE SET
                stars=CASE WHEN excluded.stars IS NOT NULL THEN excluded.stars
                           WHEN hotel_observation.stars>0 AND hotel_observation.stars<=5
                           THEN hotel_observation.stars ELSE NULL END,
                rating=CASE WHEN excluded.rating IS NOT NULL THEN excluded.rating
                            WHEN hotel_observation.rating>0 AND hotel_observation.rating<=10
                            THEN hotel_observation.rating ELSE NULL END,
                rating_original=COALESCE(excluded.rating_original,hotel_observation.rating_original),
                rating_scale=COALESCE(excluded.rating_scale,hotel_observation.rating_scale),
                rating_normalized=COALESCE(excluded.rating_normalized,hotel_observation.rating_normalized),
                rating_source=COALESCE(excluded.rating_source,hotel_observation.rating_source),
                raw_json=excluded.raw_json, observed_at=excluded.observed_at""",
                (search_id, source, hotel["source_hotel_id"], hotel["name"], hotel["address"],
                 hotel["stars"], hotel["rating"], hotel.get("rating_original"), hotel.get("rating_scale"),
                 hotel.get("rating_normalized"), hotel.get("rating_source"), hotel["reviews_count"],
                 json.dumps(hotel["images"]), json.dumps(hotel["nearby_places"], ensure_ascii=False),
                 json.dumps(raw_hotel, ensure_ascii=False), now),
            )
            for offer in hotel["offers"]:
                dedupe_payload = {key: offer.get(key) for key in (
                    "provider", "amount", "currency", "deeplink", "labels", "room_type",
                    "bed_type", "meal_plan", "cancellation", "tax_status",
                    "check_in", "check_out", "adults", "children_count", "rooms",
                    "occupancy", "taxes", "fees", "payment_method",
                )}
                dedupe_key = hashlib.sha256(json.dumps(
                    dedupe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                connection.execute(
                    """INSERT OR IGNORE INTO hotel_offer_observation
                    (search_id,source,source_hotel_id,dedupe_key,provider,amount,source_price_value,currency,
                     deeplink,labels_json,raw_json,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (search_id, source, hotel["source_hotel_id"], dedupe_key,
                     offer["provider"], offer["amount"],
                     offer["source_price_value"], offer["currency"], offer["deeplink"],
                     json.dumps(offer["labels"]), json.dumps(offer), now),
                )
                new_offer_count += connection.execute("SELECT changes()").fetchone()[0]
        if source == "sravniotel":
            connection.execute(
                "UPDATE hotel_search_session SET total_hotels=?, next_page_token=? WHERE id=?",
                (raw_page.get("totalHotels"), None if repeated_next_token else next_token, search_id),
            )
        connection.execute(
            "UPDATE hotel_source_run SET pages=pages+1, hotel_count=hotel_count+?, offer_count=offer_count+? "
            "WHERE id=(SELECT MAX(id) FROM hotel_source_run WHERE search_id=? AND source=?)",
            (new_hotel_count, new_offer_count, search_id, source),
        )
        connection.commit()

def _stop_sravni_pagination(path: str | Path, search_id: str, reason: str, *,
                            error_class: str | None = None,
                            http_status: int | None = None) -> None:
    """Record an explicit application stop without reporting source exhaustion."""
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        page = connection.execute(
            "SELECT id,raw_json FROM hotel_raw_page WHERE search_id=? AND source='sravniotel' "
            "ORDER BY page_index DESC LIMIT 1", (search_id,),
        ).fetchone()
        if page is not None:
            raw = _safe_json_loads(page["raw_json"], {})
            if isinstance(raw, dict):
                raw["_paginationStopReason"] = reason
                connection.execute("UPDATE hotel_raw_page SET raw_json=? WHERE id=?",
                                   (json.dumps(raw, ensure_ascii=False), page["id"]))
        connection.execute("UPDATE hotel_search_session SET next_page_token=NULL WHERE id=?", (search_id,))
        connection.execute(
            "UPDATE hotel_source_run SET status='partial',collection_complete=0,collection_reason=?,"
            "error_class=COALESCE(?,error_class),http_status=COALESCE(?,http_status),observed_at=? "
            "WHERE search_id=? AND source='sravniotel'",
            (reason, error_class, http_status, utc_now(), search_id),
        )
        connection.commit()

def _persist_search_source_result(database_path: str | Path, search_id: str,
                                  source: HotelSource, query: dict[str, Any],
                                  result: SourceResult) -> SourceResult:
    """Persist one provider's completed search without waiting for other sources."""
    if result.status == "ok" and not result.hotels:
        result = SourceResult(**{**result.__dict__, "status": "no_results"})
    if result.raw_page is not None:
        source_page_limit = result.raw_page.get("_sourcePageLimit")
        if source.source == "sravniotel" and isinstance(source_page_limit, int):
            query["limit"] = source_page_limit
            with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
                connection.execute("UPDATE hotel_search_session SET query_json=? WHERE id=?",
                                   (json.dumps(query, ensure_ascii=False), search_id))
                connection.commit()
        _persist_page(database_path, search_id, result.raw_page["hotels"], result.raw_page,
                      None, source.source, query, result.observed_at)
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        connection.execute(
            "UPDATE hotel_source_run SET status=?,latency_ms=?,error_class=?,http_status=?,cache_state=?,reported_total=?,collection_complete=?,collection_reason=?,observed_at=? "
            "WHERE search_id=? AND source=?",
            (result.status, result.latency_ms, result.error_class, result.http_status, result.cache_state,
             result.reported_total,
             None if result.collection_complete is None else int(result.collection_complete),
             result.collection_reason, result.observed_at or utc_now(), search_id, source.source),
        )
        connection.commit()
    if result.status not in {"ok", "no_results"}:
        log.warning("hotel source search failed: source=%s status=%s error_class=%s http_status=%s reason=%s",
                    source.source, result.status, result.error_class, result.http_status, result.reason)
    return result

def _finish_search_session_if_ready(database_path: str | Path, search_id: str) -> bool:
    with closing(_profiled_sqlite_connect(str(database_path), timeout=10)) as connection:
        rows = connection.execute("SELECT status FROM hotel_source_run WHERE search_id=?", (search_id,)).fetchall()
        statuses = [str(row[0]) for row in rows]
        if not statuses or "running" in statuses:
            connection.execute("UPDATE hotel_search_session SET status='running',finished_at=NULL WHERE id=?",
                               (search_id,))
            connection.commit()
            return False
        failed = any(status not in {"ok", "no_results"} for status in statuses)
        partial = any(status == "partial" for status in statuses)
        # Source discovery is not terminal while Sravni still exposes a cursor,
        # but an already failed/partial source must still surface that partial
        # outcome immediately instead of being hidden behind pagination.
        sravni_cursor = connection.execute(
            "SELECT next_page_token FROM hotel_search_session WHERE id=?", (search_id,),
        ).fetchone()
        if not failed and not partial and sravni_cursor and sravni_cursor[0]:
            connection.execute("UPDATE hotel_search_session SET status='running',finished_at=NULL WHERE id=?",
                               (search_id,))
            connection.commit()
            return False
        has_results = False
        if not failed and not partial:
            counts = connection.execute(
                "SELECT COALESCE(SUM(hotel_count),0) FROM hotel_source_run WHERE search_id=?", (search_id,)
            ).fetchone()
            has_results = bool(counts and int(counts[0] or 0) > 0)
        status = "partial" if failed or partial else "success" if has_results else "no_results"
        connection.execute("UPDATE hotel_search_session SET status=?,finished_at=? WHERE id=?",
                           (status, utc_now(), search_id))
        connection.commit()
        return True
