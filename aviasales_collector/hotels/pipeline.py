"""Canonical hotel matching, quality, ranking, filtering and audit assembly."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ..hotel_matching import (attach_match_candidates, current_auto_match_ids,
                              merge_auto_matched_hotels, normalize_hotel_name,
                              persist_match_candidates)
from ..hotel_quality import assess_hotel_quality
from ..hotel_ranking import rank_hotels
from ..hotel_reviews import analyze_review_quotes
from .geo.city import _read_city_reference
from .geo.core import _location_summary
from .models import utc_now
from .normalization import _normalize_rating_fields
from .runtime import _profiled_sqlite_connect, log, log_search_timeline


def _reconcile_match_state(path: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    """Recompute and persist matching for the complete lightweight search set."""
    match_started = time.perf_counter()
    hotels = result["hotels"]
    attach_match_candidates(hotels)
    matching_ms = (time.perf_counter() - match_started) * 1000
    candidate_pairs = {}
    for hotel in hotels:
        for candidate in hotel["match_candidates"]:
            pair = tuple(sorted(((hotel["source"], str(hotel["source_hotel_id"])),
                                 (candidate["source"], str(candidate["source_hotel_id"])))) )
            candidate_pairs[pair] = candidate
    review_candidates = sum(item["decision"] == "review" for item in candidate_pairs.values())
    automatic_candidates = sum(item["decision"] == "auto_match" for item in candidate_pairs.values())
    current_ids = current_auto_match_ids(hotels)
    city_reference = None
    match_db_started = time.perf_counter()
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        city_reference = _read_city_reference(connection, result.get("query") or {})
        persist_match_candidates(connection, result["search_id"], hotels)
        for hotel in hotels:
            identity = (hotel["source"], str(hotel["source_hotel_id"]))
            canonical_id = current_ids.get(identity)
            if not canonical_id:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO canonical_hotel(id,display_name,created_at) VALUES (?,?,?)",
                (canonical_id, hotel["name"], utc_now()),
            )
            connection.execute(
                "UPDATE hotel_source_identity SET canonical_hotel_id=? WHERE source=? AND source_hotel_id=?",
                (canonical_id, identity[0], identity[1]),
            )
        connection.commit()
    match_db_ms = (time.perf_counter() - match_db_started) * 1000
    for hotel in hotels:
        hotel["canonical_hotel_id"] = current_ids.get((hotel["source"], str(hotel["source_hotel_id"])))
    return {
        "current_ids": current_ids,
        "city_reference": city_reference,
        "matching_ms": matching_ms,
        "match_db_ms": match_db_ms,
        "candidate_pairs": candidate_pairs,
        "review_candidates": review_candidates,
        "automatic_candidates": automatic_candidates,
    }


def _finalize_search(
    path: str | Path, result: dict[str, Any], *, match_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finalize_started = time.perf_counter()
    search_context_id = str(result.get("search_id") or "")
    result["search_context_id"] = search_context_id
    hotels = result["hotels"]
    if match_state is None:
        match_state = _reconcile_match_state(path, result)
    matching_ms = float(match_state["matching_ms"])
    match_db_ms = float(match_state["match_db_ms"])
    candidate_pairs = match_state["candidate_pairs"]
    review_candidates = int(match_state["review_candidates"])
    automatic_candidates = int(match_state["automatic_candidates"])
    current_ids = match_state["current_ids"]
    city_reference = match_state["city_reference"]
    merge_started = time.perf_counter()
    for hotel in hotels:
        hotel["canonical_hotel_id"] = current_ids.get(
            (hotel["source"], str(hotel["source_hotel_id"])), hotel.get("canonical_hotel_id"),
        )
    canonical_hotels = merge_auto_matched_hotels(hotels)
    merge_ms = (time.perf_counter() - merge_started) * 1000
    for hotel in canonical_hotels:
        records = hotel.get("source_records") or [hotel]
        for record in records:
            record.update(_normalize_rating_fields(
                record.get("rating_original", record.get("rating")), record.get("rating_scale"),
                record.get("rating_source") or record.get("source"),
            ))
        rated_records = [record for record in records if record.get("rating_normalized") is not None]
        selected_rating = max(rated_records, key=lambda record: record.get("reviews_count") or 0,
                              default=None)
        hotel["rating_by_source"] = [
            {"source": record.get("source"), "rating": record.get("rating"),
             "rating_original": record.get("rating_original"),
             "rating_scale": record.get("rating_scale"),
             "rating_normalized": record.get("rating_normalized"),
             "rating_source": record.get("rating_source"),
             "reviews_count": record.get("reviews_count")}
            for record in rated_records
        ]
        if selected_rating:
            hotel["rating_original"] = selected_rating.get("rating_original")
            hotel["rating_scale"] = selected_rating.get("rating_scale")
            hotel["rating_normalized"] = selected_rating.get("rating_normalized")
            hotel["rating_source"] = selected_rating.get("rating_source")
        else:
            hotel.update(_normalize_rating_fields(
                hotel.get("rating_original", hotel.get("rating")), hotel.get("rating_scale"),
                hotel.get("rating_source") or hotel.get("source"),
            ))
        hotel["search_context_id"] = search_context_id
        for record in records:
            record["search_context_id"] = search_context_id
        hotel["location"] = _location_summary(hotel, records, city_reference)
        location_coordinates = hotel["location"].get("coordinates") or {}
        hotel["canonical_id"] = hotel.get("canonical_id") or hotel.get("canonical_hotel_id")
        hotel["normalized_name"] = hotel.get("normalized_name") or normalize_hotel_name(hotel.get("name"))
        hotel["entity_type"] = "CanonicalHotel"
        hotel.setdefault("sources", [hotel.get("source")] if hotel.get("source") else [])
        for offer in hotel.get("offers", []):
            offer["canonical_hotel_id"] = hotel.get("canonical_id")
            offer["search_context_id"] = search_context_id
        if hotel.get("canonical_rating") is None and hotel.get("rating") is not None:
            hotel["canonical_rating"] = hotel.get("rating")
            hotel["canonical_review_count"] = hotel.get("reviews_count")
        hotel["latitude"] = location_coordinates.get("lat") if isinstance(location_coordinates, dict) else None
        hotel["longitude"] = location_coordinates.get("lng") if isinstance(location_coordinates, dict) else None
        hotel["canonical_rating"] = hotel.get("canonical_rating")
        hotel["canonical_review_count"] = hotel.get("canonical_review_count")
        if hotel.get("source") != "canonical":
            hotel.setdefault("beach_access", "unknown")
        hotel["review_stats"] = [
            {"source": record.get("source"), "rating": record.get("rating"),
             "rating_scale": record.get("rating_scale"), "reviews_count": record.get("reviews_count"),
             "retrieved_at": record.get("observed_at")}
            for record in records if record.get("rating") is not None or record.get("reviews_count") is not None
        ]
        quote_record = next((record for record in records
                             if record.get("review_source") == "trip_com" and record.get("review_quotes")), None)
        hotel["review_summary"] = analyze_review_quotes(
            quote_record.get("review_quotes", []) if quote_record else [],
            quote_record.get("review_source") if quote_record else None,
            quote_record.get("observed_at") if quote_record else None,
        )
        for record in records:
            record.pop("geocoded_coordinates", None)
            record.pop("geocoded_coordinate_source", None)
        hotel.pop("_match_evidence", None)
        # Raw provider payloads stay in SQLite observations; the public API
        # exposes normalized provider records and our canonical offers only.
        hotel.pop("raw_payload", None)

    # Keep the durable canonical row in sync with the normalized response.
    with closing(_profiled_sqlite_connect(str(path), timeout=10)) as connection:
        now = utc_now()
        for hotel in canonical_hotels:
            canonical_id = hotel.get("canonical_hotel_id") or hotel.get("canonical_id")
            if not canonical_id:
                continue
            coords = hotel.get("location", {}).get("coordinates") or {}
            coordinate_source = hotel.get("location", {}).get("coordinate_source")
            coordinate_method = hotel.get("location", {}).get("coordinate_method")
            coordinate_confidence = hotel.get("location", {}).get("coordinate_confidence")
            connection.execute(
                """INSERT INTO canonical_hotel
                (id,display_name,normalized_name,country,city,address,latitude,longitude,
                 coordinate_source,coordinate_method,coordinate_confidence,stars,
                 canonical_rating,canonical_review_count,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  display_name=excluded.display_name,normalized_name=excluded.normalized_name,
                  country=excluded.country,city=excluded.city,address=excluded.address,
                  latitude=COALESCE(excluded.latitude,canonical_hotel.latitude),
                  longitude=COALESCE(excluded.longitude,canonical_hotel.longitude),
                  coordinate_source=COALESCE(excluded.coordinate_source,canonical_hotel.coordinate_source),
                  coordinate_method=COALESCE(excluded.coordinate_method,canonical_hotel.coordinate_method),
                  coordinate_confidence=COALESCE(excluded.coordinate_confidence,canonical_hotel.coordinate_confidence),
                  stars=excluded.stars,
                  canonical_rating=excluded.canonical_rating,
                  canonical_review_count=excluded.canonical_review_count,updated_at=excluded.updated_at""",
                (str(canonical_id), hotel.get("name") or "",
                 hotel.get("normalized_name"), hotel.get("country"), hotel.get("city"),
                 hotel.get("address"), coords.get("lat"), coords.get("lng"), coordinate_source,
                 coordinate_method, coordinate_confidence, hotel.get("stars"),
                 hotel.get("canonical_rating"), hotel.get("canonical_review_count"), now, now),
            )
        connection.commit()

    ranking_started = time.perf_counter()
    eligible_hotels, quality_signals = assess_hotel_quality(canonical_hotels)
    rank_hotels(eligible_hotels)
    eligible_hotels.sort(key=lambda hotel: (
        hotel.get("rating") is None,
        hotel.get("ranking", {}).get("quality_score") is None,
        -(hotel.get("ranking", {}).get("quality_score") or 0),
        -(hotel.get("reviews_count") or 0),
    ))
    ranking_ms = (time.perf_counter() - ranking_started) * 1000
    result["hotels"] = eligible_hotels
    raw_by_source = {source["source"]: source.get("hotel_count", 0) for source in result.get("sources", [])}
    raw_cards_by_source = {
        source["source"]: (
            source.get("collection_audit", {}).get("raw_cards")
            if isinstance(source.get("collection_audit"), dict)
            else None
        )
        for source in result.get("sources", [])
    }
    normalized_by_source = {
        source: sum(1 for hotel in hotels if hotel.get("source") == source)
        for source in {hotel.get("source") for hotel in hotels if hotel.get("source")}
    }
    cross_source_matches = sum(
        hotel.get("source") == "canonical" and len(hotel.get("sources", [])) > 1
        for hotel in canonical_hotels
    )
    review_metrics_available = sum(
        record.get("rating") is not None and record.get("reviews_count") is not None
        for hotel in canonical_hotels for record in (hotel.get("source_records") or [hotel])
    )
    review_enriched = sum(hotel.get("review_summary", {}).get("status") == "quoted_mentions"
                          for hotel in canonical_hotels)
    review_theme_summaries = sum(bool(hotel.get("review_summary", {}).get("mentions"))
                                 for hotel in canonical_hotels)
    result["search_audit"] = {
        "raw_hotels_by_source": raw_by_source,
        "raw_hotels": sum(raw_by_source.values()),
        "raw_cards_by_source": raw_cards_by_source,
        "raw_cards": sum(value for value in raw_cards_by_source.values() if isinstance(value, int)),
        "raw_cards_exact": all(value is not None for value in raw_cards_by_source.values()),
        "normalized_by_source": normalized_by_source,
        "deduplicated_by_source": normalized_by_source,
        "normalized_source_records": len(hotels),
        "deduplicated_source_records": len(hotels),
        "cross_source_matches": cross_source_matches,
        "match_candidates": review_candidates,
        "automatic_matches": automatic_candidates,
        "ambiguous_matches": review_candidates,
        "removed_no_rating": 0,
        "removed_under_3_reviews": 0,
        "removed_no_offer": quality_signals["no_offer"],
        "retained_no_rating": quality_signals["no_rating"],
        "retained_under_3_reviews": quality_signals["low_reviews"],
        "quality_eligible_hotels": len(eligible_hotels),
        "review_metrics_available": review_metrics_available,
        "review_enriched": review_enriched,
        "review_theme_summaries": review_theme_summaries,
        "deep_review_summaries": 0,
        "pagination_available": bool(result.get("has_more")),
        "collection_complete_by_source": {
            source["source"]: source.get("collection_complete")
            for source in result.get("sources", [])
            if source.get("collection_complete") is not None
        },
        "collection_reason_by_source": {
            source["source"]: source.get("collection_reason")
            for source in result.get("sources", [])
            if source.get("collection_reason")
        },
    }
    room_mode = result.get("query", {}).get("room_mode", "private")
    all_count = len(result["hotels"])
    if room_mode == "private":
        filtered = []
        for hotel in result["hotels"]:
            offers = [offer for offer in hotel.get("offers", [])
                      if offer.get("room_privacy") == "private"]
            if offers:
                filtered.append({**hotel, "offers": offers})
        result["hotels"] = filtered
    result["room_audit"] = {
        "mode": room_mode,
        "eligible_hotels": len(result["hotels"]),
        "all_hotels": all_count,
    }
    source_names = set(raw_by_source) | set(normalized_by_source)
    matched_by_source = {source: 0 for source in source_names}
    for hotel in canonical_hotels:
        records = hotel.get("source_records") or [hotel]
        if len({record.get("source") for record in records}) > 1:
            for source in {record.get("source") for record in records}:
                if source:
                    matched_by_source[source] = matched_by_source.get(source, 0) + 1
    final_by_source = {source: 0 for source in source_names}
    for hotel in result["hotels"]:
        for source in hotel.get("sources") or [hotel.get("source")]:
            if source:
                final_by_source[source] = final_by_source.get(source, 0) + 1
    source_records = [record for hotel in result["hotels"]
                      for record in (hotel.get("source_records") or [hotel])]
    trip_records = [record for record in source_records if record.get("source") == "trip_com"]
    audit = result["search_audit"]
    audit.update({
        "matched_by_source": matched_by_source,
        "unmatched_by_source": {source: max(0, normalized_by_source.get(source, 0)
                                             - matched_by_source.get(source, 0))
                                for source in source_names},
        "canonical_hotels": len(canonical_hotels),
        "final_by_source": final_by_source,
        "render_ready_cards": len(result["hotels"]),
        "offers": sum(len(hotel.get("offers", [])) for hotel in result["hotels"]),
        "room_variants": sum(1 for hotel in result["hotels"] for room in hotel.get("room_options", [])
                             if str(room.get("room_type") or "").strip()
                             and not re.fullmatch(r"(?:room|номер)\s*\d+", str(room.get("room_type")).strip(), re.I)),
        "trip_enriched": sum(bool(record.get("room_rate_enrichment_status")) for record in trip_records),
        "coverage": {
            "stars": sum(hotel.get("stars") is not None for hotel in result["hotels"]),
            "rating": sum(hotel.get("rating") is not None for hotel in result["hotels"]),
            "reviews": sum(hotel.get("reviews_count") is not None for hotel in result["hotels"]),
            "beach_distance": sum(hotel.get("location", {}).get("distance_to_beach_km") is not None
                                  for hotel in result["hotels"]),
            "center_distance": sum(hotel.get("location", {}).get("distance_to_center_km") is not None
                                   for hotel in result["hotels"]),
            "rooms": sum(bool(hotel.get("room_options")) for hotel in result["hotels"]),
            "meal": sum(any(offer.get("meal_plan") not in (None, "unknown")
                            for offer in hotel.get("offers", [])) for hotel in result["hotels"]),
            "cancellation": sum(any(offer.get("cancellation") not in (None, "unknown")
                                    for offer in hotel.get("offers", [])) for hotel in result["hotels"]),
        },
    })
    for source in sorted(source_names):
        raw_count = int(raw_by_source.get(source) or 0)
        normalized_count = int(normalized_by_source.get(source) or 0)
        matched_count = int(matched_by_source.get(source) or 0)
        log.info("[SEARCH_AUDIT] source=%s raw=%d normalized=%d matched=%d unmatched=%d final=%d",
                 source, raw_count, normalized_count, matched_count,
                 max(0, normalized_count - matched_count), int(final_by_source.get(source) or 0))
    log.info("[SEARCH_AUDIT] raw_unique=%d canonical=%d rendered=%d offers=%d room_variants=%d",
             len(hotels), len(canonical_hotels), len(result["hotels"]),
             audit["offers"], audit["room_variants"])
    log_search_timeline(
        search_context_id, "matching_and_ranking_finished",
        input_hotels=len(hotels), canonical_hotels=len(canonical_hotels),
        attach_match_candidates_ms=round(matching_ms, 1),
        matching_db_ms=round(match_db_ms, 1), merge_ms=round(merge_ms, 1),
        quality_ranking_ms=round(ranking_ms, 1),
        finalize_total_ms=round((time.perf_counter() - finalize_started) * 1000, 1),
    )
    return result
