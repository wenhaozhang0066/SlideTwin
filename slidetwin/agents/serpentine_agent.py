"""Serpentine baseline (SPEC 12).

Scans every field that holds tissue at 10x, row by row in alternating directions, looks again at
20x at the fields a colour heuristic flags, and reports with a coverage proof (a negative claim)
for each tissue piece where nothing was flagged. It demonstrates coverage proofs and rule checks;
it does not aim at diagnostic accuracy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from scipy import ndimage

from slidetwin.agents.client import Env, EnvSession
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
    scan_mag: float = 10.0
    check_mag: float = 20.0
    flag_fraction: float = 0.3  # share of dense-purple tissue pixels that flags a field
    max_checks: int = 3


@dataclass(frozen=True)
class Scan:
    observation: dict[str, Any]
    score: float
    box_px: tuple[int, int, int, int] | None  # largest dense-purple region in the view


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


def _piece_at(pieces: list[dict[str, Any]], x: float, y: float) -> int | None:
    """The tissue piece whose box holds (x, y) um, else the one with the nearest box."""
    if not pieces:
        return None

    def gap(p: dict[str, Any]) -> float:
        x0, y0, x1, y1 = p["bbox_um"]
        return math.hypot(max(x0 - x, 0, x - x1), max(y0 - y, 0, y - y1))

    best: int = min(pieces, key=gap)["piece"]
    return best


async def _check(
    s: EnvSession, scan: Scan, config: SerpentineConfig, n: int
) -> list[dict[str, Any]]:
    """Centre a flagged region, look at it at the check magnification, measure its extent."""
    box = scan.box_px or (0, 0, *scan.observation["size_px"])
    await go_to(
        s, *view_point_to_um(scan.observation, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    )
    if s.last.state["mag"] != config.check_mag:
        await s.set_magnification(config.check_mag)
    view = s.last
    claims = [
        {
            "id": f"f{n}",
            "type": "finding",
            "text": "Dense purple region flagged by a colour heuristic, examined at "
            f"{config.check_mag:g}x.",
            "evidence": [view.observation["view_id"]],
        }
    ]
    rgb = decode(view.image or b"")
    region = mask_box(dense_purple(rgb) & tissue_mask(rgb))
    if region is None:
        return claims
    x0, y0, x1, y1 = region
    w, h = view.observation["size_px"]
    p1, p2 = ([x0, (y0 + y1) / 2], [x1, (y0 + y1) / 2])
    if y1 - y0 > x1 - x0:
        p1, p2 = ([(x0 + x1) / 2, y0], [(x0 + x1) / 2, y1])
    if math.dist(p1, p2) < 20:
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
    thumb = await s.get_thumbnail()
    pieces = thumb.observation["tissue_pieces"]
    mask = tissue_mask(decode(thumb.image or b""))
    ux, uy = thumb.observation["um_per_px"]
    # The pieces the environment published may include pale or small tissue that this mask
    # misses; scanning their boxes too keeps every negative claim backed by a full scan.
    for p in pieces:
        x0, y0, x1, y1 = p["bbox_um"]
        mask[int(y0 / uy) : math.ceil(y1 / uy), int(x0 / ux) : math.ceil(x1 / ux)] = True
    await s.set_magnification(config.scan_mag)
    w, h = s.slide["dimensions_um"]
    fw, fh = s.last.state["fov_um"]
    fields = plan_serpentine(mask, (ux, uy), (w, h), (fw, fh))
    scans: list[Scan] = []
    for x, y in fields:
        step = await go_to(s, x, y)
        rgb = decode(step.image or b"")
        box = mask_box(dense_purple(rgb) & tissue_mask(rgb))
        scans.append(Scan(step.observation, purple_fraction(rgb), box))

    flagged = sorted(
        (sc for sc in scans if sc.score >= config.flag_fraction), key=lambda sc: -sc.score
    )
    claims: list[dict[str, Any]] = []
    for n, scan in enumerate(flagged[: config.max_checks], start=1):
        claims += await _check(s, scan, config, n)
    # A flag counts against the piece that holds the centre of its purple region.
    flagged_pieces = set()
    for sc in flagged:
        x0, y0, x1, y1 = sc.box_px or (0, 0, *sc.observation["size_px"])
        flagged_pieces.add(
            _piece_at(pieces, *view_point_to_um(sc.observation, (x0 + x1) / 2, (y0 + y1) / 2))
        )
    for p in pieces:
        if p["piece"] not in flagged_pieces:
            claims.append(
                {
                    "id": f"n{p['piece']}",
                    "type": "negative",
                    "text": f"No colour-flagged region in tissue piece {p['piece']}, scanned at "
                    f"{config.scan_mag:g}x.",
                    "scope": f"tissue_piece:{p['piece']}",
                }
            )
    body = report("indeterminate" if flagged else "absent", "none", claims)
    notes = [
        f"scanned {len(fields)} fields at {config.scan_mag:g}x",
        f"{len(flagged)} flagged, {min(len(flagged), config.max_checks)} checked at "
        f"{config.check_mag:g}x",
    ]
    return await finish(s, body, notes)
