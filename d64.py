"""
d64.py — Commodore disk image parser (D64 / D71 / D81).

Parses the directory and extracts individual files (PRG/SEQ/USR) by
following the sector chain, so a chosen PRG can be DMA-run on the
Ultimate 64 without mounting the whole image.
"""

from dataclasses import dataclass, field
from typing import Optional

FILE_TYPES = {0: "DEL", 1: "SEQ", 2: "PRG", 3: "USR", 4: "REL", 5: "CBM"}

# --- geometry -----------------------------------------------------------

def _d64_sectors(track: int) -> int:
    if track <= 17:
        return 21
    if track <= 24:
        return 19
    if track <= 30:
        return 18
    return 17

def _d71_sectors(track: int) -> int:
    # Tracks 36-70 mirror 1-35
    t = track if track <= 35 else track - 35
    return _d64_sectors(t)


class Geometry:
    def __init__(self, kind: str, tracks: int):
        self.kind = kind
        self.tracks = tracks
        self._offsets = {}
        off = 0
        for t in range(1, tracks + 1):
            self._offsets[t] = off
            off += self.sectors(t) * 256
        self.total_size = off

    def sectors(self, track: int) -> int:
        if self.kind == "d81":
            return 40
        if self.kind == "d71":
            return _d71_sectors(track)
        return _d64_sectors(track)

    def offset(self, track: int, sector: int) -> int:
        if track < 1 or track > self.tracks or sector < 0 or sector >= self.sectors(track):
            raise ValueError(f"illegal track/sector {track}/{sector}")
        return self._offsets[track] + sector * 256


def detect_geometry(size: int, name_hint: str = "") -> Geometry:
    hint = (name_hint or "").lower()
    table = [
        (174848, "d64", 35), (175531, "d64", 35),   # 35 tracks (+error bytes)
        (196608, "d64", 40), (197376, "d64", 40),   # 40 tracks (+error bytes)
        (205312, "d64", 42), (206114, "d64", 42),   # 42 tracks (rare)
        (349696, "d71", 70), (351062, "d71", 70),
        (819200, "d81", 80), (822400, "d81", 80),
    ]
    for sz, kind, tracks in table:
        if size == sz:
            return Geometry(kind, tracks)
    # Fall back on extension for odd sizes
    for kind, tracks in (("d71", 70), ("d81", 80), ("d64", 35)):
        if hint.endswith("." + kind):
            return Geometry(kind, tracks)
    raise ValueError(f"unrecognised image size {size} bytes")


# --- PETSCII helpers ----------------------------------------------------

def petscii_to_ascii(data: bytes) -> str:
    out = []
    for b in data:
        if b in (0x00, 0xA0):
            continue
        if 0x41 <= b <= 0x5A:            # unshifted letters -> display upper
            out.append(chr(b))
        elif 0xC1 <= b <= 0xDA:          # shifted letters
            out.append(chr(b - 0x80).lower())
        elif 0x20 <= b <= 0x3F or b in (0x5B, 0x5D):
            out.append(chr(b))
        else:
            out.append("~")
    return "".join(out)


def ascii_to_petscii(text: str) -> bytes:
    """Case-swapped mapping so typed text appears as expected on the C64."""
    out = bytearray()
    for ch in text:
        o = ord(ch)
        if "a" <= ch <= "z":
            out.append(o - 32)           # -> unshifted letter
        elif "A" <= ch <= "Z":
            out.append(o + 128)          # -> shifted letter
        elif ch == "\n":
            out.append(13)
        elif 0x20 <= o <= 0x5D or o == 13:
            out.append(o)
    return bytes(out)


# --- image --------------------------------------------------------------

@dataclass
class DirEntry:
    index: int
    name: str
    raw_name: bytes                     # PETSCII, trailing 0xA0 stripped
    file_type: str
    type_code: int
    locked: bool
    closed: bool
    blocks: int
    track: int
    sector: int
    load_address: Optional[int] = None


