"""Pure-stdlib JPEG EXIF DateTimeOriginal reader and minimal lossless injector."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import struct
from zoneinfo import ZoneInfo

_EXIF_HEADER = b"Exif\x00\x00"


def _read_ifd_entries(
    tiff: bytes,
    ifd_offset: int,
    endian: str,
) -> list[tuple[int, int, int, bytes]]:
    if ifd_offset + 2 > len(tiff):
        return []
    count = struct.unpack_from(f"{endian}H", tiff, ifd_offset)[0]
    entries: list[tuple[int, int, int, bytes]] = []
    pos = ifd_offset + 2
    for _ in range(count):
        if pos + 12 > len(tiff):
            break
        tag, dtype, cnt = struct.unpack_from(f"{endian}HHI", tiff, pos)
        val_raw = tiff[pos + 8 : pos + 12]
        entries.append((tag, dtype, cnt, val_raw))
        pos += 12
    return entries


def _extract_ascii_tag(tiff: bytes, cnt: int, val_raw: bytes, endian: str) -> str | None:
    if cnt <= 4:
        raw = val_raw[:cnt]
    else:
        offset = struct.unpack_from(f"{endian}I", val_raw, 0)[0]
        if offset + cnt > len(tiff):
            return None
        raw = tiff[offset : offset + cnt]
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def _parse_tiff_exif_datetime(tiff: bytes) -> tuple[bool, str | None]:
    if len(tiff) < 8:
        return False, None
    byte_order = tiff[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return False, None

    magic = struct.unpack_from(f"{endian}H", tiff, 2)[0]
    if magic != 42:
        return False, None

    ifd0_offset = struct.unpack_from(f"{endian}I", tiff, 4)[0]
    ifd0_entries = _read_ifd_entries(tiff, ifd0_offset, endian)

    date_time_ifd0: str | None = None
    exif_ifd_offset: int | None = None
    for tag, dtype, cnt, val_raw in ifd0_entries:
        if tag == 0x0132 and dtype == 2:
            date_time_ifd0 = _extract_ascii_tag(tiff, cnt, val_raw, endian)
        elif tag == 0x8769:
            exif_ifd_offset = struct.unpack_from(f"{endian}I", val_raw, 0)[0]

    if exif_ifd_offset is not None:
        exif_entries = _read_ifd_entries(tiff, exif_ifd_offset, endian)
        dt_orig: str | None = None
        dt_dig: str | None = None
        for tag, dtype, cnt, val_raw in exif_entries:
            if tag == 0x9003 and dtype == 2:
                dt_orig = _extract_ascii_tag(tiff, cnt, val_raw, endian)
            elif tag == 0x9004 and dtype == 2:
                dt_dig = _extract_ascii_tag(tiff, cnt, val_raw, endian)
        if dt_orig:
            return True, dt_orig
        if dt_dig:
            return False, dt_dig

    return False, date_time_ifd0


def _find_app1_exif_tiff(data: bytes) -> bytes | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    pos = 2
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            break
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xDA:  # SOS - image scan starts, stop scanning metadata segments
            break
        if pos + 2 > n:
            break
        seg_len = struct.unpack_from(">H", data, pos)[0]
        if seg_len < 2 or pos + seg_len > n:
            break
        if marker == 0xE1:
            seg_data = data[pos + 2 : pos + seg_len]
            if seg_data.startswith(_EXIF_HEADER):
                return seg_data[len(_EXIF_HEADER) :]
        pos += seg_len
    return None


def has_exif_datetime_original(path: Path) -> bool:
    """Return True if the JPEG file already has EXIF DateTimeOriginal (0x9003)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return False
    tiff = _find_app1_exif_tiff(head)
    if tiff is None:
        return False
    has_orig, _ = _parse_tiff_exif_datetime(tiff)
    return has_orig


def read_exif_datetime(path: Path, tz_name: str = "Asia/Shanghai") -> datetime | None:
    """Read EXIF DateTimeOriginal / DateTimeDigitized / DateTime from a JPEG file."""
    try:
        with path.open("rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return None
    tiff = _find_app1_exif_tiff(head)
    if tiff is None:
        return None
    _, dt_str = _parse_tiff_exif_datetime(tiff)
    if not dt_str:
        return None
    try:
        naive = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        return naive.replace(tzinfo=ZoneInfo(tz_name))
    except ValueError:
        return None


def _build_minimal_app1_segment(dt: datetime) -> bytes:
    dt_bytes = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii") + b"\x00"  # 20 bytes
    assert len(dt_bytes) == 20

    # Layout (big-endian "MM"):
    # 0..8: TIFF header ("MM", 42, ifd0_offset=8)
    # 8..38: IFD0 (count=2 -> 2 + 2*12 + 4 = 30 bytes; ends at 38)
    # 38..58: DateTime ASCII (20 bytes; ends at 58)
    # 58..88: ExifIFD (count=2 -> 2 + 2*12 + 4 = 30 bytes; ends at 88)
    # 88..108: DateTimeOriginal ASCII (20 bytes; ends at 108)
    # 108..128: DateTimeDigitized ASCII (20 bytes; ends at 128)
    ifd0_offset = 8
    dt_ifd0_offset = 38
    exif_ifd_offset = 58
    dt_orig_offset = 88
    dt_dig_offset = 108

    tiff_header = struct.pack(">2sHI", b"MM", 42, ifd0_offset)
    ifd0 = b"".join(
        [
            struct.pack(">H", 2),
            struct.pack(">HHII", 0x0132, 2, 20, dt_ifd0_offset),  # DateTime
            struct.pack(">HHII", 0x8769, 4, 1, exif_ifd_offset),  # ExifIFDPointer
            struct.pack(">I", 0),  # Next IFD offset
        ]
    )
    exif_ifd = b"".join(
        [
            struct.pack(">H", 2),
            struct.pack(">HHII", 0x9003, 2, 20, dt_orig_offset),  # DateTimeOriginal
            struct.pack(">HHII", 0x9004, 2, 20, dt_dig_offset),  # DateTimeDigitized
            struct.pack(">I", 0),
        ]
    )
    tiff = tiff_header + ifd0 + dt_bytes + exif_ifd + dt_bytes + dt_bytes
    app1_payload = _EXIF_HEADER + tiff
    return b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload


def inject_exif_datetime_if_missing(path: Path, dt: datetime) -> bool:
    """Losslessly inject EXIF DateTimeOriginal into a JPEG lacking it (preserves Motion Photo MP4)."""
    try:
        data = path.read_bytes()
    except OSError:
        return False

    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return False

    tiff = _find_app1_exif_tiff(data[: 256 * 1024])
    if tiff is not None:
        has_orig, _ = _parse_tiff_exif_datetime(tiff)
        if has_orig:
            return False

    app1_segment = _build_minimal_app1_segment(dt)
    new_data = b"\xff\xd8" + app1_segment + data[2:]

    tmp_path = path.with_name(f".{path.name}.exif.tmp")
    tmp_path.write_bytes(new_data)
    os.replace(tmp_path, path)
    return True
