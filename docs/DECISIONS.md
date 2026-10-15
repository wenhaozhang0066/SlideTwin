# Decision log

Every milestone adds its decisions here: what was decided, why, and the alternatives considered.
Pathology rules are never invented. Uncertain items are marked `draft` in the YAML configs and listed
under "Needs pathologist review" below.

## Needs pathologist review

All of these are `status: draft` until a pathologist confirms them. Record the reviewer and date when
confirmed.

| Item | Current draft value | Where |
|---|---|---|
| Magnification presets and keyboard keys | 1.25, 2.5, 5, 10, 20, 40x on keys 1-6 | `configs/profiles/*.yaml` |
| Magnification-to-resolution convention | `mpp(m) = 0.25 * 40 / m` um/px | profiles |
| Tolerance for offering a magnification | 0.06 (a nominal 40x scan at up to 0.266 um/px still offers 40x) | profiles |
| General reading habits R-G1 to R-G5 | as in SPEC 5.1 | `configs/protocols/sln_metastasis.yaml` |
| T1 thresholds | macro > 2 mm; micro > 0.2-2 mm and/or > 200 cells; ITC <= 0.2 mm and <= 200 cells. Which wording of the 200-cell boundary applies is open | protocol |
| Measurement rule | largest contiguous deposit only; satellites excluded; extranodal extension included | protocol |
| Negative coverage requirement | >= 98% of tissue seen at >= 10x, 32 um grid | protocol |
| Minimum pixels across a measurement | 20 px | protocol |
| Minimum magnification for positive evidence | 20x | protocol |
| Cost-model values | SPEC 8 placeholders; to be calibrated from pathologist logs (M7) | profiles |

## Needs user decision

| Item | Status |
|---|---|
| GitHub owner and visibility | private repository under the owner's account; waiting for a token |
| Hugging Face user or org and Space name | needed in M7 |
| CAMELYON license reading | stricter reading applied (see DATA_LICENSES.md) until confirmed |

## M0: scaffold (2026-10-14)

- **Repository.** `docs/SPEC.md` is the specification and `AGENTS.md` holds the working rules. Code,
  comments and docs are in English. `README_zh.md` and the UI strings are the only Chinese files, as SPEC
  requires.
- **Python 3.11+.** CI tests 3.11 and 3.12. The newest tiffslide (4.0), numpy (2.5) and tifffile need
  3.12; on 3.11 pip resolves tiffslide 3.0.x. The development venv and the Docker image use 3.12.
  - Alternative: require 3.12. Rejected because SPEC says 3.11+.
- **Build and tooling.**
  - hatchling, with the version taken from `slidetwin/__init__.py`.
  - ruff for lint and format; mypy strict on the whole package.
  - pytest with hypothesis.
  - A 100% line-and-branch coverage gate on `slidetwin/core`, as SPEC 6.2 requires. CI and
    `scripts/ci_local.sh` enforce it.
- **Test data.**
  - CI downloads only the CC0 `CMU-1-Small-Region.svs` (1.9 MB) and checks its sha256.
  - Tests that need it skip locally when it is missing. With `SLIDETWIN_REQUIRE_TESTDATA=1`, as in CI, a
    missing file fails the run instead.
  - Tests that need CAMELYON slides run only when `SLIDETWIN_CAMELYON_DIR` is set.
- **Versioning in logs.** `slidetwin.version.git_sha()` reads `SLIDETWIN_GIT_SHA` (set at Docker build
  time) or asks git, adding `-dirty` when tracked files have uncommitted changes.
- **Docker.**
  - `python:3.12-slim`, one uvicorn worker (sessions live in memory), uid 1000 as Hugging Face requires.
  - Slides are mounted or downloaded at runtime and never baked in.
  - The image is not built on the development cluster, which has no docker or apptainer. It is built
    and checked in M7.
- **Secrets.**
  - Tokens are read from environment variables only. `.gitignore` excludes `data/`, `cache/`, `logs/`,
    `.env`, `*.env`, virtual environments and whole-slide image formats.
  - Commits carry only the owner's identity.
- **License.** Apache-2.0, as SPEC suggests. The owner stated no preference. It can still be changed before
  the repository becomes public.
