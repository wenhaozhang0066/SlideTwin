"""Request and response models of the REST API (SPEC 7).

Each action maps 1:1 to a `Session` method. Number fields are strict: JSON integers count as
numbers; booleans and numeric strings do not, exactly as in the core.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, WithJsonSchema

from slidetwin.core.report import inline_schema

Num = Annotated[float, Field(strict=True, allow_inf_nan=False)]
PointPx = Annotated[list[Num], Field(min_length=2, max_length=2)]
ClientMeta = Annotated[
    dict[str, Any] | None,
    Field(
        description="Optional client metadata, e.g. {'think_ms': 1200}. Stored in the trajectory, "
        "never on the virtual clock, and never the reason an action fails (max 4 KB; larger or "
        "invalid metadata is replaced by a note)."
    ),
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client: ClientMeta = None


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slide_id: Annotated[str, Field(strict=True, description="A slide id from GET /api/slides.")]
    actor: Annotated[
        str, Field(strict=True, description="'human:<alias>' or 'agent:<name>' (pseudonyms only).")
    ]
    profile: str = Field("viewer", strict=True, description="Rule profile: viewer, unconstrained.")
    protocol: str = Field("sln_metastasis", strict=True, description="Reading protocol id.")
    viewport_px: Annotated[list[StrictInt], Field(min_length=2, max_length=2)] | None = Field(
        None, description="Viewport [width, height] in px; the profile default if omitted."
    )


class EmptyRequest(_Request):
    pass


class MagnificationRequest(_Request):
    mag: Num = Field(description="A preset magnification, e.g. 10.")


class PanRequest(_Request):
    dx: Num = Field(description="Move in viewport widths (0.5 = half a viewport).")
    dy: Num = Field(description="Move in viewport heights.")


class MoveToRequest(_Request):
    x_um: Num = Field(description="New centre x in um from the level-0 top-left corner.")
    y_um: Num = Field(description="New centre y in um.")


class MeasureRequest(_Request):
    p1_px: PointPx = Field(description="[x, y] in current-view pixels.")
    p2_px: PointPx = Field(description="[x, y] in current-view pixels.")


class AnnotateRequest(_Request):
    geometry: dict[str, Any] = Field(
        description='{"type": "point", "coordinates": [x, y]} or '
        '{"type": "polygon", "coordinates": [[x, y], ...]} in current-view pixels.'
    )
    label: str = Field(strict=True)
    note: str = Field("", strict=True)


class CropRequest(_Request):
    x_um: Num
    y_um: Num
    w_um: Num
    h_um: Num


class ReportRequest(_Request):
    # Documented with the full report schema; validated (and logged when invalid) by the core.
    report: Annotated[dict[str, Any], WithJsonSchema(inline_schema())] = Field(
        description="The structured report (SPEC 5.3). Also served at GET /api/report_schema."
    )


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


class TissuePiece(BaseModel):
    piece: int = Field(description="1 is the largest piece; cite it as tissue_piece:N.")
    bbox_um: list[float]
    area_mm2: float


class ThumbnailOut(BaseModel):
    type: Literal["thumbnail"]
    thumb_id: str
    seq: int
    size_px: list[int]
    um_per_px: list[float]
    pixel_sha256: str
    jpeg_sha256: str
    tissue_pieces: list[TissuePiece]
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


class RuleCheckOut(BaseModel):
    rule: str = Field(description="E1-E4 (SPEC 5.4).")
    status: Literal["pass", "fail", "not_applicable", "not_verifiable"]
    message: str
    claim_id: str | None = None


class ReportReceiptOut(BaseModel):
    type: Literal["report_receipt"]
    seq: int
    accepted: bool
    enforcement: str
    views_used: list[str]
    rule_checks: list[RuleCheckOut]
    violations: int = Field(description="Number of failed checks (advisory mode).")
    coverage: dict[str, Any] = Field(
        description="Tissue seen at or above the protocol magnification, whole slide and per piece."
    )


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
    seq: int | None = Field(None, description="Log position of a failed session action.")
    cost_s: float | None = Field(None, description="Always 0 for a failed action.")
    elapsed_s: float | None = Field(None, description="Virtual time used so far.")
    violations: list[dict[str, Any]] | None = Field(
        None, description="Failed rule checks of a report refused under an enforced protocol."
    )
    details: list[dict[str, Any]] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody
