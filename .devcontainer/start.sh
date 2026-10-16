#!/usr/bin/env bash
# GitHub Codespaces, on every start and attach (postStartCommand, postAttachCommand): start the
# server in the background unless it is already up, then print its address. With --follow, then
# show the server log.
# Restart it (e.g. after a git pull): kill "$(cat logs/server.pid)"; bash .devcontainer/start.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-7860}"
PY=.venv/bin/python
# The demo's small slide set (0.9 GB); the server and the download both read it.
export SLIDETWIN_MANIFEST="${SLIDETWIN_MANIFEST:-$PWD/configs/slides/demo.yaml}"
if [[ -z "${CODESPACE_NAME:-}" && -r /workspaces/.codespaces/shared/.env ]]; then
  # Some lifecycle shells lack the Codespaces variables; read them without sourcing the file.
  shared=/workspaces/.codespaces/shared/.env
  CODESPACE_NAME=$(sed -n 's/^CODESPACE_NAME=//p' $shared | head -n 1 | tr -d "\"'")
  GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN=$(sed -n \
    's/^GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN=//p' $shared | head -n 1 | tr -d "\"'")
fi
public_host=""
url="http://localhost:$PORT"
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  public_host="$CODESPACE_NAME-$PORT.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  url="https://$public_host"
fi
if [[ ! -x $PY ]]; then
  echo "SlideTwin is not installed yet: run bash .devcontainer/setup.sh" >&2
  exit 1
fi

up() {
  "$PY" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:$PORT/api/health', timeout=2)" \
    >/dev/null 2>&1
}

mkdir -p logs
exec 9>logs/.start.lock
flock 9  # a start and an attach may run at the same time
if ! up; then
  # The forwarding relay rewrites Host, and the Origin of the page's own requests, to
  # localhost:<port>; some requests may carry the forwarded name instead. The host check
  # (DNS-rebinding protection) accepts both and still refuses other origins.
  local_hosts="127.0.0.1,127.0.0.1:*,localhost,localhost:*,[::1],[::1]:*"
  export SLIDETWIN_ALLOWED_HOSTS="${SLIDETWIN_ALLOWED_HOSTS:-$local_hosts${public_host:+,$public_host,$public_host:*}}"
  # Behind the relay all visitors share one address, so this caps them together.
  export SLIDETWIN_RATE_LIMIT_PER_S="${SLIDETWIN_RATE_LIMIT_PER_S:-20}"
  export PORT
  # Fork into a new session with no terminal and closed streams: a background job of this
  # script could be killed by the hangup when the step's terminal closes.
  setsid -f sh -c 'echo $$ >logs/server.pid; trap "" HUP; exec "$0" -m slidetwin.serve' "$PY" \
    >>logs/server.log 2>&1 </dev/null 9>&-
  for _ in $(seq 60); do
    up && break
    sleep 1
  done
fi
if ! up; then
  echo "SlideTwin did not start; see logs/server.log" >&2
  exit 1
fi
echo "SlideTwin is running: $url"
if [[ -n $public_host ]]; then
  echo "Only you can open it. To share it: Ports panel, right-click port $PORT, Port Visibility, Public."
  echo "The port turns private again whenever the codespace restarts."
fi
stamp=$(sha256sum "$SLIDETWIN_MANIFEST" | cut -d' ' -f1)
if [[ ${SLIDETWIN_FETCH_SLIDES:-1} != 0 && "$(cat data/slides/.fetched 2>/dev/null)" != "$stamp" ]]; then
  setsid -f bash .devcontainer/fetch.sh >>logs/fetch.log 2>&1 </dev/null 9>&-
  echo "The demo slides are still downloading (logs/fetch.log); each opens once it is complete."
fi
if [[ ${1:-} == --follow ]]; then
  exec 9>&-
  exec tail -n 0 -F logs/server.log
fi
