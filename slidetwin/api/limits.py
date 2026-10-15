"""Request guards: a host check for every route, and rate limiting on /api and /mcp (SPEC 14).

The host check is DNS-rebinding protection: a local server may hold internal slides, so it answers
only requests addressed to an allowed host and, when a browser sends an Origin, coming from an
allowed host or origin.

The rate limit is a token bucket per client address. It is off by default, so that local agents run
at full speed; the Docker image turns it on. The UI's static files and the images of views already
served (the UI fetches one after every move) are not limited. Behind a proxy the address comes from
the proxy headers that uvicorn is told to trust (`FORWARDED_ALLOW_IPS`).

Behind a proxy, set `client_header` (e.g. `x-forwarded-for`): the bucket key is then the header's
last entry, the address the proxy itself saw. The first entries are written by the client and could
be forged to get a fresh bucket per request; uvicorn's own rewrite of the client address uses them.
Without a proxy, a client could set that header itself, so leave `client_header` empty there.
"""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LIMITED_PREFIXES = ("/api/", "/mcp")
IMAGE_PATH = re.compile(r"^/api/sessions/[0-9a-f]{32}/(views|thumbnails)/[tv]_\d+\.jpg$")


def _refuse(status: int, code: str, message: str, hint: str, **headers: str) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "hint": hint}}
    return JSONResponse(body, status_code=status, headers=headers or None)


def host_allowed(host: str, patterns: Sequence[str]) -> bool:
    """`host` (name or name:port) matches an entry, exactly or as `name:*` for any port."""
    host = host.lower()
    for p in patterns:
        p = p.lower()
        if host == p or (p.endswith(":*") and host.startswith(p[:-1])):
            return True
    return False


class HostCheck:
    def __init__(self, app: ASGIApp, *, hosts: Sequence[str], origins: Sequence[str]) -> None:
        self.app = app
        self.hosts = tuple(hosts)
        self.origins = tuple(o.lower().rstrip("/") for o in origins)

    def _origin_ok(self, origin: str) -> bool:
        """Listed origins, or any page on an allowed host. With hosts `*`, a listed set of
        origins still applies; without one, any origin passes."""
        if "*" in self.origins or origin.lower().rstrip("/") in self.origins:
            return True
        if "*" in self.hosts:
            return not self.origins
        netloc = urlsplit(origin).netloc
        return bool(netloc) and host_allowed(netloc, self.hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if "*" not in self.hosts and not host_allowed(host, self.hosts):
            response = _refuse(
                421,
                "HOST_NOT_ALLOWED",
                f"This server does not answer for host {host[:80]!r}.",
                "Open it by an allowed name, or add the name to SLIDETWIN_ALLOWED_HOSTS.",
            )
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin and origin != "null" and not self._origin_ok(origin):
            response = _refuse(
                403,
                "ORIGIN_NOT_ALLOWED",
                "Requests from this web page are not accepted.",
                "Use the reading UI served by this server, or add the page's origin to "
                "SLIDETWIN_ALLOWED_ORIGINS.",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_per_s: float,
        burst: int,
        client_header: str = "",
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = 10_000,
    ) -> None:
        self.app = app
        self.client_header = client_header.lower()
        self.rate = rate_per_s
        self.burst = float(burst)
        self.clock = clock
        self.max_clients = max_clients
        self.buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if (
            scope["type"] != "http"
            or self.rate <= 0
            or not path.startswith(LIMITED_PREFIXES)
            or IMAGE_PATH.fullmatch(path)
        ):
            await self.app(scope, receive, send)
            return
        key = ""
        if self.client_header:
            forwarded = Headers(scope=scope).get(self.client_header, "")
            key = forwarded.rsplit(",", 1)[-1].strip()
        if not key:
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
        response = _refuse(
            429,
            "RATE_LIMITED",
            "Too many requests from this address.",
            f"Wait {wait:.1f} s and try again.",
            **{"Retry-After": str(max(1, math.ceil(wait)))},
        )
        await response(scope, receive, send)
