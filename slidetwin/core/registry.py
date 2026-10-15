"""Slide manifest and open slide handles.

The manifest may carry an `eval_only` block (labels, annotation paths) for evaluation scripts. It is
deleted before validation, so ground truth never enters a core object, a response or a log. Public
fields (the id above all, which every reader sees) must not repeat any eval_only value.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from slidetwin.core.errors import ConfigError, SlideIntegrityError, SlideNotFound
from slidetwin.core.slide_source import SlideSource, open_slide

SLIDE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class MppOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    x: float = Field(gt=0, allow_inf_nan=False)
    y: float = Field(gt=0, allow_inf_nan=False)
    source: str = Field(min_length=3)


class SlideEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=SLIDE_ID_PATTERN)
    path: str
    source_url: str | None = None  # server-side only (downloads); never sent to readers
    sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    license: str | None = None
    attribution: str | None = None
    mpp_override: MppOverride | None = None
    backend: Literal["tiffslide", "openslide"] = "tiffslide"

    @field_validator("path")
    @classmethod
    def _relative(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or not v:
            raise ValueError("path must be relative to SLIDETWIN_SLIDE_DIR without '..'")
        return v

    def public(self) -> dict[str, Any]:
        """What a reader may see about a slide."""
        return {"id": self.id, "license": self.license, "attribution": self.attribution}


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _check_no_leak(entry: SlideEntry, eval_only: Any, origin: str) -> None:
    """Refuse an entry whose public fields repeat a ground-truth value (e.g. 'tumor' in the id)."""
    exposed = {
        "id": entry.id,
        "license": entry.license or "",
        "attribution": entry.attribution or "",
    }
    for secret in _strings(eval_only):
        token = secret.strip().lower()
        if len(token) < 3:
            continue
        for field, text in exposed.items():
            if token in text.lower():
                raise ConfigError(
                    f"{origin}: slide '{entry.id}' exposes ground truth: its {field} contains the "
                    f"eval_only value '{secret}'.",
                    hint="Use an opaque slide id and keep labels only under eval_only.",
                )


def parse_manifest(data: Any, origin: str = "manifest") -> list[SlideEntry]:
    if not isinstance(data, Mapping) or not isinstance(data.get("slides"), list):
        raise ConfigError(f"{origin}: expected a mapping with a 'slides' list.")
    entries: list[SlideEntry] = []
    for i, raw in enumerate(data["slides"]):
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{origin}: slides[{i}] must be a mapping.")
        public = {k: v for k, v in raw.items() if k != "eval_only"}
        try:
            entry = SlideEntry.model_validate(public)
        except ValidationError as err:
            first = err.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            raise ConfigError(f"{origin}: slides[{i}].{loc}: {first['msg']}") from None
        _check_no_leak(entry, raw.get("eval_only"), origin)
        entries.append(entry)
    ids = [e.id for e in entries]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise ConfigError(f"{origin}: duplicate slide ids {dupes}.")
    return entries


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class RegisteredSlide:
    slide_id: str
    source: SlideSource
    sha256: str


class SlideRegistry:
    """Resolves slide ids to open sources. Handles stay open for the life of the process.

    Each slide has its own lock, so opening and hashing one slide never blocks another.
    """

    def __init__(
        self,
        entries: list[SlideEntry],
        slide_dir: Path,
        *,
        cache_dir: Path | None = None,
    ) -> None:
        self._entries = {e.id: e for e in entries}
        self._slide_dir = slide_dir
        self._cache_dir = cache_dir
        self._open: dict[str, RegisteredSlide] = {}
        self._lock = threading.Lock()
        self._slide_locks: dict[str, threading.Lock] = {}

    @classmethod
    def from_manifest(
        cls, manifest_path: Path, slide_dir: Path, *, cache_dir: Path | None = None
    ) -> SlideRegistry:
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as err:
            raise ConfigError(
                f"Cannot read slide manifest {manifest_path.name}: {type(err).__name__}."
            ) from None
        return cls(parse_manifest(data, manifest_path.name), slide_dir, cache_dir=cache_dir)

    def add_source(self, slide_id: str, source: SlideSource, sha256: str) -> None:
        """Register an already open source (tests, synthetic slides)."""
        with self._lock:
            self._open[slide_id] = RegisteredSlide(slide_id, source, sha256)

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(set(self._entries) | set(self._open))

    def entry(self, slide_id: str) -> SlideEntry | None:
        return self._entries.get(slide_id)

    def catalogue(self) -> list[dict[str, Any]]:
        """Public metadata of every slide: id, license, attribution. No labels, paths or URLs."""
        out = []
        for slide_id in self.ids():
            e = self._entries.get(slide_id)
            out.append(e.public() if e else {"id": slide_id, "license": None, "attribution": None})
        return out

    def get(self, slide_id: str) -> RegisteredSlide:
        with self._lock:
            if slide_id in self._open:
                return self._open[slide_id]
            entry = self._entries.get(slide_id)
            if entry is None:
                raise SlideNotFound(
                    f"Unknown slide '{slide_id[:64]}'.",
                    hint="List the available slides with GET /api/slides.",
                )
            slide_lock = self._slide_locks.setdefault(slide_id, threading.Lock())
        with slide_lock:
            with self._lock:
                if slide_id in self._open:  # opened by another thread meanwhile
                    return self._open[slide_id]
            reg = self._open_entry(entry)
            with self._lock:
                self._open[slide_id] = reg
            return reg

    def _open_entry(self, entry: SlideEntry) -> RegisteredSlide:
        path = self._slide_dir / entry.path
        override = (entry.mpp_override.x, entry.mpp_override.y) if entry.mpp_override else None
        source = open_slide(path, backend=entry.backend, mpp_override=override, name=entry.id)
        try:
            # A pinned hash is always verified on the file itself (once per process); the cache
            # only spares unpinned slides from re-hashing.
            digest = file_sha256(path) if entry.sha256 else self._cached_sha256(path)
        except OSError:
            source.close()
            raise SlideNotFound(f"The file for slide '{entry.id}' could not be read.") from None
        if entry.sha256 is not None and digest != entry.sha256:
            source.close()
            raise SlideIntegrityError(
                f"Slide '{entry.id}' does not match the sha256 pinned in the manifest.",
                hint="Re-download the file or update the manifest after checking the source.",
            )
        return RegisteredSlide(entry.id, source, digest)

    def _cached_sha256(self, path: Path) -> str:
        """File hash, cached on disk by (real path, size, mtime, inode). The cache is a hint only:
        malformed content is ignored and write failures are not fatal."""
        st = path.stat()
        key = f"{os.path.realpath(path)}|{st.st_size}|{st.st_mtime_ns}|{st.st_ino}"
        cache_file = self._cache_dir / "slide_sha256.json" if self._cache_dir else None
        cache = self._read_cache(cache_file)
        cached = cache.get(key)
        if cached is not None:
            return cached
        digest = file_sha256(path)
        if cache_file is not None:
            cache[key] = digest
            self._write_cache(cache_file, cache)
        return digest

    @staticmethod
    def _read_cache(cache_file: Path | None) -> dict[str, str]:
        if cache_file is None:
            return {}
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            k: v
            for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and _HEX64.fullmatch(v)
        }

    @staticmethod
    def _write_cache(cache_file: Path, cache: dict[str, str]) -> None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=cache_file.parent, prefix=".slide_sha256.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=1, sort_keys=True)
            os.replace(tmp, cache_file)
        except OSError:
            pass  # the cache only saves time

    def close(self) -> None:
        with self._lock:
            for reg in self._open.values():
                reg.source.close()
            self._open.clear()
