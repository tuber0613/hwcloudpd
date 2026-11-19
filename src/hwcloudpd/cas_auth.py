"""Pure-HTTP Huawei ID CAS protocol authenticator (`icloudpd` parity, zero browser)."""

from __future__ import annotations

from contextlib import contextmanager
import html
from http.cookiejar import LWPCookieJar
import json
import logging
import os
from pathlib import Path
import random
import re
import secrets
import ssl
import threading
import time
from typing import Any, Callable, Iterator
import urllib.error
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse
import urllib.request

from hwcloudpd.auth import AuthManager
from hwcloudpd.client import generate_trace_id
from hwcloudpd.fileutil import LockTimeoutError, exclusive_file_lock, write_secret_file_atomic
from hwcloudpd.models import (
    AuthExpiredError,
    AuthSession,
    CaptchaRequiredError,
    HwCloudError,
    InvalidCredentialsError,
    RateLimitError,
    TwoFactorRequiredError,
)

logger = logging.getLogger(__name__)

CAS_BASE_URL = "https://id1.cloud.huawei.com"
PORTAL_BASE_URL = "https://cloud.huawei.com"
DEFAULT_AJAX_BASE = "https://id1.cloud.huawei.com/CAS/IDM_W/ajaxHandler/"
CAS_VERSION = "UP_CAS_6.26.3.100"
PORTAL_LOGIN_ACTION = "https://cloud.huawei.com:443/others/login.action"
DEFAULT_UNLOCK_URL = (
    "https://id1.cloud.huawei.com/CAS/portal/authIdentify.html"
    "?loginUrl=https%3A%2F%2Fcloud.huawei.com%3A443%2Fothers%2Flogin.action"
    "&service=https%3A%2F%2Fcloud.huawei.com%3A443%2Fothers%2Flogin.action"
    "&lang=zh-cn&reqClientType=1&loginChannel=1000002&scenesType=0"
)

DEFAULT_IFRAME_PARAMS = {
    "loginUrl": PORTAL_LOGIN_ACTION,
    "service": PORTAL_LOGIN_ACTION,
    "reqClientType": "1",
    "loginChannel": "1000002",
    "lang": "zh-cn",
    "countryCode": "cn",
    "scenesType": "0",
    "clientID": "4805300",
    "scope": "https://www.huawei.com/auth/account/pwdlowlogin",
}

_CREDENTIAL_ERROR_CODES = {
    "70002001",
    "70002003",
    "70002057",
    "70002058",
    "10002057",
    "10002058",
    "10000400",
    "11000400",
    "70002059",
    "10000003",
    "70002076",
}
_CAPTCHA_ERROR_CODES = {"10000201", "10000706"}
_RATE_LIMIT_ERROR_CODES = {"10002039", "70002039", "10002083"}
_TWO_FA_ERROR_CODES = {"10002080", "10012072", "10012076"}


def normalize_phone_account(account: str) -> str:
    """Trim whitespace and convert leading `+` to `00` (matching Huawei `convPlusOfPhoneAccount`)."""
    cleaned = account.strip()
    if cleaned.startswith("+"):
        return "00" + cleaned[1:]
    return cleaned


def infer_account_type(account: str) -> str:
    """Infer Huawei accountType (`1`=email, `2`=phone, `0`=accountName)."""
    cleaned = account.strip()
    if "@" in cleaned:
        return "1"
    if re.fullmatch(r"[+\d\s-]+", cleaned):
        return "2"
    return "0"


def _is_portal_cookie_domain(domain: str) -> bool:
    dom = domain.strip().lower()
    if not dom:
        return False
    if "id1.cloud.huawei.com" in dom:
        return False
    return dom in {"cloud.huawei.com", ".cloud.huawei.com", ".huawei.com", "huawei.com"}


def _is_home_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == "cloud.huawei.com" and parsed.path.startswith("/home")


def _is_oauth_authorize_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme.lower() == "https"
        and host.startswith("oauth-login")
        and host.endswith(".cloud.huawei.com")
        and "/oauth2/" in parsed.path
    )


def _validate_huawei_https_url(url: str, *, label: str) -> str:
    """Ensure auth/CAS redirect & AJAX endpoints stay on HTTPS *.huawei.com."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not (
        host == "huawei.com" or host.endswith(".huawei.com")
    ):
        raise AuthExpiredError(f"拒绝不受信任的认证跳转地址 ({label}): {url!r}")
    return url


def _pick_pending_2fa_channel(channels: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer a channel whose verification code was already sent (sent == 1)."""
    for ch in channels:
        if int(ch.get("sent") or 0) == 1:
            return ch
    return channels[0]


