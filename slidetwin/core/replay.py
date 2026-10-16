"""Replay a trajectory on a fresh session and check that it reproduces exactly (SPEC 9, 11)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slidetwin.core.config import content_hash, load_profile_data, load_protocol_data
from slidetwin.core.errors import ReplayError, SlideTwinError
from slidetwin.core.images import MemoryImageStore
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.session import Session
from slidetwin.core.slide_source import source_provenance
from slidetwin.core.trajectory import load_trajectory, parse_trajectory

# Fields of an event that must be identical in a replay. Wall time and client metadata are not.
COMPARED_FIELDS = ("action", "cost_s", "t_virtual_s", "state_after", "view_id")
# Encoder output may differ across Pillow builds; pixels may not. An observation's seq repeats its
# event's position, which shifts when replay skips an INTERNAL failure.
OBSERVATION_IGNORED = frozenset({"jpeg_sha256", "seq"})


@dataclass
class ReplayReport:
    ok: bool
    events: int
    images_checked: int
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0  # INTERNAL failures: environment errors that applied nothing


def _dispatch(s: Session, action: str, p: Mapping[str, Any]) -> None:
    if action == "get_state":
        s.get_state()
    elif action == "get_thumbnail":
        s.get_thumbnail()
    elif action == "set_magnification":
        s.set_magnification(p.get("mag"))
    elif action == "pan":
        s.pan(p.get("dx"), p.get("dy"))
    elif action == "move_to":
        s.move_to(p.get("x_um"), p.get("y_um"))
    elif action == "view":
        s.view()
    elif action == "measure":
        s.measure(p.get("p1_px"), p.get("p2_px"))
    elif action == "annotate":
        s.annotate(p.get("geometry"), p.get("label"), p.get("note", ""))
    elif action == "crop":
        s.crop(p.get("x_um"), p.get("y_um"), p.get("w_um"), p.get("h_um"))
    elif action == "submit_report":
        s.submit_report(p.get("report"))
    elif action == "export_trajectory":
        s.export_trajectory()
    else:
        raise ReplayError(f"Unknown action '{action}' in the trajectory.")


def _strip(obs: Any) -> Any:
    if isinstance(obs, dict):
        return {k: v for k, v in obs.items() if k not in OBSERVATION_IGNORED}
    return obs


def _compare(original: dict[str, Any], replayed: dict[str, Any], out: list[dict[str, Any]]) -> None:
    seq = original.get("seq")
    for key in COMPARED_FIELDS:
        if original.get(key) != replayed.get(key):
            out.append(
                {
                    "seq": seq,
                    "field": key,
                    "logged": original.get(key),
                    "replayed": replayed.get(key),
                }
            )
    logged_err = (original.get("error") or {}).get("code")
    replayed_err = (replayed.get("error") or {}).get("code")
    if logged_err != replayed_err:
        out.append({"seq": seq, "field": "error", "logged": logged_err, "replayed": replayed_err})
    if _strip(original.get("observation")) != _strip(replayed.get("observation")):
        out.append(
            {
                "seq": seq,
                "field": "observation",
                "logged": original.get("observation"),
                "replayed": replayed.get("observation"),
            }
        )


def replay(trajectory: Path | str, registry: SlideRegistry) -> ReplayReport:
    """Re-run every logged action. `trajectory` is a file path or the JSONL text itself."""
    if isinstance(trajectory, Path):
        header, events = load_trajectory(trajectory)
    else:
        header, events = parse_trajectory(trajectory)

    slide = registry.get(header["slide"]["id"])
    if slide.sha256 != header["slide"]["sha256"]:
        raise ReplayError(
            f"Slide '{slide.slide_id}' differs from the file used in the log (sha256 mismatch)."
        )
    if header["slide"].get("fileset_sha256", slide.fileset_sha256) != slide.fileset_sha256:
        raise ReplayError(
            f"Slide '{slide.slide_id}' differs from the files used in the log (file-set hash "
            "mismatch)."
        )
    logged = header["slide"].get("provenance")  # absent in logs written before M8
    if logged is not None and _settings(logged) != _settings(source_provenance(slide.source)):
        raise ReplayError(
            f"Slide '{slide.slide_id}' is read differently from the log (reader, format or "
            "settings changed)."
        )
    try:
        profile = load_profile_data(header["profile"]["content"], "trajectory header")
        protocol = load_protocol_data(header["protocol"]["content"], "trajectory header")
    except SlideTwinError as err:
        raise ReplayError(f"The trajectory header holds an invalid config: {err.message}") from None
    if content_hash(profile) != header["profile"]["sha256"]:
        raise ReplayError("The profile in the trajectory header does not match its hash.")
    if content_hash(protocol) != header["protocol"]["sha256"]:
        raise ReplayError("The protocol in the trajectory header does not match its hash.")

    try:
        session = Session(
            session_id=f"replay-{header['session_id']}",
            slide_id=slide.slide_id,
            source=slide.source,
            slide_sha256=slide.sha256,
            slide_fileset_sha256=slide.fileset_sha256,
            profile=profile,
            protocol=protocol,
            actor=header["actor"],
            images=MemoryImageStore(),
            viewport_px=header["viewport_px"],
        )
    except SlideTwinError as err:
        raise ReplayError(f"The session in the log cannot be recreated: {err.message}") from None
    mismatches: list[dict[str, Any]] = []
    _compare(events[0], session.history[1], mismatches)
    skipped = 0
    for event in events[1:]:
        if (event.get("error") or {}).get("code") == "INTERNAL":
            # An environment failure (e.g. a full disk) that changed nothing; it cannot be re-run.
            skipped += 1
            continue
        try:
            _dispatch(session, event["action"], event["params"])
        except ReplayError:
            raise
        except SlideTwinError:
            pass  # the replayed session logged the error; compared below
        _compare(event, session.history[-1], mismatches)
    images = sum(
        1
        for ev in events
        if isinstance(ev.get("observation"), dict) and "pixel_sha256" in ev["observation"]
    )
    session.close()
    return ReplayReport(
        ok=not mismatches,
        events=len(events),
        images_checked=images,
        mismatches=mismatches,
        skipped=skipped,
    )


def _settings(provenance: Any) -> Any:
    """Provenance without library versions, as JSON would store it (tuples become lists).
    Versions may change while the pixels stay the same; the pixel hashes check those."""
    if not isinstance(provenance, Mapping):
        return provenance
    return json.loads(json.dumps({k: v for k, v in provenance.items() if k != "versions"}))
