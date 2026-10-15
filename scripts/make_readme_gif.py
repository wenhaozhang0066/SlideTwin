"""Record the README animation (M7): a scripted reading of the CC0 test slide in headless Chromium.

    PLAYWRIGHT_BROWSERS_PATH=.tools/pw-browsers python scripts/make_readme_gif.py

Needs the e2e extra, a Chromium build and the CMU test slide (scripts/fetch_test_data.py) linked
under data/slides/openslide-testdata. Writes docs/media/reading.gif. Only the CC0 slide is used:
the CAMELYON images may not be redistributed in modified form.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from tests.helpers.live import serve

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "media" / "reading.gif"
WIDTH = 960


def main() -> None:
    settings = Settings(
        config_dir=REPO / "configs",
        manifest=REPO / "configs" / "slides" / "manifest.yaml",
        slide_dir=REPO / "data" / "slides",
        log_dir=Path(tempfile.mkdtemp()),
        cache_dir=REPO / "cache",
    )
    frames: list[Image.Image] = []
    with serve(create_app(settings)) as url, sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def shot() -> None:
            page.wait_for_timeout(250)
            img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
            frames.append(
                img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.Resampling.LANCZOS)
            )

        def idle() -> None:
            import re

            expect(page.locator("body")).not_to_have_class(re.compile(r"\bbusy\b"))

        page.goto(url + "/")
        expect(page.locator("#slide-select option")).not_to_have_count(0)
        page.select_option("#slide-select", "cmu1_small")
        page.fill("#alias", "demo")
        shot()
        page.click("#start-button")
        expect(page.locator("#negative-scope option")).not_to_have_count(1)
        idle()
        shot()
        overlay = page.locator("#overlay-canvas")
        for key in ["3", "4"]:
            page.keyboard.press(key)
            idle()
            shot()
        page.keyboard.press("ArrowRight")
        idle()
        shot()
        page.keyboard.press("5")
        idle()
        shot()
        page.keyboard.press("m")
        overlay.click(position={"x": 300, "y": 300})
        overlay.click(position={"x": 520, "y": 420})
        idle()
        shot()
        page.keyboard.press("g")
        for x, y in [(650, 250), (820, 270), (800, 420), (640, 400)]:
            overlay.click(position={"x": x, "y": y})
        page.keyboard.press("Enter")
        page.fill("#label-input", "region")
        page.keyboard.press("Enter")
        idle()
        shot()
        page.click("#add-finding")
        page.click("#add-measurement")
        texts = page.locator("#claims li input")
        texts.nth(0).fill("example finding")
        texts.nth(1).fill("example size")
        page.select_option("#r-metastasis", "indeterminate")
        page.select_option("#r-category", "none")
        page.select_option("#r-ene", "not_assessable")
        page.click("#submit-report")
        expect(page.locator("#receipt")).to_be_visible()
        page.locator("#receipt").scroll_into_view_if_needed()
        shot()
        browser.close()
    palette = [f.quantize(colors=192, method=Image.Quantize.MEDIANCUT) for f in frames]
    durations = [1600] * len(palette)
    durations[-1] = 3500
    palette[0].save(
        OUT, save_all=True, append_images=palette[1:], duration=durations, loop=0, optimize=True
    )
    print(f"{OUT} ({OUT.stat().st_size / 1e6:.1f} MB, {len(palette)} frames)")


if __name__ == "__main__":
    main()
