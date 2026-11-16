"""Unit tests for pure-HTTP HuaweiCasAuthenticator (in-memory transport)."""

from __future__ import annotations

from http.cookiejar import Cookie
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs

from hwcloudpd.auth import AuthManager
from hwcloudpd.cas_auth import HuaweiCasAuthenticator, normalize_phone_account
from hwcloudpd.models import (
    AuthSession,
    CaptchaRequiredError,
    InvalidCredentialsError,
    TwoFactorRequiredError,
)


class _FakeUrllibResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"{}",
    ) -> None:
        self.status = status
        self._url = url
        self.headers = headers or {}
        self._buf = io.BytesIO(body)

    def geturl(self) -> str:
        return self._url

    def read(self, amt: int | None = None) -> bytes:
        return self._buf.read(amt)

    def __enter__(self) -> _FakeUrllibResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeCasOpener:
    """In-memory state machine simulating id1.cloud.huawei.com CAS + cloud.huawei.com SSO."""

    def __init__(self, *, mode: str = "trusted") -> None:
        self.mode = mode
        self.calls: list[tuple[str, str, dict[str, list[str]]]] = []
        self.jar = None
        self.trusted_browser_updated = False
        self.sms_requested = False
        self.trust_oper_types: list[str] = []
        self.remote_login_called = False

    def _set_cookie(self, name: str, value: str, domain: str, *, discard: bool = True) -> None:
        assert self.jar is not None
        self.jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None if discard else 1893456000,
                discard=discard,
                comment=None,
                comment_url=None,
                rest={},
            )
        )

    def open(self, req, timeout: float = 30.0) -> _FakeUrllibResponse:
        url = req.full_url
        method = req.get_method()
        raw_body = req.data or b""
        form = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
        self.calls.append((method, url, form))

        if url == "https://cloud.huawei.com/":
            self._set_cookie("HWWAFSESID", "waf_cookie_1", "cloud.huawei.com")
            return _FakeUrllibResponse(url=url, body=b"<html></html>")

        if "cloudIframeLogin.html" in url:
            return _FakeUrllibResponse(url=url, body=b"<html>cas</html>")

        if "common/getBaseSwitchInfo" in url:
            return _FakeUrllibResponse(url=url, body=json.dumps({"isSuccess": "1"}).encode())

        if "login/getPageInfo" in url:
            if self.mode == "sso_redirect":
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps(
                        {
                            "isSuccess": "1",
                            "redirectUrl": "https://cloud.huawei.com/others/login.action?ticket=ST-SSO-1",
                        }
                    ).encode(),
                )
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {
                        "isSuccess": "1",
                        "pageName": "cloudIframeLogin",
                        "pageToken": "pt_123",
                        "pageTokenKey": "ptk_456",
                        "localInfo": {
                            "loginUrl": "https://cloud.huawei.com:443/others/login.action",
                            "service": "https://cloud.huawei.com:443/others/login.action",
                            "reqClientType": "1",
                            "loginChannel": "1000002",
                            "clientID": "4805300",
                            "lang": "zh-cn",
                            "scope": "https://www.huawei.com/auth/account/pwdlowlogin",
                            "lowLogin": "false",
                            "localHttpsAjaxPath": "/CAS/IDM_W",
                        },
                        "localStorageID": "lsid_999",
                    }
                ).encode(),
            )

        if "chkRisk" in url:
            if self.mode == "captcha":
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps({"isSuccess": "1", "isNeedImageCode": 1, "siteID": 1}).encode(),
                )
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {"isSuccess": "1", "siteID": 1, "isNeedImageCode": 0, "lowLogin": "false"}
                ).encode(),
            )

        if "remoteLogin?service=oauth" in url:
            return _FakeUrllibResponse(
                url="https://oauth-login1.cloud.huawei.com/oauth2/v3/loginCallback?ticket=1ST-OAUTH",
                body=b"<html>oauth callback</html>",
            )

        if "/oauth2/ajax/getLoginWay" in url:
            assert req.get_header("Interfaceversion") == "v3"
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {
                        "isSuccess": "true",
                        "loginInteractInfo": {
                            "cas": {
                                "casLoginRedirectUrl": "https://id1.cloud.huawei.com/CAS/remoteLogin?service=oauth"
                            }
                        },
                    }
                ).encode(),
            )

        if "/oauth2/ajax/login" in url:
            assert req.get_header("Interfaceversion") == "v3"
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {
                        "isSuccess": "true",
                        "code": "https://cloud.huawei.com:443/homeTransit?code=C1",
                    }
                ).encode(),
            )

        if "/homeTransit" in url:
            self._set_cookie("isLogin", "1", ".cloud.huawei.com", discard=True)
            self._set_cookie("CSRFToken", "share_tok_portal", ".cloud.huawei.com", discard=True)
            self._set_cookie("userId", "80086000000000001", ".cloud.huawei.com", discard=True)
            return _FakeUrllibResponse(url="https://cloud.huawei.com:443/home", body=b"<html>home</html>")

        if "remoteLogin" in url:
            self.remote_login_called = True
            if self.mode == "bad_pw":
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps(
                        {
                            "isSuccess": "0",
                            "errorCode": "70002057",
                            "errorDesc": "账号或密码错误",
                        }
                    ).encode(),
                )
            if self.mode in ("2fa_sms", "2fa_required"):
                code = (form.get("twoStepVerifyCode") or [""])[0]
                if not code:
                    desc = json.dumps(
                        {
                            "authCodeSentList": [
                                {"accountType": -1, "name": "HUAWEI Mate", "sent": 1},
                                {"accountType": 2, "name": "176****8703", "sent": 0},
                            ],
                            "isDoubleVerification": True,
                            "tipsSceneID": "1",
                        }
                    )
                    return _FakeUrllibResponse(
                        url=url,
                        body=json.dumps(
                            {"isSuccess": "0", "errorCode": "10012072", "errorDesc": desc}
                        ).encode(),
                    )
                if code != "654321":
                    return _FakeUrllibResponse(
                        url=url,
                        body=json.dumps(
                            {"isSuccess": "0", "errorCode": "10002057", "errorDesc": "验证码错误"}
                        ).encode(),
                    )
                self._set_cookie("CASTGC", "TGT-TRUSTED-1", ".id1.cloud.huawei.com", discard=True)
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps(
                        {
                            "isSuccess": "1",
                            "callbackURL": "https://cloud.huawei.com/others/login.action?ticket=ST-2FA-OK",
                        }
                    ).encode(),
                )

            # Default trusted silent password login
            self._set_cookie("CASTGC", "TGT-SILENT-1", ".id1.cloud.huawei.com", discard=True)
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {
                        "isSuccess": "1",
                        "needPopTrust": True,
                        "callbackURL": "https://cloud.huawei.com/others/login.action?ticket=ST-PWD-OK",
                    }
                ).encode(),
            )

        if "getSMSCodeV3" in url:
            self.sms_requested = True
            return _FakeUrllibResponse(url=url, body=json.dumps({"isSuccess": "1"}).encode())

        if "authIdentify.html" in url:
            return _FakeUrllibResponse(url=url, body=b"<html>authIdentify</html>")

        if "cloudIframeAuthIdentify/getPageInfo" in url:
            assert form.get("pageName") == ["cloudIframeAuthIdentify"]
            assert form.get("interfaceName") == ["cloudIframeAuthIdentify/getPageInfo"]
            assert "clientID" not in form

            if self.mode == "expired_low_tgt" and not self.remote_login_called:
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps(
                        {
                            "isSuccess": "1",
                            "pageName": "cloudIframeAuthIdentify",
                            "pageToken": "pt_expired",
                            "pageTokenKey": "ptk_expired",
                            "localInfo": {
                                "isLowTGTExpired": "True",
                                "remoteLogin": "https://id1.cloud.huawei.com/CAS/remoteLogin?reqClientType=1&loginChannel=1000002",
                                "localHttpsAjaxPath": "/CAS/IDM_W",
                            },
                        }
                    ).encode(),
                )

            if self.mode == "tier2_trusted" or (self.mode == "expired_low_tgt" and self.remote_login_called):
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps(
                        {
                            "isSuccess": "1",
                            "pageName": "cloudIframeAuthIdentify",
                            "localInfo": {
                                "redirectUrl": "https://cloud.huawei.com/others/login.action?ticket=ST-TIER2-AUTO"
                            },
                        }
                    ).encode(),
                )
            desc = json.dumps(
                {
                    "authCodeSentList": [
                        {"accountType": -1, "name": "HUAWEI Pura", "sent": 1},
                        {"accountType": 2, "name": "176****8703", "sent": 0},
                    ],
                    "isDoubleVerification": True,
                    "tipsSceneID": "22",
                }
            )
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {
                        "isSuccess": "1",
                        "pageName": "cloudIframeAuthIdentify",
                        "pageToken": "pt_ai",
                        "pageTokenKey": "ptk_ai",
                        "localInfo": {
                            "curAccountSiteID": 1,
                            "reqClientType": "1",
                            "loginChannel": "1000002",
                            "lang": "zh-cn",
                            "localHttpsAjaxPath": "/CAS/IDM_W",
                            "errorDesc": desc,
                        },
                        "localStorageID": "lsid_ai",
                    }
                ).encode(),
            )

        if "cloudAuthLogin" in url:
            assert form.get("pageToken") == ["pt_ai"]
            assert "userAccount" not in form
            code = (form.get("twoStepVerifyCode") or [""])[0]
            if code != "654321":
                return _FakeUrllibResponse(
                    url=url,
                    body=json.dumps(
                        {"isSuccess": "0", "errorCode": "10002057", "errorDesc": "验证码错误"}
                    ).encode(),
                )
            self._set_cookie("CASTGC", "TGT-HIGH-1", ".id1.cloud.huawei.com", discard=True)
            return _FakeUrllibResponse(
                url=url,
                body=json.dumps(
                    {
                        "isSuccess": "1",
                        "needPopTrust": True,
                        "callbackURL": "https://cloud.huawei.com/others/login.action?ticket=ST-HIGH-OK",
                    }
                ).encode(),
            )

        if "updateTrustBrowser" in url:
            self.trust_oper_types.append((form.get("operType") or [""])[0])
            if (form.get("trustBrowser") or [""])[0] == "1":
                self.trusted_browser_updated = True
                self._set_cookie("HWID_TRUST", "trust_tok_1", ".id1.cloud.huawei.com", discard=False)
            return _FakeUrllibResponse(url=url, body=json.dumps({"isSuccess": "1"}).encode())

        if "login.action" in url:
            if self.mode == "oauth_bridge":
                return _FakeUrllibResponse(
                    url="https://oauth-login.cloud.huawei.com/oauth2/v3/authorize?client_id=4805300",
                    body=b"<html>oauth spa shell</html>",
                )
            if (
                "ST-TIER2" in url
                or "ST-HIGH" in url
                or self.mode not in ("tier2_2fa", "tier2_trusted", "expired_low_tgt")
            ):
                self._set_cookie("loginSecLevel", "2", ".cloud.huawei.com", discard=True)
            else:
                self._set_cookie("loginSecLevel", "0", ".cloud.huawei.com", discard=True)
            self._set_cookie("shareToken", "share_tok_portal", ".cloud.huawei.com", discard=True)
            return _FakeUrllibResponse(
                url="https://cloud.huawei.com/home",
                headers={"Location": "https://cloud.huawei.com/home"},
                body=b"<html>home</html>",
            )

        if "/html/getHomeData" in url:
            return _FakeUrllibResponse(
                url=url,
                headers={"csrftoken": "share_tok_portal"},
                body=json.dumps(
                    {
                        "code": 0,
                        "isLogin": 1,
                        "userid": "80086000000000001",
                        "deviceIdForHeader": "d" * 64,
                        "unlockUrl": "https://id1.cloud.huawei.com/CAS/portal/authIdentify.html?loginUrl=https%3A%2F%2Fcloud.huawei.com%3A443%2Fothers%2Flogin.action&service=https%3A%2F%2Fcloud.huawei.com%3A443%2Fothers%2Flogin.action&lang=zh-cn&reqClientType=1&loginChannel=1000002&scenesType=0",
                    }
                ).encode(),
            )

        raise AssertionError(f"Unexpected CAS URL: {method} {url}")


