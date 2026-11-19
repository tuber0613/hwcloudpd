"""CLI entrypoint mirroring icloudpd flags plus Docker daemon & cURL import."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import sys
import time

from hwcloudpd.auth import AuthManager
from hwcloudpd.cas_auth import HuaweiCasAuthenticator
from hwcloudpd.client import HuaweiGalleryClient
from hwcloudpd.config import (
    env_bool,
    env_float,
    env_int,
    env_list,
    env_str,
    parse_byte_size,
    parse_date_bound,
    read_secret_file,
)
from hwcloudpd.daemon import SyncDaemon
from hwcloudpd.models import (
    AuthExpiredError,
    ConfigError,
    HwCloudError,
    InvalidCredentialsError,
    SyncConfig,
)
from hwcloudpd.notifier import WEBHOOK_TYPES, Notifier
from hwcloudpd.store import StateStore
from hwcloudpd.sync import SyncEngine

logger = logging.getLogger("hwcloudpd")

_BOOL = argparse.BooleanOptionalAction


def build_parser() -> argparse.ArgumentParser:
    """Construct ArgumentParser with icloudpd-compatible flags and environment defaults.

    Every boolean flag has a `--no-` counterpart so a value enabled through an environment
    variable can still be switched off on the command line.
    """
    parser = argparse.ArgumentParser(
        prog="hwcloudpd",
        description="Unattended Huawei Cloud Space Gallery downloader & Docker sync daemon (icloudpd parity).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Storage & Auth Paths
    parser.add_argument(
        "-d",
        "--directory",
        default=env_str("HWCLOUD_DIRECTORY", "./photos"),
        help="Local root directory to archive downloaded photos and videos",
    )
    parser.add_argument(
        "--config-dir",
        default=env_str("HWCLOUD_CONFIG_DIR", "./config"),
        help="Directory storing auth.json, cas_cookies.lwp, curl.txt, state.db, and health.json",
    )
    parser.add_argument(
        "-u",
        "--username",
        default=env_str("HUAWEI_USERNAME"),
        help="Huawei ID account (phone/email) for pure-HTTP CAS login (env: HUAWEI_USERNAME)",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("HUAWEI_PASSWORD") or None,
        help="Huawei ID password (env: HUAWEI_PASSWORD). Prefer --password-file: CLI args are visible in `ps`",
    )
    parser.add_argument(
        "--password-file",
        default=env_str("HUAWEI_PASSWORD_FILE"),
        help="Read the Huawei ID password from this file, e.g. a Docker secret (env: HUAWEI_PASSWORD_FILE)",
    )
    parser.add_argument(
        "--two-fa-code",
        default=env_str("HUAWEI_2FA_CODE"),
        help="Optional 6-digit 2FA code for a one-off non-TTY --auth-only (ignored in daemon mode)",
    )
    parser.add_argument(
        "--force-login",
        "--login",
        dest="force_login",
        action="store_true",
        help="Force fresh pure-HTTP CAS login (and interactive bash 2FA if needed) even if auth.json exists",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=env_int("HWCLOUD_LOGIN_TIMEOUT", 30) or 30,
        help="HTTP timeout in seconds for CAS login requests",
    )
    parser.add_argument(
        "--import-curl",
        default=None,
        metavar="PATH_OR_DASH",
        help="Import a browser 'Copy as cURL' command from file (or '-' for stdin) into auth.json and exit",
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Authenticate (via existing session, or HUAWEI_USERNAME/HUAWEI_PASSWORD + bash 2FA) and exit",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Check daemon health.json for Docker HEALTHCHECK and exit",
    )

    # Album Selection
    parser.add_argument(
        "-l",
        "--list-albums",
        action="store_true",
        help="Print all Huawei Cloud Gallery albums with photo/video counts and exit",
    )
    parser.add_argument(
        "-a",
        "--album",
        action="append",
        default=None,
        help="Only sync matching album name/ID or wildcard (repeatable; default syncs all non-recycle albums)",
    )
    parser.add_argument(
        "--exclude-album",
        action="append",
        default=None,
        help="Exclude album name/ID or wildcard pattern (repeatable)",
    )

    # Folder Structure & File Naming
    parser.add_argument(
        "--folder-structure",
        default=env_str("HWCLOUD_FOLDER_STRUCTURE", "{:%Y/%m/%d}"),
        help="Subfolder layout template (e.g. '{:%%Y/%%m/%%d}', '{album}/{:%%Y/%%m}', or 'none')",
    )
    parser.add_argument(
        "--tz",
        "--timezone",
        dest="timezone",
        default=env_str("TZ", "Asia/Shanghai"),
        help="IANA timezone used for date folder bucketing and cron schedules",
    )
    parser.add_argument(
        "--size",
        dest="photo_size",
        choices=["original", "medium", "thumb"],
        default=env_str("HWCLOUD_SIZE", "original"),
        help="Asset variant to download",
    )
    parser.add_argument(
        "--file-match-policy",
        choices=["name-size-dedup-with-suffix", "name-id7"],
        default=env_str("HWCLOUD_FILE_MATCH_POLICY", "name-size-dedup-with-suffix"),
        help="Collision and deduplication filename strategy",
    )
    parser.add_argument(
        "--live-photo-mov-filename-policy",
        choices=["none", "suffix", "original"],
        default=env_str("HWCLOUD_LIVE_PHOTO_POLICY", "none"),
        help="Optionally extract embedded MP4 from Huawei Motion Photos (fileType=5)",
    )
    parser.add_argument(
        "--set-exif-datetime",
        action=_BOOL,
        default=env_bool("HWCLOUD_SET_EXIF_DATETIME", False),
        help="Add EXIF DateTimeOriginal to JPEG files that carry no EXIF block at all",
    )
    parser.add_argument(
        "--xmp-sidecar",
        action=_BOOL,
        default=env_bool("HWCLOUD_XMP_SIDECAR", False),
        help="Write Adobe/Immich/PhotoPrism compatible .xmp sidecar metadata files",
    )

    # Filtering
    parser.add_argument(
        "--skip-videos",
        action=_BOOL,
        default=env_bool("HWCLOUD_SKIP_VIDEOS", False),
        help="Skip video files (fileType=4)",
    )
    parser.add_argument(
        "--skip-photos",
        action=_BOOL,
        default=env_bool("HWCLOUD_SKIP_PHOTOS", False),
        help="Skip still photos (e.g. JPEG/PNG fileType=1, HEIC fileType=9)",
    )
    parser.add_argument(
        "--skip-live-photos",
        action=_BOOL,
        default=env_bool("HWCLOUD_SKIP_LIVE_PHOTOS", False),
        help="Skip Huawei Motion/Live Photos (fileType=5)",
    )
    parser.add_argument(
        "--skip-bursts",
        action=_BOOL,
        default=env_bool("HWCLOUD_SKIP_BURSTS", False),
        help="Only download the burst cover instead of expanding all burst frames",
    )
    parser.add_argument(
        "--only-favorites",
        action=_BOOL,
        default=env_bool("HWCLOUD_ONLY_FAVORITES", False),
        help="Only download items marked as favorites",
    )
    parser.add_argument(
        "--created-after",
        default=env_str("HWCLOUD_CREATED_AFTER"),
        help="Only include media created at or after YYYY-MM-DD / ISO timestamp",
    )
    parser.add_argument(
        "--created-before",
        default=env_str("HWCLOUD_CREATED_BEFORE"),
        help="Only include media created at or before YYYY-MM-DD / ISO timestamp",
    )
    parser.add_argument(
        "--min-size",
        default=env_str("HWCLOUD_MIN_SIZE"),
        help="Skip files smaller than this size (e.g. 50KB, 1MB)",
    )
    parser.add_argument(
        "--max-size",
        default=env_str("HWCLOUD_MAX_SIZE"),
        help="Skip files larger than this size (e.g. 500MB, 2GB)",
    )
    parser.add_argument(
        "--recent",
        type=int,
        default=env_int("HWCLOUD_RECENT", None),
        help="Only inspect the N most recent media items (global timeline order)",
    )
    parser.add_argument(
        "--until-found",
        type=int,
        default=env_int("HWCLOUD_UNTIL_FOUND", None),
        help="Stop paginating an album after N consecutive already-synced items",
    )

    # Sync / Move / Dry-Run Modes
    parser.add_argument(
        "--auto-delete",
        action=_BOOL,
        default=env_bool("HWCLOUD_AUTO_DELETE", False),
        help="Mirror cloud deletions: delete local files the user moved to the Huawei Cloud Recycle Bin",
    )
    parser.add_argument(
        "--delete-after-download",
        action=_BOOL,
        default=env_bool("HWCLOUD_DELETE_AFTER_DOWNLOAD", False),
        help="Move items to Huawei Cloud Recycle Bin after verified local backup (never shared albums)",
    )
    parser.add_argument(
        "--keep-cloud-recent-days",
        type=int,
        default=env_int("HWCLOUD_KEEP_CLOUD_RECENT_DAYS", None),
        help="Move already-backed-up items older than N days to Huawei Cloud Recycle Bin",
    )
    parser.add_argument(
        "--dry-run",
        action=_BOOL,
        default=env_bool("HWCLOUD_DRY_RUN", False),
        help="Simulate sync without downloading or deleting files",
    )
    parser.add_argument(
        "--only-print-filenames",
        action="store_true",
        help="Print target paths of files that would be downloaded and exit",
    )

    # Performance & Anti-Throttle
    parser.add_argument(
        "--download-concurrency",
        type=int,
        default=env_int("HWCLOUD_DOWNLOAD_CONCURRENCY", 2) or 2,
        help="Concurrent binary download workers",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=env_int("HWCLOUD_PAGE_SIZE", 100) or 100,
        help="Batch size per /album/getSimpleFile request",
    )
    parser.add_argument(
        "--request-delay-ms",
        type=int,
        default=env_int("HWCLOUD_REQUEST_DELAY_MS", 120),
        help="Delay in milliseconds between pagination requests",
    )

    # Unattended Docker Daemon & Notifications
    parser.add_argument(
        "--cron",
        default=env_str("CRON_SCHEDULE") or env_str("HWCLOUD_CRON"),
        help="Run persistently as daemon executing sync on 5-field cron schedule (e.g. '0 */2 * * *')",
    )
    parser.add_argument(
        "--watch-with-interval",
        type=int,
        default=env_int("HWCLOUD_WATCH_INTERVAL", None),
        help="Run persistently as daemon polling every N seconds",
    )
    parser.add_argument(
        "--keepalive-interval",
        type=int,
        default=env_int("HWCLOUD_KEEPALIVE_INTERVAL", 60) or 60,
        help="Session heartbeatCheck interval in seconds when running in daemon mode",
    )
    parser.add_argument(
        "--run-on-startup",
        action=_BOOL,
        default=env_bool("HWCLOUD_RUN_ON_STARTUP", True),
        help="In daemon mode, sync immediately on startup (--no-run-on-startup waits for the first tick)",
    )
    parser.add_argument(
        "--full-scan-interval-hours",
        type=float,
        default=env_float("HWCLOUD_FULL_SCAN_INTERVAL_HOURS", 168.0),
        help="In daemon mode, run a full scan ignoring --until-found every N hours (0 to disable)",
    )
    parser.add_argument(
        "--max-sync-hours",
        type=float,
        default=env_float("HWCLOUD_MAX_SYNC_HOURS", 24.0),
        help="Healthcheck reports unhealthy when a single sync runs longer than this",
    )
    parser.add_argument(
        "--webhook-url",
        default=env_str("HWCLOUD_WEBHOOK_URL"),
        help="Webhook URL (WeCom / Feishu / DingTalk / Bark / Telegram / Generic) for alerts",
    )
    parser.add_argument(
        "--webhook-type",
        choices=list(WEBHOOK_TYPES),
        default=env_str("HWCLOUD_WEBHOOK_TYPE", "auto"),
        help="Webhook payload format; 'auto' detects it from the URL host",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=env_str("HWCLOUD_TELEGRAM_CHAT_ID"),
        help="Optional chat_id when using a Telegram bot webhook URL",
    )
    parser.add_argument(
        "--notification-script",
        default=env_str("HWCLOUD_NOTIFICATION_SCRIPT"),
        help="Optional executable script invoked with (event_type, title, message)",
    )
    parser.add_argument(
        "--log-level",
        default=(env_str("HWCLOUD_LOG_LEVEL", "INFO") or "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )

    return parser


def args_to_config(ns: argparse.Namespace) -> SyncConfig:
    """Convert parsed CLI namespace + env fallbacks into a SyncConfig (call .validate() after)."""
    albums = ns.album if ns.album is not None else env_list("HWCLOUD_ALBUMS")
    exclude_albums = (
        ns.exclude_album if ns.exclude_album is not None else env_list("HWCLOUD_EXCLUDE_ALBUMS")
    )
    full_scan = ns.full_scan_interval_hours
    if full_scan is not None and full_scan <= 0:
        full_scan = None

    password = ns.password if ns.password else None
    if not password and ns.password_file:
        password = read_secret_file(ns.password_file)

    return SyncConfig(
        directory=Path(ns.directory),
        config_dir=Path(ns.config_dir),
        username=(ns.username.strip() if ns.username else None),
        password=password,
        two_fa_code=(ns.two_fa_code.strip() if ns.two_fa_code else None),
        login_timeout_sec=max(10, int(ns.login_timeout)),
        folder_structure=ns.folder_structure,
        timezone=ns.timezone,
        albums=albums,
        exclude_albums=exclude_albums,
        photo_size=ns.photo_size,
        file_match_policy=ns.file_match_policy,
        live_photo_mov_filename_policy=ns.live_photo_mov_filename_policy,
        set_exif_datetime=bool(ns.set_exif_datetime),
        xmp_sidecar=bool(ns.xmp_sidecar),
        skip_videos=bool(ns.skip_videos),
        skip_photos=bool(ns.skip_photos),
        skip_live_photos=bool(ns.skip_live_photos),
        skip_bursts=bool(ns.skip_bursts),
        only_favorites=bool(ns.only_favorites),
        created_after_ms=parse_date_bound(ns.created_after, ns.timezone, end_of_day=False),
        created_before_ms=parse_date_bound(ns.created_before, ns.timezone, end_of_day=True),
        min_size=parse_byte_size(ns.min_size),
        max_size=parse_byte_size(ns.max_size),
        recent=ns.recent,
        until_found=ns.until_found,
        auto_delete=bool(ns.auto_delete),
        delete_after_download=bool(ns.delete_after_download),
        keep_cloud_recent_days=ns.keep_cloud_recent_days,
        dry_run=bool(ns.dry_run),
        only_print_filenames=bool(ns.only_print_filenames),
        download_concurrency=max(1, int(ns.download_concurrency)),
        page_size=max(1, min(200, int(ns.page_size))),
        request_delay_ms=max(0, int(ns.request_delay_ms or 0)),
        cron=ns.cron,
        watch_interval=ns.watch_with_interval,
        keepalive_interval=max(10, int(ns.keepalive_interval)),
        run_on_startup=bool(ns.run_on_startup),
        full_scan_interval_hours=full_scan,
        webhook_url=ns.webhook_url,
        telegram_chat_id=ns.telegram_chat_id,
        notification_script=ns.notification_script,
        log_level=ns.log_level,
        max_sync_hours=max(0.5, float(ns.max_sync_hours or 24.0)),
    )


def _configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _run_healthcheck(ns: argparse.Namespace) -> int:
    """Docker HEALTHCHECK. Uses only raw CLI values so a broken config can't crash it."""
    health_file = Path(ns.config_dir) / "health.json"
    is_daemon = bool((ns.cron or "").strip()) or bool(
        ns.watch_with_interval is not None and ns.watch_with_interval > 0
    )
    if not is_daemon and not health_file.exists():
        print("OK: one-shot mode (no daemon to monitor)")
        return 0

    if not health_file.exists():
        print(f"UNHEALTHY: missing {health_file}", file=sys.stderr)
        return 1
    try:
        data = json.loads(health_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"UNHEALTHY: invalid health.json ({exc})", file=sys.stderr)
        return 1

    now = time.time()
    keepalive = max(10, int(ns.keepalive_interval or 60))
    max_age = max(180.0, keepalive * 4.0)
    age = now - float(data.get("updated_at") or 0.0)
    if not data.get("ok") or age > max_age:
        print(f"UNHEALTHY: stale or stopped (age={age:.1f}s)", file=sys.stderr)
        return 1
    if data.get("auth_valid") is False:
        reason = data.get("auth_block_reason") or data.get("last_error") or "session invalid"
        print(f"UNHEALTHY: Huawei Cloud session invalid ({reason}); run `hwcloudpd --auth-only`", file=sys.stderr)
        return 1
    started = data.get("sync_started_at")
    max_hours = float(data.get("max_sync_hours") or ns.max_sync_hours or 24.0)
    if started and (now - float(started)) > max_hours * 3600.0:
        print(
            f"UNHEALTHY: current sync running for {(now - float(started)) / 3600.0:.1f}h (> {max_hours}h)",
            file=sys.stderr,
        )
        return 1
    print("OK")
    return 0


