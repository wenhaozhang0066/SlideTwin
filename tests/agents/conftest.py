"""Fixtures for agent tests: a live server on a synthetic tissue slide, reached over HTTP."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.config import ConfigStore
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from tests.conftest import CONFIG_DIR
from tests.helpers.live import serve
from tests.helpers.synth import write_tissue_slide

SECRET = "SECRET_AGENT_LABEL_654"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def tissue_manifest(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_tissue_slide(tmp_path_factory.mktemp("agent_slides"), eval_only={"label": SECRET})


@pytest.fixture(scope="module")
def live(
    tissue_manifest: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, SessionManager]]:
    """(base URL, the server's session manager)."""
    registry = SlideRegistry.from_manifest(tissue_manifest, tissue_manifest.parent)
    manager = SessionManager(
        registry=registry,
        configs=ConfigStore(CONFIG_DIR),
        log_dir=tmp_path_factory.mktemp("agent_logs"),
    )
    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=tissue_manifest,
        slide_dir=tissue_manifest.parent,
        log_dir=tmp_path_factory.mktemp("agent_logs2"),
        cache_dir=None,
    )
    with serve(create_app(settings, manager=manager)) as url:
        yield url, manager
