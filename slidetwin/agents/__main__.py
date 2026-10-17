"""Run an agent against a SlideTwin server.

    python -m slidetwin.agents serpentine --env http://localhost:7860 --slide cmu1_small
    python -m slidetwin.agents llm --env http://localhost:7860 --transport mcp --slide cmu1_small
    python -m slidetwin.agents llm --backend openai --base-url http://localhost:8000/v1 \\
        --model Qwen/Qwen2.5-VL-7B-Instruct --env http://localhost:7860 --slide cmu1_small

Prints a summary as JSON and, with --out, writes the trajectory (JSONL).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from slidetwin.agents import (
    llm_agent_example,
    random_agent,
    serpentine_agent,
    unconstrained_crop_agent,
)
from slidetwin.agents.client import Env
from slidetwin.agents.common import AgentResult


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m slidetwin.agents", description=__doc__.split("\n")[0]
    )
    p.add_argument("agent", choices=["serpentine", "random", "crop", "llm"])
    p.add_argument("--env", default="http://localhost:7860", help="server base URL")
    p.add_argument("--transport", choices=["rest", "mcp"], default="rest")
    p.add_argument("--slide", required=True, help="slide id (see GET /api/slides)")
    p.add_argument("--viewport", type=int, nargs=2, metavar=("W", "H"))
    p.add_argument("--out", type=Path, help="write the trajectory here")
    p.add_argument("--seed", type=int, default=0, help="random agent")
    p.add_argument("--backend", choices=["anthropic", "openai"], default="anthropic", help="llm")
    p.add_argument(
        "--model", help=f"llm model (anthropic default: {llm_agent_example.DEFAULT_MODEL})"
    )
    p.add_argument(
        "--base-url", help="llm: OpenAI-compatible endpoint, e.g. http://localhost:8000/v1"
    )
    p.add_argument("--effort", default="medium", help="llm (anthropic): low ... max")
    p.add_argument("--max-turns", type=int, default=40, help="llm")
    return p.parse_args(argv)


def make_chat(args: argparse.Namespace) -> llm_agent_example.Chat:
    if args.backend == "anthropic":
        return llm_agent_example.AnthropicChat(
            args.model or llm_agent_example.DEFAULT_MODEL, effort=args.effort
        )
    if not (args.base_url and args.model):
        sys.exit("--backend openai needs --base-url and --model")
    return llm_agent_example.OpenAIChat(
        args.base_url, args.model, api_key=os.environ.get("OPENAI_API_KEY")
    )


async def run(args: argparse.Namespace) -> AgentResult:
    chat = make_chat(args) if args.agent == "llm" else None
    url = args.env.rstrip("/")
    env = Env.rest(url) if args.transport == "rest" else Env.mcp(f"{url}/mcp")
    viewport = list(args.viewport) if args.viewport else None
    async with env:
        if args.agent == "serpentine":
            return await serpentine_agent.run(env, args.slide, viewport_px=viewport)
        if args.agent == "random":
            return await random_agent.run(env, args.slide, seed=args.seed, viewport_px=viewport)
        if args.agent == "crop":
            return await unconstrained_crop_agent.run(env, args.slide, viewport_px=viewport)
        assert chat is not None
        return await llm_agent_example.run(
            env, args.slide, chat, viewport_px=viewport, max_turns=args.max_turns
        )


def main(argv: list[str] | None = None) -> None:
    args = parse(argv)
    try:
        result = asyncio.run(run(args))
    except Exception as err:  # a missing key, an unreachable server: one line, not a traceback
        if os.environ.get("SLIDETWIN_DEBUG"):
            raise
        message = str(err).strip().splitlines()[0] if str(err).strip() else ""
        detail = f"{type(err).__name__}: {message[:300]}"
        sys.exit(f"slidetwin agents: {detail} (SLIDETWIN_DEBUG=1 shows the traceback)")
    if args.out:
        args.out.write_text(result.trajectory, encoding="ascii")
    receipt = result.receipt or {}
    summary = {
        "session_id": result.session_id,
        "accepted": receipt.get("accepted"),
        "coverage": result.coverage,
        "violations": receipt.get("violations"),
        "notes": result.notes,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
