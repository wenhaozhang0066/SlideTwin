"""FastAPI application entry point: `uvicorn slidetwin.api.main:app`."""

from __future__ import annotations

from fastapi import FastAPI

from slidetwin import __version__
from slidetwin.version import git_sha


def create_app() -> FastAPI:
    app = FastAPI(
        title="SlideTwin",
        version=__version__,
        description="Agent-operable digital pathology viewer simulator. "
        "Research prototype, not for clinical use.",
    )

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "git_sha": git_sha()}

    return app


app = create_app()
