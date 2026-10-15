"""Vision-LLM reader (SPEC 12): a language model reads a slide through the environment's tools.

The model sees each served view as an image, moves with set_magnification, pan and move_to,
measures, and ends with submit_report. It reaches the environment through `Env` (REST or MCP), like
any outside agent, so its whole reading is logged and replayable.

Backends. Keys come from the environment only, never from files in the repository:

- `anthropic`: the Anthropic API through the official SDK (`pip install -e ".[llm]"`), model
  `claude-opus-5-5` by default. Credentials: `ANTHROPIC_API_KEY` or an `ant auth login` profile.
  Refused requests are retried server-side on Anthropic's recommended fallback model.
- `openai`: any OpenAI-compatible chat endpoint, e.g. a local vLLM serving a vision model
  (`--base-url http://localhost:8000/v1 --model Qwen/Qwen2.5-VL-7B-Instruct`). `OPENAI_API_KEY` is
  sent when set.

    python -m slidetwin.agents llm --env http://localhost:7860 --slide cmu1_small
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2 as httpx

from slidetwin.agents.client import Env, EnvError, EnvSession, Step
from slidetwin.agents.common import AgentResult

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] | None  # None: the model sent arguments that are not a JSON object


@dataclass(frozen=True)
class Outcome:
    text: str  # JSON shown to the model
    image: bytes | None = None
    is_error: bool = False
    report: dict[str, Any] | None = None  # the receipt of an accepted report


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


NUMBER = {"type": "number"}
POINT = {"type": "array", "items": NUMBER, "minItems": 2, "maxItems": 2}


def tools(report_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The tools offered to the model, in the Anthropic format (converted for OpenAI)."""
    return [
        _tool(
            "get_thumbnail",
            "The whole slide at low resolution, its um per pixel and its numbered tissue pieces.",
            {},
        ),
        _tool(
            "set_magnification",
            "Switch to a preset magnification (the rules list them); returns the new view.",
            {"mag": NUMBER},
        ),
        _tool(
            "pan",
            "Move by (dx, dy) viewports: dx=1 is one viewport width to the right, dy=-0.5 half a "
            "viewport up. Returns the new view.",
            {"dx": NUMBER, "dy": NUMBER},
        ),
        _tool(
            "move_to",
            "Centre the view on (x_um, y_um), micrometres from the slide's top-left corner. "
            "Returns the new view.",
            {"x_um": NUMBER, "y_um": NUMBER},
        ),
        _tool(
            "measure",
            "Distance in um between two points given in pixels of the current view; returns a "
            "measurement_id to cite.",
            {"p1_px": POINT, "p2_px": POINT},
        ),
        _tool(
            "submit_report",
            "Submit the final report. It is checked against the rules; an accepted report ends "
            "the reading.",
            {"report": report_schema},
        ),
    ]


SYSTEM = """\
You are reading a digital pathology slide in SlideTwin, a research simulator of a slide viewer. \
This is not clinical use.

You see the slide only through the tools. Every move returns the new field of view as an image \
with JSON metadata: positions in micrometres (um) from the slide's top-left corner, the \
magnification, the view's pixel size, and the virtual time the action cost.

Read as the protocol below asks. Screen the tissue systematically, confirm findings at a high \
enough magnification, and measure on the current view in its pixel coordinates. Cite your \
evidence in the report: findings list the view_ids that show them, measurement claims cite a \
measurement_id, and a negative claim needs the tissue in its scope to have been seen. End with \
submit_report; its answer lists the rule checks.

Reading protocol (the task):
{task}

Viewer rules (presets, costs, budget):
{rules}
"""


def system_prompt(session: EnvSession) -> str:
    return SYSTEM.format(
        task=json.dumps(session.task, indent=1, sort_keys=True),
        rules=json.dumps(session.rules, indent=1, sort_keys=True),
    )


def _view_summary(step: Step) -> dict[str, Any]:
    obs = step.observation
    keys = ["view_id", "mag", "mpp", "center_um", "bbox_um", "size_px"]
    out: dict[str, Any] = {k: obs[k] for k in keys if k in obs}
    out.update(cost_s=step.data.get("cost_s"), elapsed_s=step.data.get("elapsed_s"))
    return out


async def execute(session: EnvSession, call: ToolCall) -> Outcome:
    """Run one tool call against the environment."""
    args = call.input
    if args is None:
        return Outcome(
            json.dumps({"error": "The arguments were not a JSON object."}), is_error=True
        )
    try:
        if call.name == "get_thumbnail":
            step = await session.get_thumbnail()
            obs = step.observation
            info = {k: obs[k] for k in ["thumb_id", "size_px", "um_per_px", "tissue_pieces"]}
            return Outcome(json.dumps(info), step.image)
        moves: dict[str, Callable[[], Awaitable[Step]]] = {
            "set_magnification": lambda: session.set_magnification(args["mag"]),
            "pan": lambda: session.pan(args["dx"], args["dy"]),
            "move_to": lambda: session.move_to(args["x_um"], args["y_um"]),
        }
        if call.name in moves:
            step = await moves[call.name]()
            return Outcome(json.dumps(_view_summary(step)), step.image)
        if call.name == "measure":
            m = (await session.measure(args["p1_px"], args["p2_px"])).observation
            keys = ["measurement_id", "view_id", "length_um", "length_px"]
            return Outcome(json.dumps({k: m[k] for k in keys}))
        if call.name == "submit_report":
            receipt = (await session.submit_report(args["report"])).observation
            return Outcome(json.dumps(receipt), report=receipt)
    except KeyError as err:
        return Outcome(json.dumps({"error": f"Missing argument {err}."}), is_error=True)
    except EnvError as err:
        return Outcome(json.dumps({"error": err.body}), is_error=True)
    return Outcome(json.dumps({"error": f"Unknown tool {call.name}."}), is_error=True)


