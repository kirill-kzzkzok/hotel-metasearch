"""B02 regression: cached terminal failures must not aggregate to `ok`.

The no-new-tasks branch of enrich_tripcom_room_rates used to answer a
repeated poll with plain `ok` whenever no individual outcome was `blocked`,
even when the cached terminal outcome was a timeout/invalid/http/error with
no usable rate. Clients trusting the aggregate received contradictory
availability/progress. The fix is one semantic aggregator shared by fresh
and reused outcomes: blocked wins, satisfied-family (ok/no_rates/
rate_inventory_absent) yields ok, everything else is partial.
"""
from __future__ import annotations

import pytest

from aviasales_collector.hotels.rate_enrichment import _aggregate_rate_status


def _outcome(status: str) -> dict:
    return {"status": status}


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["ok"], "ok"),
        (["no_rates"], "ok"),
        (["rate_inventory_absent"], "ok"),
        (["ok", "no_rates", "rate_inventory_absent"], "ok"),
        (["blocked"], "blocked"),
        (["ok", "blocked"], "blocked"),
        (["rate_inventory_timeout"], "partial"),
        (["body_timeout"], "partial"),
        (["invalid_response"], "partial"),
        (["http_error"], "partial"),
        (["error"], "partial"),
        (["navigation_error"], "partial"),
        (["ok", "rate_inventory_timeout"], "partial"),
        ([], "ok"),
    ],
)
def test_aggregate_keeps_failure_families_distinct(statuses, expected):
    assert _aggregate_rate_status([_outcome(s) for s in statuses]) == expected


def test_aggregate_missing_status_is_not_ok():
    # A malformed outcome without a status must not be treated as success.
    assert _aggregate_rate_status([{}]) == "partial"
    assert _aggregate_rate_status([{"status": None}]) == "partial"


def test_aggregator_is_shared_by_both_branches():
    """Both return sites must build the aggregate from the same helper."""
    import inspect

    from aviasales_collector.hotels import rate_enrichment

    source = inspect.getsource(rate_enrichment.enrich_tripcom_room_rates)
    assert source.count("_aggregate_rate_status(") >= 2, (
        "fresh and reused outcome branches must both use the semantic aggregator"
    )
