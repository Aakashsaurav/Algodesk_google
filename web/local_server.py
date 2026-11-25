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
from web.security import RateLimitError, security_manager
from web.service import hub_service
from web.spa import ensure_spa_bundle, has_spa_bundle, render_spa_html, resolve_spa_asset

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

    def _enforce_auth(self, method: str, path: str, query: Optional[Dict[str, List[str]]] = None) -> bool:
        """Enforce deny-by-default Bearer auth across all HTTP verbs and WebSocket/SSE routes."""
        is_ws = path.startswith("/ws/")
        if not is_ws and not security_manager.is_protected_request(method, path):
            return True
        if is_ws and not security_manager.is_auth_enforced():
            return True
        if security_manager.verify_bearer(self.headers.get("Authorization")):
            return True
        if (is_ws or path == "/api/live/stream") and query:
            raw_tok = (query.get("token") or [""])[0]
            if raw_tok and security_manager.verify_raw_token(raw_tok):
                return True
        self._send_json(
            401,
            {
                "detail": "Operator authentication required. Sign in with your admin passcode or ALGODESK_API_TOKEN.",
                "auth_required": True,
            },
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if not self._enforce_auth("GET", path, query=query):
            return

        if path in {"/", "/terminal", "/terminal/"}:
            body_html = render_spa_html(path)
            if body_html is None:
                self._send_json(500, {"detail": "Frontend SPA bundle and fallback template are missing"})
                return
            self._send_html(200, body_html)
            return

        if path == "/legacy":
            if not TEMPLATE_PATH.exists():
                self._send_json(404, {"detail": "Legacy template not found"})
                return
            self._send_html(200, TEMPLATE_PATH.read_text(encoding="utf-8"))
            return

        if path == "/api/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "algodesk",
                    "phase": 6,
                    "live_terminal": True,
                    "analytics": True,
                    "spa": has_spa_bundle(),
                },
            )
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

        if path == "/ws/live-screener":
            if not perform_handshake(self):
                self._send_json(400, {"detail": "Expected WebSocket Upgrade header"})
                return
            self.close_connection = True
            listener_q = hub_service.register_live_screener_listener()
            send_lock = threading.Lock()
            stop_event = threading.Event()
            sock: socket.socket = self.connection

            def _lsc_writer() -> None:
                try:
                    initial = hub_service.get_live_screener_status()
                    send_ws_text(sock, json.dumps({"type": "live_screen:state", **initial}), send_lock)
                    while not stop_event.is_set():
                        try:
                            ev = listener_q.get(timeout=1.0)
                            send_ws_text(sock, json.dumps(ev), send_lock)
                        except queue.Empty:
                            continue
                except OSError:
                    stop_event.set()

            writer_thread = threading.Thread(target=_lsc_writer, daemon=True)
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
                            if action == "start":
                                res = hub_service.start_live_screener(
                                    symbols=cmd.get("symbols") if isinstance(cmd.get("symbols"), list) else None,
                                    universe=str(cmd.get("universe", "CUSTOM")),
                                    exchange=str(cmd.get("exchange", "NSE")),
                                    timeframe=str(cmd.get("timeframe", "1day")),
                                    screen_id=cmd.get("screen_id"),
                                    preset=str(cmd.get("preset", "all_metrics")),
                                    params=cmd.get("params") if isinstance(cmd.get("params"), dict) else None,
                                    provider=cmd.get("provider"),
                                    backend=str(cmd.get("backend", "auto")),
                                    poll_interval_sec=float(cmd.get("poll_interval_sec", 2.0) or 2.0),
                                )
                                send_ws_text(sock, json.dumps({"type": "live_screen:state", **res}), send_lock)
                            elif action == "stop":
                                res = hub_service.stop_live_screener()
                                send_ws_text(sock, json.dumps({"type": "live_screen:stopped", **res}), send_lock)
                            elif action == "status":
                                res = hub_service.get_live_screener_status()
                                send_ws_text(sock, json.dumps({"type": "live_screen:state", **res}), send_lock)
                        except Exception as exc:
                            send_ws_text(sock, json.dumps({"type": "error", "detail": str(exc)}), send_lock)
            except OSError:
                pass
            finally:
                stop_event.set()
                hub_service.unregister_live_screener_listener(listener_q)
            return

        if path == "/api/audit/verify":
            limit = int(query.get("limit", ["25"])[0] or 25)
            self._send_json(200, hub_service.verify_audit_chain(limit=limit))
            return

        if path == "/api/analytics/meta":
            self._send_json(200, hub_service.get_analytics_metadata())
            return

        if path == "/api/analytics/universes":
            self._send_json(200, hub_service.list_nse_universes())
            return

        if path == "/api/analytics/live-screener/status":
            self._send_json(200, hub_service.get_live_screener_status())
            return

        if path == "/api/analytics/indicators/verify":
            sym = query.get("symbol", [None])[0]
            exch = query.get("exchange", ["NSE"])[0] or "NSE"
            tf = query.get("timeframe", ["1day"])[0] or "1day"
            self._send_json(200, hub_service.verify_indicators(symbol=sym, exchange=exch, timeframe=tf))
            return

        if path == "/api/analytics/jobs":
            limit = int(query.get("limit", ["15"])[0] or 15)
            self._send_json(200, hub_service.list_analytics_jobs(limit=limit))
            return

        if path == "/api/analytics/risk":
            self._send_json(200, hub_service.get_risk_status())
            return

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "analytics" and parts[2] == "jobs":
            try:
                self._send_json(200, hub_service.get_analytics_job(parts[3]))
            except ValueError as exc:
                self._send_json(404, {"detail": str(exc)})
            return

        if path == "/ws/deployments":
            ws_key = self.headers.get("Sec-WebSocket-Key")
            if not ws_key or (self.headers.get("Upgrade") or "").lower() != "websocket":
                self._send_json(400, {"detail": "Expected WebSocket upgrade request."})
                return
            accept_val = make_ws_accept_key(ws_key)
            handshake = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
            ).encode("ascii")
            self.connection.sendall(handshake)
            listener_q = hub_service.register_deployments_listener()
            send_lock = threading.Lock()
            stop_event = threading.Event()
            sock = self.connection

            def _dep_writer() -> None:
                try:
                    initial = hub_service.list_deployments()
                    send_ws_text(sock, json.dumps({"type": "deployments:state", **initial}), send_lock)
                    while not stop_event.is_set():
                        try:
                            ev = listener_q.get(timeout=1.0)
                            send_ws_text(sock, json.dumps(ev), send_lock)
                        except queue.Empty:
                            continue
                except OSError:
                    stop_event.set()

            threading.Thread(target=_dep_writer, daemon=True).start()
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
            except OSError:
                pass
            finally:
                stop_event.set()
                hub_service.unregister_deployments_listener(listener_q)
            return

        if path == "/api/streak/meta":
            self._send_json(200, hub_service.get_streak_meta())
            return

        if path == "/api/streak/discover":
            cat = query.get("category", [None])[0]
            mode_f = query.get("mode", [None])[0]
            self._send_json(200, hub_service.list_discover(category=cat, mode=mode_f))
            return

        if path == "/api/streak/rules":
            kind = query.get("kind", [None])[0]
            self._send_json(200, hub_service.list_visual_rules(kind=kind))
            return

        if path == "/api/streak/baskets":
            self._send_json(200, hub_service.list_baskets())
            return

        if path == "/api/streak/backtest/runs":
            limit = int(query.get("limit", ["20"])[0] or 20)
            self._send_json(200, hub_service.list_backtest_runs(limit=limit))
            return

        if len(parts) == 5 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "backtest" and parts[4] == "ohlcv.csv":
            try:
                sym = query.get("symbol", [None])[0]
                filename, csv_body = hub_service.export_backtest_ohlcv_csv(run_id=parts[3], symbol=sym)
                raw = csv_body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except KeyError as exc:
                self._send_json(404, {"detail": str(exc)})
            except Exception as exc:
                self._send_json(400, {"detail": str(exc)})
            return

        if path == "/api/streak/deployments":
            self._send_json(200, hub_service.list_deployments())
            return

        if path == "/api/streak/plugins/source":
            try:
                kind = query.get("kind", ["screen"])[0]
                pid = query.get("id", [""])[0]
                self._send_json(200, hub_service.get_python_plugin_source(kind=kind, plugin_id=pid))
            except Exception as exc:
                self._send_json(400, {"detail": str(exc)})
            return

        if path == "/api/streak/scanners/alerts":
            sub_id = query.get("subscription_id", [None])[0]
            self._send_json(200, hub_service.list_scanner_alerts(subscription_id=sub_id))
            return

        if path == "/api/streak/oms/orders":
            dep_id = query.get("deployment_id", [None])[0]
            limit = int(query.get("limit", ["100"])[0] or 100)
            self._send_json(200, hub_service.list_oms_orders(deployment_id=dep_id, limit=limit))
            return

        if path == "/api/data/history":
            sym = query.get("symbol", [None])[0]
            exch = query.get("exchange", [None])[0]
            tf = query.get("timeframe", [None])[0]
            self._send_json(200, hub_service.get_asset_data_history(symbol=sym, exchange=exch, timeframe=tf))
            return

        if path == "/api/security/status":
            self._send_json(200, hub_service.get_security_status())
            return

        if path == "/api/notifications/telegram":
            self._send_json(200, hub_service.get_telegram_settings())
            return

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

        if path in {"/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        asset = resolve_spa_asset(path)
        if asset is not None:
            raw_bytes, content_type, cache_control = asset
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw_bytes)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(raw_bytes)
            return

        # Client-side SPA route fallback (never hijack /api, /ws, /auth, or missing /assets)
        if (
            not path.startswith(("/api/", "/ws/", "/auth/", "/assets/"))
            and "." not in path.rsplit("/", 1)[-1]
        ):
            body_html = render_spa_html(path)
            if body_html is not None:
                self._send_html(200, body_html)
                return

        self._send_json(404, {"detail": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._enforce_auth("POST", path):
            return

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
                exch = str(body.get("exchange") or "NSE")
                self._send_json(200, hub_service.run_preflight(symbols=symbols, exchange=exch))
                return

            if path == "/api/live/kill-switch":
                engaged = bool(body.get("engaged", False))
                reason = str(body.get("reason", ""))
                self._send_json(200, hub_service.set_kill_switch(engaged=engaged, reason=reason))
                return

            if path == "/api/analytics/reload":
                self._send_json(200, hub_service.reload_analytics_plugins())
                return

            if len(parts) == 5 and parts[0] == "api" and parts[1] == "analytics" and parts[2] == "jobs" and parts[4] == "cancel":
                try:
                    self._send_json(200, hub_service.cancel_analytics_job(parts[3]))
                except ValueError as exc:
                    self._send_json(404, {"detail": str(exc)})
                return

            if path == "/api/analytics/universes/sync":
                u_ids = body.get("universes") if isinstance(body.get("universes"), list) else None
                self._send_json(200, hub_service.sync_nse_universes(universe_ids=u_ids))
                return

            if path == "/api/analytics/live-screener/start":
                self._send_json(
                    200,
                    hub_service.start_live_screener(
                        symbols=body.get("symbols") if isinstance(body.get("symbols"), list) else None,
                        basket_id=body.get("basket_id"),
                        universe=str(body.get("universe", "CUSTOM")),
                        exchange=str(body.get("exchange", "NSE")),
                        timeframe=str(body.get("timeframe", "1day")),
                        screen_id=body.get("screen_id"),
                        preset=str(body.get("preset", "all_metrics")),
                        rule_def=body.get("rule_def") if isinstance(body.get("rule_def"), dict) else None,
                        params=body.get("params") if isinstance(body.get("params"), dict) else None,
                        provider=body.get("provider"),
                        backend=str(body.get("backend", "auto")),
                        poll_interval_sec=float(body.get("poll_interval_sec", 2.0) or 2.0),
                    ),
                )
                return

            if path == "/api/analytics/live-screener/stop":
                self._send_json(200, hub_service.stop_live_screener())
                return

            if path == "/api/analytics/screener":
                query = parse_qs(parsed.query)
                async_q = query.get("async_job", [None])[0]
                is_async = bool(body.get("async_job", False)) if async_q is None else (async_q.lower() == "true")
                self._send_json(
                    200,
                    hub_service.run_screener(
                        symbols=body.get("symbols") if isinstance(body.get("symbols"), list) else None,
                        basket_id=body.get("basket_id"),
                        universe=str(body.get("universe", "CUSTOM")),
                        exchange=str(body.get("exchange", "NSE")),
                        timeframe=str(body.get("timeframe", "1day")),
                        timeframes=body.get("timeframes") if isinstance(body.get("timeframes"), list) else None,
                        lookback_days=(
                            int(body["lookback_days"])
                            if body.get("lookback_days") not in (None, "", 0)
                            else None
                        ),
                        as_of_date=body.get("as_of_date"),
                        screen_id=body.get("screen_id"),
                        preset=str(body.get("preset", "all_metrics")),
                        rule_def=body.get("rule_def") if isinstance(body.get("rule_def"), dict) else None,
                        params=body.get("params") if isinstance(body.get("params"), dict) else None,
                        provider=body.get("provider"),
                        backend=str(body.get("backend", "auto")),
                        async_job=is_async,
                    ),
                )
                return

            if path == "/api/analytics/backtest":
                query = parse_qs(parsed.query)
                async_q = query.get("async_job", [None])[0]
                is_async = bool(body.get("async_job", False)) if async_q is None else (async_q.lower() == "true")
                self._send_json(
                    200,
                    hub_service.run_backtest(
                        symbol=str(body.get("symbol", "")),
                        exchange=str(body.get("exchange", "NSE")),
                        strategy=str(body.get("strategy", "ema_crossover")),
                        params=body.get("params") if isinstance(body.get("params"), dict) else None,
                        timeframe=str(body.get("timeframe", "1day")),
                        from_date=body.get("from_date"),
                        to_date=body.get("to_date"),
                        initial_capital=float(body.get("initial_capital", 500000.0) or 500000.0),
                        slippage_bps=5.0 if body.get("slippage_bps") in (None, "") else float(body.get("slippage_bps")),
                        fee_bps=5.0 if body.get("fee_bps") in (None, "") else float(body.get("fee_bps")),
                        stop_loss_pct=5.0 if body.get("stop_loss_pct") in (None, "") else float(body.get("stop_loss_pct")),
                        take_profit_pct=12.0 if body.get("take_profit_pct") in (None, "") else float(body.get("take_profit_pct")),
                        provider=body.get("provider"),
                        backend=str(body.get("backend", "auto")),
                        instrument_type=body.get("instrument_type"),
                        instrument_key=body.get("instrument_key"),
                        async_job=is_async,
                    ),
                )
                return

            if path == "/api/analytics/risk":
                self._send_json(200, hub_service.update_risk_limits(body))
                return

            # ── Phase 6: Full Streak Platform Routes ──────────────────────
            if path == "/api/streak/rules":
                self._send_json(200, hub_service.save_visual_rule(body))
                return

            if path == "/api/streak/rules/export-python":
                rule_obj = body.get("rule_def") if isinstance(body.get("rule_def"), dict) else body
                self._send_json(
                    200,
                    hub_service.export_visual_rule_to_python(
                        rule_obj,
                        save_to_disk=bool(body.get("save_to_disk", False)),
                    ),
                )
                return

            if path == "/api/streak/baskets":
                self._send_json(200, hub_service.save_basket(body))
                return

            if path == "/api/streak/charges/preview":
                self._send_json(
                    200,
                    hub_service.preview_charges(
                        entry_price=float(body.get("entry_price", 1000.0) or 1000.0),
                        exit_price=float(body.get("exit_price", 1025.0) or 1025.0),
                        quantity=int(body.get("quantity", 100) or 100),
                        profile=str(body.get("profile", "zerodha")),
                        segment=str(body.get("segment", "equity_intraday")),
                        direction=str(body.get("direction", "LONG")),
                        overrides=body.get("overrides") if isinstance(body.get("overrides"), dict) else None,
                    ),
                )
                return

            if path == "/api/streak/formula/validate":
                self._send_json(
                    200,
                    hub_service.validate_formula_expr(
                        str(body.get("expression") or body.get("expr") or ""),
                        expect_boolean=bool(body.get("expect_boolean", False)),
                    ),
                )
                return

            if path == "/api/streak/backtest":
                query = parse_qs(parsed.query)
                async_q = query.get("async_job", [None])[0]
                is_async = bool(body.get("async_job", False)) if async_q is None else (async_q.lower() == "true")
                self._send_json(
                    200,
                    hub_service.run_streak_backtest(
                        mode=str(body.get("mode", "portfolio")),
                        symbols=body.get("symbols") if isinstance(body.get("symbols"), list) else None,
                        basket_id=body.get("basket_id"),
                        universe=body.get("universe"),
                        exchange=str(body.get("exchange", "NSE")),
                        strategy=str(body.get("strategy", "ema_crossover")),
                        rule_def=body.get("rule_def") if isinstance(body.get("rule_def"), dict) else None,
                        params=body.get("params") if isinstance(body.get("params"), dict) else None,
                        timeframe=str(body.get("timeframe", "1day")),
                        from_date=body.get("from_date"),
                        to_date=body.get("to_date"),
                        start_time=body.get("start_time"),
                        end_time=body.get("end_time"),
                        holding_type=body.get("holding_type"),
                        strategy_cycle=int(body["strategy_cycle"]) if body.get("strategy_cycle") not in (None, "", "-") else None,
                        side=body.get("side"),
                        initial_capital=float(body.get("initial_capital", 500000.0) or 500000.0),
                        allocation_mode=str(body.get("allocation_mode", "shared_pool")),
                        max_open_positions=int(body.get("max_open_positions", 5) or 5),
                        risk_per_trade_pct=float(body.get("risk_per_trade_pct", 1.5) or 1.5),
                        signal_ranking=str(body.get("signal_ranking", "momentum")),
                        stop_loss_pct=3.0 if body.get("stop_loss_pct") in (None, "") else float(body.get("stop_loss_pct")),
                        take_profit_pct=7.0 if body.get("take_profit_pct") in (None, "") else float(body.get("take_profit_pct")),
                        trailing_sl_pct=0.0 if body.get("trailing_sl_pct") in (None, "") else float(body.get("trailing_sl_pct")),
                        charges_profile=str(body.get("charges_profile", "zerodha")),
                        segment=str(body.get("segment", "equity_intraday")),
                        charges_overrides=body.get("charges_overrides") if isinstance(body.get("charges_overrides"), dict) else None,
                        slippage_bps=5.0 if body.get("slippage_bps") in (None, "") else float(body.get("slippage_bps")),
                        provider=body.get("provider"),
                        backend=str(body.get("backend", "auto")),
                        async_job=is_async,
                    ),
                )
                return

            if path == "/api/streak/technicals":
                self._send_json(
                    200,
                    hub_service.analyze_technicals(
                        symbol=str(body.get("symbol", "RELIANCE")),
                        exchange=str(body.get("exchange", "NSE")),
                        timeframe=str(body.get("timeframe", "1day")),
                        provider=body.get("provider"),
                        backend=str(body.get("backend", "auto")),
                    ),
                )
                return

            if path == "/api/streak/options/template":
                self._send_json(
                    200,
                    hub_service.build_options_template(
                        template_id=str(body.get("template_id", "iron_condor")),
                        spot=float(body.get("spot", 24000.0) or 24000.0),
                        step=float(body.get("step", 100.0) or 100.0),
                        lot_size=int(body.get("lot_size", 75) or 75),
                        dte_days=float(body.get("dte_days", 7.0) or 7.0),
                        iv_pct=float(body.get("iv_pct", 15.0) or 15.0),
                        charges_profile=str(body.get("charges_profile", "zerodha")),
                    ),
                )
                return

            if path == "/api/streak/options/analyze":
                self._send_json(
                    200,
                    hub_service.analyze_options(
                        spot=float(body.get("spot", 24000.0) or 24000.0),
                        legs=body.get("legs") if isinstance(body.get("legs"), list) else [],
                        dte_days=float(body.get("dte_days", 7.0) or 7.0),
                        risk_free_rate=float(body.get("risk_free_rate", 0.068) or 0.068),
                        charges_profile=str(body.get("charges_profile", "zerodha")),
                        charges_overrides=body.get("charges_overrides") if isinstance(body.get("charges_overrides"), dict) else None,
                    ),
                )
                return

            if path == "/api/streak/deployments/start":
                self._send_json(200, hub_service.start_deployment(body))
                return

            if len(parts) == 5 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "deployments" and parts[4] == "action":
                self._send_json(200, hub_service.control_deployment(parts[3], str(body.get("action", ""))))
                return

            if len(parts) == 6 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "deployments" and parts[3] == "alerts" and parts[5] == "action":
                self._send_json(200, hub_service.resolve_deployment_alert(parts[4], str(body.get("action", ""))))
                return

            if path == "/api/streak/plugins/python":
                self._send_json(
                    200,
                    hub_service.save_python_plugin(
                        kind=str(body.get("kind", "screen")),
                        filename=str(body.get("filename", "")),
                        code=str(body.get("code", "")),
                    ),
                )
                return

            if path == "/api/streak/scanners/alerts":
                self._send_json(200, hub_service.save_scanner_alert(body))
                return

            if len(parts) == 6 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "scanners" and parts[3] == "alerts" and parts[5] == "action":
                self._send_json(200, hub_service.control_scanner_alert(parts[4], str(body.get("action", ""))))
                return

            if path == "/api/streak/orders/quick":
                self._send_json(200, hub_service.place_quick_order(body))
                return

            if path == "/api/streak/deployments/reconcile":
                dep_id = body.get("deployment_id") if isinstance(body, dict) else None
                self._send_json(200, hub_service.reconcile_deployments(deployment_id=dep_id))
                return

            if path == "/api/security/claim":
                client_ip = str(self.client_address[0] if self.client_address else "local")
                try:
                    self._send_json(
                        200,
                        hub_service.claim_admin_passcode(
                            str(body.get("passcode", "")),
                            bootstrap_code=str(body.get("bootstrap_code") or "") or None,
                            client_key=client_ip,
                        ),
                    )
                except RateLimitError as exc:
                    self._send_json(429, {"detail": str(exc), "retry_after_sec": exc.retry_after_sec})
                except PermissionError as exc:
                    self._send_json(401, {"detail": str(exc)})
                return

            if path == "/api/security/login":
                client_ip = str(self.client_address[0] if self.client_address else "local")
                try:
                    self._send_json(
                        200,
                        hub_service.login_admin(
                            str(body.get("passcode") or body.get("token") or ""),
                            client_key=client_ip,
                        ),
                    )
                except RateLimitError as exc:
                    self._send_json(429, {"detail": str(exc), "retry_after_sec": exc.retry_after_sec})
                except PermissionError as exc:
                    self._send_json(401, {"detail": str(exc)})
                return

            if path == "/api/notifications/telegram":
                self._send_json(200, hub_service.update_telegram_settings(body))
                return

            if path == "/api/notifications/telegram/test":
                self._send_json(200, hub_service.send_telegram_test())
                return

            self._send_json(404, {"detail": "Not Found"})
        except ValueError as exc:
            self._send_json(400, {"detail": str(exc)})
        except Exception as exc:
            self._send_json(500, {"detail": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._enforce_auth("PUT", parsed.path):
            return
        self._send_json(405, {"detail": "Method Not Allowed"})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._enforce_auth("PATCH", parsed.path):
            return
        self._send_json(405, {"detail": "Method Not Allowed"})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._enforce_auth("DELETE", parsed.path):
            return
        parts = parsed.path.strip("/").split("/")
        try:
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "rules":
                self._send_json(200, hub_service.delete_visual_rule(parts[3]))
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "baskets":
                self._send_json(200, hub_service.delete_basket(parts[3]))
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "deployments":
                self._send_json(200, hub_service.control_deployment(parts[3], "delete"))
                return
            if len(parts) == 5 and parts[0] == "api" and parts[1] == "streak" and parts[2] == "scanners" and parts[3] == "alerts":
                self._send_json(200, hub_service.delete_scanner_alert(parts[4]))
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
    ensure_spa_bundle(auto_build=True)
    url = f"http://{host}:{port}"
    logger.info("Starting AlgoDesk Broker & Data Hub at %s", url)

    try:
        import uvicorn  # type: ignore
        from web.app import app, request_shutdown, reset_shutdown

        class _GracefulUvicornServer(uvicorn.Server):
            def handle_exit(self, sig: int, frame: Any) -> None:
                request_shutdown()
                super().handle_exit(sig, frame)

        reset_shutdown()
        if open_browser:
            webbrowser.open(url)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            timeout_graceful_shutdown=2,
        )
        server = _GracefulUvicornServer(config)
        try:
            server.run()
        except KeyboardInterrupt:
            request_shutdown()
        return
    except ImportError:
        logger.info("FastAPI/Uvicorn not installed; running lightweight Python stdlib server.")

    hub_service.start_background_services()
    server = _AlgoDeskHTTPServer((host, port), _StdlibBrokerHandler)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub_service.stop_background_services()
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
