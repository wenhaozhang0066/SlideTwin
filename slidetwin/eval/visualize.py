"""Pictures of a reading on the slide overview: the path of views, a replay GIF, a coverage heatmap.

Outputs are for evaluation reports and stay out of git (slide licences). Lesion outlines appear
only here, never in the reading UI.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from slidetwin.core.coverage import TissueMap
from slidetwin.core.geometry import BBox
from slidetwin.core.render import render_region
from slidetwin.core.slide_source import SlideSource, slide_size_um
from slidetwin.eval.annotations import Region
from slidetwin.eval.metrics import CONFIRM_MAG, SCREEN_MAG, SeenView

LESION = (255, 225, 0)
PATH = (255, 255, 255)
UNSEEN = (215, 48, 39)
LOW = (253, 174, 97)  # seen only below the screening magnification
SCREEN = (102, 189, 99)
CONFIRM = (26, 120, 65)


def mag_colour(mag: float) -> tuple[int, int, int]:
    if mag >= 2 * CONFIRM_MAG * 0.999:
        return (197, 27, 125)
    if mag >= CONFIRM_MAG * 0.999:
        return (241, 105, 19)
    if mag >= SCREEN_MAG * 0.999:
        return (35, 139, 69)
    return (49, 130, 189)


class Overview:
    """The slide rendered small, with the um -> pixel transform of that picture."""

    def __init__(self, source: SlideSource, long_side: int = 1024) -> None:
        w_um, h_um = slide_size_um(source)
        scale = long_side / max(w_um, h_um)
        self.size = (max(1, round(w_um * scale)), max(1, round(h_um * scale)))
        self.um_per_px = (w_um / self.size[0], h_um / self.size[1])
        self.image = Image.fromarray(render_region(source, BBox(0.0, 0.0, w_um, h_um), self.size))

    def px(self, x_um: float, y_um: float) -> tuple[float, float]:
        return x_um / self.um_per_px[0], y_um / self.um_per_px[1]

    def box(self, b: BBox) -> tuple[float, float, float, float]:
        x0, y0 = self.px(b.x0, b.y0)
        x1, y1 = self.px(b.x1, b.y1)
        return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def _lesions(draw: ImageDraw.ImageDraw, ov: Overview, regions: Sequence[Region]) -> None:
    for r in regions:
        pts = [ov.px(float(x), float(y)) for x, y in r.ring]
        draw.line([*pts, pts[0]], fill=LESION if r.group == "tumor" else (0, 0, 0), width=2)


def _legend(draw: ImageDraw.ImageDraw, items: Sequence[tuple[str, tuple[int, int, int]]]) -> None:
    y = 6
    for text, colour in items:
        draw.rectangle([6, y, 18, y + 10], fill=colour, outline=(0, 0, 0))
        draw.text((24, y - 1), text, fill=(0, 0, 0))
        y += 14


def path_overlay(
    ov: Overview, views: Sequence[SeenView], regions: Sequence[Region] = ()
) -> Image.Image:
    """Every view as a rectangle coloured by magnification, joined in reading order."""
    img = ov.image.convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for v in views:
        draw.rectangle(ov.box(v.bbox), outline=(*mag_colour(v.mag), 230), width=2)
    centres = [ov.px((v.bbox.x0 + v.bbox.x1) / 2, (v.bbox.y0 + v.bbox.y1) / 2) for v in views]
    if len(centres) > 1:
        draw.line(centres, fill=(*PATH, 200), width=1)
    if centres:
        x, y = centres[0]
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(*PATH, 255), outline=(0, 0, 0, 255))
    _lesions(draw, ov, regions)
    out = Image.alpha_composite(img, layer).convert("RGB")
    _legend(
        ImageDraw.Draw(out),
        [
            ("< 10x", mag_colour(5)),
            ("10x", mag_colour(10)),
            ("20x", mag_colour(20)),
            (">= 40x", mag_colour(40)),
            ("lesion (eval only)", LESION),
        ],
    )
    return out


def replay_gif(
    ov: Overview,
    views: Sequence[SeenView],
    path: Path,
    regions: Sequence[Region] = (),
    *,
    max_frames: int = 80,
    frame_ms: int = 150,
) -> int:
    """A GIF adding the views in reading order (the current one bold); returns the frame count."""
    if not views:
        return 0
    stops = sorted({round(i) for i in np.linspace(1, len(views), min(max_frames, len(views)))})
    base = ov.image.convert("RGB")
    frames = []
    for k in stops:
        frame = base.copy()
        draw = ImageDraw.Draw(frame)
        _lesions(draw, ov, regions)
        for v in views[: k - 1]:
            draw.rectangle(ov.box(v.bbox), outline=mag_colour(v.mag), width=1)
        current = views[k - 1]
        draw.rectangle(ov.box(current.bbox), outline=(0, 0, 0), width=4)
        draw.rectangle(ov.box(current.bbox), outline=mag_colour(current.mag), width=2)
        draw.text(
            (6, 6), f"{k}/{len(views)}  {current.mag:g}x  t={current.t_s:.1f} s", fill=(0, 0, 0)
        )
        frames.append(frame.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=frame_ms, loop=0, optimize=True
    )
    return len(frames)


def coverage_heatmap(
    ov: Overview, tissue: TissueMap, views: Sequence[SeenView], regions: Sequence[Region] = ()
) -> Image.Image:
    """Tissue cells coloured by the highest magnification they were seen at (red: never)."""
    best = np.zeros(tissue.shape, dtype=np.float64)
    for v in sorted(views, key=lambda v: v.mag):
        rs, cs = tissue.cells_in(v.bbox)
        best[rs, cs] = v.mag
    colours = np.zeros((*tissue.shape, 4), dtype=np.uint8)
    for cond, colour in [
        (tissue.mask & (best == 0), UNSEEN),
        (tissue.mask & (best > 0) & (best < SCREEN_MAG * 0.999), LOW),
        (tissue.mask & (best >= SCREEN_MAG * 0.999) & (best < CONFIRM_MAG * 0.999), SCREEN),
        (tissue.mask & (best >= CONFIRM_MAG * 0.999), CONFIRM),
    ]:
        colours[cond] = (*colour, 150)
    # The grid covers whole cells; crop the part beyond the slide before scaling to the overview.
    g = tissue.grid_um
    w_um, h_um = ov.size[0] * ov.um_per_px[0], ov.size[1] * ov.um_per_px[1]
    grid = Image.fromarray(colours, "RGBA").resize(
        (
            round(tissue.shape[1] * g / ov.um_per_px[0]),
            round(tissue.shape[0] * g / ov.um_per_px[1]),
        ),
        Image.Resampling.NEAREST,
    )
    grid = grid.crop((0, 0, round(w_um / ov.um_per_px[0]), round(h_um / ov.um_per_px[1])))
    out = Image.alpha_composite(ov.image.convert("RGBA"), grid.resize(ov.size)).convert("RGB")
    draw = ImageDraw.Draw(out)
    _lesions(draw, ov, regions)
    _legend(
        draw,
        [
            ("tissue never seen", UNSEEN),
            ("seen below 10x only", LOW),
            ("seen at 10x", SCREEN),
            ("seen at 20x or more", CONFIRM),
        ],
    )
    return out
