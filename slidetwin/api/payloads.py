"""Response payloads shared by the REST routes and the MCP tools, so both serve the same data."""

from __future__ import annotations

import base64
from typing import Any

from slidetwin.core.session import ActionResult, Session, ThumbnailRecord, ViewRecord


def image_url(session_id: str, image_id: str) -> str:
    folder = "thumbnails" if image_id.startswith("t_") else "views"
    return f"/api/sessions/{session_id}/{folder}/{image_id}.jpg"


def respond(session: Session, result: ActionResult, include_image: bool) -> dict[str, Any]:
    """The action response: seq, costs, state, observation (with the image URL, and the JPEG as
    base64 when `include_image`) and rule violations."""
    out = result.to_dict()
    out["session_id"] = session.session_id
    obs = result.observation
    if isinstance(obs, ViewRecord | ThumbnailRecord):
        image_id = obs.view_id if isinstance(obs, ViewRecord) else obs.thumb_id
        encoded = (
            base64.b64encode(result.image).decode() if include_image and result.image else None
        )
        out["observation"]["image"] = {
            "url": image_url(session.session_id, image_id),
            "mime": "image/jpeg",
            "base64": encoded,
        }
    return out