def _ensure_authenticated_session(
    auth_manager: AuthManager,
    client: HuaweiGalleryClient,
    authenticator: HuaweiCasAuthenticator,
    *,
    force_login: bool = False,
) -> None:
    """Validate existing auth.json session or fall back to pure-HTTP CAS login."""
    if not force_login:
        try:
            auth_manager.ensure_session()
            client.heartbeat_check()
            return
        except AuthExpiredError:
            if not authenticator.can_attempt_login:
                raise
    elif not authenticator.can_attempt_login:
        raise AuthExpiredError(
            "No valid auth.json and neither HUAWEI_USERNAME/HUAWEI_PASSWORD nor CASTGC cookie is available."
        )
    authenticator.login(interactive=sys.stdin.isatty())
    client.heartbeat_check()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    try:
        parser = build_parser()
    except ValueError as exc:
        # e.g. HWCLOUD_PAGE_SIZE=abc in the environment
        print(f"配置错误（环境变量）: {exc}", file=sys.stderr)
        return 2
    ns = parser.parse_args(argv)
    _configure_logging(ns.log_level)

    if ns.healthcheck:
        return _run_healthcheck(ns)

    try:
        config = args_to_config(ns)
        config.validate()
    except (ConfigError, ValueError, KeyError, OSError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    auth_manager = AuthManager(config.config_dir)

    if ns.import_curl is not None:
        try:
            if ns.import_curl == "-":
                curl_text = sys.stdin.read()
            else:
                curl_text = Path(ns.import_curl).read_text(encoding="utf-8")
            session = auth_manager.import_curl_text(curl_text)
        except (OSError, ValueError) as exc:
            print(f"Import failed: {exc}", file=sys.stderr)
            return 2
        print(
            f"Imported Huawei Cloud session for userid={session.user_id} into {auth_manager.auth_file}"
        )
        return 0

    client = HuaweiGalleryClient(auth_manager)
    notifier = Notifier(
        webhook_url=config.webhook_url,
        telegram_chat_id=config.telegram_chat_id,
        notification_script=config.notification_script,
        webhook_type=ns.webhook_type,
    )

    authenticator = HuaweiCasAuthenticator(
        auth_manager,
        username=config.username,
        password=config.password,
        two_fa_code=config.two_fa_code,
        login_timeout_sec=config.login_timeout_sec,
    )

    if ns.auth_only:
        interactive = sys.stdin.isatty()
        if not ns.force_login:
            try:
                auth_manager.ensure_session()
                client.heartbeat_check()
                print(f"Authenticated OK (auth file: {auth_manager.auth_file})")
                return 0
            except AuthExpiredError:
                pass
            except HwCloudError as exc:
                logger.warning("Could not verify existing session (%s); trying to log in", exc)

        if not authenticator.has_credentials and authenticator.can_attempt_login:
            try:
                authenticator.login(interactive=interactive)
                client.heartbeat_check()
                print(f"Authenticated OK (auth file: {auth_manager.auth_file})")
                return 0
            except (AuthExpiredError, InvalidCredentialsError, HwCloudError) as exc:
                if not interactive:
                    print(f"Authentication failed: {exc}", file=sys.stderr)
                    return 2
                logger.warning("CASTGC SSO refresh failed (%s); prompting for credentials", exc)

        uname = config.username
        upass = config.password
        if not (uname and upass) and interactive:
            if not uname:
                uname = input("请输入华为账号 (手机号/邮箱): ").strip()
            if not upass:
                upass = getpass.getpass("请输入华为账号密码: ")
            authenticator = HuaweiCasAuthenticator(
                auth_manager,
                username=uname,
                password=upass,
                two_fa_code=config.two_fa_code,
                login_timeout_sec=config.login_timeout_sec,
            )

        if not authenticator.can_attempt_login:
            print(
                "Authentication failed: no valid auth.json, no CASTGC cookie, and HUAWEI_USERNAME / HUAWEI_PASSWORD are not set.",
                file=sys.stderr,
            )
            return 2

        try:
            authenticator.login(interactive=interactive)
            client.heartbeat_check()
            print(f"Authenticated OK (auth file: {auth_manager.auth_file})")
            return 0
        except (AuthExpiredError, InvalidCredentialsError, HwCloudError) as exc:
            print(f"Authentication failed: {exc}", file=sys.stderr)
            return 2

    if ns.list_albums:
        try:
            _ensure_authenticated_session(
                auth_manager,
                client,
                authenticator,
                force_login=ns.force_login,
            )
            albums = client.list_albums(include_stats=True)
        except (AuthExpiredError, InvalidCredentialsError, HwCloudError) as exc:
            print(f"Authentication failed: {exc}", file=sys.stderr)
            return 2
        for a in albums:
            tag = " [recycle]" if a.is_recycle else (" [shared]" if a.is_shared else "")
            print(f"{a.album_id}\t{a.name}{tag}\tphotos={a.img_num}\tvideos={a.video_num}")
        return 0

    with StateStore(config.config_dir / "state.db") as store:
        if config.is_daemon:
            if authenticator.two_fa_code:
                # A 2FA code is single-use; replaying it on every silent re-login would trigger
                # new SMS/pushes and failed verifications.
                logger.warning("HUAWEI_2FA_CODE is ignored in daemon mode; use `--auth-only` instead.")
                authenticator.two_fa_code = None
            daemon = SyncDaemon(
                config,
                auth_manager,
                client,
                store,
                notifier,
                # Always pass the authenticator: credentials / CASTGC may appear later (e.g. after
                # `docker exec ... --auth-only`), and the daemon checks availability dynamically.
                authenticator=authenticator,
            )
            asyncio.run(daemon.run_forever())
            return 0

        # One-shot execution mode
        try:
            _ensure_authenticated_session(
                auth_manager,
                client,
                authenticator,
                force_login=ns.force_login,
            )

            engine = SyncEngine(config, client, store)
            try:
                report = engine.run_once()
            except KeyboardInterrupt:
                engine.request_stop()
                logger.warning("Interrupted")
                return 130
            if not config.only_print_filenames:
                logger.info("Sync summary: %s", report.summary_text())
            return 1 if (report.failed > 0 or report.cancelled) else 0
        except (AuthExpiredError, InvalidCredentialsError) as exc:
            logger.error("Session expired or login failed: %s", exc)
            notifier.notify(
                "auth_expired",
                "华为云空间登录态失效",
                f"{exc}。请更新 {auth_manager.curl_file} 或运行 `hwcloudpd --auth-only`。",
            )
            return 2
        except HwCloudError as exc:
            logger.error("Sync failed: %s", exc)
            notifier.notify("sync_error", "华为云空间同步异常", f"同步执行失败: {exc}")
            return 1
