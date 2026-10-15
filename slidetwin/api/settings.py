"""Server settings, read from environment variables (never from files in the repo)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    manifest: Path
    slide_dir: Path
    log_dir: Path | None
    cache_dir: Path | None
    max_sessions: int = 16
    idle_ttl_s: float = 7200.0
    max_body_bytes: int = 1_000_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Paths default to the working directory layout of the repository and Docker image.

        SLIDETWIN_CONFIG_DIR   configs            profiles, protocols, slide manifest
        SLIDETWIN_MANIFEST     <config>/slides/manifest.yaml
        SLIDETWIN_SLIDE_DIR    data/slides        root for manifest paths
        SLIDETWIN_LOG_DIR      logs               trajectories and served images ("" = memory only)
        SLIDETWIN_CACHE_DIR    cache              slide hash cache ("" = none)
        SLIDETWIN_MAX_SESSIONS 16
        SLIDETWIN_SESSION_TTL_S 7200              idle time before a session is dropped
        SLIDETWIN_MAX_BODY_BYTES 1000000          request body limit
        """
        env = os.environ if environ is None else environ
        config_dir = Path(env.get("SLIDETWIN_CONFIG_DIR", "configs"))
        log_dir = env.get("SLIDETWIN_LOG_DIR", "logs")
        cache_dir = env.get("SLIDETWIN_CACHE_DIR", "cache")
        return cls(
            config_dir=config_dir,
            manifest=Path(
                env.get("SLIDETWIN_MANIFEST", str(config_dir / "slides" / "manifest.yaml"))
            ),
            slide_dir=Path(env.get("SLIDETWIN_SLIDE_DIR", "data/slides")),
            log_dir=Path(log_dir) if log_dir else None,
            cache_dir=Path(cache_dir) if cache_dir else None,
            max_sessions=int(env.get("SLIDETWIN_MAX_SESSIONS", "16")),
            idle_ttl_s=float(env.get("SLIDETWIN_SESSION_TTL_S", "7200")),
            max_body_bytes=int(env.get("SLIDETWIN_MAX_BODY_BYTES", "1000000")),
        )
