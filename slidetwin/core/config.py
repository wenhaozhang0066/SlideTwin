"""Profiles (viewer rules and costs) and reading protocols, loaded from YAML.

Pathology content lives only in `configs/protocols/*.yaml`; nothing here encodes a pathology rule.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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


class GeneralRule(_Strict):
    id: str
    status: Status = "draft"
    text: str


class MagnificationRules(_Strict):
    screen_min: float = Field(gt=0)
    confirm_min: float = Field(gt=0)


class MeasurementRules(_Strict):
    rule: str
    include_satellites: bool
    include_extranodal_extension: bool
    min_pixels_across: int = Field(ge=1)


class CoverageRules(_Strict):
    negative_min_fraction: float = Field(ge=0, le=1)
    at_or_above_mag: float = Field(gt=0)
    grid_um: float = Field(gt=0)


class Protocol(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str
    status: Status
    reviewed_by: str | None = None
    reviewed_on: str | None = None
    title: str
    instructions: str
    general_rules: tuple[GeneralRule, ...] = ()
    magnification: MagnificationRules
    measurement: MeasurementRules
    coverage: CoverageRules
    categories: dict[str, dict[str, float]]
    category_notes: tuple[str, ...] = ()
    reading_path_hints: tuple[str, ...] = ()
    report_fields: tuple[str, ...]
    enforcement: Literal["advisory", "enforced"] = "advisory"

    def task_description(self) -> dict[str, Any]:
        """What every reader, human or agent, is told at session start: the full protocol they are
        judged by (it contains no ground truth)."""
        rules = self.model_dump(mode="json")
        return {
            "protocol_id": self.id,
            "protocol_version": self.version,
            "status": self.status,
            "title": self.title,
            "instructions": self.instructions,
            "general_rules": rules["general_rules"],
            "magnification": rules["magnification"],
            "measurement": rules["measurement"],
            "coverage": rules["coverage"],
            "categories": rules["categories"],
            "category_notes": rules["category_notes"],
            "reading_path_hints": rules["reading_path_hints"],
            "report_fields": rules["report_fields"],
            "enforcement": self.enforcement,
        }


# --- hashing and loading -----------------------------------------------------------------------


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
