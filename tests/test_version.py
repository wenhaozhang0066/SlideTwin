from __future__ import annotations

import subprocess

import pytest

from slidetwin import version


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    version.git_sha.cache_clear()


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIDETWIN_GIT_SHA", "abc123")
    assert version.git_sha() == "abc123"
    assert version.app_version()["git_sha"] == "abc123"


def test_unknown_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLIDETWIN_GIT_SHA", raising=False)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("git missing")

    monkeypatch.setattr(subprocess, "run", boom)
    assert version.git_sha() == "unknown"
