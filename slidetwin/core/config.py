"""Profiles (viewer rules and costs) and reading protocols, loaded from YAML.

Pathology content lives only in `configs/protocols/*.yaml`; nothing here encodes a pathology rule.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from slidetwin.core.errors import ConfigError

Status = Literal["draft", "confirmed"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- profiles ----------------------------------------------------------------------------------


class ThumbnailRules(_Strict):
    allowed: bool = True
    long_side_px: int = Field(1024, ge=16, le=4096)


class CropRules(_Strict):
    allowed: bool = False


class ImageEncoding(_Strict):
    format: Literal["jpeg"] = "jpeg"
    quality: int = Field(90, ge=1, le=100)
    subsampling: Literal["4:4:4", "4:2:2", "4:2:0"] = "4:4:4"
    fill_rgb: tuple[int, int, int] = (255, 255, 255)

    @field_validator("fill_rgb")
    @classmethod
    def _rgb(cls, v: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(not 0 <= c <= 255 for c in v):
            raise ValueError("fill_rgb values must be 0-255")
        return v


Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ViewerCostModel(_Strict):
    """Per-action virtual time (SPEC 8). All values are placeholders until calibrated (M7)."""

    kind: Literal["viewer"] = "viewer"
    view_render_s: Seconds
    set_magnification_s: Seconds
    pan_base_s: Seconds
    pan_per_viewport_s: Seconds
    move_to_s: Seconds
    thumbnail_s: Seconds
    measure_s: Seconds
    annotate_s: Seconds


class FlatCostModel(_Strict):
    """Every action costs the same; the thumbnail has its own (default free) cost."""

    kind: Literal["flat"] = "flat"
    step_s: Seconds
    thumbnail_s: Seconds = 0.0


CostModel = Annotated[ViewerCostModel | FlatCostModel, Field(discriminator="kind")]


class Budget(_Strict):
    max_virtual_s: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    max_views: Annotated[int, Field(gt=0)] | None = None


class Profile(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = ""
    status: Status = "draft"
    auto_view: bool = True
    viewport_default_px: tuple[int, int]
    magnifications: tuple[float, ...] = Field(min_length=1)
    mpp_ref_40x: float = Field(0.25, gt=0, allow_inf_nan=False)
    mag_tolerance: float = Field(0.06, ge=0, lt=1)
    thumbnail: ThumbnailRules = ThumbnailRules()
    crop: CropRules = CropRules()
    image: ImageEncoding = ImageEncoding()
    cost_model: CostModel
    budget: Budget = Budget()

    @field_validator("magnifications")
    @classmethod
    def _mags(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(m) or m <= 0 for m in v):
            raise ValueError("magnifications must be positive numbers")
        if list(v) != sorted(set(v)):
            raise ValueError("magnifications must be strictly increasing")
        return v

    @field_validator("viewport_default_px")
    @classmethod
    def _viewport(cls, v: tuple[int, int]) -> tuple[int, int]:
        if not all(64 <= x <= 4096 for x in v):
            raise ValueError("viewport_default_px values must be 64-4096")
        return v


# --- protocols ---------------------------------------------------------------------------------

Finite = Annotated[float, Field(allow_inf_nan=False)]
PositiveFinite = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Reviewed(_Strict):
    """Review state of one rule (SPEC 5): draft until a pathologist confirms it, with name and
    date."""

    status: Status = "draft"
    reviewed_by: str | None = None
    reviewed_on: datetime.date | None = None

    @model_validator(mode="after")
    def _confirmed_needs_reviewer(self) -> Reviewed:
        if self.status == "confirmed" and (not self.reviewed_by or self.reviewed_on is None):
            raise ValueError("a confirmed rule needs reviewed_by and reviewed_on (YYYY-MM-DD)")
        return self


class TextRule(Reviewed):
    id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9-]{0,31}$")
    text: str = Field(min_length=1)


class MagnificationRules(Reviewed):
    screen_min: PositiveFinite
    confirm_min: PositiveFinite


class MeasurementRules(Reviewed):
    rule: str
    include_satellites: bool
    include_extranodal_extension: bool
    min_pixels_across: int = Field(ge=1)


class CoverageRules(Reviewed):
    negative_min_fraction: float = Field(ge=0, le=1)
    at_or_above_mag: PositiveFinite
    grid_um: PositiveFinite
    # Tissue detection on the coverage grid: saturation (0-255) above this counts as tissue, and
    # connected pieces smaller than the area below are ignored (dust, debris).
    tissue_min_saturation: int = Field(12, ge=0, le=255)
    tissue_min_area_um2: float = Field(10000.0, ge=0, allow_inf_nan=False)


class CategoryRule(Reviewed):
    """One report category with its size and cell-count limits (e.g. gt_mm, le_mm, cells_le)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    limits: dict[str, Finite] = Field(min_length=1)
    note: str | None = None


class Hints(Reviewed):
    hints: tuple[str, ...] = ()


class ReportingRules(Reviewed):
    fields: tuple[str, ...] = Field(min_length=1)


