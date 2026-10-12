from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


# Transparent first-pass product weights; they are heuristics, not learned probabilities.
_RANKING_WEIGHTS = {
    "rating_score": 0.60,
    "review_confidence": 0.10,
    "location_score": 0.15,
    "price_score": 0.15,
}
_RATING_PRIOR_REVIEWS = 50


def rank_hotels(hotels: list[dict[str, Any]]) -> None:
    prior_by_source: dict[str, list[float]] = defaultdict(list)
    prices_by_group: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    for hotel in hotels:
        rating, scale = hotel.get("rating"), hotel.get("rating_scale")
        normalized_rating = hotel.get("rating_normalized")
        if isinstance(normalized_rating, (int, float)) and math.isfinite(normalized_rating):
            rating_source = hotel.get("rating_source") or hotel.get("source")
            prior_by_source[rating_source].append(max(0.0, min(1.0, normalized_rating / 10)))
        elif isinstance(rating, (int, float)) and scale:
            rating_source = hotel.get("rating_source") or hotel["source"]
            prior_by_source[rating_source].append(max(0.0, min(1.0, rating / scale)))
        for offer in hotel.get("offers", []):
            try:
                amount = float(offer.get("amount"))
            except (TypeError, ValueError):
                continue
            if offer.get("currency") and math.isfinite(amount) and amount > 0:
                key = (offer.get("source") or hotel["source"], offer["currency"],
                       offer.get("tax_status", "unknown"), offer.get("meal_plan", "unknown"),
                       offer.get("cancellation", "unknown"))
                prices_by_group[key].append(amount)

    priors = {source: sum(values) / len(values) for source, values in prior_by_source.items()}
    price_ranks = {key: sorted(set(values)) for key, values in prices_by_group.items()}
    for hotel in hotels:
        components: dict[str, float] = {}
        rating, scale = hotel.get("rating"), hotel.get("rating_scale")
        normalized_rating = hotel.get("rating_normalized")
        if isinstance(normalized_rating, (int, float)) and math.isfinite(normalized_rating):
            observed = max(0.0, min(1.0, normalized_rating / 10))
            try:
                reviews = max(0, int(hotel.get("reviews_count") or 0))
            except (TypeError, ValueError):
                reviews = 0
            source = hotel.get("rating_source") or hotel.get("source")
            prior = priors.get(source, observed)
            bayes = (reviews * observed + _RATING_PRIOR_REVIEWS * prior) / (reviews + _RATING_PRIOR_REVIEWS)
            components["rating_score"] = bayes * 100
            components["review_confidence"] = reviews / (reviews + _RATING_PRIOR_REVIEWS)
        elif isinstance(rating, (int, float)) and scale:
            observed = max(0.0, min(1.0, rating / scale))
            try:
                reviews = max(0, int(hotel.get("reviews_count") or 0))
            except (TypeError, ValueError):
                reviews = 0
            source = hotel.get("rating_source") or hotel["source"]
            prior = priors.get(source, observed)
            bayes = (reviews * observed + _RATING_PRIOR_REVIEWS * prior) / (reviews + _RATING_PRIOR_REVIEWS)
            components["rating_score"] = bayes * 100
            components["review_confidence"] = reviews / (reviews + _RATING_PRIOR_REVIEWS)

        distances = [
            float(place["distance_meters"])
            for place in hotel.get("nearby_places", [])
            if place.get("type") == "city_center"
            and isinstance(place.get("distance_meters"), (int, float))
            and place["distance_meters"] >= 0
        ]
        if distances:
            components["location_score"] = 100 * math.exp(-min(distances) / 5000)

        hotel_prices: list[tuple[tuple[str, str, str, str, str], float]] = []
        for offer in hotel.get("offers", []):
            try:
                amount = float(offer.get("amount"))
            except (TypeError, ValueError):
                continue
            currency = offer.get("currency")
            if currency and math.isfinite(amount) and amount > 0:
                key = (offer.get("source") or hotel["source"], currency,
                       offer.get("tax_status", "unknown"), offer.get("meal_plan", "unknown"),
                       offer.get("cancellation", "unknown"))
                hotel_prices.append((key, amount))
        if hotel_prices:
            scores = []
            for group, amount in hotel_prices:
                cohort = price_ranks[group]
                rank = cohort.index(amount)
                scores.append(100.0 if len(cohort) <= 1 else 100 * (1 - rank / (len(cohort) - 1)))
            components["price_score"] = max(scores)

        available_weight = sum(_RANKING_WEIGHTS[key] for key in components)
        quality_score = (
            sum(_RANKING_WEIGHTS[key] * value for key, value in components.items()) / available_weight
            if available_weight else None
        )
        reasons = []
        if components.get("rating_score", 0) >= 80:
            reasons.append("high_rating")
        if components.get("location_score", 0) >= 70:
            reasons.append("near_center")
        if components.get("price_score", 0) >= 70:
            reasons.append("competitive_price")
        hotel["ranking"] = {
            **{key: components.get(key) for key in _RANKING_WEIGHTS},
            **components,
            "quality": round(components["rating_score"], 2) if "rating_score" in components else None,
            "location": round(components["location_score"], 2) if "location_score" in components else None,
            "price": round(components["price_score"], 2) if "price_score" in components else None,
            "quality_score": round(quality_score, 2) if quality_score is not None else None,
            "best_value": round(quality_score, 2) if quality_score is not None else None,
            "review_quality_score": None,
            "confidence": round(available_weight, 4) if available_weight else 0,
            "match_confidence": hotel.get("match_confidence"),
            "weights": {key: value for key, value in _RANKING_WEIGHTS.items() if key in components},
            "reasons": reasons,
        }
