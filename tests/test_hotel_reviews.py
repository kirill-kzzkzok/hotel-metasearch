from aviasales_collector.hotel_reviews import analyze_review_quotes


def test_review_themes_require_source_quotes_and_expose_evidence():
    quotes = [
        "Отличное расположение рядом с пляжем",
        "Завтрак вкусный",
        "Номер маленький, ночью шумно",
        "Персонал",
        "Отличный бассейн",
        "Отличное соотношение цены и качества",
    ]

    summary = analyze_review_quotes(quotes, "trip_com", "2026-09-24T12:00:00+00:00")

    assert summary["mentions"] == ["location", "breakfast", "staff", "room", "noise", "pool", "value"]
    assert summary["positive"] == ["location", "breakfast", "pool", "value"]
    assert summary["negative"] == ["room", "noise"]
    assert summary["evidence"]["location"] == [quotes[0]]
    assert summary["source"] == "trip_com"
    assert summary["retrieved_at"] == "2026-09-24T12:00:00+00:00"
    assert summary["sampled_quotes"] == 6


def test_review_theme_polarity_is_not_inferred_from_topic_only_or_negated_praise():
    summary = analyze_review_quotes(
        ["Location", "Не очень чистый номер", "The room was not good"],
        "trip_com",
        "2026-09-24T12:00:00+00:00",
    )

    assert summary["mentions"] == ["location", "cleanliness", "room"]
    assert summary["positive"] == []
    assert summary["negative"] == ["cleanliness", "room"]


def test_empty_review_quotes_stay_unknown():
    summary = analyze_review_quotes([], "trip_com", "2026-09-24T12:00:00+00:00")

    assert summary["status"] == "not_available"
    assert summary["mentions"] == []
    assert summary["positive"] == []
    assert summary["negative"] == []
    assert summary["source"] is None
    assert summary["retrieved_at"] is None
