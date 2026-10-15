"""Request rate limiting (SPEC 14): a token bucket per client address on /api and /mcp.

Off by default, so that local agents run at full speed; the Docker image (the public Space) turns
it on.

The reading UI's static files are not limited. Behind a proxy the address comes from the proxy
headers that uvicorn is told to trust (`FORWARDED_ALLOW_IPS`).
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LIMITED_PREFIXES = ("/api/", "/mcp")


class RateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_per_s: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = 10_000,
    ) -> None:
        self.app = app
        self.rate = rate_per_s
        self.burst = float(burst)
        self.clock = clock
        self.max_clients = max_clients
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or self.rate <= 0
            or not str(scope.get("path", "")).startswith(LIMITED_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = str(client[0]) if client else "unknown"
        now = self.clock()
        tokens, last = self.buckets.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        allowed = tokens >= 1
        self.buckets[key] = (tokens - 1 if allowed else tokens, now)
        while len(self.buckets) > self.max_clients:
            self.buckets.popitem(last=False)
        if allowed:
            await self.app(scope, receive, send)
            return
        wait = (1 - tokens) / self.rate
        response = JSONResponse(
            {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "Too many requests from this address.",
                    "hint": f"Wait {wait:.1f} s and try again.",
                }
            },
            status_code=429,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
        await response(scope, receive, send)
