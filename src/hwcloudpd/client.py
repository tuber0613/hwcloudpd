"""HTTP transport and Huawei Cloud Space Gallery API client."""

from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import hashlib
import json
import logging
from pathlib import Path
import random
import ssl
import threading
import time
from typing import Any, Iterator, Protocol
import urllib.error
from urllib.parse import unquote, urlparse
import urllib.request
import zlib

from hwcloudpd.auth import AuthManager
from hwcloudpd.models import (
    AlbumInfo,
    AuthExpiredError,
    AuthSession,
    HwCloudError,
    MediaItem,
    MediaStub,
    RateLimitError,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://cloud.huawei.com"

TRACE_OTHER_ID = "04100"
TRACE_QUERY_ID = "04101"
TRACE_DELETE_ID = "04102"
TRACE_GALLERY_ID = "04113"
TRACE_BURST_ID = "04116"
TRACE_SIMPLE_ID = "04118"
TRACE_HEARTBEAT_ID = "07100"

VIRTUAL_ALBUM_TYPE_MAP = {
    "default-album-all-photos": 1,
    "default-album-virtual-video": 2,
    "default-album-virtual-favorite": 3,
    "default-album-3": 4,
}

SYSTEM_ALBUM_NAMES = {
    "default-album-1": "相机",
    "default-album-2": "截屏录屏",
    "default-album-3": "最近删除",
    "default-album-all-photos": "所有照片",
    "default-album-virtual-video": "所有视频",
    "default-album-virtual-favorite": "我的收藏",
}

SIZE_TO_API_TYPE = {
    "original": "0",
    "thumb": "1",
    "medium": "2",
}


def generate_trace_id(module_id: str) -> str:
    """Generate a Huawei Web traceId matching frontend module 198367."""
    rand8 = "".join(str(random.randint(1, 9)) for _ in range(8))
    return f"{module_id}_02_{int(time.time())}_{rand8}"


@dataclass
class HttpResponse:
    """Normalized HTTP response returned by HttpTransport."""

    status: int
    headers: dict[str, str]
    set_cookies: list[str]
    body: bytes


@dataclass
class DownloadResult:
    """Result metadata from streaming a binary file download."""

    bytes_written: int
    sha256_hex: str
    md5_hex: str
    content_disposition_filename: str | None = None


class HttpTransport(Protocol):
    """Abstract HTTP transport allowing UrllibTransport in prod and MockTransport in tests."""

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
        ...

    def stream_download(
        self,
        url: str,
        headers: dict[str, str],
        part_path: Path,
        *,
        resume_from: int = 0,
        timeout: float = 120.0,
    ) -> DownloadResult:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class _SafeCrossOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and not _is_cloud_huawei_host(newurl):
            for key in list(new_req.headers):
                if key.lower() == "cookie":
                    new_req.headers.pop(key, None)
            for key in list(new_req.unredirected_hdrs):
                if key.lower() == "cookie":
                    new_req.unredirected_hdrs.pop(key, None)
        return new_req


def _decode_compressed_body(body: bytes, encoding: str) -> bytes:
    enc = encoding.strip().lower()
    if not body:
        return body
    if "gzip" in enc:
        return gzip.decompress(body)
    if "deflate" in enc:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _parse_content_disposition_filename(header_val: str | None) -> str | None:
    if not header_val:
        return None
    # Match RFC 5987 filename*=utf-8''...
    lower = header_val.lower()
    marker = "filename*="
    if marker in lower:
        idx = lower.index(marker) + len(marker)
        part = header_val[idx:].split(";", 1)[0].strip().strip('"')
        if "''" in part:
            part = part.split("''", 1)[1]
        decoded = unquote(part)
        if decoded:
            return Path(decoded).name
    marker_plain = "filename="
    if marker_plain in lower:
        idx = lower.index(marker_plain) + len(marker_plain)
        part = header_val[idx:].split(";", 1)[0].strip().strip('"')
        if part:
            return Path(unquote(part)).name
    return None


def _hash_existing_file(path: Path) -> tuple[Any, Any, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            sha.update(chunk)
            md5.update(chunk)
            total += len(chunk)
    return sha, md5, total


class UrllibTransport:
    """Pure-stdlib HTTP transport built on urllib.request."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_ctx = ssl_context or ssl.create_default_context()
        self._no_redirect_opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_ctx),
            _NoRedirectHandler(),
        )
        self._redirect_opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_ctx),
            _SafeCrossOriginRedirectHandler(),
        )

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
        req = urllib.request.Request(url=url, data=body, headers=headers, method=method.upper())
        opener = self._redirect_opener if follow_redirects else self._no_redirect_opener
        try:
            with opener.open(req, timeout=timeout) as resp:
                resp_headers = {k: v for k, v in resp.headers.items()}
                set_cookies = resp.headers.get_all("Set-Cookie") or []
                raw = resp.read()
                decoded = _decode_compressed_body(raw, resp.headers.get("Content-Encoding", ""))
                return HttpResponse(
                    status=resp.status,
                    headers=resp_headers,
                    set_cookies=set_cookies,
                    body=decoded,
                )
        except urllib.error.HTTPError as err:
            resp_headers = {k: v for k, v in err.headers.items()} if err.headers else {}
            set_cookies = (err.headers.get_all("Set-Cookie") or []) if err.headers else []
            raw = err.read() if err.fp is not None else b""
            decoded = _decode_compressed_body(
                raw, err.headers.get("Content-Encoding", "") if err.headers else ""
            )
            return HttpResponse(
                status=err.code,
                headers=resp_headers,
                set_cookies=set_cookies,
                body=decoded,
            )

    def stream_download(
        self,
        url: str,
        headers: dict[str, str],
        part_path: Path,
        *,
        resume_from: int = 0,
        timeout: float = 120.0,
    ) -> DownloadResult:
        req_headers = dict(headers)
        if resume_from > 0:
            req_headers["Range"] = f"bytes={resume_from}-"

        req = urllib.request.Request(url=url, headers=req_headers, method="GET")
        part_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._redirect_opener.open(req, timeout=timeout) as resp:
                status = resp.status
                cd_name = _parse_content_disposition_filename(
                    resp.headers.get("Content-Disposition")
                )
                if resume_from > 0 and status == 206 and part_path.exists():
                    sha, md5, total = _hash_existing_file(part_path)
                    mode = "ab"
                else:
                    sha = hashlib.sha256()
                    md5 = hashlib.md5()
                    total = 0
                    mode = "wb"

                with part_path.open(mode) as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        sha.update(chunk)
                        md5.update(chunk)
                        total += len(chunk)
                return DownloadResult(
                    bytes_written=total,
                    sha256_hex=sha.hexdigest(),
                    md5_hex=md5.hexdigest(),
                    content_disposition_filename=cd_name,
                )
        except urllib.error.HTTPError as err:
            # Handle 416 Range Not Satisfiable by discarding stale .hwpart and retrying fresh.
            if err.code == 416 and resume_from > 0:
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return self.stream_download(
                    url, headers, part_path, resume_from=0, timeout=timeout
                )
            raise


def _is_cloud_huawei_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "cloud.huawei.com" or host.endswith(".cloud.huawei.com")


def _is_auth_redirect_location(location: str) -> bool:
    low = location.lower()
    return any(
        marker in low
        for marker in ("id1.cloud.huawei.com", "oauth-login", "cas/login", "/login", "signin")
    )


def _response_code_str(data: dict[str, Any]) -> str:
    if "code" in data and data["code"] is not None:
        return str(data["code"])
    if "resultCode" in data and data["resultCode"] is not None:
        return str(data["resultCode"])
    return ""


@dataclass
class CloudDeleteResult:
    """Per-item outcome of a Recycle Bin move request.

    * `deleted`: confirmed moved to the Recycle Bin.
    * `failed`: definitely NOT moved (rejected by the server, skipped, or never attempted).
    * `unknown`: request outcome unknown (network error mid-request) - must be reconciled later.
    """

    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    aborted_error: Exception | None = None


_SHARED_LIST_KEYS = {"shareAlbumList", "sharedAlbumList", "shareAlbumInfoList"}
_KNOWN_LIST_KEYS = {"ownAlbumList", "albumList", "albumInfoList", "normalAlbumList"} | _SHARED_LIST_KEYS


def _media_ext_for_type(file_type: str) -> str:
    if file_type == "4":
        return ".mp4"
    if file_type == "9":
        return ".heic"
    return ".jpg"


class HuaweiGalleryClient:
    """Protocol client implementing Huawei Cloud Space Gallery Web APIs."""

    def __init__(
        self,
        auth_manager: AuthManager,
        transport: HttpTransport | None = None,
        *,
        base_url: str = BASE_URL,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
    ) -> None:
        self.auth_manager = auth_manager
        self.transport: HttpTransport = transport or UrllibTransport()
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._preprocess_lock = threading.Lock()
        self._preprocess_at: float = 0.0
        self.preprocess_ttl_sec: float = 600.0

    def _probe_session(self) -> AuthExpiredError | None:
        """Return an AuthExpiredError if a heartbeat proves the session is dead, else None."""
        try:
            self.heartbeat_check()
        except AuthExpiredError as exc:
            return exc
        except HwCloudError as exc:
            logger.debug("Session probe inconclusive: %s", exc)
        return None

    def _ensure_ok(self, data: dict[str, Any], what: str, *, allow_empty_code: bool = False) -> None:
        """Validate a business response code; distinguish expired sessions from other errors.

        Huawei endpoints often answer HTTP 200 with a non-zero business code when the session
        expired. Error codes are undocumented, so instead of guessing we probe /heartbeatCheck:
        if the heartbeat fails the caller gets AuthExpiredError (-> re-login flow), otherwise a
        plain HwCloudError.
        """
        code = _response_code_str(data)
        if code == "0" or (allow_empty_code and code == ""):
            return
        expired = self._probe_session()
        if expired is not None:
            raise AuthExpiredError(f"{what} failed (code={code!r}); session expired: {expired}") from expired
        raise HwCloudError(f"{what} failed: {data}")

    def _build_api_headers(
        self,
        session: AuthSession,
        trace_id: str,
        *,
        is_json: bool = True,
    ) -> dict[str, str]:
        cookie_str = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
        csrf = session.csrf_token or session.cookies.get("shareToken", "")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/home",
            "User-Agent": session.user_agent,
            "Cookie": cookie_str,
            "CSRFToken": csrf,
            "userid": session.user_id,
            "x-hw-trace-id": trace_id,
            "x-hw-device-category": "Web",
            "x-hw-device-type": "7",
            "x-hw-client-mode": "frontend",
            "x-hw-os-brand": "Web",
            "x-hw-device-brand": "HUAWEI",
            "x-hw-device-manufacturer": "HUAWEI",
            "x-hw-device-id": session.device_id,
            "x-hw-account-brand-id": "0",
            "x-hw-app-brand-id": "1",
        }
        if is_json:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        for k, v in session.extra_headers.items():
            headers.setdefault(k, v)
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        trace_module: str,
        payload: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        inject_trace_into_payload: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            url = f"{url}{'&' if '?' in url else '?'}{qs}"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            session = self.auth_manager.ensure_session()
            trace_id = generate_trace_id(trace_module)
            headers = self._build_api_headers(session, trace_id, is_json=(method.upper() != "GET"))

            body_bytes: bytes | None = None
            if payload is not None:
                body_obj = dict(payload)
                if inject_trace_into_payload and "traceId" not in body_obj:
                    body_obj["traceId"] = trace_id
                body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

            try:
                resp = self.transport.request(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body_bytes,
                    follow_redirects=False,
                    timeout=30.0,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise HwCloudError(f"Network error calling {path}: {exc}") from exc

            # Always merge Set-Cookie / csrftoken rotation before inspecting errors.
            self.auth_manager.update_from_response(resp.set_cookies, resp.headers)

            if resp.status in (301, 302, 303, 307, 308):
                loc = next(
                    (v for k, v in resp.headers.items() if k.lower() == "location"),
                    "",
                )
                if _is_auth_redirect_location(loc) or not loc:
                    raise AuthExpiredError(f"Session redirected to login ({loc or resp.status})")
                raise HwCloudError(f"Unexpected redirect {resp.status} from {path} to {loc}")

            if resp.status in (401, 402):
                raise AuthExpiredError(
                    f"HTTP {resp.status} from {path}: session expired or gallery locked (requires loginSecLevel=2)"
                )

            if resp.status == 403:
                # 403 is also used by the WAF / anti-abuse layer; only treat it as an expired
                # session when an independent heartbeat confirms it.
                if path == "/heartbeatCheck":
                    raise AuthExpiredError(f"HTTP 403 from {path}: session expired")
                expired = self._probe_session()
                if expired is not None:
                    raise AuthExpiredError(f"HTTP 403 from {path}: {expired}") from expired
                last_exc = RateLimitError(
                    f"HTTP 403 from {path} while session is still valid (WAF / rate limiting?)"
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt) * 4 + random.uniform(0.1, 0.5))
                    continue
                raise last_exc

            if resp.status == 429:
                last_exc = RateLimitError(f"HTTP 429 rate limited on {path}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt) + random.uniform(0.1, 0.5))
                    continue
                raise last_exc

            if resp.status >= 500:
                last_exc = HwCloudError(f"HTTP {resp.status} server error on {path}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise last_exc

            if resp.status >= 400:
                raise HwCloudError(
                    f"HTTP {resp.status} from {path}: {resp.body.decode('utf-8', errors='replace')[:300]}"
                )

            text = resp.body.decode("utf-8", errors="replace").strip()
            if not text:
                raise HwCloudError(f"Empty response body from {path}")
            if text.lstrip().startswith("<"):
                raise AuthExpiredError(f"Received HTML instead of JSON from {path} (session expired)")

            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HwCloudError(f"Invalid JSON from {path}: {exc}") from exc

            if not isinstance(data, dict):
                raise HwCloudError(f"Unexpected non-object JSON from {path}")
            return data

        assert last_exc is not None
        raise last_exc

    def heartbeat_check(self) -> bool:
        """Verify session liveness (and unlock level) via GET /heartbeatCheck (module 07100)."""
        trace_id = generate_trace_id(TRACE_HEARTBEAT_ID)
        data = self._request_json(
            "GET",
            "/heartbeatCheck",
            trace_module=TRACE_HEARTBEAT_ID,
            query_params={"checkType": 1, "traceId": trace_id},
        )
        code = _response_code_str(data)
        if code != "0":
            raise AuthExpiredError(
                f"Heartbeat returned non-zero code={code!r}, info={data.get('info')!r}"
            )
        session = self.auth_manager.ensure_session()
        sec_level = session.cookies.get("loginSecLevel")
        if sec_level is not None and sec_level != "2":
            raise AuthExpiredError(
                f"Gallery session locked (loginSecLevel={sec_level!r}, requires '2')"
            )
        return True

    def get_gallery_stats(self) -> dict[str, int]:
        """Return total photo and video counts via POST /album/galleryStatInfo."""
        data = self._request_json(
            "POST",
            "/album/galleryStatInfo",
            trace_module=TRACE_GALLERY_ID,
            payload={},
        )
        self._ensure_ok(data, "galleryStatInfo")
        return {
            "photoNum": int(data.get("photoNum") or 0),
            "videoNum": int(data.get("videoNum") or 0),
            "totalNum": int(data.get("totalNum") or 0),
        }

    def list_albums(self, *, include_stats: bool = False) -> list[AlbumInfo]:
        """Discover system and user albums via POST /album/queryAlbumInfo."""
        data = self._request_json(
            "POST",
            "/album/queryAlbumInfo",
            trace_module=TRACE_GALLERY_ID,
            payload={"isHash": False, "language": "zh_cn"},
        )
        self._ensure_ok(data, "queryAlbumInfo", allow_empty_code=True)

        session = self.auth_manager.get_session_if_present()
        self_user_id = session.user_id if session else ""

        albums_by_id: dict[str, AlbumInfo] = {
            "default-album-1": AlbumInfo(album_id="default-album-1", name="相机"),
            "default-album-2": AlbumInfo(album_id="default-album-2", name="截屏录屏"),
            "default-album-3": AlbumInfo(
                album_id="default-album-3",
                name="最近删除",
                is_virtual=True,
                is_recycle=True,
            ),
        }

        def _ingest_album_list(items: Any, *, shared: bool = False) -> None:
            if not isinstance(items, list):
                return
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                aid = str(entry.get("albumId") or "").strip()
                if not aid:
                    continue
                raw_name = (
                    entry.get("albumName")
                    or entry.get("name")
                    or SYSTEM_ALBUM_NAMES.get(aid)
                    or ""
                )
                if not raw_name and entry.get("lpath"):
                    raw_name = Path(str(entry["lpath"])).name
                name = str(raw_name or aid).strip()
                if aid in SYSTEM_ALBUM_NAMES and (not raw_name or raw_name == aid):
                    name = SYSTEM_ALBUM_NAMES[aid]
                owner = entry.get("ownerId") or entry.get("createrId")
                owner_str = str(owner) if owner else None
                # Albums owned by someone else are shared albums even if listed elsewhere.
                is_shared = shared or bool(
                    owner_str and self_user_id and owner_str != self_user_id
                )
                albums_by_id[aid] = AlbumInfo(
                    album_id=aid,
                    name=name,
                    album_version=str(entry.get("albumVersion") or ""),
                    owner_id=owner_str,
                    img_num=int(entry.get("imgNum") or entry.get("photoNum") or 0),
                    video_num=int(entry.get("videoNum") or 0),
                    hide=int(entry.get("hide") or 0),
                    is_virtual=aid in VIRTUAL_ALBUM_TYPE_MAP,
                    is_recycle=(aid == "default-album-3"),
                    is_shared=is_shared,
                )

        # Extract from known keys as well as any list containing album objects.
        for key in ("ownAlbumList", "albumList", "albumInfoList", "normalAlbumList"):
            if key in data:
                _ingest_album_list(data.get(key))
        for key, val in data.items():
            if key in _KNOWN_LIST_KEYS and key not in _SHARED_LIST_KEYS:
                continue
            if isinstance(val, list) and val and isinstance(val[0], dict) and "albumId" in val[0]:
                _ingest_album_list(val, shared=(key in _SHARED_LIST_KEYS or "share" in key.lower()))

        if include_stats:
            query_ids = [aid for aid in albums_by_id if aid not in VIRTUAL_ALBUM_TYPE_MAP]
            for i in range(0, len(query_ids), 100):
                batch_ids = query_ids[i : i + 100]
                if not batch_ids:
                    continue
                try:
                    stat_data = self._request_json(
                        "POST",
                        "/album/galleryAlbumStatInfo",
                        trace_module=TRACE_QUERY_ID,
                        payload={"albumList": batch_ids},
                    )
                except AuthExpiredError:
                    raise
                except HwCloudError as exc:
                    logger.debug("galleryAlbumStatInfo fallback skipped: %s", exc)
                    break
                if _response_code_str(stat_data) not in {"", "0"}:
                    logger.debug("galleryAlbumStatInfo returned non-zero code: %s", stat_data)
                    break
                for st in stat_data.get("albumStatInfoList") or []:
                    aid = str(st.get("albumId") or "")
                    if aid in albums_by_id:
                        existing = albums_by_id[aid]
                        raw_img = st.get("imgNum") if st.get("imgNum") is not None else st.get("photoNum")
                        raw_vid = st.get("videoNum")
                        img_num = (
                            int(raw_img)
                            if raw_img not in (None, "") and int(raw_img) > 0
                            else existing.img_num
                        )
                        video_num = (
                            int(raw_vid)
                            if raw_vid not in (None, "") and int(raw_vid) > 0
                            else existing.video_num
                        )
                        albums_by_id[aid] = AlbumInfo(
                            album_id=existing.album_id,
                            name=existing.name,
                            album_version=existing.album_version,
                            owner_id=existing.owner_id,
                            img_num=img_num,
                            video_num=video_num,
                            hide=int(st.get("hide") or existing.hide),
                            is_virtual=existing.is_virtual,
                            is_recycle=existing.is_recycle,
                            is_shared=existing.is_shared,
                        )

        return list(albums_by_id.values())

    def iter_simple_files(
        self,
        album_id: str | None,
        *,
        album_name: str = "",
        album_version: str = "",
        owner_id: str | None = None,
        query_type: int | None = None,
        page_size: int = 100,
        request_delay_ms: int = 0,
    ) -> Iterator[list[MediaStub]]:
        """Yield pages of MediaStub items from POST /album/getSimpleFile."""
        resolved_type = query_type
        resolved_album_id = album_id
        if resolved_type is None and album_id in VIRTUAL_ALBUM_TYPE_MAP:
            resolved_type = VIRTUAL_ALBUM_TYPE_MAP[album_id]
            resolved_album_id = None

        offset = 0
        while True:
            payload = {
                "albumId": resolved_album_id,
                "currentNum": offset,
                "count": page_size,
                "type": resolved_type,
            }
            data = self._request_json(
                "POST",
                "/album/getSimpleFile",
                trace_module=TRACE_SIMPLE_ID,
                payload=payload,
            )
            self._ensure_ok(data, f"getSimpleFile(album={album_id})")

            raw_list = data.get("fileList") or []
            if not raw_list:
                break

            stubs: list[MediaStub] = []
            for entry in raw_list:
                uid = str(entry.get("uniqueId") or "").strip()
                if not uid:
                    continue
                item_aid = str(entry.get("albumId") or album_id or "")
                stubs.append(
                    MediaStub(
                        unique_id=uid,
                        album_id=item_aid,
                        album_name=album_name or SYSTEM_ALBUM_NAMES.get(item_aid, item_aid),
                        file_type=str(entry.get("fileType") or "1"),
                        create_time_ms=int(entry.get("createTime") or 0),
                        is_cover=int(entry.get("isCover") or 0),
                        is_fav=int(entry.get("isFav") or 0),
                        uid=str(entry.get("uid") or ""),
                        file_name=str(entry.get("fileName") or ""),
                        album_version=album_version,
                        owner_id=owner_id,
                        recycle_time=str(entry.get("recycleTime") or ""),
                    )
                )

            if stubs:
                yield stubs

            has_more = bool(data.get("hasMore"))
            if not has_more:
                break
            offset += len(raw_list)
            if request_delay_ms > 0:
                jitter = random.uniform(0.0, request_delay_ms * 0.3)
                time.sleep((request_delay_ms + jitter) / 1000.0)

    def get_burst_files(
        self,
        album_id: str,
        unique_id: str,
        *,
        album_name: str = "",
        album_version: str = "",
        owner_id: str | None = None,
    ) -> list[MediaStub]:
        """Expand a burst stack cover item via POST /album/burstphotolist."""
        data = self._request_json(
            "POST",
            "/album/burstphotolist",
            trace_module=TRACE_BURST_ID,
            payload={"albumId": album_id, "uniqueId": unique_id},
        )
        self._ensure_ok(data, f"burstphotolist({unique_id})")

        bursts: list[MediaStub] = []
        for entry in data.get("burstIdList") or []:
            uid = str(entry.get("uniqueId") or "").strip()
            if not uid:
                continue
            item_aid = str(entry.get("albumId") or album_id)
            bursts.append(
                MediaStub(
                    unique_id=uid,
                    album_id=item_aid,
                    album_name=album_name or SYSTEM_ALBUM_NAMES.get(item_aid, item_aid),
                    file_type=str(entry.get("fileType") or "1"),
                    create_time_ms=int(entry.get("createTime") or 0),
                    is_cover=0,
                    is_fav=int(entry.get("isFav") or 0),
                    uid=str(entry.get("uid") or ""),
                    file_name=str(entry.get("fileName") or ""),
                    album_version=album_version,
                    owner_id=owner_id,
                )
            )
        return bursts

    def enrich_metadata(self, stubs: list[MediaStub]) -> list[MediaItem]:
        """Enrich MediaStubs with fileName, fileSize, and sha256/hash via queryCloudFileName."""
        if not stubs:
            return []

        enriched: dict[str, dict[str, Any]] = {}
        # Group by albumVersion so payload matches Huawei web client module 589576/81158.
        by_version: dict[str, list[MediaStub]] = {}
        for s in stubs:
            by_version.setdefault(s.album_version or "", []).append(s)

        for album_ver, group in by_version.items():
            for i in range(0, len(group), 500):
                batch = group[i : i + 500]
                file_list_req = [
                    {"albumId": s.album_id, "uniqueId": s.unique_id}
                    for s in batch
                ]
                payload: dict[str, Any] = {"fileList": file_list_req}
                if album_ver:
                    payload["albumVersion"] = album_ver
                data = self._request_json(
                    "POST",
                    "/album/queryCloudFileName",
                    trace_module=TRACE_QUERY_ID,
                    payload=payload,
                )
                self._ensure_ok(data, "queryCloudFileName")
                rows = data.get("fileList") or data.get("successList") or []
                for row in rows:
                    uid = str(row.get("uniqueId") or "").strip()
                    if uid:
                        enriched[uid] = row

        # Fallback via getThumbLcdUrl for any items still lacking fileName.
        missing_stubs = [
            s
            for s in stubs
            if not (
                (enriched.get(s.unique_id, {}).get("fileName"))
                or (enriched.get(s.unique_id, {}).get("name"))
                or s.file_name
            )
        ]
        for i in range(0, len(missing_stubs), 100):
            batch = missing_stubs[i : i + 100]
            try:
                lcd_data = self._request_json(
                    "POST",
                    "/album/getThumbLcdUrl",
                    trace_module=TRACE_QUERY_ID,
                    payload={
                        "fileList": [
                            {"albumId": s.album_id, "uniqueId": s.unique_id}
                            for s in batch
                        ],
                        "type": "1",
                    },
                )
            except HwCloudError as exc:
                logger.debug("getThumbLcdUrl fallback failed: %s", exc)
                continue

            for row in lcd_data.get("successList") or []:
                uid = str(row.get("uniqueId") or row.get("fileId") or "").strip()
                if not uid:
                    file_url = str(row.get("fileUrl") or "")
                    for candidate in batch:
                        if candidate.unique_id in file_url:
                            uid = candidate.unique_id
                            break
                if not uid and len(lcd_data.get("successList") or []) == 1 and len(batch) == 1:
                    uid = batch[0].unique_id
                if uid:
                    existing = enriched.setdefault(uid, {})
                    if not existing.get("fileName") and row.get("fileName"):
                        existing["fileName"] = row["fileName"]
                    if not existing.get("expand") and row.get("expand"):
                        existing["expand"] = row["expand"]
                    if not existing.get("createTime") and row.get("createTime"):
                        existing["createTime"] = row["createTime"]

        items: list[MediaItem] = []
        for s in stubs:
            meta = enriched.get(s.unique_id, {})
            file_name = str(meta.get("fileName") or meta.get("name") or s.file_name or "").strip()
            file_type = str(meta.get("fileType") or s.file_type or "1")
            name_synthesized = False
            if not file_name:
                prefix = "VID" if file_type == "4" else "IMG"
                file_name = f"{prefix}_{s.unique_id[:12]}{_media_ext_for_type(file_type)}"
                name_synthesized = True

            raw_size = meta.get("fileSize")
            if raw_size is None:
                raw_size = meta.get("size")
            file_size = int(raw_size or 0)

            sha256_val = str(meta.get("sha256") or "").strip().lower()
            hash_val = str(meta.get("hash") or "").strip().lower()
            checksum = sha256_val if sha256_val else (hash_val if len(hash_val) == 32 else "")
            create_time_ms = int(meta.get("createTime") or s.create_time_ms or 0)
            expand = meta.get("expand") if isinstance(meta.get("expand"), dict) else {}

            items.append(
                MediaItem(
                    unique_id=s.unique_id,
                    album_id=s.album_id,
                    album_name=s.album_name,
                    file_type=file_type,
                    create_time_ms=create_time_ms,
                    is_cover=s.is_cover,
                    is_fav=s.is_fav,
                    uid=s.uid,
                    file_name=Path(file_name).name,
                    file_size=file_size,
                    checksum=checksum,
                    album_version=s.album_version,
                    owner_id=s.owner_id,
                    expand=expand,
                    is_shared=s.is_shared,
                    name_synthesized=name_synthesized,
                )
            )
        return items

    def pre_process(self, *, force: bool = False) -> None:
        """Warm up download signing session via POST /proxyserver/driveFileProxy/preProcess.

        The warm-up is cached for `preprocess_ttl_sec` so per-item callers don't add one extra
        request per download.
        """
        with self._preprocess_lock:
            if not force and (time.monotonic() - self._preprocess_at) < self.preprocess_ttl_sec and self._preprocess_at > 0:
                return
            data = self._request_json(
                "POST",
                "/proxyserver/driveFileProxy/preProcess",
                trace_module=TRACE_QUERY_ID,
                payload={
                    "needToSignUrl": "",
                    "httpMethod": "GET",
                    "generateSignFlag": False,
                },
                inject_trace_into_payload=False,
            )
            self._ensure_ok(data, "preProcess")
            self._preprocess_at = time.monotonic()

    def get_download_url(self, item: MediaItem, size_variant: str = "original") -> str:
        """Exchange item identity for a pre-signed download URL via POST /album/getSingleUrl."""
        if item.album_version == "2.0":
            self.pre_process()

        api_type = SIZE_TO_API_TYPE.get(size_variant, "0")
        payload: dict[str, Any] = {
            "fileList": [
                {
                    "albumId": item.album_id,
                    "uniqueId": item.unique_id,
                    "fileName": item.file_name,
                }
            ],
            "type": api_type,
        }
        if item.owner_id:
            payload["ownerId"] = item.owner_id
        if item.album_version:
            payload["albumVersion"] = item.album_version

        data = self._request_json(
            "POST",
            "/album/getSingleUrl",
            trace_module=TRACE_QUERY_ID,
            payload=payload,
        )
        self._ensure_ok(data, f"getSingleUrl({item.unique_id})")

        url_list = data.get("urlList") or []
        if url_list and isinstance(url_list[0], dict) and url_list[0].get("url"):
            return str(url_list[0]["url"])
        if data.get("url"):
            return str(data["url"])
        raise HwCloudError(f"getSingleUrl returned no download URL for {item.unique_id}: {data}")

    def download_to_part(self, url: str, part_path: Path) -> DownloadResult:
        """Stream binary file into `part_path`, supporting Range resume without leaking cookies."""
        session = self.auth_manager.ensure_session()
        headers = {
            "User-Agent": session.user_agent,
            "Accept": "*/*",
            "Referer": f"{self.base_url}/",
        }
        if _is_cloud_huawei_host(url):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
            headers["Referer"] = f"{self.base_url}/home"

        resume_from = 0
        if part_path.exists():
            try:
                resume_from = part_path.stat().st_size
            except OSError:
                resume_from = 0

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.transport.stream_download(
                    url, headers, part_path, resume_from=resume_from, timeout=180.0
                )
            except urllib.error.HTTPError as err:
                if err.code in (401, 403):
                    raise AuthExpiredError(
                        f"Download URL unauthorized/expired (HTTP {err.code})"
                    ) from err
                last_exc = err
                if err.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt))
                    resume_from = part_path.stat().st_size if part_path.exists() else 0
                    continue
                raise HwCloudError(f"Binary download failed (HTTP {err.code}): {err}") from err
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt))
                    resume_from = part_path.stat().st_size if part_path.exists() else 0
                    continue
                raise HwCloudError(f"Binary download network error: {exc}") from exc

        assert last_exc is not None
        raise last_exc

    def delete_cloud_files(self, items: list[MediaItem]) -> CloudDeleteResult:
        """Move items to the Huawei Cloud Recycle Bin via POST /album/deleteAlbumFile.

        Safety rules:
        * Items in shared albums (or owned by someone else) are never touched.
        * `recycle` is always "1" (move to Recycle Bin, never hard-delete).
        * The result reports confirmed / rejected / unknown items separately so the caller can
          keep its state database exact even after partial failures.
        """
        result = CloudDeleteResult()
        if not items:
            return result

        grouped: dict[tuple[str, str | None, str], list[str]] = {}
        for item in items:
            if item.is_shared:
                logger.warning(
                    "Refusing to delete shared-album item %s (%s) from the cloud",
                    item.file_name,
                    item.unique_id,
                )
                result.failed.append(item.unique_id)
                continue
            key = (item.album_id, item.owner_id, item.album_version or "")
            grouped.setdefault(key, []).append(item.unique_id)

        batches: list[tuple[tuple[str, str | None, str], list[str]]] = []
        for key, unique_ids in grouped.items():
            for i in range(0, len(unique_ids), 200):
                batches.append((key, unique_ids[i : i + 200]))

        for idx, ((album_id, owner_id, album_ver), batch_ids) in enumerate(batches):
            payload: dict[str, Any] = {
                "albumId": album_id,
                "recycle": "1",
                "hash": [""],
                "uniqueId": batch_ids,
            }
            if album_ver:
                payload["albumVersion"] = album_ver
            if owner_id:
                payload["ownerId"] = owner_id
            try:
                data = self._request_json(
                    "POST",
                    "/album/deleteAlbumFile",
                    trace_module=TRACE_DELETE_ID,
                    payload=payload,
                )
            except AuthExpiredError as exc:
                # A 401/redirect means the request was rejected, but be conservative: the current
                # batch is "unknown", later batches were never sent and are definitely intact.
                result.unknown.extend(batch_ids)
                for _key, rest in batches[idx + 1 :]:
                    result.failed.extend(rest)
                result.aborted_error = exc
                return result
            except HwCloudError as exc:
                logger.error("deleteAlbumFile request failed (outcome unknown): %s", exc)
                result.unknown.extend(batch_ids)
                continue

            if _response_code_str(data) != "0":
                logger.error("deleteAlbumFile rejected batch for album %s: %s", album_id, data)
                result.failed.extend(batch_ids)
                continue

            success_list = data.get("successList")
            if isinstance(success_list, list):
                ok: set[str] = set()
                for entry in success_list:
                    if isinstance(entry, dict):
                        uid = str(entry.get("uniqueId") or "").strip()
                    else:
                        uid = str(entry or "").strip()
                    if uid:
                        ok.add(uid)
                for uid in batch_ids:
                    (result.deleted if uid in ok else result.failed).append(uid)
            else:
                result.deleted.extend(batch_ids)
        return result
