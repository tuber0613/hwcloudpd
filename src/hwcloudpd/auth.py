"""Session persistence, hot-reload, and Chrome/Safari 'Copy as cURL' importer."""

from __future__ import annotations

from http.cookies import SimpleCookie
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import threading
import time
from typing import Iterable

from hwcloudpd.models import AuthExpiredError, AuthSession

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_MAP = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}

_PRESERVED_FINGERPRINT_HEADERS = {
    "accept-language",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
}


def _decode_ansi_c_body(body: str) -> str:
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_ESCAPE_MAP:
            out.append(_ANSI_ESCAPE_MAP[nxt])
            i += 2
        elif nxt == "x" and i + 3 < n and re.fullmatch(r"[0-9a-fA-F]{2}", body[i + 2 : i + 4]):
            out.append(chr(int(body[i + 2 : i + 4], 16)))
            i += 4
        elif nxt == "u" and i + 5 < n and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2 : i + 6]):
            out.append(chr(int(body[i + 2 : i + 6], 16)))
            i += 6
        elif nxt == "U" and i + 9 < n and re.fullmatch(r"[0-9a-fA-F]{8}", body[i + 2 : i + 10]):
            out.append(chr(int(body[i + 2 : i + 10], 16)))
            i += 10
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def _preprocess_ansi_c_quotes(text: str) -> str:
    """Convert bash $'...' ANSI-C quoted strings into standard single/double-quoted tokens."""
    result: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    in_double = False

    while i < n:
        ch = text[i]
        if in_single:
            result.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            result.append(ch)
            if ch == "\\" and i + 1 < n:
                result.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            if j < n and text[j] == "'":
                decoded = _decode_ansi_c_body(text[i + 2 : j])
                result.append(shlex.quote(decoded))
                i = j + 1
                continue

        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        result.append(ch)
        i += 1

    return "".join(result)


def _parse_cookie_header(cookie_str: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for pair in cookie_str.split(";"):
        part = pair.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def parse_curl_command(
    curl_text: str,
    *,
    fallback_user_id: str = "",
    fallback_device_id: str = "",
) -> AuthSession:
    """Parse a Chrome/Safari/Firefox 'Copy as cURL' command into an AuthSession."""
    cleaned = curl_text.strip()
    if not cleaned:
        raise ValueError("cURL input is empty")

    # Join line continuations used by bash and Windows cmd exports.
    cleaned = re.sub(r"\\\r?\n", " ", cleaned)
    cleaned = re.sub(r"\^\r?\n", " ", cleaned)
    cleaned = _preprocess_ansi_c_quotes(cleaned)

    try:
        tokens = shlex.split(cleaned)
    except ValueError as exc:
        raise ValueError(f"Failed to tokenize cURL command: {exc}") from exc

    if not tokens:
        raise ValueError("cURL command contained no tokens")

    headers_lower: dict[str, str] = {}
    cookies: dict[str, str] = {}

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"-H", "--header"} and i + 1 < len(tokens):
            hdr = tokens[i + 1]
            i += 2
            if ":" in hdr:
                k, v = hdr.split(":", 1)
                key = k.strip().lower()
                val = v.strip()
                if key == "cookie":
                    cookies.update(_parse_cookie_header(val))
                else:
                    headers_lower[key] = val
            continue
        if tok in {"-b", "--cookie"} and i + 1 < len(tokens):
            cookies.update(_parse_cookie_header(tokens[i + 1]))
            i += 2
            continue
        if tok in {"-A", "--user-agent"} and i + 1 < len(tokens):
            headers_lower["user-agent"] = tokens[i + 1].strip()
            i += 2
            continue
        if tok.startswith("--header="):
            hdr = tok.split("=", 1)[1]
            if ":" in hdr:
                k, v = hdr.split(":", 1)
                key = k.strip().lower()
                val = v.strip()
                if key == "cookie":
                    cookies.update(_parse_cookie_header(val))
                else:
                    headers_lower[key] = val
        elif tok.startswith("--cookie="):
            cookies.update(_parse_cookie_header(tok.split("=", 1)[1]))
        i += 1

    if not cookies:
        raise ValueError(
            "No cookies found in cURL command. Please copy a logged-in request to "
            "cloud.huawei.com (such as /album/getSimpleFile or /heartbeatCheck) as cURL (bash)."
        )

    csrf_token = headers_lower.get("csrftoken") or cookies.get("shareToken") or ""
    if csrf_token and "shareToken" not in cookies:
        cookies["shareToken"] = csrf_token
    if not csrf_token:
        raise ValueError(
            "Missing 'CSRFToken' header and 'shareToken' cookie in cURL command."
        )

    user_id = (
        headers_lower.get("userid")
        or fallback_user_id
        or os.environ.get("HWCLOUD_USER_ID", "").strip()
    )
    if not user_id:
        raise ValueError(
            "Missing 'userid' header in cURL command (and HWCLOUD_USER_ID is unset). "
            "Please copy a request under /album/ (e.g. getSimpleFile or galleryStatInfo) as cURL."
        )

    device_id = (
        headers_lower.get("x-hw-device-id")
        or fallback_device_id
        or os.environ.get("HWCLOUD_DEVICE_ID", "").strip()
        or secrets.token_hex(32)
    )
    user_agent = headers_lower.get("user-agent") or AuthSession.user_agent
    extra_headers = {
        k: headers_lower[k]
        for k in _PRESERVED_FINGERPRINT_HEADERS
        if k in headers_lower and headers_lower[k]
    }

    return AuthSession(
        cookies=cookies,
        csrf_token=csrf_token,
        user_id=user_id,
        device_id=device_id,
        user_agent=user_agent,
        extra_headers=extra_headers,
        updated_at=time.time(),
    )


