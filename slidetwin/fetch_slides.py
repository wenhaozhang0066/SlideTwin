"""Download the manifest's slides before the server starts (M7, SPEC 14).

    python -m slidetwin.fetch_slides [--only id1,id2] [--manifest PATH] [--slide-dir DIR]

Each slide with a `source_url` is fetched into the slide folder at its manifest path. A file that is
already there is kept when its sha256 matches the pin; a download goes to a temporary name and is
renamed only after its hash matches. Slides without a URL (local or internal ones) are skipped.
Progress goes to stderr. Exit status 1 when a slide could not be fetched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import httpx2 as httpx
import yaml

from slidetwin.core.config import default_config_dir
from slidetwin.core.registry import SlideEntry, file_sha256, parse_manifest

CHUNK = 8 << 20
ATTEMPTS = 4
RETRY_PAUSE_S = 10.0  # times the attempt number


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def fetch_one(entry: SlideEntry, slide_dir: Path, client: httpx.Client) -> bool:
    """True when the slide is in place with the pinned hash (or no pin) afterwards. Any failure,
    a read-only folder included, is reported and leaves no partial file; it never raises. A
    network error or a server error (5xx) is tried again after a growing pause."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _fetch(entry, slide_dir, client)
        except (httpx.HTTPError, OSError) as err:
            _say(f"{entry.id}: failed ({type(err).__name__}: {err})")
            if attempt == ATTEMPTS or not _passing(err):
                return False
            time.sleep(RETRY_PAUSE_S * attempt)
    return False  # not reached


def _passing(err: Exception) -> bool:
    """Whether trying again may help: a network error or a server error, not a 4xx or a disk."""
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code >= 500
    return isinstance(err, httpx.TransportError)


def _fetch(entry: SlideEntry, slide_dir: Path, client: httpx.Client) -> bool:
    target = slide_dir / entry.path
    if target.is_file() and (entry.sha256 is None or file_sha256(target) == entry.sha256):
        _say(f"{entry.id}: present")
        return True
    if entry.source_url is None:
        _say(f"{entry.id}: missing and no source_url; skipped")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    digest = hashlib.sha256()
    done = 0
    started = time.monotonic()
    try:
        with client.stream("GET", entry.source_url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            next_note = 0.0
            with partial.open("wb") as f:
                for chunk in resp.iter_bytes(CHUNK):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total and done / total >= next_note:
                        rate = done / max(time.monotonic() - started, 1e-6) / 1e6
                        share = f"{done / total:6.1%} of {total / 1e9:.2f} GB"
                        _say(f"{entry.id}: {share}, {rate:.0f} MB/s")
                        next_note += 0.1
        if entry.sha256 is not None and digest.hexdigest() != entry.sha256:
            _say(f"{entry.id}: sha256 does not match the manifest; file discarded")
            return False
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)  # after a failure; after os.replace it is already gone
    _say(f"{entry.id}: done in {time.monotonic() - started:.0f} s")
    return True


def fetch_all(
    entries: Iterable[SlideEntry], slide_dir: Path, client: httpx.Client | None = None
) -> bool:
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0))
    try:
        return all([fetch_one(e, slide_dir, client) for e in entries])
    finally:
        if own:
            client.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m slidetwin.fetch_slides", description=__doc__.split("\n")[0]
    )
    config_dir = default_config_dir()
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            os.environ.get("SLIDETWIN_MANIFEST", str(config_dir / "slides" / "manifest.yaml"))
        ),
    )
    p.add_argument(
        "--slide-dir", type=Path, default=Path(os.environ.get("SLIDETWIN_SLIDE_DIR", "data/slides"))
    )
    p.add_argument(
        "--only", default=os.environ.get("SLIDETWIN_FETCH_ONLY", ""), help="comma-separated ids"
    )
    args = p.parse_args(argv)
    entries = parse_manifest(
        yaml.safe_load(args.manifest.read_text(encoding="utf-8")), args.manifest.name
    )
    wanted = {s for s in args.only.split(",") if s}
    unknown = wanted - {e.id for e in entries}
    if unknown:
        _say(f"unknown slide ids: {', '.join(sorted(unknown))}")
        return 1
    chosen = [e for e in entries if not wanted or e.id in wanted]
    return 0 if fetch_all(chosen, args.slide_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
