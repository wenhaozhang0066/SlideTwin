#!/usr/bin/env bash
# GitHub Codespaces, before the editor connects (updateContentCommand): install SlideTwin with its
# reader extras into .venv. The slides are downloaded afterwards, in the background (fetch.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -e ".[openslide]"
