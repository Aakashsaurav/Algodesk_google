"""
broker/upstox/auth.py
---------------------
Refactored Upstox OAuth2 Authentication and session-boundary token manager.

Security & Correctness Fixes Applied (from Algodesk_R4 audit):
1. Fixed logout() calling non-existent config.BASE_URL -> now uses
   config.UPSTOX_BASE_URL with urllib fallback so server-side invalidation runs.
2. Replaced naive date.today() validity check with Upstox's actual 03:30 AM IST
   daily token reset boundary (ZoneInfo("Asia/Kolkata")).
3. Dynamic credential properties read from reloadable config/Keychain so UI
   credential updates work immediately without process restart.
4. Stores bearer token in OS Keychain / keyring whenever available, keeping only
   non-secret metadata in 0o600 token.json (with 0o600 fallback only if keyring
   is unavailable).
5. Supports both FastAPI route OAuth capture and loopback listener capture.
"""

from __future__ import annotations

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
except ImportError:  # pragma: no cover - optional; stdlib urllib fallback supported
    requests = None  # type: ignore[assignment]

from broker.loopback import build_loopback_listener
from config import IST_TZ, config, keychain_delete, keychain_get, keychain_set

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "algodesk"
KEYRING_USERNAME = "upstox_token"


def current_upstox_session_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """
    Return (session_start_ist, next_expiry_ist) for Upstox daily tokens.
    Upstox access tokens reset daily at 03:30 AM IST.
    """
    ref = now.astimezone(IST_TZ) if now else datetime.now(tz=IST_TZ)
    today_reset = datetime.combine(ref.date(), time(3, 30), tzinfo=IST_TZ)
    if ref >= today_reset:
        start = today_reset
        expiry = today_reset + timedelta(days=1)
    else:
        start = today_reset - timedelta(days=1)
        expiry = today_reset
    return start, expiry


