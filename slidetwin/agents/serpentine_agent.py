"""Serpentine baseline (SPEC 12).

Scans every field that holds tissue at 10x, row by row in alternating directions, looks again at
20x at the fields a colour heuristic flags, and reports with a coverage proof (a negative claim)
for each tissue piece where nothing was flagged. It demonstrates coverage proofs and rule checks;
it does not aim at diagnostic accuracy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scipy import ndimage

from slidetwin.agents.client import Env, EnvError, EnvSession
from slidetwin.agents.common import AgentResult, finish, go_to, report, view_point_to_um
from slidetwin.agents.vision import (
    Mask,
    decode,
    dense_purple,
    mask_box,
    purple_fraction,
    tissue_mask,
)


@dataclass(frozen=True)
class SerpentineConfig:
    scan_mag: float | None = None  # None: the protocol's coverage magnification (10x)
    check_mag: float | None = None  # None: the protocol's confirmation magnification (20x)
    flag_fraction: float = 0.3  # share of dense-purple tissue pixels that flags a field
    max_checks: int = 3
    max_claims: int = 200  # the report schema's limit


@dataclass(frozen=True)
class Scan:
    observation: dict[str, Any]
    score: float
    box_px: tuple[int, int, int, int] | None  # largest dense-purple region in the view


Box = tuple[float, float, float, float]


def plan_serpentine(
    mask: Mask,
    um_per_px: tuple[float, float],
    slide_um: tuple[float, float],
    fov_um: tuple[float, float],
) -> list[tuple[float, float]]:
    """Centres of the fields that tile the slide at this field size and hold tissue in `mask` (a
    thumbnail mask, dilated by two pixels to be safe), row by row in alternating directions."""
    tissue = ndimage.binary_dilation(mask, iterations=2)
    (ux, uy), (w, h), (fw, fh) = um_per_px, slide_um, fov_um
    rows, cols = tissue.shape
    out: list[tuple[float, float]] = []
    for j in range(math.ceil(h / fh)):
        y0, y1 = int(j * fh / uy), min(rows, math.ceil((j + 1) * fh / uy))
        row = [
            ((i + 0.5) * fw, (j + 0.5) * fh)
            for i in range(math.ceil(w / fw))
            if tissue[y0:y1, int(i * fw / ux) : min(cols, math.ceil((i + 1) * fw / ux))].any()
        ]
        out.extend(reversed(row) if j % 2 else row)
    return out


def _overlaps(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _region_um(scan: Scan) -> Box:
    x0, y0, x1, y1 = scan.box_px or (0, 0, *scan.observation["size_px"])
    ax, ay = view_point_to_um(scan.observation, x0, y0)
    bx, by = view_point_to_um(scan.observation, x1, y1)
    return ax, ay, bx, by


def _pieces_touching(pieces: list[dict[str, Any]], region: Box) -> set[int]:
    """Every piece whose box meets the region: boxes can overlap or nest, so this is the only
    attribution that never clears a piece holding a flag. With no box met, the nearest piece."""
    hit = {p["piece"] for p in pieces if _overlaps(p["bbox_um"], region)}
    if hit or not pieces:
        return hit
    cx, cy = (region[0] + region[2]) / 2, (region[1] + region[3]) / 2

    def gap(p: dict[str, Any]) -> float:
        x0, y0, x1, y1 = p["bbox_um"]
        return math.hypot(max(x0 - cx, 0, cx - x1), max(y0 - cy, 0, cy - y1))

    return {min(pieces, key=gap)["piece"]}


def _preset(available: Sequence[float], wanted: float) -> float:
    """The lowest available preset at or above `wanted`, else the highest available."""
    return min((m for m in available if m >= wanted * 0.999), default=max(available))


async def _check(
    s: EnvSession, scan: Scan, check_mag: float, min_px: float, n: int
) -> list[dict[str, Any]]:
    """Look at a flagged region at the check magnification and measure its visible extent. The
    magnification changes first, so the move is clamped only by the slide's edge at this field
    size; a finding is made only if the view shows the region."""
    if s.last.state["mag"] != check_mag:
        await s.set_magnification(check_mag)
    region = _region_um(scan)
    view = await go_to(s, (region[0] + region[2]) / 2, (region[1] + region[3]) / 2)
    rgb = decode(view.image or b"")
    seen = mask_box(dense_purple(rgb) & tissue_mask(rgb))
    if seen is None:
        return []
    claims = [
        {
            "id": f"f{n}",
            "type": "finding",
            "text": "Dense purple region flagged by a colour heuristic, examined at "
            f"{check_mag:g}x.",
            "evidence": [view.observation["view_id"]],
        }
    ]
    x0, y0, x1, y1 = seen
    w, h = view.observation["size_px"]
    p1, p2 = ([x0, (y0 + y1) / 2], [x1, (y0 + y1) / 2])
    if y1 - y0 > x1 - x0:
        p1, p2 = ([(x0 + x1) / 2, y0], [(x0 + x1) / 2, y1])
    if math.dist(p1, p2) < min_px:
        return claims
    m = (await s.measure(p1, p2)).observation
    cut = " It extends beyond this field." if x0 == 0 or y0 == 0 or x1 == w or y1 == h else ""
    claims.append(
        {
            "id": f"m{n}",
            "type": "measurement",
            "text": f"Longest visible extent of the flagged region: {m['length_um']:.0f} um.{cut}",
            "measurement_id": m["measurement_id"],
        }
    )
    return claims


async def run(
    env: Env,
    slide_id: str,
    *,
    actor: str = "agent:serpentine",
    profile: str = "viewer",
    viewport_px: list[int] | None = None,
    config: SerpentineConfig | None = None,
) -> AgentResult:
    config = config or SerpentineConfig()
    s = await env.open(slide_id, actor=actor, profile=profile, viewport_px=viewport_px)
    task = s.task
    available = s.slide["available_mags"]
    scan_mag = _preset(available, config.scan_mag or float(task["coverage"]["at_or_above_mag"]))
    check_mag = _preset(available, config.check_mag or float(task["magnification"]["confirm_min"]))
    min_px = float(task["measurement"]["min_pixels_across"])
    notes: list[str] = []
    scans: list[Scan] = []
    pieces: list[dict[str, Any]] = []
    unvisited: list[Box] = []  # planned fields never seen (a refused action ends the scan)
    try:
        thumb = await s.get_thumbnail()
        pieces = thumb.observation["tissue_pieces"]
        mask = tissue_mask(decode(thumb.image or b""))
        ux, uy = thumb.observation["um_per_px"]
        # The pieces the environment published may include pale or small tissue that this mask
        # misses; scanning their boxes too keeps every negative claim backed by a full scan.
        for p in pieces:
            x0, y0, x1, y1 = p["bbox_um"]
            mask[int(y0 / uy) : math.ceil(y1 / uy), int(x0 / ux) : math.ceil(x1 / ux)] = True
        await s.set_magnification(scan_mag)
        w, h = s.slide["dimensions_um"]
        fw, fh = s.last.state["fov_um"]
        fields = plan_serpentine(mask, (ux, uy), (w, h), (fw, fh))
        unvisited = [(x - fw / 2, y - fh / 2, x + fw / 2, y + fh / 2) for x, y in fields]
        for x, y in fields:
            step = await go_to(s, x, y)
            rgb = decode(step.image or b"")
            box = mask_box(dense_purple(rgb) & tissue_mask(rgb))
            scans.append(Scan(step.observation, purple_fraction(rgb), box))
            unvisited.pop(0)
        notes.append(f"scanned {len(fields)} fields at {scan_mag:g}x")
    except EnvError as err:
        notes.append(f"scan stopped after {len(scans)} fields: {err.code}")

    flagged = sorted(
        (sc for sc in scans if sc.score >= config.flag_fraction), key=lambda sc: -sc.score
    )
    claims: list[dict[str, Any]] = []
    checked = 0
    try:
        for n, scan in enumerate(flagged[: config.max_checks], start=1):
            got = await _check(s, scan, check_mag, min_px, n)
            checked += 1
            if not got:
                notes.append(f"flag {n}: no purple region in the {check_mag:g}x view")
            claims += got
    except EnvError as err:
        notes.append(f"checks stopped after {checked}: {err.code}")
    notes.append(f"{len(flagged)} flagged, {checked} checked at {check_mag:g}x")

    flagged_pieces: set[int] = set()
    for sc in flagged:
        flagged_pieces |= _pieces_touching(pieces, _region_um(sc))
    negatives = [
        p
        for p in pieces
        if p["piece"] not in flagged_pieces
        and not any(_overlaps(p["bbox_um"], f) for f in unvisited)
    ]
    room = max(0, config.max_claims - len(claims))
    if len(negatives) > room:
        notes.append(f"{len(negatives) - room} scanned pieces left without a claim (report limit)")
    for p in negatives[:room]:
        claims.append(
            {
                "id": f"n{p['piece']}",
                "type": "negative",
                "text": f"No colour-flagged region in tissue piece {p['piece']}, scanned at "
                f"{scan_mag:g}x.",
                "scope": f"tissue_piece:{p['piece']}",
            }
        )
    complete = not unvisited and bool(scans)
    conclusion = "absent" if complete and not flagged else "indeterminate"
    return await finish(s, report(conclusion, "none", claims), notes)
