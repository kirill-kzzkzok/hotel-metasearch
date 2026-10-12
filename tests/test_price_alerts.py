from datetime import datetime, timedelta, timezone

from aviasales_collector.price_alerts import analyze_deal, materially_better_price


def _history(prices):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [
        {"price": price, "currency": "RUB", "observed_at": (start + timedelta(hours=index * 8)).isoformat()}
        for index, price in enumerate(prices)
    ]


def test_gradual_price_drop_uses_rolling_reference_not_only_previous():
    result = analyze_deal(5400, "RUB", _history([7100, 6400]))
    assert result["previous_price"] == 6400
    assert result["reference_price"] == 6750
    assert result["reference_drop_amount"] == 1350
    assert result["previous_change_amount"] == -1000
    assert result["finding"] is True
    assert result["reason"] == "historical_value_and_price_event"


def test_extreme_single_observation_drop_is_a_finding():
    result = analyze_deal(11940, "RUB", _history([30431]))
    assert result["reference_drop_amount"] == 18491
    assert result["reference_drop_percent"] == 60.76
    assert result["finding"] is True
    assert result["reason"] == "strong_single_drop"


def test_small_stepwise_changes_do_not_score_as_a_deal():
    result = analyze_deal(9000, "RUB", _history([10000, 9500]))
    assert result["finding"] is False
    assert result["deal_score"] < 70


def test_single_20_percent_drop_is_alertable():
    result = analyze_deal(8000, "RUB", _history([10000]))
    assert result["finding"] is True


def test_repeated_same_price_has_no_additional_saving():
    result = analyze_deal(8000, "RUB", _history([10000, 8000, 8000, 8000]))
    assert result["finding"] is False
    assert result["reason"] == "not_below_historical_reference"


def test_meaningfully_lower_price_can_be_a_new_event_but_tiny_moves_are_deduped():
    assert materially_better_price(5300, 5400, "RUB") is False
    assert materially_better_price(5000, 5400, "RUB") is True


def test_no_baseline_does_not_invent_a_deal():
    result = analyze_deal(8000, "RUB", [])
    assert result["finding"] is False
    assert result["observation_count"] == 0
    assert result["baseline_confidence"] == 0


def test_configured_threshold_boundary_is_inclusive_and_does_not_replace_score_gate():
    history = _history([8500, 9500, 9800, 10000, 10000, 10500, 11000, 10000])
    for price in (9500, 9499, 9400):
        result = analyze_deal(price, "RUB", history, minimum_reference_drop_percent=5)
        assert result["threshold_met"] is True
        # The configured threshold is necessary, not a replacement for score.
        assert result["finding"] is False

    exact = analyze_deal(9000, "RUB", history, minimum_reference_drop_percent=10)
    above = analyze_deal(8999, "RUB", history, minimum_reference_drop_percent=10)
    below = analyze_deal(9500, "RUB", history, minimum_reference_drop_percent=10)
    assert exact["reference_drop_percent"] == 10
    assert exact["threshold_met"] is True
    assert above["threshold_met"] is True
    assert below["threshold_met"] is False

    five_exact = analyze_deal(9500, "RUB", history, minimum_reference_drop_percent=5)
    five_plus = analyze_deal(9499, "RUB", history, minimum_reference_drop_percent=5)
    assert five_exact["reference_drop_percent"] == 5
    assert five_exact["threshold_met"] is True
    assert five_plus["reference_drop_percent"] == 5.01
    assert five_plus["threshold_met"] is True


def test_setting_change_5_to_12_to_20_changes_finding_decision():
    history = _history([8500, 9500, 9800, 10000, 10000, 10500, 11000, 10000])
    results = {
        threshold: analyze_deal(
            8500, "RUB", history, minimum_reference_drop_percent=threshold
        )
        for threshold in (5, 12, 20)
    }
    assert results[5]["threshold_met"] and results[5]["finding"]
    assert results[12]["threshold_met"] and results[12]["finding"]
    assert not results[20]["threshold_met"] and not results[20]["finding"]


def test_reference_and_previous_metrics_are_independent():
    result = analyze_deal(15572, "RUB", _history([10000, 18418, 18419, 18591]))

    assert result["reference_price"] == 18418.5
    assert result["previous_price"] == 18591
    assert result["reference_drop_amount"] == 2846.5
    assert result["reference_drop_percent"] == 15.45
    assert result["previous_change_amount"] == -3019
    assert result["previous_change_percent"] == -16.24
    assert result["drop_amount"] == result["reference_drop_amount"]
    assert result["drop_percent"] == result["reference_drop_percent"]


def test_large_previous_drop_below_reference_threshold_does_not_find():
    result = analyze_deal(
        9500, "RUB", _history([9000, 10000, 30000]),
        minimum_reference_drop_percent=12,
    )

    assert result["previous_change_percent"] == -68.33
    assert result["reference_drop_percent"] == 5
    assert result["threshold_met"] is False
    assert result["finding"] is False


def test_reference_drop_above_threshold_finds_even_if_previous_change_is_small():
    result = analyze_deal(
        9600, "RUB", _history([10000, 12000, 12000, 12000, 10100]),
        minimum_reference_drop_percent=12,
    )

    assert result["reference_drop_percent"] == 20
    assert result["previous_change_percent"] == round((9600 - 10100) / 10100 * 100, 2)
    assert abs(result["previous_change_percent"]) < 5
    assert result["threshold_met"] is True
    assert result["finding"] is True
