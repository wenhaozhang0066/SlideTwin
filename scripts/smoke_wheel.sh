#!/usr/bin/env bash
# Install a built wheel into a fresh environment and use it from outside the repository (M9):
# the command, the packaged configs and demo manifest, the CC0 slide, a served session, MCP over
# HTTP and over stdio, and the training interface.
#
#   python -m build && bash scripts/smoke_wheel.sh dist/slidetwin-*.whl
set -euo pipefail

wheel=$(realpath "$1")
python=${PYTHON:-python3}
work=$(mktemp -d)
server=""
cleanup() {
  if [[ -n $server ]]; then kill "$server" 2>/dev/null || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

"$python" -m venv "$work/venv"
"$work/venv/bin/pip" install --quiet --disable-pip-version-check "$wheel"
cd "$work" # no configs/ here: the configs packaged in the wheel are used
export PATH="$work/venv/bin:$PATH" SLIDETWIN_LOG_DIR="$work/logs" SLIDETWIN_CACHE_DIR=""

slidetwin --version
demo=$(python -c 'from slidetwin.core.config import default_config_dir as d; print(d() / "slides" / "demo.yaml")')
case $demo in "$work"/venv/*) ;; *) echo "the demo manifest is not the packaged one: $demo" >&2; exit 1 ;; esac
slidetwin fetch --demo --only cmu1_small --slides slides
slidetwin check slides/openslide-testdata/CMU-1-Small-Region.svs

port=$(python -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
slidetwin serve --manifest "$demo" --slides slides --port "$port" >serve.log 2>&1 &
server=$!
python - "$port" <<'PY'
import json, sys, time, urllib.request

base = f"http://127.0.0.1:{sys.argv[1]}"


def call(path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"content-type": "application/json", **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(base + path, data, hdrs), timeout=60) as r:
        return r.status, r.read()


for _ in range(120):
    try:
        if call("/api/health")[0] == 200:
            break
    except OSError:
        time.sleep(0.5)
else:
    sys.exit("the server did not start")
assert b"SlideTwin" in call("/")[1]  # the web UI
status, raw = call("/api/sessions", {"slide_id": "cmu1_small", "actor": "agent:smoke"})
assert status == 201, status
sid = json.loads(raw)["session_id"]
assert call(f"/api/sessions/{sid}/magnification", {"mag": 10})[0] == 200
init = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}},
}
status, raw = call("/mcp", init, {"accept": "application/json, text/event-stream"})
assert status == 200 and json.loads(raw)["result"]["serverInfo"]["name"] == "SlideTwin", raw
print("serve: web UI, REST session and MCP over HTTP work")
PY

python - "$demo" <<'PY'
import asyncio, json, sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from slidetwin.env import Env

with Env(sys.argv[1], "slides") as env:
    first = env.reset("cmu1_small")
    assert first.image is not None and first.image.shape == (1024, 1280, 3)
    assert env.step({"action": "set_magnification", "mag": 10}).error is None


async def mcp_stdio():
    params = StdioServerParameters(command="slidetwin", args=["mcp", "--manifest", sys.argv[1], "--slides", "slides"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_slides", {})
            ids = [s["id"] for s in json.loads(result.content[0].text)["slides"]]
            assert "cmu1_small" in ids, ids


asyncio.run(mcp_stdio())
print("Env and MCP over stdio work")
PY
echo "smoke test passed"
