"""FastAPI application entry point: `uvicorn slidetwin.api.main:app`.

The module-level `app` is built on first access from environment settings (see `Settings`), so
importing this module has no side effects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from slidetwin import __version__
from slidetwin.api.errors import BodySizeLimit, install_error_handlers
from slidetwin.api.routes import router
from slidetwin.api.settings import Settings
from slidetwin.core.config import ConfigStore
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from slidetwin.version import git_sha

DESCRIPTION = """Agent-operable digital pathology viewer simulator.
**Research prototype, not for clinical use.**

Humans and agents use the same session actions. Every pixel comes from a logged action (view,
thumbnail or crop); coordinates are micrometres from the level-0 top-left corner; time is a virtual
clock in seconds."""


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
    )


def create_app(
    settings: Settings | None = None, *, manager: SessionManager | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    mgr = manager if manager is not None else build_manager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        mgr.close_all()
        mgr.registry.close()

    app = FastAPI(
        title="SlideTwin", version=__version__, description=DESCRIPTION, lifespan=lifespan
    )
    app.state.manager = mgr
    install_error_handlers(app)
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict[str, str]:  # async: answers even when worker threads are busy
        return {"status": "ok", "version": __version__, "git_sha": git_sha()}

    app.include_router(router)
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
