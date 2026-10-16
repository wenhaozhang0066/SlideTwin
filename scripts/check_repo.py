"""Check that the repository holds no internal paths, secrets, personal addresses or slide data
(M9, before it is made public). Scans the tracked files as bytes, and with --history every blob,
author, committer and message of every commit on every ref.

    python scripts/check_repo.py [--history]

SLIDETWIN_PRIVATE_PATTERNS adds a regular expression of your own (user names, host names, project
folders), which is kept out of the repository so that this script does not publish them.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = "scripts/check_repo.py"  # holds the patterns themselves
PATTERNS = {
    "an absolute path on a shared or personal machine": (
        r"/(?:home|scratch|project|sfs|gpfs|nfs|mnt|Users)/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\"
    ),
    "a GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "a Hugging Face token": r"\bhf_[A-Za-z0-9]{30,}",
    "an Anthropic or OpenAI key": r"\bsk-(?:ant|proj)-[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9]{40,}",
    "a Google API key": r"\bAIza[0-9A-Za-z_-]{35}",
    "a Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "an AWS key": r"\bAKIA[0-9A-Z]{16}\b",
    "a private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "an e-mail address": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
}
# Addresses that are fine: GitHub's private commit addresses and examples.
EMAIL_OK = re.compile(
    r"@users\.noreply\.github\.com$|@example\.(?:com|org|net)$|\.test$|@localhost$", re.I
)
# Slide, image, annotation and label data never belong here; the docs' few pictures do.
DATA_SUFFIXES = (".svs", ".ndpi", ".mrxs", ".dcm", ".czi", ".bif", ".vsi", ".isyntax", ".scn")
DATA_SUFFIXES += (".qptiff", ".zvi", ".vms", ".vmu", ".tif", ".tiff", ".dat", ".ets", ".jpg")
DATA_SUFFIXES += (".jpeg", ".png", ".gif", ".xml", ".geojson", ".csv", ".npy", ".npz", ".h5")
PICTURES = re.compile(r"^docs/media/[^/]+\.(?:gif|png|jpe?g|svg)$", re.I)
MAX_BYTES = 2_000_000


def patterns() -> dict[str, re.Pattern[bytes]]:
    out = dict(PATTERNS)
    private = os.environ.get("SLIDETWIN_PRIVATE_PATTERNS")
    if private:
        out["a private pattern (SLIDETWIN_PRIVATE_PATTERNS)"] = private
    return {what: re.compile(regex.encode()) for what, regex in out.items()}


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def scan_bytes(data: bytes, where: str) -> Iterator[str]:
    """Problems in `data`, as 'where:line: what'; binary content is scanned too."""
    for what, regex in patterns().items():
        for match in regex.finditer(data):
            if what == "an e-mail address" and EMAIL_OK.search(match.group().decode("latin-1")):
                continue
            line = data.count(b"\n", 0, match.start()) + 1
            yield f"{where}:{line}: {what}"


def data_file(name: str) -> bool:
    return name.lower().endswith(DATA_SUFFIXES) and not PICTURES.match(name)


def check_tree(root: Path = ROOT) -> list[str]:
    problems = []
    for raw in git(root, "ls-files", "-z").split(b"\0"):
        name = raw.decode("utf-8", "surrogateescape")
        if not name:
            continue
        path = root / name
        if data_file(name):
            problems.append(f"{name}: slide, image or label data")
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_BYTES:
            problems.append(f"{name}: larger than {MAX_BYTES} bytes")
        if name != SELF:
            problems += scan_bytes(path.read_bytes(), name)
    return problems


def check_history(root: Path = ROOT) -> list[str]:
    """Every blob reachable from any ref (deleted files and merged branches included), and every
    commit's author, committer and message."""
    problems = []
    objects = git(root, "rev-list", "--objects", "--all").decode("utf-8", "surrogateescape")
    blobs: dict[str, str] = {}
    for line in objects.splitlines():
        sha, _, name = line.partition(" ")
        if name:
            blobs.setdefault(sha, name)
    names = sorted(set(blobs.values()))
    problems += [f"{n} (in the history): slide, image or label data" for n in names if data_file(n)]
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input="".join(f"{sha}\n" for sha in blobs).encode(),
        check=True,
        capture_output=True,
    ).stdout
    pos = 0
    while pos < len(batch):
        header_end = batch.index(b"\n", pos)
        sha, kind, size = batch[pos:header_end].decode().split()
        body = batch[header_end + 1 : header_end + 1 + int(size)]
        pos = header_end + 1 + int(size) + 1
        name = blobs[sha]
        if kind != "blob" or name == SELF:
            continue
        if int(size) > MAX_BYTES:
            problems.append(f"{name} (blob {sha[:10]}): larger than {MAX_BYTES} bytes")
        problems += scan_bytes(body, f"{name} (blob {sha[:10]})")
    log = git(root, "log", "--all", "--format=%h%x00%an <%ae>%x00%cn <%ce>%x00%B%x01")
    for record in log.split(b"\x01"):
        fields = record.strip(b"\n").split(b"\0")
        if len(fields) < 4:
            continue
        commit = fields[0].decode()
        problems += scan_bytes(b"\n".join(fields[1:]), f"commit {commit} (people, message)")
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
