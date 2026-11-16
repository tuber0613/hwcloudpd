"""Path resolution, collision deduplication, Motion Photo extraction, and XMP sidecars."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

from hwcloudpd.models import MediaItem
from hwcloudpd.store import StateStore

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_KNOWN_MP4_BOXES = {b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pdin"}


def sanitize_component(name: str, *, fallback: str = "unnamed") -> str:
    """Sanitize a filename or album folder component for POSIX/NAS filesystems."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip().strip(".")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned


def format_folder_path(folder_structure: str, dt: datetime, album_name: str) -> str:
    """Render relative subfolder path from `--folder-structure` template."""
    template = folder_structure.strip()
    if not template or template.lower() == "none":
        return ""

    safe_album = sanitize_component(album_name, fallback="default")
    sentinel = "__HWCLOUD_ALBUM_TOKEN__"
    rendered = template.replace("{album}", sentinel)
    try:
        rendered = rendered.format(dt)
    except Exception:
        # Fallback if a format template without positional datetime syntax was provided.
        rendered = dt.strftime("%Y/%m/%d")
    rendered = rendered.replace(sentinel, safe_album)

    parts = [
        sanitize_component(part, fallback="unknown")
        for part in rendered.replace("\\", "/").split("/")
        if part and part not in {".", ".."}
    ]
    return "/".join(parts)


def resolve_target_path(
    root_dir: Path,
    *,
    folder_structure: str,
    file_match_policy: str,
    item: MediaItem,
    dt: datetime,
    store: StateStore,
) -> Path:
    """Determine final local file path, handling icloudpd-style deduplication policies."""
    rel_folder = format_folder_path(folder_structure, dt, item.album_name)
    raw_name = sanitize_component(item.file_name, fallback=f"{item.unique_id[:12]}.jpg")
    stem = Path(raw_name).stem or item.unique_id[:12]
    suffix = Path(raw_name).suffix
    if not suffix:
        suffix = ".mp4" if item.is_video else ".jpg"

    if file_match_policy == "name-id7":
        candidate_name = f"{stem}_{item.unique_id[:7]}{suffix}"
        rel_path = f"{rel_folder}/{candidate_name}" if rel_folder else candidate_name
        return root_dir / rel_path

    # Default: name-size-dedup-with-suffix
    def _is_occupied_by_other(rel_candidate: str) -> bool:
        existing = store.find_by_rel_path(rel_candidate)
        if existing is not None and existing["unique_id"] != item.unique_id:
            return True
        abs_candidate = root_dir / rel_candidate
        if abs_candidate.exists():
            if existing is not None and existing["unique_id"] == item.unique_id:
                return False
            # File exists on disk without a matching state record for this unique_id.
            return True
        return False

    base_name = f"{stem}{suffix}"
    rel_base = f"{rel_folder}/{base_name}" if rel_folder else base_name
    if not _is_occupied_by_other(rel_base):
        return root_dir / rel_base

    if item.file_size > 0:
        sized_name = f"{stem}-{item.file_size}{suffix}"
        rel_sized = f"{rel_folder}/{sized_name}" if rel_folder else sized_name
        if not _is_occupied_by_other(rel_sized):
            return root_dir / rel_sized

    id_name = f"{stem}-{item.unique_id[:7]}{suffix}"
    rel_id = f"{rel_folder}/{id_name}" if rel_folder else id_name
    return root_dir / rel_id


