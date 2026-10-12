import copy

import pytest

from aviasales_collector.hotel_matching import (
    attach_match_candidates,
    compare_hotels,
    current_auto_match_ids,
    merge_auto_matched_hotels,
    normalize_hotel_name,
)


def hotel(source, hotel_id, name, address=None, stars=None):
    return {"source": source, "source_hotel_id": hotel_id, "name": name,
            "address": address, "stars": stars}


def test_name_normalization_handles_case_punctuation_and_cyrillic():
    assert normalize_hotel_name("Отель Holiday-Inn Dubai") == "holiday inn dubai"
    assert normalize_hotel_name("Holiday Inn Dubai JVC") == "holiday inn dubai jumeirah village circle"


def test_name_only_match_is_candidate_but_never_automatic_merge():
    candidate = compare_hotels(
        hotel("sravniotel", "1", "Holiday Inn Dubai Jumeirah Village Circle"),
        hotel("trip_com", "2", "Holiday Inn Dubai Jumeirah Village Circle by IHG"),
    )
    assert candidate is not None
    assert candidate["decision"] == "review"


def test_matching_name_and_address_can_auto_match():
    candidate = compare_hotels(
        hotel("sravniotel", "1", "Rove Expo City", "Al Wasl Plaza Expo 2020, Dubai"),
        hotel("trip_com", "2", "Rove Expo City", "Al Wasl Plaza Expo 2020, Dubai"),
    )
    assert candidate is not None
    assert candidate["decision"] == "auto_match"


def test_exact_full_name_city_and_stars_can_confirm_a_match_without_address():
    left = hotel("sravniotel", "1", "Holiday Inn Dubai Jumeirah Village Circle by IHG", stars=4)
    right = hotel("trip_com", "2", "Holiday Inn Dubai Jumeirah Village Circle by IHG", stars=4)
    left.update(city="Dubai", country="UAE")
    right.update(city="Dubai", country="United Arab Emirates")

    candidate = compare_hotels(left, right)

    assert candidate is not None
    assert candidate["features"]["country"] == 1
    assert candidate["decision"] == "auto_match"
    assert candidate["confidence"] >= 0.97


def test_exact_name_does_not_override_city_or_country_conflicts():
    left = hotel("sravniotel", "1", "Holiday Inn Dubai Jumeirah Village Circle by IHG", stars=4)
    right = hotel("trip_com", "2", "Holiday Inn Dubai Jumeirah Village Circle by IHG", stars=4)
    left.update(city="Dubai", country="United Arab Emirates")
    right.update(city="Abu Dhabi", country="United Arab Emirates")
    assert compare_hotels(left, right) is None

    right["city"] = "Dubai"
    right["country"] = "Thailand"
    assert compare_hotels(left, right) is None


def test_same_source_ids_are_not_cross_source_match_candidates():
    assert compare_hotels(hotel("sravniotel", "1", "Rove"), hotel("sravniotel", "2", "Rove")) is None


def test_candidate_collection_never_auto_merges_ambiguous_name_only_matches():
    hotels = [
        hotel("sravniotel", "1", "Rove Downtown"),
        hotel("sravniotel", "2", "Rove Downtown"),
        hotel("trip_com", "3", "Rove Downtown"),
    ]
    attach_match_candidates(hotels)
    assert hotels[2]["match_candidates"]
    assert all(candidate["decision"] == "review" for candidate in hotels[2]["match_candidates"])