def _deep_find_str(obj: Any, key: str) -> str:
    """Recursively search nested dicts for `key` (matching Huawei `At(e, t)`), returning unescaped str."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return html.unescape(str(obj[key]))
        for value in obj.values():
            found = _deep_find_str(value, key)
            if found:
                return found
    return ""


_UNESCAPED_LOCAL_INFO_KEYS = {"randomCodeHtml", "agreementContents", "errorDesc"}


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    for candidate in (raw, html.unescape(raw)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def _unescape_cas_value(value: Any) -> Any:
    if isinstance(value, str):
        return html.unescape(value)
    if isinstance(value, list):
        return [_unescape_cas_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _unescape_cas_value(v) for k, v in value.items()}
    return value


def _normalize_local_info(page_info: dict[str, Any]) -> dict[str, Any]:
    """Normalize CAS getPageInfo response into `window.localInfo` matching Huawei frontend JS."""
    raw_local = page_info.get("localInfo")
    local_info: dict[str, Any] = dict(raw_local) if isinstance(raw_local, dict) else {}

    for key, value in list(local_info.items()):
        if key not in _UNESCAPED_LOCAL_INFO_KEYS:
            local_info[key] = _unescape_cas_value(value)

    for top_key in ("pageName", "pageToken", "pageTokenKey"):
        if page_info.get(top_key) is not None:
            local_info[top_key] = str(page_info[top_key])

    if local_info.get("regionCode") and not local_info.get("countryCode"):
        local_info["countryCode"] = local_info["regionCode"]
    return local_info


class HuaweiCasAuthenticator:
    """Pure-HTTP Huawei ID CAS authenticator with cookiejar persistence and REST 2FA."""

    def __init__(
        self,
        auth_manager: AuthManager,
        *,
        username: str | None = None,
        password: str | None = None,
        cookie_jar_path: Path | None = None,
        two_fa_code: str | None = None,
        login_timeout_sec: int = 30,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.auth_manager = auth_manager
        self.username = (username or "").strip() or None
        self.password = password if password else None
        self.cookie_jar_path = cookie_jar_path or (auth_manager.config_dir / "cas_cookies.lwp")
        self.pending_2fa_path = auth_manager.config_dir / "cas_pending_2fa.json"
        self.cas_state_path = auth_manager.config_dir / "cas_state.json"
        self.lock_file = auth_manager.config_dir / "cas_auth.lock"
        self.two_fa_code = (two_fa_code or "").strip() or None
        self.login_timeout_sec = max(10, int(login_timeout_sec))
        self.user_agent = AuthSession.user_agent

        self._jar_lock = threading.RLock()
        self._login_thread_lock = threading.Lock()
        self._cookie_jar = LWPCookieJar(str(self.cookie_jar_path))
        self._load_cookie_jar()
        self._custom_opener = opener
        if opener is not None:
            self._opener = opener
        else:
            ssl_ctx = ssl.create_default_context()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_ctx),
                urllib.request.HTTPCookieProcessor(self._cookie_jar),
            )

    @property
    def has_credentials(self) -> bool:
        """Return True if both Huawei ID username and password are configured."""
        return bool(self.username and self.password)

    @property
    def can_attempt_login(self) -> bool:
        """Return True if Huawei ID credentials or an unexpired CASTGC cookie are available."""
        return self.has_credentials or self._has_cas_tgt()

    def _jar_file_mtime(self) -> float:
        try:
            return self.cookie_jar_path.stat().st_mtime
        except OSError:
            return -1.0

    def _load_cookie_jar(self) -> None:
        with self._jar_lock:
            self._jar_mtime = self._jar_file_mtime()
            if not self.cookie_jar_path.exists():
                return
            try:
                self._cookie_jar.load(ignore_discard=True, ignore_expires=False)
            except Exception as exc:
                logger.debug("Could not load existing cookie jar %s: %s", self.cookie_jar_path, exc)

    def _reload_cookie_jar_if_changed(self) -> None:
        """Pick up a cookie jar written by another process (e.g. `--auth-only` via docker exec).

        Without this, a long-running daemon would later save its stale in-memory jar over the
        fresh one and lose the "trust this browser" cookies, forcing 2FA again.
        """
        with self._jar_lock:
            if self._jar_file_mtime() == self._jar_mtime:
                return
            logger.info("Cookie jar %s changed on disk; reloading", self.cookie_jar_path)
            self._cookie_jar.clear()
            self._load_cookie_jar()

    def _save_cookie_jar(self) -> None:
        with self._jar_lock:
            body = "#LWP-Cookies-2.0\n" + self._cookie_jar.as_lwp_str(
                ignore_discard=True, ignore_expires=False
            )
            write_secret_file_atomic(self.cookie_jar_path, body, mode=0o600)
            self._jar_mtime = self._jar_file_mtime()

    @contextmanager
    def _acquire_lock(self, timeout_sec: float = 30.0) -> Iterator[None]:
        try:
            with self._login_thread_lock, exclusive_file_lock(self.lock_file, timeout_sec=timeout_sec):
                yield
        except LockTimeoutError as exc:
            raise HwCloudError(f"Timed out waiting for CAS auth lock {self.lock_file}") from exc

    def _has_cas_tgt(self) -> bool:
        with self._jar_lock:
            # Never clear/reload the in-memory jar while another thread is actively running login().
            if not self._login_thread_lock.locked():
                self._reload_cookie_jar_if_changed()
            for cookie in self._cookie_jar:
                if cookie.name.upper().startswith("CASTGC") and cookie.value:
                    if not cookie.is_expired():
                        return True
            return False

    def _open_http(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, str, dict[str, str], bytes]:
        req = urllib.request.Request(
            url=url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with self._opener.open(req, timeout=self.login_timeout_sec) as resp:
                status = int(getattr(resp, "status", 200))
                final_url = str(resp.geturl() if hasattr(resp, "geturl") else url)
                resp_headers = {str(k): str(v) for k, v in resp.headers.items()}
                raw = resp.read()
                return status, final_url, resp_headers, raw
        except urllib.error.HTTPError as err:
            status = int(err.code)
            final_url = str(err.geturl() if hasattr(err, "geturl") else url)
            resp_headers = {str(k): str(v) for k, v in err.headers.items()} if err.headers else {}
            raw = err.read() if err.fp is not None else b""
            return status, final_url, resp_headers, raw
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HwCloudError(f"HTTP error requesting {urlparse(url).path}: {exc}") from exc

    def _cas_post(
        self,
        ajax_base: str,
        interface_name: str,
        data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        local_info: dict[str, Any] = state.get("local_info") or {}
        query_params: dict[str, Any] = state.get("query_params") or {}
        payload: dict[str, Any] = {}

        # Replicate ze(...) common parameter injection using current page localInfo / URL params
        for key in ("reqClientType", "loginChannel", "clientID", "lang", "state", "terminalType"):
            val = local_info.get(key) or query_params.get(key)
            if val:
                payload[key] = str(val)
                if key == "lang":
                    payload["languageCode"] = str(val)

        if local_info.get("pageToken") is not None:
            payload["pageToken"] = str(local_info["pageToken"])
        if local_info.get("pageTokenKey") is not None:
            payload["pageTokenKey"] = str(local_info["pageTokenKey"])

        for k, v in data.items():
            if v is not None:
                payload[k] = v

        if "countryCode" in payload:
            payload["regionCode"] = payload.pop("countryCode")

        if state.get("local_storage_id"):
            payload["localStorageID"] = state["local_storage_id"]

        base = ajax_base.rstrip("/") + "/"
        sep = "&" if "?" in interface_name else "?"
        url = (
            f"{base}{interface_name.lstrip('/')}"
            f"{sep}reflushCode={random.random():.16f}&cVersion={CAS_VERSION}"
        )

        encoded_body = urlencode(
            [(k, "" if v is None else str(v)) for k, v in payload.items()]
        ).encode("utf-8")

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": CAS_BASE_URL,
            "Referer": state["referer"],
            "User-Agent": self.user_agent,
            "X-Requested-With": "XMLHttpRequest",
        }

        status, _final_url, _resp_headers, raw = self._open_http(
            "POST", url, headers=headers, body=encoded_body
        )
        text = raw.decode("utf-8", errors="replace").strip()
        if status >= 400 and not text:
            raise HwCloudError(f"CAS {interface_name} returned HTTP {status}")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HwCloudError(
                f"CAS {interface_name} returned non-JSON (HTTP {status}): {text[:200]}"
            ) from exc

        if not isinstance(parsed, dict):
            raise HwCloudError(f"CAS {interface_name} returned unexpected JSON payload")

        if parsed.get("localStorageID"):
            state["local_storage_id"] = str(parsed["localStorageID"])
            self._save_cas_state(state["local_storage_id"])
        if parsed.get("hwErrorDesc") and not parsed.get("errorDesc"):
            parsed["errorDesc"] = parsed["hwErrorDesc"]
        return parsed

    def _collect_portal_cookies(self) -> dict[str, str]:
        with self._jar_lock:
            portal_cookies: dict[str, str] = {}
            for cookie in self._cookie_jar:
                if not cookie.name or not cookie.value:
                    continue
                if cookie.is_expired():
                    continue
                if _is_portal_cookie_domain(cookie.domain):
                    portal_cookies[cookie.name] = cookie.value
            return portal_cookies

    def _write_json_atomic(self, target: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        write_secret_file_atomic(target, text, mode=0o600)

    def _load_cas_state(self) -> str:
        if not self.cas_state_path.exists():
            return ""
        try:
            data = json.loads(self.cas_state_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("local_storage_id") or "")

    def _save_cas_state(self, local_storage_id: str) -> None:
        if not local_storage_id:
            return
        self._write_json_atomic(
            self.cas_state_path,
            {
                "local_storage_id": local_storage_id,
                "updated_at": time.time(),
            },
        )

    def _save_pending_2fa(self, payload: dict[str, Any]) -> None:
        self._write_json_atomic(self.pending_2fa_path, payload)

    def _load_pending_2fa(self, *, max_age_sec: float = 600.0) -> dict[str, Any] | None:
        if not self.pending_2fa_path.exists():
            return None
        try:
            data = json.loads(self.pending_2fa_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        created = float(data.get("created_at") or 0.0)
        if created <= 0 or (time.time() - created) > max_age_sec:
            return None
        return data

    def _clear_pending_2fa(self) -> None:
        try:
            self.pending_2fa_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _fetch_portal_home_data(
        self,
        portal_cookies: dict[str, str],
        csrf_token: str,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        trace_id = generate_trace_id("00001")
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in portal_cookies.items())
        home_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": PORTAL_BASE_URL,
            "Referer": f"{PORTAL_BASE_URL}/home",
            "User-Agent": self.user_agent,
            "x-hw-trace-id": trace_id,
            "x-hw-device-category": "Web",
            "x-hw-device-type": "7",
            "x-hw-client-mode": "frontend",
            "x-hw-os-brand": "Web",
        }
        if cookie_hdr:
            home_headers["Cookie"] = cookie_hdr
        if csrf_token:
            home_headers["CSRFToken"] = csrf_token

        status, _url, resp_hdrs, raw = self._open_http(
            "POST",
            f"{PORTAL_BASE_URL}/html/getHomeData",
            headers=home_headers,
            body=json.dumps({"traceId": trace_id}).encode("utf-8"),
        )
        home_json = _parse_json_dict(raw.decode("utf-8", errors="replace"))
        return status, resp_hdrs, home_json

    def _select_2fa_channel_and_send(
        self,
        ajax_base: str,
        channels: list[dict[str, Any]],
        state: dict[str, Any],
        *,
        user_account: str | None,
        site_id: Any,
        req_client_type: str,
        lang: str,
        login_channel: str,
        sms_oper_type: str,
        email_oper_type: str,
        interactive: bool,
        prompt_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> dict[str, Any]:
        selected_idx = 0
        if interactive and not self.two_fa_code and len(channels) > 1:
            output_fn("检测到华为账号二次验证，请选择验证方式:")
            for i, ch in enumerate(channels):
                output_fn("  " + self._format_channel_label(i + 1, ch))
            raw_choice = prompt_fn(f"请输入序号 [1-{len(channels)}]（直接回车默认 1）: ").strip()
            if raw_choice.isdigit():
                idx_val = int(raw_choice) - 1
                if 0 <= idx_val < len(channels):
                    selected_idx = idx_val

        selected = channels[selected_idx]
        atype = selected["accountType"]
        cleaned_acct = (user_account or "").strip()

        if atype in (1, 2, 5, 6) and selected["sent"] != 1:
            common_req: dict[str, Any] = {
                "siteID": str(site_id),
                "reqClientType": req_client_type,
                "languageCode": lang,
                "loginChannel": login_channel,
            }
            if cleaned_acct:
                common_req["userAccount"] = normalize_phone_account(cleaned_acct)
                common_req["accountType"] = infer_account_type(cleaned_acct)

            if atype in (2, 6):
                self._cas_post(
                    ajax_base,
                    "getSMSCodeV3",
                    {
                        **common_req,
                        "mobilePhone": selected["name"],
                        "operType": sms_oper_type,
                        "smsReqType": "6",
                    },
                    state,
                )
                if interactive:
                    output_fn(f"已向 {selected['name']} 发送短信验证码。")
            else:
                self._cas_post(
                    ajax_base,
                    "getEMailAuthCode",
                    {
                        **common_req,
                        "email": selected["name"],
                        "operType": email_oper_type,
                        "emailReqType": "6",
                    },
                    state,
                )
                if interactive:
                    output_fn(f"已向 {selected['name']} 发送邮箱验证码。")
        elif interactive:
            output_fn(f"请查看受信任设备 ({selected['name']}) 收到的 6 位验证码。")

        return selected

    def _run_2fa_verification_loop(
        self,
        ajax_base: str,
        state: dict[str, Any],
        submit_fn: Callable[[str], dict[str, Any]],
        trust_payload: dict[str, Any] | None,
        *,
        require_need_pop_trust: bool,
        interactive: bool,
        prompt_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> str:
        max_tries = 1 if self.two_fa_code else 3
        for attempt in range(max_tries):
            if self.two_fa_code:
                code = self.two_fa_code
            else:
                try:
                    code = prompt_fn("请输入华为账号 6 位二次验证码: ").strip()
                except EOFError as exc:
                    raise TwoFactorRequiredError("标准输入已关闭，无法读取二次验证码") from exc

            code = re.sub(r"\s+", "", code)
            if not code:
                if interactive:
                    output_fn("验证码不能为空，请重新输入。")
                continue

            v_resp = submit_fn(code)
            if str(v_resp.get("isSuccess")) == "1":
                self._clear_pending_2fa()
                if trust_payload and (not require_need_pop_trust or v_resp.get("needPopTrust")):
                    try:
                        self._cas_post(ajax_base, "updateTrustBrowser", trust_payload, state)
                    except Exception as exc:
                        logger.warning("updateTrustBrowser call failed (non-fatal): %s", exc)
                self._save_cookie_jar()
                return html.unescape(str(v_resp.get("callbackURL") or ""))

            verr = str(v_resp.get("errorCode") or "")
            vdesc = str(v_resp.get("errorDesc") or "")
            if interactive and attempt < max_tries - 1:
                output_fn(f"二次验证码校验失败 (errorCode={verr}) {vdesc}，请重试。")
            else:
                raise TwoFactorRequiredError(
                    f"二次验证码校验失败 (errorCode={verr}): {vdesc}"
                )

        raise TwoFactorRequiredError("二次验证未完成或尝试次数过多")

    def _submit_cloud_auth_login(
        self,
        ajax_base: str,
        state: dict[str, Any],
        selected: dict[str, Any],
        tips_scene_id: str,
        *,
        interactive: bool,
        prompt_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> str:
        local_info = state.get("local_info") or {}
        atype = int(selected["accountType"])

        def _submit(code: str) -> dict[str, Any]:
            verify_payload: dict[str, Any] = {
                "twoStepVerifyCode": code,
                "verifyAccountType": str(atype),
                "verifyUserAccount": selected["name"],
                "hwmeta": "",
            }
            if local_info.get("userAccount"):
                verify_payload["userAccount"] = str(local_info["userAccount"])
            if local_info.get("password") is not None:
                verify_payload["password"] = str(local_info["password"])
            if atype != -1 and tips_scene_id:
                verify_payload["tipsSceneID"] = tips_scene_id
            return self._cas_post(ajax_base, "cloudAuthLogin", verify_payload, state)

        return self._run_2fa_verification_loop(
            ajax_base,
            state,
            _submit,
            {"operType": "2", "trustBrowser": "1"},
            require_need_pop_trust=True,
            interactive=interactive,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )

    def _step_up_unlock_gallery(
        self,
        unlock_url: str,
        *,
        local_storage_id: str = "",
        interactive: bool,
        prompt_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> str:
        # Resume pending authIdentify 2FA challenge if a non-TTY caller supplied --two-fa-code.
        if self.two_fa_code:
            pending = self._load_pending_2fa()
            if pending and pending.get("flow") == "authIdentify":
                channels = pending.get("channels") or []
                if channels:
                    return self._submit_cloud_auth_login(
                        _validate_huawei_https_url(str(pending["ajax_base"]), label="pending ajax_base"),
                        pending["state"],
                        _pick_pending_2fa_channel(channels),
                        str(pending.get("tips_scene_id") or ""),
                        interactive=interactive,
                        prompt_fn=prompt_fn,
                        output_fn=output_fn,
                    )

        unlock_url = _validate_huawei_https_url(unlock_url, label="unlockUrl")
        parsed = urlparse(unlock_url)
        raw_qs = parsed.query
        flat_params = {k: v[-1] for k, v in parse_qs(raw_qs, keep_blank_values=True).items() if v}
        state: dict[str, Any] = {
            "referer": unlock_url,
            "query_params": flat_params,
            "local_info": {},
            "local_storage_id": local_storage_id or self._load_cas_state(),
        }

        _s, final_unlock_url, _hdrs, _raw = self._open_http(
            "GET",
            unlock_url,
            headers={
                "User-Agent": self.user_agent,
                "Referer": f"{PORTAL_BASE_URL}/home",
                "Accept": "text/html,*/*;q=0.8",
            },
        )
        if "login.action" in final_unlock_url and "ticket=" in final_unlock_url:
            return final_unlock_url

        ajax_base = DEFAULT_AJAX_BASE
        try:
            self._cas_post(ajax_base, "common/getBaseSwitchInfo", {}, state)
        except Exception as exc:
            logger.debug("authIdentify getBaseSwitchInfo ignored: %s", exc)

        page_req = dict(flat_params)
        page_req["pageName"] = "cloudIframeAuthIdentify"
        page_req["interfaceName"] = "cloudIframeAuthIdentify/getPageInfo"
        page_req["supportHarmonyTheme"] = "false"
        page_req["urlParam"] = raw_qs
        page_info = self._cas_post(ajax_base, "cloudIframeAuthIdentify/getPageInfo", page_req, state)

        raw_local_info = (
            page_info.get("localInfo") if isinstance(page_info.get("localInfo"), dict) else {}
        )
        local_info = _normalize_local_info(page_info)
        state["local_info"] = local_info
        if local_info.get("localHttpsAjaxPath"):
            resolved = _validate_huawei_https_url(
                urljoin(CAS_BASE_URL, str(local_info["localHttpsAjaxPath"])),
                label="authIdentify localHttpsAjaxPath",
            )
            ajax_base = resolved.rstrip("/") + "/ajaxHandler/"

        if page_info.get("redirectUrl"):
            return html.unescape(str(page_info["redirectUrl"]))
        if str(page_info.get("errorCode")) == "10000302" and str(
            page_info.get("errorDesc") or ""
        ).startswith("http"):
            return html.unescape(str(page_info["errorDesc"]))
        if local_info.get("redirectUrl"):
            return str(local_info["redirectUrl"])
        if str(local_info.get("isUnlock")).lower() == "true" and local_info.get("remoteLogin"):
            return str(local_info["remoteLogin"])
        if str(local_info.get("isLowTGTExpired")).lower() == "true":
            raise AuthExpiredError("CAS 低安全级会话 (CASTGCLOW) 已过期，需要重新执行密码登录")

        desc_obj = _parse_json_dict(
            str(raw_local_info.get("errorDesc") or page_info.get("errorDesc") or "")
        )
        channels = self._build_2fa_channels(desc_obj)
        tips_scene_id = str(desc_obj.get("tipsSceneID") or "")

        if not channels:
            raise AuthExpiredError(
                f"cloudIframeAuthIdentify 未返回解锁跳转地址或二次验证方式 (errorCode={page_info.get('errorCode')!r})"
            )

        channel_summary = "; ".join(
            self._format_channel_label(i + 1, ch) for i, ch in enumerate(channels)
        )

        if not interactive and not self.two_fa_code:
            self._save_cookie_jar()
            self._save_pending_2fa(
                {
                    "flow": "authIdentify",
                    "created_at": time.time(),
                    "ajax_base": ajax_base,
                    "state": state,
                    "channels": channels,
                    "tips_scene_id": tips_scene_id,
                }
            )
            raise TwoFactorRequiredError(
                f"华为云空间图库解锁需要二次验证（{channel_summary}）。"
                "请在终端执行 `docker exec -it hwcloudpd hwcloudpd --auth-only` 输入验证码（完成后将记住受信任设备）。"
            )

        raw_user = str(local_info.get("userAccount") or "").strip() or None
        site_id = local_info.get("curAccountSiteID") or local_info.get("currentSiteID") or 1
        req_client_type = str(local_info.get("reqClientType") or flat_params.get("reqClientType") or "1")
        lang = str(local_info.get("lang") or flat_params.get("lang") or "zh-cn")
        login_channel = str(local_info.get("loginChannel") or flat_params.get("loginChannel") or "1000002")

        selected = self._select_2fa_channel_and_send(
            ajax_base,
            channels,
            state,
            user_account=raw_user,
            site_id=site_id,
            req_client_type=req_client_type,
            lang=lang,
            login_channel=login_channel,
            sms_oper_type="22",
            email_oper_type="22",
            interactive=interactive,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )

        return self._submit_cloud_auth_login(
            ajax_base,
            state,
            selected,
            tips_scene_id,
            interactive=interactive,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )

    def _oauth2_ajax_post(
        self,
        origin: str,
        endpoint: str,
        query_body: str,
        referer: str,
    ) -> dict[str, Any]:
        url = f"{origin.rstrip('/')}/oauth2/ajax/{endpoint.lstrip('/')}?reflushCode={random.random():.16f}"
        status, _final_url, _hdrs, raw = self._open_http(
            "POST",
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "interfaceVersion": "v3",
                "fromLoginAuth": "false",
                "Origin": origin,
                "Referer": referer,
                "User-Agent": self.user_agent,
            },
            body=query_body.encode("utf-8"),
        )
        text = raw.decode("utf-8", errors="replace").strip()
        if status >= 400 and not text:
            raise AuthExpiredError(f"OAuth2 {endpoint} returned HTTP {status}")
        parsed = _parse_json_dict(text)
        if not parsed:
            raise AuthExpiredError(f"OAuth2 {endpoint} returned non-JSON (HTTP {status})")
        return parsed

    def _complete_oauth2_authorize(self, authorize_url: str) -> str:
        """Complete Huawei OAuth2 v3 silent bridge (`/oauth2/v3/authorize` -> `/homeTransit`)."""
        authorize_url = _validate_huawei_https_url(authorize_url, label="oauth2 authorize_url")
        parsed_auth = urlparse(authorize_url)
        oauth_origin = f"{parsed_auth.scheme or 'https'}://{parsed_auth.netloc}"
        auth_qs = parsed_auth.query

        way = self._oauth2_ajax_post(oauth_origin, "getLoginWay", auth_qs, authorize_url)
        if way.get("code"):
            code_url = _validate_huawei_https_url(
                html.unescape(str(way["code"])), label="oauth2 code_url"
            )
            _, final_url, _, _ = self._open_http(
                "GET",
                code_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"{oauth_origin}/",
                    "User-Agent": self.user_agent,
                },
            )
            return final_url

        cas_redir = _deep_find_str(way.get("loginInteractInfo"), "casLoginRedirectUrl")
        if not cas_redir:
            raise AuthExpiredError("OAuth2 getLoginWay 未返回 casLoginRedirectUrl")
        cas_redir = _validate_huawei_https_url(cas_redir, label="casLoginRedirectUrl")

        _, cb_url, _, _ = self._open_http(
            "GET",
            cas_redir,
            headers={
                "Accept": "text/html,*/*;q=0.8",
                "Referer": authorize_url,
                "User-Agent": self.user_agent,
            },
        )
        if _is_home_url(cb_url):
            return cb_url

        cb_url = _validate_huawei_https_url(cb_url, label="oauth2 callback")
        parsed_cb = urlparse(cb_url)
        cb_origin = f"{parsed_cb.scheme or 'https'}://{parsed_cb.netloc}"
        cb_qs = parsed_cb.query

        login_res = self._oauth2_ajax_post(cb_origin, "login", cb_qs, cb_url)
        code_url = html.unescape(str(login_res.get("code") or ""))
        if not code_url:
            raise AuthExpiredError(
                f"OAuth2 login 未返回 homeTransit 跳转地址: {sorted(login_res.keys())}"
            )
        code_url = _validate_huawei_https_url(code_url, label="oauth2 homeTransit")

        _, final_home_url, _, _ = self._open_http(
            "GET",
            code_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{cb_origin}/",
                "User-Agent": self.user_agent,
            },
        )
        return final_home_url

    def _exchange_ticket_and_bootstrap_portal(
        self,
        callback_url: str | None,
        *,
        allow_step_up: bool = True,
        local_storage_id: str = "",
        interactive: bool = False,
        prompt_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> AuthSession:
        if callback_url:
            callback_url = _validate_huawei_https_url(callback_url, label="SSO callbackURL")
            status, final_url, _hdrs, _raw = self._open_http(
                "GET",
                callback_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"{CAS_BASE_URL}/",
                    "User-Agent": self.user_agent,
                },
            )
            if _is_oauth_authorize_url(final_url):
                final_url = self._complete_oauth2_authorize(final_url)
            logger.debug("SSO callback resolved status=%s url=%s", status, final_url)

        self._save_cookie_jar()
        portal_cookies = self._collect_portal_cookies()
        csrf_token = portal_cookies.get("shareToken") or portal_cookies.get("CSRFToken") or ""

        status, resp_hdrs, home_json = self._fetch_portal_home_data(portal_cookies, csrf_token)
        self._save_cookie_jar()
        portal_cookies = self._collect_portal_cookies()
        for k, v in resp_hdrs.items():
            if k.lower() == "csrftoken" and v.strip():
                csrf_token = v.strip()
        if not csrf_token:
            csrf_token = portal_cookies.get("shareToken") or portal_cookies.get("CSRFToken") or ""
        if csrf_token and "shareToken" not in portal_cookies:
            portal_cookies["shareToken"] = csrf_token

        # Step-up unlock to loginSecLevel=2 if initial pwdlowlogin gave loginSecLevel=0
        sec_level = portal_cookies.get("loginSecLevel")
        if allow_step_up and sec_level is not None and sec_level != "2":
            unlock_url = str(home_json.get("unlockUrl") or DEFAULT_UNLOCK_URL)
            logger.info(
                "Portal session has loginSecLevel=%r; running Tier-2 authIdentify unlock...",
                sec_level,
            )
            unlock_cb = self._step_up_unlock_gallery(
                unlock_url,
                local_storage_id=local_storage_id,
                interactive=interactive,
                prompt_fn=prompt_fn,
                output_fn=output_fn,
            )
            return self._exchange_ticket_and_bootstrap_portal(
                unlock_cb or None,
                allow_step_up=False,
                local_storage_id=local_storage_id,
                interactive=interactive,
                prompt_fn=prompt_fn,
                output_fn=output_fn,
            )

        if not allow_step_up and sec_level is not None and sec_level != "2":
            raise AuthExpiredError(
                f"云空间解锁票据交换后 loginSecLevel 仍为 {sec_level!r}（需要 '2' 才能访问图库）"
            )

        # Also query /html/queryCookieValuesByNames if userId wasn't directly in getHomeData.
        user_id = str(
            home_json.get("userid")
            or home_json.get("uid")
            or portal_cookies.get("userId")
            or portal_cookies.get("ts_uid")
            or ""
        ).strip()

        if not user_id and portal_cookies and csrf_token:
            q_trace = generate_trace_id("25001")
            q_headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": PORTAL_BASE_URL,
                "Referer": f"{PORTAL_BASE_URL}/home",
                "User-Agent": self.user_agent,
                "x-hw-trace-id": q_trace,
                "CSRFToken": csrf_token,
                "Cookie": "; ".join(f"{k}={v}" for k, v in portal_cookies.items()),
            }
            _qs, _qu, _qh, qraw = self._open_http(
                "POST",
                f"{PORTAL_BASE_URL}/html/queryCookieValuesByNames",
                headers=q_headers,
                body=json.dumps({"traceId": q_trace}).encode("utf-8"),
            )
            qjson = _parse_json_dict(qraw.decode("utf-8", errors="replace"))
            cookie_map = qjson.get("cookies") if isinstance(qjson.get("cookies"), dict) else {}
            for cname in ("userId", "uid", "ts_uid"):
                if cookie_map.get(cname):
                    user_id = str(cookie_map[cname]).strip()
                    break

        if not portal_cookies or not csrf_token or not user_id:
            raise AuthExpiredError(
                f"SSO 票据交换后未能提取完整云空间凭证 (status={status}, has_cookies={bool(portal_cookies)}, has_csrf={bool(csrf_token)}, has_uid={bool(user_id)})"
            )

        existing = self.auth_manager.get_session_if_present()
        device_id = (
            (existing.device_id if existing and existing.device_id else "")
            or str(home_json.get("deviceIdForHeader") or "").strip()
            or secrets.token_hex(32)
        )
        extra_headers = dict(existing.extra_headers) if existing else {}

        session = AuthSession(
            cookies=portal_cookies,
            csrf_token=csrf_token,
            user_id=user_id,
            device_id=device_id,
            user_agent=self.user_agent,
            extra_headers=extra_headers,
            updated_at=time.time(),
        )
        self.auth_manager.save(session)
        logger.info("Pure-HTTP CAS login succeeded for userid=%s (loginSecLevel=%s)", user_id, sec_level)
        return session

    def _build_2fa_channels(self, desc_obj: dict[str, Any]) -> list[dict[str, Any]]:
        sent_list = desc_obj.get("authCodeSentList") or []
        device_channels: list[dict[str, Any]] = []
        other_channels: list[dict[str, Any]] = []
        for entry in sent_list:
            if not isinstance(entry, dict):
                continue
            atype = int(entry.get("accountType") or 0)
            item = {
                "accountType": atype,
                "name": str(entry.get("name") or ""),
                "sent": int(entry.get("sent") or 0),
            }
            if atype == -1 and item["sent"] == 1:
                device_channels.append(item)
            else:
                other_channels.append(item)
        return (device_channels[:1] + other_channels) if device_channels else other_channels

    @staticmethod
    def _format_channel_label(idx: int, ch: dict[str, Any]) -> str:
        atype = ch["accountType"]
        name = ch["name"] or "未知"
        if atype == -1:
            tag = "华为受信任设备验证码 [已自动推送]" if ch["sent"] == 1 else "华为受信任设备验证码"
        elif atype in (2, 6):
            tag = "短信验证码"
        elif atype in (1, 5):
            tag = "邮箱验证码"
        else:
            tag = f"验证方式({atype})"
        return f"[{idx}] {tag}: {name}"

    def _submit_remote_login_2fa(
        self,
        ajax_base: str,
        login_form: dict[str, Any],
        state: dict[str, Any],
        selected: dict[str, Any],
        tips_scene_id: str,
        *,
        interactive: bool,
        prompt_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> str:
        atype = int(selected["accountType"])

        def _submit(code: str) -> dict[str, Any]:
            verify_form = dict(login_form)
            if not verify_form.get("password") and self.password:
                verify_form["password"] = self.password
            verify_form.update(
                {
                    "twoStepVerifyCode": code,
                    "verifyAccountType": str(atype),
                    "verifyUserAccount": selected["name"],
                    "opType": "1",
                }
            )
            if atype != -1 and tips_scene_id:
                verify_form["tipsSceneID"] = tips_scene_id
            return self._cas_post(ajax_base, "remoteLogin", verify_form, state)

        return self._run_2fa_verification_loop(
            ajax_base,
            state,
            _submit,
            {
                "userAccount": login_form.get("userAccount") or normalize_phone_account(self.username or ""),
                "operType": "1",
                "trustBrowser": "1",
            },
            require_need_pop_trust=False,
            interactive=interactive,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )

    def _handle_2fa(
        self,
        ajax_base: str,
        login_form: dict[str, Any],
        resp: dict[str, Any],
        state: dict[str, Any],
        site_id: Any,
        *,
        interactive: bool,
        prompt_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
    ) -> str:
        desc_obj = _parse_json_dict(str(resp.get("errorDesc") or ""))
        channels = self._build_2fa_channels(desc_obj)
        tips_scene_id = str(desc_obj.get("tipsSceneID") or "")

        if not channels:
            raise TwoFactorRequiredError("华为账号触发二次验证，但未返回可用验证方式。")

        channel_summary = "; ".join(
            self._format_channel_label(i + 1, ch) for i, ch in enumerate(channels)
        )

        if not interactive and not self.two_fa_code:
            self._save_cookie_jar()
            persisted_form = {k: v for k, v in login_form.items() if k != "password"}
            self._save_pending_2fa(
                {
                    "flow": "remoteLogin",
                    "created_at": time.time(),
                    "ajax_base": ajax_base,
                    "state": state,
                    "login_form": persisted_form,
                    "channels": channels,
                    "tips_scene_id": tips_scene_id,
                    "site_id": site_id,
                }
            )
            raise TwoFactorRequiredError(
                f"触发华为账号二次验证（{channel_summary}）。"
                "请在终端执行 `docker exec -it hwcloudpd hwcloudpd --auth-only` 输入验证码（完成后将自动记住受信任设备）。"
            )

        raw_user = (self.username or "").strip() or None
        selected = self._select_2fa_channel_and_send(
            ajax_base,
            channels,
            state,
            user_account=raw_user,
            site_id=site_id,
            req_client_type=str(login_form["reqClientType"]),
            lang=str(login_form["lang"]),
            login_channel=str(login_form["loginChannel"]),
            sms_oper_type="8",
            email_oper_type="9",
            interactive=interactive,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )
        return self._submit_remote_login_2fa(
            ajax_base,
            login_form,
            state,
            selected,
            tips_scene_id,
            interactive=interactive,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )

    def login(
        self,
        *,
        interactive: bool = False,
        prompt_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> AuthSession:
        """Execute pure-HTTP CAS authentication (plus Tier-2 authIdentify unlock) and persist session."""
        with self._acquire_lock():
            self._reload_cookie_jar_if_changed()
            persisted_lsid = self._load_cas_state()

            # Fast-path 0: Resume pending 2FA when --two-fa-code is passed in a second non-TTY call.
            if self.two_fa_code:
                pending = self._load_pending_2fa()
                if pending and pending.get("flow") == "authIdentify":
                    pending_state = pending.get("state") or {}
                    unlock_cb = self._step_up_unlock_gallery(
                        DEFAULT_UNLOCK_URL,
                        local_storage_id=str(pending_state.get("local_storage_id") or persisted_lsid),
                        interactive=interactive,
                        prompt_fn=prompt_fn,
                        output_fn=output_fn,
                    )
                    return self._exchange_ticket_and_bootstrap_portal(
                        unlock_cb or None,
                        allow_step_up=False,
                        local_storage_id=str(pending_state.get("local_storage_id") or persisted_lsid),
                        interactive=interactive,
                        prompt_fn=prompt_fn,
                        output_fn=output_fn,
                    )
                if pending and pending.get("flow") == "remoteLogin":
                    channels = pending.get("channels") or []
                    if channels:
                        pending_state = pending.get("state") or {}
                        cb_url = self._submit_remote_login_2fa(
                            _validate_huawei_https_url(
                                str(pending["ajax_base"]), label="pending ajax_base"
                            ),
                            pending["login_form"],
                            pending_state,
                            _pick_pending_2fa_channel(channels),
                            str(pending.get("tips_scene_id") or ""),
                            interactive=interactive,
                            prompt_fn=prompt_fn,
                            output_fn=output_fn,
                        )
                        return self._exchange_ticket_and_bootstrap_portal(
                            cb_url or None,
                            local_storage_id=str(pending_state.get("local_storage_id") or persisted_lsid),
                            interactive=interactive,
                            prompt_fn=prompt_fn,
                            output_fn=output_fn,
                        )

            # Fast-path 1: If portal already has an active loginSecLevel=0 session, skip Tier-1 password login.
            skip_js_remote_login = False
            existing = self.auth_manager.get_session_if_present()
            if existing and existing.cookies.get("loginSecLevel") == "0" and self._has_cas_tgt():
                try:
                    _st, _hdrs, home_json = self._fetch_portal_home_data(
                        existing.cookies, existing.csrf_token
                    )
                    if str(home_json.get("isLogin")) == "1":
                        unlock_url = str(home_json.get("unlockUrl") or DEFAULT_UNLOCK_URL)
                        unlock_cb = self._step_up_unlock_gallery(
                            unlock_url,
                            local_storage_id=persisted_lsid,
                            interactive=interactive,
                            prompt_fn=prompt_fn,
                            output_fn=output_fn,
                        )
                        return self._exchange_ticket_and_bootstrap_portal(
                            unlock_cb or None,
                            allow_step_up=False,
                            local_storage_id=persisted_lsid,
                            interactive=interactive,
                            prompt_fn=prompt_fn,
                            output_fn=output_fn,
                        )
                except (TwoFactorRequiredError, CaptchaRequiredError, InvalidCredentialsError):
                    raise
                except Exception as exc:
                    skip_js_remote_login = True
                    logger.debug("Tier-1 existing session step-up check fell back to login: %s", exc)

            iframe_params = dict(DEFAULT_IFRAME_PARAMS)
            iframe_params["time"] = str(int(time.time() * 1000))
            iframe_qs = urlencode(iframe_params, quote_via=quote_plus)
            iframe_url = f"{CAS_BASE_URL}/CAS/portal/cloudIframeLogin.html?{iframe_qs}"
            state: dict[str, Any] = {
                "referer": iframe_url,
                "query_params": iframe_params,
                "local_info": {},
                "local_storage_id": persisted_lsid,
            }

            # 1. Warm portal & CAS iframe cookies
            _s1, home_check_url, _h1, _b1 = self._open_http(
                "GET",
                f"{PORTAL_BASE_URL}/",
                headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*"},
            )
            if _is_home_url(home_check_url) or _is_oauth_authorize_url(home_check_url):
                if _is_oauth_authorize_url(home_check_url):
                    self._complete_oauth2_authorize(home_check_url)
                return self._exchange_ticket_and_bootstrap_portal(
                    None,
                    local_storage_id=state["local_storage_id"],
                    interactive=interactive,
                    prompt_fn=prompt_fn,
                    output_fn=output_fn,
                )

            _s2, iframe_final_url, _h2, _b2 = self._open_http(
                "GET",
                iframe_url,
                headers={
                    "User-Agent": self.user_agent,
                    "Referer": f"{PORTAL_BASE_URL}/",
                    "Accept": "text/html,*/*;q=0.8",
                },
            )
            if _is_home_url(iframe_final_url) or _is_oauth_authorize_url(iframe_final_url):
                if _is_oauth_authorize_url(iframe_final_url):
                    self._complete_oauth2_authorize(iframe_final_url)
                return self._exchange_ticket_and_bootstrap_portal(
                    None,
                    local_storage_id=state["local_storage_id"],
                    interactive=interactive,
                    prompt_fn=prompt_fn,
                    output_fn=output_fn,
                )

            ajax_base = DEFAULT_AJAX_BASE
            try:
                self._cas_post(ajax_base, "common/getBaseSwitchInfo", {}, state)
            except Exception as exc:
                logger.debug("getBaseSwitchInfo ignored: %s", exc)

            page_req = dict(iframe_params)
            page_req["pageName"] = "cloudIframeLogin"
            page_req["urlParam"] = iframe_qs
            page_info = self._cas_post(ajax_base, "login/getPageInfo", page_req, state)

            if str(page_info.get("isSuccess")) == "1" and page_info.get("redirectUrl"):
                return self._exchange_ticket_and_bootstrap_portal(
                    html.unescape(str(page_info["redirectUrl"])),
                    local_storage_id=state["local_storage_id"],
                    interactive=interactive,
                    prompt_fn=prompt_fn,
                    output_fn=output_fn,
                )
            if str(page_info.get("errorCode")) == "10000302" and str(
                page_info.get("errorDesc") or ""
            ).startswith("http"):
                return self._exchange_ticket_and_bootstrap_portal(
                    html.unescape(str(page_info["errorDesc"])),
                    local_storage_id=state["local_storage_id"],
                    interactive=interactive,
                    prompt_fn=prompt_fn,
                    output_fn=output_fn,
                )

            local_info = _normalize_local_info(page_info)
            state["local_info"] = local_info
            if local_info.get("localHttpsAjaxPath"):
                resolved = _validate_huawei_https_url(
                    urljoin(CAS_BASE_URL, str(local_info["localHttpsAjaxPath"])),
                    label="localHttpsAjaxPath",
                )
                ajax_base = resolved.rstrip("/") + "/ajaxHandler/"

            service = str(local_info.get("service") or iframe_params["service"])
            scope = str(local_info.get("scope") or iframe_params["scope"])

            # 2. SSO Fast-path via jsRemoteLogin if CASTGC exists
            if not skip_js_remote_login and self._has_cas_tgt():
                try:
                    sso_resp = self._cas_post(
                        ajax_base,
                        "login/jsRemoteLogin",
                        {
                            "service": service,
                            "notAuthTypeRemoteLogin": "true",
                        },
                        state,
                    )
                    if str(sso_resp.get("isSuccess")) == "1" and sso_resp.get("callbackURL"):
                        return self._exchange_ticket_and_bootstrap_portal(
                            html.unescape(str(sso_resp["callbackURL"])),
                            local_storage_id=state["local_storage_id"],
                            interactive=interactive,
                            prompt_fn=prompt_fn,
                            output_fn=output_fn,
                        )
                except (TwoFactorRequiredError, CaptchaRequiredError, InvalidCredentialsError):
                    raise
                except Exception as exc:
                    logger.debug("jsRemoteLogin SSO fast-path fell back to password login: %s", exc)

            if not self.username or not self.password:
                raise AuthExpiredError(
                    "未配置 HUAWEI_USERNAME / HUAWEI_PASSWORD，且本地无有效 CASTGC 会话。"
                )

            norm_user = normalize_phone_account(self.username)

            # 3. Pre-flight risk check (`chkRisk`)
            low_login = str(local_info.get("lowLogin") or "false")
            risk_resp = self._cas_post(
                ajax_base,
                "chkRisk",
                {
                    "userAccount": norm_user,
                    "operType": "0",
                    "lowLogin": low_login,
                    "service": service,
                    "scope": scope,
                },
                state,
            )
            if risk_resp.get("extInfo"):
                local_info["extInfo"] = risk_resp["extInfo"]
            if risk_resp.get("lowLogin") is not None:
                low_login = str(risk_resp["lowLogin"])
            site_id = risk_resp.get("siteID") or local_info.get("currentSiteID") or 1

            risk_err = str(risk_resp.get("errorCode") or "")
            if str(risk_resp.get("isNeedImageCode") or "0") == "1" or risk_err in _CAPTCHA_ERROR_CODES:
                raise CaptchaRequiredError(
                    "检测到华为云空间人机滑块/图形验证码（isNeedImageCode=1），按合规规范已停止自动登录；"
                    "请通过浏览器完成一次登录后导出 config/curl.txt。"
                )

            # 4. Password submission (`remoteLogin`)
            login_form: dict[str, Any] = {
                "loginUrl": str(local_info.get("loginUrl") or iframe_params["loginUrl"]),
                "service": service,
                "loginChannel": str(local_info.get("loginChannel") or iframe_params["loginChannel"]),
                "reqClientType": str(local_info.get("reqClientType") or iframe_params["reqClientType"]),
                "lang": str(local_info.get("lang") or iframe_params["lang"]),
                "quickAuth": str(local_info.get("quickAuth") or ""),
                "isThirdBind": str(local_info.get("isThirdBind") or "0"),
                "hwmeta": "",
                "lowLogin": low_login,
                "userAccount": norm_user,
                "password": self.password,
                "clientID": str(local_info.get("clientID") or iframe_params["clientID"]),
                "scope": scope,
            }

            resp = self._cas_post(ajax_base, "remoteLogin", login_form, state)
            self._save_cookie_jar()

            callback_url = ""
            if str(resp.get("isSuccess")) == "1":
                if resp.get("needPopTrust"):
                    try:
                        self._cas_post(
                            ajax_base,
                            "updateTrustBrowser",
                            {
                                "userAccount": norm_user,
                                "operType": "1",
                                "trustBrowser": "1",
                            },
                            state,
                        )
                    except Exception as exc:
                        logger.warning("updateTrustBrowser call failed (non-fatal): %s", exc)
                callback_url = html.unescape(str(resp.get("callbackURL") or ""))
            else:
                err_code = str(resp.get("errorCode") or "")
                err_desc = str(resp.get("errorDesc") or "")
                if err_code in _TWO_FA_ERROR_CODES:
                    callback_url = self._handle_2fa(
                        ajax_base,
                        login_form,
                        resp,
                        state,
                        site_id,
                        interactive=interactive,
                        prompt_fn=prompt_fn,
                        output_fn=output_fn,
                    )
                elif err_code in _CREDENTIAL_ERROR_CODES:
                    raise InvalidCredentialsError(
                        f"华为账号或密码被拒绝 (errorCode={err_code}): {err_desc}"
                    )
                elif err_code in _CAPTCHA_ERROR_CODES:
                    raise CaptchaRequiredError(
                        f"华为云空间触发图形/滑块验证 (errorCode={err_code})，已停止自动登录；"
                        "请通过浏览器完成一次登录后导出 config/curl.txt。"
                    )
                elif err_code in _RATE_LIMIT_ERROR_CODES:
                    raise RateLimitError(f"华为账号登录请求过于频繁 (errorCode={err_code}): {err_desc}")
                else:
                    raise AuthExpiredError(
                        f"华为账号登录未成功 (errorCode={err_code or 'unknown'}): {err_desc}"
                    )

            return self._exchange_ticket_and_bootstrap_portal(
                callback_url or None,
                local_storage_id=state["local_storage_id"],
                interactive=interactive,
                prompt_fn=prompt_fn,
                output_fn=output_fn,
            )
