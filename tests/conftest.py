"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from slidetwin.core.config import ConfigStore, Profile, Protocol
from slidetwin.core.images import MemoryImageStore
from slidetwin.core.session import Session
from slidetwin.core.slide_source import SlideSource
from tests.helpers.synth import line_source

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "configs"
TESTDATA_DIR = Path(os.environ.get("SLIDETWIN_TESTDATA_DIR", REPO_ROOT / "data" / "testdata"))
CMU_SMALL = TESTDATA_DIR / "openslide-testdata" / "CMU-1-Small-Region.svs"


@pytest.fixture(scope="session")
def cmu_small_path() -> Path:
    """Path to the CC0 Aperio test slide (0.499 um/px, one level).

    Skips when the file is missing, unless SLIDETWIN_REQUIRE_TESTDATA=1 (CI), where a
    missing file is an error.
    """
    if not CMU_SMALL.exists():
        if os.environ.get("SLIDETWIN_REQUIRE_TESTDATA") == "1":
            pytest.fail(f"missing test slide {CMU_SMALL}; run scripts/fetch_test_data.py")
        pytest.skip("test slide not downloaded; run scripts/fetch_test_data.py")
    return CMU_SMALL


@pytest.fixture(scope="session")
def configs() -> ConfigStore:
    return ConfigStore(CONFIG_DIR)


@pytest.fixture(scope="session")
def viewer(configs: ConfigStore) -> Profile:
    return configs.profile("viewer")


@pytest.fixture(scope="session")
def unconstrained(configs: ConfigStore) -> Profile:
    return configs.profile("unconstrained")


@pytest.fixture(scope="session")
def protocol(configs: ConfigStore) -> Protocol:
    return configs.protocol("sln_metastasis")


@pytest.fixture(scope="session")
def synthetic() -> SlideSource:
    """2048x1536 px at 1.0 um/px (native 10x), 4 levels, lines at known positions."""
    return line_source()


@pytest.fixture(scope="session")
def synthetic40() -> SlideSource:
    """2048x1536 px at 0.25 um/px (native 40x), 5 levels."""
    return line_source(mpp=0.25, n_levels=5, name="synthetic40")


SessionFactory = Callable[..., Session]


@pytest.fixture
def make_session(viewer: Profile, protocol: Protocol, synthetic: SlideSource) -> SessionFactory:
    def factory(
        source: SlideSource | None = None,
        profile: Profile | None = None,
        **kwargs: Any,
    ) -> Session:
        kwargs.setdefault("actor", "agent:test")
        kwargs.setdefault("viewport_px", (1024, 768))
        return Session(
            session_id=kwargs.pop("session_id", "s-test"),
            slide_id=kwargs.pop("slide_id", "synthetic"),
            source=source or synthetic,
            slide_sha256=kwargs.pop("slide_sha256", "0" * 64),
            profile=profile or viewer,
            protocol=kwargs.pop("protocol", protocol),
            images=kwargs.pop("images", MemoryImageStore()),
            **kwargs,
        )

    return factory