class Protocol(Reviewed):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str
    title: str
    instructions: str
    general_rules: tuple[TextRule, ...] = ()
    task_rules: tuple[TextRule, ...] = ()
    magnification: MagnificationRules
    measurement: MeasurementRules
    coverage: CoverageRules
    categories: tuple[CategoryRule, ...] = Field(min_length=1)
    reading_path_hints: Hints = Hints()
    reporting: ReportingRules
    enforcement: Literal["advisory", "enforced"] = "advisory"

    @model_validator(mode="after")
    def _confirmed_only_when_every_rule_is(self) -> Protocol:
        ids = [r.id for r in (*self.general_rules, *self.task_rules)]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        categories = [c.id for c in self.categories]
        if len(categories) != len(set(categories)):
            raise ValueError("category ids must be unique")
        if self.status == "confirmed":
            drafts = [ref for ref, rule in self.review_items() if rule.status != "confirmed"]
            if drafts:
                raise ValueError(f"protocol is confirmed but these rules are draft: {drafts}")
        return self

    def review_items(self) -> list[tuple[str, Reviewed]]:
        """Every reviewable rule with a stable reference, e.g. 'general_rules.R-G1', 'coverage'."""
        items: list[tuple[str, Reviewed]] = [
            (f"general_rules.{r.id}", r) for r in self.general_rules
        ]
        items += [(f"task_rules.{r.id}", r) for r in self.task_rules]
        items += [
            ("magnification", self.magnification),
            ("measurement", self.measurement),
            ("coverage", self.coverage),
        ]
        items += [(f"categories.{c.id}", c) for c in self.categories]
        items += [("reading_path_hints", self.reading_path_hints), ("reporting", self.reporting)]
        return items

    def task_description(self) -> dict[str, Any]:
        """What every reader, human or agent, is told at session start: the full protocol they are
        judged by, with each rule's review status (it contains no ground truth)."""
        out: dict[str, Any] = self.model_dump(mode="json")
        out["protocol_id"] = out.pop("id")
        out["protocol_version"] = out.pop("version")
        return out


# --- hashing and loading -----------------------------------------------------------------------


def canonical_json(data: Any) -> str:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def content_hash(model: BaseModel) -> str:
    """sha256 of the normalised config (defaults included), so equal rules hash equally."""
    return hashlib.sha256(canonical_json(model.model_dump(mode="json")).encode()).hexdigest()


def _describe(err: ValidationError) -> str:
    parts = []
    for e in err.errors()[:5]:
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


def load_profile_data(data: Any, origin: str = "profile") -> Profile:
    try:
        return Profile.model_validate(data)
    except ValidationError as err:
        raise ConfigError(f"Invalid profile in {origin}: {_describe(err)}") from None


def load_protocol_data(data: Any, origin: str = "protocol") -> Protocol:
    try:
        return Protocol.model_validate(data)
    except ValidationError as err:
        raise ConfigError(f"Invalid protocol in {origin}: {_describe(err)}") from None


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ConfigError(f"Cannot read {path.name}: {err}") from None


_PACKAGE = Path(__file__).resolve().parents[1]  # the slidetwin package


def default_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where profiles, protocols and slide manifests are read: SLIDETWIN_CONFIG_DIR; else a
    configs/ folder in the working directory (the repository and Docker layout); else the configs
    installed with SlideTwin (a wheel carries them inside the package, a source tree beside it)."""
    env = os.environ if environ is None else environ
    if env.get("SLIDETWIN_CONFIG_DIR"):
        return Path(env["SLIDETWIN_CONFIG_DIR"])
    for candidate in (Path("configs"), _PACKAGE / "configs", _PACKAGE.parent / "configs"):
        if (candidate / "profiles").is_dir():
            return candidate
    return Path("configs")  # none found: loading reports the folder it looked in


class ConfigStore:
    """All profiles and protocols under a config directory, validated at load time."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.profiles: dict[str, Profile] = {}
        self.protocols: dict[str, Protocol] = {}
        for path in sorted((config_dir / "profiles").glob("*.yaml")):
            prof = load_profile_data(_read_yaml(path), path.name)
            self._register(self.profiles, prof.id, prof, path)
        for path in sorted((config_dir / "protocols").glob("*.yaml")):
            proto = load_protocol_data(_read_yaml(path), path.name)
            self._register(self.protocols, proto.id, proto, path)
        if not self.profiles:
            raise ConfigError(f"No profiles found in {config_dir / 'profiles'}.")
        if not self.protocols:
            raise ConfigError(f"No protocols found in {config_dir / 'protocols'}.")

    @staticmethod
    def _register(table: dict[str, Any], key: str, value: Any, path: Path) -> None:
        if path.stem != key:
            raise ConfigError(
                f"{path.name} declares id '{key}'; the file must be named {key}.yaml."
            )
        table[key] = value

    def profile(self, profile_id: str) -> Profile:
        try:
            return self.profiles[profile_id]
        except KeyError:
            raise ConfigError(
                f"Unknown profile '{profile_id}'. Available: {', '.join(sorted(self.profiles))}."
            ) from None

    def protocol(self, protocol_id: str) -> Protocol:
        try:
            return self.protocols[protocol_id]
        except KeyError:
            raise ConfigError(
                f"Unknown protocol '{protocol_id}'. Available: {', '.join(sorted(self.protocols))}."
            ) from None
