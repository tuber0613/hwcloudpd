"""Webhook and script notifications for auth expiration and sync errors."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import subprocess
import time
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import urllib.request

logger = logging.getLogger(__name__)


def _build_webhook_request(
    webhook_url: str,
    *,
    event_type: str,
    title: str,
    message: str,
    telegram_chat_id: str | None = None,
) -> tuple[str, bytes, dict[str, str]]:
    parsed = urlparse(webhook_url)
    host = (parsed.hostname or "").lower()
    full_text = f"[{title}] {message}"
    headers = {"Content-Type": "application/json;charset=utf-8"}

    if "qyapi.weixin.qq.com" in host or "oapi.dingtalk.com" in host:
        body = {"msgtype": "text", "text": {"content": full_text}}
        return webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    if "open.feishu.cn" in host or "open.larksuite.com" in host:
        body = {"msg_type": "text", "content": {"text": full_text}}
        return webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    if "api.telegram.org" in host:
        qs = parse_qs(parsed.query)
        chat_id = (qs.get("chat_id") or [telegram_chat_id or ""])[0]
        clean_query = {k: v[0] for k, v in qs.items() if k != "chat_id" and v}
        clean_url = urlunparse(parsed._replace(query=urlencode(clean_query)))
        body: dict[str, object] = {"text": full_text}
        if chat_id:
            body["chat_id"] = chat_id
        return clean_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    if "day.app" in host or parsed.path.rstrip("/").endswith("/push"):
        body = {"title": title, "body": message, "group": "hwcloudpd"}
        return webhook_url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers

    generic = {
        "event": event_type,
        "title": title,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return webhook_url, json.dumps(generic, ensure_ascii=False).encode("utf-8"), headers


class Notifier:
    """Dispatches deduplicated Webhook and custom script alerts."""

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        telegram_chat_id: str | None = None,
        notification_script: str | None = None,
        auth_cooldown_sec: float = 12 * 3600.0,
        sender: Callable[[str, bytes, dict[str, str]], None] | None = None,
    ) -> None:
        self.webhook_url = webhook_url.strip() if webhook_url else None
        self.telegram_chat_id = telegram_chat_id.strip() if telegram_chat_id else None
        self.notification_script = notification_script.strip() if notification_script else None
        self.auth_cooldown_sec = auth_cooldown_sec
        self._sender = sender or self._default_http_post
        self._last_auth_alert_at: float = 0.0

    @staticmethod
    def _default_http_post(url: str, body: bytes, headers: dict[str, str]) -> None:
        req = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            resp.read()

    def reset_auth_cooldown(self) -> None:
        """Reset cooldown once authentication recovers."""
        self._last_auth_alert_at = 0.0

    def notify(self, event_type: str, title: str, message: str) -> bool:
        """Send alert to configured Webhook and/or notification script. Return True if sent."""
        now = time.monotonic()
        if event_type == "auth_expired":
            if (
                self._last_auth_alert_at > 0
                and (now - self._last_auth_alert_at) < self.auth_cooldown_sec
            ):
                logger.debug("Skipping duplicate auth_expired notification during cooldown")
                return False
            self._last_auth_alert_at = now

        sent = False
        if self.webhook_url:
            try:
                url, body, headers = _build_webhook_request(
                    self.webhook_url,
                    event_type=event_type,
                    title=title,
                    message=message,
                    telegram_chat_id=self.telegram_chat_id,
                )
                self._sender(url, body, headers)
                sent = True
            except Exception as exc:
                logger.warning("Webhook notification failed: %s", exc)

        if self.notification_script:
            try:
                subprocess.run(
                    [self.notification_script, event_type, title, message],
                    timeout=15,
                    check=False,
                    capture_output=True,
                )
                sent = True
            except Exception as exc:
                logger.warning("Notification script failed: %s", exc)

        return sent
