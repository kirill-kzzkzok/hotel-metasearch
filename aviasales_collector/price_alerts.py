"""Deterministic deal scoring for observed route prices.

The previous observation remains a useful signal, but it is not the sole
reference: a rolling median protects against gradual price declines being
forgotten when each scan updates the last-known price.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Mapping, Sequence


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def analyze_deal(
    current_price: int,
    currency: str,
    history: Sequence[Mapping[str, Any]],
    *,
    historical_summary: Mapping[str, Any] | None = None,
    minimum_reference_drop_percent: float = 12.0,
) -> dict[str, Any]:
    """Compare a valid current observation with earlier observations only."""
    prices = [
        int(item["price"])
        for item in history
        if isinstance(item.get("price"), int) and item["price"] > 0
        and str(item.get("currency") or "").upper() == currency.upper()
    ][-30:]
    summary = historical_summary or {}
    count = int(summary.get("observation_count", len(prices)))
    reference = float(median(prices[-30:])) if prices else None
    historical_low = summary.get("historical_low", min(prices) if prices else None)
    previous = prices[-1] if prices else None
    saving = max(0.0, reference - current_price) if reference is not None else 0.0
    reference_drop = ((reference - current_price) / reference) if reference else 0.0
    previous_change = (current_price - previous) if previous is not None else None
    previous_change_percent = (
        (previous_change / previous * 100) if previous else None
    )
    higher_count = int(summary.get("higher_count", sum(price > current_price for price in prices)))
    percentile = higher_count / count if count else 0.0
    low_closeness = 0.0
    if reference is not None and historical_low is not None and current_price <= reference:
        spread = reference - historical_low
        low_closeness = 1.0 if spread <= 0 else _clamp(1 - (current_price - historical_low) / spread)

    # A timestamp span is deliberately not inferred from record counts. The DB
    # supplies real timestamps for confidence; pure tests can omit them.
    valid_times = sorted(
        str(item["observed_at"])
        for item in history
        if item.get("observed_at") and isinstance(item.get("price"), int)
        and item["price"] > 0 and str(item.get("currency") or "").upper() == currency.upper()
    )
    span_hours = 0.0
    first_at = summary.get("first_observed_at") or (valid_times[0] if valid_times else None)
    last_at = summary.get("last_observed_at") or (valid_times[-1] if valid_times else None)
    if first_at and last_at:
        from datetime import datetime

        try:
            first = datetime.fromisoformat(str(first_at).replace("Z", "+00:00"))
            last = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            span_hours = max(0.0, (last - first).total_seconds() / 3600)
        except ValueError:
            span_hours = 0.0
    distinct_count = int(summary.get("distinct_count", len(set(prices))))
    distinct_factor = _clamp((distinct_count - 1) / 3)
    confidence = min(1.0, count / 8) * distinct_factor * min(1.0, span_hours / 6)

    normal_level_points = 30 * _clamp(reference_drop / 0.30)
    # Both price-drop score components must use the same historical baseline;
    # previous-to-current movement is reported separately and never scores.
    movement_points = 15 * _clamp(reference_drop / 0.25)
    if currency.upper() == "RUB":
        saving_points = 20 * _clamp(saving / 2_000)
        affordability_points = 8 * _clamp((50_000 - current_price) / 50_000)
        min_saving = max(300.0, (reference or current_price) * 0.05)
    else:
        saving_points = 20 * _clamp(saving / max(1.0, (reference or current_price) * 0.25))
        affordability_points = 0.0
        min_saving = max(1.0, (reference or current_price) * 0.05)
    low_points = 15 * low_closeness
    percentile_points = 10 * percentile
    confidence_points = 10 * confidence
    score = round(min(100.0, sum((
        normal_level_points, movement_points, saving_points, low_points,
        percentile_points, confidence_points, affordability_points,
    ))), 1)

    threshold = max(0.0, float(minimum_reference_drop_percent)) / 100
    threshold_met = reference_drop >= threshold
    # Keep the existing strong-single-event escape hatch and its absolute
    # saving guard, but make its percentage condition use the same configured
    # threshold as the historical-reference path.
    strong_single_drop = bool(
        reference is not None
        and reference_drop >= threshold
        and saving >= max(1_000, min_saving)
    )
    enough_reference = count >= 2 and reference_drop >= threshold and saving >= min_saving
    finding = bool(score >= 68 and (enough_reference or strong_single_drop))
    if finding:
        reason = "strong_single_drop" if strong_single_drop and not enough_reference else "historical_value_and_price_event"
    elif count < 3 and not strong_single_drop:
        reason = "insufficient_baseline"
    elif reference is not None and reference_drop < threshold:
        reason = "not_below_historical_reference"
    elif saving < min_saving:
        reason = "absolute_saving_too_small"
    else:
        reason = "deal_score_below_alert_level"

    return {
        "observation_count": count,
        "baseline_confidence": round(confidence, 3),
        "reference_price": round(reference, 2) if reference is not None else None,
        "historical_low": historical_low,
        "previous_price": previous,
        "reference_drop_amount": round(reference - current_price, 2) if reference is not None else None,
        "reference_drop_percent": round(reference_drop * 100, 2),
        "previous_change_amount": round(previous_change, 2) if previous_change is not None else None,
        "previous_change_percent": (
            round(previous_change_percent, 2) if previous_change_percent is not None else None
        ),
        # Keep the established DB column names as aliases, but define them
        # unambiguously as reference-based drops.
        "drop_amount": round(reference - current_price, 2) if reference is not None else None,
        "drop_percent": round(reference_drop * 100, 2),
        "saving_amount": round(saving, 2),
        "minimum_reference_drop_percent": float(minimum_reference_drop_percent),
        "threshold_met": threshold_met,
        "historical_percentile": round(percentile * 100, 1),
        "deal_score": score,
        "finding": finding,
        "reason": reason,
    }


def materially_better_price(price: int, last_alert_price: int | None, currency: str) -> bool:
    """Allow a fresh alert for a meaningfully cheaper step in an active deal."""
    if last_alert_price is None:
        return True
    saving = last_alert_price - price
    minimum = max(300, last_alert_price * 0.04) if currency.upper() == "RUB" else last_alert_price * 0.04
    return saving >= minimum
