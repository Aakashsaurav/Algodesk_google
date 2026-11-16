"""
broker/zerodha/auth.py
----------------------
Zerodha Kite Connect v3 OAuth2 Authentication and 06:00 AM IST token manager.

Implements:
- Dynamic credential resolution (.env live reload > macOS Keychain).
- Kite Connect v3 SHA-256 checksum exchange (`sha256(api_key + request_token + api_secret)`).
- Accurate 06:00 AM IST daily token reset boundary (`ZoneInfo("Asia/Kolkata")`).
- Secure bearer persistence in macOS Keychain with 0o600 non-secret metadata file.
- Automatic local loopback callback capture (`?request_token=...`) and clean server logout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - stdlib urllib fallback supported
    requests = None  # type: ignore[assignment]

from broker.loopback import build_loopback_listener
from config import IST_TZ, config, keychain_delete, keychain_get, keychain_set

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "algodesk"
KEYRING_USERNAME = "zerodha_token"


def current_zerodha_session_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """
    Return (session_start_ist, next_expiry_ist) for Kite Connect daily tokens.
    Kite access tokens expire daily at 06:00 AM IST.
    """
    ref = now.astimezone(IST_TZ) if now else datetime.now(tz=IST_TZ)
    today_reset = datetime.combine(ref.date(), time(6, 0), tzinfo=IST_TZ)
    if ref >= today_reset:
        start = today_reset
        expiry = today_reset + timedelta(days=1)
    else:
        start = today_reset - timedelta(days=1)
        expiry = today_reset
    return start, expiry


class ZerodhaAuthManager:
    """Manages Zerodha Kite Connect OAuth checksum exchange and IST session tracking."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token_data: Optional[dict] = None

    @property
    def api_key(self) -> str:
        return config.ZERODHA_API_KEY

    @property
    def api_secret(self) -> str:
        return config.ZERODHA_API_SECRET

    @property
    def redirect_uri(self) -> str:
        return config.ZERODHA_REDIRECT_URI

    @property
    def session_url(self) -> str:
        return config.ZERODHA_SESSION_URL

    @property
    def token_file(self) -> Path:
        return config.ZERODHA_TOKEN_FILE_PATH

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def save_credentials(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: Optional[str] = None,
    ) -> None:
        """Persist Zerodha Kite credentials safely in process env and local 0o600 vault."""
        clean_key = api_key.strip()
        clean_secret = api_secret.strip()
        if not clean_key or not clean_secret:
            raise ValueError("Both Zerodha API Key and API Secret are required.")

        config.save_local_credential("ZERODHA_API_KEY", "zerodha_api_key", clean_key)
        config.save_local_credential("ZERODHA_API_SECRET", "zerodha_api_secret", clean_secret)

        if redirect_uri and redirect_uri.strip():
            config.save_local_credential("ZERODHA_REDIRECT_URI", "zerodha_redirect_uri", redirect_uri.strip())

    def get_login_url(self, redirect_uri: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("ZERODHA_API_KEY is not configured. Save API credentials before reconnecting.")
        params: Dict[str, str] = {
            "v": "3",
            "api_key": self.api_key,
        }
        return f"{config.ZERODHA_LOGIN_URL}?{urlencode(params)}"

    def open_login_page(self, redirect_uri: Optional[str] = None) -> str:
        url = self.get_login_url(redirect_uri=redirect_uri)
        webbrowser.open(url)
        return url

    def build_redirect_listener(self, redirect_uri: Optional[str] = None):
        target_uri = redirect_uri or self.redirect_uri
        return build_loopback_listener(
            redirect_uri=target_uri,
            broker_id="zerodha",
            ssl_cert_file=config.UPSTOX_REDIRECT_SSL_CERT_FILE,
            ssl_key_file=config.UPSTOX_REDIRECT_SSL_KEY_FILE,
        )

    def _compute_checksum(self, request_token: str) -> str:
        raw = f"{self.api_key}{request_token}{self.api_secret}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _http_post_session(self, payload: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
        headers = {
            "X-Kite-Version": "3",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if requests is not None:
            resp = requests.post(self.session_url, data=payload, headers=headers, timeout=timeout)
            try:
                resp.raise_for_status()
            except Exception as exc:
                err_msg = str(exc)
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("message") or err_msg
                except Exception:
                    pass
                raise ValueError(f"Kite session exchange failed: {err_msg}") from exc
            parsed = resp.json()
            return parsed.get("data", parsed) if isinstance(parsed, dict) else {}

        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(self.session_url, data=encoded, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
                return parsed.get("data", parsed) if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            err_msg = str(exc)
            try:
                err_body = json.loads(body_bytes.decode("utf-8"))
                err_msg = err_body.get("message") or err_msg
            except Exception:
                pass
            raise ValueError(f"Kite session exchange failed: {err_msg}") from exc

    def generate_token(self, request_token: str, redirect_uri: Optional[str] = None) -> dict:
        """Exchange Kite request_token + SHA-256 checksum for an access_token."""
        if not self.has_credentials():
            raise ValueError("Zerodha API Key and Secret must be configured before exchanging request_token.")

        clean_req_token = request_token.strip()
        if not clean_req_token:
            raise ValueError("request_token cannot be empty.")

        payload = {
            "api_key": self.api_key,
            "request_token": clean_req_token,
            "checksum": self._compute_checksum(clean_req_token),
        }
        token_data = dict(self._http_post_session(payload, timeout=15))

        now_ist = datetime.now(tz=IST_TZ)
        _, next_expiry = current_zerodha_session_bounds(now_ist)
        token_data["generated_at_ist"] = now_ist.isoformat(timespec="seconds")
        token_data["expires_at_ist"] = next_expiry.isoformat(timespec="seconds")
        token_data["generated_date"] = now_ist.date().isoformat()

        with self._lock:
            self._save_token(token_data)
            self._token_data = token_data

        return token_data

    def generate_token_from_url(
        self,
        token_or_redirect_url: str,
        redirect_uri: Optional[str] = None,
    ) -> dict:
        """Accept either a bare Kite request_token or full redirected callback URL."""
        raw = token_or_redirect_url.strip()
        if not raw:
            raise ValueError("request_token or callback URL is required.")

        if "://" in raw or "request_token=" in raw or "code=" in raw:
            parsed = urlparse(raw if "://" in raw else f"https://dummy.local/?{raw.lstrip('?')}")
            params = parse_qs(parsed.query)
            status = (params.get("status", [""])[0] or "").lower()
            if status and status not in {"success", "ok"}:
                raise ValueError(f"Kite login did not succeed (status={status}).")
            req_token = params.get("request_token", [None])[0] or params.get("code", [None])[0]
            if not req_token:
                raise ValueError("Could not find 'request_token' parameter in callback URL.")
            return self.generate_token(req_token, redirect_uri=redirect_uri)

        return self.generate_token(raw, redirect_uri=redirect_uri)

    @staticmethod
    def _is_token_fresh(token_data: dict) -> bool:
        """Check token against Zerodha Kite 06:00 AM IST daily reset boundary."""
        now_ist = datetime.now(tz=IST_TZ)
        session_start, _ = current_zerodha_session_bounds(now_ist)

        generated_at_raw = token_data.get("generated_at_ist")
        if generated_at_raw:
            try:
                generated_at = datetime.fromisoformat(str(generated_at_raw))
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=IST_TZ)
                return generated_at >= session_start
            except ValueError:
                pass

        return token_data.get("generated_date") == now_ist.date().isoformat()

    def _save_token(self, token_data: dict) -> None:
        try:
            config.ensure_runtime_dirs()
            access_token = token_data.get("access_token", "")
            disk_token_data = dict(token_data)
            disk_token_data.pop("api_key", None)

            if access_token and config.USE_KEYCHAIN:
                if keychain_set(KEYRING_SERVICE, KEYRING_USERNAME, access_token):
                    disk_token_data.pop("access_token", None)

            with open(self.token_file, "w", encoding="utf-8") as fh:
                json.dump(disk_token_data, fh, indent=2)
            os.chmod(self.token_file, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as exc:
            logger.warning("Could not persist Zerodha token metadata: %s", exc)

    def _load_token(self) -> Optional[dict]:
        if not self.token_file.exists():
            return None
        try:
            with open(self.token_file, "r", encoding="utf-8") as fh:
                token_data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

        if not self._is_token_fresh(token_data):
            return None

        access_token = token_data.get("access_token")
        if not access_token and config.USE_KEYCHAIN:
            access_token = keychain_get(KEYRING_SERVICE, KEYRING_USERNAME)
        if not access_token:
            return None

        token_data["access_token"] = access_token
        return token_data

    def get_valid_token(self) -> str:
        with self._lock:
            if self._token_data and self._is_token_fresh(self._token_data):
                return self._token_data["access_token"]
            self._token_data = None

            loaded = self._load_token()
            if loaded:
                self._token_data = loaded
                return loaded["access_token"]

        raise RuntimeError("No valid Zerodha Kite session token for the current trading day.")

    def is_authenticated(self) -> bool:
        try:
            self.get_valid_token()
            return True
        except RuntimeError:
            return False

    def get_token_info(self) -> dict:
        with self._lock:
            data = (
                self._token_data
                if (self._token_data and self._is_token_fresh(self._token_data))
                else self._load_token()
            )
            if data:
                return {
                    "status": "valid",
                    "generated_at_ist": data.get("generated_at_ist"),
                    "expires_at_ist": data.get("expires_at_ist"),
                    "generated_date": data.get("generated_date"),
                    "user_id": data.get("user_id", ""),
                    "user_name": data.get("user_name", ""),
                }
        return {"status": "not_authenticated"}

    def invalidate_local(self) -> None:
        """Clear local in-memory, disk, and optional Keychain Zerodha token state."""
        with self._lock:
            self._token_data = None
            if self.token_file.exists():
                try:
                    self.token_file.unlink()
                except OSError:
                    pass
            if config.USE_KEYCHAIN:
                keychain_delete(KEYRING_SERVICE, KEYRING_USERNAME)

    def logout(self) -> bool:
        """Invalidate session on Kite servers (DELETE /session/token) and clear local stores."""
        server_ok = True
        token: Optional[str] = None
        with self._lock:
            if self._token_data:
                token = self._token_data.get("access_token")
            if not token:
                loaded = self._load_token()
                token = loaded.get("access_token") if loaded else None

        if token and self.api_key:
            params = urllib.parse.urlencode({"api_key": self.api_key, "access_token": token})
            logout_url = f"{self.session_url}?{params}"
            headers = {"X-Kite-Version": "3", "Accept": "application/json"}
            try:
                if requests is not None:
                    resp = requests.delete(logout_url, headers=headers, timeout=8)
                    server_ok = resp.status_code < 500
                else:
                    req = urllib.request.Request(logout_url, headers=headers, method="DELETE")
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        server_ok = resp.status < 500
            except Exception as exc:
                logger.warning("Server-side Zerodha logout warning: %s", exc)
                server_ok = False

        self.invalidate_local()
        return server_ok


zerodha_auth_manager = ZerodhaAuthManager()

__all__ = ["ZerodhaAuthManager", "zerodha_auth_manager", "current_zerodha_session_bounds"]
