"""FastAPI application entry point: `uvicorn slidetwin.api.main:app`.

The module-level `app` is built on first access from environment settings (see `Settings`), so
importing this module has no side effects.
"""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from slidetwin import __version__
from slidetwin.api.errors import BodySizeLimit, install_error_handlers
from slidetwin.api.limits import HostCheck, RateLimit
from slidetwin.api.routes import router
from slidetwin.api.settings import Settings
from slidetwin.core.config import ConfigStore
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from slidetwin.mcp_server import SDK_TRANSPORT_SECURITY, build_mcp
from slidetwin.readers import quiet_library_logs
from slidetwin.version import git_sha

DESCRIPTION = """Agent-operable digital pathology viewer simulator.
**Research prototype, not for clinical use.**

Humans and agents use the same session actions. Every pixel comes from a logged action (view,
thumbnail or crop); coordinates are micrometres from the level-0 top-left corner; time is a virtual
clock in seconds."""

# The reading UI (M3): static files that call only the /api routes below.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
mimetypes.add_type("text/javascript", ".js")  # module scripts need a JavaScript MIME type


class _RevalidatedStatic(StaticFiles):
    """Static files that browsers revalidate on every load, so a redeploy never leaves a reader
    on old scripts (ETag and Last-Modified make the check cheap)."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


def build_manager(settings: Settings) -> SessionManager:
    settings.check_log_dir()
    return SessionManager(
        registry=SlideRegistry.from_manifest(
            settings.manifest, settings.slide_dir, cache_dir=settings.cache_dir
        ),
        configs=ConfigStore(settings.config_dir),
        log_dir=settings.log_dir,
        max_sessions=settings.max_sessions,
        idle_ttl_s=settings.idle_ttl_s,
        max_age_s=settings.max_age_s,
        evict_idle_s=settings.evict_idle_s,
    )


def create_app(
    settings: Settings | None = None, *, manager: SessionManager | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    quiet_library_logs()
    mgr = manager if manager is not None else build_manager(settings)
    mcp = build_mcp(mgr)
    # Stateless JSON responses: tools take the session id, so MCP needs no transport session, and
    # plain JSON passes proxies that buffer event streams.
    mcp_http = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=SDK_TRANSPORT_SECURITY,
        max_request_body_size=settings.max_body_bytes,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():  # a mounted MCP app's own lifespan never runs
            yield
        mgr.close_all()
        mgr.registry.close()

    app = FastAPI(
        title="SlideTwin", version=__version__, description=DESCRIPTION, lifespan=lifespan
    )
    app.state.manager = mgr
    install_error_handlers(app)
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        RateLimit,
        rate_per_s=settings.rate_limit_per_s,
        burst=settings.rate_limit_burst,
        client_header=settings.rate_limit_client_header,
    )
    app.add_middleware(  # outermost: a misdirected request is refused before anything else
        HostCheck, hosts=settings.allowed_hosts, origins=settings.allowed_origins
    )

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict[str, str]:  # async: answers even when worker threads are busy
        return {"status": "ok", "version": __version__, "git_sha": git_sha()}

    app.include_router(router)
    # The MCP route itself (exactly /mcp), not a mount: a mount would answer /mcp with a redirect
    # to /mcp/ and send every unknown path to the MCP app instead of our 404 envelope.
    app.router.routes.extend(mcp_http.routes)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"cache-control": "no-cache"})

    app.mount("/static", _RevalidatedStatic(directory=WEB_DIR), name="static")
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Build `app` lazily for `uvicorn slidetwin.api.main:app`."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
