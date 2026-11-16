"""Tests for CronSchedule, config parsers, JPEG EXIF injector, Motion Photo extractor, and XMP."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import struct
import tempfile
import unittest
from zoneinfo import ZoneInfo

from hwcloudpd.config import parse_byte_size, parse_date_bound
from hwcloudpd.cron import CronSchedule
from hwcloudpd.exif import (
    has_exif_datetime_original,
    inject_exif_datetime_if_missing,
    read_exif_datetime,
)
from hwcloudpd.models import MediaItem
from hwcloudpd.postprocess import (
    extract_motion_photo_video,
    format_folder_path,
    write_xmp_sidecar,
)


def _make_minimal_mp4() -> bytes:
    ftyp_payload = b"mp42\x00\x00\x00\x00mp42isom"
    ftyp = struct.pack(">I4s", 8 + len(ftyp_payload), b"ftyp") + ftyp_payload
    moov_payload = b"\x00" * 16
    moov = struct.pack(">I4s", 8 + len(moov_payload), b"moov") + moov_payload
    mdat_payload = b"\x11\x22\x33\x44" * 8
    mdat = struct.pack(">I4s", 8 + len(mdat_payload), b"mdat") + mdat_payload
    return ftyp + moov + mdat


def _make_minimal_jpeg() -> bytes:
    # Minimal valid-header JPEG SOI + APP0 + SOS + EOI markers
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xaa\xbb\xff\xd9"


class CronAndPostprocessTests(unittest.TestCase):
    def test_cron_schedule_standard_and_leap_year(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        sched = CronSchedule.parse("0 */2 * * *")
        nxt = sched.next_after(datetime(2026, 11, 16, 13, 25, tzinfo=tz), "Asia/Shanghai")
        self.assertEqual(nxt, datetime(2026, 11, 16, 14, 0, tzinfo=tz))

        # Month rollover
        nxt_eom = sched.next_after(datetime(2026, 11, 30, 23, 15, tzinfo=tz), "Asia/Shanghai")
        self.assertEqual(nxt_eom, datetime(2026, 12, 1, 0, 0, tzinfo=tz))

        # Leap day across 2026 -> 2028
        leap = CronSchedule.parse("15 3 29 2 *")
        nxt_leap = leap.next_after(datetime(2026, 1, 1, 0, 0, tzinfo=tz), "Asia/Shanghai")
        self.assertEqual(nxt_leap, datetime(2028, 2, 29, 3, 15, tzinfo=tz))

        # Macros & Sunday=7
        self.assertEqual(CronSchedule.parse("@daily").expression, "@daily")
        sunday = CronSchedule.parse("0 9 * * 7")
        nxt_sun = sunday.next_after(datetime(2026, 11, 16, 10, 0, tzinfo=tz), "Asia/Shanghai")
        self.assertEqual(nxt_sun.weekday(), 6)  # Sunday
        self.assertEqual((nxt_sun.hour, nxt_sun.minute), (9, 0))

    def test_config_size_and_date_parsers(self) -> None:
        self.assertEqual(parse_byte_size("50KB"), 50000)
        self.assertEqual(parse_byte_size("2MiB"), 2 * 1024 * 1024)
        self.assertEqual(parse_byte_size("1024"), 1024)
        self.assertIsNone(parse_byte_size(None))

        start_ms = parse_date_bound("2026-11-12", "Asia/Shanghai", end_of_day=False)
        end_ms = parse_date_bound("2026-11-12", "Asia/Shanghai", end_of_day=True)
        assert start_ms is not None and end_ms is not None
        self.assertEqual(end_ms - start_ms, 86399 * 1000 + 999)

    def test_folder_structure_formatting(self) -> None:
        dt = datetime(2026, 11, 12, 2, 48, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(format_folder_path("{:%Y/%m/%d}", dt, "相机"), "2026/11/12")
        self.assertEqual(format_folder_path("{album}/{:%Y/%m}", dt, "DJI/Mimo"), "DJI_Mimo/2026/11")
        self.assertEqual(format_folder_path("none", dt, "相机"), "")

    def test_exif_injection_preserves_motion_photo_mp4_and_skips_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "IMG_live.jpg"
            mp4_payload = _make_minimal_mp4()
            footer = b"LIVE_00000123"
            jpg.write_bytes(_make_minimal_jpeg() + mp4_payload + footer)

            self.assertFalse(has_exif_datetime_original(jpg))
            dt = datetime(2026, 11, 12, 2, 48, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertTrue(inject_exif_datetime_if_missing(jpg, dt))
            self.assertTrue(has_exif_datetime_original(jpg))
            self.assertEqual(read_exif_datetime(jpg, "Asia/Shanghai"), dt)

            # Second injection should be a no-op
            new_dt = datetime(2020, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertFalse(inject_exif_datetime_if_missing(jpg, new_dt))
            self.assertEqual(read_exif_datetime(jpg, "Asia/Shanghai"), dt)

            # Embedded MP4 extraction still yields exact MP4 bytes without LIVE_ footer
            out_suffix = extract_motion_photo_video(jpg, "suffix", mtime_sec=dt.timestamp())
            self.assertIsNotNone(out_suffix)
            assert out_suffix is not None
            self.assertEqual(out_suffix.name, "IMG_live_HEVC.mp4")
            self.assertEqual(out_suffix.read_bytes(), mp4_payload)

            out_orig = extract_motion_photo_video(jpg, "original", mtime_sec=dt.timestamp())
            self.assertIsNotNone(out_orig)
            assert out_orig is not None
            self.assertEqual(out_orig.name, "IMG_live.mp4")
            self.assertEqual(out_orig.read_bytes(), mp4_payload)

    def test_xmp_sidecar_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "IMG_0001.jpg"
            media.write_bytes(b"img")
            dt = datetime(2026, 11, 12, 8, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            item = MediaItem(
                unique_id="abc123",
                album_id="default-album-1",
                album_name="相机",
                file_type="1",
                create_time_ms=int(dt.timestamp() * 1000),
                is_fav=1,
                file_name="IMG_0001.jpg",
            )
            xmp_path = write_xmp_sidecar(media, item, dt)
            text = xmp_path.read_text(encoding="utf-8")
            self.assertIn('xmp:Rating="5"', text)
            self.assertIn("2026-11-12T08:00:00+08:00", text)
            self.assertIn("<rdf:li>相机</rdf:li>", text)


if __name__ == "__main__":
    unittest.main()