def _find_embedded_mp4_span(data: bytes) -> tuple[int, int] | None:
    """Locate (start_offset, end_offset) of an embedded MP4 stream inside a Motion Photo JPEG."""
    n = len(data)
    search_from = 4
    while True:
        ftyp_pos = data.find(b"ftyp", search_from)
        if ftyp_pos < 4 or ftyp_pos + 8 > n:
            return None
        start = ftyp_pos - 4
        box_size = struct.unpack_from(">I", data, start)[0]
        brand = data[ftyp_pos + 4 : ftyp_pos + 8]
        is_ascii_brand = all(32 <= b <= 126 for b in brand)
        if 16 <= box_size <= 256 and is_ascii_brand and start + box_size <= n:
            # Verify the next MP4 atom right after ftyp looks valid.
            next_pos = start + box_size
            if next_pos + 8 <= n:
                next_size = struct.unpack_from(">I", data, next_pos)[0]
                next_type = data[next_pos + 4 : next_pos + 8]
                if next_type in _KNOWN_MP4_BOXES and next_size >= 8:
                    # Walk top-level MP4 boxes to strip any trailing Huawei LIVE_ footer bytes.
                    cursor = start
                    last_valid_end = start
                    while cursor + 8 <= n:
                        bsize = struct.unpack_from(">I", data, cursor)[0]
                        btype = data[cursor + 4 : cursor + 8]
                        if bsize == 1 and cursor + 16 <= n:
                            bsize = struct.unpack_from(">Q", data, cursor + 8)[0]
                        if bsize < 8 or cursor + bsize > n:
                            break
                        if not all(32 <= b <= 126 for b in btype):
                            break
                        cursor += bsize
                        last_valid_end = cursor
                    return start, last_valid_end if last_valid_end > start + box_size else n
        search_from = ftyp_pos + 4


def extract_motion_photo_video(
    jpg_path: Path,
    policy: str,
    *,
    mtime_sec: float | None = None,
) -> Path | None:
    """Losslessly extract embedded MP4 video from a Huawei Motion Photo JPEG."""
    if policy == "none":
        return None
    if not jpg_path.is_file():
        return None
    if jpg_path.suffix.lower() not in {".jpg", ".jpeg"}:
        return None

    data = jpg_path.read_bytes()
    span = _find_embedded_mp4_span(data)
    if span is None:
        return None

    start, end = span
    mp4_bytes = data[start:end]
    if not mp4_bytes:
        return None

    if policy == "original":
        out_path = jpg_path.with_suffix(".mp4")
    else:  # "suffix"
        out_path = jpg_path.with_name(f"{jpg_path.stem}_HEVC.mp4")

    tmp_path = out_path.with_name(f".{out_path.name}.part")
    tmp_path.write_bytes(mp4_bytes)
    if mtime_sec is not None and mtime_sec > 0:
        os.utime(tmp_path, (mtime_sec, mtime_sec))
    os.replace(tmp_path, out_path)
    return out_path


def write_xmp_sidecar(media_path: Path, item: MediaItem, dt: datetime) -> Path:
    """Write an XMP sidecar file (`<media_path>.xmp`) for Lightroom / PhotoPrism / Immich."""
    iso_dt = dt.isoformat()
    rating = "5" if item.is_fav else "0"

    xmpmeta = ET.Element(
        "x:xmpmeta",
        {"xmlns:x": "adobe:ns:meta/"},
    )
    rdf = ET.SubElement(
        xmpmeta,
        "rdf:RDF",
        {"xmlns:rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"},
    )
    desc = ET.SubElement(
        rdf,
        "rdf:Description",
        {
            "rdf:about": "",
            "xmlns:xmp": "http://ns.adobe.com/xap/1.0/",
            "xmlns:photoshop": "http://ns.adobe.com/photoshop/1.0/",
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
            "xmp:CreateDate": iso_dt,
            "xmp:Rating": rating,
            "photoshop:DateCreated": iso_dt,
        },
    )
    if item.album_name:
        subj = ET.SubElement(desc, "dc:subject")
        bag = ET.SubElement(subj, "rdf:Bag")
        li = ET.SubElement(bag, "rdf:li")
        li.text = item.album_name

    ET.indent(xmpmeta, space="  ")
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        xmpmeta, encoding="utf-8"
    ) + b"\n"

    sidecar_path = Path(f"{media_path}.xmp")
    tmp_path = sidecar_path.with_name(f".{sidecar_path.name}.tmp")
    tmp_path.write_bytes(xml_bytes)
    os.replace(tmp_path, sidecar_path)
    return sidecar_path