class AuthManager:
    """Thread-safe credential store supporting curl.txt auto-import and auth.json hot-reload."""

    def __init__(self, config_dir: Path | str) -> None:
        self.config_dir = Path(config_dir)
        self.auth_file = self.config_dir / "auth.json"
        self.curl_file = self.config_dir / "curl.txt"
        self._lock = threading.RLock()
        self._session: AuthSession | None = None
        self._last_auth_mtime: float = -1.0
        self._last_curl_mtime: float = -1.0

    def ensure_session(self) -> AuthSession:
        """Return the current AuthSession, reloading if files changed, or raise AuthExpiredError."""
        with self._lock:
            self.load_or_reload()
            if self._session is None or not self._session.cookies or not self._session.user_id:
                raise AuthExpiredError(
                    f"No valid session found in {self.config_dir}. "
                    f"Save a browser 'Copy as cURL' command to {self.curl_file} or run `hwcloudpd --import-curl`."
                )
            return AuthSession.from_dict(self._session.to_dict())

    def get_session_if_present(self) -> AuthSession | None:
        """Return a copy of the loaded session if available, else None."""
        with self._lock:
            self.load_or_reload()
            if self._session is None:
                return None
            return AuthSession.from_dict(self._session.to_dict())

    def load_or_reload(self) -> bool:
        """Check for new curl.txt or updated auth.json. Return True if session changed."""
        with self._lock:
            changed = False
            if self.curl_file.exists():
                try:
                    curl_mtime = self.curl_file.stat().st_mtime
                except OSError:
                    curl_mtime = -1.0
                if curl_mtime > self._last_curl_mtime or not self.auth_file.exists():
                    raw_curl = self.curl_file.read_text(encoding="utf-8", errors="replace")
                    if raw_curl.strip():
                        prev_uid = self._session.user_id if self._session else ""
                        prev_dev = self._session.device_id if self._session else ""
                        session = parse_curl_command(
                            raw_curl,
                            fallback_user_id=prev_uid,
                            fallback_device_id=prev_dev,
                        )
                        self.save(session)
                        imported_path = self.config_dir / "curl.txt.imported"
                        try:
                            os.replace(self.curl_file, imported_path)
                        except OSError as exc:
                            logger.warning("Could not archive %s: %s", self.curl_file, exc)
                        self._last_curl_mtime = curl_mtime
                        logger.info(
                            "Imported credentials from %s into %s (userid=%s)",
                            self.curl_file,
                            self.auth_file,
                            session.user_id,
                        )
                        return True

            if self.auth_file.exists():
                try:
                    auth_mtime = self.auth_file.stat().st_mtime
                except OSError:
                    return False
                if self._session is None or auth_mtime > self._last_auth_mtime:
                    data = json.loads(self.auth_file.read_text(encoding="utf-8"))
                    self._session = AuthSession.from_dict(data)
                    self._last_auth_mtime = auth_mtime
                    changed = True
                    logger.debug("Loaded auth session from %s", self.auth_file)
            return changed

    def import_curl_text(self, curl_text: str) -> AuthSession:
        """Parse and immediately persist a cURL string to auth.json."""
        with self._lock:
            prev_uid = self._session.user_id if self._session else ""
            prev_dev = self._session.device_id if self._session else ""
            session = parse_curl_command(
                curl_text,
                fallback_user_id=prev_uid,
                fallback_device_id=prev_dev,
            )
            self.save(session)
            return AuthSession.from_dict(session.to_dict())

    def save(self, session: AuthSession) -> None:
        """Atomically persist AuthSession to auth.json with 0600 permissions."""
        with self._lock:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            session.updated_at = time.time()
            tmp_path = self.config_dir / f".auth.{os.getpid()}.{threading.get_ident()}.tmp"
            payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n"
            tmp_path.write_text(payload, encoding="utf-8")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, self.auth_file)
            self._session = AuthSession.from_dict(session.to_dict())
            try:
                self._last_auth_mtime = self.auth_file.stat().st_mtime
            except OSError:
                self._last_auth_mtime = time.time()

    def update_from_response(
        self,
        set_cookie_headers: Iterable[str],
        response_headers: dict[str, str],
    ) -> bool:
        """Merge Set-Cookie headers and rotated csrftoken header into persisted auth.json."""
        with self._lock:
            if self._session is None:
                return False

            changed = False
            cookies = dict(self._session.cookies)
            csrf = self._session.csrf_token

            for raw_cookie in set_cookie_headers:
                parsed = SimpleCookie()
                try:
                    parsed.load(raw_cookie)
                except Exception:
                    continue
                for name, morsel in parsed.items():
                    max_age = morsel.get("max-age")
                    if max_age is not None and str(max_age).strip() == "0":
                        if name in cookies:
                            del cookies[name]
                            changed = True
                        continue
                    val = morsel.value
                    if val and cookies.get(name) != val:
                        cookies[name] = val
                        changed = True

            # Check case-insensitive csrftoken header returned by Huawei backend.
            for k, v in response_headers.items():
                if k.lower() == "csrftoken" and v.strip():
                    new_csrf = v.strip()
                    if new_csrf != csrf:
                        csrf = new_csrf
                        changed = True
                    if cookies.get("shareToken") != new_csrf:
                        cookies["shareToken"] = new_csrf
                        changed = True

            if "shareToken" in cookies and cookies["shareToken"] != csrf and not any(
                k.lower() == "csrftoken" and v.strip() for k, v in response_headers.items()
            ):
                csrf = cookies["shareToken"]
                changed = True

            if changed:
                updated = AuthSession(
                    cookies=cookies,
                    csrf_token=csrf,
                    user_id=self._session.user_id,
                    device_id=self._session.device_id,
                    user_agent=self._session.user_agent,
                    extra_headers=dict(self._session.extra_headers),
                    updated_at=time.time(),
                )
                self.save(updated)
                logger.debug("Persisted rotated session cookies/CSRFToken to %s", self.auth_file)
            return changed
