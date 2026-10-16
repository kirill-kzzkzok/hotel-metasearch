"""Shared hotel, offer, meal, rating, beach-line, and city-label normalization."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from ..hotel_matching import normalize_hotel_name
from .models import utc_now

_SHARED_ROOM_RE = re.compile(
    r"\b(?:bed\s+in|dorm(?:itory)?|shared\s+(?:room|dorm)|bunk\s*bed|capsule|pod|"
    r"bed\s*space|hostel\s*bed|койк\w*|общежити\w*|общ(?:ая|ей)\s+комнат\w*)\b", re.I
)

_PRIVATE_ROOM_RE = re.compile(
    r"\b(?:private\s+(?:room|double|bathroom)|double\s+room|twin\s+room|single\s+room|"
    r"king\s+room|queen\s+room|standard\s+room|deluxe\s+room|superior\s+room|"
    r"studio|apartment|entire\s+apartment|suite|family\s+room|"
    r"номер\s+(?:делюкс|люкс)|(?:улучшенн|стандартн|делюкс|комфорт|классическ)\w*\s+номер|"
    r"апартамент\w*|студио|люкс|семейн\w*\s+номер|двухместн\w*|одноместн\w*)\b", re.I
)

def classify_room_privacy(room_type: Any, bed_type: Any = None) -> str:
    """Classify only explicit offer-level room evidence; missing data stays unknown."""
    text = " ".join(str(value).strip() for value in (room_type, bed_type) if value).strip()
    if not text:
        return "unknown"
    if re.search(r"\bprivate\s+(?:room|double)\b|приватн\w*\s+номер", text, re.I):
        return "private"
    if _SHARED_ROOM_RE.search(text):
        return "shared"
    if _PRIVATE_ROOM_RE.search(text):
        return "private"
    return "unknown"

_MEAL_PATTERNS = {
    "all_inclusive": (r"all\s*[- ]?inclusive", r"все\s*включено", r"всё\s*включено"),
    "full_board": (r"full\s*board", r"полный\s*пансион"),
    "half_board": (r"half\s*board", r"полупансион", r"завтрак\s*(?:и|\+)\s*ужин"),
    "breakfast": (r"breakfast", r"завтрак"),
    "no_meal": (r"room\s*only", r"без\s*питания", r"только\s*номер", r"no\s*meals?",
                 r"meals?\s+(?:are\s+)?(?:not\s+included|excluded|not\s+provided)",
                 r"(?:питание|при[её]мы\s+пищи)\s+(?:не\s+включено|не\s+предусмотрено)"),
}

MEAL_NORMALIZER_VERSION = "meal_v4"

def _has_included_breakfast(evidence: str) -> bool:
    return bool(re.search(
        r"breakfast(?:\s+is)?\s+included|included\s+breakfast|bed\s*(?:and|&)\s*breakfast|"
        r"free breakfast|завтрак[^|;]{0,50}(?:включ[её]н|бесплат)|"
        r"включ[её]н\w*[^|;]{0,50}завтрак",
        evidence, re.I,
    ))

def _breakfast_extra_price(evidence: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"(?:breakfast|завтрак)[^|;]{0,40}?(?:for|за)\s*([\d\s\u00a0.,]+?)\s*(₽|RUB|€|EUR|\$|USD)(?=$|\W)",
        evidence, re.I,
    )
    if not match:
        return None, None
    number = re.sub(r"[\s\u00a0]", "", match.group(1))
    if "," in number and "." in number:
        decimal_separator = "," if number.rfind(",") > number.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        number = number.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None, None
    if not amount.is_finite() or amount <= 0:
        return None, None
    currency = {"₽": "RUB", "€": "EUR", "$": "USD"}.get(match.group(2).upper(), match.group(2).upper())
    return str(amount), currency

def classify_meal_plan(value: Any = None, labels: Any = None, text: Any = None) -> str:
    """Return a meal enum only when the offer contains explicit board evidence."""
    label_values = labels if isinstance(labels, (list, tuple, set)) else ([labels] if labels else [])
    raw_value = str(value or "").strip().casefold()
    value_code = re.sub(r"[\s-]+", "_", raw_value)
    canonical = {
        "breakfast_included": "breakfast", "bed_and_breakfast": "breakfast",
        "room_only": "room_only", "no_meals": "room_only", "no_meal": "room_only",
        "half_board": "half_board", "full_board": "full_board",
        "all_inclusive": "all_inclusive", "breakfast_dinner": "breakfast_dinner",
        "dinner": "dinner", "lunch_dinner": "lunch_dinner",
    }
    if value_code in canonical:
        return canonical[value_code]

    # Source label codes can be unambiguous even when they are not natural language.
    label_codes = {re.sub(r"[\s-]+", "_", str(part).strip().casefold()) for part in label_values}
    for code, meal in canonical.items():
        if code in label_codes:
            return meal

    evidence = " ".join(str(part).strip() for part in (value, text, *label_values) if part)
    evidence = re.sub(r"[_-]+", " ", evidence).casefold()
    if re.search(
        r"(?:breakfast|завтрак)[^|;]{0,100}(?:not\s+included|excluded|не\s+включ)"
        r"|(?:not\s+included|excluded|не\s+включ)[^|;]{0,100}(?:breakfast|завтрак)",
        evidence, re.I,
    ):
        return "room_only"
    if re.search(
        r"(?:breakfast|завтрак)[^|;]{0,100}(?:additional|extra|paid|charge|surcharge|optional|available|доплат|платн|по\s+запросу|по\s+желанию)"
        r"|(?:additional|extra|paid|charge|surcharge|optional|доплат|платн)[^|;]{0,100}(?:breakfast|завтрак)",
        evidence, re.I,
    ):
        return "unknown"
    if re.search(r"breakfast\s*(?:and|&|\+)\s*dinner|завтрак\s*(?:и|\+)\s*ужин", evidence, re.I):
        return "breakfast_dinner"
    if re.search(r"lunch\s*(?:and|&|\+)\s*dinner|обед\s*(?:и|\+)\s*ужин", evidence, re.I):
        return "lunch_dinner"

    if _has_included_breakfast(evidence):
        return "breakfast"

    if re.search(
        r"(?:dinner|ужин|lunch|обед)[^|;]{0,80}(?:additional|extra|paid|charge|surcharge|optional|available|доплат|платн|по\s+запросу|по\s+желанию)"
        r"|(?:additional|extra|paid|charge|surcharge|optional|доплат|платн)[^|;]{0,80}(?:dinner|ужин|lunch|обед)",
        evidence, re.I,
    ):
        return "unknown"
    if re.search(r"(?:dinner|ужин)[^|;]{0,40}(?:included|included with|включ[её]н)|(?:included|включ[её]н)[^|;]{0,40}(?:dinner|ужин)", evidence, re.I):
        return "dinner"

    # Breakfast alone is not proof that this specific rate includes it.
    evidence_without_breakfast = re.sub(r"breakfast|завтрак", " ", evidence, flags=re.I)
    for meal, patterns in _MEAL_PATTERNS.items():
        if meal == "breakfast":
            continue
        if any(re.search(pattern, evidence_without_breakfast, re.I) for pattern in patterns):
            return "room_only" if meal == "no_meal" else meal
    return "unknown"

def _offer_meal_facts(offer: dict[str, Any], source: str) -> dict[str, Any]:
    """Normalize only meal evidence attached to this offer; keep source paths and raw labels."""
    evidence_fields = ("meal_plan", "board", "meal", "rate_name", "offer_title", "description")
    raw_parts: list[str] = []
    source_paths: list[str] = []
    for key in evidence_fields:
        value = offer.get(key)
        if isinstance(value, str) and value.strip() and value.strip().casefold() != "unknown":
            raw_parts.append(value.strip())
            source_paths.append(f"offer.{key}")
    labels = offer.get("labels")
    if isinstance(labels, (list, tuple)):
        meal_labels = [str(label).strip() for label in labels if isinstance(label, (str, int, float))
                       and re.search(r"meal|breakfast|board|inclusive|питан|завтрак|пансион", str(label), re.I)]
        if meal_labels:
            raw_parts.extend(meal_labels)
            source_paths.extend(["offer.labels"] * len(meal_labels))

    raw_text = " | ".join(dict.fromkeys(raw_parts)) or None
    meal_plan = classify_meal_plan(
        offer.get("meal_plan"), labels,
        " ".join(str(offer.get(key) or "") for key in ("board", "meal", "rate_name", "offer_title", "description")),
    )
    all_evidence = re.sub(r"[_-]+", " ", raw_text or "").casefold()
    breakfast = _breakfast_facts(raw_text, meal_plan=meal_plan)
    meal_terms = r"(?:breakfast|meal|board|dinner|lunch|завтрак|питан\w*|ужин|обед|пансион)"
    paid_terms = r"(?:additional|extra|paid|charge|surcharge|доплат\w*|платн\w*|по желанию|по запросу|optional)"
    explicit_paid_meal = bool(re.search(
        rf"{meal_terms}[^|;]{{0,100}}{paid_terms}|{paid_terms}[^|;]{{0,100}}{meal_terms}",
        all_evidence, re.I,
    ))
    explicit_meal_not_included = bool(re.search(
        rf"{meal_terms}[^|;]{{0,100}}(?:not\s+included|excluded|not\s+provided|не\s+включ|не\s+предусмотр)|"
        rf"(?:not\s+included|excluded|not\s+provided|не\s+включ|не\s+предусмотр)[^|;]{{0,100}}{meal_terms}",
        all_evidence, re.I,
    ))
    included_plans = {"breakfast", "breakfast_dinner", "half_board", "full_board",
                      "all_inclusive", "dinner", "lunch_dinner"}
    included = True if meal_plan in included_plans else \
        False if meal_plan == "room_only" or explicit_paid_meal or explicit_meal_not_included else None
    meal_status = ("included" if meal_plan in included_plans else
                   "paid" if explicit_paid_meal else
                   "not_included" if meal_plan == "room_only" or explicit_meal_not_included else
                   "unknown")
    available = True if included is True or explicit_paid_meal or breakfast["breakfast_available"] is True else None
    extra_amount, extra_currency = _breakfast_extra_price(raw_text or "")
    known = meal_plan != "unknown" or meal_status != "unknown"
    return {
        "meal_plan": meal_plan,
        "meal_status": meal_status,
        "meal_included": included,
        "meal_available": available,
        "meal_source": f"{source}:{','.join(dict.fromkeys(source_paths))}" if source_paths else None,
        "meal_confidence": "high" if known or breakfast["breakfast_available"] is True else "none",
        "meal_raw_text": raw_text,
        "meal_extra_amount": extra_amount,
        "meal_extra_currency": extra_currency,
        "meal_normalizer_version": MEAL_NORMALIZER_VERSION,
        **breakfast,
    }

def _hotel_breakfast_facts(item: dict[str, Any], source: str) -> dict[str, Any]:
    """Read structured hotel amenity fields without using reviews or offer terms."""
    values: list[str] = []
    for key in ("amenities", "facilities", "hotel_facilities", "features", "services"):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            for entry in value:
                if isinstance(entry, (str, int, float)):
                    values.append(str(entry))
                elif isinstance(entry, dict):
                    values.extend(str(entry[field]) for field in ("name", "title", "label", "text")
                                  if isinstance(entry.get(field), (str, int, float)))
        elif isinstance(value, str):
            values.append(value)
    breakfast_values = [value.strip() for value in values
                        if re.search(r"breakfast|завтрак", value, re.I)]
    if not breakfast_values:
        return {"breakfast_available": None, "breakfast_source": None, "breakfast_raw_text": None}
    return {"breakfast_available": True, "breakfast_source": f"{source}:hotel_amenities",
            "breakfast_raw_text": " | ".join(dict.fromkeys(breakfast_values))}

def compare_offers(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Compare two offers using booking context and explicit included terms only."""
    reasons: list[str] = []
    left_hotel = left.get("canonical_hotel_id") or left.get("canonical_id")
    right_hotel = right.get("canonical_hotel_id") or right.get("canonical_id")
    if left_hotel and right_hotel and str(left_hotel) != str(right_hotel):
        return {"status": "unknown", "reasons": ["different_hotels"]}
    core = ("check_in", "check_out", "adults", "children_count")
    missing_context = [key for key in core if left.get(key) is None or right.get(key) is None]
    mismatched_context = [key for key in core if left.get(key) is not None and right.get(key) is not None
                          and left.get(key) != right.get(key)]
    if missing_context:
        reasons.extend(f"missing_{key}" for key in missing_context)
    if mismatched_context:
        reasons.extend(f"different_{key}" for key in mismatched_context)
    currency_left, currency_right = left.get("normalized_currency") or left.get("currency"), \
        right.get("normalized_currency") or right.get("currency")
    if not currency_left or not currency_right:
        reasons.append("currency_unknown")
    elif str(currency_left).upper() != str(currency_right).upper():
        reasons.append("currency_unconverted")

    if reasons:
        return {"status": "unknown", "reasons": reasons}

    differences: list[str] = []
    unknown: list[str] = []
    if left.get("rooms") is not None and right.get("rooms") is not None \
            and left.get("rooms") != right.get("rooms"):
        differences.append("rooms")
    meal_left, meal_right = left.get("meal_plan"), right.get("meal_plan")
    if meal_left in (None, "unknown") or meal_right in (None, "unknown"):
        unknown.append("meal")
    elif meal_left != meal_right:
        differences.append("meal")

    cancellation_left, cancellation_right = left.get("cancellation"), right.get("cancellation")
    if cancellation_left in (None, "unknown") or cancellation_right in (None, "unknown"):
        unknown.append("cancellation")
    elif cancellation_left != cancellation_right:
        differences.append("cancellation")

    tax_left, tax_right = left.get("tax_status"), right.get("tax_status")
    if tax_left in (None, "unknown") or tax_right in (None, "unknown"):
        unknown.append("taxes")
    elif tax_left != tax_right:
        differences.append("taxes")

    room_left, room_right = left.get("room_type"), right.get("room_type")
    if not room_left or not room_right:
        unknown.append("room")
    elif normalize_hotel_name(room_left) != normalize_hotel_name(room_right):
        differences.append("room")

    if differences:
        return {"status": "partially_comparable",
                "reasons": differences + [f"unknown_{item}" for item in unknown]}
    if unknown:
        return {"status": "unknown", "reasons": [f"unknown_{item}" for item in unknown]}
    return {"status": "comparable", "reasons": []}

def _breakfast_facts(value: Any = None, labels: Any = None, text: Any = None,
                     meal_plan: str | None = None) -> dict[str, Any]:
    """Keep availability and inclusion separate when a provider exposes it."""
    evidence = " ".join(str(part).strip() for part in (value, text, *(labels or ())) if part)
    evidence = re.sub(r"[_-]+", " ", evidence).casefold()
    mentions = bool(re.search(r"breakfast|завтрак", evidence, re.I))
    paid = bool(re.search(
        r"additional|extra|paid|charge|surcharge|optional|доплат|платн|по желанию|по запросу",
        evidence, re.I,
    ))
    included = _has_included_breakfast(evidence)
    if included or meal_plan == "breakfast":
        return {"breakfast_available": True, "breakfast_included": True,
                "breakfast_price": None}
    if mentions:
        return {"breakfast_available": True,
                "breakfast_included": False if paid else None,
                "breakfast_price": None}
    return {"breakfast_available": None, "breakfast_included": None, "breakfast_price": None}

_AMENITY_PATTERNS = {
    "pool": (r"\bpool\b", r"бассейн", r"swimming"),
    "wifi": (r"wi[\s-]?fi", r"беспроводн", r"интернет"),
    "parking": (r"parking", r"парковк"),
    "air_conditioning": (r"air\s*condition", r"кондиционер"),
    "spa": (r"\bspa\b", r"спа"),
    "fitness": (r"fitness", r"gym", r"фитнес", r"тренажер"),
    "restaurant": (r"restaurant", r"ресторан"),
    "breakfast": (r"breakfast", r"завтрак"),
    "elevator": (r"elevator", r"лифт"),
    "laundry": (r"laundry", r"прачеч"),
    "pets": (r"pets", r"животн"),
    "kitchen": (r"kitchen", r"кухн", r"готов[^;]{0,14}еды"),
    "heating": (r"heating", r"отоплен"),
    "luggage": (r"luggage", r"багаж"),
    "terrace": (r"terrace", r"террас", r"смотровая площадк"),
    "balcony": (r"balcony", r"балкон"),
    "refrigerator": (r"refrigerator", r"fridge", r"холодильник"),
    "television": (r"television", r"\btv\b", r"телевизор"),
    "shuttle": (r"shuttle", r"трансфер"),
    "sauna": (r"sauna", r"саун"),
    "seaside": (r"sea view", r"вид на море", r"первая линия"),
}

