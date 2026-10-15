"""Rate limiting (SPEC 14): a token bucket per client on /api and /mcp."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slidetwin.api.limits import RateLimit
from slidetwin.api.settings import Settings
from slidetwin.core.errors import ConfigError


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def limited_app(clock: Clock, **kw: Any) -> tuple[TestClient, RateLimit]:
    app = FastAPI()

    @app.get("/api/x")
    def x() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/static/y")
    def y() -> dict[str, bool]:
        return {"ok": True}

    kw.setdefault("rate_per_s", 1.0)
    kw.setdefault("burst", 3)
    app.add_middleware(RateLimit, clock=clock, **kw)
    client = TestClient(app)
    client.get("/static/y")  # builds the middleware stack
    limiter = app.middleware_stack
    while not isinstance(limiter, RateLimit):
        limiter = limiter.app  # type: ignore[union-attr]
    return client, limiter


def test_a_burst_then_the_sustained_rate() -> None:
    clock = Clock()
    client, _ = limited_app(clock)
    assert [client.get("/api/x").status_code for _ in range(3)] == [200, 200, 200]
    refused = client.get("/api/x")
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert "Wait" in refused.json()["error"]["hint"]
    assert all(client.get("/static/y").status_code == 200 for _ in range(10))  # not limited
    clock.t += 1.0
    assert client.get("/api/x").status_code == 200
    assert client.get("/api/x").status_code == 429


def test_clients_have_their_own_buckets_and_old_ones_are_dropped() -> None:
    clock = Clock()
    client, limiter = limited_app(clock, burst=1, max_clients=2)
    assert client.get("/api/x").status_code == 200
    assert client.get("/api/x").status_code == 429
    limiter.buckets["a"] = (1.0, clock.t)
    limiter.buckets["b"] = (1.0, clock.t)
    client.get("/api/x")
    assert list(limiter.buckets) == ["b", "testclient"]


def test_rate_zero_turns_it_off() -> None:
    client, _ = limited_app(Clock(), rate_per_s=0.0, burst=1)
    assert all(client.get("/api/x").status_code == 200 for _ in range(5))


def test_settings_validate_the_limits(tmp_path: Any) -> None:
    base = {
        "config_dir": tmp_path,
        "manifest": tmp_path,
        "slide_dir": tmp_path,
        "log_dir": tmp_path,
        "cache_dir": None,
    }
    with pytest.raises(ConfigError, match="RATE_LIMIT_PER_S"):
        Settings(**base, rate_limit_per_s=-1.0)
    with pytest.raises(ConfigError, match="RATE_LIMIT_BURST"):
        Settings(**base, rate_limit_burst=0)
    env = {"SLIDETWIN_RATE_LIMIT_PER_S": "0", "SLIDETWIN_RATE_LIMIT_BURST": "5"}
    s = Settings.from_env(env)
    assert (s.rate_limit_per_s, s.rate_limit_burst) == (0.0, 5)
