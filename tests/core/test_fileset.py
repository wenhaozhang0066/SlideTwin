"""File sets (M8): completeness, safe relative paths and one canonical hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from slidetwin.core.errors import ConfigError, SlideMetadataError, SlideNotFound
from slidetwin.core.fileset import (
    FILESET_VERSION,
    check_complete,
    check_relative,
    fileset_sha256,
)
from slidetwin.core.registry import file_sha256


@pytest.fixture
def slide(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "slide.mrxs").write_bytes(b"preview")
    (tmp_path / "data" / "Index.dat").write_bytes(b"index")
    (tmp_path / "data" / "Data0000.dat").write_bytes(b"tiles")
    return tmp_path


FILES = ["slide.mrxs", "data/Index.dat", "data/Data0000.dat"]


def test_hash_is_canonical_and_order_free(slide: Path) -> None:
    digest = fileset_sha256(slide, FILES, file_sha256)
    assert digest == fileset_sha256(slide, list(reversed(FILES)), file_sha256)
    lines = [FILESET_VERSION] + [
        json.dumps([rel, (slide / rel).stat().st_size, file_sha256(slide / rel)])
        for rel in sorted(FILES, key=lambda r: r.encode())
    ]
    assert digest == hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()
    (slide / "data" / "Data0000.dat").write_bytes(b"tileZ")  # same size, other content
    assert fileset_sha256(slide, FILES, file_sha256) != digest


def test_unicode_names_hash(tmp_path: Path) -> None:
    (tmp_path / "切片 1.dat").write_bytes(b"x")
    assert len(fileset_sha256(tmp_path, ["切片 1.dat"], file_sha256)) == 64


@pytest.mark.parametrize("bad", ["", "/etc/passwd_DOE", "../up_DOE.dat", "a/../../DOE", "DOE\\b"])
def test_unsafe_paths_are_refused_without_echo(bad: str) -> None:
    with pytest.raises(SlideMetadataError, match="names 1 file") as err:
        check_relative(["ok.dat", bad], "s1")
    assert "DOE" not in err.value.message


def test_links_out_of_the_folder_are_refused(
    slide: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "secret.dat"
    elsewhere.write_bytes(b"other slide")
    (slide / "data" / "Data0000.dat").unlink()
    (slide / "data" / "Data0000.dat").symlink_to(elsewhere)
    with pytest.raises(SlideMetadataError, match="1 file\\(s\\) that link outside") as err:
        check_complete(slide, FILES, "s1")
    assert "secret" not in err.value.message
    (slide / "data" / "Data0000.dat").unlink()
    (slide / "data" / "inside.dat").write_bytes(b"tiles")
    (slide / "data" / "Data0000.dat").symlink_to(slide / "data" / "inside.dat")
    assert check_complete(slide, FILES, "s1")  # a link within the folder is fine


def test_empty_set_is_refused() -> None:
    with pytest.raises(ConfigError):
        check_relative([])


def test_completeness_counts_but_never_names(slide: Path) -> None:
    assert check_complete(slide, FILES, "s1") == tuple(sorted(FILES, key=lambda r: r.encode()))
    (slide / "data" / "Index.dat").unlink()
    with pytest.raises(SlideNotFound, match="1 of its 3 files are missing") as err:
        check_complete(slide, FILES, "s1")
    assert "Index" not in err.value.message