def extract_amenities(item: dict[str, Any]) -> list[str]:
    """Extract only explicitly supplied facility names from provider payloads."""
    raw_values: list[str] = []
    for key in ("amenities", "facilities", "hotel_facilities", "features", "services"):
        value = item.get(key)
        if isinstance(value, dict):
            raw_values.extend(str(name) for name, enabled in value.items() if enabled)
        elif isinstance(value, list):
            raw_values.extend(str(entry.get("name") if isinstance(entry, dict) else entry) for entry in value)
        elif isinstance(value, str):
            raw_values.append(value)
    evidence = " ".join(raw_values)
    return [amenity for amenity, patterns in _AMENITY_PATTERNS.items()
            if any(re.search(pattern, evidence, re.I) for pattern in patterns)]

def _normalize_city_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)

_CITY_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "o",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
})

def _city_label_forms(value: str) -> set[str]:
    raw = unicodedata.normalize("NFC", value.casefold())
    variants = {raw}
    if "ё" in raw:
        variants.update(raw.replace("ё", replacement) for replacement in ("e", "o", "yo"))
    normalized = {_normalize_city_label(variant) for variant in variants}
    return normalized | {form.translate(_CITY_CYRILLIC_TO_LATIN) for form in normalized}

def _city_label_distance(left: str, right: str) -> int:
    left_forms = _city_label_forms(left)
    right_forms = _city_label_forms(right)
    return min(_edit_distance(a, b) for a in left_forms for b in right_forms)

