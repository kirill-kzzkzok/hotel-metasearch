# Phase A — Hotel Field Data-Flow Audit Matrix

**Repo:** `hot-tickets-project` @ HEAD 3f72238 (READ-ONLY audit; no code changed, no searches created, server on 8765 untouched)
**Date:** 2026-10-19
**Scope:** Sravni (sources/sravni.py, sravni_detail.py) and Trip.com (sources/tripcom.py, sources/tripcom_rate_fetch.py, sources/tripcom_rates.py) → normalization.py → persistence.py → schema.py tables → read_model.py → api.py → web_assets/hotels.js.

**Method.** Every row was traced by reading the parser code, then confirmed against the live SQLite store (`hot-tickets.sqlite3`, opened `mode=ro`): the raw keys below are the keys actually present in `hotel_raw_page.raw_json`, `hotel_room_rate_observation.raw_json`, `hotel_provider_metadata.metadata_json`, and `hotel_room_rate_fetch.property_details_json`. Raw Sravni list payload keys observed: `id, name, address, stars, rating, reviews_count, location{lat,lon}, images[], nearby_places[{name,type,distance_meters}], offers[{provider, price{currency,value}, deeplink, labels}], canonical_url`. Trip card keys observed: `id, name, rating, reviews_count, stars, images, coordinates(+coordinate_source), amenities, nearby_places, offers[...], review_quotes, review_source` plus (list-XHR merge, present on 527–1200 of ~5700 cards) `name_en, offer_tags, landmarks_desc, rating_subscores, bed_type, rating_description, review_summary_line`.

**Pipeline summary.**
- Sravni: `sravni.py:84-166 search()` → raw page persisted by `persistence.py:178-304 _persist_page` (normalization re-run at `persistence.py:231 normalize_hotel(raw_hotel, now, source, query)`) → `hotel_observation` / `hotel_offer_observation` / `hotel_raw_page` → `read_model.py:247-1128 _read_search` → `api.py:216-245 GET /api/hotels/search/{id}` → `pipeline.py:75-335 _finalize_search` (canonical merge, `location` summary, `review_stats`) → `hotels.js render()` 1388-1976. Sravni hotel-page facts go through a **separate** side channel: `sravni_detail.py:266-323 enrich_sravni_hotel_pages` → `hotel_provider_metadata` → `read_model.py:818-830` merges them into `property_details`.
- Trip list: `tripcom.py:745-1555 _tripcom_browser_search` (fetchHotelList XHR capture 876-1036, SSR `__next_f` regex extraction 1040-1111, DOM card snapshot 1700-1803) → same `_persist_page` path with `source="trip_com"` (normalization re-run at tripcom.py:1529-1533 with query) → read model → API → UI.
- Trip rates: `tripcom_rate_fetch.py:123-367 collect_tripcom_room_rates_via_fetch` (in-page POST to `/restapi/soa2/33269/getHotelRoomListOversea`, template captured from one detail-page landing) → `tripcom_rates.py:55-290 normalize_tripcom_rate_inventory` → `rate_enrichment.py:208-263 persist_completed_rate_result` → `persistence.py:54-147 persist_tripcom_room_rates` → `hotel_room_rate_observation` (+ `hotel_room_rate_fetch` accounting + `property_details_json`) → `read_model.py:476-516, 783-814` → API → UI rooms modal. Comment enrichment: in-page POST `getHotelCommentInfo` (`tripcom_rate_fetch.py:39-51, 326-356`), attached as `review_info` on the rate outcome, merged into `property_details_json` (`rate_enrichment.py:226-236`) and re-exposed by `read_model.py:831-844, 990-991`.

**Status legend.** `extracted` = reaches the UI render; `discarded` = parsed/normalized/persisted and API-exposed but never rendered (or only counted); `requires_investigation` = claimed path could not be verified in code; `unreliable_unavailable` = parser supports the field but the live source payload never carries it, or the only producing path is the permanently-forbidden Trip detail-page navigation.

Abbreviations: N=normalization.py, P=persistence.py, RM=read_model.py, A=api.py, JS=web_assets/hotels.js, TC=sources/tripcom.py, TRF=sources/tripcom_rate_fetch.py, TRS=sources/tripcom_rates.py, SD=sravni_detail.py, PL=pipeline.py, GC=geo/core.py. Table.column = schema.py DDL lines 58-214.

---

## 1. Sravni — list API (property scope)

