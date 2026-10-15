"""MCP adapter (M4): one tool per action, same payloads and logs as REST, errors as envelopes."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.types import CallToolResult

from slidetwin.api.main import create_app
from slidetwin.api.settings import Settings
from slidetwin.core.manager import SessionManager
from slidetwin.core.replay import replay
from slidetwin.core.report import export_schema
from slidetwin.mcp_server import build_mcp
from tests.conftest import CONFIG_DIR
from tests.helpers.live import serve
from tests.mcp.conftest import SECRET

pytestmark = pytest.mark.anyio

TOOLS = {
    "list_slides",
    "list_profiles",
    "list_protocols",
    "get_report_schema",
    "create_session",
    "get_state",
    "get_thumbnail",
    "set_magnification",
    "pan",
    "move_to",
    "view",
    "measure",
    "annotate",
    "crop",
    "submit_report",
    "export_trajectory",
}

REPORT = {
    "task": "sln_metastasis",
    "conclusion": {
        "metastasis": "absent",
        "category": "none",
        "largest_deposit_mm": None,
        "extranodal_extension": "not_assessable",
    },
    "claims": [{"id": "n1", "type": "negative", "text": "no deposits", "scope": "tissue_piece:2"}],
    "free_text": "",
}


def _jpeg(result: CallToolResult) -> bytes:
    images = [b for b in result.content if b.type == "image"]
    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"
    data = base64.b64decode(images[0].data)
    assert data[:3] == b"\xff\xd8\xff"
    return data


def _data(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.content[0].type == "text"
    data = json.loads(result.content[0].text)
    assert data == result.structured_content
    out: dict[str, Any] = data
    return out


def _err(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error
    body = json.loads(result.content[0].text)
    assert body == result.structured_content
    err: dict[str, Any] = body["error"]
    return err


async def test_tools_mirror_the_session_actions(manager: SessionManager) -> None:
    async with Client(build_mcp(manager)) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == TOOLS
        assert all(t.description for t in tools)
        pan = next(t for t in tools if t.name == "pan").input_schema
        assert pan["required"] == ["session_id", "dx", "dy"]
        assert client.server_info is not None
        assert "not for clinical use" in (client.instructions or "")


async def test_a_whole_session_through_mcp(manager: SessionManager) -> None:
    async with Client(build_mcp(manager)) as c:
        slides = _data(await c.call_tool("list_slides", {}))["slides"]
        assert slides == [{"id": "syn_tissue", "license": "CC0-1.0", "attribution": None}]
        assert _data(await c.call_tool("get_report_schema", {})) == export_schema()
        assert {p["id"] for p in _data(await c.call_tool("list_profiles", {}))["profiles"]} == {
            "viewer",
            "unconstrained",
        }
        assert _data(await c.call_tool("list_protocols", {}))["protocols"][0]["protocol_id"]

        r = await c.call_tool(
            "create_session",
            {"slide_id": "syn_tissue", "actor": "agent:mcp-test", "viewport_px": [512, 384]},
        )
        opened = _data(r)
        _jpeg(r)
        sid = opened["session_id"]
        assert opened["slide"]["available_mags"][-1] == 20
        assert opened["task"]["protocol_id"] == "sln_metastasis"
        assert opened["rules"]["id"] == "viewer"
        assert opened["observation"]["image"]["base64"] is None  # the image is the image block

        r = await c.call_tool("get_thumbnail", {"session_id": sid})
        thumb = _data(r)["observation"]
        _jpeg(r)
        assert [p["piece"] for p in thumb["tissue_pieces"]] == [1, 2]

        piece2 = thumb["tissue_pieces"][1]["bbox_um"]
        centre = {"x_um": (piece2[0] + piece2[2]) / 2, "y_um": (piece2[1] + piece2[3]) / 2}
        _jpeg(await c.call_tool("set_magnification", {"session_id": sid, "mag": 10}))
        _jpeg(await c.call_tool("move_to", {"session_id": sid, **centre}))
        _jpeg(await c.call_tool("pan", {"session_id": sid, "dx": 0.25, "dy": 0}))
        _jpeg(await c.call_tool("pan", {"session_id": sid, "dx": -0.5, "dy": 0}))
        _jpeg(await c.call_tool("view", {"session_id": sid}))
        m = _data(
            await c.call_tool("measure", {"session_id": sid, "p1_px": [0, 0], "p2_px": [480, 360]})
        )
        assert m["observation"]["length_um"] == pytest.approx(600.0)  # 600 px at 1 um/px
        a = _data(
            await c.call_tool(
                "annotate",
                {
                    "session_id": sid,
                    "geometry": {"type": "point", "coordinates": [10, 20]},
                    "label": "spot",
                },
            )
        )
        assert a["observation"]["label"] == "spot"
        state = _data(await c.call_tool("get_state", {"session_id": sid}))["state"]
        assert state["views_served"] == 6

        receipt = _data(await c.call_tool("submit_report", {"session_id": sid, "report": REPORT}))
        e4 = {
            ch.get("claim_id"): ch["status"]
            for ch in receipt["observation"]["rule_checks"]
            if ch["rule"] == "E4"
        }
        assert e4 == {None: "fail", "n1": "pass"}  # piece 2 was seen in full; the slide was not
        assert receipt["observation"]["accepted"] is True  # advisory protocol
        log = (await c.call_tool("export_trajectory", {"session_id": sid})).content[0].text

    assert SECRET not in log
    events = [json.loads(line) for line in log.splitlines()[1:]]
    unmarked = {"create_session", "export_trajectory"}  # opened and exported by the manager
    assert all(e["client"] == {"via": "mcp"} for e in events if e["action"] not in unmarked)
    report = replay(log, manager.registry)
    assert report.ok, report.mismatches
    assert report.images_checked == 7


async def test_failures_are_tool_errors_with_the_rest_envelope(manager: SessionManager) -> None:
    async with Client(build_mcp(manager)) as c:
        err = _err(await c.call_tool("create_session", {"slide_id": "nope", "actor": "agent:x"}))
        assert err["code"] == "SLIDE_NOT_FOUND"
        err = _err(await c.call_tool("create_session", {"slide_id": "syn_tissue", "actor": "x"}))
        assert err["code"] == "INVALID_PARAMETER"
        assert "agent:" in err["message"]
        sid = _data(
            await c.call_tool("create_session", {"slide_id": "syn_tissue", "actor": "agent:e"})
        )["session_id"]
        err = _err(await c.call_tool("set_magnification", {"session_id": sid, "mag": 40}))
        assert err["code"] == "MAG_UNAVAILABLE"
        assert err["seq"] == 2
        assert err["cost_s"] == 0
        assert "20x" in err["hint"]
        err = _err(
            await c.call_tool(
                "crop", {"session_id": sid, "x_um": 0, "y_um": 0, "w_um": 9, "h_um": 9}
            )
        )
        assert err["code"] == "ACTION_NOT_ALLOWED"
        err = _err(await c.call_tool("get_state", {"session_id": "0" * 32}))
        assert err["code"] == "SESSION_NOT_FOUND"
        bad = {"session_id": sid, "report": {**REPORT, "task": "other"}}
        assert _err(await c.call_tool("submit_report", bad))["code"] == "REPORT_INVALID"
        # Argument types are checked against the tool schema, before the session (not logged).
        for args in [{"session_id": sid, "mag": "10"}, {"session_id": sid, "mag": True}]:
            err = _err(await c.call_tool("set_magnification", args))
            assert err["code"] == "INVALID_PARAMETER"
            assert "mag" in err["message"]
        log = (await c.call_tool("export_trajectory", {"session_id": sid})).content[0].text
    assert SECRET not in log
    assert [json.loads(line)["action"] for line in log.splitlines()[1:]] == [
        "create_session",
        "set_magnification",
        "crop",
        "submit_report",
        "export_trajectory",
    ]


async def test_rest_and_mcp_sessions_log_the_same_events(
    manager: SessionManager, tissue_manifest: Path, tmp_path: Path
) -> None:
    steps: list[tuple[str, str, dict[str, Any]]] = [
        ("thumbnail", "get_thumbnail", {}),
        ("magnification", "set_magnification", {"mag": 5}),
        ("pan", "pan", {"dx": 0.5, "dy": -0.25}),
        ("move_to", "move_to", {"x_um": 600.5, "y_um": 700}),
        ("magnification", "set_magnification", {"mag": 20}),
        ("measure", "measure", {"p1_px": [1, 2], "p2_px": [100.5, 2]}),
        ("magnification", "set_magnification", {"mag": 40}),  # fails the same way
        ("report", "submit_report", {"report": REPORT}),
    ]
    opening = {"slide_id": "syn_tissue", "actor": "agent:twin", "viewport_px": [640, 480]}
    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=tissue_manifest,
        slide_dir=tissue_manifest.parent,
        log_dir=tmp_path / "logs",
        cache_dir=None,
        allowed_hosts=("testserver",),
    )
    rest_out: list[dict[str, Any]] = []
    mcp_out: list[dict[str, Any]] = []
    with TestClient(create_app(settings, manager=manager)) as rest:
        first = rest.post("/api/sessions?include_image=false", json=opening).json()
        rid = first["session_id"]
        for route, _, body in steps:
            r = rest.post(f"/api/sessions/{rid}/{route}?include_image=false", json=body).json()
            rest_out.append(r.get("error") or r)
        async with Client(build_mcp(manager)) as c:
            opened = _data(await c.call_tool("create_session", opening))
            mid = opened["session_id"]
            for _, tool, body in steps:
                r = await c.call_tool(tool, {"session_id": mid, **body})
                mcp_out.append(r.structured_content.get("error") or r.structured_content)
        logs = [[json.loads(x) for x in manager.trajectory(sid).splitlines()] for sid in (rid, mid)]

    def same(a: Any, b: Any) -> Any:
        """The two sessions differ only in their ids (and the image URLs that contain them)."""
        text = json.dumps(b).replace(mid, rid)
        return json.loads(text) == a

    assert same(first, opened)  # every field, null values included (claim_id, base64, ...)
    for a, b in zip(rest_out, mcp_out, strict=True):
        assert same(a, b), (a, b)

    rest_log, mcp_log = logs
    assert len(rest_log) == len(mcp_log) == len(steps) + 3  # header, create, steps, export
    for a, b in zip(rest_log[1:-1], mcp_log[1:-1], strict=True):
        for key in ["action", "params", "cost_s", "t_virtual_s", "state_after", "view_id", "error"]:
            assert a[key] == b[key], key
        assert a["observation"] == b["observation"]  # same pixels, ids and receipts
    assert rest_log[0]["actor"] == mcp_log[0]["actor"]


def test_every_route_answers_only_allowed_hosts_and_origins(
    manager: SessionManager, tissue_manifest: Path, tmp_path: Path
) -> None:
    def settings(hosts: tuple[str, ...], origins: tuple[str, ...] = ()) -> Settings:
        return Settings(
            config_dir=CONFIG_DIR,
            manifest=tissue_manifest,
            slide_dir=tissue_manifest.parent,
            log_dir=tmp_path / "logs",
            cache_dir=None,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )

    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream"}
    local = {**headers, "host": "localhost:7860"}
    opening = {"slide_id": "syn_tissue", "actor": "agent:h"}
    with TestClient(create_app(settings(("localhost:*",)), manager=manager)) as client:
        # DNS rebinding: a page on another name reaches neither MCP nor REST nor the UI.
        for resp in [
            client.post("/mcp", json=ping, headers=headers),
            client.post("/api/sessions", json=opening),
            client.get("/"),
        ]:
            assert resp.status_code == 421
            assert resp.json()["error"]["code"] == "HOST_NOT_ALLOWED"
        assert client.post("/mcp", json=ping, headers=local).status_code == 200
        assert client.get("/api/slides", headers=local).status_code == 200
        same_site = {**local, "origin": "http://localhost:7860"}
        assert client.post("/api/sessions", json=opening, headers=same_site).status_code == 201
        evil = {**local, "origin": "https://evil.example"}
        for resp in [
            client.post("/mcp", json=ping, headers=evil),
            client.post("/api/sessions", json=opening, headers=evil),
        ]:
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
        # The REST error envelope still answers unknown paths; /mcp/ is not a second endpoint.
        assert client.get("/mcp/extra", headers=local).json()["error"]["code"] == "NOT_FOUND"
    space = settings(("myspace.hf.space",), ("*",))
    with TestClient(create_app(space, manager=manager)) as client:
        hf = {**headers, "host": "myspace.hf.space", "origin": "https://anything.example"}
        assert client.post("/mcp", json=ping, headers=hf).status_code == 200  # any origin
    with TestClient(
        create_app(settings(("*",), ("https://app.example",)), manager=manager)
    ) as client:
        assert client.post("/mcp", json=ping, headers=headers).status_code == 200  # any host
        bad = {**headers, "origin": "https://evil.example"}
        assert client.post("/mcp", json=ping, headers=bad).status_code == 403  # origins still apply
        good = {**headers, "origin": "https://app.example/"}
        assert client.post("/mcp", json=ping, headers=good).status_code == 200


async def test_arguments_are_as_strict_as_rest(manager: SessionManager) -> None:
    async with Client(build_mcp(manager)) as c:
        bad_open = {"slide_id": "syn_tissue", "actor": "agent:s", "profle": "unconstrained"}
        err = _err(await c.call_tool("create_session", bad_open))
        assert err["code"] == "INVALID_PARAMETER"
        assert "profle" in err["message"]
        sid = _data(
            await c.call_tool("create_session", {"slide_id": "syn_tissue", "actor": "agent:s"})
        )["session_id"]
        for tool, args in [
            ("measure", {"p1_px": "[1, 2]", "p2_px": [5, 5]}),  # JSON held in a string
            (
                "annotate",
                {"geometry": {"type": "point", "coordinates": [1, 1]}, "label": "x", "nte": "y"},
            ),
            ("submit_report", {"report": json.dumps(REPORT)}),
            ("set_magnification", {"mag": True}),
            ("pan", {"dx": 0.5}),
        ]:
            err = _err(await c.call_tool(tool, {"session_id": sid, **args}))
            assert err["code"] == "INVALID_PARAMETER", (tool, err)
        assert (
            _err(await c.call_tool("get_state", {"session_id": "nope"}))["code"]
            == "INVALID_PARAMETER"
        )
        log = (await c.call_tool("export_trajectory", {"session_id": sid})).content[0].text
    # Refused before the session, like a REST 422: nothing but opening and export is logged.
    assert [json.loads(line)["action"] for line in log.splitlines()[1:]] == [
        "create_session",
        "export_trajectory",
    ]


async def test_unexpected_failures_come_back_as_internal_errors(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(**_: Any) -> Any:
        raise PermissionError("logs are read-only")

    monkeypatch.setattr(manager, "create", broken)
    monkeypatch.setattr(manager, "trajectory", lambda sid: (_ for _ in ()).throw(OSError("disk")))
    async with Client(build_mcp(manager)) as c:
        err = _err(
            await c.call_tool("create_session", {"slide_id": "syn_tissue", "actor": "agent:x"})
        )
        assert err == {
            "code": "INTERNAL",
            "message": "Internal error; the request was not completed.",
        }
        err = _err(await c.call_tool("export_trajectory", {"session_id": "0" * 32}))
        assert err["code"] == "INTERNAL"
        assert "disk" not in json.dumps(err)


async def test_an_mcp_client_reads_a_whole_session_over_http(
    manager: SessionManager, tissue_manifest: Path, tmp_path: Path
) -> None:
    settings = Settings(
        config_dir=CONFIG_DIR,
        manifest=tissue_manifest,
        slide_dir=tissue_manifest.parent,
        log_dir=tmp_path / "logs",
        cache_dir=None,
    )
    with serve(create_app(settings, manager=manager)) as url:
        async with Client(f"{url}/mcp") as c:
            assert {t.name for t in (await c.list_tools()).tools} == TOOLS
            r = await c.call_tool(
                "create_session", {"slide_id": "syn_tissue", "actor": "agent:http"}
            )
            sid = _data(r)["session_id"]
            _jpeg(r)
            _jpeg(await c.call_tool("set_magnification", {"session_id": sid, "mag": 10}))
            receipt = _data(
                await c.call_tool("submit_report", {"session_id": sid, "report": REPORT})
            )
            assert receipt["observation"]["accepted"] is True
            log = (await c.call_tool("export_trajectory", {"session_id": sid})).content[0].text
    assert replay(log, manager.registry).ok
