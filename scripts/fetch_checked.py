"""Download a file and keep it only if its sha256 matches (used by the Docker build).

python scripts/fetch_checked.py URL SHA256 OUT
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path


def main(argv: list[str]) -> int:
    url, sha256, out = argv
    with urllib.request.urlopen(url, timeout=300) as resp:
        data = resp.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        print(f"sha256 mismatch for {url}", file=sys.stderr)
        return 1
    Path(out).write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
