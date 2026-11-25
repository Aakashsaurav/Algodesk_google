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
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from backtest.engine import backtest_engine
from backtest.portfolio import VALID_ALLOCATION_MODES, VALID_SIGNAL_RANKINGS, portfolio_backtest_engine
from backtest.registry import strategy_registry
from baskets.store import basket_store
from broker.base import BrokerHealthSnapshot, ConnectionState, OrderRequest, OrderStatus
from broker.registry import BrokerRegistry, SelectionStore
from broker.upstox.auth import auth_manager as upstox_auth_manager
from broker.zerodha.auth import zerodha_auth_manager
from charges.calculator import charges_engine, list_charge_presets
from config import IST_TZ, config
from core.audit import audit_trail
from core.feed_manager import feed_manager
from core.historical import historical_data_manager
from core.instruments import instrument_index
from core.jobs import analytics_job_runner
from core.preflight import PreflightChecker, kill_switch_controller
from deploy.manager import deployment_manager
from discover.catalog import list_discover_catalog
from execution.oms import oms_manager
from indicators.toolkit import available_backends, backend_diagnostics, refresh_backend_cache
from notifications.telegram import telegram_notifier
from options.payoff import (
    OPTIONS_TEMPLATES,
    analyze_options_strategy,
    build_template_legs,
    list_options_templates,
)
from risk.limits import risk_limit_manager, risk_watchdog
from rules.engine import (
    SUPPORTED_OPERANDS,
    SUPPORTED_OPERATORS,
    export_rule_to_python,
    visual_rule_store,
)
from screener.alerts import scanner_alerts_manager
from screener.engine import screener_engine
from screener.live_engine import live_screener_engine
from screener.nse_universe_sync import nse_universe_sync
from screener.registry import screen_registry
from screener.universes import DEFAULT_WATCHLIST, universe_resolver
from technicals.analyzer import technicals_analyzer
from web.security import (
    PLUGIN_TRUST_NOTICE,
    is_live_trading_enabled,
    security_manager,
    validate_python_plugin_ast,
)

logger = logging.getLogger(__name__)