def test_identical_match_evidence_reuses_scores_and_changed_coordinates_recompute():
    from aviasales_collector import hotel_matching

    hotel_matching._compare_cached_evidence.cache_clear()
    try:
        left = hotel("sravniotel", "101", "Rove Downtown", "Downtown Dubai Road 1", 4)
        right = hotel("trip_com", "202", "Rove Downtown", "Downtown Dubai Road 1", 4)
        left["coordinates"] = {"lat": 25.2, "lng": 55.27}
        right["coordinates"] = {"lat": 25.2001, "lng": 55.2701}
        expected = compare_hotels(left, right)

        first = copy.deepcopy([left, right])
        attach_match_candidates(first)
        assert first[0]["match_candidates"][0] == expected
        first_misses = hotel_matching._compare_cached_evidence.cache_info().misses

        second = copy.deepcopy([left, right])
        attach_match_candidates(second)
        assert second[0]["match_candidates"][0] == expected
        assert hotel_matching._compare_cached_evidence.cache_info().hits > 0
        assert hotel_matching._compare_cached_evidence.cache_info().misses == first_misses

        changed = copy.deepcopy([left, right])
        changed[1]["coordinates"] = {"lat": 25.22, "lng": 55.29}
        attach_match_candidates(changed)
        assert not changed[0]["match_candidates"]
        assert hotel_matching._compare_cached_evidence.cache_info().misses > first_misses
    finally:
        hotel_matching._compare_cached_evidence.cache_clear()


def test_hilton_hotels_in_different_districts_are_not_matched():
    assert compare_hotels(
        hotel("sravniotel", "1", "Hilton Dubai Marina"),
        hotel("trip_com", "2", "Hilton Dubai Creek"),
    ) is None


def test_shortened_brand_name_with_same_address_stays_a_review_candidate():
    candidate = compare_hotels(
        hotel("sravniotel", "1", "Marina Bay Resort Dubai", "Marina Bay Road, Dubai", 4),
        hotel("trip_com", "2", "Marina Bay Resort", "Marina Bay Road, Dubai", 4),
    )
    assert candidate is not None
    assert candidate["decision"] == "review"


def test_jvc_alias_and_chain_suffix_create_a_review_candidate():
    candidate = compare_hotels(
        hotel("sravniotel", "1", "Holiday Inn Dubai Jumeirah Village Circle by IHG"),
        hotel("trip_com", "2", "Holiday Inn Dubai JVC"),
    )
    assert candidate is not None
    assert candidate["decision"] == "review"
    assert candidate["features"]["name"] >= 0.8
    assert candidate["confidence"] < candidate["score"]


def test_trip_street_context_can_confirm_an_exact_name_match():
    left = hotel("sravniotel", "1", "Anrizon Hotel Nha Trang",
                 "03 - 05 Pham Van Dong Street, Nha Trang")
    right = hotel("trip_com", "2", "Anrizon Hotel Nha Trang")
    right["nearby_places"] = [{"name": "Пляж Фам Ван Донг", "type": "district"}]
    candidate = compare_hotels(left, right)
    assert candidate is not None
    assert candidate["features"]["location_context"] == 1.0
    assert candidate["decision"] == "auto_match"
    assert candidate["confidence"] >= 0.97


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Grand Hotel", "Grand Hotel City"),
        ("Grand Hotel Downtown", "Grand Hotel Residence"),
        ("Grand Hyatt Dubai", "Grand Hyatt Residence Dubai"),
        ("Grand Hotel", "Grand Hotel Downtown"),
    ],
)
def test_distinctive_name_qualifiers_block_false_merges(left, right):
    assert compare_hotels(
        hotel("sravniotel", "1", left, "Same Street 1"),
        hotel("trip_com", "2", right, "Same Street 1"),
    ) is None


def test_coordinates_phone_and_website_are_strong_independent_evidence():
    left = hotel("sravniotel", "1", "Rove Expo City", "Dubai, Expo Road 1", 4)
    right = hotel("trip_com", "2", "Rove Expo City", "Dubai, Expo Road 1", 4)
    left.update(coordinates={"lat": 25.026, "lng": 55.145}, phone="+971 4 555 0101", website="https://rovehotels.com")
    right.update(coordinates={"latitude": 25.0261, "longitude": 55.1451}, phone="97145550101", website="https://www.rovehotels.com/rooms")
    candidate = compare_hotels(left, right)
    assert candidate is not None
    assert candidate["decision"] == "auto_match"
    assert candidate["features"]["coordinates"] > 0.8
    assert candidate["features"]["phone"] == 1
    assert candidate["features"]["website"] == 1


