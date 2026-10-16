"""Write the licences of every installed distribution but SlideTwin, and the notices kept in
third_party/, into one file, for an image that redistributes them (M9).

    python scripts/third_party_notices.py --out THIRD_PARTY_NOTICES.txt
"""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path

WORDS = ("licen", "copying", "notice")  # file names that hold licence texts
RULE = "=" * 80


def notices(third_party: Path) -> str:
    parts = [
        "Third-party software installed with SlideTwin in this image, with the licence files each\n"
        "package ships, then notices for code those packages bundle (from third_party/).\n"
    ]
    dists = sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower())
    seen = {"slidetwin"}
    for dist in dists:
        name = dist.metadata["Name"]
        if name.lower() in seen:  # once each, even when two paths lead to one site-packages
            continue
        seen.add(name.lower())
        licence = dist.metadata.get("License-Expression") or ""
        if not licence:
            licence = (dist.metadata.get("License") or "").strip().split("\n")[0][:80]
        parts.append(
            f"\n{RULE}\n{name} {dist.version} ({licence or 'see the files below'})\n{RULE}\n"
        )
        for file in sorted(dist.files or [], key=str):
            if not any(w in file.name.lower() for w in WORDS) or file.suffix in (".py", ".pyc"):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            parts.append(f"--- {file}\n{text.strip()}\n")
    for path in sorted(third_party.glob("*.txt")):
        parts.append(f"\n{RULE}\n{path.name}\n{RULE}\n{path.read_text(encoding='utf-8').strip()}\n")
    return "".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--third-party", type=Path, default=Path(__file__).parents[1] / "third_party")
    args = p.parse_args()
    args.out.write_text(notices(args.third_party), encoding="utf-8")


if __name__ == "__main__":
    main()