_AUTH_MANAGERS = {
    "upstox": upstox_auth_manager,
    "zerodha": zerodha_auth_manager,
}


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

    @staticmethod
    def _auth_for(broker_id: str):
        bid = broker_id.strip().lower()
        if bid not in _AUTH_MANAGERS:
            raise ValueError(f"Credential & OAuth management is not supported for '{broker_id}'.")
        return _AUTH_MANAGERS[bid]

    def _broker_cred_entry(self, broker_id: str) -> Dict[str, Any]:
        prefix = broker_id.upper()
        acct_prefix = broker_id.lower()
        auth = self._auth_for(broker_id)
        key = auth.api_key
        secret = auth.api_secret
        redirect = auth.redirect_uri
        return {
            "has_api_key": bool(key),
            "has_api_secret": bool(secret),
            "api_key_preview": _mask_key(key),
            "redirect_uri": redirect,
            "key_source": config.credential_source(f"{prefix}_API_KEY", f"{acct_prefix}_api_key"),
            "secret_source": config.credential_source(f"{prefix}_API_SECRET", f"{acct_prefix}_api_secret"),
            "redirect_source": config.credential_source(f"{prefix}_REDIRECT_URI", f"{acct_prefix}_redirect_uri"),
        }

    def _credential_summary(self) -> Dict[str, Any]:
        config.reload_if_changed()
        return {
            "upstox": self._broker_cred_entry("upstox"),
            "zerodha": self._broker_cred_entry("zerodha"),
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
            "security": security_manager.status(),
            "live_trading_enabled": is_live_trading_enabled(),
            "telegram": telegram_notifier.get_status(),
            "watchdog": risk_watchdog.status(),
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
        feed_manager.sync_primary(d_id)
        return {"ok": True, "selections": saved}

    def save_credentials(
        self,
        broker_id: str,
        api_key: str,
        api_secret: str,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        auth = self._auth_for(bid)
        auth.save_credentials(api_key=api_key, api_secret=api_secret, redirect_uri=redirect_uri)
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
            raise ValueError(f"{adapter.display_name} is planned for future rollout.")

        if not adapter.capabilities.requires_oauth:
            snap = self.probe_one(bid)
            return {"mode": "probe", "broker": snap}

        auth = self._auth_for(bid)
        effective_redirect = (redirect_uri or auth.redirect_uri).strip()

        if not auth.has_credentials():
            return {
                "mode": "needs_credentials",
                "broker_id": bid,
                "redirect_uri": effective_redirect,
                "credentials": self._credential_summary().get(bid),
                "message": f"No {adapter.display_name} API Key/Secret found in .env or local vault. Configure them to continue.",
            }

        login_url = adapter.get_login_url(redirect_uri=effective_redirect)

        with self._lock:
            self._stop_capture_session_locked(bid)

        capture_mode = "web_callback"
        capture_msg = f"Listening on {effective_redirect} for automatic OAuth callback..."

        if self._needs_separate_loopback_listener(effective_redirect, server_port):
            try:
                server, event, captured = auth.build_redirect_listener(redirect_uri=effective_redirect)
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
                            session.message = "Authorization token captured automatically. Exchanging for session..."

                        res = self.complete_reconnect(
                            broker_id=bid,
                            code_or_url=str(captured["url"]),
                            redirect_uri=effective_redirect,
                        )
                        with self._lock:
                            if res.get("ok"):
                                session.status = "connected"
                                session.message = f"Connected automatically and verified with {adapter.display_name}."
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

                threading.Thread(target=_loopback_worker, name=f"{bid.title()}AutoCapture", daemon=True).start()
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
            "credentials": self._credential_summary().get(bid),
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
        if snap.is_connected:
            feed_manager.clear_primary_error(bid)
        return {"ok": snap.is_connected, "broker": snap_dict}

    def disconnect_broker(self, broker_id: str) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        with self._lock:
            self._stop_capture_session_locked(bid)
        adapter = BrokerRegistry.get(bid)
        adapter.disconnect()
        snap = self.probe_one(bid)
        return {"ok": True, "broker": snap}

    # ── Phase 3: SQLite Instrument Index & Gap-Only Historical Data ───────

    def instrument_stats(self) -> Dict[str, Any]:
        return instrument_index.stats()

    def search_instruments(
        self,
        query: str = "",
        broker_id: Optional[str] = None,
        exchange: Optional[str] = None,
        instrument_type: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        limit: int = 25,
    ) -> Dict[str, Any]:
        results = instrument_index.search(
            query=query,
            broker_id=broker_id,
            exchange=exchange,
            instrument_type=instrument_type,
            expiry=expiry,
            strike=strike,
            limit=limit,
        )
        return {"ok": True, "count": len(results), "results": results, "stats": instrument_index.stats()}

    def list_fno_contracts(
        self,
        underlying: str = "",
        exchange: str = "NFO",
        contract_type: str = "FO",
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        broker_id: Optional[str] = None,
        limit: int = 40,
    ) -> Dict[str, Any]:
        res = instrument_index.list_fno_contracts(
            underlying=underlying,
            exchange=exchange,
            contract_type=contract_type,
            expiry=expiry,
            strike=strike,
            broker_id=broker_id,
            limit=limit,
        )
        res["stats"] = instrument_index.stats()
        return res

    def sync_instruments(self, broker_id: str) -> Dict[str, Any]:
        bid = (broker_id or "upstox").strip().lower()
        if bid == "upstox":
            res = instrument_index.sync_upstox_master()
        elif bid == "zerodha":
            res = instrument_index.sync_zerodha_master()
        else:
            raise ValueError(f"Instrument master sync is supported for 'upstox' or 'zerodha', not '{broker_id}'.")
        res["stats"] = instrument_index.stats()
        return res

    def download_historical(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        provider: Optional[str] = None,
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        selected_provider = (provider or self._store.load().get("data_connection") or "yfinance").strip().lower()
        return historical_data_manager.download(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            provider=selected_provider,
            instrument_type=instrument_type,
            instrument_key=instrument_key,
        )

    def get_historical_records(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        view: str = "snapshot",
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict[str, Any]:
        return historical_data_manager.get_records(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            view=view,
            limit=limit,
            offset=offset,
        )

    def list_historical_catalog(self) -> Dict[str, Any]:
        return {"ok": True, "datasets": historical_data_manager.list_catalog()}

    def export_historical_csv(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> str:
        return historical_data_manager.export_csv(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
        )

    # ── Phase 4: Hot-Standby Feed Failover, Preflight & SEBI Kill Switch ─

    def get_live_status(self, audit_limit: int = 20) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        return {
            "ok": True,
            "selections": sel,
            "feeds": feed_manager.status(),
            "kill_switch": kill_switch_controller.status(),
            "audit_verify": audit_trail.verify_chain(),
            "audit_events": audit_trail.list_events(limit=audit_limit),
        }

    def poll_live_feeds(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        res = feed_manager.poll_ticks(symbols=symbols)
        return {
            **res,
            "audit_verify": audit_trail.verify_chain(),
            "audit_events": audit_trail.list_events(limit=20),
        }

    def trigger_feed_failover(self, reason: Optional[str] = None) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        clean_reason = (reason or "").strip() or "Operator failover drill / primary drop"
        res = feed_manager.trigger_failover(reason=clean_reason)
        return {
            **res,
            "audit_verify": audit_trail.verify_chain(),
            "audit_events": audit_trail.list_events(limit=20),
        }

    def recover_primary_feed(self) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        res = feed_manager.recover_primary()
        return {
            **res,
            "audit_verify": audit_trail.verify_chain(),
            "audit_events": audit_trail.list_events(limit=20),
        }

    def get_subscriptions(self) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        status = feed_manager.status()
        return {
            "ok": True,
            "subscriptions": status["subscriptions"],
            "feeds": status,
        }

    def subscribe_asset(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        return feed_manager.subscribe(
            symbol=symbol,
            exchange=exchange,
            instrument_type=instrument_type or "",
            instrument_key=instrument_key or "",
        )

    def unsubscribe_asset(self, *, symbol: str, exchange: Optional[str] = None) -> Dict[str, Any]:
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        return feed_manager.unsubscribe(symbol=symbol, exchange=exchange)

    def register_live_listener(self):
        sel = self._store.load()
        feed_manager.sync_primary(sel.get("data_connection", "upstox"))
        return feed_manager.register_listener()

    def unregister_live_listener(self, listener_queue) -> None:
        feed_manager.unregister_listener(listener_queue)

    def run_preflight(
        self,
        symbols: Optional[List[str]] = None,
        exchange: str = "NSE",
    ) -> Dict[str, Any]:
        sel = self._store.load()
        res = PreflightChecker.run_all(
            trading_broker=sel.get("trading_broker", "upstox"),
            data_connection=sel.get("data_connection", "upstox"),
            symbols=symbols,
            exchange=exchange,
        )
        return {
            **res,
            "kill_switch": kill_switch_controller.status(),
            "live_trading_enabled": is_live_trading_enabled(),
            "audit_verify": audit_trail.verify_chain(),
            "audit_events": audit_trail.list_events(limit=20),
        }

    def set_kill_switch(self, engaged: bool, reason: str = "") -> Dict[str, Any]:
        sel = self._store.load()
        t_broker = sel.get("trading_broker", "upstox")
        if engaged:
            event = kill_switch_controller.engage(
                reason=reason.strip() or "Manual emergency stop triggered from dashboard",
                actor="operator",
                trading_broker=t_broker,
            )
        else:
            event = kill_switch_controller.reset(
                reason=reason.strip() or "Operator cleared emergency kill switch from dashboard",
                actor="operator",
                trading_broker=t_broker,
            )
        return {
            **self.get_live_status(audit_limit=20),
            "event": event,
        }

    def verify_audit_chain(self, limit: int = 25) -> Dict[str, Any]:
        return {
            "ok": True,
            "verification": audit_trail.verify_chain(),
            "events": audit_trail.list_events(limit=limit),
        }

    # ── Phase 5: Screener, O(N) Backtester, Risk Console & Async Jobs ────

    def get_analytics_metadata(self) -> Dict[str, Any]:
        from core.historical import VALID_TIMEFRAMES

        sel = self._store.load()
        screens = screen_registry.list_screens()
        strat_specs = strategy_registry.list_strategies()
        timeframes = [
            {"id": k, "label": str(v["label"]), "base": str(v["base"]), "resampled": k != v["base"]}
            for k, v in VALID_TIMEFRAMES.items()
        ]
        return {
            "ok": True,
            "selections": sel,
            "presets": {s["id"]: s["name"] for s in screens},
            "screens": screens,
            "strategies": {s["id"]: s["name"] for s in strat_specs},
            "strategy_specs": strat_specs,
            "universes": universe_resolver.list_universes(),
            "timeframes": timeframes,
            "indicator_backends": available_backends(),
            "indicator_backend_diagnostics": backend_diagnostics(),
            "storage": historical_data_manager.storage_info(),
            "file_errors": {
                "screens": screen_registry.get_errors(),
                "strategies": strategy_registry.get_errors(),
            },
            "screens_dir": str(screen_registry.screens_dir),
            "strategies_dir": str(strategy_registry.strategies_dir),
            "universes_dir": str(nse_universe_sync.universes_dir),
            "default_watchlist": DEFAULT_WATCHLIST,
        }

    def list_nse_universes(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "universes_dir": str(nse_universe_sync.universes_dir),
            "universes": universe_resolver.list_universes(),
        }

    def sync_nse_universes(self, universe_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        summary = nse_universe_sync.sync_all(universe_ids=universe_ids)
        return {
            **summary,
            "universes": universe_resolver.list_universes(),
        }

    def start_live_screener(
        self,
        *,
        symbols: Optional[List[str]] = None,
        basket_id: Optional[str] = None,
        universe: str = "CUSTOM",
        exchange: str = "NSE",
        timeframe: str = "1day",
        screen_id: Optional[str] = None,
        preset: str = "all_metrics",
        rule_def: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        backend: str = "auto",
        poll_interval_sec: float = 2.0,
    ) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (provider or sel.get("data_connection") or "yfinance").strip().lower()
        eff_symbols = symbols
        eff_universe = (universe or "CUSTOM").strip().upper()
        if basket_id:
            b = basket_store.get(basket_id)
            if b and b.get("symbols"):
                eff_symbols = list(b["symbols"])
                eff_universe = "CUSTOM"
        return live_screener_engine.start(
            symbols=eff_symbols,
            universe=eff_universe,
            exchange=exchange,
            timeframe=timeframe,
            screen_id=screen_id or preset or "all_metrics",
            rule_def=rule_def,
            params=params,
            provider=eff_provider,
            backend=backend,
            poll_interval_sec=poll_interval_sec,
        )

    def stop_live_screener(self) -> Dict[str, Any]:
        return live_screener_engine.stop()

    def get_live_screener_status(self) -> Dict[str, Any]:
        return live_screener_engine.status()

    def register_live_screener_listener(self):
        return live_screener_engine.register_listener()

    def unregister_live_screener_listener(self, q) -> None:
        live_screener_engine.unregister_listener(q)

    def verify_indicators(
        self,
        *,
        symbol: Optional[str] = None,
        exchange: str = "NSE",
        timeframe: str = "1day",
    ) -> Dict[str, Any]:
        from indicators.verify import cross_check_backends, verify_golden_indicators

        candles = None
        if symbol and symbol.strip():
            rec = historical_data_manager.get_records(
                symbol=symbol.strip(),
                exchange=exchange,
                timeframe=timeframe,
                view="full",
            )
            candles = rec.get("rows", [])
        golden = verify_golden_indicators()
        parity = cross_check_backends(candles)
        return {
            "ok": bool(golden["ok"]),
            "golden_reference": golden,
            "backend_parity": parity,
        }

    def reload_analytics_plugins(self) -> Dict[str, Any]:
        refresh_backend_cache()
        screen_registry.refresh_if_changed(force=True)
        strategy_registry.refresh_if_changed(force=True)
        return self.get_analytics_metadata()

    def run_screener(
        self,
        *,
        symbols: Optional[List[str]] = None,
        basket_id: Optional[str] = None,
        universe: str = "CUSTOM",
        exchange: str = "NSE",
        timeframe: str = "1day",
        timeframes: Optional[List[str]] = None,
        lookback_days: Optional[int] = None,
        as_of_date: Optional[str] = None,
        screen_id: Optional[str] = None,
        preset: str = "all_metrics",
        rule_def: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        backend: str = "auto",
        async_job: bool = False,
    ) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (provider or sel.get("data_connection") or "yfinance").strip().lower()
        target_screen = screen_id or preset or "all_metrics"
        eff_symbols = symbols
        eff_universe = (universe or "CUSTOM").strip().upper()
        if basket_id:
            b = basket_store.get(basket_id)
            if b and b.get("symbols"):
                eff_symbols = list(b["symbols"])
                eff_universe = "CUSTOM"
        meta = {
            "screen_id": target_screen,
            "preset": target_screen,
            "universe": eff_universe,
            "as_of_date": as_of_date or "LATEST",
            "provider": eff_provider,
            "exchange": exchange,
            "timeframe": ",".join(timeframes) if timeframes else timeframe,
        }

        def _task(progress_cb, cancel_ev):
            return screener_engine.scan(
                symbols=eff_symbols,
                universe=eff_universe,
                exchange=exchange,
                timeframe=timeframe,
                timeframes=timeframes,
                lookback_days=lookback_days,
                as_of_date=as_of_date,
                screen_id=target_screen,
                rule_def=rule_def,
                params=params,
                provider=eff_provider,
                backend=backend,
                progress_cb=progress_cb,
                cancel_event=cancel_ev,
            )

        if async_job:
            job = analytics_job_runner.submit("screener", meta, _task)
            return {"ok": True, "async": True, "job": job}

        job = analytics_job_runner.run_sync("screener", meta, _task)
        return {"ok": True, "async": False, "job": job, **(job["result"] or {})}

    def run_backtest(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        strategy: str = "ema_crossover",
        params: Optional[Dict[str, Any]] = None,
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        initial_capital: float = 500000.0,
        slippage_bps: float = 5.0,
        fee_bps: float = 5.0,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 12.0,
        provider: Optional[str] = None,
        backend: str = "auto",
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
        async_job: bool = False,
    ) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (provider or sel.get("data_connection") or "yfinance").strip().lower()
        meta = {
            "symbol": (symbol or "").strip().upper(),
            "exchange": exchange,
            "strategy": strategy,
            "timeframe": timeframe,
            "provider": eff_provider,
        }

        def _task(progress_cb, cancel_ev):
            return backtest_engine.run(
                symbol=symbol,
                exchange=exchange,
                strategy=strategy,
                params=params,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                initial_capital=initial_capital,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                provider=eff_provider,
                backend=backend,
                instrument_type=instrument_type,
                instrument_key=instrument_key,
                progress_cb=progress_cb,
                cancel_event=cancel_ev,
            )

        if async_job:
            job = analytics_job_runner.submit("backtest", meta, _task)
            return {"ok": True, "async": True, "job": job}

        job = analytics_job_runner.run_sync("backtest", meta, _task)
        return {"ok": True, "async": False, "job": job, **(job["result"] or {})}

    def get_analytics_job(self, job_id: str) -> Dict[str, Any]:
        job = analytics_job_runner.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")
        return {"ok": True, "job": job}

    def cancel_analytics_job(self, job_id: str) -> Dict[str, Any]:
        job = analytics_job_runner.cancel_job(job_id)
        return {"ok": True, "job": job}

    def list_analytics_jobs(self, limit: int = 15) -> Dict[str, Any]:
        return {"ok": True, "jobs": analytics_job_runner.list_jobs(limit=limit)}

    def get_risk_status(self) -> Dict[str, Any]:
        sel = self._store.load()
        res = risk_limit_manager.evaluate(trading_broker=sel.get("trading_broker", "upstox"))
        return {
            **res,
            "watchdog": risk_watchdog.status(),
            "live_trading_enabled": is_live_trading_enabled(),
            "telegram": telegram_notifier.get_status(),
        }

    def update_risk_limits(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        risk_limit_manager.save_limits(updates, actor="operator")
        return self.get_risk_status()

    # ── Security, Telegram, OMS Reconciliation & Data History Catalog ─────

    def get_security_status(self) -> Dict[str, Any]:
        return {"ok": True, **security_manager.status()}

    def claim_admin_passcode(
        self,
        passcode: str,
        *,
        bootstrap_code: Optional[str] = None,
        client_key: str = "local",
    ) -> Dict[str, Any]:
        return security_manager.claim_passcode(
            passcode,
            bootstrap_code=bootstrap_code,
            client_key=client_key,
        )

    def login_admin(self, passcode: str, *, client_key: str = "local") -> Dict[str, Any]:
        return security_manager.login(passcode, client_key=client_key)

    def get_telegram_settings(self) -> Dict[str, Any]:
        return {"ok": True, **telegram_notifier.get_status()}

    def update_telegram_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        telegram_notifier.save_settings(updates)
        return {"ok": True, **telegram_notifier.get_status()}

    def send_telegram_test(self) -> Dict[str, Any]:
        return telegram_notifier.send_test()

    def get_asset_data_history(
        self,
        *,
        symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "storage": historical_data_manager.storage_info(),
            "history": historical_data_manager.list_asset_history(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
            ),
        }

    def start_background_services(self) -> None:
        """Initialize one-time bootstrap secret, startup reconciliation, and headless risk watchdog."""
        try:
            if security_manager.is_auth_enforced():
                security_manager.ensure_bootstrap_code()
        except Exception as exc:
            logger.warning("Bootstrap code initialization note: %s", exc)

        def _periodic_reconcile() -> None:
            try:
                deployment_manager.reconcile_orders()
            except Exception as exc:
                logger.warning("Periodic OMS order reconciliation note: %s", exc)
            try:
                deployment_manager.reconcile_positions()
            except Exception as exc:
                logger.warning("Periodic position reconciliation note: %s", exc)

        try:
            risk_watchdog.set_reconcile_callback(_periodic_reconcile)
            risk_watchdog.start()
        except Exception as exc:
            logger.warning("RiskWatchdog startup skipped: %s", exc)

        # Best-effort startup reconciliation for any live deployments persisted before restart
        try:
            deployment_manager.reconcile_startup()
        except Exception as exc:
            logger.warning("Startup reconciliation note: %s", exc)

    def stop_background_services(self) -> None:
        """Cleanly stop headless background threads on server shutdown."""
        try:
            risk_watchdog.stop()
        except Exception:
            pass

    # ── Phase 6: Full Streak Platform (Visual + Python, Portfolio Backtest,
    #    Real Charges, Options Payoff, Technicals, Baskets, Discover, Deploy) ─

    def get_streak_meta(self) -> Dict[str, Any]:
        analytics = self.get_analytics_metadata()
        return {
            "ok": True,
            "selections": analytics["selections"],
            "screens": analytics["screens"],
            "strategy_specs": analytics["strategy_specs"],
            "universes": analytics["universes"],
            "timeframes": analytics["timeframes"],
            "indicator_backends": analytics["indicator_backends"],
            "charge_presets": list_charge_presets(),
            "allocation_modes": [
                {"id": "shared_pool", "label": "Shared Pool + Max Positions + Signal Ranking"},
                {"id": "equal_split", "label": "Equal Capital Split Across Slots"},
                {"id": "risk_based", "label": "Risk-Based Sizing (% Risk / Stop-Loss)"},
            ],
            "signal_rankings": [
                {"id": "momentum", "label": "Momentum (10-Bar ROC)"},
                {"id": "score", "label": "Signal Strength / Score"},
                {"id": "alphabetical", "label": "Alphabetical (Deterministic)"},
            ],
            "visual_operands": SUPPORTED_OPERANDS,
            "visual_operators": SUPPORTED_OPERATORS,
            "visual_rules": visual_rule_store.list_rules(),
            "baskets": basket_store.list_baskets(),
            "discover": list_discover_catalog(),
            "options_templates": list_options_templates(),
            "recent_backtests": portfolio_backtest_engine.list_runs(limit=15),
            "deployments_summary": deployment_manager.list_deployments()["summary"],
        }

    def list_discover(self, category: Optional[str] = None, mode: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": True, "items": list_discover_catalog(category=category, mode=mode)}

    def list_visual_rules(self, kind: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": True, "rules": visual_rule_store.list_rules(kind=kind)}

    def save_visual_rule(self, rule_def: Dict[str, Any]) -> Dict[str, Any]:
        saved = visual_rule_store.save(rule_def)
        return {"ok": True, "rule": saved, "rules": visual_rule_store.list_rules()}

    def delete_visual_rule(self, rule_id: str) -> Dict[str, Any]:
        res = visual_rule_store.delete(rule_id)
        return {**res, "rules": visual_rule_store.list_rules()}

    def export_visual_rule_to_python(self, rule_def: Dict[str, Any], save_to_disk: bool = False) -> Dict[str, Any]:
        import tempfile

        kind = str(rule_def.get("kind") or "strategy").strip().lower()
        if save_to_disk:
            target_dir = screen_registry.screens_dir if kind == "screen" else strategy_registry.strategies_dir
            exported = export_rule_to_python(rule_def, kind=kind, target_dir=target_dir)
            if kind == "screen":
                screen_registry.refresh_if_changed(force=True)
            else:
                strategy_registry.refresh_if_changed(force=True)
            return {"ok": True, "code": exported["code"], "saved_path": exported["path"], "filename": exported["filename"]}

        with tempfile.TemporaryDirectory() as tmp_dir:
            exported = export_rule_to_python(rule_def, kind=kind, target_dir=Path(tmp_dir))
            return {"ok": True, "code": exported["code"], "saved_path": "", "filename": exported["filename"]}

    def list_baskets(self) -> Dict[str, Any]:
        return {"ok": True, "baskets": basket_store.list_baskets()}

    def save_basket(self, basket_def: Dict[str, Any]) -> Dict[str, Any]:
        raw_syms = basket_def.get("symbols") or []
        if isinstance(raw_syms, str):
            raw_syms = [s.strip() for s in raw_syms.replace("\n", ",").split(",") if s.strip()]
        saved = basket_store.save(
            name=str(basket_def.get("name") or ""),
            symbols=list(raw_syms),
            exchange=str(basket_def.get("exchange") or "NSE"),
            description=str(basket_def.get("description") or ""),
            basket_id=basket_def.get("id") or basket_def.get("basket_id"),
        )
        return {"ok": True, "basket": saved, "baskets": basket_store.list_baskets()}

    def delete_basket(self, basket_id: str) -> Dict[str, Any]:
        res = basket_store.delete(basket_id)
        return {**res, "baskets": basket_store.list_baskets()}

    def run_streak_backtest(
        self,
        *,
        mode: str = "portfolio",
        symbols: Optional[List[str]] = None,
        basket_id: Optional[str] = None,
        universe: Optional[str] = None,
        exchange: str = "NSE",
        strategy: str = "ema_crossover",
        rule_def: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        holding_type: Optional[str] = None,
        strategy_cycle: Optional[int] = None,
        side: Optional[str] = None,
        initial_capital: float = 500000.0,
        allocation_mode: str = "shared_pool",
        max_open_positions: int = 5,
        risk_per_trade_pct: float = 1.5,
        signal_ranking: str = "momentum",
        stop_loss_pct: float = 3.0,
        take_profit_pct: float = 7.0,
        trailing_sl_pct: float = 0.0,
        charges_profile: str = "zerodha",
        segment: str = "equity_intraday",
        charges_overrides: Optional[Dict[str, Any]] = None,
        slippage_bps: float = 5.0,
        provider: Optional[str] = None,
        backend: str = "auto",
        async_job: bool = False,
    ) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (provider or sel.get("data_connection") or "yfinance").strip().lower()
        meta = {
            "mode": mode,
            "strategy": strategy,
            "allocation_mode": allocation_mode,
            "timeframe": timeframe,
            "provider": eff_provider,
            "charges_profile": charges_profile,
        }

        def _task(progress_cb, cancel_ev):
            return portfolio_backtest_engine.run(
                mode=mode,
                symbols=symbols,
                basket_id=basket_id,
                universe=universe,
                exchange=exchange,
                strategy=strategy,
                rule_def=rule_def,
                params=params,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                start_time=start_time,
                end_time=end_time,
                holding_type=holding_type,
                strategy_cycle=strategy_cycle,
                side=side,
                initial_capital=initial_capital,
                allocation_mode=allocation_mode,
                max_open_positions=max_open_positions,
                risk_per_trade_pct=risk_per_trade_pct,
                signal_ranking=signal_ranking,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                trailing_sl_pct=trailing_sl_pct,
                charges_profile=charges_profile,
                segment=segment,
                charges_overrides=charges_overrides,
                slippage_bps=slippage_bps,
                provider=eff_provider,
                backend=backend,
                progress_cb=progress_cb,
                cancel_event=cancel_ev,
            )

        if async_job:
            job = analytics_job_runner.submit("portfolio_backtest", meta, _task)
            return {"ok": True, "async": True, "job": job}

        job = analytics_job_runner.run_sync("portfolio_backtest", meta, _task)
        return {"ok": True, "async": False, "job": job, **(job["result"] or {})}

    def list_backtest_runs(self, limit: int = 20) -> Dict[str, Any]:
        return {"ok": True, "runs": portfolio_backtest_engine.list_runs(limit=limit)}

    def export_backtest_ohlcv_csv(self, run_id: str, symbol: Optional[str] = None) -> tuple[str, str]:
        return portfolio_backtest_engine.export_ohlcv_csv(run_id=run_id, symbol=symbol)

    def get_backtest_chart_series(
        self,
        run_id: str,
        symbol: Optional[str] = None,
        max_bars: int = 3000,
    ) -> Dict[str, Any]:
        return portfolio_backtest_engine.get_chart_series(
            run_id=run_id,
            symbol=symbol,
            max_bars=max_bars,
        )

    def validate_formula_expr(self, expression: str, *, expect_boolean: bool = False) -> Dict[str, Any]:
        from rules.formula import validate_formula

        return validate_formula(expression, expect_boolean=expect_boolean)

    def analyze_technicals(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        provider: Optional[str] = None,
        backend: str = "auto",
    ) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (provider or sel.get("data_connection") or "yfinance").strip().lower()
        return technicals_analyzer.analyze(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            provider=eff_provider,
            backend=backend,
        )

    def build_options_template(
        self,
        *,
        template_id: str,
        spot: float,
        step: float = 50.0,
        lot_size: int = 75,
        dte_days: float = 7.0,
        iv_pct: float = 16.0,
        charges_profile: str = "zerodha",
    ) -> Dict[str, Any]:
        legs = build_template_legs(
            template_id=template_id,
            spot=spot,
            step=step,
            lot_size=lot_size,
            dte_days=dte_days,
            iv_pct=iv_pct,
        )
        analysis = analyze_options_strategy(
            spot=spot,
            legs=legs,
            dte_days=dte_days,
            charges_profile=charges_profile,
        )
        return {"ok": True, "template_id": template_id, "legs": legs, "analysis": analysis}

    def analyze_options(
        self,
        *,
        spot: float,
        legs: List[Dict[str, Any]],
        dte_days: float = 7.0,
        risk_free_rate: float = 0.068,
        charges_profile: str = "zerodha",
        charges_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return analyze_options_strategy(
            spot=spot,
            legs=legs,
            dte_days=dte_days,
            risk_free_rate=risk_free_rate,
            charges_profile=charges_profile,
            charges_overrides=charges_overrides,
        )

    def preview_charges(
        self,
        *,
        entry_price: float,
        exit_price: float,
        quantity: int,
        profile: str = "zerodha",
        segment: str = "equity_intraday",
        direction: str = "LONG",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = charges_engine.resolve_config(profile=profile, segment=segment, overrides=overrides)
        return {
            "ok": True,
            "config": cfg.to_dict(),
            **charges_engine.calculate_round_trip(
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                cfg=cfg,
                direction=direction,
            ),
        }

    def list_deployments(self) -> Dict[str, Any]:
        data = deployment_manager.list_deployments()
        return {
            **data,
            "live_trading_enabled": is_live_trading_enabled(),
            "oms_orders": oms_manager.list_orders(limit=50),
        }

    def start_deployment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (payload.get("provider") or sel.get("data_connection") or "yfinance").strip().lower()
        return deployment_manager.deploy(
            name=payload.get("name"),
            exec_mode=str(payload.get("exec_mode") or "paper"),
            strategy_id=str(payload.get("strategy_id") or payload.get("strategy") or "ema_crossover"),
            rule_def=payload.get("rule_def") if isinstance(payload.get("rule_def"), dict) else None,
            params=payload.get("params") if isinstance(payload.get("params"), dict) else None,
            symbols=payload.get("symbols") if isinstance(payload.get("symbols"), list) else None,
            exchange=str(payload.get("exchange") or "NSE"),
            timeframe=str(payload.get("timeframe") or "1day"),
            initial_capital=float(payload.get("initial_capital") or 500000.0),
            allocation_mode=str(payload.get("allocation_mode") or "shared_pool"),
            max_open_positions=int(payload.get("max_open_positions") or 5),
            risk_per_trade_pct=float(payload.get("risk_per_trade_pct") or 1.5),
            stop_loss_pct=3.0 if payload.get("stop_loss_pct") in (None, "") else float(payload.get("stop_loss_pct")),
            take_profit_pct=7.0 if payload.get("take_profit_pct") in (None, "") else float(payload.get("take_profit_pct")),
            trailing_sl_pct=0.0 if payload.get("trailing_sl_pct") in (None, "") else float(payload.get("trailing_sl_pct")),
            charges_profile=str(payload.get("charges_profile") or "zerodha"),
            segment=str(payload.get("segment") or "equity_intraday"),
            charges_overrides=payload.get("charges_overrides") if isinstance(payload.get("charges_overrides"), dict) else None,
            product=str(payload.get("product") or "MIS"),
            side=str(payload.get("side") or "BUY"),
            evaluation_mode=str(payload.get("evaluation_mode") or "completed_bar"),
            provider=eff_provider,
            poll_interval_sec=float(payload.get("poll_interval_sec") or 2.0),
            backtest_run_id=payload.get("backtest_run_id"),
        )

    def reconcile_deployments(self, deployment_id: Optional[str] = None) -> Dict[str, Any]:
        return deployment_manager.reconcile_orders(deployment_id=deployment_id)

    def list_oms_orders(
        self,
        deployment_id: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "orders": oms_manager.list_orders(deployment_id=deployment_id, limit=limit),
        }

    def control_deployment(self, deployment_id: str, action: str) -> Dict[str, Any]:
        return deployment_manager.control(deployment_id, action)

    def resolve_deployment_alert(self, alert_id: str, action: str) -> Dict[str, Any]:
        return deployment_manager.resolve_alert(alert_id, action)

    def register_deployments_listener(self):
        return deployment_manager.register_listener()

    def unregister_deployments_listener(self, q) -> None:
        deployment_manager.unregister_listener(q)

    # ── Python Plugin Source Viewer / Safe Writer ────────────────────────

    def get_python_plugin_source(self, kind: str, plugin_id: str) -> Dict[str, Any]:
        clean_kind = (kind or "screen").strip().lower()
        if clean_kind not in {"screen", "strategy"}:
            raise ValueError("kind must be 'screen' or 'strategy'.")
        base_dir = (
            screen_registry.screens_dir.resolve()
            if clean_kind == "screen"
            else strategy_registry.strategies_dir.resolve()
        )
        if clean_kind == "screen":
            screen_registry.refresh_if_changed(force=False)
            try:
                plugin_obj = screen_registry.get(plugin_id)
            except (KeyError, ValueError):
                plugin_obj = None
            src_path = screen_registry.get_source_path(plugin_id)
        else:
            strategy_registry.refresh_if_changed(force=False)
            try:
                plugin_obj = strategy_registry.create(plugin_id)
            except (KeyError, ValueError):
                plugin_obj = None
            src_path = strategy_registry.get_source_path(plugin_id)

        if plugin_obj is None or src_path is None:
            raise ValueError(f"Python {clean_kind} plugin '{plugin_id}' not found.")

        candidate = Path(src_path).resolve()
        if candidate.parent != base_dir and not str(candidate).startswith(str(base_dir) + "/"):
            raise ValueError("Invalid plugin path.")
        if not candidate.exists():
            raise ValueError(f"Source file '{candidate.name}' does not exist.")
        return {
            "ok": True,
            "kind": clean_kind,
            "id": getattr(plugin_obj, "id", plugin_id),
            "name": getattr(plugin_obj, "name", plugin_id),
            "filename": candidate.name,
            "code": candidate.read_text(encoding="utf-8"),
        }

    def save_python_plugin(
        self,
        *,
        kind: str,
        filename: str,
        code: str,
    ) -> Dict[str, Any]:
        clean_kind = (kind or "screen").strip().lower()
        if clean_kind not in {"screen", "strategy"}:
            raise ValueError("kind must be 'screen' or 'strategy'.")
        raw_filename = str(filename or "").strip()
        if not raw_filename or "/" in raw_filename or "\\" in raw_filename or ".." in raw_filename:
            raise ValueError("Invalid filename: path separators and '..' are not allowed.")
        raw_code = str(code or "")
        if not raw_code.strip():
            raise ValueError("Python source code cannot be empty.")
        if len(raw_code) > 200_000:
            raise ValueError("Python file exceeds 200 KB safety limit.")

        stem = re.sub(r"[^a-z0-9_]+", "_", Path(raw_filename).stem.strip().lower()).strip("_")
        if not stem or stem.startswith("_"):
            raise ValueError("Filename must start with a letter or digit (e.g. my_custom_screen.py).")

        # Validate Python syntax AND static AST safety policy BEFORE writing to disk
        ast_violations = validate_python_plugin_ast(raw_code, f"{stem}.py")
        if ast_violations:
            raise ValueError("Plugin rejected by safety policy: " + "; ".join(ast_violations[:3]))

        base_dir = (
            screen_registry.screens_dir.resolve()
            if clean_kind == "screen"
            else strategy_registry.strategies_dir.resolve()
        )
        base_dir.mkdir(parents=True, exist_ok=True)
        out_path = (base_dir / f"{stem}.py").resolve()
        if out_path.parent != base_dir:
            raise ValueError("Target filename escapes plugin directory.")

        out_path.write_text(raw_code, encoding="utf-8")
        if clean_kind == "screen":
            screen_registry.refresh_if_changed(force=True)
            specs = screen_registry.list_screens()
        else:
            strategy_registry.refresh_if_changed(force=True)
            specs = strategy_registry.list_strategies()

        return {
            "ok": True,
            "kind": clean_kind,
            "filename": out_path.name,
            "specs": specs,
            "plugin_trust_notice": PLUGIN_TRUST_NOTICE,
        }

    # ── Streak-style Scanner Alerts ──────────────────────────────────────

    def list_scanner_alerts(self, subscription_id: Optional[str] = None) -> Dict[str, Any]:
        return scanner_alerts_manager.list_alerts(subscription_id=subscription_id)

    def save_scanner_alert(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return scanner_alerts_manager.create_or_update(payload)

    def control_scanner_alert(self, alert_id: str, action: str) -> Dict[str, Any]:
        sel = self._store.load()
        eff_provider = (sel.get("data_connection") or "yfinance").strip().lower()
        return scanner_alerts_manager.control(alert_id, action, provider=eff_provider)

    def delete_scanner_alert(self, alert_id: str) -> Dict[str, Any]:
        return scanner_alerts_manager.delete(alert_id)

    # ── Quick Order Execution (Scanner/Strategy B/S Modal) ───────────────

    def place_quick_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("Trading symbol is required.")
        exchange = str(payload.get("exchange") or "").strip().upper()
        if not exchange:
            raise ValueError("Exchange (e.g. NSE, BSE, NFO, MCX) is required for live orders.")
        side = str(payload.get("side") or "BUY").strip().upper()
        if side not in {"BUY", "SELL", "SHORT", "COVER"}:
            raise ValueError("Order side must be BUY, SELL, SHORT, or COVER.")
        qty = int(payload.get("quantity") or 0)
        if qty < 1 or qty > 100_000:
            raise ValueError("Quantity must be between 1 and 100,000.")
        order_type = str(payload.get("order_type") or "MARKET").strip().upper()
        if order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("Order type must be MARKET or LIMIT.")
        price = float(payload.get("price") or 0.0)
        if order_type == "LIMIT" and price <= 0:
            raise ValueError("Limit price must be greater than 0.")
        product = str(payload.get("product") or "MIS").strip().upper()
        if product not in {"MIS", "CNC", "NRML"}:
            product = "MIS"

        # 0) Master live-trading opt-in lock
        if not is_live_trading_enabled():
            raise ValueError(
                "Live order routing is disabled by default (ALGODESK_ENABLE_LIVE_TRADING=false). "
                "Set ALGODESK_ENABLE_LIVE_TRADING=true in .env and restart to enable real broker orders."
            )

        # 1) Hard Kill Switch check
        ks = kill_switch_controller.status()
        if ks.get("engaged"):
            raise ValueError(f"Order blocked: Emergency Kill Switch is engaged ({ks.get('reason') or 'operator stop'}).")

        sel = self._store.load()
        trading_broker = str(sel.get("trading_broker") or "upstox").strip().lower()

        # 2) Portfolio Risk Limits check (entry + notional guard)
        risk_eval = risk_limit_manager.evaluate(trading_broker=trading_broker)
        if risk_eval.get("status") == "BREACHED":
            first_breach = (risk_eval.get("breaches") or [{}])[0].get("message", "Portfolio risk limit breached.")
            raise ValueError(f"Order blocked by Risk Limits: {first_breach}")

        # 3) Multi-exchange 6-Gate Preflight verification
        preflight = self.run_preflight(symbols=[symbol], exchange=exchange)
        if not preflight.get("overall_ready"):
            failed = [c["message"] for c in (preflight.get("checks") or []) if not c.get("passed") and c.get("severity") == "ERROR"]
            reason_txt = failed[0] if failed else "Preflight safety gates did not pass."
            raise ValueError(f"Order blocked by Preflight ({trading_broker.upper()}): {reason_txt}")

        # 4) Route order through OMS to active Trading Broker
        instrument_key = ""
        try:
            hits = instrument_index.search(q=symbol, broker_id=trading_broker, limit=5)
            exact = next(
                (h for h in hits if str(h.get("trading_symbol", "")).upper() == symbol and str(h.get("exchange", "")).upper() == exchange),
                hits[0] if hits else None,
            )
            if exact:
                instrument_key = str(exact.get("instrument_key") or "")
        except Exception:
            pass

        adapter = BrokerRegistry.get(trading_broker)
        cid = str(payload.get("client_order_id") or f"quick-{uuid.uuid4().hex[:12]}")
        req = OrderRequest(
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=qty,
            instrument_key=instrument_key,
            order_type=order_type,
            price=price if order_type == "LIMIT" else None,
            product=product,
            tag=f"algodesk:quick:{cid[-6:]}",
            algo_tag="ALGODESK-QUICK",
            client_order_id=cid,
        )
        oms_rec, _delta_qty, _fill_px = oms_manager.submit_order(
            req,
            adapter,
            deployment_id="quick_order",
            exec_mode="live_auto",
            reason="quick_order",
            reference_price=price,
        )
        oms_status = str(oms_rec.get("status") or "")
        if oms_status == OrderStatus.REJECTED.value:
            err_msg = str(oms_rec.get("rejection_reason") or "Order rejected by broker.")
            audit_trail.log(
                event_type="QUICK_ORDER_REJECTED",
                actor="operator",
                broker_id=trading_broker,
                details={
                    "client_order_id": oms_rec.get("client_order_id"),
                    "broker_order_id": oms_rec.get("broker_order_id"),
                    "symbol": symbol,
                    "exchange": exchange,
                    "side": side,
                    "quantity": qty,
                    "product": product,
                    "reason": err_msg,
                },
            )
            raise ValueError(f"Broker rejected quick order: {err_msg}")

        order_id = str(oms_rec.get("broker_order_id") or oms_rec.get("client_order_id") or "")
        audit_trail.log(
            event_type="QUICK_ORDER_PLACED",
            actor="operator",
            broker_id=trading_broker,
            details={
                "client_order_id": oms_rec.get("client_order_id"),
                "order_id": order_id,
                "status": oms_status,
                "symbol": symbol,
                "exchange": exchange,
                "side": side,
                "quantity": qty,
                "order_type": order_type,
                "price": price,
                "product": product,
            },
        )
        return {
            "ok": True,
            "broker": trading_broker,
            "order_id": order_id,
            "client_order_id": oms_rec.get("client_order_id"),
            "status": oms_status,
            "symbol": symbol,
            "exchange": exchange,
            "side": side,
            "quantity": qty,
            "order_type": order_type,
            "product": product,
            "message": f"{side} {qty} {exchange}:{symbol} ({product}) submitted to {trading_broker.upper()} ({oms_status}, ID: {order_id}).",
        }


hub_service = BrokerHubService()

__all__ = ["BrokerHubService", "hub_service"]
