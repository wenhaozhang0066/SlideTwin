"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA_DIR = Path(os.environ.get("SLIDETWIN_TESTDATA_DIR", REPO_ROOT / "data" / "testdata"))
CMU_SMALL = TESTDATA_DIR / "openslide-testdata" / "CMU-1-Small-Region.svs"


@pytest.fixture(scope="session")
def cmu_small_path() -> Path:
    """Path to the CC0 Aperio test slide (0.499 um/px, one level).

    Skips when the file is missing, unless SLIDETWIN_REQUIRE_TESTDATA=1 (CI), where a
    missing file is an error.
    """
    if not CMU_SMALL.exists():
        if os.environ.get("SLIDETWIN_REQUIRE_TESTDATA") == "1":
            pytest.fail(f"missing test slide {CMU_SMALL}; run scripts/fetch_test_data.py")
        pytest.skip("test slide not downloaded; run scripts/fetch_test_data.py")
    return CMU_SMALL
