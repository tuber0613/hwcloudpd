"""Webhook and script notifications for auth expiration and sync errors."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import urllib.request

logger = logging.getLogger(__name__)


WEBHOOK_TYPES = ("auto", "wecom", "dingtalk", "feishu", "telegram", "bark", "generic")


def _detect_webhook_type(parsed) -> str:
    host = (parsed.hostname or "").lower()
    if "qyapi.weixin.qq.com" in host:
        return "wecom"
    if "oapi.dingtalk.com" in host:
        return "dingtalk"
    if "open.feishu.cn" in host or "open.larksuite.com" in host:
        return "feishu"
    if "api.telegram.org" in host:
        return "telegram"
    if host == "day.app" or host.endswith(".day.app"):
        return "bark"
    return "generic"


def _build_webhook_request(
    webhook_url: str,
    *,
    event_type: str,
    title: str,
    message: str,
    telegram_chat_id: str | None = None,
    webhook_type: str = "auto",
) -> tuple[str, bytes, dict[str, str]]:
    parsed = urlparse(webhook_url)
    kind = webhook_type if webhook_type and webhook_type != "auto" else _detect_webhook_type(parsed)
    full_text = f"[{title}] {message}"
    headers = {"Content-Type": "application/json;charset=utf-8"}

    if kind in ("wecom", "dingtalk"):
        body = {"msgtype": "text", "text": {"content": full_text}}
        return webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    if kind == "feishu":
        body = {"msg_type": "text", "content": {"text": full_text}}
        return webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    if kind == "telegram":
        qs = parse_qs(parsed.query)
        chat_id = (qs.get("chat_id") or [telegram_chat_id or ""])[0]
        clean_query = {k: v[0] for k, v in qs.items() if k != "chat_id" and v}
        clean_url = urlunparse(parsed._replace(query=urlencode(clean_query)))
        body: dict[str, object] = {"text": full_text}
        if chat_id:
            body["chat_id"] = chat_id
        return clean_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    if kind == "bark":
        body = {"title": title, "body": message, "group": "hwcloudpd"}
        return webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    generic = {
        "event": event_type,
        "title": title,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return webhook_url, json.dumps(generic, ensure_ascii=False).encode("utf-8"), headers


DEFAULT_COOLDOWNS_SEC: dict[str, float] = {
    "auth_expired": 12 * 3600.0,
    "sync_error": 6 * 3600.0,
    "sync_partial": 6 * 3600.0,
    "import_error": 3600.0,
}


class Notifier:
    """Dispatches deduplicated Webhook and custom script alerts."""

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        telegram_chat_id: str | None = None,
        notification_script: str | None = None,
        webhook_type: str = "auto",
        auth_cooldown_sec: float = 12 * 3600.0,
        cooldowns_sec: dict[str, float] | None = None,
        sender: Callable[[str, bytes, dict[str, str]], None] | None = None,
    ) -> None:
        self.webhook_url = webhook_url.strip() if webhook_url else None
        self.telegram_chat_id = telegram_chat_id.strip() if telegram_chat_id else None
        self.notification_script = notification_script.strip() if notification_script else None
        self.webhook_type = (webhook_type or "auto").strip().lower()
        self.cooldowns_sec = dict(DEFAULT_COOLDOWNS_SEC)
        self.cooldowns_sec["auth_expired"] = auth_cooldown_sec
        if cooldowns_sec:
            self.cooldowns_sec.update(cooldowns_sec)
        self._sender = sender or self._default_http_post
        self._last_sent_at: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def auth_cooldown_sec(self) -> float:
        return self.cooldowns_sec["auth_expired"]

    @staticmethod
    def _default_http_post(url: str, body: bytes, headers: dict[str, str]) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Refusing webhook URL with disallowed scheme: {url!r}")
        req = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            resp.read()

    def reset_cooldown(self, event_type: str) -> None:
        """Allow the next alert of `event_type` to be sent immediately."""
        with self._lock:
            self._last_sent_at.pop(event_type, None)

    def reset_auth_cooldown(self) -> None:
        """Reset cooldown once authentication recovers."""
        self.reset_cooldown("auth_expired")

    def _in_cooldown(self, event_type: str) -> bool:
        cooldown = self.cooldowns_sec.get(event_type, 0.0)
        if cooldown <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            last = self._last_sent_at.get(event_type)
            if last is not None and (now - last) < cooldown:
                return True
            self._last_sent_at[event_type] = now
            return False

    def notify(self, event_type: str, title: str, message: str) -> bool:
        """Send alert to configured Webhook and/or notification script. Return True if sent."""
        if not self.webhook_url and not self.notification_script:
            return False
        if self._in_cooldown(event_type):
            logger.debug("Skipping duplicate %s notification during cooldown", event_type)
            return False

        sent = False
        if self.webhook_url:
            try:
                url, body, headers = _build_webhook_request(
                    self.webhook_url,
                    event_type=event_type,
                    title=title,
                    message=message,
                    telegram_chat_id=self.telegram_chat_id,
                    webhook_type=self.webhook_type,
                )
                self._sender(url, body, headers)
                sent = True
            except Exception as exc:
                logger.warning("Webhook notification failed: %s", exc)

        if self.notification_script:
            try:
                proc = subprocess.run(
                    [self.notification_script, event_type, title, message],
                    timeout=15,
                    check=False,
                    capture_output=True,
                )
                if proc.returncode == 0:
                    sent = True
                else:
                    logger.warning(
                        "Notification script exited with %d: %s",
                        proc.returncode,
                        (proc.stderr or b"").decode("utf-8", errors="replace")[:300],
                    )
            except Exception as exc:
                logger.warning("Notification script failed: %s", exc)

        return sent
