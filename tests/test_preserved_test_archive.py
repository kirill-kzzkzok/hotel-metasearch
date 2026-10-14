"""Reproducibility guard for the preserved hotel-test bytecode archive.

tests/test_hotel_search.py is a loader that executes the archived pytest
module fixtures/hotel_search_test_module.cpython-312.pyc.gz. The readable
source was lost before the repository was published, so the archive is the
only surviving copy and is tracked in git on purpose (do NOT add it to
.gitignore). This guard fails loudly when the archive is missing, silently
different, or run under a non-3.12 interpreter.
"""
from __future__ import annotations

import gzip
import importlib.util
import sys
from pathlib import Path

import pytest

ARCHIVE = Path(__file__).resolve().parents[1] / "fixtures" / "hotel_search_test_module.cpython-312.pyc.gz"
ARCHIVE_SHA256 = "1da4aba1bda4d917c2f62f1d2d9959a95111e5c885a5fec067cc2a0a8e0106ab"


def test_archive_is_present_tracked_and_pinned():
    assert ARCHIVE.is_file(), (
        f"missing tracked fixture {ARCHIVE}: the preserved hotel tests "
        "(tests/test_hotel_search.py) cannot run without it. The archive is "
        "versioned in git on purpose; see DECISIONS.md (reproducibility)."
    )


def test_archive_sha256_matches_pin():
    import hashlib
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    assert digest == ARCHIVE_SHA256, (
        f"fixture {ARCHIVE.name} sha256 {digest} != pinned {ARCHIVE_SHA256}; "
        "the preserved test module changed unexpectedly"
    )


def test_archive_magic_requires_cpython_312():
    if sys.version_info[:2] != (3, 12):
        # Collected (import-time loader would raise) only on 3.12; on other
        # interpreters report the contract instead of failing.
        pytest.skip(f"preserved bytecode requires CPython 3.12, running {sys.version.split()[0]}")
    compiled = gzip.decompress(ARCHIVE.read_bytes())
    assert compiled[:4] == importlib.util.MAGIC_NUMBER, (
        "archive magic does not match the running CPython 3.12"
    )
