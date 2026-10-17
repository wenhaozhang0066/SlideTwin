# Releasing SlideTwin

What is ready for the open-source release, and the steps only the owner can take.

## Ready

- **The package.** A wheel installs and works outside the repository (CI job `package`);
  `twine check --strict` passes; the metadata names the URLs, classifiers and licence.
- **The release workflow** (`.github/workflows/release.yml`). A tag `v<version>` checks that the
  tag names the package version, scans the repository and its history
  (`scripts/check_repo.py --history`), builds and checks the sdist and the wheel, tests an
  installed wheel, and publishes to PyPI through trusted publishing (no token).
- **The Docker image** (CI job `docker`): the notices of every installed package and of the
  Rust crates compiled into them, and the sources the LGPL asks for ([THIRD_PARTY.md](THIRD_PARTY.md)).
- **A clean history.** The private repository's history holds one internal path, in an early
  plan. A copy of `main` with that path rewritten in every commit is prepared outside the
  repository; its history passes the repository check, with the owner's private patterns too,
  and its last commit has exactly the files of `main`. Every commit is authored by the owner's
  GitHub address.

## Steps for the owner

1. **Create a new public repository.** Do not make the private one public: its history keeps the
   commit with the internal path, and GitHub keeps old commits reachable by their id. If its
   owner or name is not `wenhaozhang0066/SlideTwin`, change the URLs (README, pyproject.toml,
   CITATION.cff):
   `git grep -l wenhaozhang0066/SlideTwin | xargs sed -i 's#wenhaozhang0066/SlideTwin#OWNER/NAME#g'`
2. **Push the clean history.** Run the preparation again so that it holds the latest `main`, then
   add the new repository as `origin` and push `main`.
3. **Repository settings.** Actions on; Security > Private vulnerability reporting on
   (SECURITY.md sends reports there); an environment named `pypi` (Settings > Environments) for
   the publish job.
4. **PyPI.** On pypi.org, add a pending trusted publisher for the project `slidetwin`: the
   repository's owner and name, workflow `release.yml`, environment `pypi`.
5. **What the public repository keeps.** `AGENTS.md` (instructions for AI coding tools),
   `docs/SPEC.md` (the specification, in Chinese) and `docs/PLAN_*.md` (milestone plans) hold
   nothing private; keep or remove them.
6. **Citation.** CITATION.cff names "SlideTwin contributors": add the authors and, if wanted, a
   Zenodo DOI.
7. **Release.** Date the release in CHANGELOG.md, tag `v0.1.0` and push the tag.
8. **Optional: the image on a Hugging Face Space.** The Space's README.md starts with
   `deploy/hf_space_readme_header.md` (the Space settings: Docker SDK, port 7860), then the
   repository's README.md. Set the variables `SLIDETWIN_ALLOWED_HOSTS`,
   `SLIDETWIN_RATE_LIMIT_CLIENT_HEADER=x-forwarded-for` and `FORWARDED_ALLOW_IPS=*`. The libquadmath
   sources in the image are those of the GCC releases inferred from the libraries; add a written
   offer if the image must be beyond doubt.
9. **Afterwards.** Archive or delete the private repository, and revoke the tokens used during
   development (GitHub, Hugging Face).
