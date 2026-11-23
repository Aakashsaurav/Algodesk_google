"""
notifications/telegram.py
=========================
Outbound HTTPS Telegram Bot API notifier designed for MacBook Air M2 (works
behind home Wi-Fi / NAT with zero open inbound ports) and seamless cloud move.

Security rules:
- `TELEGRAM_BOT_TOKEN` is read strictly from server environment / 0o600 vault
  (`config.TELEGRAM_BOT_TOKEN`) and is NEVER returned in API responses.
- `TELEGRAM_ALLOWED_CHAT_ID` strictly restricts outbound destinations and any
  optional command polling (`/status`, `/kill`) to the operator's allowlisted chat.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import IST_TZ, config

logger = logging.getLogger(__name__)


def _mask_chat_id(chat_id: str) -> str:
    clean = (chat_id or "").strip()
    if len(clean) <= 4:
        return clean
    return f"{clean[:2]}{'•' * max(2, len(clean) - 4)}{clean[-2:]}"


class TelegramNotifier:
    """Thread-safe outbound Telegram Bot notifier with persisted event preferences."""

    def __init__(
        self,
        settings_file: Optional[Path] = None,
        sender: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
    ) -> None:
        self._explicit_path = settings_file
        self._sender = sender
        self._lock = threading.RLock()
        self._recent: List[Dict[str, Any]] = []

    @property
    def settings_file(self) -> Path:
        return self._explicit_path or config.TELEGRAM_SETTINGS_FILE

    def _default_prefs(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "chat_id": config.TELEGRAM_ALLOWED_CHAT_ID or "",
            "notify_fills": True,
            "notify_rejections": True,
            "notify_daily_loss_breach": True,
            "notify_kill_switch": True,
            "notify_scanner_alerts": True,
            "updated_at_ist": None,
        }

    def load_settings(self) -> Dict[str, Any]:
        with self._lock:
            prefs = self._default_prefs()
            path = self.settings_file
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k in (
                            "enabled",
                            "chat_id",
                            "notify_fills",
                            "notify_rejections",
                            "notify_daily_loss_breach",
                            "notify_kill_switch",
                            "notify_scanner_alerts",
                            "updated_at_ist",
                        ):
                            if k in raw and raw[k] is not None:
                                prefs[k] = raw[k]
                except (OSError, json.JSONDecodeError):
                    pass
            env_chat = config.TELEGRAM_ALLOWED_CHAT_ID
            if env_chat and not prefs.get("chat_id"):
                prefs["chat_id"] = env_chat
            return prefs

    def save_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cur = self.load_settings()
            for key in (
                "enabled",
                "notify_fills",
                "notify_rejections",
                "notify_daily_loss_breach",
                "notify_kill_switch",
                "notify_scanner_alerts",
            ):
                if key in updates and updates[key] is not None:
                    cur[key] = bool(updates[key])
            if "chat_id" in updates and updates["chat_id"] is not None:
                cur["chat_id"] = str(updates["chat_id"]).strip()
            cur["updated_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            path = self.settings_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        prefs = self.load_settings()
        token_configured = bool(config.TELEGRAM_BOT_TOKEN)
        chat_id = str(prefs.get("chat_id") or config.TELEGRAM_ALLOWED_CHAT_ID or "").strip()
        return {
            "ok": True,
            "configured": bool(token_configured and chat_id),
            "bot_token_configured": token_configured,
            "bot_token_source": config.credential_source("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
            "chat_id_masked": _mask_chat_id(chat_id) if chat_id else "",
            "chat_id_configured": bool(chat_id),
            "enabled": bool(prefs.get("enabled", True)),
            "notify_fills": bool(prefs.get("notify_fills", True)),
            "notify_rejections": bool(prefs.get("notify_rejections", True)),
            "notify_daily_loss_breach": bool(prefs.get("notify_daily_loss_breach", True)),
            "notify_kill_switch": bool(prefs.get("notify_kill_switch", True)),
            "notify_scanner_alerts": bool(prefs.get("notify_scanner_alerts", True)),
            "updated_at_ist": prefs.get("updated_at_ist"),
            "recent_notifications": list(self._recent[-10:]),
        }

    def send_message(self, text: str, *, event_type: str = "INFO", force: bool = False) -> Dict[str, Any]:
        prefs = self.load_settings()
        ts = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        if not force and not prefs.get("enabled", True):
            return {"ok": False, "sent": False, "reason": "Telegram notifications disabled."}

        token = config.TELEGRAM_BOT_TOKEN
        chat_id = str(prefs.get("chat_id") or config.TELEGRAM_ALLOWED_CHAT_ID or "").strip()

        if self._sender is not None:
            try:
                res = self._sender(token or "test-token", chat_id or "test-chat", text)
                rec = {"ts_ist": ts, "event_type": event_type, "sent": True, "preview": text[:140]}
                with self._lock:
                    self._recent.append(rec)
                    self._recent = self._recent[-30:]
                return {"ok": True, "sent": True, "response": res}
            except Exception as exc:
                logger.warning("Telegram custom sender failed: %s", exc)
                return {"ok": False, "sent": False, "reason": str(exc)}

        if not token or not chat_id:
            return {
                "ok": False,
                "sent": False,
                "reason": "Set TELEGRAM_BOT_TOKEN in server .env and configure Chat ID to enable Telegram alerts.",
            }

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            ok = bool(raw.get("ok", True))
            rec = {"ts_ist": ts, "event_type": event_type, "sent": ok, "preview": text[:140]}
            with self._lock:
                self._recent.append(rec)
                self._recent = self._recent[-30:]
            return {"ok": ok, "sent": ok, "response": raw}
        except Exception as exc:
            logger.warning("Telegram sendMessage failed: %s", exc)
            return {"ok": False, "sent": False, "reason": str(exc)}

    def notify_event(self, event_type: str, title: str, lines: List[str]) -> Dict[str, Any]:
        prefs = self.load_settings()
        pref_map = {
            "FILL": "notify_fills",
            "ORDER_REJECTED": "notify_rejections",
            "DAILY_LOSS_BREACH": "notify_daily_loss_breach",
            "KILL_SWITCH": "notify_kill_switch",
            "SCANNER_ALERT": "notify_scanner_alerts",
        }
        flag = pref_map.get(event_type.upper())
        if flag and not prefs.get(flag, True):
            return {"ok": False, "sent": False, "reason": f"Preference {flag} disabled."}
        body = "\n".join([f"[AlgoDesk | {event_type.upper()}] {title}", *lines])
        return self.send_message(body, event_type=event_type.upper())


telegram_notifier = TelegramNotifier()

__all__ = ["TelegramNotifier", "telegram_notifier"]
