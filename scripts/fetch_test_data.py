"""Download the small CC0 test slide used by CI and the unit tests.

Usage: python scripts/fetch_test_data.py [--dest data/testdata]

The file is checked against a pinned sha256 and skipped when already present.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

FILES = [
    {
        "path": "openslide-testdata/CMU-1-Small-Region.svs",
        "url": "https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs",
        "sha256": "ed92d5a9f2e86df67640d6f92ce3e231419ce127131697fbbce42ad5e002c8a7",
        "license": "CC0-1.0",
    },
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", default=str(Path(__file__).resolve().parent.parent / "data" / "testdata")
    )
    args = parser.parse_args()
    dest = Path(args.dest)
    for item in FILES:
        target = dest / item["path"]
        if target.exists() and sha256_of(target) == item["sha256"]:
            print(f"ok      {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        print(f"fetch   {item['url']}")
        with urllib.request.urlopen(item["url"], timeout=120) as resp, tmp.open("wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        digest = sha256_of(tmp)
        if digest != item["sha256"]:
            tmp.unlink()
            print(f"error   sha256 mismatch for {item['path']}: {digest}", file=sys.stderr)
            return 1
        tmp.replace(target)
        print(f"saved   {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
