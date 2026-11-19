"""Regression tests for Round-2 code review findings (logic, security, concurrency, CLI/Docker)."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
from http.cookiejar import Cookie, LWPCookieJar
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from hwcloudpd.auth import AuthManager
from hwcloudpd.cas_auth import (
    HuaweiCasAuthenticator,
    _pick_pending_2fa_channel,
    _validate_huawei_https_url,
)
from hwcloudpd.cli import main as cli_main
from hwcloudpd.client import (
    DownloadResult,
    HttpResponse,
    HuaweiGalleryClient,
    UrllibTransport,
    _SafeCrossOriginRedirectHandler,
    _is_cloud_huawei_host,
)
from hwcloudpd.config import env_bool
from hwcloudpd.cron import CronSchedule
from hwcloudpd.daemon import SyncDaemon
from hwcloudpd.fileutil import exclusive_file_lock, write_secret_file_atomic
from hwcloudpd.models import (
    AuthExpiredError,
    AuthSession,
    ConfigError,
    HwCloudError,
    MediaStub,
    SyncCancelledError,
    SyncConfig,
    SyncReport,
)
from hwcloudpd.notifier import Notifier
from hwcloudpd.postprocess import sanitize_component
from hwcloudpd.store import (
    CLOUD_DELETE_FAILED,
    CLOUD_DELETE_PENDING,
    CLOUD_DELETED,
    CLOUD_MISSING,
    StateStore,
)
from hwcloudpd.sync import MAX_CLOUD_DELETE_RETRIES, SyncEngine

from test_review_fixes import _cfg, _setup
from test_sync_and_daemon import _make_jpeg_bytes, _make_mp4_bytes, _ts_ms


class Round2HighPriorityLogicTests(unittest.TestCase):
    def test_daemon_startup_syncs_once_and_respects_no_run_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, mgr, server, client, store = _setup(tmp)
            try:
                # 1) run_on_startup=True -> keepalive_step() must NOT pre-arm _immediate_sync_event
                daemon_on = SyncDaemon(
                    _cfg(photos_dir, cfg_dir, watch_interval=3600, run_on_startup=True),
                    mgr,
                    client,
                    store,
                    Notifier(),
                )

                async def check_startup_on() -> None:
                    self.assertTrue(await daemon_on.keepalive_step())
                    self.assertTrue(daemon_on._auth_ready_event.is_set())
                    self.assertFalse(daemon_on._immediate_sync_event.is_set())

                    # Mid-run expiry + reload SHOULD arm _immediate_sync_event
                    daemon_on._auth_valid = False
                    future = time.time() + 10
                    os.utime(mgr.auth_file, (future, future))
                    self.assertTrue(await daemon_on.keepalive_step())
                    self.assertTrue(daemon_on._immediate_sync_event.is_set())

                asyncio.run(check_startup_on())

                # 2) run_on_startup=False -> scheduler_loop must NOT run sync immediately on startup
                sync_calls = 0

                class CountingEngine:
                    def run_once(self, *, force_full_scan: bool = False) -> SyncReport:
                        nonlocal sync_calls
                        sync_calls += 1
                        return SyncReport()

                    def request_stop(self) -> None:
                        pass

                mgr2 = AuthManager(cfg_dir)
                daemon_off = SyncDaemon(
                    _cfg(photos_dir, cfg_dir, watch_interval=3600, run_on_startup=False),
                    mgr2,
                    client,
                    store,
                    Notifier(),
                    engine_factory=CountingEngine,  # type: ignore[arg-type]
                )

                async def check_no_run_on_startup() -> None:
                    self.assertTrue(await daemon_off.keepalive_step())
                    task = asyncio.create_task(daemon_off.scheduler_loop())
                    await asyncio.sleep(0.05)
                    daemon_off.request_stop()
                    await task

                asyncio.run(check_no_run_on_startup())
                self.assertEqual(sync_calls, 0)
            finally:
                store.close()

    def test_virtual_album_move_mode_only_deletes_matching_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                old_ms = int((time.time() - 60 * 86400) * 1000)
                server.add_media(
                    unique_id="fav_pic",
                    album_id="default-album-1",
                    file_name="FAV.jpg",
                    file_type="1",
                    create_time_ms=old_ms,
                    content=_make_jpeg_bytes(b"fav"),
                    is_fav=1,
                )
                server.add_media(
                    unique_id="normal_pic",
                    album_id="default-album-1",
                    file_name="NORM.jpg",
                    file_type="1",
                    create_time_ms=old_ms,
                    content=_make_jpeg_bytes(b"norm"),
                    is_fav=0,
                )
                server.add_media(
                    unique_id="old_vid",
                    album_id="default-album-1",
                    file_name="VID.mp4",
                    file_type="4",
                    create_time_ms=old_ms,
                    content=b"\x00\x00\x00\x14ftypisom" + b"video_bytes",
                    is_fav=0,
                )
                # Initial full backup without move mode
                SyncEngine(_cfg(photos_dir, cfg_dir), client, store).run_once()

                # Move mode scoped to "我的收藏" must delete ONLY fav_pic
                rep_fav = SyncEngine(
                    _cfg(photos_dir, cfg_dir, albums=["我的收藏"], keep_cloud_recent_days=30),
                    client,
                    store,
                ).run_once()
                self.assertEqual(rep_fav.deleted_cloud, 1)
                self.assertEqual(store.get_record("fav_pic")["cloud_state"], CLOUD_DELETED)
                self.assertEqual(store.get_record("normal_pic")["cloud_state"], "present")
                self.assertEqual(store.get_record("old_vid")["cloud_state"], "present")

                # Move mode scoped to "所有视频" must delete ONLY old_vid (not normal_pic)
                rep_vid = SyncEngine(
                    _cfg(photos_dir, cfg_dir, albums=["所有视频"], keep_cloud_recent_days=30),
                    client,
                    store,
                ).run_once()
                self.assertEqual(rep_vid.deleted_cloud, 1)
                self.assertEqual(store.get_record("old_vid")["cloud_state"], CLOUD_DELETED)
                self.assertEqual(store.get_record("normal_pic")["cloud_state"], "present")
            finally:
                store.close()

    def test_motion_photo_extraction_never_overwrites_existing_untracked_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                existing_mp4 = photos_dir / "2026/11/12/IMG_001.mp4"
                existing_mp4.parent.mkdir(parents=True)
                existing_mp4.write_bytes(b"real_user_video_do_not_overwrite")

                live_jpg = _make_jpeg_bytes(b"live", live_mp4=_make_mp4_bytes())
                server.add_media(
                    unique_id="live_1",
                    album_id="default-album-1",
                    file_name="IMG_001.jpg",
                    file_type="5",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=live_jpg,
                )
                cfg = _cfg(
                    photos_dir,
                    cfg_dir,
                    albums=["相机"],
                    live_photo_mov_filename_policy="original",
                )
                rep = SyncEngine(cfg, client, store).run_once()
                self.assertEqual(rep.downloaded, 1)
                self.assertEqual(existing_mp4.read_bytes(), b"real_user_video_do_not_overwrite")
            finally:
                store.close()

    def test_download_403_with_valid_session_refreshes_url_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                server.add_media(
                    unique_id="cdn403",
                    album_id="default-album-1",
                    file_name="CDN.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=_make_jpeg_bytes(b"cdn_ok"),
                )
                orig_dl = server.stream_download
                dl_calls = {"n": 0}

                def flaky_cdn(url, headers, part_path, **kw):
                    dl_calls["n"] += 1
                    if dl_calls["n"] == 1:
                        raise urllib.error.HTTPError(url, 403, "signed url expired", {}, None)
                    return orig_dl(url, headers, part_path, **kw)

                server.stream_download = flaky_cdn  # type: ignore[method-assign]
                rep = SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()
                self.assertEqual((rep.downloaded, rep.failed), (1, 0))
                self.assertEqual(dl_calls["n"], 2)
            finally:
                store.close()

    def test_cloud_delete_failed_converges_and_caps_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                old_ms = int((time.time() - 60 * 86400) * 1000)
                for uid in ("u_recycled", "u_purged", "u_stuck"):
                    server.add_media(
                        unique_id=uid,
                        album_id="default-album-1",
                        file_name=f"{uid}.jpg",
                        file_type="1",
                        create_time_ms=old_ms,
                        content=_make_jpeg_bytes(uid.encode()),
                    )
                SyncEngine(_cfg(photos_dir, cfg_dir), client, store).run_once()
                store.set_cloud_state(["u_recycled", "u_purged", "u_stuck"], CLOUD_DELETE_FAILED)

                # u_recycled was moved to Recycle Bin manually; u_purged vanished completely from cloud.
                cam_items = server.items_by_album["default-album-1"]
                recycled_entry = next(x for x in cam_items if x["uniqueId"] == "u_recycled")
                server.items_by_album["default-album-1"] = [
                    x for x in cam_items if x["uniqueId"] == "u_stuck"
                ]
                server.items_by_album["default-album-3"].append(recycled_entry)

                # Full scan converges u_recycled -> deleted, u_purged -> missing.
                rep = SyncEngine(_cfg(photos_dir, cfg_dir), client, store).run_once(force_full_scan=True)
                self.assertEqual(store.get_record("u_recycled")["cloud_state"], CLOUD_DELETED)
                self.assertEqual(store.get_record("u_purged")["cloud_state"], CLOUD_MISSING)
                self.assertEqual(rep.cloud_missing, 1)

                # Permanently rejected u_stuck stops retrying after MAX_CLOUD_DELETE_RETRIES
                orig_req = server.request
                delete_attempts = {"n": 0}

                def reject_del(method, url, headers, body=None, **kw):
                    if url.endswith("/album/deleteAlbumFile"):
                        delete_attempts["n"] += 1
                        return HttpResponse(200, {}, [], json.dumps({"code": "500"}).encode())
                    return orig_req(method, url, headers, body, **kw)

                server.request = reject_del  # type: ignore[method-assign]
                move_cfg = _cfg(photos_dir, cfg_dir, albums=["相机"], keep_cloud_recent_days=30)
                for _ in range(MAX_CLOUD_DELETE_RETRIES + 3):
                    SyncEngine(move_cfg, client, store).run_once()
                self.assertEqual(delete_attempts["n"], MAX_CLOUD_DELETE_RETRIES)
            finally:
                store.close()


class Round2SecurityTests(unittest.TestCase):
    def test_quarantine_sets_0600_and_secret_atomic_write_uses_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            secret = cfg / "secret.json"
            write_secret_file_atomic(secret, '{"a": 1}\n', mode=0o600)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

            bad_curl = cfg / "curl.txt"
            bad_curl.write_text("curl 'https://cloud.huawei.com' -H 'x: y'", encoding="utf-8")
            os.chmod(bad_curl, 0o644)
            mgr = AuthManager(cfg)
            with self.assertRaises(AuthExpiredError):
                mgr.ensure_session()
            invalid = cfg / "curl.txt.invalid"
            self.assertTrue(invalid.exists())
            self.assertEqual(stat.S_IMODE(invalid.stat().st_mode), 0o600)

    def test_https_only_cookie_host_and_cross_origin_header_strip(self) -> None:
        self.assertTrue(_is_cloud_huawei_host("https://cloud.huawei.com/file"))
        self.assertTrue(_is_cloud_huawei_host("https://sub.cloud.huawei.com/file"))
        self.assertFalse(_is_cloud_huawei_host("http://cloud.huawei.com/file"))
        self.assertFalse(_is_cloud_huawei_host("https://evilcloud.huawei.com/file"))

        handler = _SafeCrossOriginRedirectHandler()
        orig = urllib.request.Request(
            "https://cloud.huawei.com/download",
            headers={
                "Cookie": "shareToken=abc",
                "CSRFToken": "abc",
                "Authorization": "Bearer secret",
                "userid": "123",
                "User-Agent": "UA",
            },
        )
        new_req = handler.redirect_request(orig, None, 302, "Found", {}, "https://cdn.example.com/obj")
        assert new_req is not None
        lower_keys = {k.lower() for k in new_req.headers} | {k.lower() for k in new_req.unredirected_hdrs}
        for sensitive in ("cookie", "csrftoken", "authorization", "userid"):
            self.assertNotIn(sensitive, lower_keys)

    def test_cas_url_validation_and_webhook_scheme_and_env_bool_strict(self) -> None:
        self.assertEqual(
            _validate_huawei_https_url("https://id1.cloud.huawei.com/CAS/x", label="test"),
            "https://id1.cloud.huawei.com/CAS/x",
        )
        for bad_url in (
            "http://id1.cloud.huawei.com/CAS/x",
            "https://evil.example.com/CAS/x",
            "https://huawei.com.evil.com/x",
        ):
            with self.assertRaises(AuthExpiredError):
                _validate_huawei_https_url(bad_url, label="test")

        with self.assertRaises(ConfigError):
            SyncConfig(webhook_url="file:///etc/passwd").validate()
        with self.assertRaises(ValueError):
            Notifier._default_http_post("file:///etc/passwd", b"{}", {})

        with patch.dict(os.environ, {"HWCLOUD_DRY_RUN": "ture"}):
            with self.assertRaises(ValueError):
                env_bool("HWCLOUD_DRY_RUN", False)
            self.assertEqual(cli_main([]), 2)


class Round2ReliabilityAndEdgeCaseTests(unittest.TestCase):
    def test_sync_lock_blocks_concurrent_process_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                engine = SyncEngine(_cfg(photos_dir, cfg_dir), client, store, sync_lock_timeout_sec=0.05)
                with exclusive_file_lock(cfg_dir / "sync.lock", timeout_sec=1.0):
                    with self.assertRaises(HwCloudError):
                        engine.run_once()
            finally:
                store.close()

    def test_enrich_metadata_fallback_propagates_auth_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, server, client, store = _setup(tmp)
            try:
                stub = MediaStub(
                    unique_id="no_name_1",
                    album_id="default-album-1",
                    album_name="相机",
                    file_type="1",
                    create_time_ms=0,
                )
                orig = server.request

                def fail_lcd(method, url, headers, body=None, **kw):
                    if url.endswith("/album/getThumbLcdUrl"):
                        return HttpResponse(401, {}, [], b"unauthorized")
                    return orig(method, url, headers, body, **kw)

                server.request = fail_lcd  # type: ignore[method-assign]
                with self.assertRaises(AuthExpiredError):
                    client.enrich_metadata([stub])
            finally:
                store.close()

    def test_premature_eof_preserves_part_and_resumes_and_stream_honors_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                full_data = _make_jpeg_bytes(b"0123456789" * 10)
                server.add_media(
                    unique_id="resume_uid",
                    album_id="default-album-1",
                    file_name="BIG.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 12),
                    content=full_data,
                )
                seen_resume_offsets: list[int] = []

                def partial_then_resume(url, headers, part_path, *, resume_from=0, **kw):
                    seen_resume_offsets.append(resume_from)
                    part_path.parent.mkdir(parents=True, exist_ok=True)
                    if resume_from == 0:
                        chunk = full_data[:20]
                        part_path.write_bytes(chunk)
                        return DownloadResult(
                            bytes_written=len(chunk),
                            sha256_hex=hashlib.sha256(chunk).hexdigest(),
                            md5_hex=hashlib.md5(chunk).hexdigest(),
                        )
                    # Resume from offset 20
                    existing = part_path.read_bytes()
                    combined = existing + full_data[len(existing) :]
                    part_path.write_bytes(combined)
                    return DownloadResult(
                        bytes_written=len(combined),
                        sha256_hex=hashlib.sha256(combined).hexdigest(),
                        md5_hex=hashlib.md5(combined).hexdigest(),
                    )

                server.stream_download = partial_then_resume  # type: ignore[method-assign]
                rep = SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()
                self.assertEqual((rep.downloaded, rep.failed), (1, 0))
                self.assertEqual(seen_resume_offsets, [0, 20])
            finally:
                store.close()

            # Verify UrllibTransport.stream_download checks should_stop mid-stream
            transport = UrllibTransport()

            class FakeResp:
                status = 200
                headers = {}

                def __init__(self) -> None:
                    self.reads = 0

                def read(self, n: int) -> bytes:
                    self.reads += 1
                    return b"a" * 1024 if self.reads <= 5 else b""

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            fake_resp = FakeResp()
            part = Path(tmp) / "stop.hwpart"
            with patch.object(transport._redirect_opener, "open", return_value=fake_resp):
                with self.assertRaises(SyncCancelledError):
                    transport.stream_download(
                        "https://cdn.example.com/file",
                        {},
                        part,
                        should_stop=lambda: fake_resp.reads >= 2,
                    )
            self.assertEqual(part.stat().st_size, 2048)

    def test_edited_item_cleans_orphan_preserves_extras_and_does_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                v1 = _make_jpeg_bytes(b"version_1")
                server.add_media(
                    unique_id="edit_uid",
                    album_id="default-album-1",
                    file_name="E.jpg",
                    file_type="1",
                    create_time_ms=_ts_ms(2026, 11, 10),
                    content=v1,
                )
                cfg = _cfg(photos_dir, cfg_dir, albums=["相机"], xmp_sidecar=True)
                SyncEngine(cfg, client, store).run_once()
                old_file = photos_dir / "2026/11/10/E.jpg"
                old_xmp = photos_dir / "2026/11/10/E.jpg.xmp"
                self.assertTrue(old_file.is_file())
                self.assertTrue(old_xmp.is_file())

                # Edit item AND change its capture date so target folder moves to 2026/11/15
                v2 = _make_jpeg_bytes(b"version_2_longer_edited")
                new_ts = _ts_ms(2026, 11, 15)
                server.blobs["edit_uid"] = v2
                server._meta_map["edit_uid"]["fileSize"] = len(v2)
                server._meta_map["edit_uid"]["sha256"] = hashlib.sha256(v2).hexdigest()
                server._meta_map["edit_uid"]["createTime"] = new_ts
                server.items_by_album["default-album-1"][0]["createTime"] = new_ts

                rep = SyncEngine(cfg, client, store).run_once(force_full_scan=True)
                self.assertEqual(rep.redownloaded_changed, 1)
                self.assertEqual(rep.skipped_existing, 0)
                self.assertFalse(old_file.exists())
                self.assertFalse(old_xmp.exists())
                self.assertTrue((photos_dir / "2026/11/15/E.jpg").is_file())

                # Re-upserting with extra_paths=None must preserve existing extra_paths
                rec_before = store.get_record("edit_uid")
                assert rec_before is not None
                self.assertTrue(StateStore.extra_paths_of(rec_before))
                store.upsert_downloaded(
                    client.enrich_metadata([MediaStub("edit_uid", "default-album-1", "相机", "1", new_ts)])[0],
                    local_rel_path=rec_before["local_rel_path"],
                    local_sha256=rec_before["local_sha256"],
                    size_variant="original",
                )
                rec_after = store.get_record("edit_uid")
                assert rec_after is not None
                self.assertEqual(
                    StateStore.extra_paths_of(rec_after),
                    StateStore.extra_paths_of(rec_before),
                )
            finally:
                store.close()

    def test_daemon_execute_sync_tries_silent_relogin_before_alerting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, mgr, server, client, store = _setup(tmp)
            try:
                alerts: list[str] = []
                notifier = Notifier(
                    webhook_url="https://example.com/hook",
                    sender=lambda u, b, h: alerts.append(b.decode()),
                )

                class SilentAuth:
                    can_attempt_login = True
                    has_credentials = True
                    calls = 0

                    def login(self, *, interactive=False):
                        SilentAuth.calls += 1
                        server.auth_valid = True
                        return mgr.ensure_session()

                class ExpireEngine:
                    def run_once(self, *, force_full_scan=False):
                        raise AuthExpiredError("expired mid-sync")

                    def request_stop(self):
                        pass

                daemon = SyncDaemon(
                    _cfg(photos_dir, cfg_dir, watch_interval=60),
                    mgr,
                    client,
                    store,
                    notifier,
                    authenticator=SilentAuth(),  # type: ignore[arg-type]
                    engine_factory=ExpireEngine,  # type: ignore[arg-type]
                )
                daemon._ever_authenticated = True
                asyncio.run(daemon.execute_sync_once())
                self.assertEqual(SilentAuth.calls, 1)
                self.assertEqual(alerts, [])
                self.assertTrue(daemon._auth_valid)
                self.assertTrue(daemon._immediate_sync_event.is_set())
            finally:
                store.close()

    def test_due_failures_retry_despite_until_found_early_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                # 4 items; item #3 (oldest) fails on first run
                for i in range(4):
                    server.add_media(
                        unique_id=f"item_{i}",
                        album_id="default-album-1",
                        file_name=f"IMG_{i}.jpg",
                        file_type="1",
                        create_time_ms=_ts_ms(2026, 11, 15 - i),
                        content=_make_jpeg_bytes(f"p{i}".encode()),
                    )
                orig_dl = server.stream_download
                fail_once = {"fail": True}

                def flaky(url, headers, part_path, **kw):
                    if url.endswith("/item_3") and fail_once["fail"]:
                        raise OSError("transient")
                    return orig_dl(url, headers, part_path, **kw)

                server.stream_download = flaky  # type: ignore[method-assign]
                rep1 = SyncEngine(_cfg(photos_dir, cfg_dir, albums=["相机"]), client, store).run_once()
                self.assertEqual((rep1.downloaded, rep1.failed), (3, 1))

                # Fast-forward backoff window and run incremental with until_found=2
                store._conn.execute("UPDATE download_failures SET next_retry_at = 0")
                fail_once["fail"] = False
                rep2 = SyncEngine(
                    _cfg(photos_dir, cfg_dir, albums=["相机"], until_found=2),
                    client,
                    store,
                ).run_once()
                self.assertEqual((rep2.downloaded, rep2.failed), (1, 0))
                self.assertIsNotNone(store.get_record("item_3"))
            finally:
                store.close()

    def test_curl_hot_reload_accepts_older_mtime_and_pagination_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            mgr = AuthManager(cfg)
            curl_path = cfg / "curl.txt"
            curl_path.write_text(
                "curl 'https://cloud.huawei.com/heartbeatCheck' "
                "-b 'shareToken=tok1' -H 'userid: 111'",
                encoding="utf-8",
            )
            self.assertTrue(mgr.load_or_reload())
            self.assertEqual(mgr.ensure_session().user_id, "111")

            # Drop a new curl.txt with an OLDER mtime (like `cp -p`): must still be imported!
            curl_path.write_text(
                "curl 'https://cloud.huawei.com/heartbeatCheck' "
                "-b 'shareToken=tok2' -H 'userid: 222'",
                encoding="utf-8",
            )
            old_t = time.time() - 3600
            os.utime(curl_path, (old_t, old_t))
            self.assertTrue(mgr.load_or_reload())
            self.assertEqual(mgr.ensure_session().user_id, "222")

            # Pagination: hasMore="false" must stop, and duplicate pages must break out
            _, _, _, server, client, store = _setup(tmp)
            try:
                calls = {"n": 0}

                def bad_pages(method, url, headers, body=None, **kw):
                    calls["n"] += 1
                    return HttpResponse(
                        200,
                        {},
                        [],
                        json.dumps(
                            {
                                "resultCode": "0",
                                "hasMore": "false" if calls["n"] == 1 else "true",
                                "fileList": [{"uniqueId": "dup_1", "albumId": "default-album-1", "fileType": "1", "createTime": 1}],
                            }
                        ).encode(),
                    )

                server.request = bad_pages  # type: ignore[method-assign]
                pages = list(client.iter_simple_files("default-album-1"))
                self.assertEqual(len(pages), 1)
                self.assertEqual(calls["n"], 1)

                # Even with hasMore="true", repeating identical page stops after 2nd request
                calls["n"] = 1
                pages2 = list(client.iter_simple_files("default-album-1"))
                self.assertEqual(len(pages2), 1)
                self.assertEqual(calls["n"], 3)

                # Full page with hasMore=False must keep paginating until short/empty page
                full_calls = {"n": 0}

                def full_pages_false_has_more(method, url, headers, body=None, **kw):
                    full_calls["n"] += 1
                    off = json.loads(body.decode("utf-8")).get("currentNum", 0) if body else 0
                    if off == 0:
                        fl = [
                            {"uniqueId": "p1_a", "albumId": "default-album-1", "fileType": "1", "createTime": 3},
                            {"uniqueId": "p1_b", "albumId": "default-album-1", "fileType": "1", "createTime": 2},
                        ]
                    elif off == 2:
                        fl = [{"uniqueId": "p2_a", "albumId": "default-album-1", "fileType": "1", "createTime": 1}]
                    else:
                        fl = []
                    return HttpResponse(
                        200,
                        {},
                        [],
                        json.dumps({"resultCode": "0", "hasMore": False, "fileList": fl}).encode(),
                    )

                server.request = full_pages_false_has_more  # type: ignore[method-assign]
                pages3 = list(client.iter_simple_files("default-album-1", page_size=2))
                self.assertEqual([s.unique_id for p in pages3 for s in p], ["p1_a", "p1_b", "p2_a"])
                self.assertEqual(full_calls["n"], 2)
            finally:
                store.close()

    def test_pending_2fa_prefers_sent_channel_and_cron_dow_dst_and_long_cjk_name(self) -> None:
        ch = _pick_pending_2fa_channel(
            [
                {"accountType": 2, "name": "138****0000", "sent": 0},
                {"accountType": -1, "name": "Pura 70", "sent": 1},
            ]
        )
        self.assertEqual(ch["accountType"], -1)

        # DOW 1/2 must be Mon/Wed/Fri = {1, 3, 5}, NOT include Sunday (0)
        sched = CronSchedule.parse("0 0 * * 1/2")
        self.assertEqual(sched.days_of_week, frozenset({1, 3, 5}))
        sched_range = CronSchedule.parse("0 0 * * 5-7")
        self.assertEqual(sched_range.days_of_week, frozenset({0, 5, 6}))

        # DST spring-forward in America/New_York: 2026-03-08 02:30 does NOT exist
        tz_ny = ZoneInfo("America/New_York")
        dst_sched = CronSchedule.parse("30 2 * * *")
        nxt = dst_sched.next_after(datetime(2026, 3, 8, 1, 0, tzinfo=tz_ny), "America/New_York")
        self.assertEqual((nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute), (2026, 3, 9, 2, 30))

        # Long CJK filename truncated cleanly to <= 200 UTF-8 bytes preserving extension
        long_name = "旅游照片" * 80 + ".heic"
        safe = sanitize_component(long_name)
        self.assertLessEqual(len(safe.encode("utf-8")), 200)
        self.assertTrue(safe.endswith(".heic"))

    def test_exif_cleanup_refuses_truncated_local_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir, photos_dir, _, server, client, store = _setup(tmp)
            try:
                old_ms = int((time.time() - 60 * 86400) * 1000)
                server.add_media(
                    unique_id="exif_pic",
                    album_id="default-album-1",
                    file_name="EX.jpg",
                    file_type="1",
                    create_time_ms=old_ms,
                    content=_make_jpeg_bytes(b"exif_original"),
                )
                cfg = _cfg(photos_dir, cfg_dir, albums=["相机"], set_exif_datetime=True)
                SyncEngine(cfg, client, store).run_once()
                rec = store.get_record("exif_pic")
                assert rec is not None
                local = photos_dir / rec["local_rel_path"]
                # Corrupt/truncate local JPEG to 0 bytes -> move mode must NOT delete cloud original
                local.write_bytes(b"")
                move_cfg = _cfg(
                    photos_dir,
                    cfg_dir,
                    albums=["相机"],
                    set_exif_datetime=True,
                    keep_cloud_recent_days=30,
                )
                rep = SyncEngine(move_cfg, client, store).run_once()
                self.assertEqual(rep.deleted_cloud, 0)
                self.assertEqual(store.get_record("exif_pic")["cloud_state"], "present")
            finally:
                store.close()


class Round2CliAndDockerTests(unittest.TestCase):
    def test_invalid_timezone_with_created_after_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli_main(
                ["--config-dir", tmp, "--tz", "Mars/Olympus", "--created-after", "2026-01-01"]
            )
            self.assertEqual(rc, 2)

    def test_auth_only_allows_castgc_without_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            jar = LWPCookieJar(str(cfg_dir / "cas_cookies.lwp"))
            jar.set_cookie(
                Cookie(
                    0, "CASTGC", "tgt_valid", None, False, ".id1.cloud.huawei.com",
                    True, True, "/", True, True, int(time.time()) + 3600,
                    False, None, None, {},
                )
            )
            jar.save(ignore_discard=True)

            called = {"login": 0}

            def fake_login(self_auth, *, interactive=False):
                called["login"] += 1
                sess = AuthSession(
                    cookies={"shareToken": "tok_sso", "HWWAFSESID": "waf_sso"},
                    csrf_token="tok_sso",
                    user_id="80086000000000001",
                    device_id="a" * 64,
                )
                self_auth.auth_manager.save(sess)
                return sess

            with (
                patch.dict(os.environ, {"HUAWEI_USERNAME": "", "HUAWEI_PASSWORD": ""}),
                patch.object(HuaweiCasAuthenticator, "login", fake_login),
                patch.object(HuaweiGalleryClient, "heartbeat_check", return_value=True),
            ):
                rc = cli_main(["--config-dir", str(cfg_dir), "--auth-only"])
            self.assertEqual(rc, 0)
            self.assertEqual(called["login"], 1)

    def test_healthcheck_monitors_existing_health_json_even_without_env_daemon_vars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            # No env cron/watch and no health.json -> one-shot OK
            with patch.dict(os.environ, {"CRON_SCHEDULE": "", "HWCLOUD_CRON": "", "HWCLOUD_WATCH_INTERVAL": ""}):
                self.assertEqual(cli_main(["--config-dir", str(cfg_dir), "--healthcheck"]), 0)
                # Once a CLI-started daemon creates health.json, healthcheck MUST enforce it!
                (cfg_dir / "health.json").write_text(
                    json.dumps({"mode": "daemon", "ok": True, "auth_valid": False, "updated_at": time.time()}),
                    encoding="utf-8",
                )
                self.assertEqual(cli_main(["--config-dir", str(cfg_dir), "--healthcheck"]), 1)

    def test_dockerfile_and_compose_hardening(self) -> None:
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("0777", dockerfile)
        self.assertNotIn("CRON_SCHEDULE:", compose)


if __name__ == "__main__":
    unittest.main()
