"""REST endpoints (SPEC 7).

Each route calls exactly one `SessionManager` or `Session` method. No route produces pixels
other than a session's own view, crop and thumbnail images, and there is no tile endpoint.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from slidetwin.api.payloads import respond
from slidetwin.api.schemas import (
    ActionResponse,
    AnnotateRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    CropRequest,
    EmptyRequest,
    ErrorOut,
    FeedbackRequest,
    MagnificationRequest,
    MeasureRequest,
    MoveToRequest,
    PanRequest,
    ProfileList,
    ProtocolList,
    ReportRequest,
    SlideList,
)
from slidetwin.core.manager import SessionManager
from slidetwin.core.report import export_schema

router = APIRouter(prefix="/api")


def _manager(request: Request) -> SessionManager:
    manager: SessionManager = request.app.state.manager
    return manager


Manager = Annotated[SessionManager, Depends(_manager)]
SessionId = Annotated[
    str, Path(pattern=r"^[0-9a-f]{32}$", description="Session id returned by POST /api/sessions.")
]
IncludeImage = Annotated[
    bool, Query(description="Embed the JPEG as base64; false returns only its URL.")
]


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {"model": ErrorOut} for code in (*codes, 422, 500)}


ACTION_ERRORS = _errors(400, 403, 404, 409, 413)
READ_ERRORS = _errors(404)
CREATE_ERRORS = _errors(400, 404, 413, 503)
JPEG = {200: {"content": {"image/jpeg": {}}, "description": "JPEG image"}, **READ_ERRORS}


def _client(body: Any) -> dict[str, Any] | None:
    client: dict[str, Any] | None = getattr(body, "client", None)
    return client


# --- catalogue -----------------------------------------------------------------------------------


@router.get("/slides", response_model=SlideList, tags=["catalogue"])
def list_slides(m: Manager) -> dict[str, Any]:
    """Slides that can be opened: id, license, attribution. No labels, file names or images."""
    return {"slides": m.catalogue()}


@router.get("/profiles", response_model=ProfileList, tags=["catalogue"])
def list_profiles(m: Manager) -> dict[str, Any]:
    """Rule profiles: presets, costs, budgets. Everyone sees the rules they are timed by."""
    return {"profiles": m.profiles()}


@router.get("/protocols", response_model=ProtocolList, tags=["catalogue"])
def list_protocols(m: Manager) -> dict[str, Any]:
    """Reading protocols with every rule and its review status."""
    return {"protocols": m.protocols()}


@router.get("/report_schema", tags=["catalogue"])
def report_schema() -> dict[str, Any]:
    """JSON Schema of the report accepted by POST /api/sessions/{id}/report (SPEC 5.3)."""
    return export_schema()


# --- sessions ------------------------------------------------------------------------------------


@router.post(
    "/sessions",
    status_code=201,
    response_model=CreateSessionResponse,
    responses=CREATE_ERRORS,
    tags=["sessions"],
)
def create_session(
    body: CreateSessionRequest, m: Manager, include_image: IncludeImage = True
) -> dict[str, Any]:
    """Open a slide. The session starts at the slide centre at the lowest magnification and returns
    the initial view, the slide geometry, the task (protocol) and the rule profile."""
    s = m.create(
        slide_id=body.slide_id,
        actor=body.actor,
        profile=body.profile,
        protocol=body.protocol,
        viewport_px=body.viewport_px,
    )
    out = respond(s, s.initial, include_image)
    out.update(s.opening())
    return out


@router.get(
    "/sessions/{session_id}/state",
    response_model=ActionResponse,
    responses=READ_ERRORS,
    tags=["sessions"],
)
def get_state(session_id: SessionId, m: Manager) -> dict[str, Any]:
    """Current position, magnification, field of view, elapsed virtual time and budget. No pixels;
    no cost. Reading the state does not keep an idle session alive."""
    s = m.get(session_id, touch=False)
    return respond(s, s.get_state(), False)


@router.post(
    "/sessions/{session_id}/thumbnail",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def get_thumbnail(
    session_id: SessionId,
    m: Manager,
    body: EmptyRequest | None = None,
    include_image: IncludeImage = True,
) -> dict[str, Any]:
    """The navigation thumbnail and its um per pixel, if the profile allows it."""
    s = m.get(session_id)
    return respond(s, s.get_thumbnail(client=_client(body)), include_image)


@router.post(
    "/sessions/{session_id}/magnification",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def set_magnification(
    session_id: SessionId,
    body: MagnificationRequest,
    m: Manager,
    include_image: IncludeImage = True,
) -> dict[str, Any]:
    """Switch to a preset magnification; returns the new view."""
    s = m.get(session_id)
    return respond(s, s.set_magnification(body.mag, client=body.client), include_image)


@router.post(
    "/sessions/{session_id}/pan",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def pan(
    session_id: SessionId, body: PanRequest, m: Manager, include_image: IncludeImage = True
) -> dict[str, Any]:
    """Move by (dx, dy) viewports; clamped to the slide. Returns the new view."""
    s = m.get(session_id)
    return respond(s, s.pan(body.dx, body.dy, client=body.client), include_image)


@router.post(
    "/sessions/{session_id}/move_to",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def move_to(
    session_id: SessionId, body: MoveToRequest, m: Manager, include_image: IncludeImage = True
) -> dict[str, Any]:
    """Centre the view on (x_um, y_um), like clicking the thumbnail; clamped to the slide."""
    s = m.get(session_id)
    return respond(s, s.move_to(body.x_um, body.y_um, client=body.client), include_image)


@router.post(
    "/sessions/{session_id}/view",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def view(
    session_id: SessionId,
    m: Manager,
    body: EmptyRequest | None = None,
    include_image: IncludeImage = True,
) -> dict[str, Any]:
    """Serve the current field of view again (needed only when the profile has auto_view off)."""
    s = m.get(session_id)
    return respond(s, s.view(client=_client(body)), include_image)


@router.post(
    "/sessions/{session_id}/measure",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def measure(session_id: SessionId, body: MeasureRequest, m: Manager) -> dict[str, Any]:
    """Distance between two points of the current view, in um."""
    s = m.get(session_id)
    return respond(s, s.measure(list(body.p1_px), list(body.p2_px), client=body.client), False)


@router.post(
    "/sessions/{session_id}/annotations",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def annotate(session_id: SessionId, body: AnnotateRequest, m: Manager) -> dict[str, Any]:
    """Mark a point or polygon on the current view; stored in pixels and um."""
    s = m.get(session_id)
    result = s.annotate(body.geometry, body.label, body.note, client=body.client)
    return respond(s, result, False)


@router.post(
    "/sessions/{session_id}/crop",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def crop(
    session_id: SessionId, body: CropRequest, m: Manager, include_image: IncludeImage = True
) -> dict[str, Any]:
    """Free rectangle (profile 'unconstrained' only); scaled to the viewport, never past native."""
    s = m.get(session_id)
    result = s.crop(body.x_um, body.y_um, body.w_um, body.h_um, client=body.client)
    return respond(s, result, include_image)


@router.post(
    "/sessions/{session_id}/report",
    response_model=ActionResponse,
    responses=ACTION_ERRORS,
    tags=["sessions"],
)
def submit_report(session_id: SessionId, body: ReportRequest, m: Manager) -> dict[str, Any]:
    """Submit the structured report and close the session."""
    s = m.get(session_id)
    return respond(s, s.submit_report(body.report, client=body.client), False)


@router.get(
    "/sessions/{session_id}/trajectory",
    response_class=Response,
    responses={200: {"content": {"application/x-ndjson": {}}}, **READ_ERRORS},
    tags=["sessions"],
)
def export_trajectory(session_id: SessionId, m: Manager) -> Response:
    """The session log as JSONL (header line, then one event per action). Still available after the
    session expires."""
    return Response(
        m.trajectory(session_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.jsonl"'},
    )


@router.post("/sessions/{session_id}/feedback", responses=READ_ERRORS, tags=["pilot"])
def feedback(session_id: SessionId, body: FeedbackRequest, m: Manager) -> dict[str, bool]:
    """Optional reader feedback for pilot studies, stored next to the session log (not part of
    the trajectory). Accepted during and after the session."""
    m.save_feedback(session_id, body.model_dump())
    return {"saved": True}


# --- images of this session only ----------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/views/{view_id}.jpg",
    response_class=Response,
    responses=JPEG,
    tags=["images"],
)
def view_image(
    session_id: SessionId,
    view_id: Annotated[str, Path(pattern=r"^v_\d{4,}$")],
    m: Manager,
) -> Response:
    """JPEG of a view or crop that this session has already been served (and logged)."""
    s = m.get(session_id, touch=False)
    return Response(
        s.get_image(view_id),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get(
    "/sessions/{session_id}/thumbnails/{thumb_id}.jpg",
    response_class=Response,
    responses=JPEG,
    tags=["images"],
)
def thumbnail_image(
    session_id: SessionId,
    thumb_id: Annotated[str, Path(pattern=r"^t_\d{4,}$")],
    m: Manager,
) -> Response:
    """JPEG of a thumbnail that this session has already been served (and logged)."""
    s = m.get(session_id, touch=False)
    return Response(
        s.get_image(thumb_id),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )
