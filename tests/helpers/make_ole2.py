"""Minimal OLE2 compound files (MS-CFB) for the ZVI tests: a directory of storages and empty
streams, nothing else. Siblings form a right-leaning chain, which readers accept.

`write_ole2` returns the file's layout, so tests can damage chosen bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

STORAGE, STREAM, ROOT = 1, 2, 5
FREE, END, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def zvi_tree(items: int) -> dict[str, int]:
    """A ZVI's layout, as in Zeiss-1-Merged, with `items` raw images."""
    return {
        "Tags": STREAM,
        "Image": STORAGE,
        "Image/Contents": STREAM,
        "Image/Tags": STORAGE,
        "Image/Tags/Contents": STREAM,
        **{f"Image/Item({i})": STORAGE for i in range(items)},
        **{f"Image/Item({i})/Contents": STREAM for i in range(items)},
        "Thumbnail": STREAM,
    }


ZVI = zvi_tree(3)


@dataclass(frozen=True)
class Layout:
    sector_size: int
    first_dir: int  # sector of the first directory entries
    fat_sectors: int
    first_difat: int = END  # the first DIFAT sector; END when the header lists every FAT sector

    def offset(self, sector: int) -> int:
        return (sector + 1) * self.sector_size


def _entry(name: str, kind: int, left: int, right: int, child: int) -> bytes:
    encoded = name.encode("utf-16-le") + b"\x00\x00"
    out = bytearray(128)
    out[: len(encoded)] = encoded
    struct.pack_into("<HBB", out, 64, len(encoded), kind, 1)
    struct.pack_into("<III", out, 68, left, right, child)
    struct.pack_into("<IQ", out, 116, END, 0)
    return bytes(out)


def directory(entries: dict[str, int]) -> bytes:
    """The directory entries: the root, then every path; each storage's children chained by their
    right siblings."""
    paths = list(entries)
    index = {p: i + 1 for i, p in enumerate(paths)}
    children: dict[str, list[str]] = {"": []}
    for p in paths:
        children.setdefault(p.rpartition("/")[0], []).append(p)
    out = [_entry("Root Entry", ROOT, FREE, FREE, index[children[""][0]] if children[""] else FREE)]
    for p in paths:
        siblings = children[p.rpartition("/")[0]]
        k = siblings.index(p)
        right = index[siblings[k + 1]] if k + 1 < len(siblings) else FREE
        kids = children.get(p, [])
        child = index[kids[0]] if kids else FREE
        out.append(_entry(p.rpartition("/")[2], entries[p], FREE, right, child))
    return b"".join(out)


def write_ole2(
    path: Path, entries: dict[str, int], *, sector_size: int = 512, fat_sectors: int = 1
) -> Layout:
    """FAT sectors first, then the directory, then the DIFAT sectors that more than 109 FAT
    sectors need."""
    raw = directory(entries)
    per = sector_size // 4
    raw += bytes(-len(raw) % sector_size)
    n_dir = len(raw) // sector_size
    n_difat = 0 if fat_sectors <= 109 else -(-(fat_sectors - 109) // (per - 1))
    total = fat_sectors + n_dir + n_difat
    if total > fat_sectors * per:
        raise ValueError("more FAT sectors needed")
    fat = [FREE] * (fat_sectors * per)
    fat[:fat_sectors] = [FATSECT] * fat_sectors
    first_dir = fat_sectors
    for s in range(first_dir, first_dir + n_dir):
        fat[s] = s + 1 if s + 1 < first_dir + n_dir else END
    first_difat = first_dir + n_dir
    for s in range(first_difat, first_difat + n_difat):
        fat[s] = DIFSECT
    locations = list(range(fat_sectors))
    header = bytearray(max(512, sector_size))
    header[:8] = MAGIC
    v3 = sector_size == 512
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3 if v3 else 4, 0xFFFE, 9 if v3 else 12, 6)
    struct.pack_into(
        "<IIIIIIIII",
        header,
        40,
        0 if v3 else n_dir,
        fat_sectors,
        first_dir,
        0,
        4096,
        END,
        0,
        first_difat if n_difat else END,
        n_difat,
    )
    head = locations[:109] + [FREE] * (109 - min(109, len(locations)))
    struct.pack_into("<109I", header, 76, *head)
    difat = b""
    rest = locations[109:]
    for i in range(n_difat):
        chunk = rest[i * (per - 1) : (i + 1) * (per - 1)]
        chunk += [FREE] * (per - 1 - len(chunk))
        following = first_difat + i + 1 if i + 1 < n_difat else END
        difat += struct.pack(f"<{per}I", *chunk, following)
    path.write_bytes(bytes(header) + struct.pack(f"<{len(fat)}I", *fat) + raw + difat)
    return Layout(sector_size, first_dir, fat_sectors, first_difat if n_difat else END)
