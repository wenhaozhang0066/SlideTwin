"""Trajectory log (SPEC 9): JSONL, a header line followed by one event line per action.

Lines are pure ASCII JSON (non-ASCII text is \\u-escaped) and are separated by "\\n" only, so no
character a reader types can split or corrupt a line.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from slidetwin.core.errors import ReplayError

SCHEMA_VERSION = "1.0"


def dumps(record: Mapping[str, Any]) -> str:
    """One log line. Raises ValueError, TypeError or RecursionError if the record is not JSON."""
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


class TrajectoryWriter:
    """Appends records to a file (if given) and to memory, in that order.

    A record that cannot be written is not kept in memory either, and a partial line is cut back,
    so the file and the in-memory copy never disagree.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self._lines: list[str] = []
        self._fh: IO[str] | None = None
        self.closed = False
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("x", encoding="ascii")

    def write(self, record: dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("trajectory writer is closed")
        line = dumps(record)
        if self._fh is not None:
            start = self._fh.tell()
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except BaseException:
                try:
                    self._fh.truncate(start)
                    self._fh.seek(start)
                except OSError:
                    pass
                raise
        self.records.append(record)
        self._lines.append(line)

    def text(self) -> str:
        return "".join(line + "\n" for line in self._lines)

    def close(self) -> None:
        self.closed = True
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --- reading --------------------------------------------------------------------------------------


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class _SlideRef(_Loose):
    id: StrictStr
    sha256: StrictStr


class _ConfigRef(_Loose):
    id: StrictStr
    sha256: StrictStr
    content: dict[str, Any]


class _Header(_Loose):
    type: StrictStr
    schema_version: StrictStr
    session_id: StrictStr
    slide: _SlideRef
    profile: _ConfigRef
    protocol: _ConfigRef
    viewport_px: list[StrictInt] = Field(min_length=2, max_length=2)
    actor: StrictStr


class _Event(_Loose):
    type: StrictStr
    seq: StrictInt
    action: StrictStr
    params: dict[str, Any]
    error: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None


def parse_trajectory(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split a JSONL trajectory into (header, events), checking its structure."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        raise ReplayError("The trajectory is empty.")
    records: list[Any] = []
    for n, line in enumerate(lines, 1):
        try:
            records.append(json.loads(line))
        except (ValueError, RecursionError):
            raise ReplayError(f"Line {n} of the trajectory is not valid JSON.") from None
    header, events = records[0], records[1:]
    if not isinstance(header, dict) or header.get("type") != "header":
        raise ReplayError("The first line of a trajectory must be its header.")
    if header.get("schema_version") != SCHEMA_VERSION:
        raise ReplayError(
            f"Trajectory schema {str(header.get('schema_version'))[:20]!r} is not supported "
            f"(expected {SCHEMA_VERSION})."
        )
    try:
        _Header.model_validate(header)
    except ValidationError as err:
        raise ReplayError(f"The trajectory header is malformed: {_first(err)}.") from None
    for expected, ev in enumerate(events, 1):
        if not isinstance(ev, dict) or ev.get("type") != "event":
            raise ReplayError(f"Line {expected + 1} is not an event.")
        try:
            _Event.model_validate(ev)
        except ValidationError as err:
            raise ReplayError(f"Event {expected} is malformed: {_first(err)}.") from None
        if ev["seq"] != expected:
            raise ReplayError(f"Event {expected} is missing or out of order.")
    if not events or events[0].get("action") != "create_session":
        raise ReplayError("The first event of a trajectory must be create_session.")
    return header, events


def _first(err: ValidationError) -> str:
    e = err.errors()[0]
    return f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"


def load_trajectory(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ReplayError(f"Cannot read the trajectory file ({type(err).__name__}).") from None
    return parse_trajectory(text)
