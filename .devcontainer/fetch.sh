#!/usr/bin/env bash
# GitHub Codespaces: download the demo's slides (configs/slides/demo.yaml, 0.9 GB, sha256-checked,
# or the manifest SLIDETWIN_MANIFEST names) into data/slides, in the background. start.sh runs it
# after starting the server, and again on later starts until a download has finished. Never fails:
# a missing slide is reported when it is opened.
set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs data/slides
exec 7>logs/.fetch.lock
flock -n 7 || { echo "The slides are already being downloaded."; exit 0; }
manifest="${SLIDETWIN_MANIFEST:-configs/slides/demo.yaml}"
stamp=$(sha256sum "$manifest" | cut -d' ' -f1)
if [[ "$(cat data/slides/.fetched 2>/dev/null)" == "$stamp" ]]; then
  exit 0
fi
if .venv/bin/python -m slidetwin.fetch_slides --manifest "$manifest"; then
  echo "$stamp" >data/slides/.fetched
else
  echo "Some slides could not be downloaded; the next start of the codespace retries." >&2
fi
exit 0
