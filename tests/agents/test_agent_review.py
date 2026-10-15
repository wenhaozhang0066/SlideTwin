"""Regressions from the M4 review: what the serpentine and LLM agents claim must match what they
saw, and a refused action must not cost the reading."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from slidetwin.agents import llm_agent_example as llm
from slidetwin.agents import serpentine_agent, unconstrained_crop_agent
from slidetwin.agents.client import Env
from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.report import export_schema
from tests.conftest import CONFIG_DIR
from tests.helpers.live import serve
from tests.helpers.synth import PINK, PURPLE, write_tissue_slide

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _live(slide_dir: Path, config_dir: Path = CONFIG_DIR) -> Any:
    settings = Settings(
        config_dir=config_dir,
        manifest=slide_dir / "manifest.yaml",
        slide_dir=slide_dir,
        log_dir=slide_dir / "logs",
        cache_dir=None,
    )
    return serve(create_app(settings))


def _report(trajectory: str) -> dict[str, Any]:
    for line in trajectory.splitlines()[1:]:
        e = json.loads(line)
        if e["action"] == "submit_report" and not e["error"]:
            return dict(e["params"]["report"])
    raise AssertionError("no accepted report")


@pytest.fixture(scope="module")
def nested(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A U-shaped pink piece whose box encloses a separate purple piece."""
    d = tmp_path_factory.mktemp("nested")
    u = [(200, 200, 1600, 200, PINK), (200, 400, 200, 1200, PINK), (1600, 400, 200, 1200, PINK)]
    write_tissue_slide(d, pieces=[*u, (800, 700, 400, 400, PURPLE)])
    return d


async def test_a_flag_inside_a_nested_piece_clears_neither_piece(nested: Path) -> None:
    with _live(nested) as url:
        async with Env.rest(url) as env:
            result = await serpentine_agent.run(env, "syn_tissue", viewport_px=[512, 384])
    claims = _report(result.trajectory)["claims"]
    assert not [c for c in claims if c["type"] == "negative"]  # both boxes hold the flag
    assert result.receipt is not None
    assert result.receipt["violations"] == 0


async def test_the_check_view_shows_a_flag_at_the_slide_edge(tmp_path: Path) -> None:
    # Purple tissue in the top-right corner: at 10x the view centre is kept away from that edge,
    # so the 20x look must be aimed after switching magnification.
    write_tissue_slide(
        tmp_path, pieces=[(300, 300, 1200, 1200, PINK), (3650, 190, 380, 450, PURPLE)]
    )
    with _live(tmp_path) as url:
        async with Env.rest(url) as env:
            result = await serpentine_agent.run(env, "syn_tissue")
    events = [json.loads(line) for line in result.trajectory.splitlines()[1:]]
    views = {e["view_id"]: e["observation"]["bbox_um"] for e in events if e["view_id"]}
    findings = [c for c in _report(result.trajectory)["claims"] if c["type"] == "finding"]
    assert findings
    purple = (1825.0, 95.0, 2015.0, 320.0)  # um, at 0.5 um/px
    for f in findings:
        x0, y0, x1, y1 = views[f["evidence"][0]]
        assert x0 < purple[2]
        assert purple[0] < x1
        assert y0 < purple[3]
        assert purple[1] < y1


def _budget_configs(tmp_path: Path, profile: str, views: int) -> Path:
    target = tmp_path / "cfg"
    shutil.copytree(CONFIG_DIR, target)
    path = target / "profiles" / f"{profile}.yaml"
    data = yaml.safe_load(path.read_text())
    data["budget"] = {"max_views": views}
    path.write_text(yaml.safe_dump(data))
    return target


async def test_a_refused_action_still_ends_in_a_report(tmp_path: Path) -> None:
    slides = tmp_path / "slides"
    slides.mkdir()
    write_tissue_slide(slides)
    with _live(slides, _budget_configs(tmp_path, "viewer", 4)) as url:
        async with Env.rest(url) as env:
            serp = await serpentine_agent.run(env, "syn_tissue", viewport_px=[512, 384])
    assert serp.receipt is not None
    assert any("scan stopped" in n and "BUDGET_EXHAUSTED" in n for n in serp.notes)
    report = _report(serp.trajectory)
    assert report["conclusion"]["metastasis"] == "indeterminate"  # an incomplete scan
    assert not [c for c in report["claims"] if c["type"] == "negative"]
    with _live(slides, _budget_configs(tmp_path / "b", "unconstrained", 2)) as url:
        async with Env.rest(url) as env:
            crop = await unconstrained_crop_agent.run(env, "syn_tissue")
    assert crop.receipt is not None
    assert any("BUDGET_EXHAUSTED" in n for n in crop.notes)


