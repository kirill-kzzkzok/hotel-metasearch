"""Regression guard: real Trip.com browser sessions must always be headed.

The mission spec forbids headless Chromium anywhere on the real Trip.com path
(discovery, rate tasks, retries, recovery, login). Both launch sites use
persistent contexts on the authorized Trip profile; if either regresses to
headless, this test fails without launching a browser.
"""
import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIP_PATHS = [
    "aviasales_collector/hotels/sources/tripcom.py",
    "aviasales_collector/hotels/sources/tripcom_rates.py",
    "aviasales_collector/tripcom_login.py",
]


def _persistent_context_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "launch_persistent_context"
    ]


@pytest.mark.parametrize("relative_path", TRIP_PATHS)
def test_tripcom_persistent_context_launches_are_headed(relative_path):
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    calls = _persistent_context_calls(ast.parse(source))
    assert calls, f"no launch_persistent_context found in {relative_path}"

    for call in calls:
        headless = None
        for keyword in call.keywords:
            if keyword.arg == "headless":
                headless = keyword.value
        assert headless is not None, (
            f"{relative_path}: launch_persistent_context must set headless explicitly"
        )
        assert isinstance(headless, ast.Constant) and headless.value is False, (
            f"{relative_path}: real Trip.com sessions must be headed "
            f"(headless=False), got headless={ast.dump(headless)}"
        )


def test_no_headless_fallback_on_trip_launch_error():
    """A failed headed launch must not fall back to a headless browser."""
    for relative_path in TRIP_PATHS:
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "headless=True" not in source, (
            f"{relative_path}: headless=True found on the real Trip.com path"
        )
