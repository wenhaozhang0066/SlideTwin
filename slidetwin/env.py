"""Training interface (M9): SlideTwin sessions inside the training process.

    from slidetwin.env import Env

    with Env("configs/slides/manifest.yaml", "data/slides") as env:
        step = env.reset("c16_fa751c")
        step = env.step({"action": "set_magnification", "mag": 10})
        step.image     # the view: an (h, w, 3) uint8 array
        step.response  # what REST and MCP return for the action, without the image

An Env holds one session at a time and runs it in the calling process: no HTTP, no JPEG round trip,
the same rules, costs and logs as the web UI, REST and MCP. An action is a dict: "action" names a
session action and the other keys are its REST request body (the MCP tool's parameters, and an
optional "client" with metadata for the log), checked by the same models as REST. NumPy numbers
are accepted. A rejected action is a step with an error, as REST and MCP report it: one REST refuses
before the session (INVALID_REQUEST) is not logged, one the session refuses is.

The environment computes no reward: ground truth never enters a step. Training code scores a
reading itself, for example from the receipt that an accepted report returns, or with
slidetwin.eval against the annotations. Without `log_dir` nothing is written to disk; with it,
every episode writes the trajectory a web session would, and it can be replayed.

VecEnv runs n environments in worker processes. They are spawned, so a script that makes a VecEnv
needs the usual `if __name__ == "__main__":` guard.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import numbers
import operator
import os
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ValidationError

from slidetwin.api.payloads import respond
from slidetwin.api.schemas import (
    ActionResponse,
    AnnotateRequest,
    CreateSessionResponse,
    CropRequest,
    EmptyRequest,
    MagnificationRequest,
    MeasureRequest,
    MoveToRequest,
    PanRequest,
    ReportRequest,
)
from slidetwin.core.config import ConfigStore, default_config_dir
from slidetwin.core.errors import SlideTwinError
from slidetwin.core.manager import SessionManager
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.session import ActionResult, Session

# Each action: the REST request body it takes, and the fields that become the Session method's
# arguments, in order.
ACTIONS: dict[str, tuple[type[BaseModel], tuple[str, ...]]] = {
    "get_state": (EmptyRequest, ()),
    "get_thumbnail": (EmptyRequest, ()),
    "set_magnification": (MagnificationRequest, ("mag",)),
    "pan": (PanRequest, ("dx", "dy")),
    "move_to": (MoveToRequest, ("x_um", "y_um")),
    "view": (EmptyRequest, ()),
    "measure": (MeasureRequest, ("p1_px", "p2_px")),
    "annotate": (AnnotateRequest, ("geometry", "label", "note")),
    "crop": (CropRequest, ("x_um", "y_um", "w_um", "h_um")),
    "submit_report": (ReportRequest, ("report",)),
}
IMAGE_FORMATS = ("array", "jpeg")
VIA_ENV = {"via": "env"}
MAX_BODY_BYTES = 1_000_000  # REST's default limit of a request body (SLIDETWIN_MAX_BODY_BYTES)
_PLAIN_DEPTH = 64  # NumPy values nested deeper than this are not converted


@dataclass(frozen=True)
class Step:
    """What reset or step returns.

    response: what REST and MCP return for the action (seq, action, cost_s, elapsed_s, state,
        observation, rule_violations; after reset also slide, task and rules), without the image.
        None when the action was rejected.
    error: the rejection (code, message, hint, and for a report its rule violations), as REST
        and MCP report it; None when the action was accepted.
    image: the pixels the action served: an (h, w, 3) uint8 array, read-only (copy it to change
        it), or with image="jpeg" the JPEG that REST and MCP serve. None when it served none.
    state: the session state after the action.
    done: a report was accepted, which closes the session; only get_state is left.
    """

    response: dict[str, Any] | None
    error: dict[str, Any] | None
    image: NDArray[np.uint8] | bytes | None
    state: dict[str, Any]
    done: bool

    @property
    def cost_s(self) -> float:
        """Virtual seconds the action cost (0 for a rejected action)."""
        return 0.0 if self.response is None else float(self.response["cost_s"])

    @property
    def elapsed_s(self) -> float:
        """Virtual seconds of the session so far."""
        return float(self.state["elapsed_s"])


class Env:
    """One session at a time on the slides of a manifest.

    Paths default as for the server (SLIDETWIN_CONFIG_DIR, SLIDETWIN_MANIFEST,
    SLIDETWIN_SLIDE_DIR, SLIDETWIN_CACHE_DIR), except that nothing is logged unless `log_dir` is
    given. `image` is "array" (the rendered pixels, no JPEG) or "jpeg" (the JPEG that REST and
    MCP serve, for training on exactly what an agent sees there).
    """

    def __init__(
        self,
        manifest: str | Path | None = None,
        slide_dir: str | Path | None = None,
        *,
        config_dir: str | Path | None = None,
        profile: str = "viewer",
        protocol: str = "sln_metastasis",
        actor: str = "agent:env",
        image: str = "array",
        viewport: Sequence[int] | None = None,
        log_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        if image not in IMAGE_FORMATS:
            raise ValueError(f"image must be one of {IMAGE_FORMATS}, got {image!r}")
        env = os.environ
        configs = Path(config_dir) if config_dir else default_config_dir()
        manifest = manifest or env.get("SLIDETWIN_MANIFEST") or configs / "slides" / "manifest.yaml"
        slide_dir = slide_dir or env.get("SLIDETWIN_SLIDE_DIR") or "data/slides"
        cache = cache_dir if cache_dir is not None else env.get("SLIDETWIN_CACHE_DIR", "cache")
        registry = SlideRegistry.from_manifest(
            Path(manifest), Path(slide_dir), cache_dir=Path(cache) if cache else None
        )
        store = ConfigStore(configs)
        store.profile(profile)  # an unknown profile or protocol fails here, not at reset
        store.protocol(protocol)
        self._manager = SessionManager(
            registry=registry,
            configs=store,
            log_dir=None if log_dir is None else Path(log_dir),
            max_sessions=1,
        )
        self.profile, self.protocol, self.actor = profile, protocol, actor
        self.image = image
        self.viewport = _viewport(viewport)
        self._session: Session | None = None
        self._rng = random.Random()

    @property
    def slide_ids(self) -> list[str]:
        return sorted(entry["id"] for entry in self._manager.catalogue())

    @property
    def session(self) -> Session | None:
        """The current session (for its trajectory or receipt), or None before the first reset."""
        return self._session

    def reset(
        self,
        slide_id: str | None = None,
        *,
        seed: int | None = None,
        viewport: Sequence[int] | None = None,
    ) -> Step:
        """End the current session and open `slide_id` (or, without one, a slide chosen by the
        seeded generator: `seed` reseeds it). Returns the first view, the slide, task and rules."""
        if seed is not None:
            self._rng.seed(operator.index(seed))  # NumPy integers too
        if slide_id is None:
            slide_id = self._rng.choice(self.slide_ids)
        size = _viewport(viewport) if viewport is not None else self.viewport
        self._end()
        session = self._manager.create(
            slide_id=slide_id,
            actor=self.actor,
            profile=self.profile,
            protocol=self.protocol,
            viewport_px=size,
            view_format="array" if self.image == "array" else "jpeg",
            store_images=False,  # each image is handed over in its step; none is fetched later
        )
        self._session = session
        return self._accepted(session, session.initial, session.opening())

    def step(self, action: Mapping[str, Any]) -> Step:
        """Run one action on the current session."""
        session = self._session
        if session is None:
            raise RuntimeError("Call reset() before step().")
        try:
            name, args, client = _parse(action)
        except _Refused as refused:  # as REST refuses a body before the session: not logged
            return Step(None, refused.error, None, session.state_dict(), session.closed)
        try:
            result = getattr(session, name)(*args, client=client)
        except SlideTwinError as err:
            return Step(None, err.to_dict(), None, session.state_dict(), session.closed)
        return self._accepted(session, result)

    def trajectory(self) -> str:
        """The current session's log as JSONL (logged as export_trajectory)."""
        if self._session is None:
            raise RuntimeError("Call reset() first.")
        return self._session.export_trajectory(client=VIA_ENV).text or ""

    def close(self) -> None:
        self._end()
        self._manager.close_all()
        self._manager.registry.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _end(self) -> None:
        if self._session is not None:
            self._manager.end(self._session.session_id)
            self._session = None

    def _accepted(
        self, session: Session, result: ActionResult, opening: dict[str, Any] | None = None
    ) -> Step:
        """The step of an accepted action: its response as REST and MCP serialise it (through
        the same response models), without the image, which is in Step.image."""
        data = respond(session, result, False)
        model: type[ActionResponse] = ActionResponse
        if opening is not None:
            data.update(opening)
            model = CreateSessionResponse
        response = model.model_validate(data).model_dump(mode="json")
        if isinstance(response.get("observation"), dict):
            response["observation"].pop("image", None)
        image = result.array if self.image == "array" else result.image
        if isinstance(image, np.ndarray):
            image.setflags(write=False)  # as it comes from the renderer, and from VecEnv
        return Step(response, None, image, result.state, session.closed)


