"""Evaluation tools (M6). Ground truth is read here and nowhere else.

    python -m slidetwin.eval convert --xml-dir PATH          # ASAP XML -> data/annotations/
    python -m slidetwin.eval run --out data/eval/RUN --slides c16_fa751c,c16_f606e8
    python -m slidetwin.eval evaluate --out data/eval/RUN      # metrics.json, report.md, figures/
    python -m slidetwin.eval calibrate logs/sessions --fitted fitted.yaml   # cost model (SPEC 8)

Paths default to the repository layout (configs/, data/slides/, cache/).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from slidetwin.core.config import ConfigStore, default_config_dir
from slidetwin.core.errors import SlideNotFound
from slidetwin.core.registry import SlideRegistry
from slidetwin.core.trajectory import load_trajectory
from slidetwin.eval.annotations import parse_asap, to_geojson
from slidetwin.eval.calibrate import fit
from slidetwin.eval.compare import (
    AGENT_PROFILES,
    Paths,
    evaluate_dir,
    markdown,
    run_matrix,
    summary,
)
from slidetwin.eval.metrics import load_truth


def convert(paths: Paths, xml_dir: Path, out: Path) -> list[Path]:
    """Convert the annotation of every manifest slide that names one (eval_only.annotation)."""
    out.mkdir(parents=True, exist_ok=True)
    registry = SlideRegistry.from_manifest(
        paths.manifest, paths.slide_dir, cache_dir=paths.cache_dir
    )
    written = []
    try:
        for slide_id, truth in load_truth(paths.manifest).items():
            name = truth.get("annotation")
            if not name:
                continue
            xml = xml_dir / Path(str(name)).name
            try:
                mpp = registry.get(slide_id).source.mpp
            except SlideNotFound:  # not downloaded: the others are still converted
                print(f"skipped {slide_id}: its slide is not in the slide folder", file=sys.stderr)
                continue
            regions = parse_asap(xml.read_bytes(), mpp)
            target = out / f"{slide_id}.geojson"
            data = to_geojson(regions, slide_id=slide_id, source=Path(str(name)).name)
            target.write_text(json.dumps(data), encoding="utf-8")
            written.append(target)
    finally:
        registry.close()
    return written


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m slidetwin.eval", description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["convert", "run", "evaluate", "calibrate"])
    p.add_argument("logs", nargs="*", type=Path, help="calibrate: trajectory files or folders")
    p.add_argument("--config-dir", type=Path, default=default_config_dir())
    p.add_argument("--manifest", type=Path)
    p.add_argument("--slide-dir", type=Path, default=Path("data/slides"))
    p.add_argument("--cache-dir", type=Path, default=Path("cache"))
    p.add_argument("--xml-dir", type=Path, help="convert: folder with the ASAP XML files")
    p.add_argument("--annotations", type=Path, default=Path("data/annotations"))
    p.add_argument("--out", type=Path, help="run/evaluate: output folder")
    p.add_argument("--slides", help="run: comma-separated slide ids")
    p.add_argument(
        "--agents", default="serpentine,random,crop", help="run: " + ",".join(AGENT_PROFILES)
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--no-pictures", action="store_true")
    p.add_argument("--profile", default="viewer", help="calibrate: the profile whose costs to fit")
    p.add_argument(
        "--max-gap-s", type=float, default=60.0, help="calibrate: longer gaps are breaks"
    )
    p.add_argument("--fitted", type=Path, help="calibrate: write the fitted cost model here")
    args = p.parse_args(argv)
    paths = Paths(
        args.config_dir,
        args.manifest or args.config_dir / "slides" / "manifest.yaml",
        args.slide_dir,
        args.cache_dir,
    )
    if args.command == "calibrate":
        if not args.logs:
            p.error("calibrate needs trajectory files or folders")
        files = sorted(
            f
            for q in args.logs
            for f in ([q] if q.is_file() else q.rglob("*.jsonl"))
            if f.is_file()
        )
        current = ConfigStore(paths.config_dir).profile(args.profile).cost_model.model_dump()
        result = fit((load_trajectory(f) for f in files), current, max_gap_s=args.max_gap_s)
        if not result.sessions:
            p.exit(1, "No human sessions with usable intervals in the given logs.\n")
        print(result.markdown())
        if args.fitted:
            args.fitted.write_text(result.yaml(), encoding="utf-8")
        return
    if args.command == "convert":
        if args.xml_dir is None:
            p.error("convert needs --xml-dir")
        for path in convert(paths, args.xml_dir, args.annotations):
            print(path)
        return
    if args.out is None:
        p.error(f"{args.command} needs --out")
    if args.command == "run":
        if not args.slides:
            p.error("run needs --slides")
        agents = [a for a in args.agents.split(",") if a]
        files = asyncio.run(
            run_matrix(paths, args.out, args.slides.split(","), agents, seed=args.seed)
        )
        print(f"{len(files)} trajectories in {args.out / 'trajectories'}")
        return
    results = evaluate_dir(paths, args.out, args.annotations, pictures=not args.no_pictures)
    report = f"## Summary\n\n{summary(results)}\n## Readings\n\n{markdown(results)}"
    (args.out / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
