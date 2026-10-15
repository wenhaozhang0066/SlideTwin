"""Trajectory log (SPEC 9): JSONL, a header line followed by one event line per action."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from slidetwin.core.errors import ReplayError

SCHEMA_VERSION = "1.0"


def dumps(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class TrajectoryWriter:
    """Appends records to an in-memory list and, if a path is given, to a file.

    Each line is flushed as it is written.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self._lines: list[str] = []
        self._fh: IO[str] | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("x", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        line = dumps(record)
        self.records.append(record)
        self._lines.append(line)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()

    def text(self) -> str:
        return "".join(line + "\n" for line in self._lines)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def parse_trajectory(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split a JSONL trajectory into (header, events), checking its structure."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ReplayError("The trajectory is empty.")
    try:
        records = [json.loads(line) for line in lines]
    except ValueError as err:
        raise ReplayError(f"The trajectory is not valid JSONL: {err}.") from None
    header, events = records[0], records[1:]
    if not isinstance(header, dict) or header.get("type") != "header":
        raise ReplayError("The first line of a trajectory must be its header.")
    if header.get("schema_version") != SCHEMA_VERSION:
        raise ReplayError(
            f"Trajectory schema {header.get('schema_version')!r} is not supported "
            f"(expected {SCHEMA_VERSION})."
        )
    expected = 1
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "event" or ev.get("seq") != expected:
            raise ReplayError(f"Event {expected} is missing or out of order.")
        expected += 1
    if not events or events[0].get("action") != "create_session":
        raise ReplayError("The first event of a trajectory must be create_session.")
    return header, events


def load_trajectory(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return parse_trajectory(path.read_text(encoding="utf-8"))
