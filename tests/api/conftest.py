"""Fixtures for REST contract tests: a synthetic TIFF slide behind a manifest with ground truth."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.config import ConfigStore
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from tests.conftest import CONFIG_DIR
from tests.helpers.synth import LINES_X, LINES_Y, line_image, pyramid, write_pyramid_tiff

SECRET_LABEL = "SECRET_LABEL_TUMOR_123"
SECRET_ANNOTATION = "SECRET_ANNOTATION_456.xml"


@pytest.fixture(scope="session")
def slide_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("api_slides")
    img = line_image(2048, 1536, LINES_X, LINES_Y)
    write_pyramid_tiff(d / "syn40.tif", pyramid(img, 5), 0.25)  # native 40x
    write_pyramid_tiff(d / "syn20.tif", pyramid(img, 4), 0.5)  # native 20x
    return d


@pytest.fixture(scope="session")
def manifest(slide_dir: Path) -> Path:
    data = {
        "slides": [
            {
                "id": "syn40",
                "path": "syn40.tif",
                "license": "CC0-1.0",
                "attribution": "synthetic",
                "eval_only": {"label": SECRET_LABEL, "annotation": SECRET_ANNOTATION},
            },
            {"id": "syn20", "path": "syn20.tif", "eval_only": {"label": SECRET_LABEL}},
        ]
    }
    path = slide_dir / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def settings(tmp_path: Path, manifest: Path, slide_dir: Path) -> Settings:
    return Settings(
        config_dir=CONFIG_DIR,
        manifest=manifest,
        slide_dir=slide_dir,
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        max_sessions=8,
        idle_ttl_s=3600.0,
        max_body_bytes=200_000,
    )


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(settings: Settings) -> Iterator[ClientFactory]:
    clients: list[TestClient] = []

    def factory(config_dir: Path | None = None, **manager_kw: Any) -> TestClient:
        mgr = None
        if config_dir is not None or manager_kw:
            mgr = SessionManager(
                registry=SlideRegistry.from_manifest(
                    settings.manifest, settings.slide_dir, cache_dir=settings.cache_dir
                ),
                configs=ConfigStore(config_dir or settings.config_dir),
                log_dir=settings.log_dir,
                **manager_kw,
            )
        client = TestClient(create_app(settings, manager=mgr))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


def open_session(client: TestClient, **body: Any) -> dict[str, Any]:
    body.setdefault("slide_id", "syn40")
    body.setdefault("actor", "agent:test")
    body.setdefault("viewport_px", [512, 384])
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data
