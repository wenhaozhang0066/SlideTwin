#!/usr/bin/env bash
# Run the Docker image and use it (M9): the demo's CC0 slide downloaded at start, a REST session,
# the readers it carries and leaves out, the OpenSlide source archive and the notices.
#
#   docker build -t slidetwin . && bash scripts/smoke_docker.sh slidetwin
set -euo pipefail

image=$1
name="slidetwin-smoke-$$"
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" -p "127.0.0.1:$port:7860" -e SLIDETWIN_FETCH_ONLY=cmu1_small "$image"
for _ in $(seq 1 120); do
  if curl -fs "http://127.0.0.1:$port/api/health" >/dev/null; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$port/api/health"
echo
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'content-type: application/json' \
  -d '{"slide_id": "cmu1_small", "actor": "agent:smoke"}' "http://127.0.0.1:$port/api/sessions")
[[ $code == 201 ]] || { echo "session: HTTP $code" >&2; docker logs "$name" >&2; exit 1; }

run() { docker exec "$name" "$@"; }
[[ $(run id -u) == 1000 ]] || { echo "the server does not run as the unprivileged user" >&2; exit 1; }
run python -c "import openslide, isyntax"
if run python -c "import slideio" 2>/dev/null; then
  echo "slideio is in the image, but the notices of its bundled libraries are not" >&2
  exit 1
fi
run sh -c 'test -s /usr/share/slidetwin/sources/openslide-bin-*.tar.gz'
run sh -c 'test -s /usr/share/slidetwin/sources/openslide_python-*.tar.gz'
run grep -q '^CRC32C (BSD-3-Clause)' /usr/share/slidetwin/THIRD_PARTY_NOTICES.txt
if run sh -c 'find /app -name __pycache__ | grep -q .'; then  # .dockerignore keeps them out
  echo "compiled files of the build context reached the image" >&2
  exit 1
fi
run grep -q '^openslide-bin ' /usr/share/slidetwin/THIRD_PARTY_NOTICES.txt
run grep -q '^libisyntax (BSD-2-Clause)' /usr/share/slidetwin/THIRD_PARTY_NOTICES.txt
echo "docker smoke test passed"
