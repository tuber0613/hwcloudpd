"""Tests for Copy-as-cURL parsing, AuthManager hot-reload, and CSRFToken rotation."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from hwcloudpd.auth import AuthManager, parse_curl_command
from hwcloudpd.models import AuthExpiredError

SAMPLE_CHROME_CURL = r"""curl 'https://cloud.huawei.com/album/getSimpleFile' \
  -H 'Accept: application/json, text/plain, */*' \
  -H $'CSRFToken: csrf_token_initial_123' \
  -H 'Content-Type: application/json;charset=UTF-8' \
  -b 'HWWAFSESID=waf_111; HW_id_hicloudportal_2_=portal_222; shareToken=csrf_token_initial_123; isLogin=1' \
  -H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0.0.0' \
  -H 'sec-ch-ua-platform: "macOS"' \
  -H 'userid: 80086000000000001' \
  -H 'x-hw-device-id: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' \
  --data-raw '{"albumId":"default-album-1","currentNum":0,"count":100}'"""

SAMPLE_COOKIE_HEADER_CURL = """curl "https://cloud.huawei.com/album/queryAlbumInfo" \\
  --header "Cookie: shareToken=tok_abc; HWWAFSESID=waf_xyz; CASTGC=tgt_secret_999" \\
  --header "userid: 900123456" \\
  --header "Accept-Language: zh-CN"
"""


class AuthCurlTests(unittest.TestCase):
    def test_parse_chrome_bash_curl_with_ansi_c_quotes(self) -> None:
        session = parse_curl_command(SAMPLE_CHROME_CURL)
        self.assertEqual(session.user_id, "80086000000000001")
        self.assertEqual(session.csrf_token, "csrf_token_initial_123")
        self.assertEqual(session.cookies["shareToken"], "csrf_token_initial_123")
        self.assertEqual(session.cookies["HWWAFSESID"], "waf_111")
        self.assertEqual(session.cookies["HW_id_hicloudportal_2_"], "portal_222")
        self.assertEqual(
            session.device_id,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        self.assertIn("Chrome/131.0.0.0", session.user_agent)
        self.assertEqual(session.extra_headers.get("sec-ch-ua-platform"), '"macOS"')

    def test_parse_cookie_header_and_generate_device_id(self) -> None:
        session = parse_curl_command(SAMPLE_COOKIE_HEADER_CURL)
        self.assertEqual(session.user_id, "900123456")
        self.assertEqual(session.csrf_token, "tok_abc")
        self.assertEqual(session.cookies["CASTGC"], "tgt_secret_999")
        self.assertEqual(len(session.device_id), 64)
        self.assertEqual(session.extra_headers.get("accept-language"), "zh-CN")

    def test_missing_required_fields_raise_clear_errors(self) -> None:
        with self.assertRaises(ValueError):
            parse_curl_command("curl 'https://cloud.huawei.com/home'")

        with self.assertRaises(ValueError):
            parse_curl_command(
                "curl 'https://cloud.huawei.com/home' -b 'foo=bar' -H 'userid: 123'"
            )

        with self.assertRaises(ValueError):
            parse_curl_command(
                "curl 'https://cloud.huawei.com/home' -b 'shareToken=abc'"
            )

    def test_auth_manager_auto_imports_curl_txt_and_hot_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            curl_path = cfg_dir / "curl.txt"
            curl_path.write_text(SAMPLE_CHROME_CURL, encoding="utf-8")

            mgr = AuthManager(cfg_dir)
            session = mgr.ensure_session()
            self.assertEqual(session.user_id, "80086000000000001")
            self.assertTrue(mgr.auth_file.exists())
            self.assertFalse(curl_path.exists())
            self.assertFalse((cfg_dir / "curl.txt.imported").exists())

            # Rotate CSRFToken and Set-Cookie from HTTP response
            changed = mgr.update_from_response(
                [
                    "HW_id_hicloudportal_2_=portal_rotated; Path=/; Secure; HttpOnly",
                    "HWWAFSESID=waf_222; Path=/",
                ],
                {"Content-Type": "application/json", "csrftoken": "csrf_rotated_456"},
            )
            self.assertTrue(changed)

            reloaded_mgr = AuthManager(cfg_dir)
            persisted = reloaded_mgr.ensure_session()
            self.assertEqual(persisted.csrf_token, "csrf_rotated_456")
            self.assertEqual(persisted.cookies["shareToken"], "csrf_rotated_456")
            self.assertEqual(persisted.cookies["HW_id_hicloudportal_2_"], "portal_rotated")
            self.assertEqual(persisted.cookies["HWWAFSESID"], "waf_222")

            # Dropping a brand-new curl.txt triggers hot reload with bumped mtime
            time.sleep(0.02)
            curl_path.write_text(SAMPLE_COOKIE_HEADER_CURL, encoding="utf-8")
            future_t = time.time() + 5
            os.utime(curl_path, (future_t, future_t))
            self.assertTrue(reloaded_mgr.load_or_reload())
            self.assertEqual(reloaded_mgr.ensure_session().user_id, "900123456")

    def test_empty_config_dir_raises_auth_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AuthManager(Path(tmp))
            with self.assertRaises(AuthExpiredError):
                mgr.ensure_session()


if __name__ == "__main__":
    unittest.main()
