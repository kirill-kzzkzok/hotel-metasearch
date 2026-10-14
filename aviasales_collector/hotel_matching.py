from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit


_TRANSLITERATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})
_GENERIC = {"hotel", "hotels", "otel", "otelya", "oteley", "the", "and", "i"}
_DISTINCTIVE = {
    "city", "downtown", "residence", "residences", "apartment", "apartments",
    "suite", "suites", "beach", "airport", "oldtown", "old", "new", "tower",
}
_LOCATION_GENERIC = {"plyazh", "beach", "center", "centre", "city", "downtown", "hotel", "hotels"}
_WEIGHTS = {
    "name": 0.36, "address": 0.22, "coordinates": 0.16, "phone": 0.08,
    "website": 0.04, "location_context": 0.06, "city": 0.025, "country": 0.025,
    "stars_agreement": 0.02, "chain": 0.02, "image_exact": 0.01,
}


def _blocking_keys(hotel: dict[str, Any]) -> set[tuple[str, str]]:
    """Return conservative candidate blocks for one provider hotel.

    Blocking is deliberately only an optimisation.  ``compare_hotels`` still
    validates every candidate and rejects conflicting city/country/address or
    coordinate evidence.  Name-token blocks keep transliterations and
    abbreviated provider names comparable while preventing an all-pairs scan.
    """
    keys: set[tuple[str, str]] = set()
    country = _normalize_country(hotel.get("country"))
    city = normalize_hotel_name(hotel.get("city"))
    tokens = [token for token in normalize_hotel_name(hotel.get("name")).split()
              if len(token) >= 4 and token not in _GENERIC]
    for token in tokens:
        keys.add(("name", token))
        if city:
            keys.add(("city_name", f"{city}|{token}"))
        if country:
            keys.add(("country_name", f"{country}|{token}"))
        if city and country:
            keys.add(("location_name", f"{country}|{city}|{token}"))
    # A provider may omit city/country.  A normalized name token is still a
    # useful candidate block; the feature scorer supplies the safety gate.
    return keys


def _normalize_country(value: str | None) -> str:
    normalized = normalize_hotel_name(value)
    aliases = {
        "uae": "united arab emirates",
        "u a e": "united arab emirates",
        "rossiya": "russia",
        "russian federation": "russia",
        "viet nam": "vietnam",
    }
    return aliases.get(normalized, normalized)


