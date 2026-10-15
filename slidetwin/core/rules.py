"""Rule checks E1-E4, run when a report is submitted (SPEC 5.4).

Every threshold comes from the protocol YAML (status draft until a pathologist confirms it); this
module only interprets them. Statuses: pass, fail, not_applicable (nothing to check) and
not_verifiable (the report relies on something the environment cannot measure, e.g. cell counts).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from slidetwin.core.config import Protocol
from slidetwin.core.coverage import TissueMap, coverage, seen_cells
from slidetwin.core.geometry import BBox
from slidetwin.core.report import Report

Status = Literal["pass", "fail", "not_applicable", "not_verifiable"]
_REGION = re.compile(r"region:(-?[0-9.]+),(-?[0-9.]+),(-?[0-9.]+),(-?[0-9.]+)")
_PIECE = re.compile(r"tissue_piece:([0-9]{1,6})")
_EPS = 1e-9


@dataclass(frozen=True)
class ViewFact:
    view_id: str
    mag: float
    bbox_um: BBox


@dataclass(frozen=True)
class MeasurementFact:
    measurement_id: str
    length_um: float
    length_px: float


@dataclass(frozen=True)
class RuleCheck:
    rule: str
    status: Status
    message: str
    claim_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"rule": self.rule, "status": self.status, "message": self.message}
        if self.claim_id is not None:
            out["claim_id"] = self.claim_id
        return out


@dataclass(frozen=True)
class RuleOutcome:
    checks: tuple[RuleCheck, ...]
    coverage: dict[str, Any]

    @property
    def failures(self) -> list[RuleCheck]:
        return [c for c in self.checks if c.status == "fail"]


def _mag(x: float) -> str:
    return f"{x:g}x"


def check_e1(
    report: Report, views: Mapping[str, ViewFact], measurements: Mapping[str, Any]
) -> list[RuleCheck]:
    """E1: every cited view and measurement belongs to this session."""
    out = []
    for claim in report.claims:
        cited = list(claim.evidence) + ([claim.measurement_id] if claim.measurement_id else [])
        if not cited:
            continue
        unknown = [e for e in claim.evidence if e not in views]
        if claim.measurement_id and claim.measurement_id not in measurements:
            unknown.append(claim.measurement_id)
        if unknown:
            out.append(RuleCheck("E1", "fail", f"Claim {claim.id} cites ids not in this session: "
                                 f"{', '.join(unknown[:10])}.", claim.id))  # fmt: skip
        else:
            out.append(
                RuleCheck("E1", "pass", f"All ids cited by claim {claim.id} exist.", claim.id)
            )
    return out or [RuleCheck("E1", "not_applicable", "No claim cites a view or measurement.")]


def check_e2(report: Report, views: Mapping[str, ViewFact], confirm_min: float) -> list[RuleCheck]:
    """E2: a finding needs at least one evidence view at or above the confirmation magnification."""
    out = []
    for claim in report.claims:
        if claim.type != "finding":
            continue
        mags = [views[e].mag for e in claim.evidence if e in views]
        if not mags:
            out.append(RuleCheck("E2", "fail", f"Finding {claim.id} has no evidence view of this "
                                 "session.", claim.id))  # fmt: skip
        elif max(mags) >= confirm_min * (1 - _EPS):
            out.append(RuleCheck("E2", "pass", f"Finding {claim.id} was seen at "
                                 f"{_mag(max(mags))}.", claim.id))  # fmt: skip
        else:
            out.append(RuleCheck("E2", "fail", f"Finding {claim.id} was seen at most at "
                                 f"{_mag(max(mags))}; it needs a view at {_mag(confirm_min)} or "
                                 "higher.", claim.id))  # fmt: skip
    return out or [RuleCheck("E2", "not_applicable", "The report has no finding claims.")]


def _size_fits(size_mm: float, limits: Mapping[str, float]) -> tuple[bool | None, str]:
    """Whether a size meets a category's limits; None when only a cell count could decide."""
    ok = True
    parts = []
    for key, value in limits.items():
        if key == "gt_mm":
            ok &= size_mm > value
            parts.append(f"> {value:g} mm")
        elif key == "le_mm":
            ok &= size_mm <= value
            parts.append(f"<= {value:g} mm")
        elif key not in ("or_cells_gt", "cells_le"):
            return None, f"unknown limit '{key}'"
    if not ok and "or_cells_gt" in limits:
        return None, "only a cell count, which the environment cannot measure, could justify it"
    return ok, " and ".join(parts) or "no size limit"


