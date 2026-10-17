"""Write the licence notices of the Rust crates compiled into installed packages (M9, for an image
that redistributes those packages; their wheels do not carry the crates' notices).

    python scripts/rust_notices.py --out RUST_NOTICES.txt

Each installed package whose compiled modules hold Rust code is looked up on PyPI: its sdist (sha256
checked) has the Cargo.lock that pins every crate. Crates whose source paths the modules hold
(those of the Rust standard library, for one) are added. Each crate comes from crates.io (sha256
checked), and its licence files and its Cargo.toml licence field are written out. The lock file
names every crate the build may use (also build tools and other platforms' crates), so the notices
cover more than the modules hold, never less. A package with Rust code but no Cargo.lock in its
sdist fails the run, unless it is in BUNDLED (prebuilt libraries whose licences it ships itself).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import re
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

# Packages whose Rust code is a prebuilt library they bundle, with that library's licence files
# in their own licence folder (scripts/third_party_notices.py writes those out).
BUNDLED = {
    "imagecodecs": "pcodec and rav1e, whose licences (and rav1e's patent grant) imagecodecs ships",
}
STD = (
    "Every module listed here also holds the Rust standard library (MIT OR Apache-2.0,\n"
    "https://github.com/rust-lang/rust/blob/master/COPYRIGHT); the crates it is built from appear\n"
    "under the packages whose modules name them.\n"
)
CRATE_PATH = re.compile(rb"([A-Za-z0-9_-]+)-(\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)/src/")
LICENCE_FILE = re.compile(r"^(?:licen[cs]e|copying|notice|copyright|unlicense)", re.I)
OPENSSL = re.compile(rb"OpenSSL \d+\.\d+\.\d+[a-z]?")
RULE = "=" * 80


def fetch(url: str, sha256: str | None = None) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        data: bytes = resp.read()
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"sha256 mismatch for {url}")
    return data


def rust_packages() -> list[
    tuple[importlib.metadata.Distribution, str | None, set[tuple[str, str]]]
]:
    """Installed distributions with a compiled module that holds Rust code, each with the OpenSSL
    version its modules link in statically (None if none) and the crates whose source paths the
    modules hold."""
    out, seen = [], set()
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"].lower()
        if name in seen:
            continue
        seen.add(name)
        rust, openssl, paths = False, None, set()
        for file in dist.files or []:
            if file.suffix == ".so" or ".so." in file.name:
                try:
                    data = Path(str(dist.locate_file(file))).read_bytes()
                except OSError:
                    continue
                if b"/rustc/" in data:
                    rust = True
                    paths |= {(n.decode(), v.decode()) for n, v in CRATE_PATH.findall(data)}
                found = OPENSSL.search(data)
                openssl = openssl or (found.group().decode() if found else None)
        if rust:
            out.append((dist, openssl, paths))
    return sorted(out, key=lambda item: item[0].metadata["Name"].lower())


def cargo_lock(name: str, version: str) -> dict[str, object]:
    """The Cargo.lock in the package's sdist on PyPI."""
    meta = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
    sdist = next((u for u in meta["urls"] if u["packagetype"] == "sdist"), None)
    if sdist is None:
        raise RuntimeError(f"{name} {version} has no sdist on PyPI")
    data = fetch(sdist["url"], sdist["digests"]["sha256"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        locks = sorted(
            (m for m in archive.getmembers() if m.name.endswith("/Cargo.lock")),
            key=lambda m: m.name.count("/"),
        )
        if not locks:
            raise RuntimeError(f"{name} {version}: no Cargo.lock in its sdist")
        handle = archive.extractfile(locks[0])
        assert handle is not None
        return tomllib.loads(handle.read().decode("utf-8"))


def registry_checksum(name: str, version: str) -> str | None:
    """The sha256 crates.io gives for a crate version, or None when there is no such crate (a
    path in a binary that only looks like one)."""
    try:
        meta = json.loads(fetch(f"https://crates.io/api/v1/crates/{name}/{version}"))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    checksum: str = meta["version"]["checksum"]
    return checksum


def crate_notice(name: str, version: str, checksum: str) -> str:
    data = fetch(f"https://static.crates.io/crates/{name}/{name}-{version}.crate", checksum)
    top = f"{name}-{version}/"
    licence, texts = "", []
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            rel = member.name.removeprefix(top)
            if not member.isfile() or "/" in rel:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            if rel == "Cargo.toml":
                manifest = tomllib.loads(handle.read().decode("utf-8", "replace"))
                licence = str(manifest.get("package", {}).get("license", ""))
            elif LICENCE_FILE.match(rel):
                texts.append(f"--- {rel}\n{handle.read().decode('utf-8', 'replace').strip()}\n")
    head = f"{name} {version} ({licence or 'no licence field'})\n"
    missing = (
        "(no licence file in the crate: its licence field above; the standard texts of those "
        "licences are in this file and in THIRD_PARTY_NOTICES.txt)\n"
    )
    return head + ("".join(texts) or missing)


def notices() -> str:
    parts = [
        "Rust crates compiled into packages installed with SlideTwin in this image, from each\n"
        "package's Cargo.lock and the crate sources its modules name, with the licence files of\n"
        "each crate.\n",
        STD,
    ]
    for dist, openssl, paths in rust_packages():
        name, version = dist.metadata["Name"], dist.version
        crates: dict[tuple[str, str], str] = {}
        if name.lower() in BUNDLED:
            parts.append(f"\n{RULE}\n{name} {version}: {BUNDLED[name.lower()]}\n{RULE}\n")
        else:
            lock = cargo_lock(name, version)
            for p in lock.get("package", []):  # type: ignore[attr-defined]
                if str(p.get("source", "")).startswith("registry+"):
                    crates[(p["name"], p["version"])] = p["checksum"]
        for key in sorted(paths - set(crates)):
            checksum = registry_checksum(*key)
            if checksum is not None:
                crates[key] = checksum
        if openssl is not None:
            parts.append(
                f"\n{name} {version} also links in {openssl} (Apache-2.0, "
                "https://www.openssl.org/source/license.html): Copyright The OpenSSL Project "
                "Authors.\n"
            )
        parts.append(f"\n{RULE}\n{name} {version}: {len(crates)} crates\n{RULE}\n")
        for (crate, crate_version), checksum in sorted(crates.items()):
            parts.append(crate_notice(crate, crate_version, checksum))
            print(f"{name}: {crate} {crate_version}", file=sys.stderr)
    return "".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.write_text(notices(), encoding="utf-8")


if __name__ == "__main__":
    main()