def test_conflicting_phone_or_property_coordinates_rejects_candidate():
    left = hotel("sravniotel", "1", "Rove Downtown", "Dubai, Street 1")
    right = hotel("trip_com", "2", "Rove Downtown", "Dubai, Street 1")
    left["phone"], right["phone"] = "+97145550101", "+97145550202"
    assert compare_hotels(left, right) is None
    left.pop("phone")
    right.pop("phone")
    left["coordinates"], right["coordinates"] = [25.0, 55.0], [25.02, 55.02]
    assert compare_hotels(left, right) is None


def test_enrichment_fields_do_not_change_source_match_or_canonical_id():
    left = hotel("sravniotel", "101", "Rove Downtown", "Downtown Dubai Road 1", 4)
    right = hotel("trip_com", "202", "Rove Downtown", "Downtown Dubai Road 1", 4)
    left["_match_evidence"] = {
        "address": left["address"], "stars": 4, "coordinates": None,
        "images": [{"url": "https://example.test/source-image.jpg"}],
    }
    right["_match_evidence"] = {
        "address": right["address"], "stars": 4, "coordinates": None,
        "images": [{"url": "https://example.test/source-image.jpg"}],
    }
    initial = copy.deepcopy([left, right])
    attach_match_candidates(initial)
    initial_ids = current_auto_match_ids(initial)
    assert initial_ids[("sravniotel", "101")] == initial_ids[("trip_com", "202")]

    enriched = copy.deepcopy([left, right])
    enriched[0].update({
        "address": "Geo detail address changed",
        "stars": 3,
        "coordinates": {"lat": 25.2, "lng": 55.27},
        "geocoded_coordinates": {"lat": 25.2, "lng": 55.27},
        "images": [{"url": "https://example.test/enrichment-image.jpg"}],
    })
    enriched[1].update({
        "address": "Different rate detail address",
        "stars": 5,
        "coordinates": {"lat": 40.7, "lng": -74.0},
        "geocoded_coordinates": {"lat": 40.7, "lng": -74.0},
        "images": [{"url": "https://example.test/other-enrichment-image.jpg"}],
    })
    attach_match_candidates(enriched)

    assert enriched[0]["match_candidates"][0]["decision"] == "auto_match"
    assert current_auto_match_ids(enriched) == initial_ids


def test_historical_canonical_ids_do_not_link_a_current_review_candidate():
    left = hotel("sravniotel", "1", "Same Hotel")
    right = hotel("trip_com", "2", "Same Hotel")
    left["canonical_hotel_id"] = right["canonical_hotel_id"] = "old-link"
    attach_match_candidates([left, right])
    assert left["match_candidates"][0]["decision"] == "review"
    ids = current_auto_match_ids([left, right])
    assert ids[("sravniotel", "1")] != ids[("trip_com", "2")]
    assert "old-link" not in ids.values()


def test_current_auto_match_gets_stable_canonical_identity():
    left = hotel("sravniotel", "1", "Rove Expo City", "Al Wasl Plaza Expo 2020, Dubai", 4)
    right = hotel("trip_com", "2", "Rove Expo City", "Al Wasl Plaza Expo 2020, Dubai", 4)
    attach_match_candidates([left, right])
    ids = current_auto_match_ids([left, right])
    assert ids[("sravniotel", "1")] == ids[("trip_com", "2")]
    assert ids == current_auto_match_ids([left, right])


