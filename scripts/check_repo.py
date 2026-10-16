"""Check that the repository holds no internal paths, secrets or slide data (M9, before it is made
public). Scans the tracked files, and with --history every commit.

    python scripts/check_repo.py [--history]

SLIDETWIN_PRIVATE_PATTERNS adds a regular expression of your own (user names, host names, project
folders), which is kept out of the repository so that this script does not publish them.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {  # POSIX extended regular expressions, for git grep and git log -G alike
    "an absolute path on a shared machine": "/(home|scratch|project|sfs|gpfs|nfs)/[A-Za-z0-9_.-]+/",
    "a GitHub token": "(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    "a Hugging Face token": "hf_[A-Za-z0-9]{30,}",
    "an Anthropic key": "sk-ant-[A-Za-z0-9_-]{20,}",
    "an AWS key": "AKIA[0-9A-Z]{16}",
    "a private key": "-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
# Slide and image data never belong here; the docs' few pictures live under docs/media.
DATA_SUFFIXES = (".svs", ".ndpi", ".mrxs", ".dcm", ".czi", ".bif", ".vsi", ".isyntax", ".scn")
DATA_SUFFIXES += (".qptiff", ".zvi", ".vms", ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".gif")
ALLOWED_DATA = ("docs/media/",)
MAX_BYTES = 2_000_000


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def patterns() -> dict[str, str]:
    out = dict(PATTERNS)
    private = os.environ.get("SLIDETWIN_PRIVATE_PATTERNS")
    if private:
        out["a private pattern (SLIDETWIN_PRIVATE_PATTERNS)"] = private
    return out


def check_tree() -> list[str]:
    problems = []
    for what, regex in patterns().items():
        found = subprocess.run(
            ["git", "grep", "-n", "-I", "-E", regex, "--", ".", ":!scripts/check_repo.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        ).stdout
        problems += [f"{line[:160]}: {what}" for line in found.splitlines()]
    for name in git("ls-files").splitlines():
        if name.lower().endswith(DATA_SUFFIXES) and not name.startswith(ALLOWED_DATA):
            problems.append(f"{name}: slide or image data")
        path = ROOT / name
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            problems.append(f"{name}: larger than {MAX_BYTES} bytes")
    return problems


def check_history() -> list[str]:
    problems = []
    for what, regex in patterns().items():
        commits = git(
            "log", "--all", "--format=%h %s", f"-G{regex}", "--", ".", ":!scripts/check_repo.py"
        )
        problems += [f"commit {line}: {what}" for line in commits.splitlines()]
    names = git("log", "--all", "--name-only", "--format=").splitlines()
    for name in sorted(set(names)):
        if name.lower().endswith(DATA_SUFFIXES) and not name.startswith(ALLOWED_DATA):
            problems.append(f"{name} (in the history): slide or image data")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--history", action="store_true", help="also scan every commit")
    args = p.parse_args(argv)
    problems = check_tree() + (check_history() if args.history else [])
    for problem in problems:
        print(problem)
    print(f"{len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