def _edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    if len(left) - len(right) > 1:
        return len(left)
    previous = list(range(len(right) + 1))
    for index, left_character in enumerate(left, start=1):
        current = [index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_character != right_character),
            ))
        previous = current
    return previous[-1]

def _normalize_coordinate_pair(value: Any) -> dict[str, float] | None:
    """Accept {lat,lon}/{lat,lng}/{latitude,longitude} provider shapes.

    SravniOtel list results carry the point under item["location"] while
    other providers use item["coordinates"]; without this fallback 394/400
    live cards resolved to no coordinates (empty map, no metro/center).
    """
    if not isinstance(value, dict):
        return None
    lat = value.get("lat", value.get("latitude"))
    lon = value.get("lon") if value.get("lon") is not None else (value.get("lng") if value.get("lng") is not None else value.get("longitude"))
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return {"lat": lat_f, "lng": lon_f}


def normalize_hotel(item: dict[str, Any], observed_at: str | None = None,
                    source: str = "sravniotel", query: dict[str, Any] | None = None) -> dict[str, Any] | None:
    hotel_id = item.get("id")
    name = " ".join(str(item.get("name") or "").split())
    if hotel_id is None or not name:
        return None
    images = []
    for image in item.get("images", []) if isinstance(item.get("images"), list) else []:
        if isinstance(image, str):
            candidate = {"url": image}
        elif isinstance(image, dict):
            candidate = {key: value for key, value in image.items()
                         if key in {"url", "x240", "x500", "x1024", "alt", "title", "role", "source"}}
        else:
            continue
        image_text = " ".join(str(candidate.get(key) or "") for key in ("alt", "title", "role")).casefold()
        image_url = " ".join(str(candidate.get(key) or "") for key in ("url", "x500", "x1024")).casefold()
        if re.search(r"food|dish|menu|restaurant|meal|logo|icon|qr|advert|карта|блюд|меню|еда|ресторан", image_text):
            continue
        if re.search(r"food|dish|menu|restaurant|meal|logo|icon|qr|advert|карта|блюд|меню|еда", image_url):
            continue
        if candidate.get("url") or candidate.get("x500") or candidate.get("x1024"):
            candidate.setdefault("source", source)
            images.append(candidate)
    offers = []
    for offer in item.get("offers", []) if isinstance(item.get("offers"), list) else []:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price") if isinstance(offer.get("price"), dict) else {}
        raw_value = price.get("value")
        amount = None
        try:
            if raw_value is not None:
                decimal_amount = Decimal(str(raw_value)) / Decimal(100)
                if decimal_amount.is_finite() and decimal_amount > 0:
                    amount = str(decimal_amount)
        except (InvalidOperation, ValueError):
            pass
        labels = offer.get("labels") if isinstance(offer.get("labels"), list) else []
        labels = [str(label) for label in labels if isinstance(label, (str, int, float))]
        meal_facts = _offer_meal_facts(offer, source)
        offers.append({
            "source": source, "provider": offer.get("provider"),
            "provider_hotel_id": str(hotel_id), "canonical_hotel_id": None,
            "amount": amount,
            "original_amount": amount, "original_currency": price.get("currency"),
            "source_price_value": raw_value if type(raw_value) is int else None,
            "source_price_divisor": 100 if type(raw_value) is int else None,
            "normalized_amount": None, "normalized_currency": None,
            "fx_rate": None, "fx_provider": None, "fx_timestamp": None,
            "currency": price.get("currency"), "deeplink": offer.get("deeplink"),
            "check_in": (query or {}).get("check_in"), "check_out": (query or {}).get("check_out"),
            "adults": (query or {}).get("adults"), "children_count": (query or {}).get("children_count", 0),
            "rooms": (query or {}).get("rooms"), "room_type": offer.get("room_type"),
            "bed_type": offer.get("bed_type"),
            "room_privacy": classify_room_privacy(offer.get("room_type"), offer.get("bed_type")),
            "occupancy": offer.get("occupancy") or ({"adults": (query or {}).get("adults"),
                                                        "children": (query or {}).get("children_count", 0)}
                                                       if query and query.get("adults") else None),
            **meal_facts,
            "payment_method": offer.get("payment_method") or ("pay_at_hotel" if "pay_at_hotel" in labels else "unknown"),
            "taxes": offer.get("taxes"), "fees": offer.get("fees"), "valid_until": offer.get("valid_until"),
            "comparability": "unknown",
            "labels": labels,
            "tax_status": "included" if "taxes_included" in labels else "unknown",
            "availability": "unknown",
            "cancellation": "free" if "free_cancellation" in labels else
                "non_refundable" if "non_refundable" in labels else "unknown",
            "raw_payload": offer,
        })
    try:
        nights = (date.fromisoformat(str(query["check_out"])) -
                  date.fromisoformat(str(query["check_in"]))).days if query else 0
    except (KeyError, TypeError, ValueError):
        nights = 0
    if nights > 0:
        for offer in offers:
            try:
                total = Decimal(str(offer.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not total.is_finite() or total <= 0 or offer.get("taxes") or offer.get("fees"):
                continue
            # These offers were returned for the exact booking dates in the
            # query. Preserve the stay quote and expose its average nightly
            # cost only when separate taxes/fees do not make the basis unclear.
            offer["total_price"] = str(total)
            offer["nights"] = nights
            offer["price_basis"] = "search_context_stay_total"
            offer["nightly_price"] = str((total / Decimal(nights)).quantize(Decimal("0.01")))
    raw_rating = item.get("rating")
    if (not isinstance(raw_rating, (int, float)) or isinstance(raw_rating, bool)
            or not 0 < raw_rating <= 10):
        raw_rating = None
    rating_fields = _normalize_rating_fields(raw_rating, 10 if raw_rating is not None else None, source)
    reviews = item.get("reviews_count")
    stars = _normalize_hotel_stars(item.get("stars"))
    hotel_breakfast = _hotel_breakfast_facts(item, source)
    source_beach_line_raw = item.get("beach_line") or item.get("beachLine") or item.get("beachfront")
    return {
        "source": source, "source_hotel_id": str(hotel_id), "name": name,
        "normalized_name": normalize_hotel_name(name),
        "address": item.get("address"),
        "stars": stars if isinstance(stars, (int, float)) and not isinstance(stars, bool) else None,
        "rating": rating_fields["rating_original"],
        **rating_fields,
        "reviews_count": reviews if isinstance(reviews, int) and not isinstance(reviews, bool) and reviews >= 0 else None,
        "images": images, "nearby_places": item.get("nearby_places") or [],
        # 2026-10-16: list-API offer tags (scarcity, free cancellation,
        # late checkout) travel with the hotel; the card renders them as
        # badges without any detail-page visit.
        "offer_tags": [str(t) for t in item.get("offer_tags") or []
                       if isinstance(t, str) and t.strip()][:6] or None,
        "amenities": extract_amenities(item),
        **hotel_breakfast,
        "offers": offers, "observed_at": observed_at or utc_now(), "canonical_hotel_id": None,
        "coordinates": _normalize_coordinate_pair(item.get("coordinates")) or _normalize_coordinate_pair(item.get("location")),
        "phone": item.get("phone"),
        "website": item.get("website"), "chain": item.get("chain"),
        "city": (query or {}).get("city_name"), "country": item.get("country"),
        "beach_access": item.get("beach_access") or item.get("beachAccess") or "unknown",
        "beach_line": normalize_beach_line(source_beach_line_raw),
        "beach_line_source": "source" if normalize_beach_line(source_beach_line_raw) != "unknown" else None,
        "beach_line_evidence": str(source_beach_line_raw)[:160] if source_beach_line_raw else None,
        "raw_payload": item,
        "review_quotes": item.get("review_quotes") or [],
        "review_source": item.get("review_source"),
        "source_hotel_url": _normalize_source_hotel_url(item.get("canonical_url")),
    }


def _normalize_source_hotel_url(value: Any) -> str | None:
    """Absolutize the source hotel page path (reviews live on that page).

    SravniOtel returns canonical_url as a site-relative path like
    /vietnam/cities/da-nang/7-seven-sea-hotel. The absolute page on the
    source site carries the hotel reviews; a synthetic URL is never built
    when the source provides none.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("/"):
        return f"https://hotels.travel.ru{text}"
    return None

def _normalize_hotel_stars(value: Any) -> float | None:
    """Return a valid provider star classification, or unknown for sentinels."""
    if isinstance(value, bool):
        return None
    try:
        stars = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return stars if math.isfinite(stars) and 0 < stars <= 5 else None

def _normalize_rating_fields(value: Any, scale: Any, source: str | None) -> dict[str, Any]:
    """Preserve a source rating and expose a comparable 10-point value."""
    fields = {"rating_original": None, "rating_scale": None,
              "rating_normalized": None, "rating_source": None}
    if isinstance(value, bool) or scale not in (5, 10):
        return fields
    try:
        rating = float(value)
    except (TypeError, ValueError, OverflowError):
        return fields
    if not math.isfinite(rating) or not 0 < rating <= scale:
        return fields
    fields.update({"rating_original": rating, "rating_scale": int(scale),
                   "rating_normalized": rating * 10 / int(scale),
                   "rating_source": source})
    return fields

def normalize_beach_line(value: Any) -> str:
    """Keep source line evidence explicit; never infer a line from vague beach access."""
    text = " ".join(str(value or "").casefold().replace("ё", "е").split())
    if not text:
        return "unknown"
    if re.fullmatch(r"(?:first|1st|1|первая|1-я)(?:\s+(?:beach\s+)?line|\s+линия)?|beachfront|прямо\s+на\s+пляже", text):
        return "first"
    if re.fullmatch(r"(?:second|2nd|2|вторая|2-я)(?:\s+(?:beach\s+)?line|\s+линия)?", text):
        return "second"
    if re.fullmatch(r"(?:third|3rd|3|третья|3-я)(?:\s+(?:beach\s+)?line|\s+линия)?", text):
        return "third"
    if re.search(r"(?:первая|1-я|first|1st)\s+линия|(?:вторая|2-я|second|2nd)\s+линия|(?:третья|3-я|third|3rd)\s+линия", text):
        if re.search(r"(?:первая|1-я|first|1st)\s+линия", text):
            return "first"
        if re.search(r"(?:вторая|2-я|second|2nd)\s+линия", text):
            return "second"
        return "third"
    return "unknown"
