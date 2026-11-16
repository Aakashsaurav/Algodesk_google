"""
web/local_server.py
===================
Single-command local Python runner for MacBook Air M2 (8GB RAM).

Uses FastAPI + Uvicorn when installed (recommended for macOS local dev), and
transparently falls back to Python stdlib ThreadingHTTPServer with the exact
same REST + OAuth + HTML routes when running in a minimal Python environment.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from config import setup_logging
from web.service import hub_service

logger = logging.getLogger(__name__)
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"


class _StdlibBrokerHandler(BaseHTTPRequestHandler):
    """Lightweight stdlib HTTP fallback matching FastAPI routes 1-to-1."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            if not TEMPLATE_PATH.exists():
                self._send_json(500, {"detail": "index.html template missing"})
                return
            self._send_html(200, TEMPLATE_PATH.read_text(encoding="utf-8"))
            return

        if path == "/api/overview":
            refresh_raw = query.get("refresh", ["true"])[0].lower()
            refresh = refresh_raw != "false"
            self._send_json(200, hub_service.get_overview(refresh=refresh))
            return

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "brokers" and parts[3] == "reconnect-status":
            self._send_json(200, hub_service.get_reconnect_status(parts[2]))
            return

        if path.startswith("/auth/") and path.endswith("/callback"):
            broker_id = parts[1] if len(parts) >= 3 else "upstox"
            error = query.get("error", [None])[0]
            status = query.get("status", [None])[0]
            code = query.get("code", [None])[0]
            request_token = query.get("request_token", [None])[0]
            auth_token = request_token or code
            if error or (status and status.lower() not in {"success", "ok"}):
                err_text = error or f"Broker returned status={status}"
                self._send_html(
                    400,
                    f"<!doctype html><html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
                    f"<h2>Authorization declined</h2><p style='color:#94a3b8'>{html.escape(err_text)}</p>"
                    f"<a href='/' style='color:#38bdf8'>Return to AlgoDesk</a></body></html>",
                )
                return
            if not auth_token:
                self._send_html(
                    400,
                    "<!doctype html><html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
                    "<h2>Missing authorization token</h2>"
                    "<a href='/' style='color:#38bdf8'>Return to AlgoDesk</a></body></html>",
                )
                return
            try:
                result = hub_service.complete_reconnect(broker_id=broker_id, code_or_url=auth_token)
                state = result.get("broker", {}).get("state", "UNKNOWN")
                js_broker = json.dumps(broker_id)
                self._send_html(
                    200,
                    f"<!doctype html><html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
                    f"<h2>{html.escape(broker_id.title())} connection updated: {html.escape(state)}</h2>"
                    f"<p style='color:#94a3b8'>Session token captured automatically and saved securely. Returning to AlgoDesk...</p>"
                    f"<a href='/' style='color:#38bdf8'>Return to AlgoDesk</a>"
                    f"<script>"
                    f"try {{ if (window.opener) {{ window.opener.postMessage({{type: 'algodesk-oauth-captured', brokerId: {js_broker}}}, '*'); }} }} catch (e) {{}}"
                    f"setTimeout(() => {{ if (window.opener) {{ window.close(); }} else {{ window.location.href = '/'; }} }}, 900);"
                    f"</script></body></html>",
                )
            except Exception as exc:
                self._send_html(
                    400,
                    f"<!doctype html><html><body style='font-family:sans-serif;background:#0b1120;color:#f1f5f9;padding:2rem'>"
                    f"<h2>Token exchange failed</h2><p style='color:#94a3b8'>{html.escape(str(exc))}</p>"
                    f"<a href='/' style='color:#38bdf8'>Return to AlgoDesk</a></body></html>",
                )
            return

        self._send_json(404, {"detail": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"detail": "Invalid JSON body"})
            return

        try:
            if path == "/api/selection":
                res = hub_service.save_selection(
                    trading_broker=str(body.get("trading_broker", "")),
                    data_connection=str(body.get("data_connection", "")),
                )
                self._send_json(200, res)
                return

            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "brokers":
                broker_id = parts[2]
                action = parts[3]

                if action == "probe":
                    self._send_json(200, {"ok": True, "broker": hub_service.probe_one(broker_id)})
                    return
                if action == "credentials":
                    self._send_json(
                        200,
                        hub_service.save_credentials(
                            broker_id=broker_id,
                            api_key=str(body.get("api_key", "")),
                            api_secret=str(body.get("api_secret", "")),
                            redirect_uri=body.get("redirect_uri"),
                        ),
                    )
                    return
                if action == "reconnect":
                    server_port = self.server.server_address[1] if getattr(self, "server", None) else None
                    self._send_json(
                        200,
                        hub_service.start_reconnect(
                            broker_id=broker_id,
                            redirect_uri=body.get("redirect_uri"),
                            server_port=server_port,
                        ),
                    )
                    return
                if action == "reconnect-cancel":
                    self._send_json(200, hub_service.cancel_reconnect(broker_id))
                    return
                if action == "callback-exchange":
                    self._send_json(
                        200,
                        hub_service.complete_reconnect(
                            broker_id=broker_id,
                            code_or_url=str(body.get("code_or_url", "")),
                            redirect_uri=body.get("redirect_uri"),
                        ),
                    )
                    return
                if action == "disconnect":
                    self._send_json(200, hub_service.disconnect_broker(broker_id))
                    return

            self._send_json(404, {"detail": "Not Found"})
        except ValueError as exc:
            self._send_json(400, {"detail": str(exc)})
        except Exception as exc:
            self._send_json(500, {"detail": str(exc)})


def run_server(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> None:
    setup_logging()
    url = f"http://{host}:{port}"
    logger.info("Starting AlgoDesk Broker & Data Hub at %s", url)

    try:
        import uvicorn  # type: ignore
        from web.app import app

        if open_browser:
            webbrowser.open(url)
        uvicorn.run(app, host=host, port=port, log_level="info")
        return
    except ImportError:
        logger.info("FastAPI/Uvicorn not installed; running lightweight Python stdlib server.")

    server = ThreadingHTTPServer((host, port), _StdlibBrokerHandler)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run AlgoDesk local Python Broker Hub")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Listen port (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args(argv)
    run_server(host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main(sys.argv[1:])