def test_auto_match_merges_source_records_without_losing_any_offers():
    from aviasales_collector.hotel_search import _location_summary

    left = hotel("sravniotel", "101", "Marina Bay Resort Dubai", "Marina Bay Road, Dubai", 4)
    right = hotel("trip_com", "202", "Marina Bay Resort Dubai", "Marina Bay Road, Dubai", 4)
    left["nearby_places"] = []
    right["nearby_places"] = []
    left["rating"] = 8.8
    left["rating_scale"] = 10
    left["reviews_count"] = 100
    right["rating"] = 8.9
    right["rating_scale"] = 10
    right["reviews_count"] = 80
    left["geocoded_coordinates"] = {"lat": 12.273566, "lng": 109.202454}
    left["geocoded_coordinate_source"] = "trip_com_detail"
    left["cached_beach_distance_meters"] = 650
    left["cached_beach_name"] = "Verified Beach"
    left["cached_beach_source"] = "openstreetmap_geometry"
    left["cached_beach_method"] = "nearest_osm_beach_geometry"
    left["provider_details"] = [{
        "provider": "tutu.ru", "source": "tutu.ru_public_hotel_schema",
        "method": "schema_org_json_ld_hotel", "hotel_amenities": ["Бассейн"],
    }]
    left["offers"] = [
        {"source": "sravniotel", "provider": "yandex", "amount": "52000", "currency": "RUB"},
        {"source": "sravniotel", "provider": "ostrovok", "amount": "53000", "currency": "RUB"},
        {"source": "sravniotel", "provider": "tutu", "amount": "54000", "currency": "RUB"},
    ]
    right["offers"] = [
        {"source": "trip_com", "provider": "trip.com", "amount": "49900", "currency": "RUB"},
    ]
    hotels = [left, right]
    attach_match_candidates(hotels)
    assert all(item["match_candidates"][0]["decision"] == "auto_match" for item in hotels)
    for item in hotels:
        item["canonical_hotel_id"] = "canonical-123"

    result = merge_auto_matched_hotels(hotels)

    assert len(result) == 1
    canonical = result[0]
    assert canonical["source"] == "canonical"
    assert canonical["sources"] == ["sravniotel", "trip_com"]
    assert {record["source_hotel_id"] for record in canonical["source_records"]} == {"101", "202"}
    assert {offer["provider"] for offer in canonical["offers"]} == {"yandex", "ostrovok", "tutu", "trip.com"}
    assert len(canonical["offers"]) == 4
    assert canonical["provider_details"][0]["hotel_amenities"] == ["Бассейн"]
    assert next(record for record in canonical["source_records"]
                if record["source"] == "sravniotel")["provider_details"] == canonical["provider_details"]
    assert {rating["source"] for rating in canonical["rating_by_source"]} == {"sravniotel", "trip_com"}
    left_record = next(record for record in canonical["source_records"] if record["source"] == "sravniotel")
    assert left_record["geocoded_coordinates"] == {"lat": 12.273566, "lng": 109.202454}
    location = _location_summary(canonical, canonical["source_records"])
    assert location["coordinates"] == {"lat": 12.273566, "lng": 109.202454}
    assert location["distance_to_beach_km"] == 0.65
    assert location["beach_name"] == "Verified Beach"


@pytest.mark.parametrize("method", ["nearest_osm_beach_point", "nearest_osm_beach_geometry"])
def test_location_summary_does_not_reuse_incomplete_photon_distance(method):
    from aviasales_collector.hotel_search import _location_summary

    record = hotel("sravniotel", "point-only", "Beachside Hotel")
    record.update({
        "cached_beach_distance_meters": 1746.3795,
        "cached_beach_name": "My An Beach",
        "cached_beach_source": "openstreetmap_photon_geometry",
        "cached_beach_status": "ok",
        "cached_beach_method": method,
        "cached_beach_geometry_available": True,
    })

    location = _location_summary(record, [record])

    assert location["distance_to_beach_km"] is None
    assert location["beach_reference"] is None
    assert location["beach_status"] == "unknown"
    assert location["beach_geometry_available"] is None
