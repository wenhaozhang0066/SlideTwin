#!/usr/bin/env bash
# GitHub Codespaces: show what the demo is doing (server, download, last log lines), for support.
cd "$(dirname "$0")/.."
PORT="${PORT:-7860}"

echo "== server on port $PORT"
for url in "http://127.0.0.1:$PORT/api/health" "http://[::1]:$PORT/api/health"; do
  printf '%s: %s\n' "$url" "$(curl -s -m 3 -g "$url" || echo 'no answer')"
done
if [[ -f logs/server.pid ]]; then
  pid=$(cat logs/server.pid)
  if kill -0 "$pid" 2>/dev/null; then echo "process $pid: running"; else echo "process $pid: gone"; fi
fi
echo "== slides"
cat data/slides/.fetched 2>/dev/null && echo "(download complete for this manifest)" || echo "download not complete"
# Counts only: a slide's file name can carry its diagnosis (tumor_091.tif).
files=$(find data/slides -type f \( -name '*.tif*' -o -name '*.svs' \) 2>/dev/null | wc -l)
parts=$(find data/slides -name '*.part' 2>/dev/null | wc -l)
echo "$files slide files, $parts unfinished downloads, $(du -sm data/slides 2>/dev/null | cut -f1) MB"
echo "== logs/server.log (last 15 lines)"
tail -n 15 logs/server.log 2>/dev/null
echo "== logs/fetch.log (last 5 lines)"
tail -n 5 logs/fetch.log 2>/dev/null
