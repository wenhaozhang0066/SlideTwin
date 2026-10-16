"""The repository holds no internal paths, secrets or slide data (M9, scripts/check_repo.py)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout"
)
def test_the_tracked_files_are_clean() -> None:
    spec = importlib.util.spec_from_file_location("check_repo", REPO / "scripts" / "check_repo.py")
    assert spec is not None
    assert spec.loader is not None
    check_repo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_repo)
    assert check_repo.check_tree() == []
