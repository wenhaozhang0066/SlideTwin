"""M1 acceptance: create a session, change magnification, pan, view, export, then replay.

Usage:
    python scripts/demo_m1.py [--slide-id cmu1_small] [--profile viewer]
                              [--slide-dir data/slides] [--out logs/demo_m1]

Exits 0 when the replay reproduces every state, cost and view pixel hash.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from slidetwin.core.config import ConfigStore
from slidetwin.core.images import DiskImageStore
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.replay import replay
from slidetwin.core.session import Session

REPO = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slide-id", default="cmu1_small")
    parser.add_argument("--profile", default="viewer")
    parser.add_argument("--slide-dir", default=str(REPO / "data" / "slides"))
    parser.add_argument("--manifest", default=str(REPO / "configs" / "slides" / "manifest.yaml"))
    parser.add_argument("--out", default=str(REPO / "logs" / "demo_m1"))
    args = parser.parse_args(argv)

    configs = ConfigStore(REPO / "configs")
    registry = SlideRegistry.from_manifest(
        Path(args.manifest), Path(args.slide_dir), cache_dir=REPO / "cache"
    )
    slide = registry.get(args.slide_id)
    session_id = uuid.uuid4().hex
    out = Path(args.out)
    log = out / f"{session_id}.jsonl"
    session = Session(
        session_id=session_id,
        slide_id=args.slide_id,
        source=slide.source,
        slide_sha256=slide.sha256,
        profile=configs.profile(args.profile),
        protocol=configs.protocol("sln_metastasis"),
        actor="agent:demo_m1",
        images=DiskImageStore(out / session_id),
        log_path=log,
    )
    mags = session.available_mags
    steps = [
        session.get_thumbnail(),
        session.set_magnification(10 if 10 in mags else mags[-1]),
        session.pan(0.5, 0),
        session.pan(0, 0.5),
        session.set_magnification(mags[-1]),
        session.view(),
        session.measure([100, 100], [400, 100]),
    ]
    session.export_trajectory()
    session.close()
    for r in [session.initial, *steps]:
        print(f"{r.seq:3d} {r.action:18s} cost {r.cost_s:6.3f} s  elapsed {r.elapsed_s:7.3f} s")

    report = replay(log, registry)
    print(f"log: {log}")
    print(
        f"replay: {'IDENTICAL' if report.ok else 'MISMATCH'} "
        f"({report.events} events, {report.images_checked} images checked)"
    )
    for m in report.mismatches[:10]:
        print(f"  seq {m['seq']}: {m['field']} differs")
    registry.close()
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
