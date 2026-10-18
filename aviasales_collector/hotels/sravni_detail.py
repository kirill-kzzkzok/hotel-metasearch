"""SravniOtel hotel-page enrichment (HTTP-only, no browser).

The SravniOtel list API carries no check-in/out policy times and no hotel
amenities; its public hotel pages do (server-rendered HTML: "Заезд с 14:00",
"Выезд до 12:00", a description paragraph and a mantine List of facility
labels). This module fetches those pages with a plain HTTP GET (fast, no
browser, no Trip.com detail navigation - the 2026-10-15 directive forbids
opening individual HOTEL pages on Trip.com; SravniOtel pages are fetched
server-side with a light urllib request) and parses only public user-facing
facts.

Parsed fields per hotel:
- check_in_text / check_out_text: the policy times;
- amenities_text: facility labels found on the page (Wi-Fi, laundry,
  air conditioning, transfer, breakfast, pets, 24h reception, parking,
  pool, gym, ...).

Results are persisted into hotel_provider_metadata under
provider='sravniotel' with source='sravniotel_hotel_api' (HTML fallback: sravniotel_hotel_page_html) so the read
model can merge the policy times and amenities into the canonical card
without touching any API response contract.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import logging

log = logging.getLogger(__name__)

SRAVNI_BASE = "https://hotels.travel.ru"
SRAVNI_PAGE_TIMEOUT_SECONDS = 12.0
SRAVNI_DETAIL_CONCURRENCY = 3
SRAVNI_DETAIL_MAX_PER_SEARCH = 500
_SRAVNI_REQUEST_LOCK_BUSY = threading.Lock()
_SRAVNI_MIN_REQUEST_INTERVAL_SECONDS = 0.25

# Facility tokens scanned in the page text. The label shown to the user is
# the token itself; the matcher is case-insensitive over the plain text.
_FACILITY_TOKENS: tuple[tuple[str, str], ...] = (
    ("Wi-Fi", "wifi"),
    ("Прачечная/химчистка", "laundry"),
    ("Кондиционер", "ac"),
    ("Трансфер", "transfer"),
    ("Завтрак в номер", "breakfast_in_room"),
    ("Завтрак", "breakfast"),
    ("животных", "pets"),
    ("круглосуточная стойка", "reception24"),
    ("парковка", "parking"),
    ("бассейн", "pool"),
    ("фитнес", "gym"),
    ("спортзал", "gym"),
    ("сейф", "safe"),
    ("лифт", "elevator"),
    ("бар", "bar"),
    ("ресторан", "restaurant"),
    ("кухня", "kitchen"),
    ("террас", "terrace"),
    ("балкон", "balcony"),
    ("SPA", "spa"),
    ("сауна", "sauna"),
    ("баня", "sauna"),
    ("отопление", "heating"),
    ("гладильн", "ironing"),
    ("фен", "hairdryer"),
    ("уборк", "cleaning_daily"),
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_sravni_hotel_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract structured facts from the SravniOtel /api/v1/hotel response.

    2026-10-16: the site itself calls GET /api/v1/hotel?geo_id=<hotel id>
    &check_in&check_out&adults (seen in the hotel page's react-query state);
    it returns structured check_in_time / check_out_time, amenity_groups
    (Wi-Fi, reception, transfer, AC, breakfast, pets...), policies and a
    description. Prefer this API over HTML scraping: same public data,
    structured, faster, and no page fetch at all.
    """
    hotel = payload.get("hotel") if isinstance(payload.get("hotel"), dict) else {}
    check_in = str(hotel.get("check_in_time") or "").strip() or None
    check_out = str(hotel.get("check_out_time") or "").strip() or None
    amenities: list[str] = []
    for group in hotel.get("amenity_groups") or []:
        if not isinstance(group, dict):
            continue
        for amenity in group.get("amenities") or []:
            if isinstance(amenity, dict) and isinstance(amenity.get("name"), str)                     and amenity["name"].strip():
                amenities.append(amenity["name"].strip())
    # de-duplicate while preserving order
    amenities = list(dict.fromkeys(amenities))
    description_paragraphs: list[str] = []
    for block in hotel.get("description") or []:
        if isinstance(block, dict):
            description_paragraphs.extend(
                str(p) for p in (block.get("paragraphs") or []) if isinstance(p, str) and p.strip())
    policy_paragraphs: list[str] = []
    for policy in hotel.get("policies") or []:
        if isinstance(policy, dict):
            policy_paragraphs.extend(
                str(p) for p in (policy.get("paragraphs") or []) if isinstance(p, str) and p.strip())
    return {
        "check_in_text": f"Заезд с {check_in[:5]}" if check_in else None,
        "check_out_text": f"Выезд до {check_out[:5]}" if check_out else None,
        "amenities_text": amenities,
        "description_text": " ".join(description_paragraphs)[:600] or None,
        "policy_text": " ".join(policy_paragraphs)[:600] or None,
        "source": "sravniotel_hotel_api",
    }


