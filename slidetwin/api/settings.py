"""Server settings, read from environment variables (never from files in the repo)."""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from slidetwin.core.errors import ConfigError

# The MCP endpoint answers only requests addressed to these hosts and origins (DNS rebinding
# protection, on by default for a local server). "*" turns the check off, e.g. on a public Space.
LOCAL_HOSTS = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*")
LOCAL_ORIGINS = tuple(f"http://{h}" for h in LOCAL_HOSTS)


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    manifest: Path
    slide_dir: Path
    log_dir: Path
    cache_dir: Path | None
    max_sessions: int = 16
    idle_ttl_s: float = 7200.0
    max_age_s: float = 28800.0
    max_body_bytes: int = 1_000_000
    mcp_allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    mcp_allowed_origins: tuple[str, ...] = LOCAL_ORIGINS
    rate_limit_per_s: float = 0.0  # sustained requests per second per client; 0 turns it off
    rate_limit_burst: int = 60

    def __post_init__(self) -> None:
        if not isinstance(self.max_sessions, int) or self.max_sessions < 1:
            raise ConfigError(f"SLIDETWIN_MAX_SESSIONS must be >= 1, got {self.max_sessions}.")
        for var, value in (
            ("SLIDETWIN_SESSION_TTL_S", self.idle_ttl_s),
            ("SLIDETWIN_SESSION_MAX_AGE_S", self.max_age_s),
        ):
            if not (math.isfinite(value) and value > 0):
                raise ConfigError(f"{var} must be a positive number of seconds, got {value}.")
        if not isinstance(self.max_body_bytes, int) or self.max_body_bytes < 1024:
            raise ConfigError(
                f"SLIDETWIN_MAX_BODY_BYTES must be at least 1024, got {self.max_body_bytes}."
            )
        if not (math.isfinite(self.rate_limit_per_s) and self.rate_limit_per_s >= 0):
            raise ConfigError(
                f"SLIDETWIN_RATE_LIMIT_PER_S must be 0 (off) or more, got {self.rate_limit_per_s}."
            )
        if not isinstance(self.rate_limit_burst, int) or self.rate_limit_burst < 1:
            raise ConfigError(
                f"SLIDETWIN_RATE_LIMIT_BURST must be at least 1, got {self.rate_limit_burst}."
            )
        if not isinstance(self.log_dir, Path) or not str(self.log_dir):
            raise ConfigError(
                "SLIDETWIN_LOG_DIR must name a directory: trajectories are written to disk as "
                "they happen (SPEC 6.3)."
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Paths default to the working-directory layout of the repository and Docker image.

        SLIDETWIN_CONFIG_DIR         configs          profiles, protocols, slide manifest
        SLIDETWIN_MANIFEST           <config>/slides/manifest.yaml
        SLIDETWIN_SLIDE_DIR          data/slides      root for manifest paths
        SLIDETWIN_LOG_DIR            logs             trajectories and served images (required)
        SLIDETWIN_CACHE_DIR          cache            slide hash cache ("" = none)
        SLIDETWIN_MAX_SESSIONS       16
        SLIDETWIN_SESSION_TTL_S      7200             idle time before a session ends
        SLIDETWIN_SESSION_MAX_AGE_S  28800            absolute session lifetime
        SLIDETWIN_MAX_BODY_BYTES     1000000          request body limit
        SLIDETWIN_MCP_ALLOWED_HOSTS  localhost        Host values /mcp accepts (comma list, "*" any)
        SLIDETWIN_MCP_ALLOWED_ORIGINS  localhost      Origin values /mcp accepts (same format)
        SLIDETWIN_RATE_LIMIT_PER_S   0 (off)          requests per second per client (image: 20)
        SLIDETWIN_RATE_LIMIT_BURST   60               requests a client may send at once
        """
        env = os.environ if environ is None else environ
        config_dir = Path(env.get("SLIDETWIN_CONFIG_DIR", "configs"))
        cache_dir = env.get("SLIDETWIN_CACHE_DIR", "cache")
        log_dir = env.get("SLIDETWIN_LOG_DIR", "logs")
        if not log_dir:
            raise ConfigError(
                "SLIDETWIN_LOG_DIR must name a directory: trajectories are written to disk as "
                "they happen (SPEC 6.3)."
            )
        return cls(
            config_dir=config_dir,
            manifest=Path(
                env.get("SLIDETWIN_MANIFEST", str(config_dir / "slides" / "manifest.yaml"))
            ),
            slide_dir=Path(env.get("SLIDETWIN_SLIDE_DIR", "data/slides")),
            log_dir=Path(log_dir),
            cache_dir=Path(cache_dir) if cache_dir else None,
            max_sessions=_number(env, "SLIDETWIN_MAX_SESSIONS", "16", int),
            idle_ttl_s=_number(env, "SLIDETWIN_SESSION_TTL_S", "7200", float),
            max_age_s=_number(env, "SLIDETWIN_SESSION_MAX_AGE_S", "28800", float),
            max_body_bytes=_number(env, "SLIDETWIN_MAX_BODY_BYTES", "1000000", int),
            mcp_allowed_hosts=_list(env, "SLIDETWIN_MCP_ALLOWED_HOSTS", LOCAL_HOSTS),
            mcp_allowed_origins=_list(env, "SLIDETWIN_MCP_ALLOWED_ORIGINS", LOCAL_ORIGINS),
            rate_limit_per_s=_number(env, "SLIDETWIN_RATE_LIMIT_PER_S", "0", float),
            rate_limit_burst=_number(env, "SLIDETWIN_RATE_LIMIT_BURST", "60", int),
        )

    def check_log_dir(self) -> None:
        """Fail at startup, not on the first session, if trajectories cannot be written."""
        sessions = self.log_dir / "sessions"
        probe = sessions / f".probe-{uuid.uuid4().hex}"
        try:
            sessions.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
        except OSError as err:
            raise ConfigError(
                f"SLIDETWIN_LOG_DIR is not writable ({type(err).__name__}).",
                hint="Point SLIDETWIN_LOG_DIR at a writable directory.",
            ) from None


def _number(env: Mapping[str, str], var: str, default: str, kind: type[int] | type[float]) -> int:
    raw = env.get(var, default)
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{var} must be a number, got {raw!r}.") from None
    out: int = value  # type: ignore[assignment]
    return out


def _list(env: Mapping[str, str], var: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = env.get(var)
    if raw is None:
        return default
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    if not items:
        raise ConfigError(f"{var} must list at least one value, or '*' for any.")
    return items
