#!/usr/bin/env bash
# GitHub Codespaces: download the manifest's demo slides (5.3 GB, sha256-checked) into
# data/slides. Runs after creation (postCreateCommand). start.sh runs it again when an earlier
# download did not finish, since a stopped codespace does not repeat postCreateCommand.
# Never fails the step: a slide that is missing is reported when it is opened.
set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs data/slides
exec 7>logs/.fetch.lock
flock -n 7 || { echo "The slides are already being downloaded."; exit 0; }
stamp=$(sha256sum configs/slides/manifest.yaml | cut -d' ' -f1)
if [[ "$(cat data/slides/.fetched 2>/dev/null)" == "$stamp" ]]; then
  exit 0
fi
if .venv/bin/python -m slidetwin.fetch_slides; then
  echo "$stamp" >data/slides/.fetched
else
  echo "Some slides could not be downloaded; the next start of the codespace retries." >&2
fi
exit 0
