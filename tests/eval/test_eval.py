"""Evaluation tools (M6): annotations, lesion metrics, pictures, the profile comparison and the CLI.

The synthetic slide is native 20x, 2048 x 1536 um, with a pink and a purple piece; the "lesion" is
annotated exactly over the purple piece (um 1250-1650 x 950-1300).
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from slidetwin.core.geometry import BBox
from slidetwin.core.trajectory import load_trajectory
from slidetwin.eval.__main__ import main
from slidetwin.eval.annotations import (
    Region,
    build_lesions,
    convex_hull,
    feret_diameter,
    from_geojson,
    parse_asap,
    to_geojson,
)
from slidetwin.eval.compare import Paths, markdown, run_matrix
from slidetwin.eval.metrics import evaluate, load_truth
from tests.conftest import CONFIG_DIR, REPO_ROOT
from tests.helpers.synth import write_tissue_slide


def asap(polygons: list[tuple[str, list[tuple[float, float]]]]) -> str:
    anns = []
    for i, (group, pts) in enumerate(polygons):
        coords = "".join(
            f'<Coordinate Order="{k}" X="{x}" Y="{y}" />' for k, (x, y) in enumerate(pts)
        )
        anns.append(
            f'<Annotation Name="A{i}" Type="Polygon" PartOfGroup="{group}" Color="#F4FA58">'
            f"<Coordinates>{coords}</Coordinates></Annotation>"
        )
    body = "".join(anns)
    return f"<ASAP_Annotations><Annotations>{body}</Annotations></ASAP_Annotations>"


def square(x: float, y: float, side: float) -> list[tuple[float, float]]:
    return [(x, y), (x + side, y), (x + side, y + side), (x, y + side)]


LESION_XML = asap([("Tumor", [(2500, 1900), (3300, 1900), (3300, 2600), (2500, 2600)])])


@pytest.fixture(scope="module")
def study(tmp_path_factory: pytest.TempPathFactory) -> Paths:
    d = tmp_path_factory.mktemp("eval_slides")
    manifest = write_tissue_slide(
        d, eval_only={"label": "tumor", "category": "micro", "annotation": "CAMELYON16/syn.xml"}
    )
    (d / "xml").mkdir()
    (d / "xml" / "syn.xml").write_text(LESION_XML)
    return Paths(CONFIG_DIR, manifest, d, None)


# --- annotations -------------------------------------------------------------------------------


def test_asap_polygons_become_micrometres() -> None:
    xml = asap(
        [
            ("Tumor", square(100, 100, 50)),
            ("_2", square(110, 110, 10)),
            ("_1", square(400, 400, 20)),
            ("Other", square(0, 0, 5)),
            ("Tumor", [(0, 0), (1, 1)]),
        ]
    )
    regions = parse_asap(xml, (0.25, 0.5))
    assert [r.group for r in regions] == ["tumor", "exclusion", "tumor"]
    assert regions[0].bbox.to_list() == [25.0, 50.0, 37.5, 75.0]
    data = to_geojson(regions, slide_id="s", source="x.xml")
    assert data["properties"]["units"] == "um"
    ring = data["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]  # closed
    back = from_geojson(json.loads(json.dumps(data)))
    assert np.allclose(back[0].ring, regions[0].ring)
    assert back[1].group == "exclusion"


def test_feret_diameter_and_hull() -> None:
    pts = np.array([[0, 0], [3, 0], [3, 4], [0, 4], [1, 1], [2, 2]], dtype=float)
    assert len(convex_hull(pts)) == 4
    assert feret_diameter(pts) == pytest.approx(5.0)
    assert feret_diameter(np.array([[1.0, 1.0]])) == 0.0
    assert len(convex_hull(np.array([[0.0, 0.0], [1.0, 1.0]]))) == 2


def test_touching_polygons_merge_into_one_deposit() -> None:
    a = Region("tumor", np.array(square(0, 0, 100), dtype=float))
    b = Region("tumor", np.array(square(100, 0, 100), dtype=float))  # shares an edge with a
    far = Region("tumor", np.array(square(1000, 1000, 50), dtype=float))
    hole = Region("exclusion", np.array(square(20, 20, 40), dtype=float))
    lesions = build_lesions([a, b, far, hole], grid_um=4.0)
    big, small = lesions.deposits
    # The exclusion lies on this deposit, so its size comes from the grid (within one cell).
    assert big.largest_dimension_mm == pytest.approx(math.hypot(200, 100) / 1000, abs=0.004)
    assert big.area_mm2 == pytest.approx(0.02 - 0.0016, rel=0.01)  # minus the exclusion
    assert small.largest_dimension_mm == pytest.approx(math.hypot(50, 50) / 1000, abs=1e-6)
    assert lesions.largest_mm == big.largest_dimension_mm
    assert lesions.hits(BBox(150, 50, 160, 60))
    assert not lesions.hits(BBox(30, 30, 50, 50))  # inside the exclusion
    assert not lesions.hits(BBox(500, 500, 600, 600))
    assert build_lesions([hole]).deposits == ()


def test_cells_follow_their_centres_on_every_side() -> None:
    # 12.8-35.2 um on a 4 um grid is cells 3.2-8.8 (plus the 8 um margin of the raster):
    # exactly the cells whose centres are inside, 3-8 in both directions.
    sq = Region("tumor", np.array(square(12.8 + 1000, 12.8 + 1000, 22.4), dtype=float))
    anchor = Region("tumor", np.array(square(1000, 1000, 1), dtype=float))  # fixes the origin
    lesions = build_lesions([anchor, sq], grid_um=4.0)
    rows, cols = np.nonzero(lesions.mask)
    x0, y0 = lesions.origin_um
    assert (cols.min() * 4 + x0, cols.max() * 4 + x0) == (1012.0, 1032.0)  # cells 3..8
    assert (rows.min() * 4 + y0, rows.max() * 4 + y0) == (1012.0, 1032.0)
    assert lesions.area_cells == 36


def test_an_exclusion_can_split_a_deposit_and_tiny_polygons_count() -> None:
    bar = Region("tumor", np.array([(0, 0), (300, 0), (300, 100), (0, 100)], dtype=float))
    cut = Region(
        "exclusion", np.array([(140, -10), (160, -10), (160, 110), (140, 110)], dtype=float)
    )
    speck = Region("tumor", np.array([(1001, 1001), (1003, 1001), (1002, 1003)], dtype=float))
    lesions = build_lesions([bar, cut, speck], grid_um=4.0)
    left, right, tiny = lesions.deposits
    for part in (left, right):
        assert part.largest_dimension_mm == pytest.approx(math.hypot(140, 100) / 1000, abs=0.006)
    assert tiny.area_mm2 == pytest.approx(2e-6)  # a 2 um2 triangle, smaller than one cell
    assert tiny.largest_dimension_mm == pytest.approx(math.hypot(1, 2) / 1000, abs=1e-6)


# --- metrics on a real reading -------------------------------------------------------------------


def test_metrics_of_a_serpentine_reading(study: Paths, tmp_path: Path) -> None:
    out = tmp_path / "run"
    asyncio.run(run_matrix(study, out, ["syn_tissue"], ["serpentine"]))
    header, events = load_trajectory(out / "trajectories" / "syn_tissue__serpentine__viewer.jsonl")
    regions = parse_asap(LESION_XML, (0.5, 0.5))
    lesions = build_lesions(regions)
    assert lesions.largest_mm == pytest.approx(math.hypot(400, 350) / 1000, abs=1e-6)
    m = evaluate(header, events, load_truth(study.manifest)["syn_tissue"], lesions)
    assert m.actor == "agent:serpentine"
    assert m.profile == "viewer"
    assert m.coverage == pytest.approx(1.0)
    assert m.violations == 0
    assert m.metastasis == "indeterminate"
    assert m.conclusion_correct is False
    assert m.category_correct is False  # "none" against "micro"
    assert m.lesion_seen_screen is True
    assert m.lesion_seen_confirm is True
    assert m.lesion_share_screen == pytest.approx(1.0)
    assert m.time_to_lesion_s is not None
    assert m.findings >= 1
    assert m.findings_on_lesion == m.findings  # the purple piece is the lesion
    assert m.reported_size_mm is not None
    assert m.size_error_mm == pytest.approx(abs(m.reported_size_mm - lesions.largest_mm), abs=1e-3)
    # Without ground truth only the process metrics remain.
    bare = evaluate(header, events)
    assert bare.conclusion_correct is None
    assert bare.lesion_seen_screen is None
    assert bare.size_error_mm is None


# --- comparison, pictures, report and command line -------------------------------------------


def test_convert_skips_slides_that_are_not_there(
    study: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load(study.manifest.read_text())
    ghost = {
        "id": "ghost",
        "path": "ghost.tif",
        "eval_only": {"label": "tumor", "annotation": "g.xml"},
    }
    data["slides"].append(ghost)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(data))
    annotations = tmp_path / "annotations"
    args = ["--manifest", str(manifest), "--slide-dir", str(study.slide_dir)]
    main(
        [
            "convert",
            *args,
            "--xml-dir",
            str(study.slide_dir / "xml"),
            "--annotations",
            str(annotations),
        ]
    )
    assert sorted(p.name for p in annotations.iterdir()) == ["syn_tissue.geojson"]
    assert "skipped ghost: its slide is not in the slide folder" in capsys.readouterr().err


def test_profile_comparison_report_and_pictures(study: Paths, tmp_path: Path) -> None:
    out = tmp_path / "study"
    annotations = tmp_path / "annotations"
    main(
        [
            "convert",
            "--config-dir", str(study.config_dir),
            "--manifest", str(study.manifest),
            "--slide-dir", str(study.slide_dir),
            "--xml-dir", str(study.slide_dir / "xml"),
            "--annotations", str(annotations),
        ]
    )  # fmt: skip
    assert json.loads((annotations / "syn_tissue.geojson").read_text())["features"]
    common = [
        "--config-dir", str(study.config_dir),
        "--manifest", str(study.manifest),
        "--slide-dir", str(study.slide_dir),
        "--out", str(out),
    ]  # fmt: skip
    main(["run", *common, "--slides", "syn_tissue", "--agents", "serpentine,random,crop"])
    names = sorted(p.name for p in (out / "trajectories").iterdir())
    assert names == [
        "syn_tissue__crop__unconstrained.jsonl",
        "syn_tissue__random__unconstrained.jsonl",
        "syn_tissue__random__viewer.jsonl",
        "syn_tissue__serpentine__unconstrained.jsonl",
        "syn_tissue__serpentine__viewer.jsonl",
    ]
    main(["evaluate", *common, "--annotations", str(annotations)])
    metrics = json.loads((out / "metrics.json").read_text())
    by = {(m["actor"], m["profile"]): m for m in metrics}
    serp_viewer = by["agent:serpentine", "viewer"]
    serp_flat = by["agent:serpentine", "unconstrained"]
    assert serp_viewer["views"] == serp_flat["views"]  # same plan ...
    assert serp_viewer["virtual_s"] != serp_flat["virtual_s"]  # ... different clocks
    report = (out / "report.md").read_text()
    assert "| syn_tissue | serpentine | viewer |" in report
    assert "| serpentine | viewer | 1 |" in report  # the summary row
    assert "| crop | unconstrained | 1 |" in report
    figures = out / "figures"
    with Image.open(figures / "syn_tissue__serpentine__viewer__path.png") as img:
        assert img.size == (1024, 768)
    with Image.open(figures / "syn_tissue__crop__unconstrained__coverage.png") as img:
        assert img.size == (1024, 768)
    with Image.open(figures / "syn_tissue__serpentine__viewer__replay.gif") as gif:
        assert gif.n_frames >= 2
    assert not (figures / "syn_tissue__random__viewer__replay.gif").exists()
    assert markdown([]).count("\n") == 2


def test_cli_requires_its_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in [["convert"], ["run"], ["run", "--out", "x"], ["evaluate"]]:
        with pytest.raises(SystemExit):
            main(argv)
    assert "needs" in capsys.readouterr().err


# --- isolation ---------------------------------------------------------------------------------


def _imports_eval(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[:2] == ["slidetwin", "eval"] for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and module.split(".")[:2] == ["slidetwin", "eval"]:
                return True
            if (
                node.level == 0
                and module == "slidetwin"
                and any(a.name == "eval" for a in node.names)
            ):
                return True
            if node.level > 0 and (
                module.split(".")[0] == "eval" or any(a.name == "eval" for a in node.names)
            ):
                return True
    return False


def test_nothing_that_serves_sessions_imports_the_evaluation_code() -> None:
    for text in [
        "import slidetwin.eval.metrics",
        "from slidetwin.eval import metrics",
        "from slidetwin import eval",
        "from ..eval import metrics",
        "from .. import eval",
    ]:
        assert _imports_eval(ast.parse(text)), text
    assert not _imports_eval(ast.parse("from slidetwin.core import evaluation"))
    package = REPO_ROOT / "slidetwin"
    served = [package / "serve.py", package / "fetch_slides.py"]
    for sub in ["core", "api", "mcp_server", "agents", "web"]:
        served += list((package / sub).rglob("*.py"))
    for path in served:
        assert not _imports_eval(ast.parse(path.read_text(encoding="utf-8"))), path


def _reading(
    confirm_min: float, report: dict[str, object]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    header = {
        "slide": {"id": "s"},
        "actor": "agent:x",
        "profile": {"id": "viewer"},
        "protocol": {
            "content": {
                "coverage": {"at_or_above_mag": 10},
                "magnification": {"confirm_min": confirm_min},
            }
        },
    }
    view = {"type": "view", "view_id": "v_0002", "mag": 20.0, "bbox_um": [0, 0, 100, 100]}
    events = [
        {"seq": 1, "action": "create_session", "t_virtual_s": 0.3, "observation": None},
        {"seq": 2, "action": "set_magnification", "t_virtual_s": 1.1, "observation": view},
        {
            "seq": 3,
            "action": "measure",
            "t_virtual_s": 4.1,
            "observation": {"type": "measurement", "measurement_id": "m_0001", "length_um": 300.0},
        },
        {
            "seq": 4,
            "action": "submit_report",
            "t_virtual_s": 4.1,
            "params": {"report": report},
            "observation": {"coverage": {"fraction": 0.5}, "violations": 0},
            "error": None,
        },
    ]
    return header, events


def test_metrics_follow_the_protocol_and_the_stated_size() -> None:
    lesion = build_lesions(
        [Region("tumor", np.array(square(10, 10, 50), dtype=float))], grid_um=4.0
    )
    claims = [{"id": "m1", "type": "measurement", "text": "x", "measurement_id": "m_0001"}]
    report = {
        "conclusion": {"metastasis": "present", "category": "micro", "largest_deposit_mm": 0.07},
        "claims": claims,
    }
    m = evaluate(*_reading(20, report), {"label": "tumor", "category": "micro"}, lesion)
    assert m.lesion_seen_confirm is True
    assert m.reported_size_mm == 0.07  # the stated size wins over the cited 0.3 mm measurement
    stricter = evaluate(*_reading(40, report), {"label": "tumor"}, lesion)
    assert stricter.lesion_seen_confirm is False  # a 20x view is not enough under confirm_min 40
    unstated = {**report, "conclusion": {**report["conclusion"], "largest_deposit_mm": None}}
    assert evaluate(*_reading(20, unstated), {"label": "tumor"}, lesion).reported_size_mm == 0.3
    negative = {"conclusion": {"metastasis": "absent", "category": "none"}, "claims": []}
    n = evaluate(*_reading(20, negative), {"label": "normal", "category": "negative"})
    assert (n.conclusion_correct, n.category_correct, n.truth_category) == (True, True, "none")
