"""MCP adapter (SPEC 7, 12): one tool per session action, calling the same core methods as REST.

Each tool returns the JSON of the matching REST response, as text and as structured content. Tools
that serve pixels add the JPEG as image content: the same image, logged once, that the REST route
would return. A failed action returns an `isError` result carrying the REST error envelope.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent
from pydantic import Field, StrictInt

from slidetwin import __version__
from slidetwin.api.payloads import respond
from slidetwin.api.schemas import Num, PointPx
from slidetwin.core.errors import SlideTwinError
from slidetwin.core.manager import SessionManager
from slidetwin.core.report import export_schema
from slidetwin.core.session import ActionResult, Session

# Logged as the client metadata of every action that comes through MCP.
VIA_MCP = {"via": "mcp"}

INSTRUCTIONS = """\
SlideTwin simulates a digital pathology slide viewer. Research prototype, not for clinical use.

1. list_slides, then create_session(slide_id, actor="agent:<your name>"). The answer holds the
   first view (an image), the slide size, the task (reading protocol) and the rules (profile).
2. Move with set_magnification (preset values only), pan (in viewport units) and move_to
   (micrometres). Each move returns the new field of view as an image plus JSON. Positions are
   micrometres (um) from the level-0 top-left corner. Every action costs virtual time (cost_s,
   elapsed_s).
3. measure and annotate take pixel coordinates in the current view.
4. submit_report ends the session and returns the rule checks; get_report_schema gives the format.
   export_trajectory returns the full log.
