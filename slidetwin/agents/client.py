"""One client interface over REST and MCP (SPEC 12).

Agents see the environment only through this module, exactly as an outside agent would: the public
REST routes or the MCP tools, never the core objects. Actions are named after the MCP tools.

    async with Env.rest("http://localhost:7860") as env:      # or Env.mcp(".../mcp")
        s = await env.open("cmu1_small", actor="agent:me")
        step = await s.set_magnification(10)                   # step.image is the JPEG
        await s.submit_report({...})
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol, Self

import httpx2 as httpx
from mcp import Client

# Action name -> (HTTP method, path after /api/sessions/{id}/, the response may carry an image).
SESSION_ROUTES: dict[str, tuple[str, str, bool]] = {
    "get_state": ("GET", "state", False),
    "get_thumbnail": ("POST", "thumbnail", True),
    "set_magnification": ("POST", "magnification", True),
    "pan": ("POST", "pan", True),
    "move_to": ("POST", "move_to", True),
    "view": ("POST", "view", True),
    "measure": ("POST", "measure", False),
    "annotate": ("POST", "annotations", False),
    "crop": ("POST", "crop", True),
    "submit_report": ("POST", "report", False),
}
CATALOGUE_ROUTES = {
    "list_slides": "/api/slides",
    "list_profiles": "/api/profiles",
    "list_protocols": "/api/protocols",
    "get_report_schema": "/api/report_schema",
}


class EnvError(Exception):
    """An action the environment refused, with the error envelope's fields."""

    def __init__(self, body: Mapping[str, Any], status: int | None = None) -> None:
        self.code: str = str(body.get("code", "UNKNOWN"))
        self.hint: str = str(body.get("hint") or "")
        self.violations: list[dict[str, Any]] = list(body.get("violations") or [])
        self.body = dict(body)
        self.status = status
        super().__init__(f"{self.code}: {body.get('message', '')}")


@dataclass(frozen=True)
class Step:
    """One response: the JSON payload and, for views, crops and thumbnails, the JPEG."""

    data: dict[str, Any]
    image: bytes | None = None

    @property
    def state(self) -> dict[str, Any]:
        state: dict[str, Any] = self.data["state"]
        return state

    @property
    def observation(self) -> dict[str, Any]:
        obs: dict[str, Any] = self.data.get("observation") or {}
        return obs


class Transport(Protocol):
    async def call(self, name: str, args: dict[str, Any]) -> Step: ...

    async def text(self, name: str, args: dict[str, Any]) -> str: ...

    async def aclose(self) -> None: ...


class RestTransport:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._own = client is None
        self._http = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=120.0)

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = await self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            try:
                body = resp.json()["error"]
            except (ValueError, KeyError, TypeError):
                body = {"code": f"HTTP_{resp.status_code}", "message": resp.text[:200]}
            raise EnvError(body, resp.status_code)
        return resp

    async def call(self, name: str, args: dict[str, Any]) -> Step:
        args = dict(args)
        if name in CATALOGUE_ROUTES:
            return Step((await self._send("GET", CATALOGUE_ROUTES[name])).json())
        if name == "create_session":
            resp = await self._send("POST", "/api/sessions", json=args)
            return _with_image(resp.json())
        method, tail, images = SESSION_ROUTES[name]
        path = f"/api/sessions/{args.pop('session_id')}/{tail}"
        if method == "GET":
            return Step((await self._send("GET", path)).json())
        params = {"include_image": "true"} if images else None
        return _with_image((await self._send("POST", path, json=args, params=params)).json())

    async def text(self, name: str, args: dict[str, Any]) -> str:
        if name != "export_trajectory":
            raise ValueError(f"{name} does not return text")
        return (await self._send("GET", f"/api/sessions/{args['session_id']}/trajectory")).text

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()


def _with_image(data: dict[str, Any]) -> Step:
    image = ((data.get("observation") or {}).get("image") or {}).get("base64")
    return Step(data, base64.b64decode(image) if image else None)


