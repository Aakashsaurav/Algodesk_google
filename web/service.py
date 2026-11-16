"""
web/service.py
==============
Core application service powering AlgoDesk's Broker & Data Connectivity Hub.

Features:
- Auto-detects UPSTOX_API_KEY, UPSTOX_API_SECRET, and UPSTOX_REDIRECT_URI from
  .env (live reload on file changes) or OS Keychain without exposing secrets.
- Supports automatic background OAuth callback capture for BOTH same-origin
  (/auth/upstox/callback) and separate local loopback ports (e.g.
  https://127.0.0.1:5000/ used in existing Upstox Developer App configs),
  while preserving manual URL/code exchange fallback.
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from broker.base import BrokerHealthSnapshot, ConnectionState
from broker.registry import BrokerRegistry, SelectionStore
from broker.upstox.auth import auth_manager
from config import IST_TZ, config

logger = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    clean = (key or "").strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "••••"
    return f"{clean[:4]}{'•' * 4}{clean[-3:]}"


@dataclass
class _AutoCaptureSession:
    broker_id: str
    status: str  # "listening" | "exchanging" | "connected" | "error" | "timeout" | "cancelled"
    capture_mode: str  # "loopback_listener" | "web_callback" | "manual_only"
    redirect_uri: str
    message: str = ""
    server: Any = None
    stop_event: Optional[threading.Event] = None
    broker_snapshot: Optional[Dict[str, Any]] = None


class BrokerHubService:
    """Thread-safe orchestration service for broker probing, OAuth auto-capture, and routing."""

    def __init__(self, selection_store: Optional[SelectionStore] = None) -> None:
        self._store = selection_store or SelectionStore()
        self._lock = threading.RLock()
        self._cached_snapshots: Dict[str, BrokerHealthSnapshot] = {}
        self._capture_sessions: Dict[str, _AutoCaptureSession] = {}

    def _credential_summary(self) -> Dict[str, Any]:
        config.reload_if_changed()
        key = config.UPSTOX_API_KEY
        secret = config.UPSTOX_API_SECRET
        redirect = config.UPSTOX_REDIRECT_URI
        return {
            "upstox": {
                "has_api_key": bool(key),
                "has_api_secret": bool(secret),
                "api_key_preview": _mask_key(key),
                "redirect_uri": redirect,
                "key_source": config.credential_source("UPSTOX_API_KEY", "upstox_api_key"),
                "secret_source": config.credential_source("UPSTOX_API_SECRET", "upstox_api_secret"),
                "redirect_source": config.credential_source("UPSTOX_REDIRECT_URI", "upstox_redirect_uri"),
            }
        }

    def _probe_single(self, broker_id: str, timeout: float = 3.5) -> BrokerHealthSnapshot:
        config.reload_if_changed()
        adapter = BrokerRegistry.get(broker_id)
        snap = adapter.health_check(timeout=timeout)
        with self._lock:
            self._cached_snapshots[broker_id] = snap
        return snap

    def probe_all(self, timeout: float = 3.5) -> List[Dict[str, Any]]:
        """Probe every registered broker/data adapter concurrently."""
        config.reload_if_changed()
        broker_ids = BrokerRegistry.list_available()
        results: Dict[str, BrokerHealthSnapshot] = {}

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(broker_ids)))) as pool:
            future_map = {
                pool.submit(self._probe_single, bid, timeout): bid
                for bid in broker_ids
            }
            for fut in as_completed(future_map):
                bid = future_map[fut]
                try:
                    results[bid] = fut.result()
                except Exception as exc:
                    adapter = BrokerRegistry.get(bid)
                    results[bid] = BrokerHealthSnapshot(
                        broker_id=bid,
                        display_name=adapter.display_name,
                        stage=adapter.stage,
                        state=ConnectionState.ERROR,
                        capabilities=adapter.capabilities,
                        message=f"Probe failed: {exc}",
                        can_reconnect=adapter.capabilities.requires_oauth,
                    )

        return [results[bid].to_dict() for bid in broker_ids]

    def probe_one(self, broker_id: str, timeout: float = 3.5) -> Dict[str, Any]:
        snap = self._probe_single(broker_id, timeout=timeout)
        return snap.to_dict()

    def get_overview(self, refresh: bool = True) -> Dict[str, Any]:
        config.reload_if_changed()
        if refresh or not self._cached_snapshots:
            brokers = self.probe_all()
        else:
            broker_ids = BrokerRegistry.list_available()
            brokers = [self._cached_snapshots[bid].to_dict() for bid in broker_ids if bid in self._cached_snapshots]

        selections = self._store.load()
        return {
            "brokers": brokers,
            "selections": selections,
            "credentials": self._credential_summary(),
            "runtime": {
                "python_version": platform.python_version(),
                "machine": platform.machine(),
                "system": platform.system(),
                "ist_time": datetime.now(tz=IST_TZ).strftime("%d %b %Y, %H:%M:%S IST"),
                "eager_instruments_loaded": False,
                "executable": sys.executable,
            },
        }

    def save_selection(self, trading_broker: str, data_connection: str) -> Dict[str, Any]:
        t_id = trading_broker.strip().lower()
        d_id = data_connection.strip().lower()

        t_adapter = BrokerRegistry.get(t_id)
        d_adapter = BrokerRegistry.get(d_id)

        if t_adapter.stage == "planned":
            raise ValueError(f"{t_adapter.display_name} is planned for Phase 2 and cannot be activated for trading yet.")
        if not t_adapter.capabilities.supports_trading:
            raise ValueError(f"{t_adapter.display_name} does not support order execution.")

        if d_adapter.stage == "planned":
            raise ValueError(f"{d_adapter.display_name} is planned for Phase 2 and cannot be activated for market data yet.")
        if not d_adapter.capabilities.supports_market_data:
            raise ValueError(f"{d_adapter.display_name} does not support market data.")

        saved = self._store.save(trading_broker=t_id, data_connection=d_id)
        return {"ok": True, "selections": saved}

    def save_credentials(
        self,
        broker_id: str,
        api_key: str,
        api_secret: str,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        if bid != "upstox":
            raise ValueError(f"Credential configuration is not active for '{broker_id}' yet.")
        auth_manager.save_credentials(api_key=api_key, api_secret=api_secret, redirect_uri=redirect_uri)
        snap = self.probe_one(bid)
        return {
            "ok": True,
            "broker": snap,
            "credentials": self._credential_summary(),
        }

    def _stop_capture_session_locked(self, broker_id: str) -> None:
        session = self._capture_sessions.get(broker_id)
        if not session:
            return
        if session.stop_event is not None:
            session.stop_event.set()
        if session.server is not None:
            try:
                session.server.shutdown()
            except Exception:
                pass
            try:
                session.server.server_close()
            except Exception:
                pass
            session.server = None

    @staticmethod
    def _needs_separate_loopback_listener(redirect_uri: str, server_port: Optional[int]) -> bool:
        parsed = urlparse(redirect_uri)
        host = (parsed.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost"}:
            return False
        target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if server_port is not None and target_port == server_port and "/auth/" in (parsed.path or ""):
            return False
        return True

    def start_reconnect(
        self,
        broker_id: str,
        redirect_uri: Optional[str] = None,
        server_port: Optional[int] = None,
    ) -> Dict[str, Any]:
        config.reload_if_changed()
        bid = broker_id.strip().lower()
        adapter = BrokerRegistry.get(bid)
        if adapter.stage == "planned":
            raise ValueError(f"{adapter.display_name} is planned for Phase 2.")

        if not adapter.capabilities.requires_oauth:
            snap = self.probe_one(bid)
            return {"mode": "probe", "broker": snap}

        effective_redirect = (redirect_uri or config.UPSTOX_REDIRECT_URI).strip()

        if bid == "upstox" and not auth_manager.has_credentials():
            return {
                "mode": "needs_credentials",
                "broker_id": bid,
                "redirect_uri": effective_redirect,
                "credentials": self._credential_summary().get("upstox"),
                "message": "No Upstox API Key/Secret found in .env or macOS Keychain. Configure them to continue.",
            }

        login_url = adapter.get_login_url(redirect_uri=effective_redirect)

        with self._lock:
            self._stop_capture_session_locked(bid)

        capture_mode = "web_callback"
        capture_msg = f"Listening on {effective_redirect} for automatic OAuth callback..."

        if bid == "upstox" and self._needs_separate_loopback_listener(effective_redirect, server_port):
            try:
                server, event, captured = auth_manager.build_redirect_listener(redirect_uri=effective_redirect)
                stop_event = threading.Event()
                session = _AutoCaptureSession(
                    broker_id=bid,
                    status="listening",
                    capture_mode="loopback_listener",
                    redirect_uri=effective_redirect,
                    message=f"Listening locally on {effective_redirect} for automatic browser redirect...",
                    server=server,
                    stop_event=stop_event,
                )
                with self._lock:
                    self._capture_sessions[bid] = session

                def _loopback_worker() -> None:
                    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
                    serve_thread.start()
                    try:
                        # Poll stop_event and callback event every 0.25s up to 180s
                        deadline = 180.0
                        elapsed = 0.0
                        while elapsed < deadline and not stop_event.is_set():
                            if event.wait(timeout=0.25):
                                break
                            elapsed += 0.25

                        if stop_event.is_set():
                            return

                        if not event.is_set() or not captured.get("url"):
                            with self._lock:
                                if bid in self._capture_sessions and self._capture_sessions[bid] is session:
                                    session.status = "timeout"
                                    session.message = "Timed out waiting for browser redirect. You can retry or paste the URL manually."
                            return

                        with self._lock:
                            session.status = "exchanging"
                            session.message = "Authorization code captured automatically. Exchanging for token..."

                        res = self.complete_reconnect(
                            broker_id=bid,
                            code_or_url=str(captured["url"]),
                            redirect_uri=effective_redirect,
                        )
                        with self._lock:
                            if res.get("ok"):
                                session.status = "connected"
                                session.message = "Connected automatically and verified with Upstox."
                                session.broker_snapshot = res.get("broker")
                            else:
                                session.status = "error"
                                session.message = res.get("broker", {}).get("message") or "Token verification failed."
                                session.broker_snapshot = res.get("broker")
                    except Exception as exc:
                        with self._lock:
                            session.status = "error"
                            session.message = f"Auto-capture token exchange failed: {exc}"
                    finally:
                        try:
                            server.shutdown()
                        except Exception:
                            pass
                        try:
                            server.server_close()
                        except Exception:
                            pass
                        session.server = None

                threading.Thread(target=_loopback_worker, name="UpstoxAutoCapture", daemon=True).start()
                capture_mode = "loopback_listener"
                capture_msg = session.message
            except Exception as exc:
                logger.warning("Could not bind loopback listener for %s: %s", effective_redirect, exc)
                capture_mode = "manual_only"
                capture_msg = f"Could not auto-bind {effective_redirect} ({exc}). Complete login and paste redirect URL below."
                with self._lock:
                    self._capture_sessions[bid] = _AutoCaptureSession(
                        broker_id=bid,
                        status="listening",
                        capture_mode=capture_mode,
                        redirect_uri=effective_redirect,
                        message=capture_msg,
                    )
        else:
            with self._lock:
                self._capture_sessions[bid] = _AutoCaptureSession(
                    broker_id=bid,
                    status="listening",
                    capture_mode="web_callback",
                    redirect_uri=effective_redirect,
                    message=capture_msg,
                )

        return {
            "mode": "oauth",
            "broker_id": bid,
            "login_url": login_url,
            "redirect_uri": effective_redirect,
            "capture_mode": capture_mode,
            "capture_status": "listening",
            "message": capture_msg,
            "credentials": self._credential_summary().get("upstox"),
        }

    def get_reconnect_status(self, broker_id: str) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        with self._lock:
            session = self._capture_sessions.get(bid)
            if not session:
                return {
                    "broker_id": bid,
                    "status": "idle",
                    "capture_mode": "none",
                    "message": "",
                }
            return {
                "broker_id": bid,
                "status": session.status,
                "capture_mode": session.capture_mode,
                "redirect_uri": session.redirect_uri,
                "message": session.message,
                "broker": session.broker_snapshot,
            }

    def cancel_reconnect(self, broker_id: str) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        with self._lock:
            session = self._capture_sessions.get(bid)
            self._stop_capture_session_locked(bid)
            if session and session.status == "listening":
                session.status = "cancelled"
                session.message = "Auto-capture cancelled."
        return {"ok": True}

    def complete_reconnect(
        self,
        broker_id: str,
        code_or_url: str,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        config.reload_if_changed()
        bid = broker_id.strip().lower()
        adapter = BrokerRegistry.get(bid)
        snap = adapter.complete_auth(code_or_url=code_or_url, redirect_uri=redirect_uri)
        snap_dict = snap.to_dict()
        with self._lock:
            self._cached_snapshots[bid] = snap
            session = self._capture_sessions.get(bid)
            if session:
                session.status = "connected" if snap.is_connected else "error"
                session.message = snap.message
                session.broker_snapshot = snap_dict
                self._stop_capture_session_locked(bid)
        return {"ok": snap.is_connected, "broker": snap_dict}

    def disconnect_broker(self, broker_id: str) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        with self._lock:
            self._stop_capture_session_locked(bid)
        adapter = BrokerRegistry.get(bid)
        adapter.disconnect()
        snap = self.probe_one(bid)
        return {"ok": True, "broker": snap}


hub_service = BrokerHubService()

__all__ = ["BrokerHubService", "hub_service"]