You see only what you ask for: there is no other access to the slide."""

SessionId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{32}$", description="Session id returned by create_session."),
]


def transport_security(
    hosts: tuple[str, ...], origins: tuple[str, ...]
) -> TransportSecuritySettings:
    """Host and Origin checks for /mcp (DNS rebinding protection); "*" in `hosts` turns them off."""
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(hosts),
        allowed_origins=list(origins),
    )


def _text(data: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(data, ensure_ascii=False))


def _ok(data: dict[str, Any], image: bytes | None = None) -> CallToolResult:
    content: list[ContentBlock] = [_text(data)]
    if image:
        content.append(
            ImageContent(
                type="image", data=base64.b64encode(image).decode(), mime_type="image/jpeg"
            )
        )
    return CallToolResult(content=content, structured_content=data)


def _error(err: SlideTwinError) -> CallToolResult:
    body = {"error": err.to_dict()}
    return CallToolResult(content=[_text(body)], structured_content=body, is_error=True)


def build_mcp(manager: SessionManager) -> MCPServer:
    """The MCP server for `manager`. Mount its streamable HTTP route next to the REST API."""
    mcp = MCPServer(
        "SlideTwin", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING"
    )

    def act(
        session_id: str, call: Callable[[Session], ActionResult], *, touch: bool = True
    ) -> CallToolResult:
        try:
            session = manager.get(session_id, touch=touch)
            result = call(session)
        except SlideTwinError as err:
            return _error(err)
        return _ok(respond(session, result, False), result.image)

    # --- catalogue -------------------------------------------------------------------------------

    @mcp.tool()
    def list_slides() -> CallToolResult:
        """Slides that can be opened: id, license, attribution. No labels or file names."""
        return _ok({"slides": manager.catalogue()})

    @mcp.tool()
    def list_profiles() -> CallToolResult:
        """Rule profiles (viewer, unconstrained): presets, action costs, budgets."""
        return _ok({"profiles": manager.profiles()})

    @mcp.tool()
    def list_protocols() -> CallToolResult:
        """Reading protocols with every rule and its review status."""
        return _ok({"protocols": manager.protocols()})

    @mcp.tool()
    def get_report_schema() -> CallToolResult:
        """JSON Schema of the report accepted by submit_report."""
        return _ok(export_schema())

    # --- sessions --------------------------------------------------------------------------------

    @mcp.tool()
    def create_session(
        slide_id: Annotated[str, Field(description="A slide id from list_slides.")],
        actor: Annotated[
            str, Field(description="'agent:<name>' or 'human:<alias>' (1-64 of A-Z a-z 0-9 . _ -).")
        ],
        profile: Annotated[str, Field(description="Rule profile: viewer or unconstrained.")] = (
            "viewer"
        ),
        protocol: Annotated[str, Field(description="Reading protocol id.")] = "sln_metastasis",
        viewport_px: Annotated[
            list[StrictInt] | None,
            Field(min_length=2, max_length=2, description="[width, height]; default 1280x1024."),
        ] = None,
    ) -> CallToolResult:
        """Open a slide. Starts at the slide centre at the lowest magnification and returns the
        first view, the slide geometry, the task (protocol) and the rules (profile)."""
        try:
            session = manager.create(
                slide_id=slide_id,
                actor=actor,
                profile=profile,
                protocol=protocol,
                viewport_px=viewport_px,
            )
        except SlideTwinError as err:
            return _error(err)
        out = respond(session, session.initial, False)
        out.update(session.opening())
        return _ok(out, session.initial.image)

    @mcp.tool()
    def get_state(session_id: SessionId) -> CallToolResult:
        """Position, magnification, field of view, elapsed virtual time and budget. No pixels, no
        cost."""
        return act(session_id, lambda s: s.get_state(client=VIA_MCP), touch=False)

    @mcp.tool()
    def get_thumbnail(session_id: SessionId) -> CallToolResult:
        """The navigation thumbnail, its um per pixel and the numbered tissue pieces, if the profile
        allows it."""
        return act(session_id, lambda s: s.get_thumbnail(client=VIA_MCP))

    @mcp.tool()
    def set_magnification(
        session_id: SessionId,
        mag: Annotated[Num, Field(description="A preset magnification, e.g. 10.")],
    ) -> CallToolResult:
        """Switch to a preset magnification; returns the new view."""
        return act(session_id, lambda s: s.set_magnification(mag, client=VIA_MCP))

    @mcp.tool()
    def pan(
        session_id: SessionId,
        dx: Annotated[
            Num, Field(description="Move right by this many viewport widths (0.5 = half).")
        ],
        dy: Annotated[Num, Field(description="Move down by this many viewport heights.")],
    ) -> CallToolResult:
        """Move by (dx, dy) viewports, clamped to the slide; returns the new view."""
        return act(session_id, lambda s: s.pan(dx, dy, client=VIA_MCP))

    @mcp.tool()
    def move_to(
        session_id: SessionId,
        x_um: Annotated[Num, Field(description="New centre x in um from the level-0 left edge.")],
        y_um: Annotated[Num, Field(description="New centre y in um from the level-0 top edge.")],
    ) -> CallToolResult:
        """Centre the view on (x_um, y_um), like clicking the thumbnail; returns the new view."""
        return act(session_id, lambda s: s.move_to(x_um, y_um, client=VIA_MCP))

    @mcp.tool()
    def view(session_id: SessionId) -> CallToolResult:
        """Serve the current field of view again (only needed when the profile has no auto_view)."""
        return act(session_id, lambda s: s.view(client=VIA_MCP))

    @mcp.tool()
    def measure(
        session_id: SessionId,
        p1_px: Annotated[PointPx, Field(description="[x, y] in current-view pixels.")],
        p2_px: Annotated[PointPx, Field(description="[x, y] in current-view pixels.")],
    ) -> CallToolResult:
        """Distance between two points of the current view, in um; cite its measurement_id."""
        return act(session_id, lambda s: s.measure(list(p1_px), list(p2_px), client=VIA_MCP))

    @mcp.tool()
    def annotate(
        session_id: SessionId,
        geometry: Annotated[
            dict[str, Any],
            Field(
                description='{"type": "point", "coordinates": [x, y]} or {"type": "polygon", '
                '"coordinates": [[x, y], ...]} in current-view pixels.'
            ),
        ],
        label: Annotated[str, Field(description="1-64 characters.")],
        note: str = "",
    ) -> CallToolResult:
        """Mark a point or polygon on the current view; it is stored in pixels and um."""
        return act(session_id, lambda s: s.annotate(geometry, label, note, client=VIA_MCP))

    @mcp.tool()
    def crop(
        session_id: SessionId,
        x_um: Annotated[Num, Field(description="Left edge in um.")],
        y_um: Annotated[Num, Field(description="Top edge in um.")],
        w_um: Annotated[Num, Field(description="Width in um.")],
        h_um: Annotated[Num, Field(description="Height in um.")],
    ) -> CallToolResult:
        """A free rectangle (profile 'unconstrained' only), scaled to the viewport, never past the
        native resolution."""
        return act(session_id, lambda s: s.crop(x_um, y_um, w_um, h_um, client=VIA_MCP))

    @mcp.tool()
    def submit_report(
        session_id: SessionId,
        report: Annotated[
            dict[str, Any], Field(description="A report matching get_report_schema.")
        ],
    ) -> CallToolResult:
        """Submit the structured report. Returns the rule checks and the coverage; an accepted
        report closes the session."""
        return act(session_id, lambda s: s.submit_report(report, client=VIA_MCP))

    @mcp.tool()
    def export_trajectory(session_id: SessionId) -> CallToolResult:
        """The session log as JSONL: a header line, then one event per action. Still available after
        the session ends."""
        try:
            text = manager.trajectory(session_id)
        except SlideTwinError as err:
            return _error(err)
        return CallToolResult(content=[TextContent(type="text", text=text)])

    return mcp
