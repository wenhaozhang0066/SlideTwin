# Contributing

SlideTwin is a research prototype. Issues and pull requests are welcome.

## Set up

```bash
pip install -e ".[dev]"
python scripts/fetch_test_data.py --tier 2   # the CC0 test slides, about 300 MB
bash scripts/ci_local.sh                     # the checks CI runs
```

## Rules for a change

- Tests come with it. `slidetwin/core`, `slidetwin/readers`, the training interface and the
  command line keep 100% branch coverage.
- No slide, annotation, patient or company data in the repository, the tests or screenshots.
  Generate fixtures in the tests, or download public samples with a pinned sha256
  (`scripts/fetch_test_data.py`). `python scripts/check_repo.py` finds internal paths, keys and
  data files.
- Errors and logs name the slide id only, never a file name or a path: a file name can carry a
  label.
- Pathology rules live in `configs/protocols/*.yaml` with `status: draft` or `confirmed`; code
  never encodes a pathology rule.
- A new format needs a reader in `slidetwin/readers` (see "Slide readers" in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)) checked on real samples; what cannot be checked
  stays refused or experimental. For a format only you need, write a reader plugin
  (`slidetwin/readers/plugins.py`).
- A change of behaviour is recorded in [docs/DECISIONS.md](docs/DECISIONS.md).
- Code, comments and docs are in English (README_zh.md and the UI's Chinese strings excepted).

## Commits

A short subject with a scope, such as `fix(readers): ...`; the body says what changed and why.