def parse_sravni_hotel_page(html: str) -> dict[str, Any]:
    """Extract public policy/amenity facts from a SravniOtel hotel page."""
    text = re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", html)))
    checkin = re.search(r"Заезд\s*с?\s*(\d{1,2}:\d{2})", text)
    checkout = re.search(r"Выезд\s*до?\s*(\d{1,2}:\d{2})", text)
    amenities: list[str] = []
    lowered = text.casefold()
    for token, _key in _FACILITY_TOKENS:
        if token.casefold() in lowered:
            amenities.append(token)
    return {
        "check_in_text": f"Заезд с {checkin.group(1)}" if checkin else None,
        "check_out_text": f"Выезд до {checkout.group(1)}" if checkout else None,
        "amenities_text": amenities,
        "source": "sravniotel_hotel_page_html",
    }


def _fetch_hotel_api(source_hotel_id: str, check_in: str, check_out: str, adults: int) -> dict[str, Any] | None:
    """GET the SravniOtel structured hotel API (no page fetch, no browser)."""
    query = urllib.parse.urlencode({
        "geo_id": source_hotel_id, "check_in": check_in, "check_out": check_out, "adults": adults,
    })
    request = urllib.request.Request(
        f"{SRAVNI_BASE}/api/v1/hotel?{query}",
        headers={"User-Agent": "HotelMetaSearch/0.1 (sravni hotel enrichment)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=SRAVNI_PAGE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.info("[SRAVNI_DETAIL] api_fetch_failed hotel=%s error=%s", source_hotel_id, str(exc)[:120])
        return None


def _fetch_page(path: str) -> str | None:
    request = urllib.request.Request(
        SRAVNI_BASE + path,
        headers={"User-Agent": "HotelMetaSearch/0.1 (sravni hotel page enrichment)"},
    )
    with _SRAVNI_REQUEST_LOCK_BUSY:
        time.sleep(_SRAVNI_MIN_REQUEST_INTERVAL_SECONDS)
    try:
        with urllib.request.urlopen(request, timeout=SRAVNI_PAGE_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.info("[SRAVNI_DETAIL] page_fetch_failed path=%s error=%s", path, str(exc)[:120])
        return None


def _persist_metadata(database_path: str | Path, search_id: str,
                      source_hotel_id: str, facts: dict[str, Any]) -> None:
    # The facts carry their own origin (sravniotel_hotel_api for the
    # structured endpoint, sravniotel_hotel_page_html for the HTML
    # fallback); fall back to the API label when absent.
    origin = str(facts.get("source") or "sravniotel_hotel_api")
    method = "api_get" if origin == "sravniotel_hotel_api" else "http_html_page_parse"
    payload = {
        "provider": "sravniotel",
        "source": origin,
        "method": method,
        "confidence": "high",
        "retrieved_at": _utc_timestamp(),
        **{k: v for k, v in facts.items() if k != "source"},
    }
    with closing(_connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO hotel_provider_metadata(search_id, source, source_hotel_id, provider, metadata_json, retrieved_at) "
            "VALUES (?,?,?,?,?,?)",
            (search_id, "sravniotel", str(source_hotel_id), "sravniotel",
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             payload["retrieved_at"]),
        )
        connection.commit()


def _connect(database_path: str | Path):
    from .runtime import _profiled_sqlite_connect
    return _profiled_sqlite_connect(str(database_path), timeout=10)


def _load_sravni_hotels(database_path: str | Path, search_id: str) -> list[dict[str, Any]]:
    with closing(_connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT source_hotel_id, raw_json FROM hotel_observation "
            "WHERE search_id=? AND source='sravniotel' ORDER BY id",
            (search_id,),
        ).fetchall()
    hotels = []
    for row in rows:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        path = raw.get("canonical_url")
        if isinstance(path, str) and path.startswith("/"):
            hotels.append({"source_hotel_id": str(row["source_hotel_id"]), "path": path})
    return hotels


def _load_search_query(database_path: str | Path, search_id: str) -> dict[str, Any]:
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            "SELECT query_json FROM hotel_search_session WHERE id=?", (search_id,),
        ).fetchone()
    if not row or not row[0]:
        return {}
    payload = json.loads(row[0]) if isinstance(row[0], str) else {}
    return payload if isinstance(payload, dict) else {}


def _has_metadata(database_path: str | Path, search_id: str, source_hotel_id: str,
                  max_age_seconds: float = 3600.0) -> bool:
    """True for a fact row, or for a fresh negative marker.

    A failed page used to leave no row, so every cycle re-fetched the same
    broken page; with the marker it is skipped for one hour, then retried.
    """
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM hotel_provider_metadata "
            "WHERE search_id=? AND source='sravniotel' AND source_hotel_id=? AND provider='sravniotel' "
            "ORDER BY retrieved_at DESC LIMIT 1",
            (search_id, str(source_hotel_id)),
        ).fetchone()
    if row is None:
        return False
    payload = json.loads(row[0]) if row[0] else {}
    if not isinstance(payload, dict) or payload.get("source") != "sravniotel_failed_pages":
        return True
    try:
        retrieved_at = datetime.fromisoformat(str(payload.get("retrieved_at")))
        age = (datetime.now(timezone.utc) - retrieved_at).total_seconds()
    except (TypeError, ValueError):
        return True
    return age < max_age_seconds


def enrich_sravni_hotel_pages(database_path: str | Path, search_id: str,
                              limit: int = SRAVNI_DETAIL_MAX_PER_SEARCH) -> dict[str, Any]:
    """Fetch SravniOtel hotel pages and persist policy/amenity facts.

    Idempotent: hotels that already have a metadata row are skipped, so
    repeated enrichment cycles only fetch the remainder. A failed page
    fetch is honestly reported and leaves the hotel without facts (the
    next cycle retries it).
    """
    started = time.perf_counter()
    hotels = _load_sravni_hotels(database_path, search_id)
    pending = [hotel for hotel in hotels[:limit]
               if not _has_metadata(database_path, search_id, hotel["source_hotel_id"])]
    query = _load_search_query(database_path, search_id)
    check_in = str(query.get("check_in") or "")
    check_out = str(query.get("check_out") or "")
    adults = int(query.get("adults") or 2)
    enriched = 0
    failed = 0
    if pending:
        def work(hotel: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            # Preferred: the structured /api/v1/hotel endpoint (the same call
            # the hotel page itself makes). Fallback: HTML page parse.
            payload = _fetch_hotel_api(hotel["source_hotel_id"], check_in, check_out, adults)
            if payload is not None:
                facts = parse_sravni_hotel_api(payload)
                if facts.get("check_in_text") or facts.get("amenities_text"):
                    return hotel, facts
            html = _fetch_page(hotel["path"])
            return hotel, (parse_sravni_hotel_page(html) if html else None)

        with ThreadPoolExecutor(max_workers=SRAVNI_DETAIL_CONCURRENCY) as pool:
            for hotel, facts in pool.map(work, pending):
                if facts and (facts["check_in_text"] or facts["amenities_text"]):
                    try:
                        _persist_metadata(database_path, search_id, hotel["source_hotel_id"], facts)
                        enriched += 1
                    except sqlite3.OperationalError as exc:
                        # A locked/busy database must not kill the whole
                        # enrichment cycle; the hotel stays pending.
                        failed += 1
                        log.warning("[SRAVNI_DETAIL] persist_skipped hotel=%s error=%s",
                                    hotel["source_hotel_id"], str(exc)[:120])
                else:
                    failed += 1
                    # Negative marker: a broken/404 page used to be retried
                    # on every enrichment cycle. Skip it for an hour.
                    try:
                        _persist_metadata(database_path, search_id, hotel["source_hotel_id"],
                                          {"source": "sravniotel_failed_pages",
                                           "retrieved_at": _utc_timestamp()})
                    except sqlite3.OperationalError:
                        pass
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info("[SRAVNI_DETAIL] search=%s hotels=%d pending=%d enriched=%d failed=%d elapsed_ms=%s",
             search_id, len(hotels), len(pending), enriched, failed, elapsed_ms)
    return {"search_id": search_id, "sravni_hotels": len(hotels), "pending": len(pending),
            "enriched": enriched, "failed": failed, "elapsed_ms": elapsed_ms}


def lookup_sravni_policy(database_path: str | Path, search_id: str,
                         source_hotel_id: str) -> dict[str, Any] | None:
    """Return the persisted page facts for one SravniOtel hotel, if any."""
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM hotel_provider_metadata "
            "WHERE search_id=? AND source='sravniotel' AND source_hotel_id=? "
            "ORDER BY retrieved_at DESC LIMIT 1",
            (search_id, str(source_hotel_id)),
        ).fetchone()
    if not row:
        return None
    payload = json.loads(row[0]) if row[0] else None
    if not isinstance(payload, dict):
        return None
    # The negative failure marker is bookkeeping, not hotel facts.
    if payload.get("source") == "sravniotel_failed_pages":
        return None
    return payload
