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
import queue
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from config import setup_logging
from core.ws_protocol import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_TEXT,
    perform_handshake,
    read_ws_frame,
    send_ws_close,
    send_ws_pong,
    send_ws_text,
)
from web.service import hub_service

logger = logging.getLogger(__name__)
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
TERMINAL_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "terminal.html"


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

        if path in {"/terminal", "/terminal/"}:
            if not TERMINAL_TEMPLATE_PATH.exists():
                self._send_json(500, {"detail": "terminal.html template missing"})
                return
            self._send_html(200, TERMINAL_TEMPLATE_PATH.read_text(encoding="utf-8"))
            return

        if path == "/api/health":
            self._send_json(200, {"ok": True, "service": "algodesk", "phase": 5, "live_terminal": True})
            return

        if path == "/api/overview":
            refresh_raw = query.get("refresh", ["true"])[0].lower()
            refresh = refresh_raw != "false"
            self._send_json(200, hub_service.get_overview(refresh=refresh))
            return

        if path == "/api/instruments/stats":
            self._send_json(200, hub_service.instrument_stats())
            return

        if path == "/api/instruments/search":
            q = query.get("q", [""])[0]
            broker_id = query.get("broker_id", [None])[0]
            exchange = query.get("exchange", [None])[0]
            instrument_type = query.get("instrument_type", [None])[0]
            expiry = query.get("expiry", [None])[0]
            strike_raw = query.get("strike", [None])[0]
            strike = float(strike_raw) if strike_raw else None
            limit = int(query.get("limit", ["25"])[0] or 25)
            self._send_json(
                200,
                hub_service.search_instruments(
                    query=q,
                    broker_id=broker_id,
                    exchange=exchange,
                    instrument_type=instrument_type,
                    expiry=expiry,
                    strike=strike,
                    limit=limit,
                ),
            )
            return

        if path == "/api/instruments/fno":
            strike_raw = query.get("strike", [None])[0]
            strike = float(strike_raw) if strike_raw else None
            self._send_json(
                200,
                hub_service.list_fno_contracts(
                    underlying=query.get("underlying", [""])[0],
                    exchange=query.get("exchange", ["NFO"])[0],
                    contract_type=query.get("contract_type", ["FO"])[0],
                    expiry=query.get("expiry", [None])[0],
                    strike=strike,
                    broker_id=query.get("broker_id", [None])[0],
                    limit=int(query.get("limit", ["40"])[0] or 40),
                ),
            )
            return

        if path == "/api/data/catalog":
            self._send_json(200, hub_service.list_historical_catalog())
            return

        if path == "/api/data/records":
            try:
                symbol = query.get("symbol", [""])[0]
                if not symbol.strip():
                    self._send_json(400, {"detail": "symbol query parameter is required."})
                    return
                limit_raw = query.get("limit", [None])[0]
                self._send_json(
                    200,
                    hub_service.get_historical_records(
                        symbol=symbol,
                        exchange=query.get("exchange", ["NSE"])[0],
                        timeframe=query.get("timeframe", ["1day"])[0],
                        from_date=query.get("from_date", [None])[0],
                        to_date=query.get("to_date", [None])[0],
                        view=query.get("view", ["snapshot"])[0],
                        limit=int(limit_raw) if limit_raw else None,
                        offset=int(query.get("offset", ["0"])[0] or 0),
                    ),
                )
            except Exception as exc:
                self._send_json(400, {"detail": str(exc)})
            return

        if path == "/api/data/export.csv":
            symbol = query.get("symbol", [""])[0]
            timeframe = query.get("timeframe", ["1day"])[0]
            csv_text = hub_service.export_historical_csv(
                symbol=symbol,
                exchange=query.get("exchange", ["NSE"])[0],
                timeframe=timeframe,
                from_date=query.get("from_date", [None])[0],
                to_date=query.get("to_date", [None])[0],
            )
            raw = csv_text.encode("utf-8")
            safe_sym = (symbol.strip().upper() or "DATA")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Disposition", f'attachment; filename="{safe_sym}_{timeframe}.csv"')
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/live/status":
            self._send_json(200, hub_service.get_live_status())
            return

        if path == "/api/live/subscriptions":
            self._send_json(200, hub_service.get_subscriptions())
            return

        if path == "/api/live/stream":
            listener_q = hub_service.register_live_listener()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                initial = hub_service.get_live_status()["feeds"]
                self.wfile.write(f"data: {json.dumps({'type': 'tick_snapshot', **initial})}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        ev = listener_q.get(timeout=5.0)
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub_service.unregister_live_listener(listener_q)
            return

        if path == "/ws/live":
            if not perform_handshake(self):
                self._send_json(400, {"detail": "Expected WebSocket Upgrade header"})
                return
            self.close_connection = True
            listener_q = hub_service.register_live_listener()
            send_lock = threading.Lock()
            stop_event = threading.Event()
            sock: socket.socket = self.connection

            def _ws_writer() -> None:
                try:
                    initial = hub_service.get_live_status()["feeds"]
                    send_ws_text(sock, json.dumps({"type": "tick_snapshot", **initial}), send_lock)
                    while not stop_event.is_set():
                        try:
                            ev = listener_q.get(timeout=1.0)
                            send_ws_text(sock, json.dumps(ev), send_lock)
                        except queue.Empty:
                            continue
                except OSError:
                    stop_event.set()

            writer_thread = threading.Thread(target=_ws_writer, daemon=True)
            writer_thread.start()
            try:
                sock.settimeout(1.0)
                while not stop_event.is_set():
                    try:
                        frame = read_ws_frame(sock)
                    except socket.timeout:
                        continue
                    if frame is None:
                        break
                    opcode, raw_payload = frame
                    if opcode == OPCODE_CLOSE:
                        send_ws_close(sock, send_lock)
                        break
                    if opcode == OPCODE_PING:
                        send_ws_pong(sock, raw_payload, send_lock)
                        continue
                    if opcode == OPCODE_TEXT:
                        try:
                            cmd = json.loads(raw_payload.decode("utf-8"))
                        except Exception:
                            cmd = {}
                        action = str(cmd.get("action") or "").strip().lower()
                        try:
                            if action == "subscribe":
                                res = hub_service.subscribe_asset(
                                    symbol=str(cmd.get("symbol") or ""),
                                    exchange=str(cmd.get("exchange") or "NSE"),
                                    instrument_type=cmd.get("instrument_type"),
                                    instrument_key=cmd.get("instrument_key"),
                                )
                                send_ws_text(sock, json.dumps({"type": "ack", **res}), send_lock)
                            elif action == "unsubscribe":
                                res = hub_service.unsubscribe_asset(
                                    symbol=str(cmd.get("symbol") or ""),
                                    exchange=cmd.get("exchange"),
                                )
                                send_ws_text(sock, json.dumps({"type": "ack", **res}), send_lock)
                            elif action == "poll":
                                res = hub_service.poll_live_feeds()
                                send_ws_text(sock, json.dumps({"type": "tick_snapshot", **res}), send_lock)
                        except Exception as exc:
                            send_ws_text(sock, json.dumps({"type": "error", "detail": str(exc)}), send_lock)
            except OSError:
                pass
            finally:
                stop_event.set()
                hub_service.unregister_live_listener(listener_q)
            return

        if path == "/api/audit/verify":
            limit = int(query.get("limit", ["25"])[0] or 25)
            self._send_json(200, hub_service.verify_audit_chain(limit=limit))
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

            if path == "/api/instruments/sync":
                res = hub_service.sync_instruments(str(body.get("broker_id", "upstox")))
                self._send_json(200, res)
                return

            if path == "/api/data/download":
                res = hub_service.download_historical(
                    symbol=str(body.get("symbol", "")),
                    exchange=str(body.get("exchange", "NSE")),
                    timeframe=str(body.get("timeframe", "1day")),
                    from_date=body.get("from_date"),
                    to_date=body.get("to_date"),
                    provider=body.get("provider"),
                    instrument_type=body.get("instrument_type"),
                    instrument_key=body.get("instrument_key"),
                )
                self._send_json(200, res)
                return

            if path in {"/api/live/feeds/poll", "/api/live/poll"}:
                symbols = body.get("symbols") if isinstance(body.get("symbols"), list) else None
                self._send_json(200, hub_service.poll_live_feeds(symbols=symbols))
                return

            if path in {"/api/live/feeds/failover", "/api/live/failover"}:
                self._send_json(200, hub_service.trigger_feed_failover(reason=body.get("reason")))
                return

            if path in {"/api/live/feeds/recover", "/api/live/recover-primary"}:
                self._send_json(200, hub_service.recover_primary_feed())
                return

            if path == "/api/live/subscribe":
                self._send_json(
                    200,
                    hub_service.subscribe_asset(
                        symbol=str(body.get("symbol", "")),
                        exchange=str(body.get("exchange", "NSE")),
                        instrument_type=body.get("instrument_type"),
                        instrument_key=body.get("instrument_key"),
                    ),
                )
                return

            if path == "/api/live/unsubscribe":
                self._send_json(
                    200,
                    hub_service.unsubscribe_asset(
                        symbol=str(body.get("symbol", "")),
                        exchange=body.get("exchange"),
                    ),
                )
                return

            if path == "/api/live/preflight":
                symbols = body.get("symbols") if isinstance(body.get("symbols"), list) else None
                self._send_json(200, hub_service.run_preflight(symbols=symbols))
                return

            if path == "/api/live/kill-switch":
                engaged = bool(body.get("engaged", False))
                reason = str(body.get("reason", ""))
                self._send_json(200, hub_service.set_kill_switch(engaged=engaged, reason=reason))
                return

            self._send_json(404, {"detail": "Not Found"})
        except ValueError as exc:
            self._send_json(400, {"detail": str(exc)})
        except Exception as exc:
            self._send_json(500, {"detail": str(exc)})


class _AlgoDeskHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


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

    server = _AlgoDeskHTTPServer((host, port), _StdlibBrokerHandler)
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