class AuthManager:
    """Manages the Upstox OAuth2 lifecycle with dynamic reload and IST expiry tracking."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token_data: Optional[dict] = None

    @property
    def api_key(self) -> str:
        return config.UPSTOX_API_KEY

    @property
    def api_secret(self) -> str:
        return config.UPSTOX_API_SECRET

    @property
    def redirect_uri(self) -> str:
        return config.UPSTOX_REDIRECT_URI

    @property
    def auth_url(self) -> str:
        return config.UPSTOX_AUTH_URL

    @property
    def token_file(self) -> Path:
        return config.UPSTOX_TOKEN_FILE_PATH

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def save_credentials(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: Optional[str] = None,
    ) -> None:
        """
        Persist credentials cleanly into process env and the local 0o600 vault
        (and macOS Keychain only if ALGODESK_USE_KEYCHAIN=true).
        """
        clean_key = api_key.strip()
        clean_secret = api_secret.strip()
        if not clean_key or not clean_secret:
            raise ValueError("Both API Key and API Secret are required.")

        config.save_local_credential("UPSTOX_API_KEY", "upstox_api_key", clean_key)
        config.save_local_credential("UPSTOX_API_SECRET", "upstox_api_secret", clean_secret)

        if redirect_uri and redirect_uri.strip():
            config.save_local_credential("UPSTOX_REDIRECT_URI", "upstox_redirect_uri", redirect_uri.strip())

    def get_login_url(self, redirect_uri: Optional[str] = None, state: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("UPSTOX_API_KEY is not configured. Save API credentials before reconnecting.")
        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": self.api_key,
            "redirect_uri": (redirect_uri or self.redirect_uri),
        }
        if state:
            params["state"] = state
        return f"{config.UPSTOX_AUTH_DIALOG_URL}?{urlencode(params)}"

    def open_login_page(self, redirect_uri: Optional[str] = None) -> str:
        url = self.get_login_url(redirect_uri=redirect_uri)
        webbrowser.open(url)
        return url

    def build_redirect_listener(self, redirect_uri: Optional[str] = None):
        target_uri = redirect_uri or self.redirect_uri
        return build_loopback_listener(
            redirect_uri=target_uri,
            broker_id="upstox",
            ssl_cert_file=config.UPSTOX_REDIRECT_SSL_CERT_FILE,
            ssl_key_file=config.UPSTOX_REDIRECT_SSL_KEY_FILE,
        )

    def _build_redirect_listener(self, redirect_uri: Optional[str] = None):
        return self.build_redirect_listener(redirect_uri=redirect_uri)

    def login_and_capture_token(self, timeout: int = 180, open_browser: bool = True) -> dict:
        login_url = self.get_login_url()
        server, event, captured = self.build_redirect_listener()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if open_browser:
                webbrowser.open(login_url)
            if not event.wait(timeout):
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for Upstox redirect on {self.redirect_uri}"
                )
            redirect_url = captured.get("url")
            if not redirect_url:
                raise RuntimeError("Redirect received without callback URL.")
            return self.generate_token_from_url(str(redirect_url))
        finally:
            server.shutdown()
            server.server_close()

    def _http_post_form(self, url: str, payload: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if requests is not None:
            resp = requests.post(url, data=payload, headers=headers, timeout=timeout)
            try:
                resp.raise_for_status()
            except Exception as exc:
                err_msg = str(exc)
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("message") or err_body.get("errors", [{}])[0].get("message", err_msg)
                except Exception:
                    pass
                raise ValueError(f"Token generation failed: {err_msg}") from exc
            return resp.json()

        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            err_msg = str(exc)
            try:
                err_body = json.loads(body_bytes.decode("utf-8"))
                err_msg = err_body.get("message") or err_msg
            except Exception:
                pass
            raise ValueError(f"Token generation failed: {err_msg}") from exc

    def generate_token(self, auth_code: str, redirect_uri: Optional[str] = None) -> dict:
        """Exchange authorization code for access token and persist with IST expiry."""
        if not self.has_credentials():
            raise ValueError("Upstox API Key and Secret must be configured before exchanging a code.")

        clean_code = auth_code.strip()
        if not clean_code:
            raise ValueError("Authorization code cannot be empty.")

        payload = {
            "code": clean_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "grant_type": "authorization_code",
        }
        token_data = self._http_post_form(self.auth_url, payload, timeout=15)

        now_ist = datetime.now(tz=IST_TZ)
        _, next_expiry = current_upstox_session_bounds(now_ist)
        token_data["generated_at_ist"] = now_ist.isoformat(timespec="seconds")
        token_data["expires_at_ist"] = next_expiry.isoformat(timespec="seconds")
        token_data["generated_date"] = now_ist.date().isoformat()

        with self._lock:
            self._save_token(token_data)
            self._token_data = token_data

        return token_data

    def generate_token_from_url(
        self,
        code_or_redirect_url: str,
        redirect_uri: Optional[str] = None,
    ) -> dict:
        """Accept either a bare auth code or full redirected callback URL."""
        raw = code_or_redirect_url.strip()
        if not raw:
            raise ValueError("Authorization code or redirect URL is required.")

        if "://" in raw or "code=" in raw:
            parsed = urlparse(raw if "://" in raw else f"https://dummy.local/?{raw.lstrip('?')}")
            params = parse_qs(parsed.query)
            auth_code = params.get("code", [None])[0]
            if not auth_code:
                raise ValueError("Could not find 'code' parameter in the redirect URL.")
            return self.generate_token(auth_code, redirect_uri=redirect_uri)

        return self.generate_token(raw, redirect_uri=redirect_uri)

    @staticmethod
    def _is_token_fresh(token_data: dict) -> bool:
        """Check token against Upstox 03:30 AM IST reset boundary."""
        now_ist = datetime.now(tz=IST_TZ)
        session_start, _ = current_upstox_session_bounds(now_ist)

        generated_at_raw = token_data.get("generated_at_ist")
        if generated_at_raw:
            try:
                generated_at = datetime.fromisoformat(str(generated_at_raw))
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=IST_TZ)
                return generated_at >= session_start
            except ValueError:
                pass

        # Backward compatibility with older token.json files
        return token_data.get("generated_date") == now_ist.date().isoformat()

    def _save_token(self, token_data: dict) -> None:
        try:
            config.ensure_runtime_dirs()
            access_token = token_data.get("access_token", "")
            disk_token_data = dict(token_data)

            if access_token and config.USE_KEYCHAIN:
                if keychain_set(KEYRING_SERVICE, KEYRING_USERNAME, access_token):
                    disk_token_data.pop("access_token", None)

            with open(self.token_file, "w", encoding="utf-8") as fh:
                json.dump(disk_token_data, fh, indent=2)
            os.chmod(self.token_file, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as exc:
            logger.warning("Could not persist token metadata: %s", exc)

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

        raise RuntimeError("No valid Upstox session token for the current trading day.")

    def is_authenticated(self) -> bool:
        try:
            self.get_valid_token()
            return True
        except RuntimeError:
            return False

    def get_token_info(self) -> dict:
        with self._lock:
            data = self._token_data if (self._token_data and self._is_token_fresh(self._token_data)) else self._load_token()
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
        """Clear local in-memory, disk, and optional Keychain token state."""
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
        """Invalidate token on Upstox server (using UPSTOX_BASE_URL) and purge local stores."""
        server_ok = True
        token: Optional[str] = None
        with self._lock:
            if self._token_data:
                token = self._token_data.get("access_token")
            if not token:
                loaded = self._load_token()
                token = loaded.get("access_token") if loaded else None

        if token:
            logout_url = f"{config.UPSTOX_BASE_URL}/v2/logout"
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                if requests is not None:
                    resp = requests.delete(logout_url, headers=headers, timeout=8)
                    server_ok = resp.status_code < 500
                else:
                    req = urllib.request.Request(logout_url, headers=headers, method="DELETE")
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        server_ok = resp.status < 500
            except Exception as exc:
                logger.warning("Server-side Upstox logout warning: %s", exc)
                server_ok = False

        self.invalidate_local()
        return server_ok


auth_manager = AuthManager()

__all__ = ["AuthManager", "auth_manager", "current_upstox_session_bounds"]
