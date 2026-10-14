"""The fast-path fetch body must carry per-task occupancy and dates.

Round-2 audit (2026-10-15): _FETCH_JS overwrote only body.search.hotelId.
The captured template belonged to hotels[0] with children=0, so a
children_count=1 search (live smoke rate-pool-smoke2, 296 persisted
observations) actually requested rates for the wrong guest composition
while persistence recorded the task's composition. The fetch body now
overwrites checkIn/checkOut/adult per task, and childInfoItems only when
the app knows real ages (inventing ages would fabricate the composition).
"""
from __future__ import annotations

from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "aviasales_collector" / "hotels" / "sources" / "tripcom_rate_fetch.py"


def test_fetch_js_overwrites_per_task_fields():
    code = SOURCE.read_text(encoding="utf-8")
    # hotelId, dates, and occupancy must all be per-task on the fetch body
    assert "body.search.hotelId = Number(task.hotelId);" in code
    assert "body.search.checkIn = task.checkIn;" in code
    assert "body.search.checkOut = task.checkOut;" in code
    assert "body.search.adult = task.adult;" in code
    # childInfoItems is copied only when explicitly provided (ages known)
    assert "task.childInfoItems !== undefined" in code


def test_fetch_js_template_not_reused_between_tasks():
    # The body must be a deep copy of the template, so per-task overwrites
    # cannot leak into the next hotel's request.
    code = SOURCE.read_text(encoding="utf-8")
    assert "JSON.parse(JSON.stringify(window.__rateTemplate))" in code


def test_capture_url_uses_task_children_count():
    code = SOURCE.read_text(encoding="utf-8")
    # The capture URL must reflect the task's guest composition (the site
    # builds childInfoItems itself from children=N); children=0 hardcoded
    # would capture a template for the wrong composition.
    assert '"children=0"' not in code
    assert 'f"children={children_count}"' in code


def test_fetch_task_includes_dates_without_dashes():
    code = SOURCE.read_text(encoding="utf-8")
    # The endpoint's search block uses YYYYMMDD (see the captured template).
    assert '.replace("-", "")' in code


def test_fetch_task_only_sets_child_info_items_with_real_ages():
    code = SOURCE.read_text(encoding="utf-8")
    # No default/placeholder age may be invented for children without ages.
    assert '"age": 6' not in code
    assert "default" not in code.split("children_ages")[0].splitlines()[-1]
