"""B12 regression: one stable headed rate page, no worker-tab pool.

The collector used to open up to four visible worker tabs (plus the
template-capture detail navigation) while one global schedule lock
serialized fetch+pause across all of them - extra navigation and failure
surface with no actual fetch concurrency. The plan's smallest fix: fetch
from ONE stable headed page with the existing bounded pacing.
"""
from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "aviasales_collector" / "hotels" / "sources" / "tripcom_rate_fetch.py"


def test_single_worker_page_no_tab_pool():
    code = SOURCE.read_text(encoding="utf-8")
    # The worker pool loop (context.new_page per extra worker) must be gone.
    assert "worker_pages.append(await context.new_page())" not in code, (
        "worker tab pool still creates extra pages"
    )
    # All fetches reuse the single stable page.
    assert "worker_pages = [page]" not in code or "effective_concurrency" not in code


def test_concurrency_capped_at_one_page():
    code = SOURCE.read_text(encoding="utf-8")
    # effective_concurrency must no longer multiply browser tabs.
    assert "min(int(concurrency or 1), 4," not in code, (
        "concurrency still opens up to 4 tabs"
    )


def test_inter_request_pacing_retained():
    code = SOURCE.read_text(encoding="utf-8")
    # The bounded pacing (gentle request rate) must survive the restructure.
    assert "TRIPCOM_RATE_FETCH_INTER_REQUEST_DELAY_SECONDS" in code
    assert "async with _REQUEST_SCHEDULE_LOCK" in code
