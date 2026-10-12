from aviasales_collector.hotel_quality import assess_hotel_quality, quality_signal


def test_quality_signals_keep_priceable_hotels_with_sparse_review_data():
    hotels = [
        {"name": "No rating", "rating": None, "rating_scale": None, "reviews_count": 100,
         "offers": [{"amount": "100.00"}]},
        {"name": "No reviews", "rating": 8.8, "rating_scale": 10, "reviews_count": 0,
         "offers": [{"amount": "100.00"}]},
        {"name": "Two reviews", "rating": 9.5, "rating_scale": 10, "reviews_count": 2,
         "offers": [{"amount": "100.00"}]},
        {"name": "Three reviews", "rating": 7.0, "rating_scale": 10, "reviews_count": 3,
         "offers": [{"amount": "100.00"}]},
        {"name": "No offer", "rating": 9.0, "rating_scale": 10, "reviews_count": 50,
         "offers": []},
    ]
    available, signals = assess_hotel_quality(hotels)
    assert [hotel["name"] for hotel in available] == ["No rating", "No reviews", "Two reviews", "Three reviews"]
    assert signals == {"no_rating": 1, "low_reviews": 2, "no_offer": 1}


def test_canonical_entity_can_pass_quality_gate_using_verified_trip_metrics():
    hotel = {
        "source": "canonical",
        "source_records": [
            {"source": "sravniotel", "rating": 8.5, "rating_scale": 10, "reviews_count": 2},
            {"source": "trip_com", "rating": 8.9, "rating_scale": 10, "reviews_count": 120},
        ],
    }
    assert quality_signal(hotel) is None


def test_canonical_entity_without_any_rating_does_not_get_an_imputed_score():
    hotel = {
        "source": "canonical",
        "source_records": [
            {"source": "sravniotel", "rating": None, "rating_scale": None, "reviews_count": 1000},
            {"source": "trip_com", "rating": None, "rating_scale": None, "reviews_count": None},
        ],
    }
    assert quality_signal(hotel) == "no_rating"