async def test_reports_stay_within_the_claim_limit(tmp_path: Path) -> None:
    # 15 x 14 separate squares (150 um, 190 um apart: wider than the tissue mask's closing).
    squares = [(30 + 170 * i, 30 + 170 * j, 75, 75, PINK) for i in range(15) for j in range(14)]
    write_tissue_slide(tmp_path, size_px=(2580, 2410), mpp=2.0, pieces=squares)
    with _live(tmp_path) as url:
        async with Env.rest(url) as env:
            result = await serpentine_agent.run(env, "syn_tissue", viewport_px=[512, 384])
    assert result.receipt is not None  # 210 pieces, 200 claims at most
    assert len(_report(result.trajectory)["claims"]) == 200
    assert any("report limit" in n for n in result.notes)


def test_llm_tools_follow_the_profile_and_the_report_schema_resolves() -> None:
    schema = export_schema()
    viewer = llm.tools(schema)
    assert [t["name"] for t in viewer] == [
        "get_thumbnail",
        "set_magnification",
        "pan",
        "move_to",
        "measure",
        "submit_report",
    ]
    assert "crop" in [t["name"] for t in llm.tools(schema, crop=True)]
    assert "get_thumbnail" not in [t["name"] for t in llm.tools(schema, thumbnail=False)]
    report_tool = viewer[-1]["input_schema"]
    assert "$schema" not in json.dumps(report_tool["properties"])
    valid = {
        "report": {
            "task": "sln_metastasis",
            "conclusion": {
                "metastasis": "absent",
                "category": "none",
                "largest_deposit_mm": None,
                "extranodal_extension": "not_assessable",
            },
            "claims": [{"id": "n1", "type": "negative", "text": "none seen"}],
        }
    }
    Draft202012Validator(report_tool).validate(valid)  # the $refs resolve
    assert list(Draft202012Validator(report_tool).iter_errors({"report": {"task": 1}}))


async def test_llm_malformed_arguments_are_tool_errors(tmp_path: Path) -> None:
    write_tissue_slide(tmp_path)
    with _live(tmp_path) as url:
        async with Env.rest(url) as env:
            s = await env.open("syn_tissue", actor="agent:x")
            for args in [{"p1_px": None, "p2_px": [1, 1]}, {"p1_px": 3, "p2_px": [1, 1]}]:
                out = await llm.execute(s, llm.ToolCall("t", "measure", args))
                assert out.is_error
                assert "Invalid arguments" in json.loads(out.text)["error"]
            crop = await llm.execute(
                s, llm.ToolCall("c", "crop", {"x_um": 0, "y_um": 0, "w_um": 9, "h_um": 9})
            )
            assert json.loads(crop.text)["error"]["code"] == "ACTION_NOT_ALLOWED"


async def test_mcp_refusals_at_the_http_layer_are_env_errors(tmp_path: Path) -> None:
    from slidetwin.agents.client import EnvError

    write_tissue_slide(tmp_path)

    def app(**kw: Any) -> Any:
        settings = Settings(
            config_dir=CONFIG_DIR,
            manifest=tmp_path / "manifest.yaml",
            slide_dir=tmp_path,
            log_dir=tmp_path / "logs",
            cache_dir=None,
            **kw,
        )
        return create_app(settings)

    async def exhaust(url: str) -> None:
        async with Env.mcp(f"{url}/mcp") as env:
            for _ in range(20):
                await env.list_slides()

    async def connect(url: str) -> None:
        async with Env.mcp(f"{url}/mcp"):
            pass

    limited = app(rate_limit_per_s=0.001, rate_limit_burst=8)
    with serve(limited) as url, pytest.raises(EnvError) as info:
        await exhaust(url)
    assert info.value.code == "MCP_TRANSPORT_ERROR"
    with serve(app(allowed_hosts=("only.example",))) as url, pytest.raises(EnvError) as info:
        await connect(url)
    assert info.value.code == "MCP_CONNECT_FAILED"
    assert "SLIDETWIN_ALLOWED_HOSTS" in info.value.hint
