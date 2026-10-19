"""B01 regression: the real frontend coordinate-stream request must be accepted.

The frontend used to unconditionally send batch_size 25 while the API rejects
streamed batches above 20 (HTTP 422 "Invalid coordinate stream batch size").
That made normal coordinate enrichment never start: hotelReadyForRender waits
for coordinates, the UI retried an intrinsically invalid request and after
three failures marked geo state ERROR. This test pins the cross-layer
contract with the actual request shape the client sends.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from aviasales_collector.web import create_app
from aviasales_collector.hotel_search import init_hotel_store


def _make_client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "b01-contract.sqlite3"
    init_hotel_store(db_path)
    return TestClient(create_app(db_path, frozenset({10})))


def test_frontend_coordinate_batch_contract(tmp_path):
    client = _make_client(tmp_path)
    # The exact frontend shape (hotels.js loadMapCoordinates): stream + 20.
    frontend_body = {"stream": True, "batch_size": 20, "hotel_keys": []}
    response = client.post(
        "/api/hotels/search/00000000-0000-0000-0000-000000000000/coordinates",
        json=frontend_body,
        headers={"Origin": "http://127.0.0.1:8765"},
    )
    assert response.status_code != 422, (
        "frontend coordinate request rejected: " + response.text[:200]
    )


def test_frontend_source_batch_size_matches_api_limit():
    """The shipped hotels.js must not request a batch above the API limit."""
    hotels_js = Path("aviasales_collector/web_assets/hotels.js").read_text(encoding="utf-8")
    assert "batch_size: 25" not in hotels_js, (
        "hotels.js still sends batch_size 25 which the API rejects (> 20)"
    )
    assert "batch_size: 20" in hotels_js, "hotels.js should send batch_size <= 20"
