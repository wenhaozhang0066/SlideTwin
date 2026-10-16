"""Actions per second of the training interface (M9): one Env, then VecEnv with n workers.

    python scripts/bench_env.py --slide c16_fa751c --workers 1,8 --steps 200

Each environment runs the same fixed reading: 10x, then rows of half-viewport pans with a switch
to 20x and 40x now and then. Only the steps are timed, not reset (which opens the slide).
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

from slidetwin.env import Env, VecEnv


def reading(steps: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"action": "set_magnification", "mag": 10}]
    direction = 1
    while len(out) < steps:
        for _ in range(12):
            out.append({"action": "pan", "dx": 0.5 * direction, "dy": 0})
        out.append({"action": "pan", "dx": 0, "dy": 0.5})
        out.append({"action": "set_magnification", "mag": 20})
        out.append({"action": "set_magnification", "mag": 40})
        out.append({"action": "set_magnification", "mag": 10})
        direction = -direction
    return out[:steps]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--manifest", default="configs/slides/manifest.yaml")
    p.add_argument("--slide-dir", default="data/slides")
    p.add_argument("--slide", required=True)
    p.add_argument("--workers", default="1,8", help="comma-separated; 1 means a plain Env")
    p.add_argument("--steps", type=int, default=200)
    p.add_argument("--image", default="array", choices=["array", "jpeg"])
    args = p.parse_args()
    actions = reading(args.steps)
    kw: dict[str, Any] = {"image": args.image, "cache_dir": os.environ.get("SLIDETWIN_CACHE_DIR")}
    print(f"slide {args.slide}, {args.steps} steps per environment, image={args.image}")
    for n in (int(v) for v in args.workers.split(",")):
        if n == 1:
            with Env(args.manifest, args.slide_dir, **kw) as env:
                env.reset(args.slide)
                start = time.perf_counter()
                for action in actions:
                    assert env.step(action).error is None
                seconds = time.perf_counter() - start
        else:
            with VecEnv(n, manifest=args.manifest, slide_dir=args.slide_dir, **kw) as vec:
                vec.reset([args.slide] * n)
                start = time.perf_counter()
                for action in actions:
                    assert all(s.error is None for s in vec.step([action] * n))
                seconds = time.perf_counter() - start
        rate, per_step_ms = n * len(actions) / seconds, seconds / len(actions) * 1000
        print(f"{n} worker(s): {rate:.1f} actions/s ({per_step_ms:.0f} ms per step)")


if __name__ == "__main__":
    main()
