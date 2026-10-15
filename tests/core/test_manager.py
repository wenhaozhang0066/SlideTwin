from __future__ import annotations

from pathlib import Path

import pytest

from slidetwin.core.config import ConfigStore
from slidetwin.core.errors import InvalidParameter, SessionNotFound, TooManySessions
from slidetwin.core.images import DiskImageStore, MemoryImageStore
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.slide_source import SlideSource
from tests.core.test_report import EXAMPLE


@pytest.fixture
def registry(synthetic: SlideSource) -> SlideRegistry:
    reg = SlideRegistry([], Path("."))
    reg.add_source("syn", synthetic, "a" * 64)
    return reg


def make(registry: SlideRegistry, configs: ConfigStore, **kw: object) -> SessionManager:
    return SessionManager(registry=registry, configs=configs, **kw)  # type: ignore[arg-type]


def test_rejects_bad_limits(registry: SlideRegistry, configs: ConfigStore) -> None:
    with pytest.raises(ValueError, match="max_sessions"):
        make(registry, configs, max_sessions=0)
    with pytest.raises(ValueError, match="idle_ttl_s"):
        make(registry, configs, idle_ttl_s=0)


def test_create_get_and_memory_storage(registry: SlideRegistry, configs: ConfigStore) -> None:
    m = make(registry, configs)
    s = m.create(slide_id="syn", actor="agent:x", viewport_px=(256, 192))
    assert len(m) == 1
    assert m.get(s.session_id) is s
    assert isinstance(s._images, MemoryImageStore)
    assert m.trajectory(s.session_id).startswith('{"type":"header"')


def test_unknown_profile_or_protocol_is_a_client_error(
    registry: SlideRegistry, configs: ConfigStore
) -> None:
    m = make(registry, configs)
    with pytest.raises(InvalidParameter, match="Unknown profile 'zoom'"):
        m.create(slide_id="syn", actor="agent:x", profile="zoom")
    with pytest.raises(InvalidParameter, match="Unknown protocol"):
        m.create(slide_id="syn", actor="agent:x", protocol="t9")


def test_disk_storage_and_expired_trajectory(
    registry: SlideRegistry, configs: ConfigStore, tmp_path: Path
) -> None:
    now = [0.0]
    m = make(registry, configs, log_dir=tmp_path, idle_ttl_s=10.0, clock=lambda: now[0])
    s = m.create(slide_id="syn", actor="human:r1", viewport_px=(256, 192))
    assert isinstance(s._images, DiskImageStore)
    assert (tmp_path / "sessions" / f"{s.session_id}.jsonl").is_file()
    assert (tmp_path / "sessions" / s.session_id / "v_0001.jpg").is_file()
    now[0] = 5.0
    m.get(s.session_id)  # touching keeps it alive
    now[0] = 14.0
    m.sweep()
    assert len(m) == 1
    now[0] = 30.0
    m.sweep()
    assert len(m) == 0
    with pytest.raises(SessionNotFound):
        m.get(s.session_id)
    assert m.trajectory(s.session_id).count("\n") == 2  # header + create_session
    for bad in ("../../etc/passwd", "f" * 32):
        with pytest.raises(SessionNotFound):
            m.trajectory(bad)


def test_trajectory_of_unknown_session_without_log_dir(
    registry: SlideRegistry, configs: ConfigStore
) -> None:
    with pytest.raises(SessionNotFound):
        make(registry, configs).trajectory("0" * 32)


def test_limit_and_closed_session_eviction(registry: SlideRegistry, configs: ConfigStore) -> None:
    m = make(registry, configs, max_sessions=2)
    a = m.create(slide_id="syn", actor="agent:a", viewport_px=(256, 192))
    b = m.create(slide_id="syn", actor="agent:b", viewport_px=(256, 192))
    with pytest.raises(TooManySessions, match=r"All 2 session slots"):
        m.create(slide_id="syn", actor="agent:c", viewport_px=(256, 192))
    b.submit_report(EXAMPLE)
    c = m.create(slide_id="syn", actor="agent:c", viewport_px=(256, 192))
    assert m.get(a.session_id) is a
    assert m.get(c.session_id) is c
    with pytest.raises(SessionNotFound):
        m.get(b.session_id)
    m.close_all()
    assert len(m) == 0


def test_malformed_ids_are_not_found(registry: SlideRegistry, configs: ConfigStore) -> None:
    m = make(registry, configs)
    for bad in ("", "abc", "G" * 32, "0" * 33):
        with pytest.raises(SessionNotFound):
            m.get(bad)
