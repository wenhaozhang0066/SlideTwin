"""Request and response models of the REST API (SPEC 7).

Each action maps 1:1 to a `Session` method.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Finite = Annotated[float, Field(allow_inf_nan=False)]
PointPx = tuple[Finite, Finite]
ClientMeta = Annotated[
    dict[str, Any] | None,
    Field(
        description="Optional client metadata, e.g. {'think_ms': 1200}. Stored in the trajectory, "
        "never on the virtual clock (max 4 KB)."
    ),
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client: ClientMeta = None


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slide_id: str = Field(description="A slide id from GET /api/slides.")
    actor: str = Field(description="'human:<alias>' or 'agent:<name>' (pseudonyms only).")
    profile: str = Field("viewer", description="Rule profile, e.g. 'viewer' or 'unconstrained'.")
    protocol: str = Field("sln_metastasis", description="Reading protocol id.")
    viewport_px: tuple[int, int] | None = Field(
        None, description="Viewport [width, height] in px; the profile default if omitted."
    )


class EmptyRequest(_Request):
    pass


class MagnificationRequest(_Request):
    mag: Finite = Field(description="A preset magnification, e.g. 10.")


class PanRequest(_Request):
    dx: Finite = Field(description="Move in viewport widths (0.5 = half a viewport).")
    dy: Finite = Field(description="Move in viewport heights.")


class MoveToRequest(_Request):
    x_um: Finite = Field(description="New centre x in um from the level-0 top-left corner.")
    y_um: Finite = Field(description="New centre y in um.")


class MeasureRequest(_Request):
    p1_px: PointPx = Field(description="[x, y] in current-view pixels.")
    p2_px: PointPx = Field(description="[x, y] in current-view pixels.")


class AnnotateRequest(_Request):
    geometry: dict[str, Any] = Field(
        description='{"type": "point", "coordinates": [x, y]} or '
        '{"type": "polygon", "coordinates": [[x, y], ...]} in current-view pixels.'
    )
    label: str
    note: str = ""


class CropRequest(_Request):
    x_um: Finite
    y_um: Finite
    w_um: Finite
    h_um: Finite


class ReportRequest(_Request):
    report: dict[str, Any] = Field(description="See slidetwin/core/report_schema.json.")


# --- responses -------------------------------------------------------------------------------


class ImageRef(BaseModel):
    url: str = Field(description="GET this URL for the same JPEG (only images served here).")
    mime: Literal["image/jpeg"] = "image/jpeg"
    base64: str | None = Field(None, description="The JPEG itself unless include_image=false.")


class ViewOut(BaseModel):
    type: Literal["view", "crop"]
    view_id: str
    seq: int
    center_um: list[float]
    mag: float
    mpp: float
    bbox_um: list[float]
    size_px: list[int]
    pixel_sha256: str
    jpeg_sha256: str
    image: ImageRef


class ThumbnailOut(BaseModel):
    type: Literal["thumbnail"]
    thumb_id: str
    seq: int
    size_px: list[int]
    um_per_px: list[float]
    pixel_sha256: str
    jpeg_sha256: str
    image: ImageRef


class MeasurementOut(BaseModel):
    type: Literal["measurement"]
    measurement_id: str
    seq: int
    view_id: str
    p1_px: list[float]
    p2_px: list[float]
    p1_um: list[float]
    p2_um: list[float]
    length_um: float
    length_px: float


class AnnotationOut(BaseModel):
    type: Literal["annotation"]
    annotation_id: str
    seq: int
    view_id: str
    geometry_type: str
    coords_px: list[list[float]]
    coords_um: list[list[float]]
    label: str
    note: str


class ReportReceiptOut(BaseModel):
    type: Literal["report_receipt"]
    seq: int
    accepted: bool
    enforcement: str
    views_used: list[str]
    rule_checks: list[dict[str, Any]]
    coverage: dict[str, Any] | None
    note: str


Observation = Annotated[
    ViewOut | ThumbnailOut | MeasurementOut | AnnotationOut | ReportReceiptOut,
    Field(discriminator="type"),
]


class BudgetOut(BaseModel):
    virtual_s: float | None
    views: int | None


class StateOut(BaseModel):
    session_id: str
    slide_id: str
    profile: str
    center_um: list[float]
    mag: float
    mpp: float
    bbox_um: list[float]
    viewport_px: list[int]
    fov_um: list[float]
    elapsed_s: float
    views_served: int
    current_view_id: str | None
    closed: bool
    budget_remaining: BudgetOut


class ActionResponse(BaseModel):
    session_id: str
    seq: int
    action: str
    cost_s: float = Field(description="Virtual time of this action, seconds.")
    elapsed_s: float = Field(description="Virtual time used so far, seconds.")
    state: StateOut
    observation: Observation | None = None
    rule_violations: list[dict[str, Any]] = Field(default_factory=list)


class SlideInfoOut(BaseModel):
    slide_id: str
    dimensions_px: list[int]
    dimensions_um: list[float]
    mpp: list[float]
    available_mags: list[float]
    max_mag: float


class CreateSessionResponse(ActionResponse):
    slide: SlideInfoOut
    task: dict[str, Any] = Field(description="The reading protocol the session is judged by.")
    rules: dict[str, Any] = Field(description="The rule profile (costs, presets, budget).")


class SlideListItem(BaseModel):
    id: str
    license: str | None
    attribution: str | None
    source_url: str | None


class SlideList(BaseModel):
    slides: list[SlideListItem]


class ProfileList(BaseModel):
    profiles: list[dict[str, Any]]


class ProtocolList(BaseModel):
    protocols: list[dict[str, Any]]


class ErrorBody(BaseModel):
    code: str
    message: str
    hint: str | None = None
    details: list[dict[str, Any]] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody
