"""Reading session: the state machine behind every adapter (SPEC 4, 7, 8, 9).

Every action runs through `_execute`, which:
1. rejects actions on a closed session and actions over budget,
2. validates and computes the result without touching state,
3. charges the ledger, then applies the state change and assigns ids,
4. appends one event to the trajectory (failed actions are logged with their error and zero cost).
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slidetwin.core.config import Profile, Protocol, content_hash
from slidetwin.core.errors import (
    ActionNotAllowed,
    ImageNotFound,
    InvalidParameter,
    MagnificationNotPreset,
    MagnificationUnavailable,
    NoCurrentView,
    OutsideView,
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
from slidetwin.core.images import ImageStore, encode_jpeg, pixel_sha256, sha256_bytes
from slidetwin.core.ledger import Ledger, to_s
from slidetwin.core.render import render_region
from slidetwin.core.report import validate_report
from slidetwin.core.slide_source import RGB, SlideSource, native_mpp, slide_size_um
from slidetwin.core.trajectory import SCHEMA_VERSION, TrajectoryWriter
from slidetwin.version import app_version

ACTOR_RE = re.compile(r"^(human|agent):[A-Za-z0-9._-]{1,64}$")
VIEWPORT_MIN, VIEWPORT_MAX = 64, 4096
CLIENT_META_MAX_BYTES = 4096
AFTER_CLOSE = frozenset({"get_state", "export_trajectory"})
LABEL_MAX, NOTE_MAX, POLYGON_MAX = 64, 1000, 1000


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
    jpeg_sha256: str

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
    jpeg_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "thumbnail",
            "thumb_id": self.thumb_id,
            "seq": self.seq,
            "size_px": list(self.size_px),
            "um_per_px": list(self.um_per_px),
            "pixel_sha256": self.pixel_sha256,
            "jpeg_sha256": self.jpeg_sha256,
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
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "report_receipt",
            "seq": self.seq,
            "accepted": self.accepted,
            "enforcement": self.enforcement,
            "views_used": list(self.views_used),
            "rule_checks": [],
            "coverage": None,
            "note": self.note,
        }


Observation = ViewRecord | ThumbnailRecord | MeasurementRecord | AnnotationRecord | ReportReceipt
Finalize = Callable[[int], tuple[Observation | None, bytes | None]]


@dataclass(frozen=True)
class ActionResult:
    seq: int
    action: str
    cost_s: float
    elapsed_s: float
    state: dict[str, Any]
    observation: Observation | None = None
    image: bytes | None = None  # JPEG of a served view, crop or thumbnail
    text: str | None = None  # export_trajectory only

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "action": self.action,
            "cost_s": self.cost_s,
            "elapsed_s": self.elapsed_s,
            "state": self.state,
            "observation": None if self.observation is None else self.observation.to_dict(),
            "rule_violations": [],
        }


# --- input checks --------------------------------------------------------------------------


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidParameter(f"{name} must be a finite number, got {value!r}.")
    return float(value)


def _point(value: Any, name: str) -> Point:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        raise InvalidParameter(f"{name} must be [x, y] in view pixels, got {value!r}.")
    return (_number(value[0], f"{name}[0]"), _number(value[1], f"{name}[1]"))


def _client_meta(client: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if client is None:
        return None
    try:
        encoded = json.dumps(dict(client), allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidParameter("client metadata must be a JSON object.") from None
    if len(encoded.encode()) > CLIENT_META_MAX_BYTES:
        raise InvalidParameter(f"client metadata is larger than {CLIENT_META_MAX_BYTES} bytes.")
    out: dict[str, Any] = json.loads(encoded)
    return out


def _loggable(params: dict[str, Any]) -> dict[str, Any]:
    try:
        out: dict[str, Any] = json.loads(json.dumps(params, allow_nan=False))
    except (TypeError, ValueError):
        return {"unserializable": repr(params)[:500]}
    return out


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        protocol: Protocol,
        actor: str,
        images: ImageStore,
        viewport_px: Sequence[int] | None = None,
        log_path: Path | None = None,
        wall_ms: Callable[[], int] | None = None,
        now_utc: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(actor, str) or not ACTOR_RE.match(actor):
            raise InvalidParameter(
                f"actor must look like 'human:<alias>' or 'agent:<name>', got {actor!r}.",
                hint="Use 1-64 characters from A-Z, a-z, 0-9, '.', '_' and '-' after the colon.",
            )
        viewport = self._check_viewport(viewport_px, profile.viewport_default_px)
        self.session_id = session_id
        self.slide_id = slide_id
        self.actor = actor
        self.profile = profile
        self.protocol = protocol
        self.viewport_px: Size = viewport
        self._source = source
        self._slide_sha256 = slide_sha256
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
        self._closed = False
        self.created_utc = now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self._writer = TrajectoryWriter(log_path)
        self._writer.write(self._header())
        self.initial = self._execute(
            "create_session",
            {
                "slide_id": slide_id,
                "profile": profile.id,
                "protocol": protocol.id,
                "viewport_px": list(viewport),
                "actor": actor,
            },
            None,
            self._op_initial,
        )

    # --- read-only views of the session ---------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def history(self) -> list[dict[str, Any]]:
        """All trajectory records written so far (header first)."""
        return self._writer.records

    @property
    def views(self) -> Mapping[str, ViewRecord]:
        return dict(self._views)

    @property
    def measurements(self) -> Mapping[str, MeasurementRecord]:
        return dict(self._measurements)

    @property
    def annotations(self) -> Mapping[str, AnnotationRecord]:
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

    def state_dict(self) -> dict[str, Any]:
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

    def get_image(self, image_id: str) -> bytes:
        """JPEG of a view, crop or thumbnail already served (and logged) in this session."""
        if image_id not in self._views and image_id not in self._thumbs:
            raise ImageNotFound(f"Image '{image_id}' was not served in this session.")
        return self._images.get(image_id)

    def close(self) -> None:
        self._writer.close()

    # --- actions -------------------------------------------------------------------------------

    def get_state(self, *, client: Mapping[str, Any] | None = None) -> ActionResult:
        return self._execute("get_state", {}, client, lambda: (0, lambda seq: (None, None)))

    def set_magnification(
        self, mag: Any, *, client: Mapping[str, Any] | None = None
    ) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            m = self._match_preset(mag)
            center = clamp_center(self._center, self._fov(m), self._slide_um)
            return self._move(center, m, self._ledger.cost_us("set_magnification"))

        return self._execute("set_magnification", {"mag": mag}, client, op)

    def pan(self, dx: Any, dy: Any, *, client: Mapping[str, Any] | None = None) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            fx, fy = _number(dx, "dx"), _number(dy, "dy")
            fov = self._fov(self._mag)
            target = (self._center[0] + fx * fov[0], self._center[1] + fy * fov[1])
            center = clamp_center(target, fov, self._slide_um)
            moved = displacement_in_viewports(self._center, center, fov)
            return self._move(center, self._mag, self._ledger.cost_us("pan", viewports=moved))

        return self._execute("pan", {"dx": dx, "dy": dy}, client, op)

    def move_to(
        self, x_um: Any, y_um: Any, *, client: Mapping[str, Any] | None = None
    ) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            target = (_number(x_um, "x_um"), _number(y_um, "y_um"))
            center = clamp_center(target, self._fov(self._mag), self._slide_um)
            return self._move(center, self._mag, self._ledger.cost_us("move_to"))

        return self._execute("move_to", {"x_um": x_um, "y_um": y_um}, client, op)

    def view(self, *, client: Mapping[str, Any] | None = None) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            pending = self._render(self._center, self._mag)
            return self._ledger.cost_us("view"), self._finalize_view(pending)

        return self._execute("view", {}, client, op)

    def get_thumbnail(self, *, client: Mapping[str, Any] | None = None) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            rules = self.profile.thumbnail
            if not rules.allowed:
                raise ActionNotAllowed(
                    f"The thumbnail is not available under the '{self.profile.id}' profile."
                )
            size = thumbnail_size(self._slide_um, rules.long_side_px)
            whole = BBox(0.0, 0.0, self._slide_um[0], self._slide_um[1])
            arr = render_region(self._source, whole, size, fill_rgb=self.profile.image.fill_rgb)
            um_per_px = (self._slide_um[0] / size[0], self._slide_um[1] / size[1])

            def fin(seq: int) -> tuple[Observation | None, bytes | None]:
                tid = self._next_id("t")
                jpeg = self._encode(arr)
                rec = ThumbnailRecord(
                    tid, seq, size, um_per_px, pixel_sha256(arr), sha256_bytes(jpeg)
                )
                self._images.put(tid, jpeg)
                self._thumbs[tid] = rec
                return rec, jpeg

            return self._ledger.cost_us("get_thumbnail"), fin

        return self._execute("get_thumbnail", {}, client, op)

    def crop(
        self,
        x_um: Any,
        y_um: Any,
        w_um: Any,
        h_um: Any,
        *,
        client: Mapping[str, Any] | None = None,
    ) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            if not self.profile.crop.allowed:
                raise ActionNotAllowed(
                    f"crop is not available under the '{self.profile.id}' profile.",
                    hint="Navigate with move_to, pan and set_magnification.",
                )
            x, y = _number(x_um, "x_um"), _number(y_um, "y_um")
            w, h = _number(w_um, "w_um"), _number(h_um, "h_um")
            if w <= 0 or h <= 0:
                raise InvalidParameter(f"Crop width and height must be positive, got {w} x {h} um.")
            bbox, size, mpp = crop_geometry(
                BBox(x, y, x + w, y + h), self._slide_um, max(self.viewport_px), self._native
            )
            arr = render_region(self._source, bbox, size, fill_rgb=self.profile.image.fill_rgb)
            mag = mag_for_mpp(mpp, self.profile.mpp_ref_40x)
            pending = (arr, bbox, mpp, mag, bbox.center)
            return self._ledger.cost_us("crop"), self._finalize_view(pending, kind="crop")

        params = {"x_um": x_um, "y_um": y_um, "w_um": w_um, "h_um": h_um}
        return self._execute("crop", params, client, op)

    def measure(
        self, p1_px: Any, p2_px: Any, *, client: Mapping[str, Any] | None = None
    ) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            view = self._require_view("measure")
            p1 = self._inside(_point(p1_px, "p1_px"), view)
            p2 = self._inside(_point(p2_px, "p2_px"), view)
            u1 = view_px_to_um(p1, view.bbox_um, view.mpp)
            u2 = view_px_to_um(p2, view.bbox_um, view.mpp)

            def fin(seq: int) -> tuple[Observation | None, bytes | None]:
                mid = self._next_id("m")
                rec = MeasurementRecord(
                    mid, seq, view.view_id, p1, p2, u1, u2, distance(u1, u2), distance(p1, p2)
                )
                self._measurements[mid] = rec
                return rec, None

            return self._ledger.cost_us("measure"), fin

        return self._execute("measure", {"p1_px": p1_px, "p2_px": p2_px}, client, op)

    def annotate(
        self,
        geometry: Any,
        label: Any,
        note: Any = "",
        *,
        client: Mapping[str, Any] | None = None,
    ) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            view = self._require_view("annotate")
            gtype, points = self._parse_geometry(geometry, view)
            if not isinstance(label, str) or not 1 <= len(label) <= LABEL_MAX:
                raise InvalidParameter(f"label must be 1-{LABEL_MAX} characters.")
            if not isinstance(note, str) or len(note) > NOTE_MAX:
                raise InvalidParameter(f"note must be text of at most {NOTE_MAX} characters.")
            coords_um = tuple(view_px_to_um(p, view.bbox_um, view.mpp) for p in points)

            def fin(seq: int) -> tuple[Observation | None, bytes | None]:
                aid = self._next_id("a")
                rec = AnnotationRecord(
                    aid, seq, view.view_id, gtype, points, coords_um, label, note
                )
                self._annotations[aid] = rec
                return rec, None

            return self._ledger.cost_us("annotate"), fin

        params = {"geometry": geometry, "label": label, "note": note}
        return self._execute("annotate", params, client, op)

    def submit_report(
        self, report: Any, *, client: Mapping[str, Any] | None = None
    ) -> ActionResult:
        def op() -> tuple[int, Finalize]:
            validate_report(report, expected_task=self.protocol.id)

            def fin(seq: int) -> tuple[Observation | None, bytes | None]:
                self._closed = True
                receipt = ReportReceipt(
                    seq=seq,
                    accepted=True,
                    enforcement=self.protocol.enforcement,
                    views_used=tuple(self._views),
                    note="Rule checks E1-E4 and the coverage summary are added in M5.",
                )
                return receipt, None

            return 0, fin

        return self._execute("submit_report", {"report": report}, client, op)

    def export_trajectory(self, *, client: Mapping[str, Any] | None = None) -> ActionResult:
        result = self._execute(
            "export_trajectory", {}, client, lambda: (0, lambda seq: (None, None))
        )
        return ActionResult(
            seq=result.seq,
            action=result.action,
            cost_s=result.cost_s,
            elapsed_s=result.elapsed_s,
            state=result.state,
            text=self._writer.text(),
        )

    # --- machinery -----------------------------------------------------------------------------

    def _execute(
        self,
        action: str,
        params: dict[str, Any],
        client: Mapping[str, Any] | None,
        op: Callable[[], tuple[int, Finalize]],
    ) -> ActionResult:
        with self._lock:
            t_wall = self._wall_ms()
            seq = self._seq + 1
            client_meta: dict[str, Any] | None = None
            try:
                client_meta = _client_meta(client)
                if self._closed and action not in AFTER_CLOSE:
                    raise SessionClosed(
                        "This session is closed because a report was submitted.",
                        hint="Start a new session. get_state and export_trajectory still work.",
                    )
                self._ledger.check(action)
                cost_us, finalize = op()
            except SlideTwinError as err:
                self._seq = seq
                self._log(seq, action, params, t_wall, 0, None, err.to_dict(), client_meta)
                raise
            self._seq = seq
            self._ledger.charge(cost_us)
            observation, image = finalize(seq)
            self._log(seq, action, params, t_wall, cost_us, observation, None, client_meta)
            return ActionResult(
                seq=seq,
                action=action,
                cost_s=to_s(cost_us),
                elapsed_s=self._ledger.elapsed_s,
                state=self.state_dict(),
                observation=observation,
                image=image,
            )

    def _log(
        self,
        seq: int,
        action: str,
        params: dict[str, Any],
        t_wall: int,
        cost_us: int,
        observation: Observation | None,
        error: dict[str, str] | None,
        client: dict[str, Any] | None,
    ) -> None:
        mpp = self._mpp(self._mag)
        self._writer.write(
            {
                "type": "event",
                "seq": seq,
                "action": action,
                "params": _loggable(params),
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
                "rule_violations": [],
                "error": error,
                "client": client,
            }
        )

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

    @staticmethod
    def _check_viewport(viewport_px: Sequence[int] | None, default: tuple[int, int]) -> Size:
        if viewport_px is None:
            return default
        vp = tuple(viewport_px)
        if len(vp) != 2 or not all(
            isinstance(v, int) and not isinstance(v, bool) and VIEWPORT_MIN <= v <= VIEWPORT_MAX
            for v in vp
        ):
            raise InvalidParameter(
                f"viewport_px must be [width, height] with integers {VIEWPORT_MIN}-{VIEWPORT_MAX}, "
                f"got {viewport_px!r}."
            )
        return (vp[0], vp[1])

    def _mpp(self, mag: float) -> float:
        return mpp_for_mag(mag, self.profile.mpp_ref_40x)

    def _fov(self, mag: float) -> tuple[float, float]:
        return fov_um(self.viewport_px, self._mpp(mag))

    def _match_preset(self, mag: Any) -> float:
        value = _number(mag, "mag")
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

    def _move(self, center: Point, mag: float, cost_us: int) -> tuple[int, Finalize]:
        """A navigation step: render the new view if auto_view, then commit position on success."""
        pending = self._render(center, mag) if self.profile.auto_view else None

        def fin(seq: int) -> tuple[Observation | None, bytes | None]:
            self._center, self._mag = center, mag
            if pending is None:
                self._current_view = None
                return None, None
            return self._finalize_view(pending)(seq)

        return cost_us, fin

    def _finalize_view(
        self, pending: tuple[RGB, BBox, float, float, Point], kind: str = "view"
    ) -> Finalize:
        arr, bbox, mpp, mag, center = pending

        def fin(seq: int) -> tuple[Observation | None, bytes | None]:
            vid = self._next_id("v")
            jpeg = self._encode(arr)
            rec = ViewRecord(
                view_id=vid,
                kind=kind,
                seq=seq,
                center_um=center,
                mag=mag,
                mpp=mpp,
                bbox_um=bbox,
                size_px=(int(arr.shape[1]), int(arr.shape[0])),
                pixel_sha256=pixel_sha256(arr),
                jpeg_sha256=sha256_bytes(jpeg),
            )
            self._images.put(vid, jpeg)
            self._views[vid] = rec
            self._current_view = rec
            self._ledger.note_view()
            return rec, jpeg

        return fin

    def _op_initial(self) -> tuple[int, Finalize]:
        pending = self._render(self._center, self._mag)
        return self._ledger.cost_us("create_session"), self._finalize_view(pending)

    def _encode(self, arr: RGB) -> bytes:
        enc = self.profile.image
        return encode_jpeg(arr, quality=enc.quality, subsampling=enc.subsampling)

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
        if not isinstance(geometry, Mapping):
            raise InvalidParameter(
                "geometry must be an object with 'type' and 'coordinates'.",
                hint='Example: {"type": "point", "coordinates": [100, 200]}.',
            )
        gtype, coords = geometry.get("type"), geometry.get("coordinates")
        if gtype == "point":
            return "point", (self._inside(_point(coords, "coordinates"), view),)
        if gtype == "polygon":
            if (
                not isinstance(coords, Sequence)
                or isinstance(coords, str)
                or not 3 <= len(coords) <= POLYGON_MAX
            ):
                raise InvalidParameter(
                    f"A polygon needs 3-{POLYGON_MAX} [x, y] points in view pixels."
                )
            pts = tuple(
                self._inside(_point(c, f"coordinates[{i}]"), view) for i, c in enumerate(coords)
            )
            return "polygon", pts
        raise InvalidParameter(f"Unsupported geometry type {gtype!r}; use 'point' or 'polygon'.")