class CasAuthTests(unittest.TestCase):
    def _make_auth(self, tmp: str, opener: _FakeCasOpener, **kwargs) -> tuple[AuthManager, HuaweiCasAuthenticator]:
        mgr = AuthManager(Path(tmp) / "config")
        auth = HuaweiCasAuthenticator(mgr, opener=opener, **kwargs)  # type: ignore[arg-type]
        opener.jar = auth._cookie_jar
        return mgr, auth

    def test_phone_normalization(self) -> None:
        self.assertEqual(normalize_phone_account(" +8613800138000 "), "008613800138000")
        self.assertEqual(normalize_phone_account("13800138000"), "13800138000")

    def test_sso_fast_path_via_get_page_info_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="sso_redirect")
            mgr, auth = self._make_auth(tmp, opener)
            session = auth.login(interactive=False)
            self.assertEqual(session.user_id, "80086000000000001")
            self.assertEqual(session.csrf_token, "share_tok_portal")
            self.assertNotIn("CASTGC", session.cookies)
            self.assertTrue(mgr.auth_file.exists())
            self.assertFalse(any("remoteLogin" in u for _, u, _ in opener.calls))

    def test_trusted_silent_password_login_and_cookie_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="trusted")
            mgr, auth = self._make_auth(tmp, opener, username="+8613800138000", password="secret")
            session = auth.login(interactive=False)

            self.assertEqual(session.user_id, "80086000000000001")
            self.assertTrue(opener.trusted_browser_updated)
            self.assertTrue(auth.cookie_jar_path.exists())
            lwp_text = auth.cookie_jar_path.read_text(encoding="utf-8")
            self.assertIn("CASTGC", lwp_text)
            self.assertIn("HWID_TRUST", lwp_text)

            login_calls = [form for _, u, form in opener.calls if "remoteLogin" in u]
            self.assertEqual(login_calls[0]["userAccount"][0], "008613800138000")
            self.assertEqual(login_calls[0]["localStorageID"][0], "lsid_999")
            self.assertEqual(login_calls[0]["pageToken"][0], "pt_123")

    def test_interactive_bash_2fa_sms_selection_and_trust_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="2fa_sms")
            mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            prompts: list[str] = []
            answers = iter(["2", "wrong0", "654321"])

            def _prompt(msg: str) -> str:
                prompts.append(msg)
                return next(answers)

            outputs: list[str] = []
            session = auth.login(interactive=True, prompt_fn=_prompt, output_fn=outputs.append)

            self.assertTrue(opener.sms_requested)
            self.assertTrue(opener.trusted_browser_updated)
            self.assertEqual(session.user_id, "80086000000000001")
            self.assertTrue(any("176****8703" in line for line in outputs))
            self.assertEqual(len(prompts), 3)

    def test_non_interactive_2fa_raises_two_factor_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="2fa_required")
            _mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            with self.assertRaises(TwoFactorRequiredError) as ctx:
                auth.login(interactive=False)
            self.assertIn("HUAWEI Mate", str(ctx.exception))
            self.assertIn("176****8703", str(ctx.exception))

    def test_invalid_password_raises_invalid_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="bad_pw")
            _mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="wrong")
            with self.assertRaises(InvalidCredentialsError):
                auth.login(interactive=False)

    def test_chk_risk_captcha_stops_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="captcha")
            _mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            with self.assertRaises(CaptchaRequiredError):
                auth.login(interactive=False)
            self.assertFalse(any("remoteLogin" in u for _, u, _ in opener.calls))

    def test_tier2_trusted_browser_auto_unlocks_without_2fa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="tier2_trusted")
            _mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            session = auth.login(interactive=False)
            self.assertEqual(session.cookies.get("loginSecLevel"), "2")
            self.assertEqual(session.device_id, "d" * 64)
            self.assertTrue(any("cloudIframeAuthIdentify/getPageInfo" in u for _, u, _ in opener.calls))
            self.assertFalse(any("cloudAuthLogin" in u for _, u, _ in opener.calls))

    def test_tier1_low_login_triggers_tier2_auth_identify_and_2fa_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="tier2_2fa")
            _mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            answers = iter(["1", "654321"])
            outputs: list[str] = []
            session = auth.login(
                interactive=True,
                prompt_fn=lambda _msg: next(answers),
                output_fn=outputs.append,
            )
            self.assertEqual(session.cookies.get("loginSecLevel"), "2")
            self.assertIn("2", opener.trust_oper_types)
            self.assertTrue(any("cloudAuthLogin" in u for _, u, _ in opener.calls))
            self.assertTrue(any("HUAWEI Pura" in line for line in outputs))

    def test_pending_2fa_resume_with_two_fa_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="tier2_2fa")
            mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            with self.assertRaises(TwoFactorRequiredError):
                auth.login(interactive=False)
            self.assertTrue(auth.pending_2fa_path.exists())

            opener.calls.clear()
            auth2 = HuaweiCasAuthenticator(
                mgr,
                username="13800138000",
                password="secret",
                two_fa_code="654321",
                opener=opener,  # type: ignore[arg-type]
            )
            opener.jar = auth2._cookie_jar
            session = auth2.login(interactive=False)
            self.assertEqual(session.cookies.get("loginSecLevel"), "2")
            self.assertFalse(auth2.pending_2fa_path.exists())
            self.assertFalse(any("remoteLogin" in u for _, u, _ in opener.calls))
            self.assertTrue(any("cloudAuthLogin" in u for _, u, _ in opener.calls))

    def test_expired_low_tgt_falls_back_to_full_password_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="expired_low_tgt")
            mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            mgr.save(
                AuthSession(
                    cookies={"loginSecLevel": "0", "shareToken": "old_tok"},
                    csrf_token="old_tok",
                    user_id="80086000000000001",
                    device_id="d" * 64,
                )
            )
            opener._set_cookie("CASTGCLOW", "TGT-OLD-LOW", ".id1.cloud.huawei.com", discard=True)

            session = auth.login(interactive=False)
            self.assertEqual(session.cookies.get("loginSecLevel"), "2")
            self.assertTrue(opener.remote_login_called)
            ai_calls = [u for _, u, _ in opener.calls if "cloudIframeAuthIdentify/getPageInfo" in u]
            self.assertEqual(len(ai_calls), 2)
            self.assertTrue(auth.cas_state_path.exists())

    def test_pending_remote_login_2fa_resume_without_plaintext_password_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="2fa_required")
            mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            with self.assertRaises(TwoFactorRequiredError):
                auth.login(interactive=False)
            self.assertTrue(auth.pending_2fa_path.exists())
            pending_text = auth.pending_2fa_path.read_text(encoding="utf-8")
            self.assertNotIn("secret", pending_text)

            opener.calls.clear()
            auth2 = HuaweiCasAuthenticator(
                mgr,
                username="13800138000",
                password="secret",
                two_fa_code="654321",
                opener=opener,  # type: ignore[arg-type]
            )
            opener.jar = auth2._cookie_jar
            session = auth2.login(interactive=False)
            self.assertEqual(session.cookies.get("loginSecLevel"), "2")
            self.assertFalse(auth2.pending_2fa_path.exists())
            self.assertTrue(opener.trusted_browser_updated)

    def test_oauth2_v3_authorize_bridge_completes_home_transit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeCasOpener(mode="oauth_bridge")
            _mgr, auth = self._make_auth(tmp, opener, username="13800138000", password="secret")
            session = auth.login(interactive=False)
            self.assertEqual(session.user_id, "80086000000000001")
            self.assertEqual(session.csrf_token, "share_tok_portal")
            self.assertTrue(any("/oauth2/ajax/getLoginWay" in u for _, u, _ in opener.calls))
            self.assertTrue(any("/oauth2/ajax/login" in u for _, u, _ in opener.calls))
            self.assertTrue(any("/homeTransit" in u for _, u, _ in opener.calls))


if __name__ == "__main__":
    unittest.main()