def _viewport(value: Sequence[Any] | None) -> tuple[int, int] | None:
    """A viewport size: two integers (NumPy ones too), as REST and MCP take it; the session
    checks the range."""
    if value is None:
        return None
    items = list(value)
    if len(items) != 2 or not all(
        isinstance(v, numbers.Integral) and not isinstance(v, bool) for v in items
    ):
        raise ValueError(f"viewport must be two integers [width, height], got {value!r}")
    return int(items[0]), int(items[1])


class _Refused(Exception):
    """An action refused before it reaches the session, with the error REST would return."""

    def __init__(self, message: str, hint: str = "", code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.error = {"code": code, "message": message, **({"hint": hint} if hint else {})}


def _parse(action: Mapping[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
    """The action's name, the Session method's arguments and the client metadata, checked by
    the action's REST request model."""
    name = action.get("action") if isinstance(action, Mapping) else None
    if not isinstance(name, str) or name not in ACTIONS:
        raise _Refused(f"Unknown action {str(name)[:40]!r}.", f"Use one of: {', '.join(ACTIONS)}.")
    model, fields = ACTIONS[name]
    try:
        raw = {k: _plain(v) for k, v in action.items() if k != "action"}
        size = len(json.dumps(raw))
    except RecursionError:
        raise _Refused("Invalid request: body: nested too deeply.") from None
    except (TypeError, ValueError):
        raise _Refused(
            "Invalid request: body: not JSON.",
            "Use numbers, strings, booleans, null, lists and dicts.",
        ) from None
    if size > MAX_BODY_BYTES:  # as REST: refused before the session, not logged
        raise _Refused(
            f"Request body is larger than {MAX_BODY_BYTES} bytes.", code="PAYLOAD_TOO_LARGE"
        )
    try:
        body = model.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        raise _Refused(
            f"Invalid request: {where}: {first.get('msg', 'invalid')}.",
            f"Parameters of {name}: {', '.join(fields) or 'none'} (and client).",
        ) from None
    args = [getattr(body, f) for f in fields]
    if name == "measure":
        args = [list(p) for p in args]  # as the REST route passes them
    client = getattr(body, "client", None)
    return name, args, {**client, **VIA_ENV} if client else dict(VIA_ENV)


def _plain(value: Any, depth: int = 0) -> Any:
    """NumPy numbers and arrays as Python numbers and lists, which the strict models accept."""
    if depth > _PLAIN_DEPTH:
        return value  # deeper: left as it is; the models and the session judge it
    if isinstance(value, np.generic | np.ndarray):
        return value.tolist()
    if isinstance(value, list | tuple):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v, depth + 1) for k, v in value.items()}
    return value


# --- several environments in worker processes ----------------------------------------------------


class VecEnv:
    """n environments, each in its own worker process with its own slide handles. reset and step
    take and return lists, one item per environment; the workers run their actions in parallel.
    The keyword arguments are those of Env, plus `timeout_s`: how long a call waits for a worker
    (None: no limit). An error a worker reports is raised once every worker has answered, and the
    VecEnv stays usable. After a call that did not finish (a worker stopped or timed out, or the
    call was interrupted) every later call raises: close the VecEnv and make a new one."""

    def __init__(self, n: int, *, timeout_s: float | None = None, **env_kwargs: Any) -> None:
        n = operator.index(n)  # NumPy integers too
        if n < 1:
            raise ValueError("n must be a positive integer")
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError("timeout_s must be positive, or None for no limit")
        self._timeout_s = timeout_s
        self._broken: str | None = None
        context = multiprocessing.get_context("spawn")
        self._conns: list[Any] = []
        self._procs: list[Any] = []
        try:
            for _ in range(n):
                ours, theirs = context.Pipe()
                proc = context.Process(target=_work, args=(theirs, env_kwargs), daemon=True)
                proc.start()
                theirs.close()
                self._conns.append(ours)
                self._procs.append(proc)
            _, problems = self._gather()  # each worker reports once its Env is built
            if problems:
                raise RuntimeError(" ".join(problems))
        except BaseException:
            self.close()
            raise

    def __len__(self) -> int:
        return len(self._conns)

    def reset(
        self,
        slide_ids: Sequence[str | None] | None = None,
        *,
        seed: int | None = None,
        viewport: Sequence[int] | None = None,
    ) -> list[Step]:
        """Reset every environment: environment i opens slide_ids[i] (or a slide its generator
        chooses), and with `seed` its generator is reseeded with seed + i."""
        ids = list(slide_ids) if slide_ids is not None else [None] * len(self)
        if len(ids) != len(self):
            raise ValueError(f"expected {len(self)} slide ids, got {len(ids)}")
        base = None if seed is None else operator.index(seed)
        size = _viewport(viewport)
        messages = [
            ("reset", ids[i], None if base is None else base + i, size) for i in range(len(self))
        ]
        steps: list[Step] = self._call(messages)
        return steps

    def step(self, actions: Sequence[Any]) -> list[Step]:
        """One action per environment; an action that is not a dict is refused by its Env, as
        Env.step refuses it."""
        if len(actions) != len(self):
            raise ValueError(f"expected {len(self)} actions, got {len(actions)}")
        steps: list[Step] = self._call([("step", action) for action in actions])
        return steps

    def close(self) -> None:
        for conn in self._conns:
            with contextlib.suppress(OSError):  # that worker is gone already
                conn.send(("close",))
        for proc in self._procs:
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join()
        for conn in self._conns:
            conn.close()
        self._conns, self._procs = [], []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _call(self, messages: list[tuple[Any, ...]]) -> list[Any]:
        """Send one message to each worker and read every answer. Each message is pickled before
        any is sent, so one that cannot be is refused with nothing sent."""
        if self._broken is not None:
            raise RuntimeError(self._broken)
        payloads = [bytes(ForkingPickler.dumps(message)) for message in messages]
        try:
            for i, payload in enumerate(payloads):
                self._send(i, payload)
            answers, problems = self._gather()
        except BaseException as err:
            self._broken = (
                f"An earlier call to this VecEnv did not finish ({type(err).__name__}: {err}); "
                "close it and make a new one."
            )
            raise
        if problems:
            raise RuntimeError(" ".join(problems))
        return answers

    def _send(self, i: int, payload: bytes) -> None:
        try:
            self._conns[i].send_bytes(payload)
        except OSError:
            raise RuntimeError(self._stopped(i)) from None

    def _gather(self) -> tuple[list[Any], list[str]]:
        """Every worker's answer and the errors the workers reported. A worker that stopped, or
        that has not answered when the call's time is up (it is killed), raises."""
        deadline = None if self._timeout_s is None else time.monotonic() + self._timeout_s
        answers: list[Any] = []
        problems = []
        for i, conn in enumerate(self._conns):
            if deadline is not None and not conn.poll(max(0.0, deadline - time.monotonic())):
                self._procs[i].kill()
                raise RuntimeError(f"Environment {i} did not answer within {self._timeout_s:g} s.")
            try:
                status, value = conn.recv()
            except (EOFError, OSError):
                raise RuntimeError(self._stopped(i)) from None
            if status != "ok":
                problems.append(f"Environment {i} failed: {value}")
            elif isinstance(value, Step) and isinstance(value.image, np.ndarray):
                value.image.setflags(write=False)  # read-only, as from a plain Env
            answers.append(value)
        return answers, problems

    def _stopped(self, i: int) -> str:
        self._procs[i].join(1)
        return f"Environment {i} stopped (exit code {self._procs[i].exitcode})."


def _work(conn: Any, env_kwargs: dict[str, Any]) -> None:
    """A worker: build the Env, then answer reset and step until told to close."""
    try:
        env = Env(**env_kwargs)
    except Exception as err:
        conn.send(("error", f"{type(err).__name__}: {err}"))
        return
    conn.send(("ok", None))
    with env:
        while True:
            try:
                message = conn.recv()
            except (EOFError, OSError):
                return  # the parent is gone
            try:
                if message[0] == "close":
                    return
                if message[0] == "reset":
                    _, slide_id, seed, viewport = message
                    conn.send(("ok", env.reset(slide_id, seed=seed, viewport=viewport)))
                else:
                    conn.send(("ok", env.step(message[1])))
            except Exception as err:
                conn.send(("error", f"{type(err).__name__}: {err}"))
