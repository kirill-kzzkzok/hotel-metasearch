"""Existing Trip.com detail and rate inventory flow, relocated without selector or retry changes."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit

from ..geo.core import _coordinate_pair
from ..models import utc_now
from ..normalization import _normalize_rating_fields, _offer_meal_facts
from ..runtime import log_search_timeline


TRIPCOM_RATE_BODY_TIMEOUT_SECONDS = 12.0
TRIPCOM_RATE_ENDPOINT_PATH = "/restapi/soa2/33269/getHotelRoomListOversea"
TRIPCOM_ROUTE_CALLBACK_IDLE_TIMEOUT_SECONDS = 2.0
TRIPCOM_ROUTE_CALLBACK_DRAIN_TIMEOUT_SECONDS = 1.0


async def _read_rate_response_body(response: Any, *, timeout_seconds: float | None = None) -> bytes:
    """Read a Trip rate response without allowing a partial stream to hang forever."""
    timeout = TRIPCOM_RATE_BODY_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    return await asyncio.wait_for(response.body(), timeout=timeout)


async def _capture_rate_response_body(response: Any) -> tuple[bytes, str | None, str | None]:
    """Read a response body with a deadline, preserving read failures for diagnostics."""
    try:
        return await _read_rate_response_body(response), None, None
    except asyncio.TimeoutError:
        return b"", None, (
            f"Trip.com rate response body exceeded {TRIPCOM_RATE_BODY_TIMEOUT_SECONDS:g}s "
            "deadline after HTTP 200 headers"
        )
    except Exception as exc:
        return b"", f"{type(exc).__name__}: {exc}", None


def _start_rate_response_body_capture(response: Any) -> dict[str, Any]:
    """Start consuming a response body immediately from its Playwright event."""
    return {
        "body_wait_started": time.perf_counter(),
        "body_wait_started_utc": _utc_timestamp(),
        "body_task": asyncio.create_task(_capture_rate_response_body(response)),
    }


def normalize_tripcom_rate_inventory(payload: Any, *, source_hotel_id: str,
                                    check_in: str, check_out: str, adults: int,
                                    children_count: int = 0,
                                    room_details: dict[str, dict[str, Any]] | None = None,
                                    observed_at: str | None = None) -> dict[str, Any]:
    """Convert only public, user-facing Trip.com room/rate fields to safe observations.

    The original provider payload is deliberately not retained. This function
    accepts only the rate inventory response, and copies a small field allowlist
    so opaque request/session metadata cannot reach storage or API responses.
    """
    booking_context = {"check_in": check_in, "check_out": check_out, "adults": adults,
                       "children_count": children_count}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"source_hotel_id": str(source_hotel_id), **booking_context, "rates": [], "room_groups": [],
                "status": "invalid_response"}
    if "saleRoomMap" not in data:
        # Trip.com's anti-bot marks a request as spider traffic with
        # data.htlSpiderActionErrorCode (observed: 4030) while still
        # returning HTTP 200. Without this branch a protection response
        # would be classified as a benign absent inventory and terminal
        # "success-family" status, hiding the real cause from the user.
        spider_code = data.get("htlSpiderActionErrorCode")
        if spider_code is not None:
            return {"source_hotel_id": str(source_hotel_id), **booking_context, "rates": [], "room_groups": [],
                    "status": "blocked", "spider_action_error_code": spider_code}
        return {"source_hotel_id": str(source_hotel_id), **booking_context, "rates": [], "room_groups": [],
                "status": "rate_inventory_absent"}
    rate_map = data.get("saleRoomMap")
    if isinstance(rate_map, dict):
        raw_rates = list(rate_map.items())
    elif isinstance(rate_map, list):
        raw_rates = [(str(item.get("id")), item) for item in rate_map if isinstance(item, dict)]
    else:
        raw_rates = []

    observed_timestamp = observed_at or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    room_groups: list[dict[str, Any]] = []
    rate_to_group: dict[str, int] = {}
    room_list = data.get("roomList")
    if isinstance(room_list, list):
        # 2026-10-17: saleRoomMap keys are '<sRoomId>_<roomToken>' (or a
        # full skey), while subRoomList carries the bare sRoomId/skey.
        # Matching only the bare id missed EVERY rate (0/22, 0/20 live) -
        # room_group_index was None for all. Match the full skey first,
        # then the '<sRoomId>_' prefix, then the bare id.
        for index, room in enumerate(room_list):
            if not isinstance(room, dict):
                continue
            group = {"group_index": index, "rate_ids": []}
            subrooms = room.get("subRoomList")
            if isinstance(subrooms, list):
                for subroom in subrooms:
                    if not isinstance(subroom, dict):
                        continue
                    sub_keys = []
                    for field in ("skey", "key", "sRoomId"):
                        value = subroom.get(field)
                        if isinstance(value, (str, int)) and not isinstance(value, bool):
                            sub_keys.append(str(value))
                    for rate_id in dict.fromkeys(sub_keys):
                        group["rate_ids"].append(rate_id)
                        rate_to_group[rate_id] = index
            room_groups.append(group)

    def text_items(value: Any, field: str) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                result.append(entry.strip())
            elif isinstance(entry, dict):
                text = entry.get(field)
                if isinstance(text, str) and text.strip():
                    result.append(text.strip())
        return list(dict.fromkeys(result))

    rates: list[dict[str, Any]] = []
    physical_rooms = data.get("physicRoomMap") if isinstance(data.get("physicRoomMap"), dict) else {}
    for map_id, rate in raw_rates:
        if not isinstance(rate, dict):
            continue
        # Keep map-key variants separate even when Trip.com repeats an inner
        # record id across room/rate contexts.
        rate_id = str(map_id).strip() if map_id is not None else ""
        if not rate_id or len(rate_id) > 256 or any(ord(character) < 32 for character in rate_id):
            continue
        # Resolve the saleRoomMap key ('<sRoomId>_<token>' or full skey) to
        # its room group: exact skey match, else '<sRoomId>_' prefix, else
        # the bare id. Without this every rate lands in no group.
        group_index = rate_to_group.get(rate_id)
        if group_index is None:
            for sub_key, idx in rate_to_group.items():
                if rate_id.startswith(f"{sub_key}_") or sub_key.startswith(f"{rate_id}_"):
                    group_index = idx
                    break
        meal_info = rate.get("mealInfo") if isinstance(rate.get("mealInfo"), dict) else {}
        meal_title = meal_info.get("title") if isinstance(meal_info.get("title"), str) else None
        categories = rate.get("saleRoomCategoryList")
        category_labels = [entry.get("title", "").strip() for entry in categories
                           if isinstance(entry, dict) and isinstance(entry.get("title"), str)
                           and entry.get("title", "").strip()] if isinstance(categories, list) else []
        category_meals = [entry.get("title", "").strip() for entry in categories
                          if isinstance(entry, dict) and entry.get("type") == "mealInfo"
                          and isinstance(entry.get("title"), str) and entry.get("title", "").strip()] \
            if isinstance(categories, list) else []
        category_by_type = {entry.get("type"): entry.get("title", "").strip()
                            for entry in categories if isinstance(entry, dict)
                            and isinstance(entry.get("title"), str)} if isinstance(categories, list) else {}
        raw_meal = meal_title or (category_meals[0] if category_meals else None)
        meal_offer = {"meal": raw_meal} if raw_meal else {}
        meal_facts = _offer_meal_facts(meal_offer, "trip_com_rate_inventory")
        price_info = rate.get("priceInfo") if isinstance(rate.get("priceInfo"), dict) else {}
        price_value = price_info.get("price")
        price_amount = None
        if isinstance(price_value, (int, float)) and not isinstance(price_value, bool) \
                and math.isfinite(float(price_value)) and price_value > 0:
            price_amount = str(price_value)
        cancel_info = rate.get("cancelInfo") if isinstance(rate.get("cancelInfo"), dict) else {}
        cancel_title = cancel_info.get("title") if isinstance(cancel_info.get("title"), str) else None
        cancel_title = cancel_title.strip() if cancel_title else None
        cancellation = "free" if cancel_title and re.search(r"free cancellation|бесплатн\w* отмен", cancel_title, re.I) \
            else "non_refundable" if cancel_title and re.search(r"non.?refundable|не подлежит возврату", cancel_title, re.I) \
            else "unknown"
        total_info = rate.get("totalPriceInfo") if isinstance(rate.get("totalPriceInfo"), dict) else {}
        charges: list[dict[str, str]] = []
        for charge_key, charge in total_info.items():
            if charge_key not in {"total", "payHotelTax"} or not isinstance(charge, dict):
                continue
            allowed_charge = {field: charge[field] for field in ("title", "description", "price", "localPrice")
                              if isinstance(charge.get(field), str) and charge[field].strip()}
            if allowed_charge:
                charges.append(allowed_charge)
        booking_status = rate.get("bookingStatusInfo") if isinstance(rate.get("bookingStatusInfo"), dict) else {}
        booking_remaining = booking_status.get("remainRoomQuantity")
        physical_room_id = str(rate.get("physicalRoomId") or "")
        room_detail = (room_details or {}).get(physical_room_id) or {}
        physical_room = physical_rooms.get(physical_room_id)
        # 2026-10-16: the rate payload itself carries rich per-room facilities
        # (physicRoomMap[room].physicalFacilityList / faciltityInfo.list
        # sub-titles / wifiInfo) - Wi-Fi, air conditioning, view, kettle.
        # Extract them without opening any detail page.
        physical_room_amenities: list[str] | None = None
        physical_room_area: str | None = None
        physical_room_bed: str | None = None
        if isinstance(physical_room, dict):
            # Room area ("12 м²") as its own field; the generic amenity
            # filter strips it from facility titles, losing the fact.
            area_title = (physical_room.get("areaInfo") or {}).get("title")                 if isinstance(physical_room.get("areaInfo"), dict) else None
            if isinstance(area_title, str) and re.match(r"^\d+", area_title.strip()):
                physical_room_area = area_title.strip()[:24]
            # Bed type from the rate payload (physicRoomMap.bedInfo.title /
            # bedDetail.detail) - the list XHR bedInfo also exists, this
            # covers rate-only hotels.
            bed_title = (physical_room.get("bedInfo") or {}).get("title")                 if isinstance(physical_room.get("bedInfo"), dict) else None
            physical_room_bed: str | None = bed_title.strip()[:120]                 if isinstance(bed_title, str) and bed_title.strip() else None
            facility_titles: list[str] = []
            for facility in (physical_room.get("physicalFacilityList")
                             or physical_room.get("newPhysicalFacilityList") or []):
                if isinstance(facility, dict) and isinstance(facility.get("title"), str)                         and facility["title"].strip():
                    facility_titles.append(facility["title"].strip())
            wifi_info = physical_room.get("wifiInfo")
            if isinstance(wifi_info, dict) and isinstance(wifi_info.get("title"), str)                     and wifi_info["title"].strip():
                facility_titles.append(wifi_info["title"].strip())
            facility_info = physical_room.get("faciltityInfo")
            if isinstance(facility_info, dict):
                for category in (facility_info.get("list") or []):
                    if not isinstance(category, dict):
                        continue
                    for sub in (category.get("subList") or []):
                        if isinstance(sub, dict) and isinstance(sub.get("title"), str)                                 and sub["title"].strip():
                            facility_titles.append(sub["title"].strip())
            facility_titles = [t for t in dict.fromkeys(facility_titles) if not re.match(r"^\d+\s*(?:м²|m²|sqm| кв)", t)]
            physical_room_amenities = facility_titles[:24] or None
        # 2026-10-16: per-room photos ride in the same rate payload
        # (physicRoomMap[room].pictureInfo[].url) - previously the room image
        # gallery was unreachable without opening detail pages.
        physical_room_images: list[str] = []
        if isinstance(physical_room, dict):
            for picture in (physical_room.get("pictureInfo") or []):
                if isinstance(picture, dict) and isinstance(picture.get("url"), str)                         and picture["url"].startswith(("http://", "https://")):
                    physical_room_images.append(picture["url"][:512])
            physical_room_images = list(dict.fromkeys(physical_room_images))[:12]
        physical_room_name = physical_room.get("name") if isinstance(physical_room, dict) else None
        physical_room_name = physical_room_name.strip() if isinstance(physical_room_name, str) else None
        rates.append({
            "provider": "trip.com", "source_hotel_id": str(source_hotel_id),
            "provider_rate_id": rate_id,
            "provider_rate_record_id": str(rate.get("id")) if rate.get("id") is not None else None,
            "provider_room_id": str(rate.get("physicalRoomId"))
                if str(rate.get("physicalRoomId") or "").isdigit() else None,
            "room_group_index": group_index,
            "check_in": check_in, "check_out": check_out, "adults": adults,
            "children_count": children_count,
            "room_type": room_detail.get("room_type") or physical_room_name or next(
                (str(rate[key]).strip() for key in ("roomName", "roomTitle", "roomType")
                 if isinstance(rate.get(key), str) and rate[key].strip()), None),
            "bed_type": room_detail.get("bed_type"),
            "room_amenities": room_detail.get("room_amenities") or
                physical_room_amenities or
                text_items(rate.get("roomFacilityList"), "title") or None,
            "room_images": physical_room_images or None,
            "room_area": physical_room_area,
            "bed_type": room_detail.get("bed_type") or physical_room_bed,
            # 2026-10-16: scarcity and discount facts from the same rate
            # payload - remainRoomQuantity (осталось N номеров) and the
            # strikethrough pre-discount price.
            "rooms_left": booking_remaining if isinstance(booking_remaining, int) and 0 < booking_remaining < 100 else None,
            "original_price_display": price_info.get("deletePrice") if isinstance(price_info.get("deletePrice"), str) else None,
            "meal_raw_text": raw_meal,
            **meal_facts,
            "meal_source": f"trip_com_rate_inventory:{'mealInfo.title' if meal_title else 'saleRoomCategoryList.mealInfo'}"
                if raw_meal else None,
            "cancellation": cancellation, "cancellation_raw_text": cancel_title,
            "price_amount": price_amount,
            "price_display_text": price_info.get("displayPrice") if isinstance(price_info.get("displayPrice"), str) else None,
            "currency": price_info.get("currency") if isinstance(price_info.get("currency"), str) else None,
            "taxes_fees_raw_text": price_info.get("taxText") if isinstance(price_info.get("taxText"), str) else None,
            "price_explanation": price_info.get("priceExplanation")
                if isinstance(price_info.get("priceExplanation"), str) else None,
            "additional_charge_evidence": charges,
            "rate_labels": category_labels,
            "payment_terms_raw_text": category_by_type.get("paymentInfo"),
            "confirmation_raw_text": category_by_type.get("confirmInfo"),
            "availability_raw_text": " | ".join(text_items(rate.get("roomLabelList"), "text")) or None,
            "discount_labels": text_items(rate.get("priceLabelList"), "text"),
            "rate_descriptions": text_items(rate.get("roomDescList"), "text"),
            "rate_availability_labels": text_items(rate.get("roomLabelList"), "text"),
            "price_basis": "trip_com_priceInfo.price; UI cross-check pending",
            "observed_at": observed_timestamp,
        })
    return {"source_hotel_id": str(source_hotel_id), **booking_context,
            "status": "ok" if rates else "no_rates",
            "rates": rates, "room_groups": room_groups}


def _classify_tripcom_rate_response(
    response_status: int | None,
    payload: Any,
    *,
    body_wait_error: str | None,
    response_body_timeout: str | None = None,
    source_hotel_id: str,
    check_in: str,
    check_out: str,
    adults: int,
    children_count: int,
    room_details: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any] | None, str | None, str | None]:
    """Classify the rate API result independently of page-body readiness.

    The page body is only used to detect a challenge. A locator timeout must
    not discard a captured rate response, but remains visible when no response
    was captured.
    """
    if response_status is None:
        if body_wait_error:
            return "body_timeout", None, "body_wait_timeout", body_wait_error
        return "rate_inventory_timeout", None, "rate_response_timeout", \
            "No room-rate response arrived before the bounded wait ended"
    if response_status in {401, 403, 429, 430}:
        return "blocked", None, "robots_or_provider_protection", \
            f"Provider protection response with HTTP {response_status}"
    if response_status != 200:
        return "http_error", None, "http_error", f"Rate endpoint returned HTTP {response_status}"
    if response_body_timeout:
        return "body_timeout", None, "rate_response_body_timeout", response_body_timeout
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return "invalid_response", None, "invalid_response", \
            "Rate response did not contain a valid data object"
    inventory = normalize_tripcom_rate_inventory(
        payload, source_hotel_id=source_hotel_id, check_in=check_in,
        check_out=check_out, adults=adults, children_count=children_count,
        room_details=room_details,
    )
    return inventory.get("status", "invalid_response"), inventory, None, None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def normalize_tripcom_property_details(payload: Any, *, observed_at: str | None = None) -> dict[str, Any]:
    """Validate user-visible hotel detail facts without mixing them into rates.

    Distances are retained as Trip.com landmark observations. They are not
    treated as our calculated center/beach distance, and raw page text is not
    retained wholesale.
    """
    if not isinstance(payload, dict):
        payload = {}
    def clean_text(value: Any, limit: int = 240) -> str | None:
        if not isinstance(value, str):
            return None
        value = re.sub(r"\s+", " ", value).strip()
        return value[:limit] or None

    details: dict[str, Any] = {
        "source": "trip_com_hotel_detail_dom",
        "observed_at": observed_at or utc_now(),
        "name": clean_text(payload.get("name"), 200),
        "description": clean_text(payload.get("description"), 2000),
        "category": clean_text(payload.get("category"), 100),
        "address": clean_text(payload.get("address"), 500),
        "coordinates": _coordinate_pair(payload.get("coordinates")),
        "check_in_text": clean_text(payload.get("check_in_text"), 160),
        "check_out_text": clean_text(payload.get("check_out_text"), 160),
        "reception_hours": clean_text(payload.get("reception_hours"), 160),
        "breakfast": None,
        "amenities": [],
        "images": [],
        "nearby_landmarks": [],
    }
    raw_stars = payload.get("stars")
    try:
        stars = float(raw_stars)
        details["stars"] = stars if math.isfinite(stars) and 0 < stars <= 5 else None
    except (TypeError, ValueError):
        details["stars"] = None
    raw_scale = payload.get("rating_scale")
    rating_scale = int(raw_scale) if isinstance(raw_scale, (int, float)) \
        and not isinstance(raw_scale, bool) and raw_scale in (5, 10) else 10
    rating_fields = _normalize_rating_fields(payload.get("rating"), rating_scale, "trip_com")
    details["rating"] = rating_fields["rating_original"]
    details.update(rating_fields)
    raw_reviews = payload.get("reviews_count")
    try:
        reviews_count = int(raw_reviews)
        details["reviews_count"] = reviews_count if reviews_count >= 0 else None
    except (TypeError, ValueError):
        details["reviews_count"] = None
    raw_review_url = payload.get("review_url")
    if isinstance(raw_review_url, str):
        parsed_review_url = urlsplit(raw_review_url)
        host = (parsed_review_url.hostname or "").casefold()
        details["review_url"] = raw_review_url if parsed_review_url.scheme == "https" and (
            host == "trip.com" or host.endswith(".trip.com")) else None
    else:
        details["review_url"] = None
    images = payload.get("images")
    if isinstance(images, list):
        details["images"] = list(dict.fromkeys(
            value.strip() for value in images if isinstance(value, str)
            and value.strip().startswith("https://")
        ))[:12]
    breakfast_text = clean_text(payload.get("breakfast_text"), 1200)
    if breakfast_text:
        lower_breakfast = breakfast_text.casefold()
        conditional = bool(re.search(
            r"(?:ask|contact|check|confirm)[^.;]{0,100}(?:if|whether)[^.;]{0,60}breakfast|"
            r"свяжитесь[^.;]{0,100}(?:узнать|уточнить)[^.;]{0,60}завтрак|"
            r"узнать[^.;]{0,60}доступен ли[^.;]{0,30}завтрак",
            lower_breakfast, re.I,
        ))
        unavailable = bool(re.search(
            r"(?:no breakfast|breakfast (?:is )?not available|breakfast not provided|does not offer breakfast|"
            r"завтрак не предоставляется|завтрака нет|без завтрака)", lower_breakfast, re.I,
        ))
        affirmative = bool(re.search(
            r"breakfast (?:is )?available|breakfast buffet|breakfast served|"
            r"шведский стол|завтрак предоставляется|завтрак доступен|часы работы",
            lower_breakfast, re.I,
        ))
        definite_service = bool(re.search(
            r"breakfast buffet|breakfast served|шведский стол|завтрак предоставляется|часы работы",
            lower_breakfast, re.I,
        ))
        available = False if unavailable else None if conditional and not definite_service \
            else True if affirmative else None
        details["breakfast"] = {"available": available, "raw_text": breakfast_text}
    amenities = payload.get("amenities")
    if isinstance(amenities, list):
        details["amenities"] = list(dict.fromkeys(
            text for value in amenities if (text := clean_text(value))
        ))[:80]
    landmarks = payload.get("nearby_landmarks")
    if isinstance(landmarks, list):
        for value in landmarks[:80]:
            if not isinstance(value, dict):
                continue
            name = clean_text(value.get("name"))
            distance_text = clean_text(value.get("distance_text"), 64)
            category = clean_text(value.get("category"), 80)
            try:
                distance_meters = float(value["distance_meters"])
            except (KeyError, TypeError, ValueError):
                distance_meters = None
            if distance_meters is not None and (not math.isfinite(distance_meters) or distance_meters < 0):
                distance_meters = None
            if name:
                details["nearby_landmarks"].append({
                    "name": name, "category": category,
                    "distance_text": distance_text,
                    "distance_meters": distance_meters,
                    "source": "trip_com_displayed_landmark_distance",
                })
    return details

async def _tripcom_extract_property_details(page: Any) -> dict[str, Any]:
    """Extract public detail-page facts even when the separate rate API is blocked."""
    try:
        payload = await page.evaluate(r"""() => {
          const text = e => (e?.innerText || '').replace(/\s+/g, ' ').trim();
          const policy = text(document.querySelector('#tripHotelPolicy'));
          const match = (source, re) => source.match(re)?.[0] || null;
          const isHotel = item => item && typeof item === 'object' &&
            /hotel|hostel|lodging|resort/i.test([].concat(item['@type'] || []).join(' '));
          const stack = [...document.querySelectorAll('script[type="application/ld+json"]')]
            .map(node => { try { return JSON.parse(node.textContent || 'null'); } catch { return null; } });
          let hotel = null;
          while (stack.length && !hotel) {
            const item = stack.pop();
            if (Array.isArray(item)) { stack.push(...item); continue; }
            if (!item || typeof item !== 'object') continue;
            stack.push(...[item['@graph'], item.mainEntity].filter(Boolean));
            if (isHotel(item)) hotel = item;
          }
          const aggregate = hotel?.aggregateRating || {};
          const rawStars = hotel?.starRating?.ratingValue ?? hotel?.starRating ?? null;
          const rating = Number(aggregate.ratingValue);
          const stars = Number(typeof rawStars === 'object' ? rawStars?.ratingValue : rawStars);
          const reviewCount = Number(aggregate.reviewCount ?? aggregate.ratingCount);
          const coordinates = hotel?.geo ? {lat: Number(hotel.geo.latitude), lng: Number(hotel.geo.longitude)} : null;
          const rawImages = [].concat(hotel?.image || []).map(image => typeof image === 'string' ? image : image?.url)
            .filter(url => typeof url === 'string' && url.startsWith('https://')).slice(0, 12);
          const reviewLink = [...document.querySelectorAll('a[href]')].find(a =>
            /reviews?|отзыв/i.test(text(a)) && /review|comment|hotel/i.test(a.href));
          const breakfast = policy.match(/(?:Завтрак|Breakfast)[\s\S]{0,900}?(?=(?:Возрастные требования|Age requirements|$))/i)?.[0] || null;
          const amenities = [...document.querySelectorAll('[id^="fac_"]')]
            .map(e => e.getAttribute('aria-label') || e.getAttribute('title') || text(e)).filter(Boolean);
          const landmarks = [...document.querySelectorAll('[class*="hotelLocationNew_hotelLocation-details_name__"]')]
            .map(e => {
              const name = text(e);
              const places = e.closest('[class*="hotelLocationNew_hotelLocation-item_placesA__"]');
              const category = text(places?.previousElementSibling) || null;
              if (category && /Шопинг|Shopping/i.test(category)) return null;
              const item = e.parentElement;
              const distanceText = [...(item?.querySelectorAll('*') || [])]
                .map(text).find(x => /^(?:<\s*)?\d+(?:[.,]\d+)?\s*(?:m|km|м|км)(?:\s*>)?$/i.test(x)) || null;
              if (!name || !distanceText) return null;
              const d = distanceText.match(/([\d.,]+)\s*(km|км|m|м)/i);
              const n = d ? Number(d[1].replace(',', '.')) : NaN;
              return {name, category, distance_text: distanceText,
                distance_meters: Number.isFinite(n) ? n * (/km|км/i.test(d[2]) ? 1000 : 1) : null};
            }).filter(Boolean).slice(0, 80);
          const address = text(document.querySelector('[class*="hotelOverview_addressDesc"]')) ||
            text(document.querySelector('[class*="hotelOverview_addressText"]')) || null;
          const checkIn = match(policy, /(?:Заезд|Check[- ]?in):?\s*(?:после|from|after)?\s*\d{1,2}:\d{2}(?:\s*(?:–|-|to)\s*\d{1,2}:\d{2})?/i);
          const checkOut = match(policy, /(?:Время выезда|Check[- ]?out):?\s*(?:до|from|after)?\s*\d{1,2}:\d{2}(?:\s*(?:–|-|to)\s*\d{1,2}:\d{2})?/i);
          const receptionHours = match(policy, /(?:Время работы стойки регистрации|Front desk hours):?\s*[^.]{1,80}/i);
          const description = hotel?.description || document.querySelector('meta[name="description"]')?.content || null;
          const amenityFeature = [].concat(hotel?.amenityFeature || [])
            .map(item => typeof item === 'string' ? item : item?.name).filter(Boolean);
          return {name: hotel?.name || text(document.querySelector('h1')),
            description, category: [].concat(hotel?.['@type'] || [])[0] || null,
            stars: Number.isFinite(stars) ? stars : null,
            rating: Number.isFinite(rating) ? rating : null, rating_scale: 10,
            reviews_count: Number.isFinite(reviewCount) ? reviewCount : null,
            images: rawImages, review_url: reviewLink?.href || null, coordinates,
            address, check_in_text: checkIn, check_out_text: checkOut,
            reception_hours: receptionHours, breakfast_text: breakfast,
            amenities: [...new Set([...amenities, ...amenityFeature])].slice(0, 80), nearby_landmarks: landmarks};
        }""")
    except Exception:
        payload = {}
    return normalize_tripcom_property_details(payload)

async def _tripcom_extract_room_details(page: Any) -> dict[str, dict[str, Any]]:
    try:
        await page.locator('[class*="commonRoomCard__"][id]').first.wait_for(
            state="attached", timeout=1500,
        )
        result = await page.evaluate(r"""() => {
          const result = {};
          for (const card of document.querySelectorAll('[class*="commonRoomCard__"][id]')) {
            if (!/^\d+$/.test(card.id)) continue;
            const content = card.querySelector('[class*="commonRoomCard-content__"]');
            if (!content) continue;
            const lines = (content.innerText || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
            if (!lines.length) continue;
            const offset = /^\d+$/.test(lines[0]) ? 1 : 0;
            const name = (lines[offset] || '').trim();
            const more = lines.findIndex(x => /^(?:подробнее о номере|room details|view room details)$/i.test(x));
            const features = lines.slice(offset + 2, more < 0 ? lines.length : more)
              .filter(x => x.length < 180 && !/^\d+\s*(?:м²|m²|sqm)$/i.test(x));
            result[card.id] = {room_type: name || null,
              bed_type: lines[offset + 1] || null, room_amenities: features.slice(0, 24)};
          }
          return result;
        }""")
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}

async def collect_tripcom_room_rates(hotels: list[dict[str, Any]], profile: str | Path,
                                     concurrency: int = 5,
                                     on_result: Any | None = None) -> list[dict[str, Any]]:
    """Fetch rate inventories through ordinary Trip.com detail pages in one reusable context.

    The pool has one Chromium context and at most ``concurrency`` reusable pages;
    a hotel gets one normal detail navigation. There is no fixed batch-size or
    elapsed-time cutoff. Public responses are immediately reduced to the
    allowlisted rate model, and protected/challenge responses stop new work.
    An optional async callback receives each terminal hotel result as soon as
    its page completes, while the same persistent context remains open.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - dependency is required in production
        raise RuntimeError("Playwright is required for Trip.com rate enrichment") from exc

    results: list[dict[str, Any] | None] = [None] * len(hotels)
    page_count = min(concurrency, len(hotels))
    if page_count == 0:
        return []
    pages: asyncio.Queue[Any] = asyncio.Queue()
    stopped = asyncio.Event()
    protected_statuses = {401, 403, 429, 430}
    rate_search_ids = {str(hotel.get("search_id") or "") for hotel in hotels
                       if hotel.get("search_id")}
    search_id = next(iter(rate_search_ids)) if len(rate_search_ids) == 1 else ""
    queue_wait_s: dict[int, float] = {}
    async with async_playwright() as playwright:
        browser_started = time.perf_counter()
        log_search_timeline(search_id, "rate_browser_launch_started", workers=page_count)
        context = await playwright.chromium.launch_persistent_context(
            str(Path(profile).expanduser().resolve()), headless=False, locale="ru-RU",
            viewport={"width": 1440, "height": 1000},
            # Existing Trip profiles may have a service worker that bypasses page
            # routes, making payload capture depend on stale worker state.
            service_workers="block",
        )
        log_search_timeline(search_id, "rate_browser_launch_finished",
                            elapsed_ms=round((time.perf_counter() - browser_started) * 1000, 1))
        try:
            pages_started = time.perf_counter()
            for _ in range(page_count):
                await pages.put(await context.new_page())
            log_search_timeline(search_id, "rate_pages_created", workers=page_count,
                                elapsed_ms=round((time.perf_counter() - pages_started) * 1000, 1))

            async def enrich(index: int, hotel: dict[str, Any]) -> None:
                enqueued_at = time.perf_counter()
                if stopped.is_set():
                    results[index] = {
                        "source_hotel_id": str(hotel.get("source_hotel_id") or ""),
                        "status": "not_started", "operation": "tripcom_detail_rate_inventory",
                        "error_type": "protection_or_challenge_stop",
                        "error_message": "A prior hotel triggered a protection response; remaining work stopped",
                        "attempt": 0, "retry_count": 0,
                    }
                    queue_wait_s[index] = time.perf_counter() - enqueued_at
                    return
                hotel_id, city_id = str(hotel.get("source_hotel_id") or ""), str(hotel.get("city_id") or "")
                check_in, check_out = hotel.get("check_in"), hotel.get("check_out")
                adults, children = hotel.get("adults", 2), hotel.get("children_count", 0)
                if (not hotel_id.isdigit() or not city_id.isdigit()
                        or not isinstance(check_in, str) or not isinstance(check_out, str)
                        or type(adults) is not int or adults < 1 or type(children) is not int or children < 0):
                    results[index] = {"source_hotel_id": hotel_id, "status": "invalid_input",
                                      "operation": "tripcom_detail_rate_inventory",
                                      "error_type": "invalid_input", "error_message": "Invalid hotel or stay context",
                                      "attempt": 0, "retry_count": 0}
                    queue_wait_s[index] = time.perf_counter() - enqueued_at
                    return
                page = await pages.get()
                hotel_page = page
                queue_wait_s[index] = time.perf_counter() - enqueued_at
                responses: list[dict[str, Any]] = []
                response_body_tasks: set[asyncio.Task[Any]] = set()
                started = time.perf_counter()
                navigation_started = started
                navigation_finished = None
                inventory_started = None
                inventory_finished = None
                room_details_started = None
                room_details_finished = None
                navigation = None
                diagnostics: dict[str, Any] = {
                    "hotel_id": hotel_id,
                    "hotel_name": hotel.get("hotel_name"),
                    "navigation_started": _utc_timestamp(),
                    "navigation_status": None,
                    "navigation_url": None,
                    "rate_response_status": None,
                    "rate_response_url": None,
                    "rate_response_received_at": None,
                    "body_wait_started": None,
                    "body_wait_finished": None,
                    "body_wait_error": None,
                    "rate_payload_detected": False,
                    "rate_payload_size": 0,
                    "rate_count": 0,
                    "rate_body_wait_started": None,
                    "rate_body_wait_finished": None,
                    "rate_body_wait_error": None,
                }
                route_callbacks_active = 0
                # Playwright runs route handlers in tasks owned by its page
                # dispatcher.  Keep references to those tasks so the rare
                # callback that survives ``unroute_all(wait)`` can be
                # cancelled and awaited before the page/context is retired.
                # Closing a page alone is insufficient: the dispatcher task
                # can otherwise be destroyed later when the event loop ends.
                route_callback_tasks: set[asyncio.Task[Any]] = set()
                route_callbacks_idle = asyncio.Event()
                route_callbacks_idle.set()

                async def drain_route_callbacks() -> None:
                    current = asyncio.current_task()
                    pending = {
                        task for task in route_callback_tasks
                        if task is not current and not task.done()
                    }
                    if not pending:
                        return
                    _, still_pending = await asyncio.wait(
                        pending, timeout=TRIPCOM_ROUTE_CALLBACK_DRAIN_TIMEOUT_SECONDS,
                    )
                    for task in still_pending:
                        task.cancel()
                    if still_pending:
                        await asyncio.gather(*still_pending, return_exceptions=True)

                async def drain_response_body_captures() -> None:
                    pending = {task for task in response_body_tasks if not task.done()}
                    if not pending:
                        return
                    _, still_pending = await asyncio.wait(
                        pending, timeout=TRIPCOM_ROUTE_CALLBACK_DRAIN_TIMEOUT_SECONDS,
                    )
                    for task in still_pending:
                        task.cancel()
                    if still_pending:
                        await asyncio.gather(*still_pending, return_exceptions=True)

                async def settle_route(action: str, route: Any, **kwargs: Any) -> None:
                    """Ignore only navigation races while settling a route."""
                    route_action = asyncio.create_task(getattr(route, action)(**kwargs))
                    try:
                        # Playwright's Route.fulfill creates an internal
                        # Channel.send task while racing page closure. If this
                        # handler is cancelled during retirement, cancelling
                        # that await can orphan the internal task and leave an
                        # unhandled TargetClosedError on the event loop.
                        await asyncio.shield(route_action)
                    except asyncio.CancelledError:
                        await asyncio.gather(route_action, return_exceptions=True)
                        raise
                    except Exception as exc:
                        message = str(exc).lower()
                        lifecycle_error = any(marker in message for marker in (
                            "route is already handled", "target page, context or browser has been closed",
                            "target closed", "page closed",
                        ))
                        # Concurrent matching requests can enter this handler
                        # before the response event from the primary request
                        # populates ``responses``. Playwright then reports an
                        # Assertion error when the duplicate route has already
                        # been continued by its other handler. The protocol has
                        # already settled that request, so don't leak this
                        # expected race as an unhandled page-listener exception.
                        duplicate_continue_race = (
                            action == "continue_" and "assertion error" in message
                        )
                        diagnostics["rate_route_settlement_error"] = (
                            f"{action}: {type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
                        )
                        if not lifecycle_error and not duplicate_continue_race:
                            raise

                def capture(response: Any) -> None:
                    if (not responses and urlparse(response.url).path
                            == TRIPCOM_RATE_ENDPOINT_PATH):
                        response_event = {
                            "response": response,
                            "status": response.status,
                            "url": response.url,
                            "received_at": _utc_timestamp(),
                        }
                        if response.status == 200:
                            # response.body() must start from the response event. Trip can
                            # navigate its detail page again while DOM extraction is in
                            # progress, invalidating Chromium's response body handle.
                            capture = _start_rate_response_body_capture(response)
                            response_body_tasks.add(capture["body_task"])
                            response_event.update(capture)
                        responses.append(response_event)
                        if response.status in protected_statuses:
                            stopped.set()

                async def capture_route_impl(route: Any) -> None:
                    """Capture the rate payload before Chromium exposes it to the page.

                    A page response body can become unavailable when Trip performs a
                    client-side navigation while another Playwright task is waiting for
                    DOM details. Fetching through the route gives us an independent
                    response body, then fulfills the original browser request unchanged.
                    """
                    if responses:
                        await settle_route("continue_", route)
                        return
                    fetch_started = time.perf_counter()
                    try:
                        routed_response = await route.fetch(
                            timeout=TRIPCOM_RATE_BODY_TIMEOUT_SECONDS * 1000,
                        )
                    except Exception as exc:
                        # Playwright's route-fetch exception call log may contain
                        # request headers and session cookies; retain only its summary.
                        route_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
                        diagnostics["rate_route_fetch_error"] = route_error
                        status_match = re.search(r"(?:←|<-)\s*(\d{3})\s", str(exc))
                        status = int(status_match.group(1)) if status_match else None
                        route_timeout = (isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                                         or type(exc).__name__ == "TimeoutError")
                        responses.append({
                            "response": None,
                            "status": status,
                            "url": route.request.url,
                            "received_at": _utc_timestamp(),
                            "body_error": None,
                            "body_timeout": (
                                f"Trip.com rate response body did not finish within "
                                f"{TRIPCOM_RATE_BODY_TIMEOUT_SECONDS:g}s after HTTP 200 headers"
                            ) if status == 200 and route_timeout else None,
                            "route_fetch_error": route_error,
                        })
                        if status in protected_statuses:
                            stopped.set()
                        # route.fetch already sent the source request. Continuing here
                        # would issue it a second time and hide the bounded timeout.
                        await settle_route("abort", route, error_code="timedout")
                        return

                    body = b""
                    body_error = None
                    body_timeout = None
                    body_wait_started = time.perf_counter()
                    body_wait_started_utc = _utc_timestamp()
                    if routed_response.status == 200:
                        try:
                            body = await asyncio.wait_for(
                                routed_response.body(), timeout=TRIPCOM_RATE_BODY_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            body_timeout = (
                                f"Trip.com rate response body exceeded "
                                f"{TRIPCOM_RATE_BODY_TIMEOUT_SECONDS:g}s deadline after HTTP 200 headers"
                            )
                        except Exception as exc:
                            body_error = f"{type(exc).__name__}: {exc}"

                    responses.append({
                        "response": routed_response,
                        "status": routed_response.status,
                        "url": routed_response.url,
                        "received_at": _utc_timestamp(),
                        "captured_body": body,
                        "body_error": body_error,
                        "body_timeout": body_timeout,
                        "body_wait_started": body_wait_started,
                        "body_wait_started_utc": body_wait_started_utc,
                        "route_fetch_elapsed_ms": round((time.perf_counter() - fetch_started) * 1000, 1),
                    })
                    if routed_response.status in protected_statuses:
                        stopped.set()
                    await settle_route("fulfill", route, response=routed_response)

                async def capture_route(route: Any) -> None:
                    """Track handler lifetime so a page is not reused mid-request."""
                    nonlocal route_callbacks_active
                    task = asyncio.current_task()
                    if task is not None:
                        route_callback_tasks.add(task)
                    route_callbacks_active += 1
                    route_callbacks_idle.clear()
                    try:
                        await capture_route_impl(route)
                    finally:
                        if task is not None:
                            route_callback_tasks.discard(task)
                        route_callbacks_active -= 1
                        if route_callbacks_active == 0:
                            route_callbacks_idle.set()

                page.on("response", capture)
                rate_route_pattern = f"**{TRIPCOM_RATE_ENDPOINT_PATH}"
                try:
                    await page.route(rate_route_pattern, capture_route)
                    url = "https://ru.trip.com/hotels/detail/?" + urlencode({
                        "hotelId": hotel_id, "cityId": city_id, "checkIn": check_in,
                        "checkOut": check_out, "adult": adults, "children": children,
                        "locale": "ru-RU", "crn": 1,
                    })
                    navigation = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    navigation_finished = time.perf_counter()
                    diagnostics.update({
                        "navigation_finished": _utc_timestamp(),
                        "navigation_status": navigation.status if navigation else None,
                        "navigation_url": navigation.url if navigation else url,
                    })
                    inventory_started = navigation_finished
                    deadline = time.monotonic() + 12
                    while not responses and time.monotonic() < deadline and not stopped.is_set():
                        await page.wait_for_timeout(100)
                    inventory_finished = time.perf_counter()
                    response_event = responses[0] if responses else None
                    response = response_event["response"] if response_event else None
                    response_status = response_event.get("status") if response_event else None
                    response_body = b""
                    payload = None
                    payload_parse_error = None
                    response_body_timeout = None
                    if response_event and "captured_body" in response_event:
                        response_body = response_event.get("captured_body") or b""
                        payload_parse_error = response_event.get("body_error")
                        response_body_timeout = response_event.get("body_timeout")
                        diagnostics["rate_body_capture_mode"] = "route_fetch"
                        diagnostics["rate_route_fetch_elapsed_ms"] = response_event.get("route_fetch_elapsed_ms")
                        diagnostics["rate_body_wait_started"] = response_event.get("body_wait_started_utc")
                        diagnostics["rate_body_wait_finished"] = _utc_timestamp()
                        diagnostics["rate_body_wait_elapsed_ms"] = round(
                            (time.perf_counter() - response_event["body_wait_started"]) * 1000, 1)
                        diagnostics["rate_body_wait_error"] = payload_parse_error or response_body_timeout
                        if response_body:
                            try:
                                payload = json.loads(response_body)
                            except Exception as exc:
                                payload_parse_error = f"{type(exc).__name__}: {exc}"
                                diagnostics["rate_body_wait_error"] = payload_parse_error
                    elif response_event and response_event.get("body_task") is not None:
                        response_body, payload_parse_error, response_body_timeout = await response_event["body_task"]
                        diagnostics["rate_body_capture_mode"] = "response_event_fallback"
                        diagnostics["rate_body_wait_started"] = response_event.get("body_wait_started_utc")
                        diagnostics["rate_body_wait_finished"] = _utc_timestamp()
                        diagnostics["rate_body_wait_elapsed_ms"] = round(
                            (time.perf_counter() - response_event["body_wait_started"]) * 1000, 1)
                        diagnostics["rate_body_wait_error"] = payload_parse_error or response_body_timeout
                        if response_body:
                            try:
                                payload = json.loads(response_body)
                            except Exception as exc:
                                payload_parse_error = f"{type(exc).__name__}: {exc}"
                                diagnostics["rate_body_wait_error"] = payload_parse_error
                    elif response_event and response_event.get("route_fetch_error"):
                        diagnostics["rate_body_capture_mode"] = "route_fetch_failed"
                        diagnostics["rate_route_fetch_error"] = response_event["route_fetch_error"]
                        response_body_timeout = response_event.get("body_timeout")
                        diagnostics["rate_body_wait_error"] = response_body_timeout
                    diagnostics.update({
                        "rate_response_status": response_event.get("status") if response_event else None,
                        "rate_response_url": response_event.get("url") if response_event else None,
                        "rate_response_received_at": response_event.get("received_at") if response_event else None,
                        "body_wait_started": _utc_timestamp(),
                    })
                    body_wait_started = time.perf_counter()
                    try:
                        body = await page.locator("body").inner_text(timeout=3000)
                    except Exception as exc:
                        body = ""
                        diagnostics["body_wait_error"] = f"{type(exc).__name__}: {exc}"
                    body_wait_finished = time.perf_counter()
                    diagnostics["body_wait_finished"] = _utc_timestamp()
                    diagnostics["body_wait_elapsed_ms"] = round(
                        (body_wait_finished - body_wait_started) * 1000, 1)
                    challenge = bool(re.search(
                        r"captcha|verify that you are human|access denied|unusual traffic|подтвердите, что вы человек",
                        body, re.I,
                    ))
                    if challenge:
                        stopped.set()
                    room_details_started = time.perf_counter()
                    item: dict[str, Any] = {
                        "source_hotel_id": hotel_id,
                        "operation": "tripcom_detail_rate_inventory",
                        "attempt": 1, "retry_count": 0,
                        "navigation_status": navigation.status if navigation else None,
                        "rate_status": response_status,
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                        "challenge_visible": challenge,
                    }
                    # Hotel facts and visible room names belong to the detail
                    # page, independently of the protected rate inventory API.
                    item["property_details"] = await _tripcom_extract_property_details(page)
                    room_details = await _tripcom_extract_room_details(page)
                    room_details_finished = time.perf_counter()
                    if challenge or response_status in protected_statuses:
                        item["status"] = "blocked"
                        item["reason"] = "challenge_or_protection_response"
                        item["error_type"] = "robots_or_provider_protection"
                        item["error_message"] = "Provider protection response; remaining work stopped"
                    else:
                        diagnostics["rate_payload_size"] = len(response_body)
                        diagnostics["rate_payload_detected"] = bool(
                            isinstance(payload, dict) and isinstance(payload.get("data"), dict))
                        status, inventory, error_type, error_message = _classify_tripcom_rate_response(
                            response_status, payload,
                            body_wait_error=diagnostics.get("body_wait_error"),
                            response_body_timeout=response_body_timeout,
                            source_hotel_id=hotel_id, check_in=check_in, check_out=check_out,
                            adults=adults, children_count=children, room_details=room_details,
                        )
                        if response_event and response_event.get("route_fetch_error") and response_status is None:
                            status, error_type, error_message = (
                                "rate_inventory_timeout", "rate_response_timeout",
                                response_event["route_fetch_error"],
                            )
                        if response_status == 200 and payload_parse_error:
                            status, error_type, error_message = (
                                "invalid_response", "rate_response_parse_error", payload_parse_error,
                            )
                        item["status"] = status
                        if inventory is not None:
                            item["inventory"] = inventory
                            diagnostics["rate_count"] = len(inventory.get("rates", []))
                            item["normalization_ms"] = round(
                                max(0.0, (time.perf_counter() - room_details_finished) * 1000), 1)
                        if error_type:
                            item["error_type"] = error_type
                        if error_message:
                            item["error_message"] = error_message
                    diagnostics["navigation_finished"] = diagnostics.get("navigation_finished") or _utc_timestamp()
                    diagnostics["final_status"] = item.get("status")
                    diagnostics["error_type"] = item.get("error_type")
                    diagnostics["error_message"] = item.get("error_message")
                    item["diagnostics"] = diagnostics
                    results[index] = item
                except Exception as exc:
                    ended = time.perf_counter()
                    diagnostics.update({
                        "navigation_finished": diagnostics.get("navigation_finished") or _utc_timestamp(),
                        "final_status": "navigation_error",
                        "error_type": type(exc).__name__,
                        "error_message": f"{type(exc).__name__}: {exc}",
                    })
                    results[index] = {"source_hotel_id": hotel_id, "status": "navigation_error",
                                      "operation": "tripcom_detail_rate_inventory",
                                      "error_type": type(exc).__name__,
                                      "error_message": f"{type(exc).__name__}: {exc}",
                                      "attempt": 1, "retry_count": 0,
                                      "diagnostics": diagnostics,
                                      "latency_ms": round((ended - started) * 1000),
                                      "navigation_latency_ms": round(max(0.0, (navigation_finished or ended)
                                                                         - navigation_started) * 1000),
                                      "navigation_failed": navigation_finished is None,
                                      "inventory_wait_ms": round(max(0.0, (inventory_finished or ended)
                                                                     - inventory_started) * 1000)
                                      if inventory_started is not None else None,
                                      "room_details_ms": round(max(0.0, (room_details_finished or ended)
                                                                   - room_details_started) * 1000)
                                      if room_details_started is not None else None}
                finally:
                    cleanup_started = time.perf_counter()
                    try:
                        # Let a route dispatch task that was just scheduled
                        # enter capture_route before treating the idle event
                        # as proof that no callback exists.
                        await asyncio.sleep(0)
                        await asyncio.wait_for(
                            route_callbacks_idle.wait(),
                            timeout=TRIPCOM_ROUTE_CALLBACK_IDLE_TIMEOUT_SECONDS,
                        )
                        await hotel_page.unroute_all(behavior="wait")
                    except asyncio.TimeoutError:
                        diagnostics["rate_route_cleanup_error"] = (
                            "Route callback did not settle within "
                            f"{TRIPCOM_ROUTE_CALLBACK_IDLE_TIMEOUT_SECONDS:g}s; retiring page before reuse"
                        )
                        try:
                            await hotel_page.close()
                        except Exception as exc:
                            diagnostics["rate_page_retire_error"] = (
                                f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
                            )
                        finally:
                            # Page.close() signals the dispatcher, but does
                            # not guarantee that an already-running handler
                            # has yielded. Drain and cancel it before putting
                            # the page back into the reusable pool.
                            await drain_route_callbacks()
                        page = await context.new_page()
                    except Exception as exc:
                        diagnostics["rate_route_cleanup_error"] = (
                            f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
                        )
                        try:
                            await hotel_page.close()
                        except Exception:
                            pass
                        page = await context.new_page()
                    diagnostics["rate_route_cleanup_wait_ms"] = round(
                        (time.perf_counter() - cleanup_started) * 1000, 1)
                    try:
                        hotel_page.remove_listener("response", capture)
                    except Exception as exc:
                        diagnostics["rate_response_listener_cleanup_error"] = (
                            f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
                        )
                    # A late Trip API response from a prior detail navigation can
                    # arrive after its route was removed and be observed during the
                    # next hotel's navigation. Retire each page after its hotel so
                    # response events cannot cross hotel boundaries; closing also
                    # releases any fallback body readers before a fresh page enters
                    # the worker pool.
                    try:
                        if page is hotel_page:
                            await hotel_page.close()
                            page = await context.new_page()
                        diagnostics["rate_page_retired"] = True
                    except Exception as exc:
                        diagnostics["rate_page_retired"] = False
                        diagnostics["rate_page_retire_error"] = (
                            f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
                        )
                        if page is hotel_page:
                            page = await context.new_page()
                    await drain_response_body_captures()
                    await pages.put(page)
                    item = results[index]
                    if item is not None:
                        item["queue_wait_ms"] = round(queue_wait_s.get(index, 0) * 1000, 1)
                        item["execution_ms"] = round(max(0.0, time.perf_counter() - started) * 1000, 1)
                        item["navigation_latency_ms"] = item.get("navigation_latency_ms") or round(
                            max(0.0, (navigation_finished or time.perf_counter()) - navigation_started) * 1000, 1)
                        item["inventory_wait_ms"] = item.get("inventory_wait_ms") or (
                            round(max(0.0, (inventory_finished or time.perf_counter()) - inventory_started) * 1000, 1)
                            if inventory_started is not None else 0.0)
                        item["room_details_ms"] = item.get("room_details_ms") or (
                            round(max(0.0, (room_details_finished or time.perf_counter()) - room_details_started) * 1000, 1)
                            if room_details_started is not None else 0.0)

            async def enrich_and_report(index: int, hotel: dict[str, Any]) -> None:
                await enrich(index, hotel)
                item = results[index] if results[index] is not None else {"status": "not_started"}
                item.setdefault("source_hotel_id", str(hotel.get("source_hotel_id") or ""))
                item.setdefault("operation", "tripcom_detail_rate_inventory")
                item.setdefault("attempt", 1 if item.get("status") != "not_started" else 0)
                item.setdefault("retry_count", 0)
                if on_result is not None:
                    if item.get("status") not in {"ok", "no_rates", "rate_inventory_absent"}:
                        item.setdefault("error_type", item.get("reason") or item.get("status") or "error")
                        item.setdefault("error_message", item.get("reason") or item.get("status") or "Enrichment failed")
                    await on_result(index, item)
                log_search_timeline(
                    search_id, "rate_task_terminal",
                    hotel_id=item.get("source_hotel_id"), name=hotel.get("hotel_name"),
                    status=item.get("status"), error_type=item.get("error_type"),
                    queue_wait_ms=item.get("queue_wait_ms"),
                    navigation_ms=item.get("navigation_latency_ms"),
                    navigation_failed=item.get("navigation_failed", False),
                    inventory_wait_ms=item.get("inventory_wait_ms"),
                    room_details_ms=item.get("room_details_ms"),
                    normalization_ms=item.get("normalization_ms", 0),
                    source_latency_ms=item.get("latency_ms"),
                    execution_ms=item.get("execution_ms"),
                )
                diagnostics = item.get("diagnostics")
                if isinstance(diagnostics, dict):
                    log_search_timeline(
                        search_id, "rate_task_diagnostics", hotel_id=item.get("source_hotel_id"),
                        diagnostic_json=json.dumps(
                            diagnostics, ensure_ascii=False, separators=(",", ":"), default=str),
                    )

            await asyncio.gather(*(enrich_and_report(index, hotel)
                                   for index, hotel in enumerate(hotels)))
        finally:
            await context.close()
    return [item if item is not None else {"status": "not_started"} for item in results]
