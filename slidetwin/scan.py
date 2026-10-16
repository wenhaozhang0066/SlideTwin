"""What SlideTwin reads in a slide, and manifests for folders of slides (M9, `slidetwin check` and
`slidetwin manifest`).

These run on the user's own machine for the user's own files, so unlike the server they name the
files they look at.
"""

from __future__ import annotations

import datetime
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from slidetwin.core.errors import SlideTwinError
from slidetwin.core.fileset import check_complete, fileset_sha256
from slidetwin.core.registry import check_servable, file_sha256
from slidetwin.core.slide_source import ReaderOptions, SlideSource

# File names that may be a slide; the router decides from the content.
SUFFIXES = (
    ".svs",
    ".tif",
    ".tiff",
    ".ndpi",
    ".vms",
    ".vmu",
    ".mrxs",
    ".svslide",
    ".scn",
    ".bif",
    ".czi",
    ".qptiff",
    ".avs",
    ".vsi",
    ".isyntax",
    ".zvi",
)


@dataclass
class Checked:
    """One slide: what SlideTwin would serve, or why it refuses it."""

    path: Path
    format: str | None = None
    reader: str | None = None
    levels: list[tuple[int, int]] = field(default_factory=list)
    mpp: tuple[float, float] | None = None
    mpp_source: str | None = None
    files: int = 1
    hash_field: str | None = None  # "sha256" or "fileset_sha256"
    digest: str | None = None
    refused: str | None = None

    def lines(self) -> list[str]:
        if self.refused is not None:
            return [printable(str(self.path)), f"  refused: {printable(self.refused)}"]
        assert self.mpp is not None
        w0, h0 = self.levels[0]
        wn, hn = self.levels[-1]
        out = [
            printable(str(self.path)),
            f"  format: {self.format} (reader {self.reader})",
            f"  levels: {len(self.levels)}, {w0} x {h0} px down to {wn} x {hn} px",
            f"  mpp: {self.mpp[0]:.4f} x {self.mpp[1]:.4f} um/px ({self.mpp_source})",
            f"  files: {self.files}",
        ]
        if self.digest is not None:
            out.append(f"  {self.hash_field}: {self.digest}")
        return out


def check(path: Path, *, name: str | None = None, hash_files: bool = False) -> Checked:
    """Open `path` as the server would (router, reader, servable check) and describe it."""
    name = name or path.name
    try:
        from slidetwin.readers import open_source, route  # the readers import their libraries

        match = route(path, name)
        source = open_source(path, match, name=name, mpp_override=None, options=ReaderOptions())
    except Exception as err:  # one slide's failure never stops a scan
        return Checked(path, refused=_reason(err))
    try:
        check_servable(source, name)
        out = _describe(path, source, match.format, match.reader)
        if hash_files:
            out.hash_field, out.digest = _content_hash(path, source, name)
        return out
    except Exception as err:
        return Checked(path, refused=_reason(err))
    finally:
        source.close()


def _describe(path: Path, source: SlideSource, fmt: str, reader: str | None) -> Checked:
    fileset = getattr(source, "fileset", None)
    provenance = getattr(source, "provenance", None) or {}
    return Checked(
        path,
        format=fmt,
        reader=reader,
        levels=[(int(w), int(h)) for w, h in source.level_dimensions],
        mpp=(float(source.mpp[0]), float(source.mpp[1])),
        mpp_source=str(provenance.get("mpp_source", "file")),
        files=1 if fileset is None else len(fileset[1]),
    )


def _content_hash(path: Path, source: SlideSource, name: str) -> tuple[str, str]:
    """The pin a manifest entry needs: the file's sha256, or the hash of the whole file set."""
    fileset = getattr(source, "fileset", None)
    if fileset is None:
        return "sha256", file_sha256(path)
    root, files = fileset
    return "fileset_sha256", fileset_sha256(root, check_complete(root, files, name), file_sha256)


def _reason(err: Exception) -> str:
    if isinstance(err, SlideTwinError):
        return err.message + (f" {err.hint}" if err.hint else "")
    if isinstance(err, OSError):
        return f"cannot be read ({type(err).__name__}: {err.strerror or 'error'})"
    return f"cannot be read ({type(err).__name__})"


