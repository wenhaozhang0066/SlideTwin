"""The LLM agent example without any real model: a scripted chat drives the live environment, and
the two backends are checked against a fake SDK client and a mock OpenAI-compatible server."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest

from slidetwin.agents import llm_agent_example as llm
from slidetwin.agents.__main__ import main
from slidetwin.agents.client import Env
from slidetwin.core.manager import SessionManager
from slidetwin.core.replay import replay
from tests.agents.conftest import SECRET

Live = tuple[str, SessionManager]
JPEG = b"\xff\xd8\xff\xe0fake"

REPORT = {
    "task": "sln_metastasis",
    "conclusion": {
        "metastasis": "indeterminate",
        "category": "none",
        "largest_deposit_mm": None,
        "extranodal_extension": "not_assessable",
    },
    "claims": [],
    "free_text": "scripted",
}


class ScriptedChat:
    """Plays a fixed list of turns; records what the agent loop sends back."""

    def __init__(self, turns: list[list[llm.ToolCall]], stop_after: int | None = None) -> None:
        self.turns = turns
        self.stopped: str | None = None
        self.stop_after = stop_after
        self.system = ""
        self.tool_names: list[str] = []
        self.first_text = ""
        self.results: list[tuple[list[llm.ToolCall], list[llm.Outcome]]] = []
        self.said: list[str] = []
        self.calls = 0

    def start(self, system: str, tools: list[dict[str, Any]], text: str, image: bytes) -> None:
        self.system = system
        self.tool_names = [t["name"] for t in tools]
        self.first_text = text
        assert image[:3] == b"\xff\xd8\xff"

    async def next(self) -> list[llm.ToolCall]:
        self.calls += 1
        if self.stop_after is not None and self.calls > self.stop_after:
            self.stopped = "refusal (test)"
            return []
        return self.turns.pop(0) if self.turns else []

    def add_results(self, calls: list[llm.ToolCall], outcomes: list[llm.Outcome]) -> None:
        self.results.append((calls, outcomes))

    def say(self, text: str) -> None:
        self.said.append(text)


def call(name: str, **args: Any) -> llm.ToolCall:
    return llm.ToolCall(f"id-{name}", name, args)


@pytest.mark.anyio
async def test_scripted_model_reads_and_reports(live: Live) -> None:
    url, manager = live
    chat = ScriptedChat(
        [
            [call("get_thumbnail")],
            [call("set_magnification", mag=10), call("move_to", x_um=1450, y_um=1125)],
            [call("set_magnification", mag=40)],  # refused: the slide is native 20x
            [call("set_magnification", mag=20)],
            [call("measure", p1_px=[10, 10], p2_px=[110, 10]), call("teleport")],
            [llm.ToolCall("bad", "pan", None)],
            [call("pan", dx=0.5)],  # missing dy
            [],  # no tool call: nudged once
            [call("submit_report", report=REPORT)],
        ]
    )
    async with Env.rest(url) as env:
        result = await llm.run(env, "syn_tissue", chat, viewport_px=[512, 384])
    assert chat.tool_names == [
        "get_thumbnail",
        "set_magnification",
        "pan",
        "move_to",
        "measure",
        "submit_report",
    ]
    assert "sln_metastasis" in chat.system
    assert "1.25x, 2.5x, 5x, 10x, 20x" in chat.first_text
    assert chat.said == [llm.NUDGE]
    outcomes = [o for _, batch in chat.results for o in batch]
    thumb, mag10, move, mag40, mag20, measure, unknown, bad_json, missing, report = outcomes
    assert [mag10.is_error, mag20.is_error] == [False, False]
    assert thumb.image is not None
    assert json.loads(thumb.text)["tissue_pieces"][0]["piece"] == 1
    assert json.loads(move.text)["mag"] == 10
    assert move.image is not None
    assert mag40.is_error
    assert json.loads(mag40.text)["error"]["code"] == "MAG_UNAVAILABLE"
    assert json.loads(measure.text)["length_um"] == pytest.approx(50.0)  # 100 px at 0.5 um/px
    assert unknown.is_error
    assert bad_json.is_error
    assert "Missing argument" in json.loads(missing.text)["error"]
    assert report.report is not None
    assert result.receipt == report.report
    assert result.receipt["accepted"] is True
    assert SECRET not in result.trajectory
    assert replay(result.trajectory, manager.registry).ok


@pytest.mark.anyio
async def test_loop_ends_on_a_stopped_model_or_without_tool_calls(live: Live) -> None:
    url, _ = live
    async with Env.rest(url) as env:
        refused = await llm.run(env, "syn_tissue", ScriptedChat([], stop_after=0))
        silent = await llm.run(env, "syn_tissue", ScriptedChat([]), max_nudges=1)
        busy = await llm.run(
            env, "syn_tissue", ScriptedChat([[call("pan", dx=0.1, dy=0)]] * 5), max_turns=3
        )
    assert refused.receipt is None
    assert refused.notes == ["model stopped: refusal (test)", "no accepted report"]
    assert silent.notes == ["the model stopped calling tools", "no accepted report"]
    assert busy.notes == ["no report after 3 turns", "no accepted report"]


# --- Anthropic backend (fake SDK client) -----------------------------------------------------


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def _client(responses: list[Any]) -> Any:
    messages = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _response(stop: str, *blocks: Any, details: Any = None) -> Any:
    return SimpleNamespace(stop_reason=stop, content=list(blocks), stop_details=details)


@pytest.mark.anyio
async def test_anthropic_chat_request_and_transcript() -> None:
    use = SimpleNamespace(type="tool_use", id="tu1", name="pan", input={"dx": 1, "dy": 0})
    text = SimpleNamespace(type="text", text="moving right")
    client, fake = _client(
        [
            _response("tool_use", text, use),
            _response("refusal", details=SimpleNamespace(category="bio")),
        ]
    )
    chat = llm.AnthropicChat(client=client)
    chat.start("SYSTEM", [{"name": "pan"}], "Begin.", JPEG)
    calls = await chat.next()
    assert calls == [llm.ToolCall("tu1", "pan", {"dx": 1, "dy": 0})]
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["system"] == "SYSTEM"
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["fallbacks"] == "default"
    assert req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"] == {"effort": "medium"}
    first = req["messages"][0]["content"]
    assert first[0]["type"] == "image"
    assert first[0]["source"]["media_type"] == "image/jpeg"
    chat.add_results(calls, [llm.Outcome('{"view_id": "v_0002"}', JPEG, is_error=False)])
    assistant, results = chat.messages[1], chat.messages[2]
    assert assistant == {"role": "assistant", "content": [text, use]}  # appended unchanged
    (block,) = results["content"]
    assert block["tool_use_id"] == "tu1"
    assert [c["type"] for c in block["content"]] == ["text", "image"]
    assert block["is_error"] is False
    assert await chat.next() == []
    assert chat.stopped == "refusal (bio)"


@pytest.mark.anyio
async def test_anthropic_chat_stops_at_max_tokens() -> None:
    client, _ = _client([_response("max_tokens")])
    chat = llm.AnthropicChat(client=client)
    chat.start("S", [], "Begin.", JPEG)
    assert await chat.next() == []
    assert chat.stopped == "max_tokens"
    chat.say("more")
    assert chat.messages[-1] == {"role": "user", "content": "more"}


# --- OpenAI-compatible backend (mock server) ----------------------------------------------------


@pytest.mark.anyio
async def test_openai_chat_request_and_transcript() -> None:
    seen: list[dict[str, Any]] = []
    replies = [
        {
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "pan", "arguments": '{"dx": 1, "dy": 0}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "pan", "arguments": "{oops"},
                    },
                ],
            },
        },
        {"finish_reason": "length", "message": {"content": "..."}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"auth": request.headers.get("authorization"), **json.loads(request.content)})
        return httpx.Response(200, json={"choices": [replies.pop(0)]})

    http = httpx.AsyncClient(base_url="http://vllm.test/v1", transport=httpx.MockTransport(handler))
    chat = llm.OpenAIChat("http://vllm.test/v1", "qwen-vl", api_key="k", client=http)
    chat.start("SYSTEM", llm.tools({"type": "object"}), "Begin.", JPEG)
    calls = await chat.next()
    assert calls == [llm.ToolCall("c1", "pan", {"dx": 1, "dy": 0}), llm.ToolCall("c2", "pan", None)]
    body = seen[0]
    assert body["auth"] == "Bearer k"
    assert body["model"] == "qwen-vl"
    assert body["tool_choice"] == "auto"
    assert body["tools"][2]["function"]["name"] == "pan"
    assert body["tools"][2]["function"]["parameters"]["required"] == ["dx", "dy"]
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    chat.add_results(calls, [llm.Outcome("{}", JPEG), llm.Outcome("{}", None, is_error=True)])
    assert [m["role"] for m in chat.messages[-3:]] == ["tool", "tool", "user"]
    assert len(chat.messages[-1]["content"]) == 2  # the note and one image
    assert await chat.next() == []
    assert chat.stopped == "max_tokens"
    chat.say("go on")
    assert chat.messages[-1]["content"] == "go on"
    await http.aclose()


# --- command line -----------------------------------------------------------------------------


def test_command_line_runs_an_agent(
    live: Live, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, _ = live
    out = tmp_path / "log.jsonl"
    main(
        [
            "serpentine",
            "--env",
            url,
            "--slide",
            "syn_tissue",
            "--viewport",
            "512",
            "384",
            "--out",
            str(out),
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["accepted"] is True
    assert summary["coverage"] >= 0.98
    assert json.loads(out.read_text().splitlines()[0])["actor"] == "agent:serpentine"
    main(["random", "--env", url, "--transport", "mcp", "--slide", "syn_tissue", "--seed", "3"])
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    main(["crop", "--env", url, "--slide", "syn_tissue"])
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    with pytest.raises(SystemExit):
        main(["llm", "--backend", "openai", "--env", url, "--slide", "syn_tissue"])
