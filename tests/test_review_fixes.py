"""Regression tests for the code-review fixes (data safety, robustness, config)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from hwcloudpd.auth import AuthManager
from hwcloudpd.cli import build_parser, main as cli_main
from hwcloudpd.client import HttpResponse, HuaweiGalleryClient
from hwcloudpd.daemon import SyncDaemon
from hwcloudpd.exif import has_exif_datetime_original, inject_exif_datetime_if_missing
from hwcloudpd.models import (
    AuthExpiredError,
    AuthSession,
    ConfigError,
    HwCloudError,
    MediaItem,
    RateLimitError,
    SyncConfig,
)
from hwcloudpd.notifier import Notifier
from hwcloudpd.store import StateStore
from hwcloudpd.sync import PART_DIR_NAME, SyncEngine

from test_sync_and_daemon import FakeHuaweiCloudServer, _make_jpeg_bytes, _ts_ms


def _setup(tmp: str):
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
    client = HuaweiGalleryClient(mgr, server, retry_base_delay=0.001)
    store = StateStore(cfg_dir / "state.db")
    return cfg_dir, photos_dir, mgr, server, client, store


def _cfg(photos_dir: Path, cfg_dir: Path, **kw) -> SyncConfig:
    kw.setdefault("request_delay_ms", 0)
    return SyncConfig(directory=photos_dir, config_dir=cfg_dir, **kw)


def _all_files(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and PART_DIR_NAME not in p.parts
    )


class AdoptionAndDuplicateTests(unittest.TestCase):
    def test_lost_state_db_adopts_existing_files_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            for i in range(3):
                server.add_media(
                    unique_id=f"uid_{i}",
                    album_id="default-album-1",
                    file_name=f"IMG_{i}.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(f"payload{i}".encode()),
                )
            cfg = _cfg(photos_dir, cfg_dir, albums=["相机"])
            self.assertEqual(SyncEngine(cfg, client, store).run_once().downloaded, 3)
            before = _all_files(photos_dir)
            store.close()

            # state.db lost / migrated from another tool
            for f in cfg_dir.glob("state.db*"):
                f.unlink()
            store2 = StateStore(cfg_dir / "state.db")
            try:
                rep = SyncEngine(cfg, client, store2).run_once()
                self.assertEqual(rep.downloaded, 0)
                self.assertEqual(rep.adopted, 3)
                self.assertEqual(_all_files(photos_dir), before)
                self.assertIsNotNone(store2.get_record("uid_0"))
            finally:
                store2.close()

    def test_same_name_different_content_still_gets_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                target = photos_dir / "2026/11/12/IMG_1.jpg"
                target.parent.mkdir(parents=True)
                target.write_bytes(b"someone else's file")
                content = _make_jpeg_bytes(b"cloud")
                server.add_media(
                    unique_id="cloud_uid",
                    album_id="default-album-1",
                    file_name="IMG_1.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=content,
                )
                rep = SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()
                self.assertEqual(rep.downloaded, 1)
                self.assertEqual(target.read_bytes(), b"someone else's file")
                self.assertTrue((photos_dir / f"2026/11/12/IMG_1-{len(content)}.jpg").is_file())
            finally:
                store.close()

    def test_postprocess_failure_does_not_lose_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="pp_uid",
                    album_id="default-album-1",
                    file_name="PP.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"pp"),
                )
                cfg = _cfg(photos_dir, cfg_dir, albums=["相机"], xmp_sidecar=True)
                with patch("hwcloudpd.sync.write_xmp_sidecar", side_effect=OSError("disk full")):
                    rep = SyncEngine(cfg, client, store).run_once()
                self.assertEqual((rep.downloaded, rep.failed), (1, 0))
                self.assertIsNotNone(store.get_record("pp_uid"))
                # Next run: no duplicate download
                rep2 = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(rep2.downloaded, 0)
                self.assertEqual(len([f for f in _all_files(photos_dir) if f.endswith(".jpg")]), 1)
            finally:
                store.close()

    def test_part_files_are_uid_based_and_resumable_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                engine = SyncEngine(_cfg(photos_dir, cfg_dir), client, store)
                item = MediaItem(unique_id="abc", album_id="a", album_name="x", file_type="1", create_time_ms=0)
                self.assertEqual(
                    engine._part_path(item),
                    photos_dir / PART_DIR_NAME / "abc.original.hwpart",
                )
            finally:
                store.close()


class MoveModeSafetyTests(unittest.TestCase):
    def _two_old_items(self, server: FakeHuaweiCloudServer) -> None:
        old_ms = int((time.time() - 60 * 86400) * 1000)
        for uid in ("old_a", "old_b"):
            server.add_media(
                unique_id=uid,
                album_id="default-album-1",
                file_name=f"{uid}.jpg",
                file_type="1",
                create_time_ms=old_ms,
                content=_make_jpeg_bytes(uid.encode()),
            )

    def test_unknown_delete_outcome_never_triggers_local_auto_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                self._two_old_items(server)
                SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()

                # The server processes the delete but the response is lost (network error).
                orig = server.request

                def flaky(method, url, headers, body=None, **kw):
                    if url.endswith("/album/deleteAlbumFile"):
                        orig(method, url, headers, body, **kw)
                        raise OSError("connection reset")
                    return orig(method, url, headers, body, **kw)

                server.request = flaky  # type: ignore[method-assign]
                move_cfg = _cfg(photos_dir, cfg_dir, albums=["相机"], keep_cloud_recent_days=30, auto_delete=True)
                rep = SyncEngine(move_cfg, client, store).run_once()
                self.assertEqual(rep.deleted_cloud, 0)
                self.assertEqual(store.get_record("old_a")["cloud_state"], "delete_pending")

                # Next run: items are in the Recycle Bin. auto-delete must NOT treat them as
                # user deletions; reconciliation confirms our own deletion instead.
                server.request = orig  # type: ignore[method-assign]
                rep2 = SyncEngine(move_cfg, client, store).run_once()
                self.assertEqual(rep2.deleted_local, 0)
                for uid in ("old_a", "old_b"):
                    rec = store.get_record(uid)
                    self.assertEqual(rec["cloud_state"], "deleted")
                    self.assertTrue((photos_dir / rec["local_rel_path"]).is_file())
            finally:
                store.close()

    def test_partial_success_list_only_marks_confirmed_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                self._two_old_items(server)
                SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()
                orig = server.request

                def partial(method, url, headers, body=None, **kw):
                    if url.endswith("/album/deleteAlbumFile"):
                        payload = json.loads(body)
                        payload["uniqueId"] = payload["uniqueId"][:1]
                        return orig(method, url, headers, json.dumps(payload).encode(), **kw)
                    return orig(method, url, headers, body, **kw)

                server.request = partial  # type: ignore[method-assign]
                rep = SyncEngine(
                    _cfg(photos_dir, cfg_dir, albums=["相机"], keep_cloud_recent_days=30), client, store
                ).run_once()
                self.assertEqual(rep.deleted_cloud, 1)
                states = sorted(store.get_record(u)["cloud_state"] for u in ("old_a", "old_b"))
                self.assertEqual(states, ["delete_failed", "deleted"])
            finally:
                store.close()

    def test_delete_after_download_scope_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                self._two_old_items(server)
                # Backed up earlier WITHOUT move mode: must not be deleted just by enabling it.
                SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()
                server.add_media(
                    unique_id="fresh", album_id="default-album-1", file_name="FRESH.jpg",
                    file_type="1", create_time_ms=_ts_ms(2026, 11, 1), content=_make_jpeg_bytes(b"fresh"),
                )
                orig = server.request

                def reject(method, url, headers, body=None, **kw):
                    if url.endswith("/album/deleteAlbumFile"):
                        return HttpResponse(200, {}, [], json.dumps({"code": "500", "info": "busy"}).encode())
                    return orig(method, url, headers, body, **kw)

                server.request = reject  # type: ignore[method-assign]
                move = _cfg(photos_dir, cfg_dir, albums=["相机"], delete_after_download=True)
                rep = SyncEngine(move, client, store).run_once()
                self.assertEqual((rep.downloaded, rep.deleted_cloud), (1, 0))
                self.assertEqual(store.get_record("fresh")["cloud_state"], "delete_failed")
                self.assertEqual(store.get_record("old_a")["cloud_state"], "present")

                server.request = orig  # type: ignore[method-assign]
                rep2 = SyncEngine(move, client, store).run_once()
                self.assertEqual(rep2.deleted_cloud, 1)
                self.assertEqual(store.get_record("fresh")["cloud_state"], "deleted")
                self.assertEqual(store.get_record("old_a")["cloud_state"], "present")
            finally:
                store.close()

    def test_shared_albums_are_never_deleted_and_recycle_flag_is_always_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                orig = server.request

                def with_shared(method, url, headers, body=None, **kw):
                    if url.endswith("/album/queryAlbumInfo"):
                        return HttpResponse(
                            status=200,
                            headers={},
                            set_cookies=[],
                            body=json.dumps(
                                {
                                    "code": 0,
                                    "ownAlbumList": server.albums,
                                    "shareAlbumList": [
                                        {"albumId": "share-1", "albumName": "家人共享", "ownerId": "999"}
                                    ],
                                }
                            ).encode(),
                        )
                    return orig(method, url, headers, body, **kw)

                server.request = with_shared  # type: ignore[method-assign]
                server.add_media(
                    unique_id="shared_pic",
                    album_id="share-1",
                    file_name="S.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 1, 1),
                    content=_make_jpeg_bytes(b"shared"),
                )
                server.add_media(
                    unique_id="own_pic",
                    album_id="default-album-1",
                    file_name="O.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 1, 1),
                    content=_make_jpeg_bytes(b"own"),
                )
                albums = {a.album_id: a for a in client.list_albums()}
                self.assertTrue(albums["share-1"].is_shared)
                self.assertFalse(albums["default-album-1"].is_shared)

                cfg = _cfg(photos_dir, cfg_dir, exclude_albums=["截屏*"], delete_after_download=True)
                rep = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(rep.downloaded, 2)
                self.assertEqual(rep.deleted_cloud, 1)
                deleted_ids = [u for call in server.deleted_cloud_calls for u in call["uniqueId"]]
                self.assertEqual(deleted_ids, ["own_pic"])
                self.assertTrue(all(c["recycle"] == "1" for c in server.deleted_cloud_calls))
                self.assertEqual(store.get_record("shared_pic")["cloud_state"], "present")
                self.assertEqual(store.get_record("shared_pic")["is_shared"], 1)
            finally:
                store.close()

    def test_restored_item_is_not_downloaded_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                self._two_old_items(server)
                base = _cfg(photos_dir, cfg_dir, albums=["相机"])
                SyncEngine(base, client, store).run_once()
                SyncEngine(
                    _cfg(photos_dir, cfg_dir, albums=["相机"], keep_cloud_recent_days=30), client, store
                ).run_once()
                # User restores old_a from the Recycle Bin
                item = next(i for i in server.items_by_album["default-album-3"] if i["uniqueId"] == "old_a")
                server.items_by_album["default-album-3"].remove(item)
                item["albumId"] = "default-album-1"
                server.items_by_album["default-album-1"].append(item)
                rep = SyncEngine(base, client, store).run_once()
                self.assertEqual(rep.downloaded, 0)
                self.assertEqual(store.get_record("old_a")["cloud_state"], "present")
            finally:
                store.close()


class StoreMigrationTests(unittest.TestCase):
    def test_v1_database_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """CREATE TABLE synced_media (
                    unique_id TEXT PRIMARY KEY, album_id TEXT NOT NULL, album_name TEXT NOT NULL,
                    file_name TEXT NOT NULL, file_type TEXT NOT NULL, file_size INTEGER NOT NULL DEFAULT 0,
                    checksum TEXT NOT NULL DEFAULT '', local_sha256 TEXT NOT NULL DEFAULT '',
                    create_time_ms INTEGER NOT NULL DEFAULT 0, local_rel_path TEXT NOT NULL,
                    size_variant TEXT NOT NULL DEFAULT 'original', status TEXT NOT NULL, synced_at INTEGER NOT NULL)"""
            )
            conn.execute(
                "INSERT INTO synced_media VALUES ('u1','a','A','f.jpg','1',1,'','',0,'f.jpg','original','deleted_cloud',0)"
            )
            conn.commit()
            conn.close()
            with StateStore(db) as store:
                rec = store.get_record("u1")
                self.assertEqual(rec["status"], "downloaded")
                self.assertEqual(rec["cloud_state"], "deleted")
                self.assertEqual(rec["is_shared"], 0)


class ExifTests(unittest.TestCase):
    def test_existing_exif_without_datetime_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Minimal EXIF with only an Orientation tag (no DateTimeOriginal)
            tiff = struct.pack(">2sHI", b"MM", 42, 8) + struct.pack(">H", 1)
            tiff += struct.pack(">HHIHH", 0x0112, 3, 1, 6, 0) + struct.pack(">I", 0)
            app1 = b"Exif\x00\x00" + tiff
            jpg = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"
            p = Path(tmp) / "a.jpg"
            p.write_bytes(jpg)
            from datetime import datetime

            self.assertFalse(inject_exif_datetime_if_missing(p, datetime(2020, 1, 1)))
            self.assertEqual(p.read_bytes(), jpg)

    def test_injection_goes_after_jfif_app0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from datetime import datetime

            p = Path(tmp) / "b.jpg"
            p.write_bytes(_make_jpeg_bytes(b"x"))
            self.assertTrue(inject_exif_datetime_if_missing(p, datetime(2020, 1, 1)))
            data = p.read_bytes()
            self.assertEqual(data[2:4], b"\xff\xe0")  # APP0 still first
            app0_len = struct.unpack(">H", data[4:6])[0]
            self.assertEqual(data[4 + app0_len : 6 + app0_len], b"\xff\xe1")
            self.assertTrue(has_exif_datetime_original(p))

    def test_local_sha_matches_final_file_after_exif_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="exif_uid",
                    album_id="default-album-1",
                    file_name="E.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"e"),
                )
                SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"], set_exif_datetime=True), client, store).run_once()
                rec = store.get_record("exif_uid")
                actual = hashlib.sha256((photos_dir / rec["local_rel_path"]).read_bytes()).hexdigest()
                self.assertEqual(rec["local_sha256"], actual)
            finally:
                store.close()


class ScanBehaviourTests(unittest.TestCase):
    def test_burst_cache_avoids_repeated_burstphotolist_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="cover",
                    album_id="default-album-1",
                    file_name="B0.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 10),
                    content=_make_jpeg_bytes(b"b0"),
                    is_cover=1,
                )
                frame = _make_jpeg_bytes(b"b1")
                server.blobs["frame"] = frame
                server._meta_map["frame"] = {
                    "uniqueId": "frame",
                    "albumId": "default-album-1",
                    "fileName": "B1.jpg",
                    "fileSize": len(frame),
                    "fileType": "1",
                    "sha256": hashlib.sha256(frame).hexdigest(),
                    "createTime": _ts_ms(2026, 11, 10),
                }
                server.bursts["cover"] = [
                    {"albumId": "default-album-1", "uniqueId": "cover", "fileType": "1"},
                    {"albumId": "default-album-1", "uniqueId": "frame", "fileType": "1"},
                ]
                calls: list[str] = []
                orig = server.request

                def counting(method, url, headers, body=None, **kw):
                    if url.endswith("/album/burstphotolist"):
                        calls.append(url)
                    return orig(method, url, headers, body, **kw)

                server.request = counting  # type: ignore[method-assign]
                cfg = _cfg(photos_dir, cfg_dir, albums=["相机"])
                self.assertEqual(SyncEngine(cfg, client, store).run_once().downloaded, 2)
                self.assertEqual(len(calls), 1)
                rep = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(rep.skipped_existing, 2)
                self.assertEqual(len(calls), 1)
            finally:
                store.close()

    def test_failed_items_back_off_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="bad",
                    album_id="default-album-1",
                    file_name="BAD.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"bad"),
                )
                server.corrupt_uid = "bad"
                cfg = _cfg(photos_dir, cfg_dir, albums=["相机"])
                self.assertEqual(SyncEngine(cfg, client, store).run_once().failed, 1)
                rep2 = SyncEngine(cfg, client, store).run_once()
                self.assertEqual((rep2.failed, rep2.deferred_failed), (0, 1))
            finally:
                store.close()

    def test_full_scan_redownloads_changed_cloud_content_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="edit",
                    album_id="default-album-1",
                    file_name="EDIT.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"v1"),
                )
                cfg = _cfg(photos_dir, cfg_dir, albums=["相机"])
                SyncEngine(cfg, client, store).run_once()
                path = photos_dir / store.get_record("edit")["local_rel_path"]
                v2 = _make_jpeg_bytes(b"version-2-edited")
                server.blobs["edit"] = v2
                server._meta_map["edit"].update(fileSize=len(v2), sha256=hashlib.sha256(v2).hexdigest())
                self.assertEqual(SyncEngine(cfg, client, store).run_once().redownloaded_changed, 0)
                rep = SyncEngine(cfg, client, store).run_once(force_full_scan=True)
                self.assertEqual(rep.redownloaded_changed, 1)
                self.assertEqual(path.read_bytes(), v2)
                self.assertEqual(len(_all_files(photos_dir)), 1)
            finally:
                store.close()

    def test_recent_uses_global_timeline_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="cam_old", album_id="default-album-1", file_name="OLD.jpg",
                    file_type="1", create_time_ms=_ts_ms(2020, 1, 1), content=_make_jpeg_bytes(b"o"),
                )
                server.add_media(
                    unique_id="shot_new", album_id="default-album-2", file_name="NEW.jpg",
                    file_type="1", create_time_ms=_ts_ms(2026, 1, 1), content=_make_jpeg_bytes(b"n"),
                )
                rep = SyncEngine(_cfg(photos_dir, cfg_dir, recent=1), client, store).run_once()
                self.assertEqual(rep.downloaded, 1)
                self.assertIsNotNone(store.get_record("shot_new"))
                self.assertIsNone(store.get_record("cam_old"))
            finally:
                store.close()

    def test_request_stop_cancels_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="x", album_id="default-album-1", file_name="X.jpg",
                    file_type="1", create_time_ms=_ts_ms(2026, 1, 1), content=_make_jpeg_bytes(b"x"),
                )
                engine = SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store)
                engine.request_stop()
                rep = engine.run_once()
                self.assertTrue(rep.cancelled)
                self.assertEqual(rep.downloaded, 0)
            finally:
                store.close()

    def test_auth_expiry_during_concurrent_download_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                for i in range(6):
                    server.add_media(
                        unique_id=f"c{i}", album_id="default-album-1", file_name=f"C{i}.jpg",
                        file_type="1", create_time_ms=_ts_ms(2026, 1, 1), content=_make_jpeg_bytes(b"c%d" % i),
                    )
                attempts: list[str] = []
                orig_dl = server.stream_download

                def expiring(url, headers, part_path, **kw):
                    attempts.append(url)
                    server.auth_valid = False
                    from urllib.error import HTTPError

                    raise HTTPError(url, 401, "expired", {}, None)

                server.stream_download = expiring  # type: ignore[method-assign]
                engine = SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"], download_concurrency=2), client, store)
                with self.assertRaises(AuthExpiredError):
                    engine.run_once()
                self.assertLess(len(attempts), 6)
                server.stream_download = orig_dl  # type: ignore[method-assign]
            finally:
                store.close()


class ClientErrorClassificationTests(unittest.TestCase):
    def test_business_error_probes_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, server, client, store = _setup(tmp)
            try:
                orig = server.request
                heartbeat_ok = {"value": True}

                def handler(method, url, headers, body=None, **kw):
                    if url.endswith("/album/getSimpleFile"):
                        return HttpResponse(200, {}, [], json.dumps({"code": "31004", "info": "x"}).encode())
                    if "/heartbeatCheck" in url and not heartbeat_ok["value"]:
                        return HttpResponse(200, {}, [], json.dumps({"code": "401"}).encode())
                    return orig(method, url, headers, body, **kw)

                server.request = handler  # type: ignore[method-assign]
                with self.assertRaises(HwCloudError) as ctx:
                    next(client.iter_simple_files("default-album-1"))
                self.assertNotIsInstance(ctx.exception, AuthExpiredError)
                heartbeat_ok["value"] = False
                with self.assertRaises(AuthExpiredError):
                    next(client.iter_simple_files("default-album-1"))
            finally:
                store.close()

    def test_403_with_valid_session_is_rate_limit_not_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, server, client, store = _setup(tmp)
            try:
                orig = server.request

                def handler(method, url, headers, body=None, **kw):
                    if url.endswith("/album/galleryStatInfo"):
                        return HttpResponse(403, {}, [], b"forbidden")
                    return orig(method, url, headers, body, **kw)

                server.request = handler  # type: ignore[method-assign]
                client.max_retries = 0
                with self.assertRaises(RateLimitError):
                    client.get_gallery_stats()
            finally:
                store.close()

    def test_synthesized_heic_name_gets_heic_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, server, client, store = _setup(tmp)
            try:
                from hwcloudpd.models import MediaStub

                stub = MediaStub(unique_id="nometa123456", album_id="default-album-1", album_name="相机",
                                 file_type="9", create_time_ms=0)
                orig = server.request

                def handler(method, url, headers, body=None, **kw):
                    if url.endswith("/album/getThumbLcdUrl"):
                        return HttpResponse(200, {}, [], json.dumps({"code": 0, "successList": []}).encode())
                    return orig(method, url, headers, body, **kw)

                server.request = handler  # type: ignore[method-assign]
                item = client.enrich_metadata([stub])[0]
                self.assertTrue(item.name_synthesized)
                self.assertTrue(item.file_name.endswith(".heic"))
            finally:
                store.close()


class CredentialRobustnessTests(unittest.TestCase):
    def test_invalid_curl_txt_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AuthManager(Path(tmp))
            (Path(tmp) / "curl.txt").write_text("curl 'https://x' -H 'foo: bar'", encoding="utf-8")
            with self.assertRaises(AuthExpiredError):
                mgr.ensure_session()
            self.assertFalse((Path(tmp) / "curl.txt").exists())
            self.assertTrue((Path(tmp) / "curl.txt.invalid").exists())
            self.assertIn("curl.txt", mgr.last_import_error)

    def test_corrupt_auth_json_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "auth.json").write_text("{not json", encoding="utf-8")
            mgr = AuthManager(Path(tmp))
            with self.assertRaises(AuthExpiredError):
                mgr.ensure_session()
            self.assertFalse((Path(tmp) / "auth.json").exists())
            self.assertTrue(list(Path(tmp).glob("auth.json.corrupt-*")))

    def test_rotation_does_not_overwrite_newer_auth_json_from_other_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon_mgr = AuthManager(Path(tmp))
            daemon_mgr.save(AuthSession(cookies={"shareToken": "old"}, csrf_token="old", user_id="u"))
            daemon_mgr.ensure_session()
            # Another process (docker exec --auth-only) writes a fresh session
            other = AuthManager(Path(tmp))
            other.save(AuthSession(cookies={"shareToken": "fresh", "CASTGC": "new"}, csrf_token="fresh", user_id="u"))
            future = time.time() + 5
            os.utime(Path(tmp) / "auth.json", (future, future))
            daemon_mgr.update_from_response(["shareToken=rotated_old; Path=/"], {})
            data = json.loads((Path(tmp) / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(data["csrf_token"], "fresh")
            self.assertEqual(daemon_mgr.ensure_session().cookies.get("CASTGC"), "new")

    def test_cas_cookie_jar_reloads_when_changed_on_disk(self) -> None:
        from http.cookiejar import Cookie, LWPCookieJar

        from hwcloudpd.cas_auth import HuaweiCasAuthenticator

        with tempfile.TemporaryDirectory() as tmp:
            mgr = AuthManager(Path(tmp))
            auth = HuaweiCasAuthenticator(mgr)
            self.assertFalse(auth._has_cas_tgt())
            jar = LWPCookieJar(str(Path(tmp) / "cas_cookies.lwp"))
            jar.set_cookie(
                Cookie(0, "CASTGC", "tgt", None, False, ".id1.cloud.huawei.com", True, True, "/", True,
                       True, int(time.time()) + 3600, False, None, None, {})
            )
            jar.save(ignore_discard=True)
            self.assertTrue(auth._has_cas_tgt())


class DaemonPolicyTests(unittest.TestCase):
    def test_cli_always_passes_authenticator_and_drops_2fa_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captured: dict = {}

            class FakeDaemon:
                def __init__(self, *args, authenticator=None, **kwargs):
                    captured["auth"] = authenticator

                async def run_forever(self):
                    return None

            with patch("hwcloudpd.cli.SyncDaemon", FakeDaemon):
                rc = cli_main(
                    ["--config-dir", str(Path(tmp) / "c"), "-d", str(Path(tmp) / "p"),
                     "--watch-with-interval", "60", "--two-fa-code", "123456"]
                )
            self.assertEqual(rc, 0)
            self.assertIsNotNone(captured["auth"])
            self.assertIsNone(captured["auth"].two_fa_code)

    def test_generic_login_failure_uses_exponential_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, mgr, server, client, store = _setup(tmp)
            try:
                server.auth_valid = False

                class Boom:
                    has_credentials = True
                    calls = 0

                    def login(self, *, interactive=False):
                        Boom.calls += 1
                        raise HwCloudError("network down")

                notifier = Notifier()
                daemon = SyncDaemon(_cfg(photos_dir, cfg_dir, watch_interval=60), mgr, client, store, notifier,
                                    authenticator=Boom())  # type: ignore[arg-type]

                async def scenario():
                    await daemon.keepalive_step()
                    first = daemon._auto_relogin_blocked_until - time.monotonic()
                    daemon._auto_relogin_blocked_until = 0.0
                    await daemon.keepalive_step()
                    second = daemon._auto_relogin_blocked_until - time.monotonic()
                    return first, second

                first, second = asyncio.run(scenario())
                self.assertEqual(Boom.calls, 2)
                self.assertGreater(second, first * 1.5)
            finally:
                store.close()

    def test_full_scan_timestamp_persisted_in_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, mgr, server, client, store = _setup(tmp)
            try:
                daemon = SyncDaemon(_cfg(photos_dir, cfg_dir, watch_interval=60, albums=["相机"]),
                                    mgr, client, store, Notifier())
                asyncio.run(daemon.execute_sync_once())
                self.assertIsNotNone(store.get_meta("last_full_scan_at"))
            finally:
                store.close()


class ConfigAndNotifierTests(unittest.TestCase):
    def test_validate_rejects_contradictions(self) -> None:
        bad = [
            dict(delete_after_download=True, keep_cloud_recent_days=3),
            dict(skip_photos=True, skip_videos=True, skip_live_photos=True),
            dict(recent=-1),
            dict(min_size=10, max_size=5),
            dict(folder_structure="{:%Y}/{albm}"),
            dict(timezone="Mars/Olympus"),
            dict(cron="61 * * * *"),
            dict(delete_after_download=True, photo_size="thumb"),
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ConfigError):
                SyncConfig(**kw).validate()
        SyncConfig(auto_delete=True, keep_cloud_recent_days=30).validate()

    def test_cli_returns_2_on_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli_main(["--config-dir", tmp, "--delete-after-download", "--keep-cloud-recent-days", "3"])
            self.assertEqual(rc, 2)

    def test_boolean_env_defaults_can_be_disabled_on_cli_and_empty_env_falls_back(self) -> None:
        env = {"HWCLOUD_SET_EXIF_DATETIME": "true", "HWCLOUD_SIZE": "", "HWCLOUD_FOLDER_STRUCTURE": ""}
        with patch.dict(os.environ, env):
            ns = build_parser().parse_args(["--no-set-exif-datetime"])
        self.assertFalse(ns.set_exif_datetime)
        self.assertEqual(ns.photo_size, "original")
        self.assertEqual(ns.folder_structure, "{:%Y/%m/%d}")

    def test_password_file(self) -> None:
        from hwcloudpd.cli import args_to_config

        with tempfile.TemporaryDirectory() as tmp:
            pw = Path(tmp) / "pw"
            pw.write_text("s3cret\n", encoding="utf-8")
            with patch.dict(os.environ, {"HUAWEI_PASSWORD": ""}):
                ns = build_parser().parse_args(["--password-file", str(pw)])
            self.assertEqual(args_to_config(ns).password, "s3cret")

    def test_sync_error_notifications_are_deduplicated(self) -> None:
        sent: list[bytes] = []
        n = Notifier(webhook_url="https://example.com/hook", sender=lambda u, b, h: sent.append(b))
        self.assertTrue(n.notify("sync_error", "t", "m"))
        self.assertFalse(n.notify("sync_error", "t", "m"))
        n.reset_cooldown("sync_error")
        self.assertTrue(n.notify("sync_error", "t", "m"))
        self.assertEqual(len(sent), 2)

    def test_notification_script_nonzero_exit_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fail.sh"
            script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            script.chmod(0o755)
            self.assertFalse(Notifier(notification_script=str(script)).notify("x", "t", "m"))


if __name__ == "__main__":
    unittest.main()
