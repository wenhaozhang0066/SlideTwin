"""Live sessions: creation, lookup, idle expiry and limits.

Adapters (REST, MCP, web UI) talk only to this class and to the `Session` it returns.
Sessions live in process memory; their trajectories and images are written to disk as they happen,
so a trajectory stays downloadable after its session expires.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slidetwin.core.config import ConfigStore
from slidetwin.core.errors import ConfigError, InvalidParameter, SessionNotFound, TooManySessions
from slidetwin.core.images import DiskImageStore, ImageStore, MemoryImageStore
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.session import Session

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class _Entry:
    session: Session
    last_used: float


class SessionManager:
    def __init__(
        self,
        *,
        registry: SlideRegistry,
        configs: ConfigStore,
        log_dir: Path | None = None,
        max_sessions: int = 16,
        idle_ttl_s: float = 7200.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_sessions < 1 or idle_ttl_s <= 0:
            raise ValueError("max_sessions must be >= 1 and idle_ttl_s > 0")
        self.registry = registry
        self.configs = configs
        self.log_dir = log_dir
        self.max_sessions = max_sessions
        self.idle_ttl_s = idle_ttl_s
        self._clock = clock
        self._sessions: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._create_lock = threading.Lock()  # one creation at a time, so the limit holds

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def create(
        self,
        *,
        slide_id: str,
        actor: str,
        profile: str = "viewer",
        protocol: str = "sln_metastasis",
        viewport_px: Sequence[int] | None = None,
    ) -> Session:
        prof = self._lookup(self.configs.profile, profile)
        proto = self._lookup(self.configs.protocol, protocol)
        with self._create_lock:
            with self._lock:
                self._sweep_locked()
                if len(self._sessions) >= self.max_sessions:
                    self._evict_closed_locked()
                if len(self._sessions) >= self.max_sessions:
                    raise TooManySessions(
                        f"{len(self._sessions)} sessions are open (limit {self.max_sessions}).",
                        hint="Submit or abandon an open session, or try again later.",
                    )
            slide = self.registry.get(slide_id)
            session_id = uuid.uuid4().hex
            session = Session(
                session_id=session_id,
                slide_id=slide.slide_id,
                source=slide.source,
                slide_sha256=slide.sha256,
                profile=prof,
                protocol=proto,
                actor=actor,
                images=self._image_store(session_id),
                viewport_px=viewport_px,
                log_path=self._log_path(session_id),
            )
            with self._lock:
                self._sessions[session_id] = _Entry(session, self._clock())
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            self._sweep_locked()
            entry = self._sessions.get(session_id) if SESSION_ID_RE.match(session_id) else None
            if entry is None:
                raise SessionNotFound(
                    f"Session '{session_id[:40]}' does not exist or has expired.",
                    hint="Create a new session; an expired session's trajectory can still be "
                    "downloaded.",
                )
            entry.last_used = self._clock()
            return entry.session

    def trajectory(self, session_id: str) -> str:
        """JSONL of a live session (logged as export_trajectory) or of an expired one from disk."""
        try:
            session = self.get(session_id)
        except SessionNotFound:
            path = self._log_path(session_id) if SESSION_ID_RE.match(session_id) else None
            if path is None or not path.is_file():
                raise
            return path.read_text(encoding="utf-8")
        return session.export_trajectory().text or ""

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked()

    def close_all(self) -> None:
        with self._lock:
            for entry in self._sessions.values():
                entry.session.close()
            self._sessions.clear()

    # --- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _lookup(fn: Callable[[str], Any], key: str) -> Any:
        try:
            return fn(key)
        except ConfigError as err:
            raise InvalidParameter(err.message) from None

    def _sweep_locked(self) -> None:
        now = self._clock()
        expired = [k for k, e in self._sessions.items() if now - e.last_used > self.idle_ttl_s]
        for key in expired:
            self._sessions.pop(key).session.close()

    def _evict_closed_locked(self) -> None:
        closed = sorted((e.last_used, k) for k, e in self._sessions.items() if e.session.closed)
        for _, key in closed[: len(self._sessions) - self.max_sessions + 1]:
            self._sessions.pop(key).session.close()

    def _log_path(self, session_id: str) -> Path | None:
        if self.log_dir is None:
            return None
        return self.log_dir / "sessions" / f"{session_id}.jsonl"

    def _image_store(self, session_id: str) -> ImageStore:
        if self.log_dir is None:
            return MemoryImageStore()
        return DiskImageStore(self.log_dir / "sessions" / session_id)
