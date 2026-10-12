from aviasales_collector.hotel_search import CityReference, _location_summary


def test_provider_city_center_is_preserved_but_never_used_as_canonical_distance():
    hotel = {"name": "Marina hotel", "address": "Dubai Marina"}
    record = {
        "source": "sravniotel",
        "coordinates": {"lat": 25.0794907, "lng": 55.1430130},
        "nearby_places": [{
            "name": "Palm Jumeirah", "type": "city_center", "distance_meters": 1_981,
        }],
    }
    reference = CityReference(
        "dubai", "united-arab-emirates", "Dubai Downtown", 25.1948428, 55.278195,
        "photon_openstreetmap", "2026-10-02T00:00:00Z", "photon_place_suburb_centroid",
    )

    location = _location_summary(hotel, [record], reference)

    assert location["distance_to_center_km"] == location["our_distance_to_center_km"]
    assert 18.6 < location["distance_to_center_km"] < 18.8
    assert location["distance_to_center_method"] == "haversine"
    assert location["center_reference"]["name"] == "Dubai Downtown"
    assert location["source_center_references"] == [{
        "source": "sravniotel", "name": "Palm Jumeirah",
        "source_distance_meters": 1981.0, "source_distance_km": 1.98,
        "method": "provider_reported",
    }]


def test_provider_distance_does_not_fill_missing_canonical_city_reference():
    location = _location_summary(
        {"name": "Hotel"},
        [{"source": "sravniotel", "coordinates": {"lat": 25.2, "lng": 55.2},
          "nearby_places": [{"name": "Dubai", "type": "city_center", "distance_meters": 700}]}],
    )
    assert location["distance_to_center_km"] == 0.7
    assert location["our_distance_to_center_m"] is None
    assert location["distance_to_center_method"] == "provider_reported"
    assert location["source_center_references"][0]["name"] == "Dubai"
    assert location["source_center_references"][0]["source_distance_meters"] == 700


def test_canonical_center_reference_is_city_scoped():
    reference = CityReference(
        "istanbul", "turkey", "Istanbul center", 41.006381, 28.9758715,
        "photon_openstreetmap", "2026-10-02T00:00:00Z", "photon_place_centroid",
    )
    location = _location_summary(
        {"name": "Hotel"},
        [{"source": "trip_com", "coordinates": {"lat": 41.0061, "lng": 28.9817},
          "nearby_places": []}],
        reference,
    )
    assert location["center_reference"]["city_slug"] == "istanbul"
    assert 0.4 < location["distance_to_center_km"] < 0.6
