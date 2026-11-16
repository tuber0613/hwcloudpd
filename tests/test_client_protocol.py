"""Protocol tests for HuaweiGalleryClient request headers, V3 mapping, fallback, and Range resume."""

from __future__ import annotations

import gzip
import hashlib
from http.client import HTTPMessage
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest import mock
import urllib.error

from hwcloudpd.auth import AuthManager
from hwcloudpd.client import DownloadResult, HttpResponse, HuaweiGalleryClient, UrllibTransport
from hwcloudpd.models import AuthExpiredError, AuthSession, MediaItem, MediaStub


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.download_calls: list[dict] = []
        self.request_handler = None
        self.download_handler = None

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        *,
        follow_redirects: bool = False,
        timeout: float = 30.0,
    ) -> HttpResponse:
        parsed_body = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": parsed_body,
            }
        )
        assert self.request_handler is not None
        return self.request_handler(method, url, headers, parsed_body)

    def stream_download(
        self,
        url: str,
        headers: dict[str, str],
        part_path: Path,
        *,
        resume_from: int = 0,
        timeout: float = 120.0,
    ) -> DownloadResult:
        self.download_calls.append(
            {"url": url, "headers": dict(headers), "part_path": part_path, "resume_from": resume_from}
        )
        assert self.download_handler is not None
        return self.download_handler(url, headers, part_path, resume_from)


def _make_auth_manager(tmp_dir: str) -> AuthManager:
    mgr = AuthManager(Path(tmp_dir))
    mgr.save(
        AuthSession(
            cookies={"shareToken": "tok_init", "HWWAFSESID": "waf_init"},
            csrf_token="tok_init",
            user_id="80086000000000001",
            device_id="a" * 64,
            user_agent="TestUA/1.0",
        )
    )
    return mgr


