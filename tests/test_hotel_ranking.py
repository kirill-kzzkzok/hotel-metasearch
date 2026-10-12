from aviasales_collector.hotel_ranking import rank_hotels


def test_rank_uses_source_specific_bayesian_quality_and_nullable_components():
    hotels = [
        {"source": "sravniotel", "rating": 9.0, "rating_scale": 10, "reviews_count": 300,
         "nearby_places": [{"type": "city_center", "distance_meters": 500}],
         "offers": [{"amount": "100", "currency": "RUB"}]},
        {"source": "trip_com", "rating": None, "rating_scale": None, "reviews_count": None,
         "nearby_places": [], "offers": [{"amount": "200", "currency": "RUB"}]},
        {"source": "sravniotel", "rating": 5.0, "rating_scale": 10, "reviews_count": 3,
         "nearby_places": [], "offers": []},
    ]
    rank_hotels(hotels)
    assert hotels[0]["ranking"]["quality"] > 80
    assert hotels[0]["ranking"]["location"] is not None
    assert hotels[1]["ranking"]["quality"] is None
    assert hotels[1]["ranking"]["location"] is None
    assert hotels[0]["ranking"]["price"] == 100
    assert hotels[0]["ranking"]["review_quality_score"] is None
    assert hotels[2]["ranking"]["price"] is None


def test_unknown_prices_and_bad_distances_do_not_become_zero_scores():
    hotels = [{"source": "trip_com", "rating": None, "rating_scale": None,
               "nearby_places": [{"type": "city_center", "distance_meters": -1}],
               "offers": [{"amount": "0", "currency": "RUB"}]}]
    rank_hotels(hotels)
    assert hotels[0]["ranking"]["best_value"] is None
    assert hotels[0]["ranking"]["location"] is None


def test_quality_ranking_does_not_put_a_very_low_rated_cheap_hotel_first():
    hotels = [
        {"source": "sravniotel", "rating": 9.3, "rating_scale": 10, "reviews_count": 1200,
         "nearby_places": [{"type": "city_center", "distance_meters": 800}],
         "offers": [{"source": "sravniotel", "amount": "12000", "currency": "RUB"}]},
        {"source": "sravniotel", "rating": 5.0, "rating_scale": 10, "reviews_count": 5,
         "nearby_places": [], "offers": [{"source": "sravniotel", "amount": "3000", "currency": "RUB"}]},
    ]
    rank_hotels(hotels)
    assert hotels[0]["ranking"]["quality_score"] > hotels[1]["ranking"]["quality_score"]
    assert set(hotels[0]["ranking"]["weights"]) >= {"rating_score", "review_confidence", "price_score"}


def test_ranking_scores_are_null_when_source_does_not_supply_evidence():
    hotels = [{"source": "trip_com", "rating": None, "rating_scale": None,
               "reviews_count": None, "nearby_places": [], "offers": []}]
    rank_hotels(hotels)
    assert hotels[0]["ranking"]["quality_score"] is None
    assert hotels[0]["ranking"]["rating_score"] is None
    assert hotels[0]["ranking"]["location_score"] is None
