# Releasing SlideTwin

SlideTwin is published on PyPI as `slidetwin` by `.github/workflows/release.yml`, through trusted
publishing: PyPI trusts that workflow when it runs in the GitHub environment `pypi`, so no token is
stored anywhere.

## A new version

1. Set the version in `slidetwin/__init__.py` and date its section in CHANGELOG.md.
2. Commit, push to `main` and wait for CI.
3. Tag the commit and push the tag:
   `git tag -a vX.Y.Z -m "SlideTwin X.Y.Z" && git push origin vX.Y.Z`
4. Once the workflow has published, add a GitHub release for the tag with the CHANGELOG section.

Before it publishes, the workflow checks that the tag names the package version, scans the
repository and its history (`scripts/check_repo.py --history`), builds the sdist and the wheel,
checks them with `twine check --strict` and tests the installed wheel outside the repository.
PyPI accepts each version only once: a broken release is fixed by the next version.

## Other ways to run it

- **Docker.** The image is not published; build it from the repository (README, Deployment). It
  carries the licence notices and the sources that redistribution asks for
  ([THIRD_PARTY.md](THIRD_PARTY.md)).
- **Hugging Face Space** (optional). The Space's README.md starts with
  `deploy/hf_space_readme_header.md` (Docker SDK, port 7860), then the repository's README.md. Set
  the variables `SLIDETWIN_ALLOWED_HOSTS`, `SLIDETWIN_RATE_LIMIT_CLIENT_HEADER=x-forwarded-for` and
  `FORWARDED_ALLOW_IPS=*`.
