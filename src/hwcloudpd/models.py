"""Data models, configuration dataclasses, and domain exceptions for hwcloudpd."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time


class HwCloudError(Exception):
    """Base exception for Huawei Cloud Gallery operations."""


class AuthExpiredError(HwCloudError):
    """Raised when browser session cookies or CSRFToken have expired or are missing."""


class TwoFactorRequiredError(AuthExpiredError):
    """Raised during non-interactive login when Huawei ID requires a 2FA verification code."""


class CaptchaRequiredError(AuthExpiredError):
    """Raised when Huawei ID presents a slider/image CAPTCHA challenge."""


class InvalidCredentialsError(HwCloudError):
    """Raised when Huawei ID rejects the configured username or password."""


class RateLimitError(HwCloudError):
    """Raised when Huawei Cloud endpoints throttle requests (HTTP 429 or busy)."""


@dataclass
class AuthSession:
    """Persisted browser session credentials and client fingerprint for cloud.huawei.com."""

    cookies: dict[str, str] = field(default_factory=dict)
    csrf_token: str = ""
    user_id: str = ""
    device_id: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    extra_headers: dict[str, str] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialize session credentials into JSON-compatible dict."""
        return {
            "cookies": dict(self.cookies),
            "csrf_token": self.csrf_token,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "user_agent": self.user_agent,
            "extra_headers": dict(self.extra_headers),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AuthSession:
        """Deserialize session credentials from JSON dict."""
        cookies = {
            str(k): str(v)
            for k, v in (data.get("cookies") or {}).items()
            if k
        }
        csrf = str(data.get("csrf_token") or cookies.get("shareToken") or "")
        if csrf and "shareToken" not in cookies:
            cookies["shareToken"] = csrf
        return cls(
            cookies=cookies,
            csrf_token=csrf,
            user_id=str(data.get("user_id") or ""),
            device_id=str(data.get("device_id") or ""),
            user_agent=str(data.get("user_agent") or cls.user_agent),
            extra_headers={
                str(k): str(v)
                for k, v in (data.get("extra_headers") or {}).items()
                if k
            },
            updated_at=float(data.get("updated_at") or time.time()),
        )


@dataclass(frozen=True)
class AlbumInfo:
    """Metadata describing a Huawei Cloud Gallery album."""

    album_id: str
    name: str
    album_version: str = ""
    owner_id: str | None = None
    img_num: int = 0
    video_num: int = 0
    hide: int = 0
    is_virtual: bool = False
    is_recycle: bool = False


@dataclass
class MediaStub:
    """Lightweight media record returned by `/album/getSimpleFile` or `/album/burstphotolist`."""

    unique_id: str
    album_id: str
    album_name: str
    file_type: str  # "1" = photo, "4" = video, "5" = motion/live photo, "9" = heic photo
    create_time_ms: int
    is_cover: int = 0
    is_fav: int = 0
    uid: str = ""
    file_name: str = ""
    album_version: str = ""
    owner_id: str | None = None
    recycle_time: str = ""


@dataclass
class MediaItem:
    """Enriched media item ready for filtering, path resolution, and download."""

    unique_id: str
    album_id: str
    album_name: str
    file_type: str  # "1" = photo, "4" = video, "5" = motion/live photo, "9" = heic photo
    create_time_ms: int
    is_cover: int = 0
    is_fav: int = 0
    uid: str = ""
    file_name: str = ""
    file_size: int = 0
    checksum: str = ""
    album_version: str = ""
    owner_id: str | None = None
    expand: dict = field(default_factory=dict)

    @property
    def is_photo(self) -> bool:
        return self.file_type != "4"

    @property
    def is_video(self) -> bool:
        return self.file_type == "4"

    @property
    def is_live_photo(self) -> bool:
        return self.file_type == "5"


@dataclass
class SyncConfig:
    """Runtime configuration for hwcloudpd CLI & Docker daemon."""

    directory: Path = field(default_factory=lambda: Path("./photos"))
    config_dir: Path = field(default_factory=lambda: Path("./config"))
    username: str | None = None
    password: str | None = None
    two_fa_code: str | None = None
    login_timeout_sec: int = 30
    folder_structure: str = "{:%Y/%m/%d}"
    timezone: str = "Asia/Shanghai"
    albums: list[str] = field(default_factory=list)
    exclude_albums: list[str] = field(default_factory=list)
    photo_size: str = "original"  # original | medium | thumb
    file_match_policy: str = "name-size-dedup-with-suffix"  # or name-id7
    live_photo_mov_filename_policy: str = "none"  # none | suffix | original
    set_exif_datetime: bool = False
    xmp_sidecar: bool = False
    skip_videos: bool = False
    skip_photos: bool = False
    skip_live_photos: bool = False
    skip_bursts: bool = False
    only_favorites: bool = False
    created_after_ms: int | None = None
    created_before_ms: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    recent: int | None = None
    until_found: int | None = None
    auto_delete: bool = False
    delete_after_download: bool = False
    keep_cloud_recent_days: int | None = None
    dry_run: bool = False
    only_print_filenames: bool = False
    download_concurrency: int = 2
    page_size: int = 100
    request_delay_ms: int = 120
    cron: str | None = None
    watch_interval: int | None = None
    keepalive_interval: int = 60
    run_on_startup: bool = True
    full_scan_interval_hours: float | None = 168.0
    webhook_url: str | None = None
    telegram_chat_id: str | None = None
    notification_script: str | None = None
    log_level: str = "INFO"


@dataclass
class SyncReport:
    """Summary counters produced by a single synchronization run."""

    scanned: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    skipped_filtered: int = 0
    deleted_local: int = 0
    deleted_cloud: int = 0
    failed: int = 0
    duration_sec: float = 0.0

    def summary_text(self) -> str:
        """Render a concise single-line summary for logs and notifications."""
        return (
            f"scanned={self.scanned}, downloaded={self.downloaded}, "
            f"skipped_existing={self.skipped_existing}, skipped_filtered={self.skipped_filtered}, "
            f"deleted_local={self.deleted_local}, deleted_cloud={self.deleted_cloud}, "
            f"failed={self.failed}, duration={self.duration_sec:.2f}s"
        )
