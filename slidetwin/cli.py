"""The `slidetwin` command (M9).

    slidetwin serve [--demo] [--slides DIR] [--manifest FILE] [--host H] [--port N]
    slidetwin mcp [--slides DIR] [--manifest FILE]        MCP over stdio, for a local agent
    slidetwin check PATH...                               what SlideTwin reads in each slide
    slidetwin manifest DIR [-o FILE]                      a manifest for a folder of slides
    slidetwin replay LOG [--slides DIR] [--manifest FILE] replay a trajectory, report differences
    slidetwin fetch [--demo] [--slides DIR] [--manifest FILE] [--only IDS]

Paths default as for the server: SLIDETWIN_SLIDE_DIR (data/slides), SLIDETWIN_MANIFEST
(<configs>/slides/manifest.yaml), SLIDETWIN_LOG_DIR (logs). --demo uses the demo's public slides.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from slidetwin import __version__


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    code: int = args.run(args)
    return code


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slidetwin", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"slidetwin {__version__}")
    sub = p.add_subparsers(required=True, metavar="command")

    def command(name: str, help_text: str, run: object) -> argparse.ArgumentParser:
        q = sub.add_parser(name, help=help_text, description=help_text)
        q.set_defaults(run=run)
        return q

    def slides(q: argparse.ArgumentParser) -> None:
        q.add_argument("--slides", type=Path, help="slide folder (SLIDETWIN_SLIDE_DIR)")
        q.add_argument("--manifest", type=Path, help="slide manifest (SLIDETWIN_MANIFEST)")

    q = command("serve", "Web UI, REST API and MCP over HTTP.", _serve)
    slides(q)
    q.add_argument("--demo", action="store_true", help="download and serve the demo's slides")
    q.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to listen on every interface")
    q.add_argument("--port", type=int, default=7860)
    q = command("mcp", "MCP over stdio, for a local agent.", _mcp)
    slides(q)
    q = command("check", "What SlideTwin reads in each slide; nothing is served.", _check)
    q.add_argument("paths", nargs="+", type=Path)
    q.add_argument("--hash", action="store_true", help="also print the content hash")
    q = command("manifest", "A manifest for a folder of slides.", _manifest)
    q.add_argument("folder", type=Path)
    q.add_argument("-o", "--output", type=Path, help="write it here instead of stdout")
    q = command("replay", "Replay a trajectory and report differences.", _replay)
    slides(q)
    q.add_argument("log", type=Path)
    q = command("fetch", "Download a manifest's public slides (sha256-checked).", _fetch)
    slides(q)
    q.add_argument("--demo", action="store_true", help="the demo's slides")
    q.add_argument("--only", default="", help="comma-separated slide ids")
    return p


def _demo_manifest() -> Path:
    from slidetwin.core.config import default_config_dir

    return default_config_dir() / "slides" / "demo.yaml"


def _environ(args: argparse.Namespace) -> None:
    """The command's slide options as the server's environment variables."""
    if getattr(args, "demo", False):
        os.environ["SLIDETWIN_MANIFEST"] = str(_demo_manifest())
    if args.manifest is not None:
        os.environ["SLIDETWIN_MANIFEST"] = str(args.manifest)
    if args.slides is not None:
        os.environ["SLIDETWIN_SLIDE_DIR"] = str(args.slides)


def _serve(args: argparse.Namespace) -> int:
    from slidetwin import serve

    _environ(args)
    os.environ["HOST"], os.environ["PORT"] = args.host, str(args.port)
    if args.demo:
        os.environ["SLIDETWIN_FETCH_SLIDES"] = "1"
    print(f"SlideTwin {__version__}: http://{args.host}:{args.port}", file=sys.stderr)
    serve.main()
    return 0


def _mcp(args: argparse.Namespace) -> int:
    from slidetwin.api.main import build_manager
    from slidetwin.api.settings import Settings
    from slidetwin.mcp_server.server import build_mcp
    from slidetwin.readers import quiet_library_logs

    _environ(args)
    quiet_library_logs()  # stdout carries the protocol; nothing else may print there
    build_mcp(build_manager(Settings.from_env())).run("stdio")
    return 0


def _check(args: argparse.Namespace) -> int:
    from slidetwin.scan import check

    refused = 0
    for path in args.paths:
        result = check(path, name=path.name, hash_files=args.hash)
        refused += result.refused is not None
        print("\n".join(result.lines()))
    return 1 if refused else 0


def _manifest(args: argparse.Namespace) -> int:
    from slidetwin.scan import manifest, manifest_yaml

    if not args.folder.is_dir():
        print(f"slidetwin manifest: {args.folder} is not a folder", file=sys.stderr)
        return 2
    data, refused = manifest(args.folder)
    text = manifest_yaml(data, refused)
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    print(f"{len(data['slides'])} slides, {len(refused)} not served", file=sys.stderr)
    return 0


def _replay(args: argparse.Namespace) -> int:
    from slidetwin.api.settings import Settings
    from slidetwin.core.errors import SlideTwinError
    from slidetwin.core.registry import SlideRegistry
    from slidetwin.core.replay import replay

    _environ(args)
    settings = Settings.from_env()
    registry = SlideRegistry.from_manifest(
        settings.manifest, settings.slide_dir, cache_dir=settings.cache_dir
    )
    try:
        report = replay(args.log, registry)
    except SlideTwinError as err:
        print(f"replay refused: {err.message}", file=sys.stderr)
        return 1
    finally:
        registry.close()
    print(
        f"{report.events} events, {report.images_checked} images checked, "
        f"{len(report.mismatches)} differences"
    )
    for m in report.mismatches[:20]:
        print(f"  seq {m.get('seq')}: {m.get('field')}")
    return 0 if report.ok else 1


def _fetch(args: argparse.Namespace) -> int:
    from slidetwin import fetch_slides

    _environ(args)  # fetch_slides reads its defaults from the environment
    return fetch_slides.main(["--only", args.only] if args.only else [])


if __name__ == "__main__":
    sys.exit(main())
