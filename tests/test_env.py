"""Training interface (M9): Env and VecEnv run the same sessions as REST and MCP."""

from __future__ import annotations

import io
import json
import os
import signal
import threading
from multiprocessing import Pipe
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.errors import ConfigError
from slidetwin.core.images import pixel_sha256
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.replay import replay
from slidetwin.env import Env, Step, VecEnv, _work
from tests.conftest import CONFIG_DIR
from tests.core.test_report import EXAMPLE
from tests.helpers.synth import line_image, pyramid, write_pyramid_tiff

POLYGON = {"type": "polygon", "coordinates": [[1, 1], [50, 1], [50, 40]]}
SCRIPT: list[dict[str, Any]] = [
    {"action": "get_thumbnail"},
    {"action": "set_magnification", "mag": 10},
    {"action": "pan", "dx": 0.5, "dy": 0.25, "client": {"think_ms": 50}},
    {"action": "move_to", "x_um": 200.3, "y_um": 150.7},
    {"action": "set_magnification", "mag": 7},  # the session refuses it, and logs it
    {"action": "set_magnification", "mag": 40},
    {"action": "view"},
    {"action": "measure", "p1_px": [10.5, 20.25], "p2_px": [300, 200]},
    {"action": "annotate", "geometry": POLYGON, "label": "cluster", "note": "x"},
    {"action": "get_state"},
    {"action": "submit_report", "report": EXAMPLE},
]
REST = {  # action -> (method, route, body keys)
    "get_thumbnail": ("post", "thumbnail"),
    "set_magnification": ("post", "magnification"),
    "pan": ("post", "pan"),
    "move_to": ("post", "move_to"),
    "view": ("post", "view"),
    "measure": ("post", "measure"),
    "annotate": ("post", "annotations"),
    "get_state": ("get", "state"),
    "submit_report": ("post", "report"),
}
VARYING = frozenset({"t_wall_ms", "client", "jpeg_sha256", "session_id", "created_utc"})