def candidates(folder: Path, skipped: list[Checked] | None = None) -> Iterator[Path]:
    """Files under `folder` that may be slides, and folders of DICOM files (a DICOM series), in
    a stable order. Hidden files and folders are skipped; unreadable folders and links to
    folders are not entered and are noted in `skipped`."""
    skipped = [] if skipped is None else skipped
    stack = [folder]
    found: list[Path] = []
    while stack:
        here = stack.pop()
        try:
            children = sorted(here.iterdir())
        except OSError as err:
            skipped.append(Checked(here, refused=f"a folder that cannot be read ({err.strerror})"))
            continue
        if here != folder and _dicom_folder(children):
            found.append(here)
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_symlink() and child.is_dir():
                skipped.append(Checked(child, refused="a link to a folder, not followed"))
            elif child.is_dir():
                stack.append(child)
            elif child.is_file() and child.name.lower().endswith(SUFFIXES):
                found.append(child)
    yield from sorted(found)


def _dicom_folder(children: list[Path]) -> bool:
    """A folder of a DICOM series: a file named .dcm, or a file whose content is DICOM."""
    files = [c for c in children if c.is_file() and not c.name.startswith(".")]
    if any(c.suffix.lower() == ".dcm" for c in files):
        return True
    for child in files[:20]:  # extension-less series: look at a few files' content
        try:
            with child.open("rb") as f:
                if f.read(132)[128:132] == b"DICM":
                    return True
        except OSError:
            continue
    return False


def manifest(folder: Path, *, log: Any = None) -> tuple[dict[str, Any], list[Checked]]:
    """A manifest for the slides under `folder`: opaque ids ("s_" + 10 hex digits of the content
    hash), paths relative to the folder, hashes and formats. Returns it with the refused slides."""
    log = sys.stderr if log is None else log  # looked up now: the stream may have been replaced
    entries: list[dict[str, Any]] = []
    refused: list[Checked] = []
    seen: dict[str, str] = {}
    ids: set[str] = set()
    skipped: list[Checked] = []
    for path in candidates(folder, skipped):
        rel = path.relative_to(folder).as_posix()
        print(f"checking {printable(rel)}", file=log, flush=True)
        result = check(path, name=rel, hash_files=True)
        if result.refused is not None:
            result.path = Path(rel)
            refused.append(result)
            continue
        assert result.digest is not None
        assert result.hash_field is not None
        if result.digest in seen:
            result.path, result.refused = Path(rel), f"the same slide as {seen[result.digest]}"
            refused.append(result)
            continue
        seen[result.digest] = rel
        slide_id = next(
            f"s_{result.digest[:n]}" for n in (10, 16, 64) if f"s_{result.digest[:n]}" not in ids
        )  # two hashes may share 10 hex digits: the id grows until it is unique
        ids.add(slide_id)
        entries.append(
            {"id": slide_id, "path": rel, result.hash_field: result.digest, "format": result.format}
        )
    for item in skipped:
        item.path = item.path.relative_to(folder)
    return {"schema_version": 1, "slides": entries}, refused + skipped


def manifest_yaml(data: dict[str, Any], refused: list[Checked]) -> str:
    """The manifest as YAML in plain ASCII. File names in the comments are escaped, so none can
    end a comment and add YAML of its own; the text must load back as `data`."""
    today = datetime.date.today().isoformat()
    head = [
        f"# Written by `slidetwin manifest` on {today}. Paths are relative to the slide folder",
        "# (SLIDETWIN_SLIDE_DIR or --slides). Ids are opaque: 's_' and the first 10 hex digits of",
        "# the content hash. Add license and attribution before sharing slides with anyone.",
    ]
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    text = "\n".join(head) + "\n" + body
    if refused:
        tail = ["# Not served:", *(f"#   {printable(f'{c.path}: {c.refused}')}" for c in refused)]
        text += "\n" + "\n".join(tail) + "\n"
    if yaml.safe_load(text) != data:
        raise ValueError("the manifest does not load back as written")  # a bug, never expected
    return text


def printable(text: str) -> str:
    """`text` with everything but printable ASCII escaped (file names may hold any byte)."""
    return "".join(c if " " <= c <= "~" else ascii(c)[1:-1] for c in text)
