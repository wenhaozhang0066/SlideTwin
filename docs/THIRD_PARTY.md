# Third-party software and licences

SlideTwin is Apache-2.0. These are its direct dependencies (versions as tested on 2026-10-16).
The Docker image also carries `/usr/share/slidetwin/THIRD_PARTY_NOTICES.txt`: the licence files of
every package it installs, transitive ones included, written by `scripts/third_party_notices.py`.

| Package | Version | Licence | Installed by | Notes |
|---|---|---|---|---|
| fastapi | 0.143 | MIT | base | |
| uvicorn | 0.54 | BSD-3-Clause | base | |
| mcp | 2.3 | MIT | base | |
| httpx2 | 2.13 | BSD-3-Clause | base | |
| jsonschema | 4.26 | MIT | base | |
| pydantic | 2.14 | MIT | base | |
| numpy | 2.5 | BSD-3-Clause and others (0BSD, MIT, Zlib, CC0-1.0) | base | its wheel bundles OpenBLAS (BSD-3-Clause), libgfortran (GPL-3.0 with the GCC Runtime Library Exception) and libquadmath (LGPL-2.1-or-later), with notices that point to their sources |
| pillow | 12.3 | MIT-CMU | base | |
| pyyaml | 6.0 | MIT | base | |
| scipy | 1.18 | BSD-3-Clause | base | bundles the same runtime libraries as numpy, with the same notices |
| tiffslide | 4.0 | BSD-3-Clause | base | |
| tifffile | 2026.9 | BSD-3-Clause | base | |
| imagecodecs | 2026.10 | BSD-3-Clause | base | bundles codec libraries; their licences are in the package |
| zarr | 3.4 | MIT | base | needs google-crc32c (Apache-2.0), whose wheel bundles the CRC32C library (BSD-3-Clause): `third_party/crc32c-NOTICES.txt` |
| wsidicom | 0.36 | Apache-2.0 | base | |
| pydicom | 3.0 | MIT | base | |
| czifile | 2026.10 | BSD-3-Clause | base | |
| openslide-python | 1.4.6 | LGPL-2.1-only | extra `openslide` | the Docker image pins it and carries its sdist (it holds compiled LGPL code) |
| openslide-bin | 4.0.1.2 | LGPL-2.1-only and the licences of the libraries it bundles | extra `openslide` | the Docker image pins it and carries the source archive of that release |
| slideio | 2.10.0 | BSD-3-Clause | extra `slideio` | its wheel bundles native libraries without their notices, so the public image leaves it out |
| pyisyntax | 0.1.7 | MIT | extra `isyntax` | bundles libisyntax (BSD-2-Clause) with stb, yxml and ltalloc: `third_party/isyntax-NOTICES.txt` |
| anthropic | 1.15 | MIT | extra `llm` | the LLM baseline agent only |
| playwright | 1.64 | Apache-2.0 | extra `e2e` | browser tests only |

- Installing an extra on your own machine downloads the package from PyPI; SlideTwin does not
  redistribute it. The licence duties of redistribution apply to images built from this
  repository: the Docker image includes the notices above and OpenSlide's source.
- Bio-Formats (GPL) and vendor SDKs are not used. Formats without an open reader can be added
  locally with a reader plugin (`slidetwin/readers/plugins.py`), which SlideTwin does not ship.
- Slide data has its own terms: [DATA_LICENSES.md](DATA_LICENSES.md).

## Before publishing the image

The repository and pip installs redistribute nothing. A published Docker image does, and two
points are open (found in the M9 review; the owner decides):

- Rust crates compiled into pydantic-core, rpds-py and cryptography (MIT, Apache-2.0 and
  Unicode-3.0 terms), and the OpenSSL that cryptography links in, come without their notices in
  those wheels. They can be generated from each package's `Cargo.lock` (for example with
  cargo-about) and added to `third_party/`.
- libquadmath (LGPL-2.1-or-later), bundled by the numpy and scipy wheels, is shipped with notices
  that point to the GCC sources but without those sources. Strict compliance needs the matching
  GCC source in the image, or a written offer to provide it.