class ClientProtocolTests(unittest.TestCase):
    def test_headers_trace_id_and_csrf_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_auth_manager(tmp)
            tr = RecordingTransport()

            def handler(method, url, headers, body):
                self.assertEqual(headers["CSRFToken"], "tok_init")
                self.assertEqual(headers["userid"], "80086000000000001")
                self.assertEqual(headers["x-hw-device-id"], "a" * 64)
                self.assertRegex(headers["x-hw-trace-id"], r"^07100_02_\d+_[1-9]{8}$")
                return HttpResponse(
                    status=200,
                    headers={"Content-Type": "application/json", "csrftoken": "tok_rotated"},
                    set_cookies=["shareToken=tok_rotated; Path=/; Secure"],
                    body=json.dumps({"code": "0", "info": "user has logged"}).encode("utf-8"),
                )

            tr.request_handler = handler
            client = HuaweiGalleryClient(mgr, tr, retry_base_delay=0.01)
            self.assertTrue(client.heartbeat_check())
            self.assertEqual(mgr.ensure_session().csrf_token, "tok_rotated")
            self.assertEqual(mgr.ensure_session().cookies["shareToken"], "tok_rotated")

    def test_auth_expired_on_login_redirect_and_nonzero_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_auth_manager(tmp)
            tr = RecordingTransport()
            tr.request_handler = lambda *_: HttpResponse(
                status=302,
                headers={"Location": "https://id1.cloud.huawei.com/CAS/portal/login.html"},
                set_cookies=[],
                body=b"",
            )
            client = HuaweiGalleryClient(mgr, tr, retry_base_delay=0.01)
            with self.assertRaises(AuthExpiredError):
                client.heartbeat_check()

            tr.request_handler = lambda *_: HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": "401", "info": "not logged"}).encode("utf-8"),
            )
            with self.assertRaises(AuthExpiredError):
                client.heartbeat_check()

            # HTTP 402 from Gallery API (loginSecLevel=0) must raise AuthExpiredError
            tr.request_handler = lambda *_: HttpResponse(
                status=402,
                headers={},
                set_cookies=[],
                body=b"",
            )
            with self.assertRaises(AuthExpiredError):
                client.get_gallery_stats()

            # heartbeat_check with loginSecLevel="0" must also signal locked session
            mgr.save(
                AuthSession(
                    cookies={"shareToken": "tok_init", "loginSecLevel": "0"},
                    csrf_token="tok_init",
                    user_id="80086000000000001",
                    device_id="a" * 64,
                )
            )
            tr.request_handler = lambda *_: HttpResponse(
                status=200,
                headers={},
                set_cookies=[],
                body=json.dumps({"code": "0", "info": "user has logged"}).encode("utf-8"),
            )
            with self.assertRaises(AuthExpiredError):
                client.heartbeat_check()

    def test_virtual_album_v3_mapping_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_auth_manager(tmp)
            tr = RecordingTransport()
            seen_payloads: list[dict] = []

            def handler(method, url, headers, body):
                if url.endswith("/album/getSimpleFile"):
                    seen_payloads.append(body)
                    if body["currentNum"] == 0 and body["albumId"] == "default-album-1":
                        return HttpResponse(
                            status=200,
                            headers={},
                            set_cookies=[],
                            body=json.dumps(
                                {
                                    "resultCode": "0",
                                    "hasMore": True,
                                    "fileList": [
                                        {
                                            "albumId": "default-album-1",
                                            "uniqueId": "uid_1",
                                            "fileType": "1",
                                            "createTime": 1700000001000,
                                        }
                                    ],
                                }
                            ).encode("utf-8"),
                        )
                    return HttpResponse(
                        status=200,
                        headers={},
                        set_cookies=[],
                        body=json.dumps(
                            {
                                "resultCode": "0",
                                "hasMore": False,
                                "fileList": [
                                    {
                                        "albumId": "default-album-1",
                                        "uniqueId": "uid_2",
                                        "fileType": "4",
                                        "createTime": 1700000000000,
                                    }
                                ],
                            }
                        ).encode("utf-8"),
                    )
                raise AssertionError(f"Unexpected URL {url}")

            tr.request_handler = handler
            client = HuaweiGalleryClient(mgr, tr, retry_base_delay=0.01)

            pages = list(client.iter_simple_files("default-album-1", page_size=1))
            self.assertEqual([s.unique_id for p in pages for s in p], ["uid_1", "uid_2"])
            self.assertEqual(seen_payloads[0]["albumId"], "default-album-1")
            self.assertIsNone(seen_payloads[0]["type"])
            self.assertEqual(seen_payloads[1]["currentNum"], 1)

            seen_payloads.clear()
            list(client.iter_simple_files("default-album-all-photos", page_size=10))
            self.assertIsNone(seen_payloads[0]["albumId"])
            self.assertEqual(seen_payloads[0]["type"], 1)

            seen_payloads.clear()
            list(client.iter_simple_files("default-album-3", page_size=10))
            self.assertIsNone(seen_payloads[0]["albumId"])
            self.assertEqual(seen_payloads[0]["type"], 4)

    def test_metadata_enrichment_fallback_and_cookie_strip_on_cdn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_auth_manager(tmp)
            tr = RecordingTransport()

            def handler(method, url, headers, body):
                if url.endswith("/album/queryCloudFileName"):
                    self.assertNotIn("albumVersion", body)
                    return HttpResponse(
                        status=200,
                        headers={},
                        set_cookies=[],
                        body=json.dumps(
                            {
                                "code": 0,
                                "fileList": [
                                    {
                                        "uniqueId": "uid_has_name",
                                        "fileName": "IMG_100.jpg",
                                        "fileSize": 10,
                                        "sha256": "a" * 64,
                                        "hash": "b" * 64,
                                    },
                                    {
                                        "uniqueId": "uid_hash64_only",
                                        "fileName": "IMG_101.heic",
                                        "fileType": "9",
                                        "fileSize": 20,
                                        "hash": "c" * 64,
                                    },
                                    {
                                        "uniqueId": "uid_md5_hash",
                                        "fileName": "IMG_102.jpg",
                                        "fileSize": 30,
                                        "hash": "d" * 32,
                                    },
                                ],
                            }
                        ).encode("utf-8"),
                    )
                if url.endswith("/album/getThumbLcdUrl"):
                    return HttpResponse(
                        status=200,
                        headers={},
                        set_cookies=[],
                        body=json.dumps(
                            {
                                "code": 0,
                                "successList": [
                                    {
                                        "albumId": "default-album-2",
                                        "fileId": "",
                                        "fileName": "screenshot_fallback.jpg",
                                        "fileUrl": "https://cloud.huawei.com:443/proxy/v1/download/DB3_lgSAAA4Auid_missing_name.jpg",
                                    }
                                ],
                            }
                        ).encode("utf-8"),
                    )
                if url.endswith("/proxyserver/driveFileProxy/preProcess"):
                    return HttpResponse(
                        status=200,
                        headers={},
                        set_cookies=[],
                        body=json.dumps({"code": "0", "info": "success"}).encode("utf-8"),
                    )
                if url.endswith("/album/getSingleUrl"):
                    self.assertEqual(body["type"], "0")
                    req_uid = body["fileList"][0]["uniqueId"]
                    if req_uid == "uid_missing_name":
                        self.assertNotIn("albumVersion", body)
                        self.assertNotIn("ownerId", body)
                    return HttpResponse(
                        status=200,
                        headers={},
                        set_cookies=[],
                        body=json.dumps(
                            {
                                "code": 0,
                                "urlList": [
                                    {
                                        "uniqueId": req_uid,
                                        "url": "https://download-proxy-p01-drcn.platform.dbankcloud.cn/filev2/IMG_100.jpg?key=sig",
                                    }
                                ],
                            }
                        ).encode("utf-8"),
                    )
                raise AssertionError(f"Unexpected URL {url}")

            tr.request_handler = handler

            payload = b"0123456789"
            def dl_handler(url, headers, part_path, resume_from):
                part_path.parent.mkdir(parents=True, exist_ok=True)
                part_path.write_bytes(payload)
                return DownloadResult(
                    bytes_written=len(payload),
                    sha256_hex=hashlib.sha256(payload).hexdigest(),
                    md5_hex=hashlib.md5(payload).hexdigest(),
                )

            tr.download_handler = dl_handler
            client = HuaweiGalleryClient(mgr, tr, retry_base_delay=0.01)

            stubs = [
                MediaStub("uid_has_name", "default-album-1", "相机", "1", 1700000000000),
                MediaStub("uid_missing_name", "default-album-2", "截屏录屏", "1", 1700000001000),
                MediaStub("uid_hash64_only", "default-album-1", "相机", "9", 1700000002000),
                MediaStub("uid_md5_hash", "default-album-1", "相机", "1", 1700000003000),
            ]
            items = client.enrich_metadata(stubs)
            self.assertEqual(items[0].file_name, "IMG_100.jpg")
            self.assertEqual(items[0].checksum, "a" * 64)
            self.assertEqual(items[1].file_name, "screenshot_fallback.jpg")
            self.assertEqual(items[2].checksum, "")
            self.assertTrue(items[2].is_photo)
            self.assertEqual(items[3].checksum, "d" * 32)

            # Empty album_version / owner_id must be omitted from getSingleUrl
            client.get_download_url(items[1], "original")

            # album_version == "2.0" triggers preProcess before getSingleUrl
            items[0].album_version = "2.0"
            dl_url = client.get_download_url(items[0], "original")
            self.assertIn("dbankcloud.cn", dl_url)
            self.assertTrue(any(c["url"].endswith("/preProcess") for c in tr.calls))

            # CDN download must NOT include cloud.huawei.com cookies
            part = Path(tmp) / "IMG_100.jpg.hwpart"
            client.download_to_part(dl_url, part)
            self.assertNotIn("Cookie", tr.download_calls[-1]["headers"])

            # cloud.huawei.com proxy download MUST include cookies
            client.download_to_part("https://cloud.huawei.com/proxy/v1/download/abc", part)
            self.assertIn("Cookie", tr.download_calls[-1]["headers"])

    def test_retry_on_429_and_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_auth_manager(tmp)
            tr = RecordingTransport()
            attempts = {"n": 0}

            def handler(method, url, headers, body):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return HttpResponse(status=429, headers={}, set_cookies=[], body=b"")
                if attempts["n"] == 2:
                    return HttpResponse(status=503, headers={}, set_cookies=[], body=b"")
                return HttpResponse(
                    status=200,
                    headers={},
                    set_cookies=[],
                    body=json.dumps({"code": 0, "photoNum": 5, "videoNum": 1, "totalNum": 6}).encode("utf-8"),
                )

            tr.request_handler = handler
            client = HuaweiGalleryClient(mgr, tr, retry_base_delay=0.005)
            stats = client.get_gallery_stats()
            self.assertEqual(stats["totalNum"], 6)
            self.assertEqual(attempts["n"], 3)

    def test_urllib_transport_gzip_no_redirect_and_range_resume(self) -> None:
        full_blob = b"ABCDEFGHIJ0123456789abcdefghij"

        class FakeHTTPResponse:
            def __init__(self, status: int, header_pairs: list[tuple[str, str]], data: bytes) -> None:
                self.status = status
                self.headers = HTTPMessage()
                for k, v in header_pairs:
                    self.headers.add_header(k, v)
                self._bio = io.BytesIO(data)

            def read(self, amt: int = -1) -> bytes:
                return self._bio.read(amt)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        transport = UrllibTransport(ssl_context=ssl.create_default_context())

        def fake_no_redir_open(req, timeout=30.0):
            if req.full_url.endswith("/gzip"):
                return FakeHTTPResponse(
                    200,
                    [("Content-Encoding", "gzip"), ("Set-Cookie", "shareToken=gz_tok; Path=/")],
                    gzip.compress(b'{"code":"0","info":"ok"}'),
                )
            hdrs = HTTPMessage()
            hdrs.add_header("Location", "https://id1.cloud.huawei.com/login")
            raise urllib.error.HTTPError(req.full_url, 302, "Found", hdrs, io.BytesIO(b""))

        def fake_redir_open(req, timeout=120.0):
            rng = req.get_header("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
                if start >= len(full_blob):
                    raise urllib.error.HTTPError(
                        req.full_url, 416, "Range Not Satisfiable", HTTPMessage(), io.BytesIO(b"")
                    )
                return FakeHTTPResponse(
                    206,
                    [("Content-Range", f"bytes {start}-{len(full_blob)-1}/{len(full_blob)}")],
                    full_blob[start:],
                )
            return FakeHTTPResponse(
                200,
                [("Content-Disposition", "attachment;filename*=UTF-8''live%20photo.jpg")],
                full_blob,
            )

        with (
            mock.patch.object(transport._no_redirect_opener, "open", side_effect=fake_no_redir_open),
            mock.patch.object(transport._redirect_opener, "open", side_effect=fake_redir_open),
        ):
            resp_gz = transport.request("GET", "https://cloud.huawei.com/gzip", {})
            self.assertEqual(resp_gz.status, 200)
            self.assertEqual(json.loads(resp_gz.body.decode("utf-8"))["info"], "ok")
            self.assertEqual(resp_gz.set_cookies, ["shareToken=gz_tok; Path=/"])

            resp_redir = transport.request("GET", "https://cloud.huawei.com/redir", {}, follow_redirects=False)
            self.assertEqual(resp_redir.status, 302)
            self.assertIn("id1.cloud.huawei.com", resp_redir.headers["Location"])

            with tempfile.TemporaryDirectory() as tmp:
                part = Path(tmp) / "blob.hwpart"
                part.write_bytes(full_blob[:10])
                res = transport.stream_download(
                    "https://download-proxy.dbankcloud.cn/blob", {}, part, resume_from=10
                )
                self.assertEqual(res.bytes_written, len(full_blob))
                self.assertEqual(res.sha256_hex, hashlib.sha256(full_blob).hexdigest())
                self.assertEqual(part.read_bytes(), full_blob)

                # Test 416 fallback discarding oversized .hwpart and retrying from byte 0
                part.write_bytes(full_blob + b"extra")
                res2 = transport.stream_download(
                    "https://download-proxy.dbankcloud.cn/blob",
                    {},
                    part,
                    resume_from=len(full_blob) + 5,
                )
                self.assertEqual(res2.bytes_written, len(full_blob))
                self.assertEqual(res2.content_disposition_filename, "live photo.jpg")
                self.assertEqual(part.read_bytes(), full_blob)


if __name__ == "__main__":
    unittest.main()
