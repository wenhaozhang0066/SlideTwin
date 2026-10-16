"""Slide manifest and open slide handles.

The manifest may carry an `eval_only` block (labels, annotation paths) for evaluation scripts. It is
deleted before validation, so ground truth never enters a core object, a response or a log. Public
fields (the id above all, which every reader sees) must not repeat any eval_only value.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from slidetwin.core.errors import (
    ConfigError,
    SlideIntegrityError,
    SlideMetadataError,
    SlideNotFound,
)
from slidetwin.core.fileset import check_complete, fileset_sha256
from slidetwin.core.geometry import Point
from slidetwin.core.render import MAX_READ_PIXELS
from slidetwin.core.slide_source import ReaderOptions, SlideSource, open_slide

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
    # "auto" (M8): the reader is chosen from the file's content (slidetwin.core.formats). The two
    # named backends keep the behaviour of earlier manifests.
    backend: Literal["auto", "tiffslide", "openslide"] = "auto"
    format: str | None = Field(None, pattern=r"^[a-z0-9-]{1,40}$")  # expected format, checked
    # Reader choices (M8); a reader refuses a choice its format has no use for.
    scene: int | None = Field(None, ge=0, le=999)  # one SlideTwin slide per scene or ROI
    plane: int | None = Field(None, ge=0, le=999)  # focal plane of a z-stack
    channels: tuple[Annotated[int, Field(ge=0, le=255)], ...] | None = Field(
        None, min_length=1, max_length=16
    )
    fileset_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")  # multi-file slides
    experimental: bool = False  # serve formats validated only on synthetic samples

    def options(self) -> ReaderOptions:
        return ReaderOptions(self.scene, self.plane, self.channels, self.experimental)

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
    """`sha256` is the entry file's hash (D9), or the file-set hash for a folder; a slide made of
    several files also has `fileset_sha256`, which covers every file it needs."""

    slide_id: str
    source: SlideSource
    sha256: str
    fileset_sha256: str | None = None


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
        self._cache_lock = threading.Lock()

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
        if entry.backend == "auto":
            try:
                source = _open_auto(path, entry, override)
            except OSError:  # unreadable (permissions, I/O error): never name the file
                raise SlideNotFound(
                    f"The file for slide '{entry.id}' could not be read.",
                    hint="Check that the slide's files are readable by the server.",
                ) from None
        elif (
            entry.options() != ReaderOptions()
            or entry.fileset_sha256 is not None
            or entry.format is not None
        ):
            raise ConfigError(
                f"Slide '{entry.id}': format, scene, plane, channels, experimental and "
                "fileset_sha256 need backend auto.",
            )
        else:
            source = open_slide(path, backend=entry.backend, mpp_override=override, name=entry.id)
        try:
            check_servable(source, entry.id)
            digest, whole = self._content_hash(entry, path, source)
        except OSError:
            source.close()
            raise SlideNotFound(f"The file for slide '{entry.id}' could not be read.") from None
        except Exception:
            source.close()
            raise
        return RegisteredSlide(entry.id, source, digest, whole)

    def _content_hash(
        self, entry: SlideEntry, path: Path, source: SlideSource
    ) -> tuple[str, str | None]:
        """(sha256, fileset_sha256): the entry file's hash and, when the reader reads several
        files, the hash of the whole set (for a folder, sha256 is the set's hash too). A pinned
        hash is always verified on the files themselves (once per process); the cache only
        spares unpinned slides from re-hashing."""
        if entry.sha256 is not None and not path.is_file():
            raise ConfigError(f"Slide '{entry.id}' is a folder: pin it with fileset_sha256.")
        if entry.sha256 is not None and file_sha256(path) != entry.sha256:
            raise _mismatch(entry.id, "sha256")
        fileset = getattr(source, "fileset", None)
        if fileset is None:
            if entry.fileset_sha256 is not None:
                raise ConfigError(f"Slide '{entry.id}' is a single file: use sha256.")
            digest = entry.sha256 if entry.sha256 is not None else self._cached_sha256(path)
            return digest, None
        if entry.sha256 is not None and entry.fileset_sha256 is None:
            raise ConfigError(
                f"Slide '{entry.id}' is made of several files, and sha256 covers only the first.",
                hint="Pin the whole slide with fileset_sha256 instead.",
            )
        root, files = fileset
        rels = check_complete(root, files, entry.id)
        pinned = entry.fileset_sha256 is not None
        hashes: dict[str, str] = {}  # each file is hashed once, the entry file included
        with self._hash_cache() as cached:
            base = file_sha256 if pinned else cached

            def once(p: Path) -> str:
                key = os.path.realpath(p)
                if key not in hashes:
                    hashes[key] = base(p)
                return hashes[key]

            whole = fileset_sha256(root, rels, once)
            first = once(path) if path.is_file() else whole
        if pinned and whole != entry.fileset_sha256:
            raise _mismatch(entry.id, "fileset_sha256")
        return first, whole

    def _cached_sha256(self, path: Path) -> str:
        with self._hash_cache() as cached:
            return cached(path)

    @contextlib.contextmanager
    def _hash_cache(self) -> Iterator[Callable[[Path], str]]:
        """File hashes, cached on disk by (real path, size, mtime, inode). The cache is read once
        and written once per slide, so a file set of thousands of files costs one pass. Hashing
        runs outside the lock; the write merges with what other opens wrote meanwhile, so none of
        their entries is lost. The cache is a hint only: malformed content is ignored and write
        failures are not fatal."""
        cache_file = self._cache_dir / "slide_sha256.json" if self._cache_dir else None
        with self._cache_lock:
            cache = self._read_cache(cache_file)
        added: dict[str, str] = {}

        def cached(path: Path) -> str:
            st = path.stat()
            key = f"{os.path.realpath(path)}|{st.st_size}|{st.st_mtime_ns}|{st.st_ino}"
            digest = cache.get(key) or added.get(key)
            if digest is None:
                digest = added[key] = file_sha256(path)
            return digest

        yield cached
        if added and cache_file is not None:
            with self._cache_lock:
                self._write_cache(cache_file, {**self._read_cache(cache_file), **added})

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


def _open_auto(path: Path, entry: SlideEntry, override: Point | None) -> SlideSource:
    from slidetwin.readers import open_source, route

    match = route(path, entry.id)
    if entry.format is not None and entry.format != match.format:
        raise SlideMetadataError(
            f"Slide '{entry.id}' is {match.format}, not {entry.format} as the manifest says.",
            hint="Correct the manifest's format field, or check that the right file is in place.",
        )
    return open_source(path, match, name=entry.id, mpp_override=override, options=entry.options())


def _mismatch(slide_id: str, field: str) -> SlideIntegrityError:
    return SlideIntegrityError(
        f"Slide '{slide_id}' does not match the {field} pinned in the manifest.",
        hint="Re-download the slide or update the manifest after checking the source.",
    )


def check_servable(source: SlideSource, slide_id: str) -> None:
    """Refuse a slide that no view could be rendered from: its coarsest level is larger than the
    renderer reads at once (a single-level image without a pyramid)."""
    w, h = min(source.level_dimensions, key=lambda size: size[0] * size[1])
    if w * h > MAX_READ_PIXELS:
        raise SlideMetadataError(
            f"Slide '{slide_id}' has no pyramid level small enough to show the whole slide "
            f"({w} x {h} px at its coarsest).",
            hint="Save it as a pyramidal TIFF, for example with libvips tiffsave --pyramid.",
        )