class McpTransport:
    def __init__(self, server: Any) -> None:
        """`server`: the /mcp URL, or an MCPServer for an in-process connection."""
        self._client = Client(server)
        self._stack = AsyncExitStack()

    async def open(self) -> None:
        await self._stack.enter_async_context(self._client)

    async def _tool(self, name: str, args: dict[str, Any]) -> Any:
        result = await self._client.call_tool(name, args)
        if result.is_error:
            body = (result.structured_content or {}).get("error")
            if body is None:  # rejected by the SDK's argument check, before the environment
                text = " ".join(b.text for b in result.content if b.type == "text")
                body = {"code": "INVALID_PARAMETER", "message": text}
            raise EnvError(body)
        return result

    async def call(self, name: str, args: dict[str, Any]) -> Step:
        result = await self._tool(name, args)
        data = result.structured_content
        if data is None:
            data = json.loads(next(b.text for b in result.content if b.type == "text"))
        image = next((b.data for b in result.content if b.type == "image"), None)
        return Step(data, base64.b64decode(image) if image else None)

    async def text(self, name: str, args: dict[str, Any]) -> str:
        result = await self._tool(name, args)
        return "".join(b.text for b in result.content if b.type == "text")

    async def aclose(self) -> None:
        await self._stack.aclose()


class Env:
    """The environment as one agent sees it; use `Env.rest(...)` or `Env.mcp(...)`."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    @classmethod
    def rest(cls, base_url: str, client: httpx.AsyncClient | None = None) -> Env:
        return cls(RestTransport(base_url, client))

    @classmethod
    def mcp(cls, server: Any) -> Env:
        return cls(McpTransport(server))

    async def __aenter__(self) -> Self:
        if isinstance(self.transport, McpTransport):
            await self.transport.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.transport.aclose()

    async def list_slides(self) -> list[dict[str, Any]]:
        slides: list[dict[str, Any]] = (await self.transport.call("list_slides", {})).data["slides"]
        return slides

    async def report_schema(self) -> dict[str, Any]:
        return (await self.transport.call("get_report_schema", {})).data

    async def open(
        self,
        slide_id: str,
        *,
        actor: str,
        profile: str = "viewer",
        protocol: str = "sln_metastasis",
        viewport_px: list[int] | None = None,
    ) -> EnvSession:
        args: dict[str, Any] = {
            "slide_id": slide_id,
            "actor": actor,
            "profile": profile,
            "protocol": protocol,
        }
        if viewport_px is not None:
            args["viewport_px"] = list(viewport_px)
        first = await self.transport.call("create_session", args)
        return EnvSession(self.transport, first)


@dataclass
class EnvSession:
    """One reading session. Each method is one logged action and returns its `Step`."""

    transport: Transport
    first: Step
    session_id: str = field(init=False)
    slide: dict[str, Any] = field(init=False)
    task: dict[str, Any] = field(init=False)
    rules: dict[str, Any] = field(init=False)
    last: Step = field(init=False)

    def __post_init__(self) -> None:
        self.session_id = self.first.data["session_id"]
        self.slide = self.first.data["slide"]
        self.task = self.first.data["task"]
        self.rules = self.first.data["rules"]
        self.last = self.first

    async def _do(self, name: str, **args: Any) -> Step:
        step = await self.transport.call(name, {"session_id": self.session_id, **args})
        self.last = step
        return step

    async def get_state(self) -> Step:
        return await self._do("get_state")

    async def get_thumbnail(self) -> Step:
        return await self._do("get_thumbnail")

    async def set_magnification(self, mag: float) -> Step:
        return await self._do("set_magnification", mag=mag)

    async def pan(self, dx: float, dy: float) -> Step:
        return await self._do("pan", dx=dx, dy=dy)

    async def move_to(self, x_um: float, y_um: float) -> Step:
        return await self._do("move_to", x_um=x_um, y_um=y_um)

    async def view(self) -> Step:
        return await self._do("view")

    async def measure(self, p1_px: list[float], p2_px: list[float]) -> Step:
        return await self._do("measure", p1_px=list(p1_px), p2_px=list(p2_px))

    async def annotate(self, geometry: dict[str, Any], label: str, note: str = "") -> Step:
        return await self._do("annotate", geometry=geometry, label=label, note=note)

    async def crop(self, x_um: float, y_um: float, w_um: float, h_um: float) -> Step:
        return await self._do("crop", x_um=x_um, y_um=y_um, w_um=w_um, h_um=h_um)

    async def submit_report(self, report: dict[str, Any]) -> Step:
        return await self._do("submit_report", report=report)

    async def export_trajectory(self) -> str:
        return await self.transport.text("export_trajectory", {"session_id": self.session_id})
