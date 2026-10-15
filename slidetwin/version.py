"""Build metadata written into every trajectory header and the health endpoint."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from slidetwin import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def git_sha() -> str:
    """Return the git commit of the running code.

    `SLIDETWIN_GIT_SHA` wins (set at Docker build time). Otherwise ask git; a dirty
    worktree gets a `-dirty` suffix. Returns "unknown" when neither is available.
    """
    env = os.environ.get("SLIDETWIN_GIT_SHA")
    if env:
        return env
    try:
        sha = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def app_version() -> dict[str, str]:
    return {"version": __version__, "git_sha": git_sha()}
