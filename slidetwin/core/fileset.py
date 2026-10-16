"""Slides made of several files (M8): completeness and one hash for the whole set.

A single-file slide keeps its plain sha256 (D9). A file set (an MRXS folder, a DICOM series, a VSI
with its companion folder) is hashed as sha256 over `slidetwin-fileset-v1` and one JSON line
`[relative path, size, sha256]` per file, sorted by the path's UTF-8 bytes. JSON quoting makes the
encoding unambiguous for any file name.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from slidetwin.core.errors import ConfigError, SlideNotFound

FILESET_VERSION = "slidetwin-fileset-v1"


def check_relative(paths: Iterable[str]) -> tuple[str, ...]:
    """The set's relative POSIX paths, sorted; refuses absolute paths, '..' and duplicates."""
    out = sorted(set(paths), key=lambda p: p.encode("utf-8"))
    for rel in out:
        parts = PurePosixPath(rel).parts
        if not rel or PurePosixPath(rel).is_absolute() or ".." in parts or "\\" in rel:
            raise ConfigError(f"A file-set path must be relative without '..', got {rel[:80]!r}.")
    if not out:
        raise ConfigError("A file set must contain at least one file.")
    return tuple(out)


def check_complete(root: Path, files: Iterable[str], slide_id: str) -> tuple[str, ...]:
    """All files present; the error counts the missing ones but never names them."""
    rels = check_relative(files)
    missing = sum(1 for rel in rels if not (root / rel).is_file())
    if missing:
        raise SlideNotFound(
            f"Slide '{slide_id}' is incomplete: {missing} of its {len(rels)} files are missing.",
            hint="Copy or download the whole slide (all files and folders) again.",
        )
    return rels


def fileset_sha256(root: Path, files: Iterable[str], file_hash: Callable[[Path], str]) -> str:
    h = hashlib.sha256((FILESET_VERSION + "\n").encode("ascii"))
    for rel in check_relative(files):
        path = root / rel
        line = json.dumps([rel, path.stat().st_size, file_hash(path)], ensure_ascii=False)
        h.update((line + "\n").encode("utf-8"))
    return h.hexdigest()
