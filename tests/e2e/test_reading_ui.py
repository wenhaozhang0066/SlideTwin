"""End-to-end test of the reading UI (M3) in headless Chromium.

A reader opens a synthetic slide, uses keys, drag, the overview, the measuring and annotation
tools, submits a report and downloads the trajectory, which then replays identically.

Needs Playwright and its Chromium build (`pip install -e .[e2e]`, then
`PLAYWRIGHT_BROWSERS_PATH=.tools/pw-browsers python -m playwright install chromium`). Skips without
them, unless SLIDETWIN_REQUIRE_E2E=1.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.replay import replay
from tests.conftest import CONFIG_DIR, REPO_ROOT
from tests.helpers.live import serve
from tests.helpers.synth import PINK, PURPLE, pyramid, tissue_image, write_pyramid_tiff

pytestmark = pytest.mark.e2e

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

SECRET = "SECRET_E2E_LABEL_789"
MPP = 0.5  # native 20x: the 40x preset is not available
SIZE_PX = (6144, 4608)  # 3072 x 2304 um
PIECES_PX = [(600, 600, 3000, 2400, PINK), (4200, 3000, 1200, 1000, PURPLE)]


@pytest.fixture(scope="module")
def slide_setup(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("e2e_slides")
    write_pyramid_tiff(d / "tissue.tif", pyramid(tissue_image(*SIZE_PX, PIECES_PX), 5), MPP)
    manifest = d / "manifest.yaml"
    entry = {"id": "syn_tissue", "path": "tissue.tif", "license": "CC0-1.0"}
    manifest.write_text(yaml.safe_dump({"slides": [{**entry, "eval_only": {"label": SECRET}}]}))
    return manifest, d


@pytest.fixture(scope="module")
def e2e_logs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("e2e_logs")


@pytest.fixture(scope="module")
def server(slide_setup: tuple[Path, Path], e2e_logs: Path) -> Iterator[str]:
    manifest, slide_dir = slide_setup
    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=manifest,
        slide_dir=slide_dir,
        log_dir=e2e_logs,
        cache_dir=None,
        max_sessions=4,
        idle_ttl_s=600.0,
    )
    with serve(create_app(settings)) as url:
        yield url


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    local = REPO_ROOT / ".tools" / "pw-browsers"
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and local.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local)
    with playwright_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as err:  # the browser build is not installed
            if os.environ.get("SLIDETWIN_REQUIRE_E2E") == "1":
                raise
            pytest.skip(f"Chromium for Playwright is not available: {type(err).__name__}")
        yield chromium
        chromium.close()


def _idle(page: Any) -> None:
    # A locator wait, not wait_for_function: the page's CSP forbids eval, and the test keeps it on.
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bbusy\b"))


def _changes(page: Any, action: Any) -> None:
    """Run `action` and wait until the view caption shows a new view."""
    info = page.locator("#view-info")
    before = info.inner_text()
    action()
    expect(info).not_to_have_text(before)
    _idle(page)


def test_full_reading_in_the_browser(
    server: str, browser: Any, slide_setup: tuple[Path, Path], e2e_logs: Path, tmp_path: Path
) -> None:
    page = browser.new_page(viewport={"width": 1700, "height": 1150})
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.goto(server + "/")

    # Start screen: the catalogue shows opaque ids only.
    expect(page.locator("#slide-select option")).to_have_count(1)
    assert page.locator("#slide-select").input_value() == "syn_tissue"
    expect(page.locator("#slide-source")).to_have_text("Source: not stated. Licence: CC0-1.0.")
    expect(page.locator("#profile-select")).to_have_value("viewer")  # the API's default too
    page.fill("#alias", "e2e-reader")
    page.check("#timer-toggle")
    page.click("#start-button")
    expect(page.locator("#reader")).to_be_visible()
    expect(page.locator("#negative-scope option")).to_have_count(3)  # whole slide + 2 tissue pieces
    expect(page.locator("#mag-buttons button[data-mag='40']")).to_be_disabled()
    expect(page.locator("#timer")).to_contain_text("Time used")

    # The view is shown at one device pixel per view pixel.
    size = page.eval_on_selector(
        "#view-canvas", "c => [c.width, c.height, c.clientWidth, c.clientHeight]"
    )
    assert size == [1280, 1024, 1280, 1024]

    info = page.locator("#view-info")
    _changes(page, lambda: page.keyboard.press("4"))
    expect(info).to_contain_text("at 10x")
    _changes(page, lambda: page.keyboard.press("ArrowRight"))

    box = page.locator("#overlay-canvas").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def drag() -> None:
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx - 320, cy - 256, steps=4)
        page.mouse.up()

    _changes(page, drag)

    # Overview click on the centre of tissue piece 1.
    mini = page.locator("#minimap").bounding_box()
    x0, y0, w, h, _ = PIECES_PX[0]
    fx, fy = (x0 + w / 2) / SIZE_PX[0], (y0 + h / 2) / SIZE_PX[1]
    _changes(
        page,
        lambda: page.locator("#minimap").click(
            position={"x": fx * mini["width"], "y": fy * mini["height"]}
        ),
    )

    # The overview from the keyboard: focus, move its cursor, Enter.
    def overview_keys() -> None:
        page.locator("#minimap").focus()
        page.keyboard.press("ArrowRight")
        page.keyboard.press("Shift+ArrowDown")
        page.keyboard.press("Enter")

    _changes(page, overview_keys)
    _changes(page, lambda: page.locator("#mag-buttons button[data-mag='20']").click())
    expect(info).to_contain_text("at 20x")

    message = page.locator("#message")
    page.keyboard.press("m")
    overlay = page.locator("#overlay-canvas")
    overlay.click(position={"x": 100, "y": 100})
    overlay.click(position={"x": 300, "y": 100})
    expect(message).to_contain_text("Measured 100 µm")  # 200 view px at 0.5 um/px
    # The same tool from the keyboard: Space at the cursor, Shift+arrows in 50 px steps.
    overlay.focus()
    page.keyboard.press("Space")
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Space")
    expect(message).to_contain_text("Measured 50 µm")

    page.keyboard.press("p")
    overlay.click(position={"x": 400, "y": 300})
    page.fill("#label-input", "focus-a")
    page.keyboard.press("Enter")
    expect(message).to_contain_text("Annotation")
    page.keyboard.press("g")
    for x, y in [(500, 500), (700, 500), (600, 700)]:
        overlay.click(position={"x": x, "y": y})
    page.keyboard.press("Enter")
    page.fill("#label-input", "region-b")
    page.keyboard.press("Enter")
    expect(page.locator("#label-form")).to_be_hidden()
    _idle(page)

    page.click("#add-finding")
    page.click("#add-measurement")
    page.select_option("#negative-scope", "2")
    page.click("#add-negative")
    texts = page.locator("#claims li input")
    expect(texts).to_have_count(3)
    for i, text in enumerate(["clusters of atypical cells", "largest deposit", "no deposits"]):
        texts.nth(i).fill(text)
    # Nothing is preselected: submitting now is stopped by the form, before any request.
    assert page.locator("#r-metastasis").input_value() == ""
    page.click("#submit-report")
    assert page.locator("#r-metastasis").evaluate("el => el.validity.valueMissing")
    expect(page.locator("#receipt")).to_be_hidden()
    page.select_option("#r-metastasis", "present")
    page.select_option("#r-category", "micro")
    page.fill("#r-largest", "0.1")
    page.select_option("#r-ene", "absent")
    page.click("#submit-report")
    receipt = page.locator("#receipt")
    expect(receipt).to_contain_text("Report accepted")
    expect(receipt).to_contain_text("E1")
    expect(page.locator("#submit-report")).to_be_disabled()

    # Optional pilot feedback, stored next to the session log.
    feedback = page.locator("#feedback-form")
    expect(feedback).to_be_visible()
    page.locator("fieldset[data-question='navigation'] input[value='4']").check()
    page.locator("fieldset[data-question='timing'] input[value='2']").check()
    page.fill("#f-comment", "pan felt slow")
    page.click("#feedback-form button[type='submit']")
    expect(page.locator("#feedback-thanks")).to_be_visible()
    saved = json.loads(next((e2e_logs / "feedback").glob("*.json")).read_text())
    assert (saved["navigation"], saved["timing"], saved["report"]) == (4, 2, None)
    assert saved["comment"] == "pan felt slow"

    page.click("#lang-switch")
    expect(page.locator(".banner-note")).to_contain_text("不用于临床诊断")
    page.click("#help-button")
    expect(page.locator("#help")).to_be_visible()
    expect(page.locator("#help")).to_contain_text("如何在这里阅片")
    page.click("#help button[type='submit']")
    expect(page.locator("#help")).to_be_hidden()
    assert SECRET not in page.content()

    with page.expect_download() as download:
        page.click("#download")
    log = tmp_path / "trajectory.jsonl"
    download.value.save_as(log)
    header, *events = [json.loads(line) for line in log.read_text(encoding="ascii").splitlines()]
    assert header["actor"] == "human:e2e-reader"
    actions = [e["action"] for e in events]
    for action in [
        "get_thumbnail",
        "set_magnification",
        "pan",
        "move_to",
        "measure",
        "annotate",
        "submit_report",
    ]:
        assert action in actions, action
    assert not [e for e in events if e["error"]], "the UI sent an action the server refused"
    inputs = {e["client"]["input"] for e in events if e["client"]}
    assert inputs == {"auto", "key", "drag", "minimap", "minimap-key", "button", "click"}
    assert all(e["client"]["ui"] == "web" for e in events if e["client"])
    assert SECRET not in log.read_text(encoding="ascii")

    manifest, slide_dir = slide_setup
    registry = SlideRegistry.from_manifest(manifest, slide_dir)
    try:
        result = replay(log, registry)
    finally:
        registry.close()
    assert result.ok, result.mismatches
    assert result.images_checked >= 7
    assert problems == []
    page.close()


def test_small_window_shrinks_the_view(server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.goto(server + "/")
    expect(page.locator("#slide-select option")).to_have_count(1)
    page.fill("#alias", "small")
    page.click("#start-button")
    expect(page.locator("#reader")).to_be_visible()
    w, css_w = page.eval_on_selector("#view-canvas", "c => [c.width, c.clientWidth]")
    assert w == 1280
    assert css_w < 900
    page.close()


def test_bad_alias_is_refused_in_the_form(server: str, browser: Any) -> None:
    page = browser.new_page()
    page.goto(server + "/")
    expect(page.locator("#slide-select option")).to_have_count(1)
    page.fill("#alias", "has space")
    page.click("#start-button")
    expect(page.locator("#start")).to_be_visible()
    assert page.locator("#alias").evaluate("el => el.validity.patternMismatch")
    page.close()


def _start(page: Any, server: str, alias: str) -> None:
    page.goto(server + "/")
    expect(page.locator("#slide-select option")).to_have_count(1)
    page.fill("#alias", alias)
    page.click("#start-button")
    expect(page.locator("#negative-scope option")).to_have_count(3)
    _idle(page)


def _log(server: str, page: Any) -> list[dict[str, Any]]:
    href = page.locator("#download").get_attribute("href")
    text = page.request.get(server + href).text()
    return [json.loads(line) for line in text.splitlines()[1:]]


def test_review_regressions_in_the_browser(server: str, browser: Any) -> None:
    """The UI review's scenarios (M3): views and marks stay in step with the session."""
    page = browser.new_page(viewport={"width": 1700, "height": 1150})
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    _start(page, server, "review")
    overlay = page.locator("#overlay-canvas")
    info = page.locator("#view-info")
    _changes(page, lambda: page.keyboard.press("4"))

    # A label form left open when the view changes does not save on the new view.
    page.keyboard.press("p")
    overlay.click(position={"x": 100, "y": 100})
    expect(page.locator("#label-form")).to_be_visible()
    _changes(page, lambda: page.locator("#mag-buttons button[data-mag='20']").click())
    expect(page.locator("#label-form")).to_be_hidden()
    expect(page.locator("#message")).to_contain_text("not saved")

    # The tool button hands the keyboard to the view; right clicks place nothing.
    page.locator("#tool-buttons button[data-tool='measure']").click()
    page.keyboard.press("Space")
    page.keyboard.press("Shift+ArrowRight")
    page.keyboard.press("Space")
    expect(page.locator("#message")).to_contain_text("Measured 25 µm")  # 50 view px at 0.5 um/px
    overlay.click(position={"x": 50, "y": 50}, button="right")
    overlay.click(position={"x": 90, "y": 50}, button="right")

    # Keys pressed while Help is open do nothing to the view.
    before = info.inner_text()
    page.click("#help-button")
    for key in ["ArrowDown", "4", "s"]:
        page.keyboard.press(key)
    page.keyboard.press("Escape")
    expect(page.locator("#help")).to_be_hidden()
    assert info.inner_text() == before

    # A view whose image cannot be loaded is not used for measuring; R loads it again.
    blocked = "**/views/*.jpg"
    page.route(blocked, lambda route: route.abort())
    page.locator("#tool-buttons button[data-tool='navigate']").click()
    overlay.focus()
    page.keyboard.press("d")
    expect(page.locator("#message")).to_contain_text("could not be shown", timeout=10000)
    _idle(page)
    page.keyboard.press("m")
    overlay.click(position={"x": 100, "y": 100})
    overlay.click(position={"x": 300, "y": 100})
    page.unroute(blocked)
    page.keyboard.press("r")
    _idle(page)
    expect(page.locator("#message")).to_be_hidden()

    # Enter in a statement does not submit the report; sizes take any precision.
    page.click("#add-finding")
    statement = page.locator("#claims li input").first
    statement.fill("tumour cells")
    statement.press("Enter")
    page.fill("#r-largest", "0.205")
    page.locator("#r-largest").press("Enter")
    expect(page.locator("#receipt")).to_be_hidden()
    page.select_option("#r-metastasis", "present")
    page.select_option("#r-category", "micro")
    page.select_option("#r-ene", "absent")
    page.click("#submit-report")
    expect(page.locator("#receipt")).to_contain_text("Report accepted")

    # Switching language after the report keeps the session's controls off.
    page.click("#lang-switch")
    expect(page.locator("#receipt")).to_contain_text("报告已接收")
    assert page.locator("#mag-buttons button:enabled").count() == 0
    assert page.locator("#claims button:enabled").count() == 0

    events = _log(server, page)
    actions = [e["action"] for e in events]
    assert actions.count("measure") == 1  # the measurement on the unseen view was refused
    assert "annotate" not in actions  # the label form on the old view was dropped
    report = next(e for e in events if e["action"] == "submit_report")
    assert report["params"]["report"]["conclusion"]["largest_deposit_mm"] == 0.205
    clients = [e["client"] for e in events if e["client"]]
    assert all(c["ui_version"].startswith("0.") for c in clients)
    assert problems == []
    page.close()
