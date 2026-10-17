"""The repository holds no internal paths, secrets or slide data (M9, scripts/check_repo.py)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def check_repo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_repo", REPO / "scripts" / "check_repo.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_git
@pytest.mark.skipif(not (REPO / ".git").exists(), reason="needs a git checkout")
def test_the_tracked_files_are_clean() -> None:
    assert check_repo().check_tree() == []


@needs_git
def test_what_the_history_check_finds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*args: str, who: str = "Dev <1+dev@users.noreply.github.com>") -> None:
        name, _, mail = who.partition(" <")
        ident = ["-c", f"user.name={name}", "-c", f"user.email={mail.rstrip('>')}"]
        subprocess.run(["git", *ident, *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    # The planted values are joined at run time, so this file itself stays clean.
    (tmp_path / "notes.txt").write_text("data in /scr" + "atch/alice/slides/\n")
    git("add", ".")
    git("commit", "-q", "-m", "notes", who="Alice <alice" + "@corp.io>")
    (tmp_path / "notes.txt").unlink()  # deleted, but still in the history
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\x00\x01/Use" + b"rs/bob/work/\x00")  # binary
    (tmp_path / "labels.geojson").write_text("{}")
    git("add", "-A")
    git("commit", "-q", "-m", "key sk-" + "proj-abcdefghijklmnopqrstuvwxyz0123")
    monkeypatch.setenv("SLIDETWIN_PRIVATE_PATTERNS", "alice")
    module = check_repo()
    tree = module.check_tree(tmp_path)
    assert "pic.png: slide, image or label data" in tree
    assert "pic.png:1: an absolute path on a shared or personal machine" in tree
    assert "labels.geojson: slide, image or label data" in tree
    history = "\n".join(module.check_history(tmp_path))
    assert "notes.txt (blob" in history  # a deleted file
    assert "an e-mail address" in history  # alice's address
    assert "an Anthropic or OpenAI key" in history  # in a message
    assert "a private pattern" in history
    assert "noreply" not in history  # GitHub's private addresses are fine


def test_the_maintainers_address_is_fine() -> None:
    module = check_repo()
    assert list(module.scan_bytes(b'email: "bdu8us@virginia.edu"\n', "CITATION.cff")) == []
    other = b"email: bob" + b"@virginia.edu\n"  # joined at run time, as above
    assert list(module.scan_bytes(other, "x")) == ["x:1: an e-mail address"]
