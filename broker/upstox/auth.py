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
import socket
import ssl
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - optional; stdlib urllib fallback supported
    requests = None  # type: ignore[assignment]

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


def _ensure_loopback_ssl_certs(cert_file: str, key_file: str) -> tuple[str, str]:
    """Ensure self-signed localhost TLS cert/key exist for HTTPS loopback capture."""
    cert_path = Path(cert_file)
    key_path = Path(key_file)
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    import subprocess

    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "365",
                "-subj",
                "/CN=127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        return str(cert_path), str(key_path)
    except Exception as exc:
        raise RuntimeError(
            "HTTPS redirect capture requires cert.pem and key.pem, and automatic generation via openssl failed."
        ) from exc


class _DualProtocolLoopbackListener:
    """Accept HTTPS or plain HTTP on the same loopback port for OAuth redirect capture."""

    def __init__(
        self,
        host: str,
        port: int,
        expected_path: str,
        cert_file: str,
        key_file: str,
    ) -> None:
        if not cert_file or not key_file:
            raise RuntimeError(
                "HTTPS redirect capture requires UPSTOX_REDIRECT_SSL_CERT_FILE "
                "and UPSTOX_REDIRECT_SSL_KEY_FILE."
            )
        cert_file, key_file = _ensure_loopback_ssl_certs(cert_file, key_file)

        self.host = host
        self.port = port
        self.expected_path = expected_path or "/"
        self.timeout = 0.5
        self.captured: dict[str, Optional[str]] = {"url": None}
        self.event = threading.Event()
        self._stop_event = threading.Event()

        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl_context.load_cert_chain(certfile=cert_file, keyfile=key_file)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(5)
        self._sock.settimeout(self.timeout)

    def serve_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_connection(conn)

    def shutdown(self) -> None:
        self._stop_event.set()

    def server_close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _handle_connection(self, raw_conn: socket.socket) -> None:
        conn = raw_conn
        scheme = "http"
        try:
            try:
                first_byte = raw_conn.recv(1, socket.MSG_PEEK)
            except (OSError, ValueError):
                first_byte = b""

            if first_byte[:1] == b"\x16":
                conn = self._ssl_context.wrap_socket(raw_conn, server_side=True)
                scheme = "https"

            conn.settimeout(2.0)
            request_bytes = b""
            while b"\r\n\r\n" not in request_bytes and len(request_bytes) < 8192:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                request_bytes += chunk

            if not request_bytes:
                return

            lines = request_bytes.decode("latin1", errors="ignore").split("\r\n")
            parts = (lines[0] if lines else "").split()
            path = parts[1] if len(parts) >= 2 else "/"
            host_header = f"{self.host}:{self.port}"
            for line in lines[1:]:
                if line.lower().startswith("host:"):
                    host_header = line.split(":", 1)[1].strip()
                    break

            query = parse_qs(urlparse(path).query)
            request_path = urlparse(path).path
            if request_path.startswith(self.expected_path) or "code" in query:
                self.captured["url"] = f"{scheme}://{host_header}{path}"
                self.event.set()
                body = (
                    "<html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
                    "<h3>Upstox login captured automatically.</h3>"
                    "<p>AlgoDesk is exchanging your code and verifying your session. You can close this tab.</p>"
                    "<script>try{if(window.opener){window.opener.postMessage({type:'algodesk-oauth-captured',brokerId:'upstox'},'*');}setTimeout(()=>window.close(),1200);}catch(e){}</script>"
                    "</body></html>"
                ).encode("utf-8")
                self._send_response(conn, 200, body)
                return

            self._send_response(conn, 404, b"Not Found")
        except (ssl.SSLError, OSError) as exc:
            logger.debug("Local redirect socket error: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _send_response(conn: socket.socket, status_code: int, body: bytes) -> None:
        status_text = "OK" if status_code == 200 else "Not Found"
        response = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        conn.sendall(response)


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
        Persist credentials safely into macOS Keychain (when available) and
        in-process environment variables. Never writes plaintext secrets into
        algodesk.toml or exposes them back to the browser.
        """
        clean_key = api_key.strip()
        clean_secret = api_secret.strip()
        if not clean_key or not clean_secret:
            raise ValueError("Both API Key and API Secret are required.")

        os.environ["UPSTOX_API_KEY"] = clean_key
        os.environ["UPSTOX_API_SECRET"] = clean_secret
        keychain_set(KEYRING_SERVICE, "upstox_api_key", clean_key)
        keychain_set(KEYRING_SERVICE, "upstox_api_secret", clean_secret)

        if redirect_uri and redirect_uri.strip():
            clean_uri = redirect_uri.strip()
            os.environ["UPSTOX_REDIRECT_URI"] = clean_uri
            keychain_set(KEYRING_SERVICE, "upstox_redirect_uri", clean_uri)

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
        parsed = urlparse(target_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected_path = parsed.path or "/"
        is_loopback = host in {"127.0.0.1", "localhost"}

        if parsed.scheme == "https" and is_loopback:
            server = _DualProtocolLoopbackListener(
                host=host,
                port=port,
                expected_path=expected_path,
                cert_file=config.UPSTOX_REDIRECT_SSL_CERT_FILE,
                key_file=config.UPSTOX_REDIRECT_SSL_KEY_FILE,
            )
            return server, server.event, server.captured

        captured: dict[str, Optional[str]] = {"url": None}
        event = threading.Event()

        class _CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # type: ignore[override]
                query = parse_qs(urlparse(self.path).query)
                if self.path.startswith(expected_path) or "code" in query:
                    scheme = parsed.scheme or "http"
                    host_header = self.headers.get("Host", f"{host}:{port}")
                    captured["url"] = f"{scheme}://{host_header}{self.path}"
                    event.set()
                    body = (
                        b"<html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
                        b"<h3>Upstox login captured automatically.</h3>"
                        b"<p>AlgoDesk is verifying your session. You can close this tab.</p>"
                        b"<script>try{if(window.opener){window.opener.postMessage({type:'algodesk-oauth-captured',brokerId:'upstox'},'*');}setTimeout(()=>window.close(),1200);}catch(e){}</script>"
                        b"</body></html>"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer((host, port), _CallbackHandler)
        server.timeout = 0.5
        return server, event, captured

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

            stored_in_keychain = False
            if access_token:
                stored_in_keychain = keychain_set(KEYRING_SERVICE, KEYRING_USERNAME, access_token)
                if stored_in_keychain:
                    disk_token_data.pop("access_token", None)

            with open(self.token_file, "w", encoding="utf-8") as fh:
                json.dump(disk_token_data, fh, indent=2)
            os.chmod(self.token_file, stat.S_IRUSR | stat.S_IWUSR)

            if access_token and not stored_in_keychain:
                logger.warning(
                    "OS Keychain/keyring unavailable; stored token in 0o600 file fallback."
                )
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

        access_token = keychain_get(KEYRING_SERVICE, KEYRING_USERNAME) or token_data.get("access_token")
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
        """Clear local in-memory, disk, and Keychain token state."""
        with self._lock:
            self._token_data = None
            if self.token_file.exists():
                try:
                    self.token_file.unlink()
                except OSError:
                    pass
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