@dataclass
class DiskImage:
    data: bytes
    name_hint: str = field(default="", repr=False)
    geo: Geometry = field(init=False)
    disk_name: str = field(init=False, default="")
    disk_id: str = field(init=False, default="")
    entries: list = field(init=False, default_factory=list)

    def __post_init__(self):
        self.geo = detect_geometry(len(self.data), self.name_hint)
        self._parse_header()
        self._parse_directory()

    # header ------------------------------------------------------------
    def _parse_header(self):
        if self.geo.kind == "d81":
            off = self.geo.offset(40, 0)
            self.disk_name = petscii_to_ascii(self.data[off + 0x04: off + 0x14])
            self.disk_id = petscii_to_ascii(self.data[off + 0x16: off + 0x18])
        else:
            off = self.geo.offset(18, 0)
            self.disk_name = petscii_to_ascii(self.data[off + 0x90: off + 0xA0])
            self.disk_id = petscii_to_ascii(self.data[off + 0xA2: off + 0xA4])

    # directory ---------------------------------------------------------
    def _dir_start(self):
        return (40, 3) if self.geo.kind == "d81" else (18, 1)

    def _parse_directory(self):
        track, sector = self._dir_start()
        seen = set()
        idx = 0
        while track != 0:
            if (track, sector) in seen:
                break                     # corrupt chain guard
            seen.add((track, sector))
            off = self.geo.offset(track, sector)
            block = self.data[off: off + 256]
            for e in range(8):
                ent = block[e * 32: e * 32 + 32]
                type_byte = ent[2]
                if type_byte == 0:
                    continue
                code = type_byte & 0x07
                raw = ent[5:21].rstrip(b"\xA0")
                entry = DirEntry(
                    index=idx,
                    name=petscii_to_ascii(ent[5:21]),
                    raw_name=raw,
                    file_type=FILE_TYPES.get(code, f"?{code}"),
                    type_code=code,
                    locked=bool(type_byte & 0x40),
                    closed=bool(type_byte & 0x80),
                    blocks=ent[30] | (ent[31] << 8),
                    track=ent[3],
                    sector=ent[4],
                )
                if entry.file_type == "PRG" and entry.closed:
                    try:
                        first = self._read_sector_data(entry.track, entry.sector)
                        if len(first) >= 2:
                            entry.load_address = first[0] | (first[1] << 8)
                    except ValueError:
                        pass
                self.entries.append(entry)
                idx += 1
            track, sector = block[0], block[1]

    def _read_sector_data(self, track, sector):
        off = self.geo.offset(track, sector)
        return self.data[off + 2: off + 256]

    # extraction --------------------------------------------------------
    def extract(self, entry: DirEntry, max_size: int = 2 * 1024 * 1024) -> bytes:
        """Follow the sector chain and return the file contents."""
        out = bytearray()
        track, sector = entry.track, entry.sector
        seen = set()
        while track != 0:
            if (track, sector) in seen:
                raise ValueError("circular sector chain (corrupt image)")
            seen.add((track, sector))
            off = self.geo.offset(track, sector)
            block = self.data[off: off + 256]
            nxt_t, nxt_s = block[0], block[1]
            if nxt_t == 0:
                # last sector: byte 1 = index of last valid byte
                end = max(2, min(nxt_s, 255)) + 1
                out += block[2:end]
            else:
                out += block[2:256]
            if len(out) > max_size:
                raise ValueError("file exceeds size limit")
            track, sector = nxt_t, nxt_s
        return bytes(out)

    def find(self, index: Optional[int] = None, name: Optional[str] = None) -> DirEntry:
        if index is not None:
            for e in self.entries:
                if e.index == index:
                    return e
        if name is not None:
            for e in self.entries:
                if e.name == name:
                    return e
        raise KeyError("file not found in image")

    def listing(self) -> dict:
        return {
            "disk_name": self.disk_name,
            "disk_id": self.disk_id,
            "format": self.geo.kind,
            "tracks": self.geo.tracks,
            "files": [
                {
                    "index": e.index,
                    "name": e.name,
                    "type": e.file_type,
                    "blocks": e.blocks,
                    "locked": e.locked,
                    "closed": e.closed,
                    "load_address": (f"${e.load_address:04X}" if e.load_address is not None else None),
                    "approx_bytes": max(0, e.blocks * 254 - 2),
                }
                for e in self.entries
            ],
        }