def normalize_hotel_name(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", (value or "").casefold().translate(_TRANSLITERATION))
    text = "".join(char for char in text if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9]+", text)
    tokens = [part for token in tokens
              for part in (("jumeirah", "village", "circle") if token == "jvc" else (token,))]
    return " ".join(token for token in tokens if token not in _GENERIC)


def _similarity(left: str | None, right: str | None) -> float | None:
    if not left or not right:
        return None
    a, b = normalize_hotel_name(left), normalize_hotel_name(right)
    if not a or not b:
        return None
    token_a, token_b = set(a.split()), set(b.split())
    jaccard = len(token_a & token_b) / len(token_a | token_b)
    sequence = SequenceMatcher(None, a, b).ratio()
    return max(jaccard, sequence)


def _coordinates(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        lat = value.get("lat", value.get("latitude"))
        lon = value.get("lon", value.get("lng", value.get("longitude")))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        lat, lon = value[0], value[1]
    else:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _distance_meters(left: tuple[float, float], right: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _phone(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) >= 7 else None


def _domain(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return host or None


def _image_keys(hotel: dict[str, Any]) -> set[str]:
    keys = set()
    for image in hotel.get("images") or []:
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url.startswith("https://"):
            keys.add(urlsplit(url).path.casefold())
    return keys


def compare_hotels(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    if left.get("source") == right.get("source"):
        return None

    left_name = normalize_hotel_name(left.get("name"))
    right_name = normalize_hotel_name(right.get("name"))
    if not left_name or not right_name:
        return None
    name_tokens_left, name_tokens_right = set(left_name.split()), set(right_name.split())
    # Branch and property-type qualifiers identify distinct properties, even within one chain.
    if (name_tokens_left ^ name_tokens_right) & _DISTINCTIVE:
        return None

    features: dict[str, float] = {}
    name = _similarity(left.get("name"), right.get("name"))
    if name is not None:
        features["name"] = name

    left_address, right_address = left.get("address"), right.get("address")
    address = _similarity(left_address, right_address)
    if address is not None:
        features["address"] = address
        left_numbers = set(re.findall(r"\b\d+[a-z]?\b", normalize_hotel_name(left_address)))
        right_numbers = set(re.findall(r"\b\d+[a-z]?\b", normalize_hotel_name(right_address)))
        if left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers) and address < 0.85:
            return None

    for source_hotel, other_address in ((left, right_address), (right, left_address)):
        address_tokens = set(normalize_hotel_name(other_address).split())
        for place in source_hotel.get("nearby_places") or []:
            place_tokens = set(normalize_hotel_name(place.get("name")).split()) - _LOCATION_GENERIC
            if len(place_tokens) < 2 or not address_tokens:
                continue
            matched_tokens = sum(
                any(token == address_token or (
                    min(len(token), len(address_token)) >= 3
                    and SequenceMatcher(None, token, address_token).ratio() >= 0.55
                ) for address_token in address_tokens)
                for token in place_tokens
            )
            overlap = matched_tokens / len(place_tokens)
            if overlap >= 0.75:
                features["location_context"] = max(features.get("location_context", 0), overlap)

    # Enriched coordinates are verified provider/detail/geocoder observations
    # and are valid matching evidence even when the provider card itself did
    # not contain coordinates.  They remain separate fields for provenance.
    left_coords = _coordinates(left.get("coordinates") or left.get("geocoded_coordinates"))
    right_coords = _coordinates(right.get("coordinates") or right.get("geocoded_coordinates"))
    distance = None
    if left_coords and right_coords:
        distance = _distance_meters(left_coords, right_coords)
        if distance > 500:
            return None
        # Provider/detail points can differ by a few metres (building
        # entrance versus map centroid).  Treat that range as agreement so a
        # verified coordinate does not lower an otherwise exact identity.
        features["coordinates"] = 1.0 if distance <= 50 else math.exp(-distance / 100)

    left_phone, right_phone = _phone(left.get("phone")), _phone(right.get("phone"))
    if left_phone and right_phone:
        if left_phone != right_phone:
            return None
        features["phone"] = 1.0

    left_website, right_website = _domain(left.get("website")), _domain(right.get("website"))
    if left_website and right_website:
        if left_website != right_website:
            return None
        features["website"] = 1.0

    left_city, right_city = normalize_hotel_name(left.get("city")), normalize_hotel_name(right.get("city"))
    if left_city and right_city:
        if left_city != right_city:
            return None
        features["city"] = 1.0

    left_country = _normalize_country(left.get("country"))
    right_country = _normalize_country(right.get("country"))
    if left_country and right_country:
        if left_country != right_country:
            return None
        features["country"] = 1.0

    left_stars, right_stars = left.get("stars"), right.get("stars")
    if left_stars is not None and right_stars is not None:
        features["stars_agreement"] = 1.0 if abs(float(left_stars) - float(right_stars)) < 0.5 else 0.0

    left_chain, right_chain = normalize_hotel_name(left.get("chain")), normalize_hotel_name(right.get("chain"))
    if left_chain and right_chain:
        if left_chain != right_chain:
            return None
        features["chain"] = 1.0

    left_images, right_images = _image_keys(left), _image_keys(right)
    if left_images and right_images:
        features["image_exact"] = 1.0 if left_images & right_images else 0.0

    exact_property_identity = (
        name is not None and name >= 0.999
        and len(name_tokens_left) >= 4
        and bool(left_city and right_city and left_city == right_city)
        and left_stars is not None and right_stars is not None
        and abs(float(left_stars) - float(right_stars)) < 0.5
    )

    available_weight = sum(_WEIGHTS[key] for key in features)
    if not available_weight or "name" not in features:
        return None
    score = sum(_WEIGHTS[key] * value for key, value in features.items()) / available_weight
    if score < 0.80:
        return None

    corroborating = sum(
        1 for key in ("address", "coordinates", "phone", "website", "image_exact")
        if key in features and features[key] >= (0.9 if key in {"address", "coordinates"} else 1.0)
    )
    if features.get("location_context", 0) >= 0.95:
        corroborating += 1
    if exact_property_identity:
        corroborating += 1
    confidence = score
    if corroborating == 0:
        confidence = min(confidence, 0.84)
    elif (set(features).intersection({"address", "coordinates"}) == set()
          and features.get("location_context", 0) < 0.95 and "location_context" in features):
        confidence = min(confidence, 0.90)
    auto_match = confidence >= 0.97 and features["name"] >= 0.95 and corroborating >= 1
    return {
        "source": right["source"],
        "source_hotel_id": str(right["source_hotel_id"]),
        "name": right.get("name"),
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "features": {key: round(value, 4) for key, value in features.items()},
        "distance_meters": round(distance, 1) if distance is not None else None,
        "decision": "auto_match" if auto_match else "review",
        "matcher_version": "multifeature-v2",
    }


def _match_comparison_payload(hotel: dict[str, Any]) -> str:
    """Serialize only evidence used by compare_hotels for exact pair caching."""
    stable = hotel.get("_match_evidence")
    stable = stable if isinstance(stable, dict) else {}
    nearby_places = []
    for place in hotel.get("nearby_places") or []:
        nearby_places.append({"name": place.get("name")} if isinstance(place, dict) else place)
    images = [image.get("url") if isinstance(image, dict) else image
              for image in stable.get("images", hotel.get("images")) or []]
    evidence = {
        "source": hotel.get("source"),
        "source_hotel_id": hotel.get("source_hotel_id"),
        "name": hotel.get("name"),
        "address": stable.get("address", hotel.get("address")),
        "coordinates": stable.get("coordinates", hotel.get("coordinates")),
        # Geocoding, provider-detail and rate enrichment happen after source
        # discovery. They must not change matching decisions or canonical IDs.
        "geocoded_coordinates": None if stable else hotel.get("geocoded_coordinates"),
        "phone": hotel.get("phone"),
        "website": hotel.get("website"),
        "city": hotel.get("city"),
        "country": hotel.get("country"),
        "stars": stable.get("stars", hotel.get("stars")),
        "chain": hotel.get("chain"),
    }
    evidence["nearby_places"] = nearby_places
    evidence["images"] = images
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


@lru_cache(maxsize=32_768)
def _compare_cached_evidence(left_payload: str, right_payload: str) -> dict[str, Any] | None:
    """Reuse an unchanged cross-source score without changing match semantics."""
    return compare_hotels(json.loads(left_payload), json.loads(right_payload))


def attach_match_candidates(hotels: list[dict[str, Any]]) -> None:
    for hotel in hotels:
        hotel["match_candidates"] = []
    pairs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    blocks: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for hotel in hotels:
        for block in _blocking_keys(hotel):
            blocks.setdefault(block, []).append(hotel)
    payload_by_identity = {id(hotel): _match_comparison_payload(hotel) for hotel in hotels}
    pair_ids: set[tuple[int, int]] = set()
    for bucket in blocks.values():
        for index, left in enumerate(bucket):
            for right in bucket[index + 1:]:
                if left.get("source") == right.get("source"):
                    continue
                identity = tuple(sorted((id(left), id(right))))
                if identity in pair_ids:
                    continue
                pair_ids.add(identity)
                cached = _compare_cached_evidence(
                    payload_by_identity[id(left)], payload_by_identity[id(right)],
                )
                # Candidate decisions below are adjusted for global one-to-one
                # ambiguity; never mutate the reusable cached evidence object.
                candidate = ({**cached, "features": dict(cached["features"])}
                             if cached is not None else None)
                if candidate is not None:
                    pairs.append((left, right, candidate))

    strong_counts: dict[int, int] = {}
    for left, right, candidate in pairs:
        if candidate["decision"] == "auto_match":
            strong_counts[id(left)] = strong_counts.get(id(left), 0) + 1
            strong_counts[id(right)] = strong_counts.get(id(right), 0) + 1
    for left, right, candidate in pairs:
        if candidate["decision"] == "auto_match" and not (
            strong_counts.get(id(left)) == strong_counts.get(id(right)) == 1
        ):
            candidate["decision"] = "review"
        reverse = {**candidate, "source": left["source"],
                   "source_hotel_id": str(left["source_hotel_id"]), "name": left.get("name")}
        left["match_candidates"].append(candidate)
        right["match_candidates"].append(reverse)


def current_auto_match_ids(hotels: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    identities = {
        (hotel["source"], str(hotel["source_hotel_id"])): str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hotel-source:{hotel['source']}:{hotel['source_hotel_id']}",
        ))
        for hotel in hotels
    }
    for hotel in hotels:
        left_key = (hotel["source"], str(hotel["source_hotel_id"]))
        for candidate in hotel.get("match_candidates", []):
            if candidate.get("decision") != "auto_match":
                continue
            right_key = (candidate["source"], str(candidate["source_hotel_id"]))
            pair = sorted((left_key, right_key))
            canonical_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hotel-cross-source:{pair[0][0]}:{pair[0][1]}|{pair[1][0]}:{pair[1][1]}",
            ))
            identities[left_key] = canonical_id
            identities[right_key] = canonical_id
    return identities


def merge_auto_matched_hotels(hotels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for hotel in hotels:
        canonical_id = hotel.get("canonical_hotel_id")
        if canonical_id:
            groups.setdefault(str(canonical_id), []).append(hotel)

    merged: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for hotel in hotels:
        canonical_id = str(hotel.get("canonical_hotel_id") or "")
        group = groups.get(canonical_id, [])
        if canonical_id in emitted:
            continue
        if len(group) < 2 or len({item.get("source") for item in group}) < 2:
            merged.append(hotel)
            continue

        emitted.add(canonical_id)
        group = sorted(group, key=lambda item: (item.get("reviews_count") or 0, item.get("source") == "trip_com"), reverse=True)
        base = dict(group[0])
        source_records = []
        offers = []
        offer_keys = set()
        ratings = []
        images = []
        image_keys = set()
        nearby_places = []
        amenities = []
        provider_details = []
        room_options = []
        room_rates = []
        room_option_keys = set()
        room_rate_keys = set()
        candidate_keys = set()
        candidates = []
        group_ids = {(item.get("source"), str(item.get("source_hotel_id"))) for item in group}
        for item in group:
            source_records.append({key: item.get(key) for key in (
                "source", "source_hotel_id", "name", "address", "stars", "rating",
                "rating_scale", "reviews_count", "coordinates", "phone", "website",
                "city", "country", "chain", "beach_access",
                "observed_at", "nearby_places", "review_quotes", "review_source",
                "geocoded_coordinates", "geocoded_coordinate_source",
                "coordinate_source", "coordinate_method", "coordinate_confidence",
                "coordinate_status", "coordinate_conflict", "coordinate_reason", "coordinate_attempts",
                "cached_beach_distance_meters", "cached_beach_name", "cached_beach_source",
                "cached_beach_relevant", "cached_beach_status",
                "provider_details", "property_details", "room_options", "room_rates",
                "room_rate_enrichment_status", "review_url",
            )})
            if item.get("rating") is not None:
                ratings.append({"source": item["source"], "rating": item["rating"],
                                "scale": item.get("rating_scale"), "reviews_count": item.get("reviews_count")})
            for image in item.get("images", []):
                key = json.dumps(image, sort_keys=True, ensure_ascii=False)
                if key not in image_keys:
                    image_keys.add(key)
                    images.append(image)
            for place in item.get("nearby_places", []):
                if place not in nearby_places:
                    nearby_places.append(place)
            for amenity in item.get("amenities", []):
                if amenity not in amenities:
                    amenities.append(amenity)
            for detail in item.get("provider_details", []):
                if not isinstance(detail, dict):
                    continue
                detail_key = (detail.get("provider"), detail.get("source"), detail.get("method"))
                if not any((saved.get("provider"), saved.get("source"), saved.get("method")) == detail_key
                           for saved in provider_details):
                    provider_details.append(detail)
            for option in item.get("room_options", []):
                option_key = json.dumps(option, sort_keys=True, ensure_ascii=False, default=str)
                if option_key not in room_option_keys:
                    room_option_keys.add(option_key)
                    room_options.append(option)
            for rate in item.get("room_rates", []):
                rate_key = json.dumps(rate, sort_keys=True, ensure_ascii=False, default=str)
                if rate_key not in room_rate_keys:
                    room_rate_keys.add(rate_key)
                    room_rates.append(rate)
            for offer in item.get("offers", []):
                offer_key = json.dumps(offer, sort_keys=True, ensure_ascii=False, default=str)
                if offer_key not in offer_keys:
                    offer_keys.add(offer_key)
                    offers.append(offer)
            for candidate in item.get("match_candidates", []):
                target = (candidate.get("source"), str(candidate.get("source_hotel_id")))
                if target in group_ids:
                    continue
                key = (target, candidate.get("decision"), candidate.get("score"))
                if key not in candidate_keys:
                    candidate_keys.add(key)
                    candidates.append(candidate)

        offers.sort(key=lambda item: (item.get("amount") is None,
                                      item.get("currency") or "",
                                      float(item["amount"]) if item.get("amount") is not None else 0))
        for offer in offers:
            offer["canonical_hotel_id"] = canonical_id
        match_confidence = max((candidate.get("confidence", candidate.get("score", 0))
                                for item in group for candidate in item.get("match_candidates", [])
                                if candidate.get("decision") == "auto_match"), default=None)
        base.update({
            "source": "canonical",
            "rating_source": group[0].get("source"),
            "source_hotel_id": None,
            "canonical_id": canonical_id,
            "normalized_name": normalize_hotel_name(group[0].get("name")),
            "source_records": source_records,
            "sources": sorted({item["source"] for item in source_records}),
            "rating_by_source": ratings,
            "stars_by_source": [
                {"source": item["source"], "stars": item.get("stars")}
                for item in source_records if item.get("stars") is not None
            ],
            "images": images,
            "nearby_places": nearby_places,
            "amenities": amenities,
            "provider_details": provider_details,
            "property_details": next((item.get("property_details") for item in group
                                       if isinstance(item.get("property_details"), dict)), None),
            "room_options": room_options,
            "room_rates": room_rates,
            "room_rate_enrichment_status": next((item.get("room_rate_enrichment_status") for item in group
                                                  if item.get("room_rate_enrichment_status")), None),
            "review_url": next((item.get("review_url") for item in group if item.get("review_url")), None),
            "offers": offers,
            "match_candidates": candidates,
            "match_confidence": match_confidence,
        })
        # Canonical values are facts selected from source records.  Ratings
        # are only collapsed when every source uses the same scale; otherwise
        # the per-source values remain authoritative.
        base["canonical_rating"] = None
        base["canonical_review_count"] = None
        known_stars = {round(float(record["stars"]), 1) for record in source_records
                       if isinstance(record.get("stars"), (int, float))
                       and not isinstance(record.get("stars"), bool)}
        # Do not silently choose one source when verified star classifications
        # disagree. Keep the source evidence above and leave the canonical value unknown.
        base["stars"] = next(iter(known_stars)) if len(known_stars) == 1 else None
        rating_scales = {record.get("rating_scale") for record in source_records
                         if record.get("rating") is not None and record.get("rating_scale") is not None}
        if len(rating_scales) == 1:
            rated = [record for record in group if record.get("rating") is not None]
            if rated:
                selected_rating = max(rated, key=lambda item: item.get("reviews_count") or 0)
                base["canonical_rating"] = selected_rating.get("rating")
                base["canonical_review_count"] = selected_rating.get("reviews_count")
                base["rating"] = selected_rating.get("rating")
                base["rating_scale"] = selected_rating.get("rating_scale")
        elif len(rating_scales) > 1:
            # Ratings on different scales remain source-specific. In particular,
            # do not feed an arbitrary one into the shared rating filter/rank.
            base["rating"] = None
            base["rating_scale"] = None
        base["beach_access"] = next(
            (item.get("beach_access") for item in group
             if item.get("beach_access") and item.get("beach_access") != "unknown"),
            "unknown",
        )
        merged.append(base)
    return merged


def persist_match_candidates(connection: Any, search_id: str, hotels: list[dict[str, Any]]) -> None:
    for hotel in hotels:
        for candidate in hotel.get("match_candidates", []):
            pair = sorted(((hotel["source"], str(hotel["source_hotel_id"])),
                           (candidate["source"], candidate["source_hotel_id"])))
            connection.execute(
                """INSERT INTO hotel_match_candidate
                (search_id,left_source,left_id,right_source,right_id,score,features_json,decision,matcher_version)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(search_id,left_source,left_id,right_source,right_id) DO UPDATE SET
                score=excluded.score,features_json=excluded.features_json,decision=excluded.decision,
                matcher_version=excluded.matcher_version""",
                (search_id, pair[0][0], pair[0][1], pair[1][0], pair[1][1], candidate["score"],
                 json.dumps(candidate["features"], sort_keys=True), candidate["decision"],
                 candidate["matcher_version"]),
            )