| # | Raw JSON field (live) | Parser | Normalized | Persisted | API field | UI | Scope | Source / obs-time / confidence | Status |
|---|---|---|---|---|---|---|---|---|---|
| S1 | `hotels[].id` | sravni.py:156-158 (search) → N:413 | `source_hotel_id` (N:511) | `hotel_observation.source_hotel_id` (P:251-272; schema:80) | `hotel.source_hotel_id` | JS:220-224 identity keys, 849-853 | property | source col + `hotel_observation.observed_at` (P:181 utc_now) | extracted |
| S2 | `hotels[].name` | N:414 | `name`, `normalized_name` (N:512) | `hotel_observation.name` | `hotel.name` | JS:1816 title; 906 RM | property | same row | extracted |
| S3 | `hotels[].address` | N:513 | `address` | `hotel_observation.address` | `hotel.address` / `hotel.location.address` (PL:130, GC:381) | JS:1749 address line | property | same row | extracted |
| S4 | `hotels[].stars` | N:507→572-580 `_normalize_hotel_stars` (0<x≤5) | `stars` | `hotel_observation.stars` | `hotel.stars` | JS:1817-1825 ★, 1463 filter | property | same row | extracted |
| S5 | `hotels[].rating` (0-10 scale) | N:501-505→582-597 `_normalize_rating_fields` | `rating_original, rating_scale=10, rating_normalized, rating_source, rating` (N:515-516) | `hotel_observation.rating, rating_original, rating_scale, rating_normalized, rating_source` (P:250-273; schema:81-82; ALTERs 220-225) | `hotel.rating*`; `hotel.review_stats[].rating` (PL:148-153) | JS:1626-1628 rating, 1455-1458 comparableRating, 1660-1663 secondary | property | `rating_source='sravniotel'`, `observed_at` row; rating scale preserved | extracted |
| S6 | `hotels[].reviews_count` | N:506, 517 | `reviews_count` | `hotel_observation.reviews_count` | `hotel.reviews_count`, `review_stats[].reviews_count` | JS:1631-1633 отзывы | review | same row | extracted |
| S7 | `hotels[].images[].{x240,x500}` | N:417-434 (food/logo/QR filtered, `source` stamped) | `images[]` | `hotel_observation.images_json` (P:271) | `hotel.images` | JS:216, 1582-1615 gallery + fallback | property | same row | extracted |
| S8 | `hotels[].location.{lat,lon}` | N:540→391-408 `_normalize_coordinate_pair` (location fallback documented 394-397) | `coordinates` | raw inside `hotel_observation.raw_json` (P:272) — no own column | `hotel.coordinates` → `hotel.location.coordinates` (PL:130; RM:634) | JS:374, 399 map markers, 547 | property | `coordinate_status=PROVIDER_COORDINATES` (GC:328-336), method/confidence via GC metadata | extracted |
| S9 | `hotels[].nearby_places[] type=city_center` | N:518 | `nearby_places` | `hotel_observation.nearby_places_json` (P:271) | `hotel.nearby_places` → `location.distance_to_center_km` + `source_center_references` (GC:231-246, 357-379; provider_reported method) | JS:1480 fallback, 1684-1685 «до центра», 1466 filter | property | `center_source` + `distance_to_center_method='provider_reported'` | extracted |
| S10 | `hotels[].nearby_places[] type=metro/landmark` | N:518 | `nearby_places` | same | `hotel.nearby_places` → `location.distance_to_landmark_km/landmark_name/landmark_type` (GC:241-247, 261-263) | JS:1706-1716 metro merge, 1728-1739 landmark line | property | `landmark_source` (GC:264) | extracted |
| S11 | `hotels[].canonical_url` | N:551→555-570 `_normalize_source_hotel_url` (absolutized to https://hotels.travel.ru) | `source_hotel_url` (raw-json only) | inside `hotel_observation.raw_json` | `hotel.source_hotel_url` (RM:971-974) | JS:1194, 1196 reviewLink fallback | property | RM re-normalizes on read (RM:972) | extracted |
| S12 | `hotels[].phone` | N:541 | `phone` | raw_json | `hotel.phone` (RM:955) | JS: none (grep `phone` in hotels.js: 0 render hits) — consumed only by matching features `phone` (hotel_matching.py:28, 213-217) | property | raw payload never carries `phone` (DB check: 0 occurrences) | unreliable_unavailable |
| S13 | `hotels[].website` | N:542 | `website` | raw_json | `hotel.website` (RM:955) | matching only (hotel_matching.py:29, 219-223); no UI | property | absent from live payload | unreliable_unavailable |
| S14 | `hotels[].chain` | N:542 | `chain` | raw_json | `hotel.chain` (RM:955) | matching only (hotel_matching.py:30); no UI | property | absent from live payload | unreliable_unavailable |
| S15 | `hotels[].country` | N:543 | `country` | raw_json | `hotel.country` (RM:957) | matching feature only; not rendered | property | absent from live payload (query `country` covers it instead) | unreliable_unavailable |
| S16 | `hotels[].beach_line / beachLine / beachfront / beach_access` | N:509, 544-547→599-616 `normalize_beach_line` | `beach_line, beach_line_source, beach_line_evidence, beach_access` | raw_json | `hotel.beach_*` (RM:958-965) → `location.beach_line` (GC:341-350; may derive from verified beach distance) | JS:1467 filter, 1477, 1692-1698 «до пляжа» | property | `beach_line_source` ('source'/'verified_beach_distance') + evidence | unreliable_unavailable (field never present in Sravni list payload; line usually derived from OSM beach distance instead) |
| S17 | `hotels[].amenities / facilities / hotel_facilities / features / services` | N:537→332-345 `extract_amenities`; breakfast facts N:206-225 | `amenities[]` (pattern-canonical), `breakfast_available/source/raw_text` | raw_json (re-derived on read RM:870-874) | `hotel.amenities` | JS:1104-1112 hotelAmenities, 1753-1760 badges, 1468 filter | property | pattern whitelist only; no provenance per amenity | unreliable_unavailable (Sravni list ships no amenities key — 0 in DB; amenities arrive via detail channel S43) |
| S18 | `hotels[].rating_subscores[]` | N:524-526 | `rating_subscores` (≤6) | raw_json | `hotel.rating_subscores` (RM:981) | JS:1640-1656 subscore bars | review | raw only; no per-subscore source | unreliable_unavailable (never in Sravni list payload) |
| S19 | `hotels[].offer_tags[]` | N:522-523 | `offer_tags` (≤6) | raw_json | `hotel.offer_tags` (RM:978) | JS:1665-1670 scarcity, 1772-1774 early/late | property | raw only | unreliable_unavailable (never in Sravni payload) |
| S20 | `hotels[].name_en` | N:531-532 | `name_en` | raw_json | `hotel.name_en` (RM:984) | JS: 0 hits; matcher reads `name` only | property | raw only | unreliable_unavailable (never in Sravni payload) |
| S21 | `hotels[].landmarks_desc` | N:527-528 | `landmarks_desc` (≤240) | raw_json | `hotel.landmarks_desc` (RM:982) | JS:1748 landmarks-line | property | raw only | unreliable_unavailable (never in Sravni payload) |
| S22 | `hotels[].bed_type` | N:529-530 | `bed_type` (≤120) | raw_json | `hotel.bed_type` (RM:983) | JS:1305 room-modal meta | property | raw only | unreliable_unavailable (never in Sravni payload) |
| S23 | `hotels[].rating_description` | N:533-534 | `rating_description` (≤80) | raw_json | `hotel.rating_description` (RM:985) | JS:1658-1659 verdict | review | raw only | unreliable_unavailable (never in Sravni payload) |
| S24 | `hotels[].review_summary_line` | N:535-536 | `review_summary_line` (≤160) | raw_json | `hotel.review_summary_line` (RM:986) | JS:1680-1681 «...» | review | raw only | unreliable_unavailable (never in Sravni payload) |
| S25 | `hotels[].review_quotes / review_source` | N:549-550 | `review_quotes`, `review_source` | raw_json | `hotel.review_quotes/review_source` (RM:969-970) → `hotel.review_summary` (PL:154-160 `analyze_review_quotes`) | JS:1785-1813 «Гости чаще хвалят» | review | review_summary carries source+observed_at (PL:156-159) | unreliable_unavailable (never in Sravni payload) |
| S26 | `totalHotels` / `total_hotels` | sravni.py:151-152 camelCase fix | — | `hotel_raw_page.raw_json.totalHotels`; `hotel_search_session.total_hotels` (P:294-298) | `search.total_hotels`, `sources[].reported_total` (P:350-355) | JS:129 progress, 2088; not a card field | meta | page-observed | extracted (pagination/audit only) |
| S27 | `nextPageToken` / `next_page_token` | sravni.py:153-154 | — | `hotel_search_session.next_page_token` (P:205-219; empty string→NULL) | `search.has_more / next_page_token` | JS:1504 load-more, 2177-2183 auto-pager | meta | repeated-token stop recorded as `_paginationStopReason` (P:211-219) | extracted |
| S28 | `providers[].{code,display_name}` | sravni passthrough in raw page | — | `hotel_raw_page.raw_json.providers` | `search.providers` (RM:1051-1060) | JS:1896-1900 providerName display names | meta | page-observed | extracted |

## 2. Sravni — offer scope (list API)

| # | Raw field | Parser | Normalized | Persisted | API | UI | Scope | Provenance | Status |
|---|---|---|---|---|---|---|---|---|---|
| S29 | `offers[].price.value` (minor units) | N:439-448 (Decimal/100, >0) | `amount, original_amount, source_price_value, source_price_divisor` (N:456-458) + `total_price/nights/nightly_price/price_basis` when stay>0 nights (N:481-500) | `hotel_offer_observation.amount, source_price_value` + raw_json (P:284-293; schema:88-94) | `hotel.offers[].amount/nightly_price/price_basis` (RM:772-781) | JS:460, 1474-1475 minOffer, 1619-1620, 1873-1878 «Лучшая цена», 1439-1442 price filter | offer | `observed_at` per offer row; `price_basis='search_context_stay_total'` | extracted |
| S30 | `offers[].price.currency` | N:439, 461 | `currency, original_currency` | `hotel_offer_observation.currency` | `offer.currency` | JS:1429-1430 currency select, 1873, 1830 | offer | same row | extracted |
| S31 | `offers[].provider` | N:453 | `provider` | `hotel_offer_observation.provider` | `offer.provider` | JS:1891, 1993-2000 providerName | offer | same row | extracted |
| S32 | `offers[].deeplink` | N:461 | `deeplink` | `hotel_offer_observation.deeplink` | `offer.deeplink` | JS:1880-1885 CTA, 1835 | offer | same row | extracted |
| S33 | `offers[].labels[]` | N:449-450; semantics N:471, 475, 477-478 | `labels, payment_method, tax_status, cancellation` | `hotel_offer_observation.labels_json` + raw_json | `offer.labels/tax_status/cancellation/payment_method` (RM:763-781) | JS:1432-1433 cancellation filter, 1869 badge; meal N/A | offer | same row | extracted |
| S34 | `offers[].room_type` | N:464 | `room_type` + `room_privacy` (N:466→28-39 classify_room_privacy) | raw_json (offer) | `offer.room_type/room_privacy` | JS:1836-1843 «ЛИЧНЫЙ НОМЕР», 1844-1846, private-mode filter PL:279-286 | room | same row | unreliable_unavailable (Sravni offers never carry room_type — DB offer keys: deeplink/labels/price/provider only; room_privacy always 'unknown') |
| S35 | `offers[].bed_type` | N:465 | `bed_type` | raw_json | `offer.bed_type` | JS:1305 | room | same row | unreliable_unavailable (absent from Sravni payload) |
| S36 | `offers[].meal_plan/board/meal/rate_name/description/labels` | N:145-204 `_offer_meal_facts` + 84-143 `classify_meal_plan` | `meal_plan, meal_status, meal_included, meal_available, meal_source, meal_confidence, meal_raw_text, meal_extra_amount/currency, meal_normalizer_version, breakfast_*` | raw_json (offer; re-deriveable via P:149-176) | offer fields | JS:1253-1267 mealPresentation, 1857-1867 badge, 1432 meal filter | offer | `meal_source='sravniotel:offer.…'`, `meal_confidence high/none`, version meal_v4 | extracted (label `breakfast_included` → meal_plan=breakfast) |
| S37 | `offers[].occupancy` (provider) / query adults+children | N:467-469 | `occupancy` | raw_json | `offer.occupancy` | JS:1306, 1845-1846 guests | room | query-derived when provider omits | extracted (context-derived, not provider data) |
| S38 | `offers[].taxes / fees` | N:472 | `taxes, fees` (suppress nightly split N:492) | raw_json | offer fields | JS:1322 taxes/fees line in room modal | offer | same row | unreliable_unavailable (never present) |
| S39 | `offers[].payment_method` (provider field) | N:471 | `payment_method` | raw_json | offer field | JS: none (0 hits; labels→payment_method used only internally) | offer | same row | unreliable_unavailable (never present as raw field; label-derived only) |
| S40 | `offers[].valid_until` | N:472 | `valid_until` | raw_json | offer field | JS: none | offer | same row | unreliable_unavailable (never present) |

## 3. Sravni — detail channel (`/api/v1/hotel` + HTML fallback) → `hotel_provider_metadata`

Envelope written by SD:180-203 `_persist_metadata`: `metadata_json = {provider:'sravniotel', source:'sravniotel_hotel_api'|'sravniotel_hotel_page_html', method:'api_get'|'http_html_page_parse', confidence:'high', retrieved_at:ISO-UTC, <facts>}` → table `hotel_provider_metadata(search_id, source, source_hotel_id, provider, metadata_json, retrieved_at)` (schema:209-214). Read path: RM:468-475 → RM:818-830 `sravni_page_facts` merged into `property_details` (API-exposed), amenities merged into card `amenities` (RM:875-879).

| # | Raw field | Parser | Normalized | Persisted | API | UI | Scope | Provenance | Status |
|---|---|---|---|---|---|---|---|---|---|
| S41 | `hotel.check_in_time` ("14:00:00") | SD:87-99 `parse_sravni_hotel_api`; formatted SD:120 | `check_in_text` "Заезд с 14:00" | `hotel_provider_metadata.metadata_json.check_in_text` + `retrieved_at` | `hotel.property_details.check_in_text` (RM:821-830) | JS:1113-1119 hotelPolicy → 1768-1771 policy-line | property | source+method+`confidence:'high'`+`retrieved_at` in envelope | extracted |
| S42 | `hotel.check_out_time` | SD:99, 121 | `check_out_text` "Выезд до 12:00" | same | `property_details.check_out_text` | JS:1117, 1768-1771 | property | same envelope | extracted |
| S43 | `hotel.amenity_groups[].amenities[].name` | SD:100-108 (dedup, order kept) | `amenities_text[]` | metadata_json | merged into `hotel.amenities` (RM:875-879) and `provider_details[].amenities_text` | JS:1104-1112 hotelAmenities, 1753-1760 badges, amenity filter 1468/1520 | property | same envelope | extracted |
| S44 | `hotel.description[].paragraphs[]` | SD:109-113, 123 (600 chars) | `description_text` | metadata_json | `property_details.description_text` | JS:1778-1783 «Об отеле» details block; signature 1164 | property | same envelope | extracted |
| S45 | `hotel.policies[].paragraphs[]` | SD:114-118, 124 (600 chars) | `policy_text` | metadata_json (DB-confirmed present) | `property_details.policy_text` | **JS: 0 hits — never rendered** (hotelPolicy reads only check_in/check_out) | property | same envelope | discarded (persisted+API, no UI consumer) |
| S46 | HTML page text «Заезд с HH:MM» / «Выезд до HH:MM» / facility tokens | SD:129-144 `parse_sravni_hotel_page` (tokens SD:53-80) | same `check_in_text/check_out_text/amenities_text`, `source='sravniotel_hotel_page_html'` | same table | same merge | same as S41-S43 | property | same envelope (method='http_html_page_parse') | extracted (fallback path, API-preferred per SD:286-295) |
| S47 | failure marker | SD:310-318 negative marker `{source:'sravniotel_failed_pages', retrieved_at}` | — | metadata_json (skips retries for 1h, SD:239-263) | filtered out RM/SD:326-344 | none | meta | honest negative marker | extracted (bookkeeping) |

## 4. Trip — list collection (fetchHotelList XHR + SSR flight data + DOM card)

XHR capture TC:876-1036 (`capture_inventory_facts`, merge into raw card at TC:1351-1384 — structured facts outrank DOM); SSR regex extraction TC:1040-1111 (per-hotel chunks, setdefault merge); DOM snapshot parser TC:1700-1803. Normalization for persistence: TC:1529-1533 `normalize_hotel(source='trip_com', query)`. Persistence: P:178-304 with `source='trip_com'`.

| # | Raw field | Parser | Normalized | Persisted | API | UI | Scope | Provenance | Status |
|---|---|---|---|---|---|---|---|---|---|
| T1 | `hotelInfo.summary.hotelId` / DOM `a.hotelName[href hotelId=]` / card id | TC:892-900; TC:1704-1708; SSR TC:1065 | `source_hotel_id` (N:411-511) | `hotel_observation.source_hotel_id` | `hotel.source_hotel_id` | JS:220-224, 850-853, 910 | property | obs row | extracted |
| T2 | DOM name `h2/h3/[class*=hotelName]` | TC:1709-1713 | `name` | `hotel_observation.name` | `hotel.name` | JS:1816 | property | obs row | extracted |
| T3 | `hotelInfo.positionInfo.mapCoordinate[].{latitude,longitude}` | TC:903-917; SSR TC:1066-1089; DOM map-link TC:1781-1791 | `coordinates` + `coordinate_source='trip_com_list_api'` (TC:1356-1358) | raw_json | `hotel.coordinates` (RM:634; treated as geocoded/enrichment coordinate RM:529-530, 639-642) → `location.coordinates` | JS:374, 399, 547, 1935 | property | `coordinate_source` retained in raw; location summary exposes method/confidence (GC:309-313) | extracted |
| T4 | `hotelInfo.hotelStar.star` | TC:918-922; SSR TC:1069, 1090-1094; DOM labels TC:1730-1737 («N из 5») | `stars` | `hotel_observation.stars` | `hotel.stars` | JS:1817-1825, 1463 | property | obs row | extracted |
| T5 | `hotelInfo.highlightsModule.highlightsItemList[].title` | TC:923-934 (≤12) | `amenities[]` (merged into raw card TC:1365-1366) | raw_json → re-derive RM:870-874 | `hotel.amenities` | JS:1104-1112, 1753-1760 | property | raw only, no per-item provenance | extracted (coverage partial: 1039/5700 cards in DB) |
| T6 | `hotelInfo.hotelImages.multiImgs[].url` | TC:938-948 (≤10, dedup) | `images[]` (TC:1367-1369) | `hotel_observation.images_json` | `hotel.images` | JS:216, 1582-1615 | property | obs row | extracted |
| T7 | `roomInfo[0].roomTags.{advantageTags,encourageTags}[].tagTitle` | TC:953-968 (≤6) | `offer_tags` | raw_json | `hotel.offer_tags` (RM:978) | JS:1665-1670 scarcity, 1772-1774 early/late tags; signature 1172 | offer | raw only | extracted (719/5700 cards) |
| T8 | `roomInfo[0].bedInfo.contentList[0]` | TC:970-977 (≤120) | `bed_type` | raw_json | `hotel.bed_type` (RM:983) | JS:1305 room-modal meta | room | raw only | extracted (527/5700) |
| T9 | `hotelInfo.commentInfo.subScore[].{content,number}` | TC:983-995 (0<v≤10, ≤6) | `rating_subscores[{name,score}]` | raw_json | `hotel.rating_subscores` (RM:981) | JS:1640-1656 bars; signature 1165 | review | raw only; scale assumed /10 | extracted (696/5700; superseded by comment endpoint T31 when present) |
| T10 | `hotelInfo.positionInfo.positionDesc / positionDescs[]` | TC:996-1012 (≤240) | `landmarks_desc` | raw_json | `hotel.landmarks_desc` (RM:982) | JS:1748 «📍 …» | property | raw only | extracted (700/5700) |
| T11 | `hotelInfo.nameInfo.enName` | TC:1004-1006 (≤160); merge TC:1378-1379 | `name_en` (N:531-532) | raw_json | `hotel.name_en` (RM:984) | **JS: 0 hits; hotel_matching.py does not read name_en (features: name/address/coords/phone/website/city/country/stars/chain/images — matching.py:28-30)** | property | raw only | **discarded** (extracted+persisted+API-exposed, but neither UI nor matcher consumes it; comment says "enables cross-source name matching" TC:1001-1003 — that wiring does not exist) |
| T12 | `commentInfo.commentDescription` («Потрясающе») | TC:1018-1020 (≤80) | `rating_description` | raw_json | `hotel.rating_description` (RM:985) | JS:1658-1659; signature 1170 | review | raw only | extracted (550/5700) |
| T13 | `commentInfo.oneSentenceComment.tagTitle` | TC:1021-1023 (≤160) | `review_summary_line` | raw_json | `hotel.review_summary_line` (RM:986) | JS:1680-1681; signature 1171 | review | raw only | extracted |
| T14 | `commentInfo.commentScore` | TC:1024-1030 (0<v≤10); SSR TC:1070, 1095-1102; DOM TC:1715-1727 (bare «9,5» near отзывы) | `rating` (N:501-505) | `hotel_observation.rating*` | `hotel.rating`, `review_stats` | JS:1626-1628, 1455-1458 | review | `rating_source='trip_com'`, scale 10 | extracted |
| T15 | `commentInfo.commenterNumber` | TC:1031-1034; SSR TC:1071, 1103-1105; DOM TC:1728-1729 | `reviews_count` | `hotel_observation.reviews_count` | `hotel.reviews_count` | JS:1631-1633 | review | obs row | extracted |
| T16 | DOM quoted text «…» (card) | TC:1749→1696-1698 `_tripcom_card_review_quotes` (≤4) | `review_quotes`, `review_source='trip_com'` | raw_json | `hotel.review_quotes` → `hotel.review_summary` (PL:154-160) | JS:1785-1813 «Гости чаще хвалят» | review | review_summary carries source+observed_at | extracted (5704 cards — the dominant review-quote source) |
| T17 | DOM «Рядом:» prefix / district regex | TC:1744-1748, 1799-1801 | `nearby_places[{name,type:district|landmark}]` | `hotel_observation.nearby_places_json` | `hotel.nearby_places` (RM:976) | JS:1480 uses only `type=city_center` (Trip never emits that type); `_location_summary` landmarks need `distance_meters` which Trip entries lack (GC:241-247) | property | raw only | **discarded** (persisted+API, no UI consumer — distances never computed from these) |
| T18 | DOM price lines (₽/RUB, TripCoins filtered) | TC:1750-1762 (money[-1]→minor units; both card variants 1588-1681 / snapshot) | offer `price.value` → `amount` (N:439-448), nightly split N:481-500 | `hotel_offer_observation.amount/source_price_value/currency` + raw_json | `hotel.offers[]` (RM:772-781) | JS:1619-1620, 1873-1878, 1429-1442 | offer | offer `observed_at`; price_basis stay-total | extracted |
| T19 | DOM labels (бесплатная отмена / без возврата / оплата в отеле / вкл. налоги / завтрак) | TC:1764-1775 | `labels[]`, `cancellation`, `tax_status`, `payment_method`, `meal_plan` (classify_meal_plan TC:1769) | `hotel_offer_observation.labels_json` + raw_json | offer fields (RM:766-781) | JS:1432-1433, 1869, 1848-1867 | offer | offer obs row | extracted |
| T20 | DOM room_type line (after «посмотреть на карте») | TC:1740-1742, 1743 | `room_type`, `bed_type`, `room_privacy` (N:464-466) | raw_json (offer) | `offer.room_type/room_privacy` | JS:1836-1846, private-mode PL:279-286 | room | offer row | extracted |
| T21 | `deeplink` = card hotel URL (hotelId link) | TC:1704-1706, 1776-1778 | `deeplink` | `hotel_offer_observation.deeplink` | `offer.deeplink` | JS:1880-1885, 1210-1212 reviewLink (Trip) | offer | offer row | extracted |
| T22 | `sourceCityId` (raw page) | TC:1521 | — | `hotel_raw_page.raw_json.sourceCityId` | none (read by rate_enrichment.py:66-84 to build rate tasks) | none | meta | page-observed | extracted (internal routing key) |
| T23 | `.hotel-count-tip` «Найдено N объектов размещения» | TC:1474-1487 `_tripcom_reported_total` | — | `hotel_source_run.reported_total` (P:350-355; schema:67) | `sources[].reported_total` (RM:1071) | JS: none directly; collection audit / completeness notices 2049-2063 | meta | run row | extracted |
| T24 | `collectionAudit` (scrolls, inventory requests, card counts) | TC:1445-1447, 1521-1527 | — | `hotel_raw_page.raw_json.collectionAudit` | `sources[].collection_audit` + `search_audit.raw_cards_by_source` (RM:1010-1049, PL:216-246) | JS:2043-2063 partial/failed notices | meta | run row | extracted |

## 5. Trip — rate inventory (getHotelRoomListOversea, in-page XHR)

Parser TRS:55-290 `normalize_tripcom_rate_inventory`; fetch TRF:53-81 (template TRF:86-120); classification TRS:293-332; persistence P:54-147 (context guard P:70-85; rate rows P:130-144; accounting row P:92-129). Read: RM:476-516 (latest ok fetch + newest rate per provider_rate_id) → `hotel.room_rates` / `hotel.room_options` (RM:797-814). API: full card `hotel.room_rates/room_options/property_details` (A:216-245) and scoped `room-rates/results` (A:505-526, field list A:516-522). UI: `roomOfferRows` JS:1229-1252 → `openRoomsModal` JS:1269-1335.

| # | Raw field | Parser | Normalized | Persisted | API | UI | Scope | Provenance | Status |
|---|---|---|---|---|---|---|---|---|---|
| T25 | `data.saleRoomMap` key (`<sRoomId>_<token>`/skey) | TRS:84-90, 141 | `provider_rate_id` | `hotel_room_rate_observation.provider_rate_id` (+unique key schema:101-103) | rate row in `hotel.room_rates[]` | JS:1132 signature; dedupe key 1236-1238 | offer | `observed_at` per rate (µs, TRS:92); `hotel_room_rate_fetch.observed_at` | extracted |
| T26 | `rate.id` | TRS:245 | `provider_rate_record_id` | raw_json only | rate field | JS: 0 hits | offer | raw_json | discarded |
| T27 | `rate.physicalRoomId` | TRS:192, 246-247 | `provider_room_id` | raw_json | `room_options[].room_id` (RM:799-803) | grouping key only (JS:1236) | room | raw_json | extracted (grouping) |
| T28 | `data.roomList[].subRoomList[].{skey,key,sRoomId}` | TRS:95-119, 147-152 | `room_group_index` | `hotel_room_rate_observation.room_group_index` (P:141) | `room_options[].room_group_index` | ordering/grouping (RM:786-789, JS:797-801) | room | raw_json | extracted |
| T29 | `rate.priceInfo.price` | TRS:169-174 (>0 finite) | `price_amount` | raw_json | rate field | JS:1310-1316 (row.amount ?? price_amount), 1251 filter | offer | `price_basis='trip_com_priceInfo.price; UI cross-check pending'` (TRS:285); `observed_at` | extracted |
| T30 | `priceInfo.displayPrice` | TRS:272 | `price_display_text` | raw_json | rate field | JS:1311 rawPrice | offer | same | extracted |
| T31 | `priceInfo.currency` | TRS:273 | `currency` | raw_json | rate field | JS:1251, 1316 | offer | same | extracted |
| T32 | `priceInfo.deletePrice` | TRS:265 | `original_price_display` (strikethrough) | raw_json | rate field | JS:1246, 1312-1314 «old price» | offer | same | extracted |
| T33 | `priceInfo.taxText` | TRS:274 | `taxes_fees_raw_text` | raw_json | rate field | JS:1322 taxes line | offer | same | extracted |
| T34 | `priceInfo.priceExplanation` | TRS:275-276 | `price_explanation` | raw_json (DB-confirmed) | rate field | **JS: 0 hits** | offer | same | discarded (persisted+API, never rendered) |
| T35 | `totalPriceInfo.{total,payHotelTax}.{title,description,price,localPrice}` | TRS:181-189 | `additional_charge_evidence[]` | raw_json (DB-confirmed) | rate field | **JS: 0 hits** (modal uses only taxes_fees_raw_text, JS:1322) | offer | same | discarded |
| T36 | `rate.mealInfo.title` / `saleRoomCategoryList[type=mealInfo].title` | TRS:153-168 → N:145-204 | `meal_plan, meal_status, meal_included, meal_available, meal_confidence, meal_raw_text, meal_extra_amount/currency, meal_source, breakfast_*` | raw_json | rate fields | JS:1253-1267 mealPresentation, 1857-1867 card badge, 1247 | offer | `meal_source='trip_com_rate_inventory:mealInfo.title'`, `meal_confidence`, version meal_v4 | extracted |
| T37 | `saleRoomCategoryList[].title` (all) | TRS:155-158, 278 | `rate_labels[]` | raw_json | rate field | **JS: 0 hits** | offer | same | discarded |
| T38 | `rate.cancelInfo.title` | TRS:175-180 | `cancellation` (free/non_refundable/unknown) + `cancellation_raw_text` | raw_json | rate fields | JS:1319-1321 «Бесплатная отмена» | offer | same | extracted |
| T39 | `bookingStatusInfo.remainRoomQuantity` | TRS:190-191, 264 (0<n<100) | `rooms_left` | raw_json | rate field | JS:1298-1302 «Осталось N номеров», 1246 | offer | same | extracted |
| T40 | `physicRoomMap[id].name` / rate `roomName/roomTitle/roomType` | TRS:240-253 | `room_type` | raw_json | `room_options[].room_type` | JS:1278-1284 group heading, 1250 | room | same | extracted |
| T41 | `physicRoomMap[id].bedInfo.title` | TRS:209-212, 260 | `bed_type` | raw_json | rate field / room option | JS:1305 | room | same | extracted |
| T42 | `physicRoomMap[id].areaInfo.title` («12 м²») | TRS:203-207, 259 | `room_area` | raw_json | rate field | JS:1305; signature 1174 | room | same | extracted |
| T43 | `physicRoomMap[].{physicalFacilityList,newPhysicalFacilityList}[].title`, `wifiInfo.title`, `faciltityInfo.list[].subList[].title`, `roomFacilityList[].title` | TRS:213-230, 255-257 (area titles stripped) | `room_amenities[]` (≤24) | raw_json | `room_options[].amenities` (RM:806) | **JS: only `option.amenities?.length` in render signature 1175 — never displayed** (modal shows bed/area/occupancy only, JS:1305-1307) | room | same | discarded (extracted+persisted+API; UI counts but does not render) |
| T44 | `physicRoomMap[].pictureInfo[].url` | TRS:234-239 (≤12) | `room_images[]` | raw_json | `room_options[].images` (RM:810) | JS:1287-1296 room-modal photo gallery; signature 1174 | room | same | extracted |
| T45 | `rate.roomLabelList[].text` | TRS:281, 284 | `availability_raw_text`, `rate_availability_labels` | raw_json | rate fields | **JS: 0 hits** | offer | same | discarded |
| T46 | `rate.priceLabelList[].text` | TRS:282 | `discount_labels[]` | raw_json | rate field | **JS: 0 hits** | offer | same | discarded |
| T47 | `rate.roomDescList[].text` | TRS:283 | `rate_descriptions[]` | raw_json | rate field | **JS: 0 hits** | room | same | discarded |
| T48 | `saleRoomCategoryList[type=paymentInfo/confirmInfo].title` | TRS:163-165, 279-280 | `payment_terms_raw_text`, `confirmation_raw_text` | raw_json | rate fields | **JS: 0 hits** | offer | same | discarded |
| T49 | `data.htlSpiderActionErrorCode` | TRS:73-81; TRF:77 (spider flag) | `status='blocked'` | `hotel_room_rate_fetch.status/error_type` | `sources`-level + `room_rate_enrichment_status` | JS:1926 «Trip.com временно ограничил…», 273-277 terminal set | meta | fetch accounting row (attempt/retry_count) | extracted (honest protection signal; 4030 observed) |
| T50 | — (context echo) | TRF:204-213 (task checkIn/checkout/adult/childInfoItems) | booking-context columns | `hotel_room_rate_observation.check_in/check_out/adults/children_count` (guard P:80-85) | rate fields | JS stay context 1673-1675 | meta | context match enforced at persist | extracted |

## 6. Trip — comment endpoint (getHotelCommentInfo, in-page XHR)

Parser TRF:39-51 (JS), 326-356 (Python: best-effort, `except Exception: pass`); merged into outcome `review_info` → `property_details_json` (rate_enrichment.py:226-236) → RM:831-844 → card fields RM:990-991 and `property_details.rating_subscores`.

| # | Raw field | Parser | Normalized | Persisted | API | UI | Scope | Provenance | Status |
|---|---|---|---|---|---|---|---|---|---|
| T51 | `data.commentRating.{ratingLocation,ratingFacility,ratingService,ratingRoom}` | TRF:338-344 (0<v≤10; labels Местоположение/Удобства/Сервис/Чистота) | `review_info.rating_subscores[{name,score}]` | `hotel_room_rate_fetch.property_details_json.rating_subscores` (DB-confirmed) | `hotel.property_details.rating_subscores` (RM:837-838) | JS:1640-1656 (cardSubscores prefers hotel.rating_subscores then property_details), signature 1156 | review | rides rate `observed_at`; **no own timestamp, no confidence field**; best-effort (failures silently dropped, TRF:355-356) | extracted |
| T52 | `data.commentRating.commentLevel` | TRF:351 | `review_level` | property_details_json | `hotel.review_level` (RM:990) / `property_details.review_level` | JS:1642, 1657; signature 1155 | review | same | extracted |
| T53 | `data.totalCount` | TRF:352 | `reviews_total` | property_details_json (DB-confirmed) | `hotel.reviews_total` (RM:991) | **JS: 0 hits — never rendered** (card uses reviews_count T15 instead) | review | same | discarded |
| T54 | `data.commentTagList[].{name,commentCount}` | TRF:345-353 (≤8) | `review_tags[{name,count}]` | property_details_json (DB-confirmed) | `property_details.review_tags` | **JS: 0 hits — never rendered** | review | same | discarded |

## 7. Trip — detail-DOM property path (permanently dead: detail-page navigation forbidden 2026-10-15)

Producer `TRS:454-521 _tripcom_extract_property_details` + `TRS:338-452 normalize_tripcom_property_details` ran only inside `collect_tripcom_room_rates` (detail-page pool, TRS:550-1127), which rate_enrichment.py:299-317 no longer calls (fast path only; detail fallback removed permanently). The in-page fetch path (TRF) extracts **no** property_details — only `review_info`. Rows below document the dead path for completeness.

| # | Raw field | Parser | Normalized | Persisted | API | UI | Scope | Provenance | Status |
|---|---|---|---|---|---|---|---|---|---|
| T55 | ld+json `Hotel.name/description/@type/starRating/geo/image`, meta description | TRS:457-518 | `name, description, category, stars, rating/scale, reviews_count, images, coordinates, address` (TRS:353-400) | would be `hotel_room_rate_fetch.property_details_json` + rating backfill P:101-116 | `property_details.*` | JS:1115-1117, 1640-1656, 1778-1783 (would render) | property | `observed_at`, `source='trip_com_hotel_detail_dom'` | unreliable_unavailable (path dead; only review_info fills property_details today) |
| T56 | `#tripHotelPolicy` Заезд/Выезд/стойка регистрации | TRS:503-505 | `check_in_text, check_out_text, reception_hours` | property_details_json | `property_details.check_in_text/...` | JS:1115-1117 policy (reception_hours: 0 hits) | property | same | unreliable_unavailable |
| T57 | policy breakfast section / `amenityFeature` / `[id^=fac_]` | TRS:483-485, 507-508; TRS:401-425 classify | `breakfast{available,raw_text}`, `amenities[]` (≤80) | property_details_json | `property_details.breakfast/amenities` | JS:1109, 1753-1760 (amenities would render; breakfast: 0 hits) | property | same | unreliable_unavailable |
| T58 | DOM landmark list `hotelLocationNew_*` | TRS:486-500 | `nearby_landmarks[{name,category,distance_text,distance_meters,source='trip_com_displayed_landmark_distance'}]` (TRS:431-451) | property_details_json | `property_details.nearby_landmarks` | **JS: 0 hits** (UI landmark lines come from OSM geo cache only, JS:1703-1747) | property | same | unreliable_unavailable |
| T59 | review link `a[href]` | TRS:481-482; TRS:387-394 (https trip.com host check) | `review_url` | property_details_json | `property_details.review_url` → `hotel.review_url` (RM:994) | JS:1193-1202 reviewLink | review | same | unreliable_unavailable |

---

## Cross-cutting: source / observation-time / confidence handling

| Store | Source provenance | Observation time | Confidence |
|---|---|---|---|
| `hotel_observation` | `source` column ('sravniotel'/'trip_com'); `rating_source`; `coordinate_source` in raw_json | `observed_at` (utc, P:181/TC:1531); upsert keeps newest (P:256-267) | rating: scale + normalized value; stars/rating bounds validated on write AND re-sanitized on read (RM:624-627, 852); coordinate_status/method/confidence (RM:913-933) |
| `hotel_offer_observation` | `source`, `provider`; `meal_source` in raw_json | `observed_at`; dedupe_key hash P:275-283 | `meal_confidence` ('high'/'none'), `meal_normalizer_version` meal_v4; `tax_status/cancellation` only from explicit labels |
| `hotel_room_rate_observation` | `source='trip_com'`, `provider='trip.com'`, `provider_rate_id` | `observed_at` per rate (µs) + unique-key includes observed_at (schema:101-103); read model keeps newest per rate (RM:501-511) | `meal_confidence`; `price_basis` self-flagged "UI cross-check pending" (TRS:285); spider error code → honest 'blocked' |
| `hotel_room_rate_fetch` | `source`, `operation` | `observed_at`, attempt/retry_count (P:92-129; lifecycle rows rate_enrichment.py:38-63) | status taxonomy ok/no_rates/blocked/…; error messages sanitized P:24-52 |
| `hotel_provider_metadata` (Sravni detail) | `provider`, `source` (api/html), `method` | `retrieved_at` (ms) | explicit `confidence:'high'` (SD:191) — the only store with a real confidence field |
| Comment enrichment (`review_info`) | endpoint implicit | none of its own (rides rate observed_at) | none; silently dropped on failure (TRF:355-356) |
| UI | provider labels JS:1401; review_link to source | `freshness()` JS:2007 exists but is never called; `sources[].observed_at` used only inside the revision string JS:2070 | no per-field confidence rendering; unknown values are omitted (policy JS:1764-1771, stars JS:1818-1825, amenities JS:1761) |

## Fields present in raw payloads that never reach the UI (complete list)

1. **Trip `nameInfo.enName`** (T11) — extracted, persisted, API-exposed; neither hotels.js nor hotel_matching.py reads it. Dead by design intent (comment claims it enables matching).
2. **Trip rate `priceExplanation`** (T34), **`totalPriceInfo` charges** (T35), **`saleRoomCategoryList` rate_labels** (T37), **`roomLabelList` availability** (T45), **`priceLabelList` discount labels** (T46), **`roomDescList` descriptions** (T47), **paymentInfo/confirmInfo** (T48), **`rate.id`** (T26) — all normalized into `hotel_room_rate_observation.raw_json` (DB-confirmed) and returned by the API, none rendered.
3. **Trip comment `totalCount`** (T53) and **`commentTagList` review_tags** (T54) — persisted in `property_details_json`, never rendered.
4. **Sravni detail `policy_text`** (S45) — persisted in `hotel_provider_metadata`, merged into `property_details`, never rendered.
5. **Trip DOM «Рядом:» nearby_places** (T17) — persisted and API-exposed but produce no distance (no `distance_meters`) and the UI's only nearby_places consumer filters `type=city_center`.
6. **Trip `room_amenities`** (T43) — persisted, API-exposed via `room_options[].amenities`; UI only counts `amenities?.length` inside the render signature (JS:1175), never displays them.
7. Provider-context fields carried in offers but not rendered: `payment_method`, `valid_until`, `taxes`/`fees` (S39, S40, S38 — never present in the two live sources anyway), `meal_source`/`meal_confidence`/`meal_normalizer_version`/`breakfast_price` (internal, 0 UI hits; `meal_extra_amount/currency` IS rendered JS:1257-1259).

## Row counts

Total: **106 rows** — Sravni 47 (S1-S47: list 28, offer 12, detail 7) · Trip 59 (T1-T59: list 24, rate 26, comment 4, dead detail-DOM path 5).

| Status | Sravni | Trip | Total |
|---|---|---|---|
| extracted | 27 | 41 | **68** |
| discarded | 1 | 13 | **14** |
| unreliable_unavailable | 19 | 5 | **24** |
| requires_investigation | 0 | 0 | **0** |

Every field was traced to a verified file:line, so no row needed requires_investigation. The four detail-DOM rows T55-T59 are classified unreliable_unavailable rather than requires_investigation because the producing path is verifiably unreachable (rate_enrichment.py:299-317 calls only the in-page fetch; the detail-page pool is never invoked and the fallback is permanently forbidden).

## Notes / caveats

- The Sravni list normalization (sravni.py:156-158) runs once in the source adapter but the persisted normalization re-runs inside `_persist_page` (P:231) **with the query** — so check_in/check_out/adults/children/rooms/city come from the session query, not the provider. The Sravni adapter's own normalized hotels are used only for the SourceResult count.
- `hotel.rating_by_source` (PL:108-116) and `hotel.review_stats` both exist in the API; hotels.js renders only `review_stats` (JS:1135, 1206, 1660, 1979). `rating_by_source` is API-only.
- API response is assembled in three places: full read model (A:216-245 via `_finalize_search`), scoped rate results (A:505-526 — note this scoped shape does NOT include `name`, `offer_tags`, `landmarks_desc`, `review_summary_line`, so the JS background-merge (JS:936-964) only updates room_options/room_rates/property_details/stars/rating/reviews/address/images/amenities from it, by design).
- `_finalize_search` pops `raw_payload` from the API response (PL:167) and read_model removed it earlier (RM:966-968 comment) — raw payloads stay in SQLite only.
- UI freshness/observation-time display is effectively absent: `freshness()` (JS:2007) is defined but never invoked.