@pytest.fixture(scope="module")
def slides(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("env")
    lines = line_image(2048, 1536, (300, 1000), (200, 777))
    write_pyramid_tiff(d / "syn40.tif", pyramid(lines, 5), 0.25)  # native 40x
    write_pyramid_tiff(d / "syn20.tif", pyramid(lines, 4), 0.5)  # native 20x
    entries = [{"id": "syn40", "path": "syn40.tif"}, {"id": "syn20", "path": "syn20.tif"}]
    (d / "manifest.yaml").write_text(yaml.safe_dump({"slides": entries}))
    return d


def make_env(slides: Path, **kw: Any) -> Env:
    kw.setdefault("config_dir", CONFIG_DIR)
    kw.setdefault("cache_dir", "")
    kw.setdefault("viewport", (512, 384))
    return Env(slides / "manifest.yaml", slides, **kw)


def normalised(text: str) -> list[Any]:
    """A trajectory without what differs between two runs of the same reading."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in VARYING}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return [strip(json.loads(line)) for line in text.splitlines()]


def rest_reading(slides: Path, tmp_path: Path) -> str:
    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=slides / "manifest.yaml",
        slide_dir=slides,
        log_dir=tmp_path / "rest",
        cache_dir=None,
        allowed_hosts=("testserver",),
    )
    with TestClient(create_app(settings)) as client:
        body = {"slide_id": "syn40", "actor": "agent:env", "viewport_px": [512, 384]}
        sid = client.post("/api/sessions", json=body).json()["session_id"]
        for action in SCRIPT:
            method, route = REST[action["action"]]
            payload = {k: v for k, v in action.items() if k != "action"}
            url = f"/api/sessions/{sid}/{route}"
            client.get(url) if method == "get" else client.post(url, json=payload)
        return client.get(f"/api/sessions/{sid}/trajectory").text


def test_a_reading_logs_the_same_events_as_over_rest(slides: Path, tmp_path: Path) -> None:
    with make_env(slides, log_dir=tmp_path / "env") as env:
        first = env.reset("syn40")
        steps = [env.step(a) for a in SCRIPT]
        text = env.trajectory()
    assert normalised(text) == normalised(rest_reading(slides, tmp_path))
    # every view served as an array is the image whose pixel hash the log records
    observed = [first, *steps]
    hashes = [s.response["observation"]["pixel_sha256"] for s in observed if s.image is not None]
    assert hashes == [pixel_sha256(s.image) for s in observed if isinstance(s.image, np.ndarray)]
    assert len(hashes) == 7  # the first view, the thumbnail, four moves and the view
    logged = sorted((tmp_path / "env" / "sessions").glob("*.jsonl"))
    assert len(logged) == 1  # the log a web session writes, and it replays
    registry = SlideRegistry.from_manifest(slides / "manifest.yaml", slides)
    assert replay(logged[0], registry).ok


def test_steps(slides: Path) -> None:
    with make_env(slides) as env:
        assert env.slide_ids == ["syn20", "syn40"]
        first = env.reset("syn40")
        assert first.response is not None
        assert {"slide", "task", "rules"} <= set(first.response)
        assert "image" not in first.response["observation"]  # the pixels are in Step.image
        assert isinstance(first.image, np.ndarray)
        assert first.image.shape == (384, 512, 3)
        assert (first.cost_s, first.done) == (first.response["cost_s"], False)
        moved = env.step({"action": "pan", "dx": np.float32(0.5), "dy": np.int64(0)})
        assert moved.error is None  # NumPy numbers are plain numbers here
        assert moved.elapsed_s == pytest.approx(first.elapsed_s + moved.cost_s)
        refused = env.step({"action": "set_magnification", "mag": 7})
        assert refused.error is not None
        assert (refused.error["code"], refused.cost_s, refused.image) == ("MAG_NOT_PRESET", 0, None)
        assert refused.state == moved.state
        state = env.step({"action": "get_state"})
        assert state.image is None
        report = env.step({"action": "submit_report", "report": EXAMPLE})
        assert report.done
        assert report.response is not None
        assert report.response["observation"]["type"] == "report_receipt"
        closed = env.step({"action": "pan", "dx": 0.1, "dy": 0})
        assert closed.error is not None
        assert (closed.error["code"], closed.done) == ("SESSION_CLOSED", True)


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ({"action": "zoom"}, "Unknown action 'zoom'"),
        ("pan", "Unknown action 'None'"),
        ({"action": "pan", "dx": 0.5}, "Invalid request: dy: Field required"),
        ({"action": "pan", "dx": "0.5", "dy": 0}, "Invalid request: dx:"),
        ({"action": "view", "mag": 10}, "Invalid request: mag: Extra inputs are not permitted"),
        ({"action": "measure", "p1_px": [1], "p2_px": [2, 3]}, "Invalid request: p1_px:"),
    ],
)
def test_requests_rest_refuses_are_refused_and_not_logged(
    slides: Path, action: Any, message: str
) -> None:
    with make_env(slides) as env:
        env.reset("syn40")
        before = env.session.history if env.session else []
        step = env.step(action)
        assert step.error is not None
        assert step.error["code"] == "INVALID_REQUEST"
        assert step.error["message"].startswith(message)
        assert step.response is None
        assert env.session is not None
        assert env.session.history == before


def test_jpeg_steps_serve_what_rest_serves(slides: Path) -> None:
    with make_env(slides, image="jpeg") as env:
        first = env.reset("syn40")
        assert isinstance(first.image, bytes)
        view_id = first.response["observation"]["view_id"] if first.response else ""
        assert env.session is not None
        assert first.image == env.session.get_image(view_id)
        assert Image.open(io.BytesIO(first.image)).size == (512, 384)


def test_reset_ends_the_previous_session_and_seeds_the_slide_choice(slides: Path) -> None:
    with make_env(slides) as env:
        with pytest.raises(RuntimeError, match="reset"):
            env.step({"action": "view"})
        with pytest.raises(RuntimeError, match="reset"):
            env.trajectory()
        env.reset("syn20")
        old = env.session
        assert old is not None
        chosen = [env.reset(seed=7 if i == 0 else None).state["slide_id"] for i in range(8)]
        assert old.terminated  # reset ended it
        again = [env.reset(seed=7 if i == 0 else None).state["slide_id"] for i in range(8)]
        assert again == chosen  # the same seed, the same slides
        assert set(chosen) == {"syn20", "syn40"}


def test_constructor_checks(slides: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="image must be one of"):
        make_env(slides, image="png")
    with pytest.raises(ConfigError):
        make_env(slides, profile="nope")
    monkeypatch.chdir(tmp_path)  # defaults: SLIDETWIN_* variables, then the repository layout
    monkeypatch.setenv("SLIDETWIN_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("SLIDETWIN_MANIFEST", str(slides / "manifest.yaml"))
    monkeypatch.setenv("SLIDETWIN_SLIDE_DIR", str(slides))
    monkeypatch.setenv("SLIDETWIN_CACHE_DIR", "")
    with Env() as env:
        assert env.reset("syn20").error is None
    assert not any(tmp_path.iterdir())  # nothing written without log_dir


# --- VecEnv ---------------------------------------------------------------------------------------


def comparable(step: Step) -> tuple[Any, ...]:
    """A step without its session id, and its image as a hash."""
    response = json.loads(json.dumps(step.response))
    if response is not None:
        response.pop("session_id")
        response["state"].pop("session_id")
    image = None if not isinstance(step.image, np.ndarray) else pixel_sha256(step.image)
    return response, step.error, image, step.done


def test_vec_env_steps_like_single_envs(slides: Path) -> None:
    kwargs = {"config_dir": CONFIG_DIR, "cache_dir": "", "viewport": (512, 384)}
    with VecEnv(2, manifest=slides / "manifest.yaml", slide_dir=slides, **kwargs) as vec:
        assert len(vec) == 2
        firsts = vec.reset(["syn40", "syn20"])
        steps = vec.step([SCRIPT[1], SCRIPT[2]])
        with pytest.raises(ValueError, match="expected 2 actions"):
            vec.step([SCRIPT[1]])
        with pytest.raises(ValueError, match="expected 2 slide ids"):
            vec.reset(["syn40"])
        seeded = [s.state["slide_id"] for s in vec.reset(seed=3)]
    with make_env(slides) as env:
        for slide_id, first, action, step in zip(
            ["syn40", "syn20"], firsts, [SCRIPT[1], SCRIPT[2]], steps, strict=True
        ):
            alone = env.reset(slide_id)
            assert isinstance(first.image, np.ndarray)
            np.testing.assert_array_equal(first.image, alone.image)
            assert comparable(step) == comparable(env.step(action))
        assert seeded == [env.reset(seed=3 + i).state["slide_id"] for i in range(2)]


def test_vec_env_reports_a_worker_that_fails_or_stops(slides: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Environment 0 failed: ConfigError"):
        VecEnv(1, manifest=tmp_path / "missing.yaml", slide_dir=slides, cache_dir="")
    kwargs = {"config_dir": CONFIG_DIR, "cache_dir": ""}
    vec = VecEnv(2, manifest=slides / "manifest.yaml", slide_dir=slides, **kwargs)
    try:
        with pytest.raises(RuntimeError, match="Environment 1 failed: SlideNotFound"):
            vec.reset(["syn40", "nope"])
        assert [s.state["slide_id"] for s in vec.reset(["syn20", "syn40"])] == ["syn20", "syn40"]
        pid = vec._procs[0].pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        vec._procs[0].join()
        with pytest.raises(RuntimeError, match="Environment 0 stopped"):
            vec.reset(["syn40", "syn20"])
    finally:
        vec.close()
        vec.close()  # twice is fine
    with pytest.raises(ValueError, match="positive"):
        VecEnv(0)


class Stuck:
    """A worker process that ignores the close message, so close() has to kill it."""

    def __init__(self, proc: Any) -> None:
        self.proc, self.killed = proc, False

    def join(self, timeout: float | None = None) -> None:
        if timeout is None:
            self.proc.join()

    def is_alive(self) -> bool:
        return not self.killed

    def kill(self) -> None:
        self.killed = True
        self.proc.kill()


def test_vec_env_reads_every_answer_and_kills_a_stuck_worker(slides: Path) -> None:
    kwargs = {"config_dir": CONFIG_DIR, "cache_dir": ""}
    vec = VecEnv(1, manifest=slides / "manifest.yaml", slide_dir=slides, **kwargs)
    pid = vec._procs[0].pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    with pytest.raises(RuntimeError, match=r"Environment 0 stopped \(exit code -9\)"):
        vec._gather()  # an answer that never comes: the worker is gone
    vec.close()
    vec = VecEnv(1, manifest=slides / "manifest.yaml", slide_dir=slides, **kwargs)
    stuck = Stuck(vec._procs[0])
    vec._procs[0] = stuck  # type: ignore[assignment]
    vec.close()
    assert stuck.killed


def test_the_worker_loop(slides: Path) -> None:
    """_work runs in spawned processes, where coverage does not see it; here it runs in a
    thread."""
    ours, theirs = Pipe()
    kwargs = {"manifest": slides / "manifest.yaml", "slide_dir": slides, "config_dir": CONFIG_DIR}
    worker = threading.Thread(target=_work, args=(theirs, {**kwargs, "cache_dir": ""}))
    worker.start()
    assert ours.recv() == ("ok", None)
    ours.send(("reset", "syn40", None, (256, 256)))
    status, step = ours.recv()
    assert status == "ok"
    assert isinstance(step, Step)
    assert step.image is not None
    assert step.image.shape == (256, 256, 3)
    ours.send(("step", {"action": "view"}))
    assert ours.recv()[1].error is None
    ours.send(("reset", "nope", None, None))
    assert ours.recv() == ("error", "SlideNotFound: Unknown slide 'nope'.")
    ours.send(("close",))
    worker.join(10)
    assert not worker.is_alive()
    ours.close()
    ours, theirs = Pipe()  # a parent that goes away ends the worker too
    worker = threading.Thread(target=_work, args=(theirs, {**kwargs, "cache_dir": ""}))
    worker.start()
    ours.recv()
    ours.close()
    worker.join(10)
    assert not worker.is_alive()
    ours, theirs = Pipe()  # an Env that cannot be built is reported
    _work(theirs, {**kwargs, "cache_dir": "", "profile": "nope"})
    assert ours.recv()[0] == "error"
