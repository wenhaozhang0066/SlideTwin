"""Reading session: the state machine behind every adapter (SPEC 4, 7, 8, 9).

Every action runs through `_execute` in two phases:

1. Plan. Check that the session is open and within budget, validate the input and compute the
   result (render, measure) without touching any state. A failure here changes nothing and is
   logged as a failed event at zero cost.
2. Commit. Charge the ledger, store the image, apply the state change and append the event. If any
   step fails, every change is rolled back and the failure is logged as INTERNAL. A session that
   can no longer write its log stops accepting actions, so no pixel is ever served unlogged.

Successful events log their parameters in canonical JSON (the values the action actually used), so
they replay exactly. Failed events log a bounded, JSON-safe copy of the raw input.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slidetwin.core.config import Profile, Protocol, content_hash
from slidetwin.core.coverage import TissueMap, tissue_map
from slidetwin.core.errors import (
    ActionNotAllowed,
    ImageNotFound,
    InternalError,
    InvalidParameter,
    MagnificationNotPreset,
    MagnificationUnavailable,
    NoCurrentView,
    OutsideView,
    RuleViolation,
    SessionClosed,
    SlideMetadataError,
    SlideTwinError,
)
from slidetwin.core.geometry import (
    BBox,
    Point,
    Size,
    available_mags,
    clamp_center,
    crop_geometry,
    displacement_in_viewports,
    distance,
    format_mag,
    fov_um,
    mag_for_mpp,
    mpp_for_mag,
    thumbnail_size,
    view_px_to_um,
    viewport_bbox,
)
from slidetwin.core.images import ImageStore, encode_jpeg, is_image_id, pixel_sha256, sha256_bytes
from slidetwin.core.ledger import Ledger, to_s
from slidetwin.core.render import render_region
from slidetwin.core.report import validate_report
from slidetwin.core.rules import MeasurementFact, ViewFact, check_report
from slidetwin.core.slide_source import (
    RGB,
    SlideSource,
    native_mpp,
    slide_size_um,
    source_provenance,
)
from slidetwin.core.trajectory import SCHEMA_VERSION, TrajectoryWriter
from slidetwin.version import app_version

ACTOR_RE = re.compile(r"(human|agent):[A-Za-z0-9._-]{1,64}")
VIEWPORT_MIN, VIEWPORT_MAX = 64, 4096
CLIENT_META_MAX_BYTES = 4096
RAW_PARAMS_MAX_BYTES = 16384
AFTER_CLOSE = frozenset({"get_state", "export_trajectory"})
# How a session serves pixels: "jpeg" (REST, MCP, web UI) encodes and stores each view; "array"
# (the training interface) returns the rendered RGB array, with no JPEG and nothing stored.
VIEW_FORMATS = ("jpeg", "array")
LABEL_MAX, NOTE_MAX, POLYGON_MAX = 64, 1000, 1000
VIEW_ACTIONS = frozenset({"create_session", "view", "crop"})
MOVE_ACTIONS = frozenset({"set_magnification", "pan", "move_to"})


# --- observation records -------------------------------------------------------------------


@dataclass(frozen=True)
class ViewRecord:
    view_id: str
    kind: str  # "view" or "crop"
    seq: int
    center_um: Point
    mag: float
    mpp: float
    bbox_um: BBox
    size_px: Size
    pixel_sha256: str
    jpeg_sha256: str | None  # None when the session serves arrays

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "view_id": self.view_id,
            "seq": self.seq,
            "center_um": list(self.center_um),
            "mag": self.mag,
            "mpp": self.mpp,
            "bbox_um": self.bbox_um.to_list(),
            "size_px": list(self.size_px),
            "pixel_sha256": self.pixel_sha256,
            "jpeg_sha256": self.jpeg_sha256,
        }


@dataclass(frozen=True)
class ThumbnailRecord:
    thumb_id: str
    seq: int
    size_px: Size
    um_per_px: Point
    pixel_sha256: str
    jpeg_sha256: str | None  # None when the session serves arrays
    tissue_pieces: tuple[dict[str, Any], ...] = ()  # pieces a negative claim can cite

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "thumbnail",
            "thumb_id": self.thumb_id,
            "seq": self.seq,
            "size_px": list(self.size_px),
            "um_per_px": list(self.um_per_px),
            "pixel_sha256": self.pixel_sha256,
            "jpeg_sha256": self.jpeg_sha256,
            "tissue_pieces": list(self.tissue_pieces),
        }


@dataclass(frozen=True)
class MeasurementRecord:
    measurement_id: str
    seq: int
    view_id: str
    p1_px: Point
    p2_px: Point
    p1_um: Point
    p2_um: Point
    length_um: float
    length_px: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "measurement",
            "measurement_id": self.measurement_id,
            "seq": self.seq,
            "view_id": self.view_id,
            "p1_px": list(self.p1_px),
            "p2_px": list(self.p2_px),
            "p1_um": list(self.p1_um),
            "p2_um": list(self.p2_um),
            "length_um": self.length_um,
            "length_px": self.length_px,
        }


@dataclass(frozen=True)
class AnnotationRecord:
    annotation_id: str
    seq: int
    view_id: str
    geometry_type: str
    coords_px: tuple[Point, ...]
    coords_um: tuple[Point, ...]
    label: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "annotation",
            "annotation_id": self.annotation_id,
            "seq": self.seq,
            "view_id": self.view_id,
            "geometry_type": self.geometry_type,
            "coords_px": [list(p) for p in self.coords_px],
            "coords_um": [list(p) for p in self.coords_um],
            "label": self.label,
            "note": self.note,
        }


@dataclass(frozen=True)
class ReportReceipt:
    seq: int
    accepted: bool
    enforcement: str
    views_used: tuple[str, ...]
    rule_checks: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "report_receipt",
            "seq": self.seq,
            "accepted": self.accepted,
            "enforcement": self.enforcement,
            "views_used": list(self.views_used),
            "rule_checks": list(self.rule_checks),
            "violations": sum(1 for c in self.rule_checks if c["status"] == "fail"),
            "coverage": self.coverage,
        }


Observation = ViewRecord | ThumbnailRecord | MeasurementRecord | AnnotationRecord | ReportReceipt
# An observation and the served pixels: JPEG bytes, or the RGB array when the session serves arrays.
Apply = Callable[[int], tuple[Observation | None, bytes | RGB | None]]


@dataclass(frozen=True)
class _Plan:
    cost_us: int
    params: dict[str, Any]  # canonical JSON form of the input the action used
    apply: Apply  # commits the state change; receives the event seq
    rule_violations: tuple[dict[str, Any], ...] = ()


def _nothing(seq: int) -> tuple[Observation | None, bytes | RGB | None]:
    return None, None


@dataclass(frozen=True)
class ActionResult:
    seq: int
    action: str
    cost_s: float
    elapsed_s: float
    state: dict[str, Any]
    observation: Observation | None = None
    image: bytes | None = None  # JPEG of a served view, crop or thumbnail
    array: RGB | None = None  # its RGB pixels instead, when the session serves arrays
    text: str | None = None  # export_trajectory only
    rule_violations: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "action": self.action,
            "cost_s": self.cost_s,
            "elapsed_s": self.elapsed_s,
            "state": self.state,
            "observation": None if self.observation is None else self.observation.to_dict(),
            "rule_violations": list(self.rule_violations),
        }


# --- input checks --------------------------------------------------------------------------


def _short(value: Any, limit: int = 60) -> str:
    """Bounded repr for error messages (inputs are echoed, never in full)."""
    try:
        text = repr(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidParameter(f"{name} must be a finite number, got {_short(value)}.")
    try:
        number = float(value)
    except OverflowError:
        raise InvalidParameter(f"{name} is out of range.") from None
    if not math.isfinite(number):
        raise InvalidParameter(f"{name} must be a finite number, got {_short(value)}.")
    return number


def _point(value: Any, name: str) -> Point:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise InvalidParameter(f"{name} must be [x, y] in view pixels, got {_short(value)}.")
    return (_number(value[0], f"{name}[0]"), _number(value[1], f"{name}[1]"))


def _jsonable(value: Any, depth: int = 0) -> Any:
    """A JSON-safe stand-in for any input, for the record of a failed action."""
    if depth > 20:
        return "<nested>"
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2**63 else "<int out of range>"
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(k)[:64]: _jsonable(v, depth + 1) for k, v in list(value.items())[:1000]}
    if isinstance(value, list | tuple):
        return [_jsonable(v, depth + 1) for v in value[:1000]]
    return f"<{type(value).__name__}>"


def _bounded(raw: dict[str, Any]) -> dict[str, Any]:
    safe = _jsonable(raw)
    text = json.dumps(safe, ensure_ascii=True, allow_nan=False)
    if len(text) > RAW_PARAMS_MAX_BYTES:
        return {
            "truncated": True,
            "bytes": len(text),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    out: dict[str, Any] = safe
    return out


def _client_meta(client: Any) -> dict[str, Any] | None:
    """Client metadata never decides an action: invalid metadata is replaced by a note."""
    if client is None:
        return None
    if not isinstance(client, Mapping):
        return {"rejected": "client metadata must be a JSON object"}
    try:
        text = json.dumps(dict(client), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return {"rejected": "client metadata is not valid JSON"}
    if len(text) > CLIENT_META_MAX_BYTES:
        return {"rejected": f"client metadata is larger than {CLIENT_META_MAX_BYTES} bytes"}
    out: dict[str, Any] = json.loads(text)
    return out


def _utc_now() -> datetime:
    return datetime.now(UTC)


def check_actor(actor: Any) -> str:
    if not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        raise InvalidParameter(
            f"actor must look like 'human:<alias>' or 'agent:<name>', got {_short(actor)}.",
            hint="Use 1-64 characters from A-Z, a-z, 0-9, '.', '_' and '-' after the colon.",
        )
    return actor


def check_viewport(viewport_px: Any, default: tuple[int, int]) -> Size:
    if viewport_px is None:
        return default
    ok = (
        isinstance(viewport_px, list | tuple)
        and len(viewport_px) == 2
        and all(
            isinstance(v, int) and not isinstance(v, bool) and VIEWPORT_MIN <= v <= VIEWPORT_MAX
            for v in viewport_px
        )
    )
    if not ok:
        raise InvalidParameter(
            f"viewport_px must be [width, height] with integers {VIEWPORT_MIN}-{VIEWPORT_MAX}, "
            f"got {_short(viewport_px)}."
        )
    return (viewport_px[0], viewport_px[1])


# --- session -------------------------------------------------------------------------------


class Session:
    def __init__(
        self,
        *,
        session_id: str,
        slide_id: str,
        source: SlideSource,
        slide_sha256: str,
        profile: Profile,
        slide_fileset_sha256: str | None = None,
        protocol: Protocol,
        actor: str,
        images: ImageStore,
        viewport_px: Sequence[int] | None = None,
        log_path: Path | None = None,
        wall_ms: Callable[[], int] | None = None,
        now_utc: Callable[[], datetime] = _utc_now,
        view_format: str = "jpeg",
        store_images: bool = True,
    ) -> None:
        if view_format not in VIEW_FORMATS:
            raise ValueError(f"view_format must be one of {VIEW_FORMATS}")
        self.view_format = view_format
        # Served JPEGs are kept for GET by id (REST, the web UI); the training interface hands
        # each one over at once and keeps none.
        self._store_images = store_images
        self.actor = check_actor(actor)
        self.viewport_px: Size = check_viewport(viewport_px, profile.viewport_default_px)
        self.session_id = session_id
        self.slide_id = slide_id
        self.profile = profile
        self.protocol = protocol
        self._source = source
        self._slide_sha256 = slide_sha256
        self._slide_fileset_sha256 = slide_fileset_sha256
        self._images = images
        self._wall_ms = wall_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.RLock()
        self._ledger = Ledger(profile.cost_model, profile.budget, auto_view=profile.auto_view)
        self._slide_um = slide_size_um(source)
        self._native = native_mpp(source)
        self._mags = available_mags(
            profile.magnifications,
            self._native,
            mpp_ref_40x=profile.mpp_ref_40x,
            tolerance=profile.mag_tolerance,
        )
        if not self._mags:
            raise SlideMetadataError(
                f"No preset magnification is available for slide '{slide_id}' "
                f"(native resolution {self._native:.2f} um/px)."
            )
        self._mag = self._mags[0]
        self._center = clamp_center(
            (self._slide_um[0] / 2, self._slide_um[1] / 2), self._fov(self._mag), self._slide_um
        )
        self._seq = 0
        self._counters = {"v": 0, "t": 0, "m": 0, "a": 0}
        self._views: dict[str, ViewRecord] = {}
        self._thumbs: dict[str, ThumbnailRecord] = {}
        self._measurements: dict[str, MeasurementRecord] = {}
        self._annotations: dict[str, AnnotationRecord] = {}
        self._current_view: ViewRecord | None = None
        self._closed = False  # a report was submitted
        self._terminated = False  # close() was called: no further actions at all
        self._broken = False  # the log can no longer be written
        self.created_utc = now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self._writer = TrajectoryWriter(log_path)
        self._writer.write(self._header())
        params = {
            "slide_id": slide_id,
            "profile": profile.id,
            "protocol": protocol.id,
            "viewport_px": list(self.viewport_px),
            "actor": self.actor,
        }
        self.initial = self._execute(
            "create_session", params, None, lambda: self._op_initial(params)
        )

    # --- read-only views of the session ---------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def terminated(self) -> bool:
        return self._terminated

    @property
    def history(self) -> list[dict[str, Any]]:
        """All trajectory records written so far (header first)."""
        with self._lock:
            return list(self._writer.records)

    @property
    def views(self) -> Mapping[str, ViewRecord]:
        with self._lock:
            return dict(self._views)

    @property
    def measurements(self) -> Mapping[str, MeasurementRecord]:
        with self._lock:
            return dict(self._measurements)

    @property
    def annotations(self) -> Mapping[str, AnnotationRecord]:
        with self._lock:
            return dict(self._annotations)

    @property
    def available_mags(self) -> tuple[float, ...]:
        return self._mags

    def slide_info(self) -> dict[str, Any]:
        return {
            "slide_id": self.slide_id,
            "dimensions_px": list(self._source.dimensions),
            "dimensions_um": list(self._slide_um),
            "mpp": list(self._source.mpp),
            "available_mags": list(self._mags),
            "max_mag": self._mags[-1],
        }

    def task(self) -> dict[str, Any]:
        return self.protocol.task_description()

    def rules(self) -> dict[str, Any]:
        out: dict[str, Any] = self.profile.model_dump(mode="json")
        return out

    def opening(self) -> dict[str, Any]:
        """What create_session returns besides the first view: the slide geometry, the task
        (protocol) and the rules (profile). Every adapter serves this same payload."""
        return {"slide": self.slide_info(), "task": self.task(), "rules": self.rules()}

    def state_dict(self) -> dict[str, Any]:
        with self._lock:
            return self._state()

    def get_image(self, image_id: str) -> bytes:
        """JPEG of a view, crop or thumbnail already served (and logged) in this session."""
        with self._lock:
            if self._terminated:
                raise SessionClosed("This session has ended.")
            if not is_image_id(image_id) or (
                image_id not in self._views and image_id not in self._thumbs
            ):
                raise ImageNotFound(f"Image {_short(image_id, 40)} was not served in this session.")
            return self._images.get(image_id)

    def close(self) -> None:
        """End the session for good: later actions are refused and the log file is closed.

        Waits for an action in progress, so that action is fully logged first.
        """
        with self._lock:
            self._terminated = True
            self._writer.close()

    def discard_images(self) -> None:
        """Delete the served images of an ended session (its trajectory stays)."""
        with self._lock:
            if self._terminated:
                self._images.discard()

    # --- actions -------------------------------------------------------------------------------

    def get_state(self, *, client: Any = None) -> ActionResult:
        return self._execute("get_state", {}, client, lambda: _Plan(0, {}, _nothing))

    def set_magnification(self, mag: Any, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            value = _number(mag, "mag")
            preset = self._match_preset(value)
            center = clamp_center(self._center, self._fov(preset), self._slide_um)
            return self._move(
                "set_magnification", {"mag": value}, center, preset, extra_cost_viewports=None
            )

        return self._execute("set_magnification", {"mag": mag}, client, op)

    def pan(self, dx: Any, dy: Any, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            fx, fy = _number(dx, "dx"), _number(dy, "dy")
            fov = self._fov(self._mag)
            target = (self._center[0] + fx * fov[0], self._center[1] + fy * fov[1])
            center = clamp_center(target, fov, self._slide_um)
            moved = displacement_in_viewports(self._center, center, fov)
            return self._move("pan", {"dx": fx, "dy": fy}, center, self._mag, moved)

        return self._execute("pan", {"dx": dx, "dy": dy}, client, op)

    def move_to(self, x_um: Any, y_um: Any, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            target = (_number(x_um, "x_um"), _number(y_um, "y_um"))
            center = clamp_center(target, self._fov(self._mag), self._slide_um)
            params = {"x_um": target[0], "y_um": target[1]}
            return self._move("move_to", params, center, self._mag, None)

        return self._execute("move_to", {"x_um": x_um, "y_um": y_um}, client, op)

    def view(self, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            pending = self._render(self._center, self._mag)
            return _Plan(self._ledger.cost_us("view"), {}, self._apply_view(pending))

        return self._execute("view", {}, client, op)

    def get_thumbnail(self, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            rules = self.profile.thumbnail
            if not rules.allowed:
                raise ActionNotAllowed(
                    f"The thumbnail is not available under the '{self.profile.id}' profile."
                )
            size = thumbnail_size(self._slide_um, rules.long_side_px, self._native)
            whole = BBox(0.0, 0.0, self._slide_um[0], self._slide_um[1])
            arr = render_region(self._source, whole, size, fill_rgb=self.profile.image.fill_rgb)
            um_per_px = (self._slide_um[0] / size[0], self._slide_um[1] / size[1])
            served, jpeg_hash = self._served(arr)
            pixels = pixel_sha256(arr)
            pieces = tuple(piece.to_dict() for piece in self._tissue().pieces)

            def apply(seq: int) -> tuple[Observation | None, bytes | RGB | None]:
                tid = self._next_id("t")
                if isinstance(served, bytes) and self._store_images:
                    self._images.put(tid, served)
                rec = ThumbnailRecord(tid, seq, size, um_per_px, pixels, jpeg_hash, pieces)
                self._thumbs[tid] = rec
                return rec, served

            return _Plan(self._ledger.cost_us("get_thumbnail"), {}, apply)

        return self._execute("get_thumbnail", {}, client, op)

    def crop(
        self, x_um: Any, y_um: Any, w_um: Any, h_um: Any, *, client: Any = None
    ) -> ActionResult:
        def op() -> _Plan:
            if not self.profile.crop.allowed:
                raise ActionNotAllowed(
                    f"crop is not available under the '{self.profile.id}' profile.",
                    hint="Navigate with move_to, pan and set_magnification.",
                )
            x, y = _number(x_um, "x_um"), _number(y_um, "y_um")
            w, h = _number(w_um, "w_um"), _number(h_um, "h_um")
            if w <= 0 or h <= 0:
                raise InvalidParameter(f"Crop width and height must be positive, got {w} x {h} um.")
            try:
                requested = BBox(x, y, x + w, y + h)
            except InvalidParameter:
                raise InvalidParameter("Crop rectangle is out of range.") from None
            bbox, size, mpp = crop_geometry(
                requested, self._slide_um, max(self.viewport_px), self._native
            )
            arr = render_region(self._source, bbox, size, fill_rgb=self.profile.image.fill_rgb)
            mag = mag_for_mpp(mpp, self.profile.mpp_ref_40x)
            pending = (arr, bbox, mpp, mag, bbox.center)
            params = {"x_um": x, "y_um": y, "w_um": w, "h_um": h}
            return _Plan(self._ledger.cost_us("crop"), params, self._apply_view(pending, "crop"))

        params = {"x_um": x_um, "y_um": y_um, "w_um": w_um, "h_um": h_um}
        return self._execute("crop", params, client, op)

    def measure(self, p1_px: Any, p2_px: Any, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            view = self._require_view("measure")
            p1 = self._inside(_point(p1_px, "p1_px"), view)
            p2 = self._inside(_point(p2_px, "p2_px"), view)
            u1 = view_px_to_um(p1, view.bbox_um, view.mpp)
            u2 = view_px_to_um(p2, view.bbox_um, view.mpp)

            def apply(seq: int) -> tuple[Observation | None, bytes | None]:
                mid = self._next_id("m")
                rec = MeasurementRecord(
                    mid, seq, view.view_id, p1, p2, u1, u2, distance(u1, u2), distance(p1, p2)
                )
                self._measurements[mid] = rec
                return rec, None

            params = {"p1_px": list(p1), "p2_px": list(p2)}
            return _Plan(self._ledger.cost_us("measure"), params, apply)

        return self._execute("measure", {"p1_px": p1_px, "p2_px": p2_px}, client, op)

    def annotate(
        self, geometry: Any, label: Any, note: Any = "", *, client: Any = None
    ) -> ActionResult:
        def op() -> _Plan:
            view = self._require_view("annotate")
            gtype, points = self._parse_geometry(geometry, view)
            if not isinstance(label, str) or not 1 <= len(label) <= LABEL_MAX:
                raise InvalidParameter(f"label must be 1-{LABEL_MAX} characters.")
            if not isinstance(note, str) or len(note) > NOTE_MAX:
                raise InvalidParameter(f"note must be text of at most {NOTE_MAX} characters.")
            coords_um = tuple(view_px_to_um(p, view.bbox_um, view.mpp) for p in points)

            def apply(seq: int) -> tuple[Observation | None, bytes | None]:
                aid = self._next_id("a")
                rec = AnnotationRecord(
                    aid, seq, view.view_id, gtype, points, coords_um, label, note
                )
                self._annotations[aid] = rec
                return rec, None

            coords: Any = list(points[0]) if gtype == "point" else [list(p) for p in points]
            params = {
                "geometry": {"type": gtype, "coordinates": coords},
                "label": label,
                "note": note,
            }
            return _Plan(self._ledger.cost_us("annotate"), params, apply)

        raw = {"geometry": geometry, "label": label, "note": note}
        return self._execute("annotate", raw, client, op)

    def submit_report(self, report: Any, *, client: Any = None) -> ActionResult:
        def op() -> _Plan:
            validated = validate_report(report, expected_task=self.protocol.id)
            outcome = check_report(
                validated,
                protocol=self.protocol,
                views={k: ViewFact(k, v.mag, v.bbox_um) for k, v in self._views.items()},
                measurements={
                    k: MeasurementFact(k, m.length_um, m.length_px)
                    for k, m in self._measurements.items()
                },
                tissue=self._tissue(),
            )
            failures = tuple(c.to_dict() for c in outcome.failures)
            if failures and self.protocol.enforcement == "enforced":
                listed = "; ".join(f"{c['rule']}: {c['message']}" for c in failures[:5])
                raise RuleViolation(
                    f"The report was refused because it breaks {len(failures)} protocol "
                    f"rule(s). {listed}",
                    violations=list(failures),
                    hint="Fix the listed problems, for example look at more tissue, and submit "
                    "the report again.",
                )

            def apply(seq: int) -> tuple[Observation | None, bytes | None]:
                self._closed = True
                receipt = ReportReceipt(
                    seq=seq,
                    accepted=True,
                    enforcement=self.protocol.enforcement,
                    views_used=tuple(self._views),
                    rule_checks=tuple(c.to_dict() for c in outcome.checks),
                    coverage=outcome.coverage,
                )
                return receipt, None

            params = {"report": validated.model_dump(mode="json")}
            return _Plan(0, params, apply, rule_violations=failures)

        return self._execute("submit_report", {"report": report}, client, op)

    def export_trajectory(self, *, client: Any = None) -> ActionResult:
        with self._lock:
            result = self._execute("export_trajectory", {}, client, lambda: _Plan(0, {}, _nothing))
            return replace(result, text=self._writer.text())

    # --- machinery -----------------------------------------------------------------------------

    def _serves_view(self, action: str) -> bool:
        return action in VIEW_ACTIONS or (action in MOVE_ACTIONS and self.profile.auto_view)

    def _execute(
        self, action: str, raw: dict[str, Any], client: Any, op: Callable[[], _Plan]
    ) -> ActionResult:
        with self._lock:
            if self._terminated:
                raise SessionClosed(
                    "This session has ended (it expired or the server stopped it).",
                    hint="Start a new session; this session's trajectory can still be downloaded.",
                )
            if self._broken:
                raise InternalError(
                    "This session stopped because its log could not be written.",
                    hint="Start a new session.",
                )
            t_wall = self._wall_ms()
            seq = self._seq + 1
            meta = _client_meta(client)
            try:
                if self._closed and action not in AFTER_CLOSE:
                    raise SessionClosed(
                        "This session is closed because a report was submitted.",
                        hint="Start a new session. get_state and export_trajectory still work.",
                    )
                self._ledger.check(action, serves_view=self._serves_view(action))
                plan = op()
            except SlideTwinError as err:
                self._record_failure(seq, action, _bounded(raw), t_wall, err, meta)
                raise
            except Exception as err:
                internal = InternalError("The action failed unexpectedly and was not applied.")
                self._record_failure(seq, action, _bounded(raw), t_wall, internal, meta)
                raise internal from err
            snapshot = self._snapshot()
            try:
                self._ledger.charge(plan.cost_us)
                observation, served = plan.apply(seq)
                self._writer.write(
                    self._event(
                        seq,
                        action,
                        plan.params,
                        t_wall,
                        plan.cost_us,
                        observation,
                        None,
                        meta,
                        plan.rule_violations,
                    )
                )
            except Exception as err:
                self._restore(snapshot)
                internal = InternalError("The action could not be recorded and was not applied.")
                self._record_failure(seq, action, plan.params, t_wall, internal, meta)
                raise internal from err
            self._seq = seq
            return ActionResult(
                seq=seq,
                action=action,
                cost_s=to_s(plan.cost_us),
                elapsed_s=self._ledger.elapsed_s,
                state=self._state(),
                observation=observation,
                image=served if isinstance(served, bytes) else None,
                array=None if served is None or isinstance(served, bytes) else served,
                rule_violations=plan.rule_violations,
            )

    def _record_failure(
        self,
        seq: int,
        action: str,
        params: dict[str, Any],
        t_wall: int,
        err: SlideTwinError,
        meta: dict[str, Any] | None,
    ) -> None:
        """Log a failed action at zero cost and tell the caller where it sits in the log. If even
        that cannot be written, stop the session: it could no longer keep its log complete."""
        try:
            violations = tuple(getattr(err, "violations", ()))
            self._writer.write(
                self._event(seq, action, params, t_wall, 0, None, err.to_dict(), meta, violations)
            )
        except Exception:
            self._broken = True
            return
        self._seq = seq
        err.context = {"seq": seq, "cost_s": 0.0, "elapsed_s": self._ledger.elapsed_s}

    def _snapshot(self) -> tuple[Any, ...]:
        return (
            self._center,
            self._mag,
            self._current_view,
            self._closed,
            dict(self._counters),
            self._ledger.elapsed_us,
            self._ledger.views_served,
            set(self._views),
            set(self._thumbs),
            set(self._measurements),
            set(self._annotations),
        )

    def _restore(self, snapshot: tuple[Any, ...]) -> None:
        (
            self._center,
            self._mag,
            self._current_view,
            self._closed,
            self._counters,
            self._ledger.elapsed_us,
            self._ledger.views_served,
            views,
            thumbs,
            measurements,
            annotations,
        ) = snapshot
        for table, keep in (
            (self._views, views),
            (self._thumbs, thumbs),
            (self._measurements, measurements),
            (self._annotations, annotations),
        ):
            for key in [k for k in table if k not in keep]:
                del table[key]

    def _event(
        self,
        seq: int,
        action: str,
        params: dict[str, Any],
        t_wall: int,
        cost_us: int,
        observation: Observation | None,
        error: dict[str, Any] | None,
        client: dict[str, Any] | None,
        rule_violations: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        mpp = self._mpp(self._mag)
        return {
            "type": "event",
            "seq": seq,
            "action": action,
            "params": params,
            "t_virtual_s": self._ledger.elapsed_s,
            "t_wall_ms": t_wall,
            "cost_s": to_s(cost_us),
            "state_after": {
                "center_um": list(self._center),
                "mag": self._mag,
                "bbox_um": viewport_bbox(self._center, self.viewport_px, mpp).to_list(),
            },
            "view_id": observation.view_id if isinstance(observation, ViewRecord) else None,
            "observation": None if observation is None else observation.to_dict(),
            "rule_violations": list(rule_violations),
            "error": error,
            "client": client,
        }

    def _header(self) -> dict[str, Any]:
        return {
            "type": "header",
            "schema_version": SCHEMA_VERSION,
            "app": app_version(),
            "session_id": self.session_id,
            "created_utc": self.created_utc,
            "slide": {
                "id": self.slide_id,
                "sha256": self._slide_sha256,
                "mpp": list(self._source.mpp),
                "dimensions_px": list(self._source.dimensions),
                "backend": self._source.backend,
                "provenance": source_provenance(self._source),
                # a slide made of several files: the hash of every file it needs (D9, M8)
                **(
                    {"fileset_sha256": self._slide_fileset_sha256}
                    if self._slide_fileset_sha256 is not None
                    else {}
                ),
            },
            "profile": {
                "id": self.profile.id,
                "sha256": content_hash(self.profile),
                "content": self.profile.model_dump(mode="json"),
            },
            "protocol": {
                "id": self.protocol.id,
                "sha256": content_hash(self.protocol),
                "status": self.protocol.status,
                "content": self.protocol.model_dump(mode="json"),
            },
            "viewport_px": list(self.viewport_px),
            "actor": self.actor,
        }

    def _state(self) -> dict[str, Any]:
        mpp = self._mpp(self._mag)
        bbox = viewport_bbox(self._center, self.viewport_px, mpp)
        return {
            "session_id": self.session_id,
            "slide_id": self.slide_id,
            "profile": self.profile.id,
            "center_um": list(self._center),
            "mag": self._mag,
            "mpp": mpp,
            "bbox_um": bbox.to_list(),
            "viewport_px": list(self.viewport_px),
            "fov_um": list(self._fov(self._mag)),
            "elapsed_s": self._ledger.elapsed_s,
            "views_served": self._ledger.views_served,
            "current_view_id": None if self._current_view is None else self._current_view.view_id,
            "closed": self._closed,
            "budget_remaining": self._ledger.remaining(),
        }

    def _tissue(self) -> TissueMap:
        """Tissue mask and pieces of this slide on the protocol's coverage grid (shared, cached)."""
        return tissue_map(self._source, self.protocol.coverage)

    def _mpp(self, mag: float) -> float:
        return mpp_for_mag(mag, self.profile.mpp_ref_40x)

    def _fov(self, mag: float) -> tuple[float, float]:
        return fov_um(self.viewport_px, self._mpp(mag))

    def _match_preset(self, value: float) -> float:
        available = ", ".join(format_mag(m) for m in self._mags)
        for preset in self.profile.magnifications:
            if math.isclose(preset, value, rel_tol=1e-9):
                if preset not in self._mags:
                    raise MagnificationUnavailable(
                        f"{format_mag(preset)} is not available: this slide's native resolution "
                        f"is {self._native:.2f} um/px, so the highest magnification is "
                        f"{format_mag(self._mags[-1])}.",
                        hint=f"Use one of: {available}.",
                    )
                return float(preset)
        raise MagnificationNotPreset(
            f"{value:g}x is not a preset magnification.", hint=f"Use one of: {available}."
        )

    def _render(self, center: Point, mag: float) -> tuple[RGB, BBox, float, float, Point]:
        mpp = self._mpp(mag)
        bbox = viewport_bbox(center, self.viewport_px, mpp)
        arr = render_region(
            self._source, bbox, self.viewport_px, fill_rgb=self.profile.image.fill_rgb
        )
        return arr, bbox, mpp, mag, center

    def _move(
        self,
        action: str,
        params: dict[str, Any],
        center: Point,
        mag: float,
        extra_cost_viewports: float | None,
    ) -> _Plan:
        """A navigation step: render the new view if auto_view, commit the position on success."""
        cost = self._ledger.cost_us(action, viewports=extra_cost_viewports or 0.0)
        pending = self._render(center, mag) if self.profile.auto_view else None
        view_apply = self._apply_view(pending) if pending is not None else None

        def apply(seq: int) -> tuple[Observation | None, bytes | RGB | None]:
            self._center, self._mag = center, mag
            if view_apply is None:
                self._current_view = None
                return None, None
            return view_apply(seq)

        return _Plan(cost, params, apply)

    def _apply_view(
        self, pending: tuple[RGB, BBox, float, float, Point], kind: str = "view"
    ) -> Apply:
        """Encode and hash now (planning); assign the id, store and register on commit."""
        arr, bbox, mpp, mag, center = pending
        served, jpeg_hash = self._served(arr)
        pixels = pixel_sha256(arr)
        size = (int(arr.shape[1]), int(arr.shape[0]))

        def apply(seq: int) -> tuple[Observation | None, bytes | RGB | None]:
            vid = self._next_id("v")
            if isinstance(served, bytes) and self._store_images:
                self._images.put(vid, served)
            rec = ViewRecord(vid, kind, seq, center, mag, mpp, bbox, size, pixels, jpeg_hash)
            self._views[vid] = rec
            self._current_view = rec
            self._ledger.note_view()
            return rec, served

        return apply

    def _op_initial(self, params: dict[str, Any]) -> _Plan:
        pending = self._render(self._center, self._mag)
        return _Plan(self._ledger.cost_us("create_session"), params, self._apply_view(pending))

    def _served(self, arr: RGB) -> tuple[bytes | RGB, str | None]:
        """What the session serves for rendered pixels, and the JPEG's hash: the JPEG, or the
        array itself when the session serves arrays (then there is no JPEG)."""
        if self.view_format == "array":
            return arr, None
        enc = self.profile.image
        jpeg = encode_jpeg(arr, quality=enc.quality, subsampling=enc.subsampling)
        return jpeg, sha256_bytes(jpeg)

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:04d}"

    def _require_view(self, action: str) -> ViewRecord:
        if self._current_view is None:
            raise NoCurrentView(
                f"There is no current view to {action} on.",
                hint="Call view() after moving (auto_view is off in this profile).",
            )
        return self._current_view

    @staticmethod
    def _inside(p: Point, view: ViewRecord) -> Point:
        w, h = view.size_px
        if not (0 <= p[0] <= w and 0 <= p[1] <= h):
            raise OutsideView(
                f"Point ({p[0]:g}, {p[1]:g}) is outside the current view ({w}x{h} px).",
                hint=f"Use pixel coordinates of view {view.view_id}: x 0-{w}, y 0-{h}.",
            )
        return p

    def _parse_geometry(self, geometry: Any, view: ViewRecord) -> tuple[str, tuple[Point, ...]]:
        if not isinstance(geometry, Mapping) or set(geometry) != {"type", "coordinates"}:
            raise InvalidParameter(
                "geometry must be an object with exactly the keys 'type' and 'coordinates'.",
                hint='Example: {"type": "point", "coordinates": [100, 200]}.',
            )
        gtype, coords = geometry["type"], geometry["coordinates"]
        if gtype == "point":
            return "point", (self._inside(_point(coords, "coordinates"), view),)
        if gtype == "polygon":
            if not isinstance(coords, list | tuple) or not 3 <= len(coords) <= POLYGON_MAX:
                raise InvalidParameter(
                    f"A polygon needs 3-{POLYGON_MAX} [x, y] points in view pixels."
                )
            pts = tuple(
                self._inside(_point(c, f"coordinates[{i}]"), view) for i, c in enumerate(coords)
            )
            return "polygon", pts
        raise InvalidParameter(
            f"Unsupported geometry type {_short(gtype, 30)}; use 'point' or 'polygon'."
        )
