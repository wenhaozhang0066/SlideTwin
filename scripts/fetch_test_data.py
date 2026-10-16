"""Download the test slides (OpenSlide test data) used by CI and the unit tests.

Usage: python scripts/fetch_test_data.py [--tier N] [--dest data/testdata]

Tier 1 (small, CC0) and tier 2 (larger, CC0) run in CI. Tier 3 (large files, and samples whose
licence is not CC0) is for local checks only. Each file is checked against a pinned sha256 and
skipped when already present; zip archives are checked, then extracted safely.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

BASE = "https://openslide.cs.cmu.edu/download/openslide-testdata/"
DIST = "distributable"  # OpenSlide's label for samples that may be shared but are not CC0


def _item(path: str, sha256: str, tier: int, license: str = "CC0-1.0") -> dict[str, object]:
    return {"path": path, "url": BASE + path, "sha256": sha256, "tier": tier, "license": license}


FILES = [
    # tier 1
    _item(
        "Aperio/CMU-1-Small-Region.svs",
        "ed92d5a9f2e86df67640d6f92ce3e231419ce127131697fbbce42ad5e002c8a7",
        1,
    ),
    _item(
        "Mirax/CMU-1-Saved-1_16.zip",
        "5bf66d612475e30bdc46e0f16e8e88558e02fb1fd3cb83bfd804366462b842bc",
        1,
    ),
    _item(
        "Zeiss/Zeiss-5-SlidePreview-Zstd0.czi",
        "920c10f45b781591d7a46759e692e18e14278c2d41b069b518f81fda0748dd22",
        1,
    ),
    _item(
        "Zeiss/Zeiss-5-SlidePreview-Zstd1-HiLo.czi",
        "fd5cea806a557954e2ff63c2eb0dd23c83fe05d4900aac9d81a4af2a412288b0",
        1,
    ),
    _item(
        "Zeiss/Zeiss-5-SlidePreview-JXR.czi",
        "f15d2ac93d97516feb44d072710e1ede49a8fe13e5ff4b81e661f14757906062",
        1,
    ),
    # tier 2
    _item(
        "Huron/Huron-1.tif", "295506f9872becce44033baa830dfd7a7644d104a832e6a7f73565be990c465d", 2
    ),
    _item(
        "DICOM/Leica-4.zip", "71bd6db778d8565b28aa358d1abce0c55985c149183630c2ed31d1f8757185bb", 2
    ),
    _item(
        "Zeiss/Zeiss-5-JXR.czi",
        "c202ddf7b0bd473cdbe29977aee07c10c207077779485c0b1f876e8c00da77f7",
        2,
    ),
    _item(
        "Argos/Argos-1.avs", "3b68b5be6270344ad1823bb6c74c242d60493be90926002f6cdf6e96ca8ff28d", 2
    ),
    # tier 3
    _item(
        "Hamamatsu/CMU-1.ndpi",
        "edf4a1ccf395c7000ae93ad3b44c07d97043810e00be0c1d167dd09bbe436e46",
        3,
    ),
    _item(
        "Generic-TIFF/CMU-1.tiff",
        "73a7e89bc15576587c3d68e55d9bf92f09690280166240b48ff4b48230b13bcd",
        3,
    ),
    _item(
        "Philips-TIFF/Philips-4.tiff",
        "6a743d3bbf721a63f1fc2920f41ddcca43571346665114c6827e63efe034a0b0",
        3,
    ),
    _item(
        "Ventana/Ventana-1.bif",
        "2eb05cd8f628c36408c3fde09beeeebaaae90181c4f840d5475ec7594bdcbbb6",
        3,
    ),
    _item(
        "Trestle/CMU-1.zip", "d52070d474de8cdaf3a6e2934dff077b0aee6e957c7883b101388770b6910b39", 3
    ),
    _item(
        "DICOM/JP2K-33003-1-v2.zip",
        "e298c9f75656836c540022748b3d729c1488fca620f069fc0bb3886b82e34143",
        3,
        DIST,
    ),
    _item(
        "Leica/Leica-Fluorescence-1.scn",
        "a9037f7d683dc0a95e2e5f33bceee71f0449042e70e6d1421ca580bcd97a023a",
        3,
        DIST,
    ),
    _item(
        "Zeiss/Zeiss-1-Merged.zvi",
        "507a59a0a22e7a51ee8f19f82bdfe9291e10f49dd73d2f552a6d9b5b516fc72a",
        3,
        DIST,
    ),
    _item(
        "Aperio/JP2K-33003-1.svs",
        "6205ccf75a8fa6c32df7c5c04b7377398971a490fb6b320d50d91f7ba6a0e6fd",
        3,
        DIST,
    ),
    _item(
        "Mirax/Mirax2-Fluorescence-2.zip",
        "0a72728e8ba5243f3f38f90b7f60b114678d6477ace0b3d334deb161c850da32",
        3,
        DIST,
    ),
    _item(
        "Olympus/OS-2.zip",
        "0fe056a4e89d5b8a6d7f15b1de3b9cac68f840da07cf3dd738fd31e58851b07f",
        3,
        DIST,
    ),
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def target_of(dest: Path, item: dict[str, object]) -> Path:
    """Where an item lives: the file, or for a zip the folder it is extracted to. Paths mirror
    the OpenSlide test-data tree, except the original single test slide, which keeps its place."""
    rel = str(item["path"])
    if rel == "Aperio/CMU-1-Small-Region.svs":
        return dest / "openslide-testdata" / "CMU-1-Small-Region.svs"
    rel = rel[: -len(".zip")] if rel.endswith(".zip") else rel
    return dest / "openslide-testdata" / rel


def _download(url: str, out: Path, sha256: str) -> bool:
    """Download to a temporary name and keep the file only if its hash matches."""
    tmp = out.with_name(out.name + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetch   {url}")
    try:
        with urllib.request.urlopen(url, timeout=300) as resp, tmp.open("wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
    except (OSError, http.client.HTTPException) as err:  # URLError is an OSError
        tmp.unlink(missing_ok=True)
        print(f"error   {url}: {type(err).__name__}: {err}", file=sys.stderr)
        return False
    digest = sha256_of(tmp)
    if digest != sha256:
        tmp.unlink()
        print(f"error   sha256 mismatch for {url}: {digest}", file=sys.stderr)
        return False
    tmp.replace(out)
    return True


def _extract(archive: Path, folder: Path) -> list[list[object]]:
    """Extract every member under `folder`; members with absolute paths or '..' are refused.
    Returns the extracted files and their sizes, kept in the stamp to check later runs."""
    tmp = folder.with_name(folder.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                name = PurePosixPath(member.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise ValueError(f"unsafe member {member.filename!r} in {archive.name}")
            zf.extractall(tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(folder, ignore_errors=True)
    tmp.replace(folder)
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    return [[p.relative_to(folder).as_posix(), p.stat().st_size] for p in files]


def _extracted_ok(folder: Path, stamp: Path, sha: str) -> bool:
    """The stamp names this archive, and every extracted file is still there at its size."""
    try:
        record = json.loads(stamp.read_text())
        return record["archive_sha256"] == sha and all(
            (folder / rel).is_file() and (folder / rel).stat().st_size == size
            for rel, size in record["files"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def fetch(item: dict[str, object], dest: Path) -> bool:
    """Fetch one item; a failure is reported and leaves no partial or stale file behind."""
    target = target_of(dest, item)
    sha = str(item["sha256"])
    if str(item["path"]).endswith(".zip"):
        stamp = target / ".archive-sha256"
        if _extracted_ok(target, stamp, sha):
            print(f"ok      {target}")
            return True
        archive = target.with_name(target.name + ".zip")
        if not _download(str(item["url"]), archive, sha):
            return False
        try:
            files = _extract(archive, target)
        except (OSError, ValueError, zipfile.BadZipFile) as err:
            print(f"error   {archive.name}: {type(err).__name__}: {err}", file=sys.stderr)
            return False
        finally:
            archive.unlink(missing_ok=True)
        stamp.write_text(json.dumps({"archive_sha256": sha, "files": files}) + "\n")
    else:
        if target.is_file():
            if sha256_of(target) == sha:
                print(f"ok      {target}")
                return True
            target.unlink()  # a stale or damaged copy must not be used by the tests
        if not _download(str(item["url"]), target, sha):
            return False
    print(f"saved   {target}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", default=str(Path(__file__).resolve().parent.parent / "data" / "testdata")
    )
    parser.add_argument("--tier", type=int, default=1, choices=(1, 2, 3))
    args = parser.parse_args(argv)
    dest = Path(args.dest)
    chosen = [item for item in FILES if int(str(item["tier"])) <= args.tier]
    return 0 if all([fetch(item, dest) for item in chosen]) else 1


if __name__ == "__main__":
    sys.exit(main())