class Chat(Protocol):
    """One model conversation; `stopped` says why it cannot go on (refusal, length)."""

    stopped: str | None

    def start(self, system: str, tools: list[dict[str, Any]], text: str, image: bytes) -> None: ...

    async def next(self) -> list[ToolCall]: ...

    def add_results(self, calls: list[ToolCall], outcomes: list[Outcome]) -> None: ...

    def say(self, text: str) -> None: ...


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class AnthropicChat:
    """The Anthropic API (official SDK). An append-only transcript, prompt caching on the growing
    prefix, and server-side fallback when a request is declined."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        effort: str = "medium",
        max_tokens: int = 16000,
        client: Any = None,
    ) -> None:
        if client is None:
            import anthropic  # optional dependency: pip install -e ".[llm]"

            client = anthropic.AsyncAnthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.stopped: str | None = None
        self.system = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict[str, Any]], text: str, image: bytes) -> None:
        self.system = system
        self.tools = tools
        image_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(image)},
        }
        self.messages = [{"role": "user", "content": [image_block, {"type": "text", "text": text}]}]

    async def next(self) -> list[ToolCall]:
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            cache_control={"type": "ephemeral"},  # caches the prefix, which only ever grows
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # Keep every block as returned (thinking and fallback blocks included): never edit history.
        self.messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            self.stopped = f"refusal ({getattr(details, 'category', None)})"
            return []
        if response.stop_reason == "max_tokens":
            self.stopped = "max_tokens"
            return []
        return [
            ToolCall(b.id, b.name, dict(b.input) if isinstance(b.input, dict) else None)
            for b in response.content
            if b.type == "tool_use"
        ]

    def add_results(self, calls: list[ToolCall], outcomes: list[Outcome]) -> None:
        blocks = []
        for call, out in zip(calls, outcomes, strict=True):
            content: list[dict[str, Any]] = [{"type": "text", "text": out.text}]
            if out.image:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _b64(out.image),
                        },
                    }
                )
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": content,
                    "is_error": out.is_error,
                }
            )
        self.messages.append({"role": "user", "content": blocks})  # all results in one message

    def say(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


class OpenAIChat:
    """An OpenAI-compatible chat endpoint (e.g. vLLM). Tool messages carry text only, so the images
    a tool returned follow in a user message."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        max_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.http = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=600.0)
        self.headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self.max_tokens = max_tokens
        self.stopped: str | None = None
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    @staticmethod
    def _image(data: bytes) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(data)}"}}

    def start(self, system: str, tools: list[dict[str, Any]], text: str, image: bytes) -> None:
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": text}, self._image(image)]},
        ]

    async def next(self) -> list[ToolCall]:
        body = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools,
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
        }
        resp = await self.http.post("/chat/completions", json=body, headers=self.headers)
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
        msg = choice["message"]
        raw_calls = msg.get("tool_calls") or []
        turn: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if raw_calls:
            turn["tool_calls"] = raw_calls
        self.messages.append(turn)
        if choice.get("finish_reason") == "length":
            self.stopped = "max_tokens"
            return []
        calls = []
        for tc in raw_calls:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None
            calls.append(
                ToolCall(tc["id"], tc["function"]["name"], args if isinstance(args, dict) else None)
            )
        return calls

    def add_results(self, calls: list[ToolCall], outcomes: list[Outcome]) -> None:
        for call, out in zip(calls, outcomes, strict=True):
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": out.text})
        images = [out.image for out in outcomes if out.image]
        if images:
            note = {
                "type": "text",
                "text": "The images returned by the tool calls above, in order.",
            }
            self.messages.append(
                {"role": "user", "content": [note, *(self._image(i) for i in images)]}
            )

    def say(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


START = (
    "Read slide {slide} for the task in the system prompt. The slide is {w:.0f} x {h:.0f} um; "
    "magnifications available here: {mags}. This is the first view ({view}). Begin."
)
NUDGE = "Keep reading with the tools. When you are done, call submit_report."


async def run(
    env: Env,
    slide_id: str,
    chat: Chat,
    *,
    actor: str = "agent:llm",
    profile: str = "viewer",
    viewport_px: list[int] | None = None,
    max_turns: int = 40,
    max_nudges: int = 2,
) -> AgentResult:
    s = await env.open(slide_id, actor=actor, profile=profile, viewport_px=viewport_px)
    w, h = s.slide["dimensions_um"]
    first = START.format(
        slide=slide_id,
        w=w,
        h=h,
        mags=", ".join(f"{m:g}x" for m in s.slide["available_mags"]),
        view=json.dumps(_view_summary(s.first)),
    )
    chat.start(system_prompt(s), tools(await env.report_schema()), first, s.first.image or b"")
    notes: list[str] = []
    receipt: dict[str, Any] | None = None
    nudges = 0
    for _ in range(max_turns):
        calls = await chat.next()
        if chat.stopped:
            notes.append(f"model stopped: {chat.stopped}")
            break
        if not calls:
            if nudges >= max_nudges:
                notes.append("the model stopped calling tools")
                break
            nudges += 1
            chat.say(NUDGE)
            continue
        outcomes = [await execute(s, c) for c in calls]
        chat.add_results(calls, outcomes)
        receipt = next((o.report for o in outcomes if o.report is not None), None)
        if receipt is not None:
            break
    else:
        notes.append(f"no report after {max_turns} turns")
    if receipt is None:
        notes.append("no accepted report")
    return AgentResult(s.session_id, receipt, await s.export_trajectory(), notes)