def check_e3(
    report: Report,
    protocol: Protocol,
    measurements: Mapping[str, MeasurementFact],
) -> list[RuleCheck]:
    """E3: measurements span enough pixels, and the category agrees with the measured size."""
    out: list[RuleCheck] = []
    min_px = protocol.measurement.min_pixels_across
    measured = []
    for claim in report.claims:
        if claim.type != "measurement":
            continue
        m = measurements.get(claim.measurement_id or "")
        if m is None:
            out.append(RuleCheck("E3", "fail", f"Measurement claim {claim.id} does not cite a "
                                 "measurement of this session.", claim.id))  # fmt: skip
            continue
        measured.append(m.length_um / 1000)
        if m.length_px < min_px:
            out.append(RuleCheck("E3", "fail", f"Measurement {m.measurement_id} spans "
                                 f"{m.length_px:.1f} px; at least {min_px} px are required. "
                                 "Measure at a higher magnification.", claim.id))  # fmt: skip
        else:
            out.append(RuleCheck("E3", "pass", f"Measurement {m.measurement_id} spans "
                                 f"{m.length_px:.1f} px.", claim.id))  # fmt: skip

    c = report.conclusion
    if c.metastasis == "absent" and c.category != "none":
        out.append(
            RuleCheck("E3", "fail", f"Metastasis is absent but the category is {c.category}.")
        )
    elif c.metastasis == "present" and c.category == "none":
        out.append(RuleCheck("E3", "fail", "Metastasis is present but no category is given."))
    elif c.category != "none":
        rule = next((r for r in protocol.categories if r.id == c.category), None)
        size = max(measured) if measured else c.largest_deposit_mm
        if rule is None:
            out.append(RuleCheck("E3", "not_verifiable", f"Category {c.category} is not defined "
                                 "in the protocol."))  # fmt: skip
        elif size is None:
            out.append(RuleCheck("E3", "not_verifiable", f"Category {c.category} cannot be checked "
                                 "without a measured size."))  # fmt: skip
        else:
            fits, why = _size_fits(size, rule.limits)
            if fits is None:
                out.append(RuleCheck("E3", "not_verifiable", f"Category {c.category} at "
                                     f"{size:.3g} mm: {why}."))  # fmt: skip
            elif fits:
                out.append(RuleCheck("E3", "pass", f"{size:.3g} mm fits category {c.category} "
                                     f"({why})."))  # fmt: skip
            else:
                out.append(RuleCheck("E3", "fail", f"{size:.3g} mm does not fit category "
                                     f"{c.category} ({why})."))  # fmt: skip
    return out or [RuleCheck("E3", "not_applicable", "Nothing to measure in this report.")]


def _scope_mask(scope: str | None, tissue: TissueMap) -> tuple[NDArray[np.bool_] | None, str, str]:
    """(mask or None for the whole slide, label, error message if the scope is invalid)."""
    if scope is None:
        return None, "the whole slide", ""
    piece = _PIECE.fullmatch(scope)
    if piece:
        n = int(piece.group(1))
        if not 1 <= n <= len(tissue.pieces):
            return None, scope, f"tissue piece {n} does not exist ({len(tissue.pieces)} pieces)"
        return tissue.labels == n, f"tissue piece {n}", ""
    region = _REGION.fullmatch(scope)
    if region:
        x0, y0, x1, y1 = (float(v) for v in region.groups())
        if not (x1 > x0 and y1 > y0):
            return None, scope, "the region is empty"
        mask = np.zeros(tissue.shape, dtype=bool)
        rs, cs = tissue.cells_in(BBox(x0, y0, x1, y1))
        mask[rs, cs] = True
        return mask, f"region {scope[7:]} um", ""
    return None, scope, "use tissue_piece:N or region:x0,y0,x1,y1 (um)"


def check_e4(
    report: Report, protocol: Protocol, tissue: TissueMap, seen: NDArray[np.bool_]
) -> list[RuleCheck]:
    """E4: negative statements need the protocol's coverage at or above its magnification."""
    rules = protocol.coverage
    need = rules.negative_min_fraction
    targets: list[tuple[str | None, str | None]] = []  # (claim id, scope)
    if report.conclusion.metastasis == "absent":
        targets.append((None, None))
    targets += [(c.id, c.scope) for c in report.claims if c.type == "negative"]
    out = []
    for claim_id, scope in targets:
        mask, label, problem = _scope_mask(scope, tissue)
        who = f"Negative claim {claim_id}" if claim_id else "The negative conclusion"
        if problem:
            out.append(RuleCheck("E4", "fail", f"{who} has an invalid scope: {problem}.", claim_id))
            continue
        cov = coverage(tissue, seen, rules.at_or_above_mag, mask)
        msg = (f"{who}: {cov.fraction:.1%} of the tissue in {label} was seen at "
               f"{_mag(rules.at_or_above_mag)} or higher (required {need:.0%}).")  # fmt: skip
        out.append(RuleCheck("E4", "pass" if cov.fraction >= need else "fail", msg, claim_id))
    return out or [RuleCheck("E4", "not_applicable", "The report states nothing negative.")]


def check_report(
    report: Report,
    *,
    protocol: Protocol,
    views: Mapping[str, ViewFact],
    measurements: Mapping[str, MeasurementFact],
    tissue: TissueMap,
) -> RuleOutcome:
    min_mag = protocol.coverage.at_or_above_mag
    seen = seen_cells(tissue, (v.bbox_um for v in views.values() if v.mag >= min_mag * (1 - _EPS)))
    checks = [
        *check_e1(report, views, measurements),
        *check_e2(report, views, protocol.magnification.confirm_min),
        *check_e3(report, protocol, measurements),
        *check_e4(report, protocol, tissue, seen),
    ]
    whole = coverage(tissue, seen, min_mag)
    summary = whole.to_dict()
    summary["pieces"] = [
        {
            **p.to_dict(),
            "fraction": round(
                coverage(tissue, seen, min_mag, tissue.labels == p.piece).fraction, 6
            ),
        }
        for p in tissue.pieces
    ]
    return RuleOutcome(tuple(checks), summary)
