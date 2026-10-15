"""Slide manifest and open slide handles.

The manifest may carry an `eval_only` block (labels, annotation paths) for evaluation scripts. It is
deleted before validation, so ground truth never enters a core object, a response or a log.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from slidetwin.core.errors import ConfigError, SlideIntegrityError, SlideNotFound
from slidetwin.core.slide_source import SlideSource, open_slide

SLIDE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class MppOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    x: float = Field(gt=0, allow_inf_nan=False)
    y: float = Field(gt=0, allow_inf_nan=False)
    source: str = Field(min_length=3)


class SlideEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=SLIDE_ID_PATTERN)
    path: str
    source_url: str | None = None
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


def parse_manifest(data: Any, origin: str = "manifest") -> list[SlideEntry]:
    if not isinstance(data, Mapping) or not isinstance(data.get("slides"), list):
        raise ConfigError(f"{origin}: expected a mapping with a 'slides' list.")
    entries: list[SlideEntry] = []
    for i, raw in enumerate(data["slides"]):
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{origin}: slides[{i}] must be a mapping.")
        public = {k: v for k, v in raw.items() if k != "eval_only"}
        try:
            entries.append(SlideEntry.model_validate(public))
        except ValidationError as err:
            first = err.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            raise ConfigError(f"{origin}: slides[{i}].{loc}: {first['msg']}") from None
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
    """Resolves slide ids to open sources. Handles stay open for the life of the process."""

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

    @classmethod
    def from_manifest(
        cls, manifest_path: Path, slide_dir: Path, *, cache_dir: Path | None = None
    ) -> SlideRegistry:
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as err:
            raise ConfigError(f"Cannot read slide manifest {manifest_path.name}: {err}") from None
        return cls(parse_manifest(data, manifest_path.name), slide_dir, cache_dir=cache_dir)

    def add_source(self, slide_id: str, source: SlideSource, sha256: str) -> None:
        """Register an already open source (tests, synthetic slides)."""
        with self._lock:
            self._open[slide_id] = RegisteredSlide(slide_id, source, sha256)

    def ids(self) -> list[str]:
        return sorted(set(self._entries) | set(self._open))

    def entry(self, slide_id: str) -> SlideEntry | None:
        return self._entries.get(slide_id)

    def get(self, slide_id: str) -> RegisteredSlide:
        with self._lock:
            if slide_id in self._open:
                return self._open[slide_id]
            entry = self._entries.get(slide_id)
            if entry is None:
                raise SlideNotFound(
                    f"Unknown slide '{slide_id}'.",
                    hint="List the available slides with GET /api/slides.",
                )
            path = self._slide_dir / entry.path
            override = (entry.mpp_override.x, entry.mpp_override.y) if entry.mpp_override else None
            source = open_slide(path, backend=entry.backend, mpp_override=override, name=entry.id)
            digest = self._sha256(path)
            if entry.sha256 is not None and digest != entry.sha256:
                source.close()
                raise SlideIntegrityError(
                    f"Slide '{slide_id}' does not match the sha256 pinned in the manifest.",
                    hint="Re-download the file or update the manifest after checking the source.",
                )
            reg = RegisteredSlide(slide_id, source, digest)
            self._open[slide_id] = reg
            return reg

    def _sha256(self, path: Path) -> str:
        """File hash, cached on disk by (real path, size, mtime) because slides are large."""
        st = path.stat()
        key = f"{os.path.realpath(path)}|{st.st_size}|{st.st_mtime_ns}"
        cache_file = self._cache_dir / "slide_sha256.json" if self._cache_dir else None
        cache: dict[str, str] = {}
        if cache_file is not None and cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
        if key in cache:
            return cache[key]
        digest = file_sha256(path)
        if cache_file is not None:
            cache[key] = digest
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".part")
            tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, cache_file)
        return digest

    def close(self) -> None:
        with self._lock:
            for reg in self._open.values():
                reg.source.close()
            self._open.clear()
