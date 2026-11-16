"""
broker/loopback.py
------------------
Shared HTTP / dual-protocol HTTPS local loopback OAuth callback listener.
Supports both Upstox (`?code=...`) and Zerodha Kite (`?request_token=...`)
redirects automatically without duplicating socket server code across brokers.
"""

from __future__ import annotations

import html
import json
import logging
import os
import socket
import ssl
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


def ensure_loopback_ssl_certs(cert_file: str, key_file: str) -> tuple[str, str]:
    """Ensure self-signed localhost TLS cert/key exist for HTTPS loopback capture."""
    cert_path = Path(cert_file)
    key_path = Path(key_file)
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

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


def _captured_html(broker_id: str) -> bytes:
    safe_broker = html.escape(broker_id.title())
    js_broker = json.dumps(broker_id)
    return (
        "<!doctype html><html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
        f"<h3>{safe_broker} login captured automatically.</h3>"
        "<p style='color:#94a3b8'>AlgoDesk is exchanging your token and verifying your session. You can close this tab.</p>"
        f"<script>try{{if(window.opener){{window.opener.postMessage({{type:'algodesk-oauth-captured',brokerId:{js_broker}}},'*');}}setTimeout(()=>window.close(),1200);}}catch(e){{}}</script>"
        "</body></html>"
    ).encode("utf-8")


class DualProtocolLoopbackListener:
    """Accept HTTPS or plain HTTP on the same loopback port for OAuth redirect capture."""

    def __init__(
        self,
        host: str,
        port: int,
        expected_path: str,
        cert_file: str,
        key_file: str,
        broker_id: str = "upstox",
    ) -> None:
        if not cert_file or not key_file:
            raise RuntimeError("HTTPS redirect capture requires TLS cert and key files.")
        cert_file, key_file = ensure_loopback_ssl_certs(cert_file, key_file)

        self.host = host
        self.port = port
        self.expected_path = expected_path or "/"
        self.broker_id = broker_id
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
            if (
                request_path.startswith(self.expected_path)
                or "code" in query
                or "request_token" in query
            ):
                self.captured["url"] = f"{scheme}://{host_header}{path}"
                self.event.set()
                self._send_response(conn, 200, _captured_html(self.broker_id))
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


def build_loopback_listener(
    redirect_uri: str,
    broker_id: str,
    ssl_cert_file: str,
    ssl_key_file: str,
):
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    expected_path = parsed.path or "/"
    is_loopback = host in {"127.0.0.1", "localhost"}

    if parsed.scheme == "https" and is_loopback:
        server = DualProtocolLoopbackListener(
            host=host,
            port=port,
            expected_path=expected_path,
            cert_file=ssl_cert_file,
            key_file=ssl_key_file,
            broker_id=broker_id,
        )
        return server, server.event, server.captured

    captured: dict[str, Optional[str]] = {"url": None}
    event = threading.Event()
    body_bytes = _captured_html(broker_id)

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # type: ignore[override]
            query = parse_qs(urlparse(self.path).query)
            if self.path.startswith(expected_path) or "code" in query or "request_token" in query:
                scheme = parsed.scheme or "http"
                host_header = self.headers.get("Host", f"{host}:{port}")
                captured["url"] = f"{scheme}://{host_header}{self.path}"
                event.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer((host, port), _CallbackHandler)
    server.timeout = 0.5
    return server, event, captured


__all__ = ["DualProtocolLoopbackListener", "build_loopback_listener", "ensure_loopback_ssl_certs"]
