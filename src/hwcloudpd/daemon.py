"""Unattended Docker daemon: heartbeat keep-alive, cron/interval scheduling, and hot-reload."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import random
import signal
import time
from typing import Callable
from zoneinfo import ZoneInfo

from hwcloudpd.auth import AuthManager
from hwcloudpd.cas_auth import HuaweiCasAuthenticator
from hwcloudpd.client import HuaweiGalleryClient
from hwcloudpd.cron import CronSchedule
from hwcloudpd.models import (
    AuthExpiredError,
    CaptchaRequiredError,
    InvalidCredentialsError,
    SyncConfig,
    SyncReport,
    TwoFactorRequiredError,
)
from hwcloudpd.notifier import Notifier
from hwcloudpd.store import StateStore
from hwcloudpd.sync import SyncEngine

logger = logging.getLogger(__name__)


class SyncDaemon:
    """Concurrent keep-alive + scheduled sync runner with zero stdin interaction."""

    def __init__(
        self,
        config: SyncConfig,
        auth_manager: AuthManager,
        client: HuaweiGalleryClient,
        store: StateStore,
        notifier: Notifier,
        *,
        authenticator: HuaweiCasAuthenticator | None = None,
        engine_factory: Callable[[], SyncEngine] | None = None,
        auto_relogin_cooldown_sec: float = 1800.0,
    ) -> None:
        self.config = config
        self.auth_manager = auth_manager
        self.client = client
        self.store = store
        self.notifier = notifier
        self.authenticator = authenticator
        self._engine_factory = engine_factory or (
            lambda: SyncEngine(self.config, self.client, self.store)
        )
        self.health_file = Path(config.config_dir) / "health.json"
        self._tz = ZoneInfo(config.timezone)

        self._stop_event = asyncio.Event()
        self._auth_ready_event = asyncio.Event()
        self._immediate_sync_event = asyncio.Event()
        self._sync_lock = asyncio.Lock()

        self._auth_valid = False
        self._credentials_rejected = False
        self._auto_relogin_blocked_until: float = 0.0
        self._auto_relogin_cooldown_sec = auto_relogin_cooldown_sec
        self._last_heartbeat_at: float | None = None
        self._last_sync_at: float | None = None
        self._last_full_scan_at: float = self._load_last_full_scan_at()
        self._last_error: str = ""
        self._last_report: SyncReport | None = None

        self._cron_schedule = (
            CronSchedule.parse(config.cron) if config.cron and config.cron.strip() else None
        )

    def _load_last_full_scan_at(self) -> float:
        if not self.health_file.exists():
            return 0.0
        try:
            data = json.loads(self.health_file.read_text(encoding="utf-8"))
            return float(data.get("last_full_scan_at") or 0.0)
        except Exception:
            return 0.0

    def request_stop(self) -> None:
        """Signal all daemon loops to shut down gracefully."""
        self._stop_event.set()
        self._auth_ready_event.set()
        self._immediate_sync_event.set()

    def write_health(self) -> None:
        """Atomically persist container health state for Docker HEALTHCHECK."""
        self.health_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ok": not self._stop_event.is_set(),
            "auth_valid": self._auth_valid,
            "last_heartbeat_at": self._last_heartbeat_at,
            "last_sync_at": self._last_sync_at,
            "last_full_scan_at": self._last_full_scan_at or None,
            "last_error": self._last_error,
            "last_report": self._last_report.summary_text() if self._last_report else None,
            "updated_at": time.time(),
        }
        tmp = self.health_file.with_name(f".health.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.health_file)

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True if stopped early."""
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _handle_auth_expired(
        self,
        reason: str,
        *,
        notify_title: str = "华为云空间登录态失效",
        notify_msg: str | None = None,
    ) -> None:
        self._auth_valid = False
        self._auth_ready_event.clear()
        self._last_error = reason
        self.write_health()
        logger.warning(
            "Huawei Cloud session unavailable/expired: %s. "
            "Run `docker exec -it hwcloudpd hwcloudpd --auth-only` (or update %s / %s); daemon will recover automatically.",
            reason,
            self.auth_manager.curl_file,
            self.auth_manager.auth_file,
        )
        default_msg = (
            f"{reason}。若已配置 HUAWEI_USERNAME/HUAWEI_PASSWORD 请执行 "
            "`docker exec -it hwcloudpd hwcloudpd --auth-only` 输入二次验证码；"
            "或从浏览器 Copy as cURL 覆盖写入 config/curl.txt，容器将自动热恢复。"
        )
        self.notifier.notify("auth_expired", notify_title, notify_msg or default_msg)

    async def _try_silent_auto_relogin(self) -> bool:
        if (
            self.authenticator is None
            or not getattr(self.authenticator, "can_attempt_login", self.authenticator.has_credentials)
            or self._credentials_rejected
            or time.monotonic() < self._auto_relogin_blocked_until
        ):
            return False

        logger.info("Session expired/missing; attempting silent CAS protocol auto-relogin...")
        try:
            await asyncio.to_thread(self.authenticator.login, interactive=False)
            await asyncio.to_thread(self.client.heartbeat_check)
            self._auth_valid = True
            self._last_heartbeat_at = time.time()
            self._last_error = ""
            self._auth_ready_event.set()
            self.notifier.reset_auth_cooldown()
            self.write_health()
            self._immediate_sync_event.set()
            logger.info("Silent auto-relogin succeeded; session restored.")
            return True
        except InvalidCredentialsError as exc:
            self._credentials_rejected = True
            await self._handle_auth_expired(
                str(exc),
                notify_title="华为账号或密码错误",
                notify_msg=f"{exc}。已停止自动登录重试以防账号锁定，请修正环境变量 HUAWEI_USERNAME / HUAWEI_PASSWORD 后重启容器。",
            )
            return False
        except TwoFactorRequiredError as exc:
            self._auto_relogin_blocked_until = time.monotonic() + self._auto_relogin_cooldown_sec
            await self._handle_auth_expired(
                str(exc),
                notify_title="华为云空间需要二次验证",
                notify_msg=f"{exc}。请在宿主机终端执行：`docker exec -it hwcloudpd hwcloudpd --auth-only` 完成二次验证并勾选信任此浏览器。",
            )
            return False
        except CaptchaRequiredError as exc:
            self._auto_relogin_blocked_until = time.monotonic() + self._auto_relogin_cooldown_sec
            await self._handle_auth_expired(
                str(exc),
                notify_title="华为云空间触发人机验证",
                notify_msg=str(exc),
            )
            return False
        except Exception as exc:
            self._auto_relogin_blocked_until = time.monotonic() + min(300.0, self._auto_relogin_cooldown_sec)
            await self._handle_auth_expired(f"静默自动登录失败: {exc}")
            return False

    async def keepalive_step(self) -> bool:
        """Execute a single heartbeat/hot-reload check. Return True if session is healthy."""
        was_valid = self._auth_valid
        try:
            reloaded = self.auth_manager.load_or_reload()
            if reloaded:
                logger.info("Detected updated credentials on disk; validating session...")
                self._auto_relogin_blocked_until = 0.0
            await asyncio.to_thread(self.client.heartbeat_check)
            self._auth_valid = True
            self._last_heartbeat_at = time.time()
            self._last_error = ""
            self._auth_ready_event.set()
            self.notifier.reset_auth_cooldown()
            self.write_health()
            if not was_valid and reloaded:
                # Credentials just recovered while running: kick off a sync promptly.
                self._immediate_sync_event.set()
            return True
        except AuthExpiredError as exc:
            if await self._try_silent_auto_relogin():
                return True
            if self._credentials_rejected:
                self._auth_valid = False
                self._auth_ready_event.clear()
                self.write_health()
                return False
            if time.monotonic() < self._auto_relogin_blocked_until:
                # Notification already dispatched inside _try_silent_auto_relogin().
                return False
            await self._handle_auth_expired(str(exc))
            return False
        except Exception as exc:
            # Transient network errors should not mark credentials expired prematurely.
            self._last_error = str(exc)
            self.write_health()
            logger.warning("Heartbeat check transient failure: %s", exc)
            return self._auth_valid

    async def keepalive_loop(self) -> None:
        """Background loop keeping Huawei Cloud CASTGC/CSRFToken active."""
        while not self._stop_event.is_set():
            healthy = await self.keepalive_step()
            if healthy:
                base = max(15, self.config.keepalive_interval)
                jitter = random.uniform(-min(5.0, base * 0.1), min(5.0, base * 0.1))
                delay = max(10.0, base + jitter)
            else:
                # Poll volume quickly for newly dropped curl.txt / updated auth.json.
                delay = 15.0
            await self._sleep_or_stop(delay)

    async def execute_sync_once(self) -> SyncReport | None:
        """Run one sync cycle guarded by _sync_lock."""
        async with self._sync_lock:
            now_ts = time.time()
            force_full = False
            if self.config.full_scan_interval_hours is not None and self.config.full_scan_interval_hours > 0:
                if (now_ts - self._last_full_scan_at) >= self.config.full_scan_interval_hours * 3600.0:
                    force_full = True

            try:
                engine = self._engine_factory()
                report = await asyncio.to_thread(engine.run_once, force_full_scan=force_full)
                self._last_sync_at = time.time()
                if force_full:
                    self._last_full_scan_at = self._last_sync_at
                self._last_report = report
                self._last_error = ""
                self.write_health()
                logger.info(
                    "Sync completed (%s): %s",
                    "full" if force_full else "incremental",
                    report.summary_text(),
                )
                return report
            except AuthExpiredError as exc:
                await self._handle_auth_expired(str(exc))
                return None
            except Exception as exc:
                self._last_error = str(exc)
                self.write_health()
                logger.exception("Sync run failed: %s", exc)
                self.notifier.notify(
                    "sync_error",
                    "华为云空间同步异常",
                    f"同步执行失败: {exc}",
                )
                return None

    async def scheduler_loop(self) -> None:
        """Wait for valid auth and execute syncs on startup + cron/watch schedule."""
        await self._auth_ready_event.wait()
        if self._stop_event.is_set():
            return

        if self.config.run_on_startup:
            await self.execute_sync_once()

        while not self._stop_event.is_set():
            if not self._auth_valid:
                await self._auth_ready_event.wait()
                if self._stop_event.is_set():
                    break

            wait_sec: float
            if self._cron_schedule is not None:
                now_dt = datetime.now(tz=self._tz)
                next_dt = self._cron_schedule.next_after(now_dt, self.config.timezone)
                wait_sec = max(1.0, (next_dt - now_dt).total_seconds())
                logger.info("Next scheduled sync at %s (%s)", next_dt.isoformat(), self._cron_schedule.expression)
            elif self.config.watch_interval is not None and self.config.watch_interval > 0:
                wait_sec = float(self.config.watch_interval)
                logger.info("Next scheduled sync in %ds", int(wait_sec))
            else:
                # Default fallback if neither cron nor watch_interval was given in daemon mode.
                wait_sec = 7200.0
                logger.info("Next scheduled sync in %ds", int(wait_sec))

            self._immediate_sync_event.clear()
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self._stop_event.wait()),
                    asyncio.create_task(self._immediate_sync_event.wait()),
                ],
                timeout=wait_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                t.result()

            if self._stop_event.is_set():
                break

            if not self._auth_valid:
                continue

            await self.execute_sync_once()

    async def run_forever(self) -> None:
        """Start keep-alive and scheduler loops until SIGTERM/SIGINT."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

        self.write_health()
        try:
            await asyncio.gather(
                self.keepalive_loop(),
                self.scheduler_loop(),
            )
        finally:
            self.write_health()
