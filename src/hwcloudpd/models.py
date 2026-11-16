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


class SyncCancelledError(HwCloudError):
    """Raised inside the sync engine when a graceful stop was requested."""


class ConfigError(ValueError):
    """Raised when the runtime configuration is invalid or self-contradictory."""


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
    is_shared: bool = False


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
    is_shared: bool = False


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
    is_shared: bool = False
    # True when the cloud did not return a real file name and we synthesized one.
    name_synthesized: bool = False

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
    max_sync_hours: float = 24.0

    @property
    def move_mode(self) -> bool:
        """True when any option that moves cloud items to the Recycle Bin is enabled."""
        return self.delete_after_download or self.keep_cloud_recent_days is not None

    @property
    def is_daemon(self) -> bool:
        return bool(
            (self.cron and self.cron.strip())
            or (self.watch_interval is not None and self.watch_interval > 0)
        )

    def validate(self) -> None:
        """Raise ConfigError describing every invalid / contradictory option."""
        # Local imports keep models.py free of heavy module-level dependencies.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from hwcloudpd.cron import CronSchedule
        from hwcloudpd.postprocess import validate_folder_structure

        errors: list[str] = []

        def _non_negative(name: str, value: int | float | None) -> None:
            if value is not None and value < 0:
                errors.append(f"{name} 不能为负数（当前 {value}）")

        _non_negative("--recent", self.recent)
        _non_negative("--until-found", self.until_found)
        _non_negative("--keep-cloud-recent-days", self.keep_cloud_recent_days)
        _non_negative("--min-size", self.min_size)
        _non_negative("--max-size", self.max_size)
        _non_negative("--watch-with-interval", self.watch_interval)
        if self.until_found == 0:
            errors.append("--until-found 必须 ≥ 1（不需要早停请不要设置该参数）")

        if self.delete_after_download and self.keep_cloud_recent_days is not None:
            errors.append(
                "--delete-after-download 与 --keep-cloud-recent-days 不能同时使用，请二选一"
            )
        if self.skip_photos and self.skip_videos and self.skip_live_photos:
            errors.append("--skip-photos、--skip-videos、--skip-live-photos 同时开启后没有任何内容可下载")
        if (
            self.min_size is not None
            and self.max_size is not None
            and self.min_size > self.max_size
        ):
            errors.append(f"--min-size ({self.min_size}) 大于 --max-size ({self.max_size})")
        if (
            self.created_after_ms is not None
            and self.created_before_ms is not None
            and self.created_after_ms > self.created_before_ms
        ):
            errors.append("--created-after 晚于 --created-before")
        if self.move_mode and self.photo_size != "original":
            errors.append("移动模式（删除云端原图）只允许在 --size original 下使用，否则会丢失原图")

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            errors.append(f"无效时区 '{self.timezone}': {exc}")

        if self.cron and self.cron.strip():
            try:
                CronSchedule.parse(self.cron)
            except ValueError as exc:
                errors.append(f"无效 cron 表达式: {exc}")

        try:
            validate_folder_structure(self.folder_structure)
        except ValueError as exc:
            errors.append(str(exc))

        if errors:
            raise ConfigError("配置无效：\n  - " + "\n  - ".join(errors))


@dataclass
class SyncReport:
    """Summary counters produced by a single synchronization run."""

    scanned: int = 0
    downloaded: int = 0
    adopted: int = 0
    redownloaded_changed: int = 0
    skipped_existing: int = 0
    skipped_filtered: int = 0
    deferred_failed: int = 0
    deleted_local: int = 0
    deleted_cloud: int = 0
    cloud_missing: int = 0
    failed: int = 0
    cancelled: bool = False
    full_scan: bool = False
    duration_sec: float = 0.0

    def summary_text(self) -> str:
        """Render a concise single-line summary for logs and notifications."""
        text = (
            f"scanned={self.scanned}, downloaded={self.downloaded}, "
            f"adopted={self.adopted}, changed={self.redownloaded_changed}, "
            f"skipped_existing={self.skipped_existing}, skipped_filtered={self.skipped_filtered}, "
            f"deferred_failed={self.deferred_failed}, "
            f"deleted_local={self.deleted_local}, deleted_cloud={self.deleted_cloud}, "
            f"cloud_missing={self.cloud_missing}, "
            f"failed={self.failed}, duration={self.duration_sec:.2f}s"
        )
        if self.cancelled:
            text += " (cancelled)"
        return text
