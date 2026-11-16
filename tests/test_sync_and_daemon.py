"""End-to-end tests for SyncEngine (all icloudpd features) and unattended SyncDaemon."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hwcloudpd.auth import AuthManager
from hwcloudpd.cli import main as cli_main
from hwcloudpd.client import DownloadResult, HttpResponse, HuaweiGalleryClient
from hwcloudpd.daemon import SyncDaemon
from hwcloudpd.exif import has_exif_datetime_original, read_exif_datetime
from hwcloudpd.models import (
    AuthSession,
    InvalidCredentialsError,
    SyncConfig,
    TwoFactorRequiredError,
)
from hwcloudpd.notifier import Notifier, _build_webhook_request
from hwcloudpd.store import StateStore
from hwcloudpd.sync import SyncEngine


def _make_mp4_bytes() -> bytes:
    ftyp = struct.pack(">I4s", 20, b"ftyp") + b"mp42\x00\x00\x00\x00mp42"
    moov = struct.pack(">I4s", 16, b"moov") + b"\x00" * 8
    return ftyp + moov


def _make_jpeg_bytes(tag: bytes, *, live_mp4: bytes | None = None) -> bytes:
    base = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00" + tag + b"\xff\xd9"
    )
    if live_mp4:
        return base + live_mp4 + b"LIVE_999"
    return base


class FakeHuaweiCloudServer:
    """In-memory fake Huawei Cloud Gallery backend implementing verified API endpoints."""

    def __init__(self) -> None:
        self.albums: list[dict] = [
            {"albumId": "default-album-1", "albumName": "相机", "albumVersion": "1.0"},
            {"albumId": "default-album-2", "albumName": "截屏录屏", "albumVersion": "1.0"},
            {"albumId": "default-album-3", "albumName": "最近删除", "albumVersion": "1.0"},
            {"albumId": "default-album-200-dji", "albumName": "DJI Mimo", "albumVersion": "1.0"},
            {"albumId": "default-album-200-wb", "albumName": "微博保存", "albumVersion": "1.0"},
        ]
        self.items_by_album: dict[str, list[dict]] = {
            "default-album-1": [],
            "default-album-2": [],
            "default-album-3": [],
            "default-album-200-dji": [],
            "default-album-200-wb": [],
        }
        self.bursts: dict[str, list[dict]] = {}
        self.blobs: dict[str, bytes] = {}
        self._meta_map: dict[str, dict] = {}
        self.auth_valid = True
        self.heartbeat_count = 0
        self.simple_file_calls: list[dict] = []
        self.deleted_cloud_calls: list[dict] = []
        self.corrupt_uid: str | None = None

    def add_media(
        self,
        *,
        unique_id: str,
        album_id: str,
        file_name: str,
        file_type: str,
        create_time_ms: int,
        content: bytes,
        is_fav: int = 0,
        is_cover: int = 0,
    ) -> None:
        self.items_by_album.setdefault(album_id, []).append(
            {
                "albumId": album_id,
                "uniqueId": unique_id,
                "fileType": file_type,
                "createTime": create_time_ms,
                "isCover": is_cover,
                "isFav": is_fav,
                "recycleTime": "",
                "uid": "80086000000000001",
                "fileName": "",
            }
        )
        self.blobs[unique_id] = content
        self._meta_map[unique_id] = {
            "uniqueId": unique_id,
            "albumId": album_id,
            "fileName": file_name,
            "fileSize": len(content),
            "fileType": file_type,
            "sha256": hashlib.sha256(content).hexdigest(),
            "createTime": create_time_ms,
        }

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None, **kwargs) -> HttpResponse:
        if not self.auth_valid:
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": "401", "info": "session expired"}).encode("utf-8"),
            )
        payload = json.loads(body.decode("utf-8")) if body else {}

        if "/heartbeatCheck" in url:
            self.heartbeat_count += 1
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": "0", "info": "user has logged"}).encode("utf-8"),
            )

        if url.endswith("/album/queryAlbumInfo"):
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": 0, "ownAlbumList": self.albums}).encode("utf-8"),
            )

        if url.endswith("/album/galleryAlbumStatInfo"):
            stats = []
            for aid in payload.get("albumList") or []:
                items = self.items_by_album.get(aid, [])
                stats.append(
                    {
                        "albumId": aid,
                        "imgNum": sum(1 for i in items if i["fileType"] in {"1", "5"}),
                        "videoNum": sum(1 for i in items if i["fileType"] == "4"),
                        "hide": 0,
                    }
                )
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": 0, "albumStatInfoList": stats}).encode("utf-8"),
            )

        if url.endswith("/album/getSimpleFile"):
            self.simple_file_calls.append(payload)
            aid = payload.get("albumId")
            qtype = payload.get("type")
            offset = int(payload.get("currentNum") or 0)
            count = int(payload.get("count") or 100)

            if qtype == 4:
                pool = list(self.items_by_album.get("default-album-3", []))
            elif qtype == 1:
                pool = [
                    it
                    for k, lst in self.items_by_album.items()
                    if k != "default-album-3"
                    for it in lst
                    if it["fileType"] in {"1", "5"}
                ]
            elif qtype == 2:
                pool = [
                    it
                    for k, lst in self.items_by_album.items()
                    if k != "default-album-3"
                    for it in lst
                    if it["fileType"] == "4"
                ]
            elif qtype == 3:
                pool = [
                    it
                    for k, lst in self.items_by_album.items()
                    if k != "default-album-3"
                    for it in lst
                    if it["isFav"] == 1
                ]
            elif aid is None:
                pool = [
                    it
                    for k, lst in self.items_by_album.items()
                    if k != "default-album-3"
                    for it in lst
                ]
                pool.sort(key=lambda x: x["createTime"], reverse=True)
            else:
                pool = list(self.items_by_album.get(aid, []))

            chunk = pool[offset : offset + count]
            has_more = (offset + len(chunk)) < len(pool)
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps(
                    {"resultCode": "0", "hasMore": has_more, "fileList": chunk}
                ).encode("utf-8"),
            )

        if url.endswith("/album/burstphotolist"):
            uid = payload["uniqueId"]
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps(
                    {"resultCode": "0", "burstIdList": self.bursts.get(uid, [])}
                ).encode("utf-8"),
            )

        if url.endswith("/album/queryCloudFileName"):
            out = []
            for req_item in payload.get("fileList") or []:
                uid = req_item["uniqueId"]
                if uid in self._meta_map:
                    out.append(self._meta_map[uid])
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": 0, "fileList": out}).encode("utf-8"),
            )

        if url.endswith("/proxyserver/driveFileProxy/preProcess"):
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": "0", "info": "success"}).encode("utf-8"),
            )

        if url.endswith("/album/getSingleUrl"):
            uid = payload["fileList"][0]["uniqueId"]
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps(
                    {
                        "code": 0,
                        "urlList": [
                            {
                                "uniqueId": uid,
                                "url": f"https://download-proxy-p01-drcn.platform.dbankcloud.cn/filev2/{uid}",
                            }
                        ],
                    }
                ).encode("utf-8"),
            )

        if url.endswith("/album/deleteAlbumFile"):
            self.deleted_cloud_calls.append(payload)
            uids = set(payload.get("uniqueId") or [])
            aid = payload["albumId"]
            remaining = []
            for item in self.items_by_album.get(aid, []):
                if item["uniqueId"] in uids:
                    recycled = dict(item)
                    recycled["albumId"] = "default-album-3"
                    self.items_by_album["default-album-3"].append(recycled)
                else:
                    remaining.append(item)
            self.items_by_album[aid] = remaining
            return HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": 0, "successList": list(uids)}).encode("utf-8"),
            )

        raise AssertionError(f"Unhandled request {method} {url}")

    def stream_download(self, url: str, headers: dict[str, str], part_path: Path, **kwargs) -> DownloadResult:
        uid = url.rsplit("/", 1)[-1]
        data = self.blobs[uid]
        if self.corrupt_uid == uid:
            data = b"corrupted!"
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_bytes(data)
        return DownloadResult(
            bytes_written=len(data),
            sha256_hex=hashlib.sha256(data).hexdigest(),
            md5_hex=hashlib.md5(data).hexdigest(),
        )


def _ts_ms(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


class SyncAndDaemonTests(unittest.TestCase):
    def _setup_env(self, tmp: str) -> tuple[Path, Path, AuthManager, FakeHuaweiCloudServer, HuaweiGalleryClient, StateStore]:
        cfg_dir = Path(tmp) / "config"
        photos_dir = Path(tmp) / "photos"
        mgr = AuthManager(cfg_dir)
        mgr.save(
            AuthSession(
                cookies={"shareToken": "tok_ok", "HWWAFSESID": "waf_ok"},
                csrf_token="tok_ok",
                user_id="80086000000000001",
                device_id="b" * 64,
            )
        )
        server = FakeHuaweiCloudServer()
        client = HuaweiGalleryClient(mgr, server, retry_base_delay=0.005)
        store = StateStore(cfg_dir / "state.db")
        return cfg_dir, photos_dir, mgr, server, client, store

    def test_default_date_archive_excludes_recycle_and_handles_collisions_and_postprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = self._setup_env(tmp)
            try:
                mp4_bytes = _make_mp4_bytes()
                live_jpg = _make_jpeg_bytes(b"live1", live_mp4=mp4_bytes)
                still_a = _make_jpeg_bytes(b"stillA")
                still_b = _make_jpeg_bytes(b"stillB_larger")
                vid_bytes = b"\x00\x00\x00\x14ftypisom" + b"video_data"
                recycled_bytes = _make_jpeg_bytes(b"trash")

                # Camera live photo + collision photo with same filename on same date
                server.add_media(
                    unique_id="aaaa11100000",
                    album_id="default-album-1",
                    file_name="IMG_001.jpg",
                    file_type="5",
                    create_time_ms=_ts_ms(2026, 11, 12, 9),
                    content=live_jpg,
                    is_fav=1,
                )
                server.add_media(
                    unique_id="bbbb22200000",
                    album_id="default-album-2",
                    file_name="IMG_001.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12, 10),
                    content=still_b,
                )
                server.add_media(
                    unique_id="cccc33300000",
                    album_id="default-album-200-dji",
                    file_name="DJI_001.mp4",
                    file_type="4",
                    create_time_ms=_ts_ms(2026, 11, 10, 15),
                    content=vid_bytes,
                )
                server.add_media(
                    unique_id="dddd44400000",
                    album_id="default-album-200-wb",
                    file_name="WB_001.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 9, 8),
                    content=still_a,
                )
                # Recycled item MUST NOT be downloaded
                server.add_media(
                    unique_id="eeee55500000",
                    album_id="default-album-3",
                    file_name="TRASH_001.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12, 11),
                    content=recycled_bytes,
                )

                cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    exclude_albums=["微博*"],
                    set_exif_datetime=True,
                    xmp_sidecar=True,
                    live_photo_mov_filename_policy="suffix",
                    download_concurrency=1,
                    request_delay_ms=0,
                )
                report = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(report.downloaded, 3)
                self.assertEqual(report.failed, 0)

                p_live = photos_dir / "2026/11/12/IMG_001.jpg"
                p_coll = photos_dir / f"2026/11/12/IMG_001-{len(still_b)}.jpg"
                p_vid = photos_dir / "2026/11/10/DJI_001.mp4"
                self.assertTrue(p_live.is_file())
                self.assertTrue(p_coll.is_file())
                self.assertTrue(p_vid.is_file())
                self.assertFalse((photos_dir / "2026/11/09/WB_001.jpg").exists())
                self.assertFalse((photos_dir / "2026/11/12/TRASH_001.jpg").exists())

                # Verify EXIF injected, Motion Photo MP4 extracted, XMP written
                self.assertTrue(has_exif_datetime_original(p_live))
                self.assertEqual(
                    read_exif_datetime(p_live, "Asia/Shanghai"),
                    datetime(2026, 11, 12, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                )
                self.assertEqual((photos_dir / "2026/11/12/IMG_001_HEVC.mp4").read_bytes(), mp4_bytes)
                self.assertTrue((photos_dir / "2026/11/12/IMG_001.jpg.xmp").is_file())
            finally:
                store.close()

    def test_until_found_early_stop_and_burst_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = self._setup_env(tmp)
            try:
                # Populate 5 items in camera; burst cover expands into 2 items
                for idx in range(5):
                    uid = f"cam_{idx}"
                    content = _make_jpeg_bytes(uid.encode())
                    server.add_media(
                        unique_id=uid,
                        album_id="default-album-1",
                        file_name=f"CAM_{idx}.jpg",
                        file_type="1",
                        create_time_ms=_ts_ms(2026, 11, 10 - idx),
                        content=content,
                        is_cover=(1 if idx == 0 else 0),
                    )
                burst_extra = _make_jpeg_bytes(b"burst_frame_2")
                server.blobs["cam_0_b2"] = burst_extra
                server._meta_map["cam_0_b2"] = {
                    "uniqueId": "cam_0_b2",
                    "albumId": "default-album-1",
                    "fileName": "CAM_0_BURST2.jpg",
                    "fileSize": len(burst_extra),
                    "fileType": "1",
                    "sha256": hashlib.sha256(burst_extra).hexdigest(),
                    "createTime": _ts_ms(2026, 11, 10),
                }
                server.bursts["cam_0"] = [
                    {
                        "albumId": "default-album-1",
                        "uniqueId": "cam_0",
                        "fileType": "1",
                        "createTime": _ts_ms(2026, 11, 10),
                    },
                    {
                        "albumId": "default-album-1",
                        "uniqueId": "cam_0_b2",
                        "fileType": "1",
                        "createTime": _ts_ms(2026, 11, 10),
                    },
                ]

                cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    page_size=2,
                    until_found=2,
                    request_delay_ms=0,
                )
                first = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(first.downloaded, 6)
                self.assertTrue((photos_dir / "2026/11/10/CAM_0_BURST2.jpg").is_file())

                # Add 1 brand-new item at head; second run with page_size=2 & until_found=2
                # should stop on page 2 without fetching page 3!
                new_bytes = _make_jpeg_bytes(b"brand_new")
                server.items_by_album["default-album-1"].insert(
                    0,
                    {
                        "albumId": "default-album-1",
                        "uniqueId": "cam_new",
                        "fileType": "1",
                        "createTime": _ts_ms(2026, 11, 15),
                        "isCover": 0,
                        "isFav": 0,
                        "recycleTime": "",
                        "uid": "80086000000000001",
                        "fileName": "",
                    },
                )
                server.blobs["cam_new"] = new_bytes
                server._meta_map["cam_new"] = {
                    "uniqueId": "cam_new",
                    "albumId": "default-album-1",
                    "fileName": "CAM_NEW.jpg",
                    "fileSize": len(new_bytes),
                    "fileType": "1",
                    "sha256": hashlib.sha256(new_bytes).hexdigest(),
                    "createTime": _ts_ms(2026, 11, 15),
                }
                server.simple_file_calls.clear()
                second = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(second.downloaded, 1)
                # Page 1 has [cam_new, cam_0 (+cam_0_b2 -> 2 consecutive)], stops before offset=4!
                self.assertEqual(len(server.simple_file_calls), 1)
            finally:
                store.close()

    def test_filters_dry_run_auto_delete_and_keep_cloud_recent_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = self._setup_env(tmp)
            try:
                old_ms = int((time.time() - 40 * 86400) * 1000)
                recent_ms = int((time.time() - 2 * 86400) * 1000)
                server.add_media(
                    unique_id="old_fav",
                    album_id="default-album-1",
                    file_name="OLD_FAV.jpg",
                    file_type="1",
                    create_time_ms=old_ms,
                    content=_make_jpeg_bytes(b"old_fav_payload_12345"),
                    is_fav=1,
                )
                server.add_media(
                    unique_id="recent_vid",
                    album_id="default-album-1",
                    file_name="REC_VID.mp4",
                    file_type="4",
                    create_time_ms=recent_ms,
                    content=b"small",
                    is_fav=0,
                )

                # Dry run with --only-print-filenames writes nothing
                printed: list[str] = []
                dry_cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    only_print_filenames=True,
                    skip_videos=True,
                    only_favorites=True,
                    request_delay_ms=0,
                )
                dry_report = SyncEngine(
                    dry_cfg, client, store, output_fn=printed.append
                ).run_once()
                self.assertEqual(dry_report.downloaded, 1)
                self.assertEqual(len(printed), 1)
                self.assertFalse(Path(printed[0]).exists())

                # Real download of both items
                real_cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    xmp_sidecar=True,
                    request_delay_ms=0,
                )
                SyncEngine(real_cfg, client, store).run_once()
                old_rec = store.get_record("old_fav")
                assert old_rec is not None
                self.assertTrue((photos_dir / old_rec["local_rel_path"]).is_file())

                # Move items older than 30 days to Cloud Recycle Bin
                move_cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    keep_cloud_recent_days=30,
                    request_delay_ms=0,
                )
                move_rep = SyncEngine(move_cfg, client, store).run_once()
                self.assertEqual(move_rep.deleted_cloud, 1)
                # Local status stays "downloaded"; the cloud side is tracked separately.
                self.assertEqual(store.get_record("old_fav")["status"], "downloaded")
                self.assertEqual(store.get_record("old_fav")["cloud_state"], "deleted")
                self.assertEqual(store.get_record("recent_vid")["status"], "downloaded")
                self.assertEqual(store.get_record("recent_vid")["cloud_state"], "present")
                old_fav_path = photos_dir / store.get_record("old_fav")["local_rel_path"]

                # Now move recent_vid into cloud Recycle Bin and test --auto-delete
                rec_vid_item = server.items_by_album["default-album-1"].pop(0)
                rec_vid_item["albumId"] = "default-album-3"
                server.items_by_album["default-album-3"].append(rec_vid_item)

                rec_vid_path = photos_dir / store.get_record("recent_vid")["local_rel_path"]
                self.assertTrue(rec_vid_path.is_file())
                auto_del_cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    auto_delete=True,
                    request_delay_ms=0,
                )
                del_rep = SyncEngine(auto_del_cfg, client, store).run_once()
                self.assertEqual(del_rep.deleted_local, 1)
                self.assertFalse(rec_vid_path.exists())
                self.assertFalse(Path(f"{rec_vid_path}.xmp").exists())
                self.assertEqual(store.get_record("recent_vid")["status"], "deleted_local")
                # old_fav sits in the Recycle Bin because *we* moved it: its backup must survive.
                self.assertTrue(old_fav_path.is_file())
                self.assertEqual(store.get_record("old_fav")["status"], "downloaded")
            finally:
                store.close()

    def test_checksum_mismatch_cleans_part_and_marks_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = self._setup_env(tmp)
            try:
                server.add_media(
                    unique_id="bad_uid",
                    album_id="default-album-1",
                    file_name="BAD.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"expected_content"),
                )
                server.corrupt_uid = "bad_uid"
                cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    request_delay_ms=0,
                )
                rep = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(rep.failed, 1)
                self.assertEqual(rep.downloaded, 0)
                self.assertIsNone(store.get_record("bad_uid"))
                self.assertFalse(any(photos_dir.rglob("*.hwpart")))
            finally:
                store.close()

    def test_daemon_keepalive_auth_expiry_webhook_and_curl_hot_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, mgr, server, client, store = self._setup_env(tmp)
            try:
                server.add_media(
                    unique_id="daemon_uid_1",
                    album_id="default-album-1",
                    file_name="DAEMON_1.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 16),
                    content=_make_jpeg_bytes(b"daemon_photo"),
                )
                server.auth_valid = False
                alerts: list[tuple[str, str]] = []

                def fake_sender(url: str, body: bytes, headers: dict[str, str]) -> None:
                    alerts.append((url, body.decode("utf-8")))

                notifier = Notifier(
                    webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
                    sender=fake_sender,
                )
                cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    watch_interval=3600,
                    keepalive_interval=60,
                    run_on_startup=True,
                    request_delay_ms=0,
                )
                daemon = SyncDaemon(cfg, mgr, client, store, notifier)

                async def scenario() -> None:
                    # First keepalive sees expired session -> fires 1 webhook alert
                    self.assertFalse(await daemon.keepalive_step())
                    # Second keepalive stays in cooldown -> does not spam webhook
                    self.assertFalse(await daemon.keepalive_step())
                    self.assertEqual(len(alerts), 1)
                    self.assertIn("msgtype", alerts[0][1])

                    # Simulate user dropping fresh curl.txt into mounted /config volume
                    curl_path = cfg_dir / "curl.txt"
                    curl_path.write_text(
                        "curl 'https://cloud.huawei.com/heartbeatCheck' "
                        "-H 'CSRFToken: fresh_tok' "
                        "-b 'shareToken=fresh_tok; HWWAFSESID=fresh_waf' "
                        "-H 'userid: 80086000000000001'",
                        encoding="utf-8",
                    )
                    future_t = time.time() + 10
                    os.utime(curl_path, (future_t, future_t))
                    server.auth_valid = True

                    # Keepalive hot-imports curl.txt and marks auth healthy
                    self.assertTrue(await daemon.keepalive_step())
                    self.assertEqual(mgr.ensure_session().csrf_token, "fresh_tok")

                    # Trigger sync & verify photo archived + health.json written
                    report = await daemon.execute_sync_once()
                    self.assertIsNotNone(report)
                    assert report is not None
                    self.assertEqual(report.downloaded, 1)
                    self.assertTrue((photos_dir / "2026/11/16/DAEMON_1.jpg").is_file())
                    health = json.loads((cfg_dir / "health.json").read_text(encoding="utf-8"))
                    self.assertTrue(health["ok"])
                    self.assertTrue(health["auth_valid"])

                asyncio.run(scenario())
            finally:
                store.close()

    def test_advanced_filters_name_id7_webhooks_and_cli_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = self._setup_env(tmp)
            try:
                server.add_media(
                    unique_id="id7_small_photo",
                    album_id="default-album-1",
                    file_name="SMALL.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"s"),
                )
                server.add_media(
                    unique_id="id7_large_live",
                    album_id="default-album-1",
                    file_name="BIG_LIVE.jpg",
                    file_type="5",
                    create_time_ms=_ts_ms(2026, 11, 14),
                    content=_make_jpeg_bytes(b"l" * 300),
                )
                server.add_media(
                    unique_id="id7_old_photo",
                    album_id="default-album-1",
                    file_name="OLD.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 10, 1),
                    content=_make_jpeg_bytes(b"o" * 300),
                )

                cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    albums=["相机"],
                    file_match_policy="name-id7",
                    skip_photos=True,
                    created_after_ms=_ts_ms(2026, 11, 1),
                    min_size=100,
                    request_delay_ms=0,
                )
                rep = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(rep.downloaded, 1)
                self.assertTrue((photos_dir / "2026/11/14/BIG_LIVE_id7_lar.jpg").is_file())
            finally:
                store.close()

            # Test webhook payload builders across providers
            _, feishu_b, _ = _build_webhook_request(
                "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
                event_type="auth_expired",
                title="T",
                message="M",
            )
            self.assertEqual(json.loads(feishu_b)["msg_type"], "text")

            tg_url, tg_b, _ = _build_webhook_request(
                "https://api.telegram.org/bot123/sendMessage?chat_id=999",
                event_type="sync_error",
                title="T",
                message="M",
            )
            self.assertNotIn("chat_id", tg_url)
            self.assertEqual(json.loads(tg_b)["chat_id"], "999")

            _, bark_b, _ = _build_webhook_request(
                "https://api.day.app/mykey/push",
                event_type="auth_expired",
                title="T",
                message="M",
            )
            self.assertEqual(json.loads(bark_b)["group"], "hwcloudpd")

            # Test CLI --import-curl and --healthcheck
            curl_src = Path(tmp) / "sample_curl.txt"
            curl_src.write_text(
                "curl 'https://cloud.huawei.com/heartbeatCheck' "
                "-H 'CSRFToken: tok_cli' "
                "-b 'shareToken=tok_cli; HWWAFSESID=waf_cli' "
                "-H 'userid: 80086000000000001'",
                encoding="utf-8",
            )
            cli_cfg_dir = Path(tmp) / "cli_cfg"
            rc = cli_main(["--config-dir", str(cli_cfg_dir), "--import-curl", str(curl_src)])
            self.assertEqual(rc, 0)
            self.assertTrue((cli_cfg_dir / "auth.json").is_file())

            # One-shot mode has nothing to monitor -> always healthy.
            self.assertEqual(cli_main(["--config-dir", str(cli_cfg_dir), "--healthcheck"]), 0)
            # Daemon mode: missing health.json -> unhealthy (1); fresh health.json -> healthy (0)
            hc = ["--config-dir", str(cli_cfg_dir), "--watch-with-interval", "3600", "--healthcheck"]
            self.assertEqual(cli_main(hc), 1)
            (cli_cfg_dir / "health.json").write_text(
                json.dumps({"ok": True, "updated_at": time.time()}),
                encoding="utf-8",
            )
            self.assertEqual(cli_main(hc), 0)
            # Invalid session -> unhealthy
            (cli_cfg_dir / "health.json").write_text(
                json.dumps({"ok": True, "auth_valid": False, "updated_at": time.time()}),
                encoding="utf-8",
            )
            self.assertEqual(cli_main(hc), 1)
            # Sync stuck for longer than max_sync_hours -> unhealthy
            (cli_cfg_dir / "health.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "auth_valid": True,
                        "updated_at": time.time(),
                        "sync_started_at": time.time() - 3 * 3600,
                        "max_sync_hours": 2,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(cli_main(hc), 1)

    def test_daemon_silent_auto_relogin_and_lockout_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, mgr, server, client, store = self._setup_env(tmp)
            try:
                server.add_media(
                    unique_id="auto_login_uid",
                    album_id="default-album-1",
                    file_name="AUTO_1.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 16),
                    content=_make_jpeg_bytes(b"auto_relogin"),
                )
                server.auth_valid = False
                alerts: list[str] = []

                class FakeAuth:
                    has_credentials = True

                    def __init__(self) -> None:
                        self.calls = 0
                        self.mode = "ok"

                    def login(self, *, interactive: bool = False) -> AuthSession:
                        self.calls += 1
                        if interactive:
                            raise AssertionError("Daemon must never call interactive=True")
                        if self.mode == "ok":
                            sess = AuthSession(
                                cookies={"shareToken": "tok_silent", "HWWAFSESID": "waf_silent"},
                                csrf_token="tok_silent",
                                user_id="80086000000000001",
                                device_id="c" * 64,
                            )
                            mgr.save(sess)
                            server.auth_valid = True
                            return sess
                        if self.mode == "2fa":
                            raise TwoFactorRequiredError("需要二次验证")
                        if self.mode == "bad_pw":
                            raise InvalidCredentialsError("帐号或密码错误")
                        raise AssertionError(self.mode)

                fake_auth = FakeAuth()
                notifier = Notifier(
                    webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
                    sender=lambda u, b, h: alerts.append(b.decode("utf-8")),
                )
                cfg = SyncConfig(
                    directory=photos_dir,
                    config_dir=cfg_dir,
                    username="13800138000",
                    password="secret",
                    albums=["相机"],
                    watch_interval=3600,
                    request_delay_ms=0,
                )
                daemon = SyncDaemon(
                    cfg,
                    mgr,
                    client,
                    store,
                    notifier,
                    authenticator=fake_auth,  # type: ignore[arg-type]
                    auto_relogin_cooldown_sec=1800.0,
                )

                async def scenario() -> None:
                    def auth_alerts() -> list[str]:
                        return [a for a in alerts if "首次同步完成" not in a]

                    # 1. Expired session -> silent auto-relogin succeeds with ZERO auth alerts!
                    self.assertTrue(await daemon.keepalive_step())
                    self.assertEqual(fake_auth.calls, 1)
                    self.assertEqual(len(auth_alerts()), 0)
                    report = await daemon.execute_sync_once()
                    self.assertIsNotNone(report)
                    assert report is not None
                    self.assertEqual(report.downloaded, 1)

                    # 2. Session expires and requires 2FA -> notifies once and blocks password
                    #    re-submission until the user acts (no code push / SMS every cycle).
                    server.auth_valid = False
                    fake_auth.mode = "2fa"
                    self.assertFalse(await daemon.keepalive_step())
                    self.assertFalse(await daemon.keepalive_step())
                    daemon._auto_relogin_blocked_until = 0.0  # time-based expiry alone must not unblock
                    self.assertFalse(await daemon.keepalive_step())
                    self.assertEqual(fake_auth.calls, 2)
                    self.assertEqual(len(auth_alerts()), 1)
                    self.assertIn("--auth-only", auth_alerts()[0])
                    health = json.loads((cfg_dir / "health.json").read_text(encoding="utf-8"))
                    self.assertFalse(health["auth_valid"])
                    self.assertEqual(health["auth_block_reason"], "two_factor_required")

                    # 3. User touches credentials (e.g. --auth-only) -> block lifted; an invalid
                    #    password then latches permanently to prevent account lockout.
                    future_t = time.time() + 30
                    os.utime(mgr.auth_file, (future_t, future_t))
                    fake_auth.mode = "bad_pw"
                    self.assertFalse(await daemon.keepalive_step())
                    self.assertFalse(await daemon.keepalive_step())
                    self.assertEqual(fake_auth.calls, 3)

                asyncio.run(scenario())
            finally:
                store.close()

    def test_cli_list_albums_triggers_cas_login_when_auth_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "config"
            server = FakeHuaweiCloudServer()
            login_calls: list[bool] = []

            class StubAuthenticator:
                def __init__(self, auth_manager: AuthManager, **kwargs) -> None:
                    self._auth_manager = auth_manager

                @property
                def has_credentials(self) -> bool:
                    return True

                @property
                def can_attempt_login(self) -> bool:
                    return True

                def login(self, *, interactive: bool = False) -> AuthSession:
                    login_calls.append(interactive)
                    sess = AuthSession(
                        cookies={"shareToken": "tok_list", "HWWAFSESID": "waf_list"},
                        csrf_token="tok_list",
                        user_id="80086000000000001",
                        device_id="d" * 64,
                    )
                    self._auth_manager.save(sess)
                    return sess

            def _make_client(auth_manager: AuthManager) -> HuaweiGalleryClient:
                return HuaweiGalleryClient(auth_manager, server, retry_base_delay=0.005)

            stdout_buf = io.StringIO()
            with (
                patch("hwcloudpd.cli.HuaweiCasAuthenticator", side_effect=StubAuthenticator),
                patch("hwcloudpd.cli.HuaweiGalleryClient", side_effect=_make_client),
                contextlib.redirect_stdout(stdout_buf),
            ):
                rc = cli_main(
                    ["--config-dir", str(cfg_dir), "-u", "13800138000", "-p", "secret", "--list-albums"]
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(login_calls), 1)
            self.assertTrue((cfg_dir / "auth.json").is_file())
            self.assertIn("default-album-1\t相机", stdout_buf.getvalue())
            self.assertIn("default-album-3\t最近删除 [recycle]", stdout_buf.getvalue())

    def test_list_albums_preserves_photonum_when_stats_endpoint_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, server, client, store = self._setup_env(tmp)
            try:
                server.albums.append(
                    {
                        "albumId": "default-album-200-test",
                        "albumName": "Travel",
                        "albumVersion": "",
                        "photoNum": 42,
                        "videoNum": 3,
                    }
                )
                orig_request = server.request

                def _patched_request(
                    method: str, url: str, headers: dict[str, str], body: bytes | None, **kwargs
                ):
                    if url.endswith("/album/galleryAlbumStatInfo"):
                        return HttpResponse(
                            status=200,
                            headers={},
                            set_cookies=[],
                            body=json.dumps({"code": -1, "info": "legacy stat unsupported"}).encode("utf-8"),
                        )
                    return orig_request(method, url, headers, body, **kwargs)

                server.request = _patched_request  # type: ignore[method-assign]
                albums = client.list_albums(include_stats=True)
                by_id = {a.album_id: a for a in albums}
                self.assertEqual(by_id["default-album-200-test"].img_num, 42)
                self.assertEqual(by_id["default-album-200-test"].video_num, 3)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
