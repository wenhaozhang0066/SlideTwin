"""Thumbnail-and-crop baseline (SPEC 12), the way many current pathology agents work.

It looks at the thumbnail, crops the most purple tissue blocks at full resolution and concludes from
those crops. It runs only under the `unconstrained` profile, the control condition: the `viewer`
profile has no crop action.
"""

from __future__ import annotations

from typing import Any

from slidetwin.agents.client import Env
from slidetwin.agents.common import AgentResult, finish, report
from slidetwin.agents.vision import RGB, decode, dense_purple, purple_fraction, tissue_mask

PROFILE = "unconstrained"


def pick_blocks(rgb: RGB, block_px: int, n: int) -> list[tuple[int, int]]:
    """Top-left corners (thumbnail px) of up to `n` non-overlapping blocks with the most
    dense-purple tissue, best first; blocks without tissue are never picked."""
    tissue = tissue_mask(rgb)
    purple = dense_purple(rgb) & tissue
    rows, cols = tissue.shape
    scored = []
    for y in range(0, rows, block_px):
        for x in range(0, cols, block_px):
            t = tissue[y : y + block_px, x : x + block_px].sum()
            if t:
                p = purple[y : y + block_px, x : x + block_px].sum()
                scored.append((float(p) + 1e-3 * float(t), x, y))
    scored.sort(reverse=True)
    return [(x, y) for _, x, y in scored[:n]]


async def run(
    env: Env,
    slide_id: str,
    *,
    crops: int = 6,
    crop_um: float = 400.0,
    flag_fraction: float = 0.3,
    actor: str = "agent:crop",
    viewport_px: list[int] | None = None,
) -> AgentResult:
    s = await env.open(slide_id, actor=actor, profile=PROFILE, viewport_px=viewport_px)
    thumb = await s.get_thumbnail()
    ux, uy = thumb.observation["um_per_px"]
    block_px = max(1, round(crop_um / ux))
    claims: list[dict[str, Any]] = []
    best = 0.0
    for n, (x, y) in enumerate(pick_blocks(decode(thumb.image or b""), block_px, crops), start=1):
        crop = await s.crop(x * ux, y * uy, crop_um, crop_um)
        score = purple_fraction(decode(crop.image or b""))
        best = max(best, score)
        if score >= flag_fraction:
            claims.append(
                {
                    "id": f"f{n}",
                    "type": "finding",
                    "text": f"Dense purple tissue in a crop ({score:.0%} of the tissue pixels).",
                    "evidence": [crop.observation["view_id"]],
                }
            )
    if claims:
        return await finish(s, report("present", "micro", claims), [f"best crop {best:.2f}"])
    negative = {"id": "n1", "type": "negative", "text": "No dense purple tissue in the crops."}
    return await finish(s, report("absent", "none", [negative]), [f"best crop {best:.2f}"])
