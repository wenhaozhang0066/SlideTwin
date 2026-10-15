"""Fixtures for the MCP adapter tests: a synthetic tissue slide behind a session manager."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from slidetwin.core.config import ConfigStore
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from tests.conftest import CONFIG_DIR
from tests.helpers.synth import write_tissue_slide

SECRET = "SECRET_MCP_LABEL_321"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def tissue_manifest(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_tissue_slide(tmp_path_factory.mktemp("mcp_slides"), eval_only={"label": SECRET})


@pytest.fixture
def manager(tissue_manifest: Path, tmp_path: Path) -> Iterator[SessionManager]:
    registry = SlideRegistry.from_manifest(tissue_manifest, tissue_manifest.parent)
    mgr = SessionManager(
        registry=registry, configs=ConfigStore(CONFIG_DIR), log_dir=tmp_path / "logs"
    )
    yield mgr
    mgr.close_all()
    registry.close()
