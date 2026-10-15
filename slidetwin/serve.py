"""Container entry point (M7): fetch the slides if asked, then serve with one worker.

Environment:
    SLIDETWIN_FETCH_SLIDES=1   download the manifest's slides first (SLIDETWIN_FETCH_ONLY: some)
    HOST, PORT                 default 0.0.0.0 and 7860 (a container listens on all interfaces)
    FORWARDED_ALLOW_IPS        proxies whose X-Forwarded-For is trusted (rate limits are per client)
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

import uvicorn

from slidetwin import fetch_slides


def main(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    if env.get("SLIDETWIN_FETCH_SLIDES", "0") == "1" and fetch_slides.main([]) != 0:
        print("Some slides could not be fetched; opening them will fail.", file=sys.stderr)
    uvicorn.run(
        "slidetwin.api.main:app",
        host=env.get("HOST", "0.0.0.0"),
        port=int(env.get("PORT", "7860")),
        workers=1,  # sessions live in this process
        proxy_headers=True,
        forwarded_allow_ips=env.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
