"""
deploy/manager.py
=================
Unified Strategy Deployment Engine: Backtest -> Paper Trade -> Live Trade.

Latency & Safety Architecture:
- Warmup Once: Downloads missing canonical (`1minute` / `1day`) bars once via
  `HistoricalDataManager` when deployed.
- Shared Broker WebSocket: Subscribes deployed symbols onto `feed_manager` under
  the isolated `"deploy"` scope so zero extra WebSocket connections are opened
  and zero REST polling happens while the Broker WS is live.
- Pre-Resolution at Deploy Time: Resolves every symbol's `instrument_key` once
  at deployment start so order triggers never touch SQLite during signal dispatch.
- Isolated Worker Thread: Evaluates strategies on a dedicated worker thread —
  never inside `core/broker_ws.py`'s binary decode receive loop.
- Three Deployment Execution Modes:
    1. `paper`: Virtual fills at live LTP with authentic 2026 Indian charges
       (`charges.calculator`), real-time MTM P&L, SL / TP / TSL enforcement.
    2. `live_alert` (Streak 1-Click Actionable Alerts): Generates a `PENDING`
       actionable order alert pushed immediately over `/ws/deployments`; operator
       clicks **Execute Order** (`approve_alert`) or **Dismiss** (`dismiss_alert`).
    3. `live_auto` (Gated Auto-Execute): Automatically validates every signal
       against `kill_switch_controller` and `risk_limit_manager.validate_order()`
       before routing to the active trading broker adapter (`upstox` / `zerodha`)
       and recording an immutable SEBI SHA-256 audit entry.
- SQLite Persistence (`data/deployments.db`): Persists deployments, positions,
  filled trades, and actionable alerts cleanly across restarts.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from backtest.registry import StrategyRegistry, strategy_registry
from broker.base import BrokerBase, OrderRequest, OrderStatus
from broker.registry import BrokerRegistry, SelectionStore
from charges.calculator import ChargeBreakdown, ChargesConfig, charges_engine
from config import IST_TZ, config, is_live_trading_enabled
from core.audit import AuditTrail, audit_trail
from core.feed_manager import HotStandbyFeedManager, feed_manager
from core.historical import HistoricalDataManager, historical_data_manager, normalize_timeframe
from core.instruments import InstrumentIndex, instrument_index
from core.preflight import KillSwitchController, kill_switch_controller
from execution.oms import OrderManagementSystem, PaperExecutionAdapter, oms_manager
from indicators.toolkit import IndicatorToolkit
from risk.limits import RiskLimitManager, risk_limit_manager
from rules.engine import compile_visual_strategy, visual_rule_store
from screener.engine import compute_auto_lookback_days
from screener.live_engine import merge_live_tick_into_candles

logger = logging.getLogger(__name__)


def _now_ist() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


class DeploymentManager:
    """Manages concurrent Paper Trade & Live Trade strategy deployments."""

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        hist_manager: Optional[HistoricalDataManager] = None,
        registry: Optional[StrategyRegistry] = None,
        feed_mgr: Optional[HotStandbyFeedManager] = None,
        instruments: Optional[InstrumentIndex] = None,
        risk_mgr: Optional[RiskLimitManager] = None,
        kill_switch: Optional[KillSwitchController] = None,
        audit: Optional[AuditTrail] = None,
        selection_store: Optional[SelectionStore] = None,
        oms: Optional[OrderManagementSystem] = None,
        broker_resolver: Optional[Callable[[str], BrokerBase]] = None,
        require_live_env_lock: bool = True,
    ) -> None:
        self._db_path = db_path or (config.DATA_DIR / "deployments.db")
        self._hist = hist_manager or historical_data_manager
        self._registry = registry or strategy_registry
        self._feed = feed_mgr or feed_manager
        self._instruments = instruments or instrument_index
        self._risk = risk_mgr or risk_limit_manager
        self._kill_switch = kill_switch or kill_switch_controller
        self._audit = audit or audit_trail
        self._selections = selection_store or SelectionStore()
        self._oms = oms or (
            OrderManagementSystem(db_path=self._db_path.parent / "oms.db", audit=self._audit)
            if db_path is not None
            else oms_manager
        )
        self._broker_resolver = broker_resolver or BrokerRegistry.get
        self._require_live_env_lock = require_live_env_lock
        self._paper_adapter = PaperExecutionAdapter()

        self._lock = threading.RLock()
        self._listeners: Set[queue.Queue] = set()
        self._runtime: Dict[str, Dict[str, Any]] = {}  # deployment_id -> live state & threads
        self._init_db()
        self._load_persisted_into_memory()

    # ── SQLite Persistence ───────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    exec_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_mode TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    cash REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    unrealized_pnl REAL NOT NULL DEFAULT 0,
                    total_charges REAL NOT NULL DEFAULT 0,
                    spec_json TEXT NOT NULL,
                    created_at_ist TEXT NOT NULL,
                    updated_at_ist TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployment_positions (
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    last_price REAL NOT NULL,
                    highest_price REAL NOT NULL,
                    entry_charges REAL NOT NULL DEFAULT 0,
                    entry_at_ist TEXT NOT NULL,
                    PRIMARY KEY (deployment_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS deployment_trades (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    charges REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    reason TEXT NOT NULL,
                    broker_order_id TEXT DEFAULT '',
                    executed_at_ist TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actionable_alerts (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    deployment_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    instrument_key TEXT DEFAULT '',
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    signal_price REAL NOT NULL,
                    reason TEXT NOT NULL,
                    product TEXT NOT NULL DEFAULT 'MIS',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    broker_order_id TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    created_at_ist TEXT NOT NULL,
                    resolved_at_ist TEXT DEFAULT ''
                );
                """
            )

    def _load_persisted_into_memory(self) -> None:
        """Load saved deployments as PAUSED on cold boot (never auto-fire live orders on reboot)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM deployments ORDER BY created_at_ist DESC").fetchall()
            for r in rows:
                did = str(r["id"])
                status = str(r["status"])
                if status in {"RUNNING", "WARMING_UP"}:
                    status = "PAUSED"
                    conn.execute("UPDATE deployments SET status=?, updated_at_ist=? WHERE id=?", (status, _now_ist(), did))
                pos_rows = conn.execute(
                    "SELECT * FROM deployment_positions WHERE deployment_id=?",
                    (did,),
                ).fetchall()
                positions: Dict[str, Dict[str, Any]] = {}
                for pr in pos_rows:
                    positions[str(pr["symbol"])] = {
                        "symbol": str(pr["symbol"]),
                        "qty": int(pr["qty"]),
                        "entry_price": float(pr["entry_price"]),
                        "last_price": float(pr["last_price"]),
                        "highest_price": float(pr["highest_price"]),
                        "entry_charges": float(pr["entry_charges"]),
                        "entry_at_ist": str(pr["entry_at_ist"]),
                    }
                spec = json.loads(str(r["spec_json"]))
                self._runtime[did] = {
                    "id": did,
                    "name": str(r["name"]),
                    "exec_mode": str(r["exec_mode"]),
                    "status": status,
                    "strategy_id": str(r["strategy_id"]),
                    "strategy_mode": str(r["strategy_mode"]),
                    "exchange": str(r["exchange"]),
                    "timeframe": str(r["timeframe"]),
                    "initial_capital": float(r["initial_capital"]),
                    "cash": float(r["cash"]),
                    "realized_pnl": float(r["realized_pnl"]),
                    "unrealized_pnl": float(r["unrealized_pnl"]),
                    "total_charges": float(r["total_charges"]),
                    "spec": spec,
                    "positions": positions,
                    "sym_state": {},
                    "instrument_keys": {},
                    "last_note": "Restored from disk (paused on startup for safety)." if status == "PAUSED" else "",
                    "created_at_ist": str(r["created_at_ist"]),
                    "updated_at_ist": str(r["updated_at_ist"]),
                    "stop_event": threading.Event(),
                    "thread": None,
                }

    def _persist_deployment_locked(self, rt: Dict[str, Any]) -> None:
        now = _now_ist()
        rt["updated_at_ist"] = now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deployments (
                    id, name, exec_mode, status, strategy_id, strategy_mode,
                    exchange, timeframe, initial_capital, cash, realized_pnl,
                    unrealized_pnl, total_charges, spec_json, created_at_ist, updated_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    cash=excluded.cash,
                    realized_pnl=excluded.realized_pnl,
                    unrealized_pnl=excluded.unrealized_pnl,
                    total_charges=excluded.total_charges,
                    updated_at_ist=excluded.updated_at_ist
                """,
                (
                    rt["id"],
                    rt["name"],
                    rt["exec_mode"],
                    rt["status"],
                    rt["strategy_id"],
                    rt["strategy_mode"],
                    rt["exchange"],
                    rt["timeframe"],
                    rt["initial_capital"],
                    rt["cash"],
                    rt["realized_pnl"],
                    rt["unrealized_pnl"],
                    rt["total_charges"],
                    json.dumps(rt["spec"]),
                    rt["created_at_ist"],
                    now,
                ),
            )
            conn.execute("DELETE FROM deployment_positions WHERE deployment_id=?", (rt["id"],))
            for sym, pos in rt["positions"].items():
                conn.execute(
                    """
                    INSERT INTO deployment_positions (
                        deployment_id, symbol, qty, entry_price, last_price,
                        highest_price, entry_charges, entry_at_ist
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rt["id"],
                        sym,
                        int(pos["qty"]),
                        float(pos["entry_price"]),
                        float(pos["last_price"]),
                        float(pos["highest_price"]),
                        float(pos.get("entry_charges", 0.0)),
                        str(pos.get("entry_at_ist") or now),
                    ),
                )

    def _record_trade_db(
        self,
        *,
        deployment_id: str,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        exit_price: float,
        gross_pnl: float,
        charges: float,
        net_pnl: float,
        reason: str,
        broker_order_id: str = "",
    ) -> Dict[str, Any]:
        tid = f"tr-{uuid.uuid4().hex[:10]}"
        ts = _now_ist()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deployment_trades (
                    id, deployment_id, symbol, side, qty, entry_price, exit_price,
                    gross_pnl, charges, net_pnl, reason, broker_order_id, executed_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, deployment_id, symbol, side, qty, entry_price, exit_price, gross_pnl, charges, net_pnl, reason, broker_order_id, ts),
            )
        return {
            "id": tid,
            "deployment_id": deployment_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "gross_pnl": round(gross_pnl, 2),
            "charges": round(charges, 2),
            "net_pnl": round(net_pnl, 2),
            "reason": reason,
            "broker_order_id": broker_order_id,
            "executed_at_ist": ts,
        }

    def _create_alert_db(
        self,
        *,
        deployment_id: str,
        deployment_name: str,
        symbol: str,
        exchange: str,
        instrument_key: str,
        side: str,
        qty: int,
        signal_price: float,
        reason: str,
        product: str,
    ) -> Dict[str, Any]:
        aid = f"alrt-{uuid.uuid4().hex[:10]}"
        ts = _now_ist()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO actionable_alerts (
                    id, deployment_id, deployment_name, symbol, exchange, instrument_key,
                    side, qty, signal_price, reason, product, status, created_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (aid, deployment_id, deployment_name, symbol, exchange, instrument_key, side, qty, signal_price, reason, product, ts),
            )
        return {
            "id": aid,
            "deployment_id": deployment_id,
            "deployment_name": deployment_name,
            "symbol": symbol,
            "exchange": exchange,
            "instrument_key": instrument_key,
            "side": side,
            "qty": qty,
            "signal_price": round(signal_price, 2),
            "reason": reason,
            "product": product,
            "status": "PENDING",
            "broker_order_id": "",
            "note": "",
            "created_at_ist": ts,
            "resolved_at_ist": "",
        }

    # ── WebSocket Pub/Sub Listeners ──────────────────────────────────────

    def register_listener(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=32)
        with self._lock:
            self._listeners.add(q)
        return q

    def unregister_listener(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)

    def _broadcast(self, event_type: str, extra: Optional[Dict[str, Any]] = None) -> None:
        snapshot = self.list_deployments()
        msg = {"type": event_type, **snapshot, **(extra or {})}
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                q.put_nowait(msg)
            except Exception:
                pass

    # ── Strategy & Instrument Helpers ────────────────────────────────────

    def _build_strategy(self, spec: Dict[str, Any]):
        rule_def = spec.get("rule_def")
        if rule_def is not None:
            return compile_visual_strategy(rule_def), "visual"
        sid = str(spec.get("strategy_id") or spec.get("strategy") or "ema_crossover")
        try:
            return self._registry.create(sid), "python"
        except Exception:
            saved = visual_rule_store.get(sid)
            if saved is not None:
                return compile_visual_strategy(saved), "visual"
            raise

    def _charges_cfg_for(self, spec: Dict[str, Any]) -> ChargesConfig:
        return charges_engine.resolve_config(
            profile=str(spec.get("charges_profile") or "zerodha"),
            segment=str(spec.get("segment") or "equity_intraday"),
            overrides=spec.get("charges_overrides") or {},
        )

    def _preresolve_keys(self, symbols: List[str], exchange: str, broker_id: str) -> Dict[str, str]:
        resolved: Dict[str, str] = {}
        for sym in symbols:
            try:
                key = self._instruments.resolve_key(sym, broker_id=broker_id, exchange=exchange)
                if key:
                    resolved[sym] = key
            except Exception:
                pass
        return resolved

    def _refresh_deploy_ws_scope(self) -> None:
        """Keep the shared broker WebSocket subscribed to the union of all RUNNING deployments."""
        subs: List[Dict[str, str]] = []
        seen: set[str] = set()
        with self._lock:
            for rt in self._runtime.values():
                if rt["status"] not in {"RUNNING", "WARMING_UP"}:
                    continue
                exch = rt["exchange"]
                ikeys = rt.get("instrument_keys") or {}
                for sym in rt["spec"].get("symbols") or []:
                    sk = f"{exch}:{sym}"
                    if sk in seen:
                        continue
                    seen.add(sk)
                    subs.append(
                        {
                            "symbol": sym,
                            "exchange": exch,
                            "instrument_type": "EQUITY",
                            "instrument_key": ikeys.get(sym, ""),
                        }
                    )
        try:
            if subs:
                provider = str(self._selections.load().get("data_connection") or config.ACTIVE_DATA_CONNECTION or "upstox")
                self._feed.ensure_deploy_ws(provider, subs)
            else:
                self._feed.release_deploy_ws()
        except Exception:
            pass

    # ── Deployment Lifecycle ─────────────────────────────────────────────

    def deploy(
        self,
        *,
        name: Optional[str] = None,
        exec_mode: str = "paper",
        strategy_id: str = "ema_crossover",
        rule_def: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        symbols: Optional[List[str]] = None,
        exchange: str = "NSE",
        timeframe: str = "1day",
        initial_capital: float = 500000.0,
        allocation_mode: str = "shared_pool",
        max_open_positions: int = 5,
        risk_per_trade_pct: float = 1.5,
        stop_loss_pct: float = 3.0,
        take_profit_pct: float = 7.0,
        trailing_sl_pct: float = 0.0,
        charges_profile: str = "zerodha",
        segment: str = "equity_intraday",
        charges_overrides: Optional[Dict[str, Any]] = None,
        product: str = "MIS",
        evaluation_mode: str = "completed_bar",
        side: str = "BUY",
        provider: Optional[str] = None,
        poll_interval_sec: float = 2.0,
        backtest_run_id: Optional[str] = None,
        start_worker: bool = True,
        hist_fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        live_fetcher: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        mode = (exec_mode or "paper").strip().lower()
        if mode not in {"paper", "live_alert", "live_auto"}:
            raise ValueError("exec_mode must be 'paper', 'live_alert', or 'live_auto'.")
        if mode == "live_auto" and self._require_live_env_lock and not is_live_trading_enabled():
            raise PermissionError(
                "Live trading is locked by default (ALGODESK_ENABLE_LIVE_TRADING=false). "
                "Use 'paper' or 'live_alert' mode, or set ALGODESK_ENABLE_LIVE_TRADING=true in server .env."
            )

        eval_mode = (evaluation_mode or "completed_bar").strip().lower()
        if eval_mode not in {"completed_bar", "intrabar"}:
            eval_mode = "completed_bar"

        clean_syms: List[str] = []
        seen: set[str] = set()
        for raw in symbols or ["RELIANCE"]:
            s = str(raw or "").strip().upper()
            if s and s not in seen:
                seen.add(s)
                clean_syms.append(s)
        if not clean_syms:
            raise ValueError("At least one symbol is required to deploy.")

        clean_exch = (exchange or "NSE").strip().upper()
        tf = normalize_timeframe(timeframe)
        cap = max(5000.0, float(initial_capital or 500000.0))

        spec = {
            "backtest_run_id": backtest_run_id or "",
            "strategy_id": strategy_id,
            "rule_def": rule_def,
            "params": dict(params or {}),
            "symbols": clean_syms[:30],
            "exchange": clean_exch,
            "timeframe": tf,
            "allocation_mode": allocation_mode or "shared_pool",
            "max_open_positions": max(1, min(len(clean_syms), int(max_open_positions or 5))),
            "risk_per_trade_pct": max(0.1, float(risk_per_trade_pct or 1.5)),
            "stop_loss_pct": max(0.0, float(stop_loss_pct or 0.0)),
            "take_profit_pct": max(0.0, float(take_profit_pct or 0.0)),
            "trailing_sl_pct": max(0.0, float(trailing_sl_pct or 0.0)),
            "charges_profile": charges_profile or "zerodha",
            "segment": segment or "equity_intraday",
            "charges_overrides": dict(charges_overrides or {}),
            "product": (product or ("CNC" if segment == "equity_delivery" else "MIS")).strip().upper(),
            "evaluation_mode": eval_mode,
            "side": (side or "BUY").strip().upper(),
            "provider": (provider or self._selections.load().get("data_connection") or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower(),
            "poll_interval_sec": max(1.0, min(30.0, float(poll_interval_sec or 2.0))),
        }

        strat_inst, strat_mode = self._build_strategy(spec)
        spec["params"] = strat_inst.resolve_params(spec["params"])
        spec["strategy_id"] = strat_inst.id
        spec["strategy_name"] = strat_inst.name

        did = f"dep-{uuid.uuid4().hex[:8]}"
        disp_name = (name or "").strip() or f"{strat_inst.name} ({mode.replace('_', ' ').upper()})"
        now = _now_ist()

        trading_broker = str(self._selections.load().get("trading_broker") or "upstox").strip().lower()
        pre_keys = self._preresolve_keys(spec["symbols"], clean_exch, trading_broker)

        rt: Dict[str, Any] = {
            "id": did,
            "name": disp_name,
            "exec_mode": mode,
            "status": "WARMING_UP" if start_worker else "RUNNING",
            "strategy_id": strat_inst.id,
            "strategy_mode": strat_mode,
            "exchange": clean_exch,
            "timeframe": tf,
            "initial_capital": round(cap, 2),
            "cash": round(cap, 2),
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_charges": 0.0,
            "spec": spec,
            "positions": {},
            "sym_state": {},
            "instrument_keys": pre_keys,
            "last_note": f"Deployed ({len(spec['symbols'])} symbols). Warming up historical indicators...",
            "created_at_ist": now,
            "updated_at_ist": now,
            "stop_event": threading.Event(),
            "thread": None,
        }

        with self._lock:
            self._runtime[did] = rt
            self._persist_deployment_locked(rt)

        self._audit.log(
            event_type="STRATEGY_DEPLOYED",
            actor="operator",
            payload={
                "deployment_id": did,
                "name": disp_name,
                "exec_mode": mode,
                "strategy_id": strat_inst.id,
                "strategy_mode": strat_mode,
                "symbols": spec["symbols"],
                "capital": round(cap, 2),
            },
        )

        if start_worker:
            self._start_worker(did, hist_fetcher=hist_fetcher, live_fetcher=live_fetcher)

        self._broadcast("deployments:update")
        return self.get_deployment(did)

    def _start_worker(
        self,
        did: str,
        *,
        hist_fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        live_fetcher: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]] = None,
    ) -> None:
        with self._lock:
            rt = self._runtime.get(did)
            if not rt:
                return
            rt["stop_event"] = threading.Event()
            stop_ev = rt["stop_event"]
            th = threading.Thread(
                target=self._worker_loop,
                args=(did, stop_ev, hist_fetcher, live_fetcher),
                name=f"algodesk-deploy-{did}",
                daemon=True,
            )
            rt["thread"] = th
            th.start()

    def _worker_loop(
        self,
        did: str,
        stop_ev: threading.Event,
        hist_fetcher: Optional[Callable[..., List[Dict[str, Any]]]],
        live_fetcher: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]],
    ) -> None:
        with self._lock:
            rt = self._runtime.get(did)
            if not rt:
                return
            spec = dict(rt["spec"])
            syms = list(spec["symbols"])
            tf = rt["timeframe"]
            exch = rt["exchange"]
            provider = str(spec.get("provider") or "yfinance")
            interval = float(spec.get("poll_interval_sec") or 2.0)

        # 1) Historical Warmup ONCE per symbol via canonical HistoricalDataManager
        today_ist = datetime.now(tz=IST_TZ).date()
        to_date = today_ist.isoformat()
        lookback_days = compute_auto_lookback_days(220, tf)
        from_date = (today_ist - timedelta(days=lookback_days)).isoformat()

        warmed = 0
        for sym in syms:
            if stop_ev.is_set():
                return
            try:
                dl_meta = self._hist.download(
                    symbol=sym,
                    exchange=exch,
                    timeframe=tf,
                    from_date=from_date,
                    to_date=to_date,
                    provider=provider,
                    fetcher=hist_fetcher,
                )
                rec = self._hist.get_records(
                    symbol=dl_meta["symbol"],
                    exchange=dl_meta["exchange"],
                    timeframe=dl_meta["timeframe"],
                    from_date=None,
                    to_date=to_date,
                    view="full",
                )
                bars = rec.get("rows", [])
                if bars:
                    with self._lock:
                        rt["sym_state"][sym] = {
                            "base_bars": list(bars),
                            "forming_bar": None,
                            "last_ltp": float(bars[-1]["close"]),
                            "last_sig_key": "",
                        }
                    warmed += 1
            except Exception as exc:
                logger.debug("Deployment %s warmup failed for %s: %s", did, sym, exc)

        if stop_ev.is_set():
            return

        with self._lock:
            rt["status"] = "RUNNING"
            rt["last_note"] = f"Live ({warmed}/{len(syms)} symbols warmed). Streaming via {provider.upper()}."
            self._persist_deployment_locked(rt)
        self._refresh_deploy_ws_scope()
        self._broadcast("deployments:update")

        # 2) Live Inspection Loop (reads from shared Broker WS cache first, zero REST polling when live)
        while not stop_ev.is_set():
            with self._lock:
                cur_status = rt["status"]
            if cur_status != "RUNNING":
                break
            try:
                if live_fetcher is not None:
                    quotes = live_fetcher(syms) or {}
                else:
                    res = self._feed.fetch_live_quotes_for(
                        syms,
                        exchange=exch,
                        provider=provider,
                        ws_scope="deploy",
                    )
                    quotes = res.get("quotes") or {} if res.get("ok") else {}
                if quotes:
                    self.process_ticks(did, quotes)
            except Exception as exc:
                logger.debug("Deployment %s tick loop error: %s", did, exc)
            stop_ev.wait(interval)

    # ── Signal & Execution Evaluation ────────────────────────────────────

    def _compute_order_qty(self, rt: Dict[str, Any], price: float) -> int:
        if price <= 0:
            return 0
        spec = rt["spec"]
        alloc = str(spec.get("allocation_mode") or "shared_pool").lower()
        max_slots = max(1, int(spec.get("max_open_positions") or 5))
        open_count = len(rt["positions"])
        free_slots = max(1, max_slots - open_count)
        cash = float(rt["cash"])
        equity = cash + sum(abs(int(p["qty"])) * float(p["last_price"]) for p in rt["positions"].values())

        if alloc == "equal_split":
            budget = min(cash, float(rt["initial_capital"]) / float(max_slots))
            return int(budget // (price * 1.005))
        if alloc == "risk_based":
            sl = float(spec.get("stop_loss_pct") or 2.0)
            sl = sl if sl > 0 else 2.0
            risk_pct = float(spec.get("risk_per_trade_pct") or 1.5)
            risk_amt = equity * (risk_pct / 100.0)
            qty_risk = int(risk_amt // max(0.05, price * (sl / 100.0)))
            qty_cash = int(min(cash, equity * 0.50) // (price * 1.005))
            return min(qty_risk, qty_cash)
        budget = cash / float(free_slots)
        return int(budget // (price * 1.005))

    def process_ticks(self, deployment_id: str, quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluate live quotes for a deployment (default: completed_bar close parity with backtest)."""
        actions_taken: List[Dict[str, Any]] = []
        with self._lock:
            rt = self._runtime.get(deployment_id)
            if not rt or rt["status"] != "RUNNING":
                return {"ok": False, "actions": []}
            spec = rt["spec"]
            tf = rt["timeframe"]
            eval_mode = str(spec.get("evaluation_mode") or "completed_bar").strip().lower()
            is_short_strat = str(spec.get("side") or "BUY").strip().upper() in {"SELL", "SHORT"}
            sl_pct = float(spec.get("stop_loss_pct") or 0.0)
            tp_pct = float(spec.get("take_profit_pct") or 0.0)
            tsl_pct = float(spec.get("trailing_sl_pct") or 0.0)
            max_slots = max(1, int(spec.get("max_open_positions") or 5))

        strat_inst, _ = self._build_strategy(spec)

        for sym, q in quotes.items():
            sym_u = str(q.get("symbol") or sym).strip().upper()
            raw_ltp = q.get("ltp")
            if raw_ltp is None or float(raw_ltp) <= 0:
                continue
            ltp = float(raw_ltp)

            with self._lock:
                st = rt["sym_state"].get(sym_u)
                if st is None:
                    st = {
                        "base_bars": [],
                        "forming_bar": None,
                        "last_ltp": ltp,
                        "last_sig_key": "",
                        "last_eval_bar_ts": "",
                        "indicator_eval_count": 0,
                    }
                    rt["sym_state"][sym_u] = st
                base_bars = st["base_bars"]
                forming_bar = st["forming_bar"]
                last_eval_bar_ts = str(st.get("last_eval_bar_ts") or "")

            new_base, new_forming, combined = merge_live_tick_into_candles(base_bars, forming_bar, q, tf)
            enter_sig = False
            exit_sig = False
            eval_bar_ts = ""

            if eval_mode == "completed_bar":
                # Evaluate strategy ONLY when a new completed candle closes (matches backtest semantics
                # and caches indicators between candle closes on MacBook Air M2).
                if len(new_base) >= 5:
                    latest_closed_ts = str(new_base[-1].get("ts_ist") or "")
                    if latest_closed_ts and latest_closed_ts != last_eval_bar_ts:
                        try:
                            tk = IndicatorToolkit(new_base, backend="auto")
                            strat_inst.prepare(new_base, spec["params"], tk)
                            enter_sig, exit_sig = strat_inst.signal_at(len(new_base) - 1, new_base[-1])
                            eval_bar_ts = latest_closed_ts
                            st["indicator_eval_count"] = int(st.get("indicator_eval_count") or 0) + 1
                        except Exception:
                            enter_sig, exit_sig = False, False
            else:
                if len(combined) >= 5:
                    try:
                        tk = IndicatorToolkit(combined, backend="auto")
                        strat_inst.prepare(combined, spec["params"], tk)
                        enter_sig, exit_sig = strat_inst.signal_at(len(combined) - 1, combined[-1])
                        eval_bar_ts = str(combined[-1].get("ts_ist") or "")
                        st["indicator_eval_count"] = int(st.get("indicator_eval_count") or 0) + 1
                    except Exception:
                        enter_sig, exit_sig = False, False

            with self._lock:
                st["base_bars"] = new_base
                st["forming_bar"] = new_forming
                st["last_ltp"] = ltp
                if eval_bar_ts:
                    st["last_eval_bar_ts"] = eval_bar_ts
                bar_key = eval_bar_ts or (str(combined[-1].get("ts_ist") or _now_ist()) if combined else _now_ist())

                pos = rt["positions"].get(sym_u)
                if pos is not None:
                    signed_qty = int(pos["qty"])
                    abs_qty = abs(signed_qty)
                    is_short_pos = signed_qty < 0
                    pos["last_price"] = ltp
                    if not is_short_pos:
                        if ltp > pos["highest_price"]:
                            pos["highest_price"] = ltp
                    else:
                        if ltp < pos["highest_price"]:
                            pos["highest_price"] = ltp

                    exit_reason = ""
                    if not is_short_pos:
                        if sl_pct > 0 and ltp <= pos["entry_price"] * (1.0 - sl_pct / 100.0):
                            exit_reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                        elif tsl_pct > 0 and pos["highest_price"] > pos["entry_price"] and ltp <= pos["highest_price"] * (1.0 - tsl_pct / 100.0):
                            exit_reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                        elif tp_pct > 0 and ltp >= pos["entry_price"] * (1.0 + tp_pct / 100.0):
                            exit_reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                        elif exit_sig:
                            exit_reason = "STRATEGY_EXIT"
                    else:
                        if sl_pct > 0 and ltp >= pos["entry_price"] * (1.0 + sl_pct / 100.0):
                            exit_reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                        elif tsl_pct > 0 and pos["highest_price"] < pos["entry_price"] and ltp >= pos["highest_price"] * (1.0 + tsl_pct / 100.0):
                            exit_reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                        elif tp_pct > 0 and ltp <= pos["entry_price"] * (1.0 - tp_pct / 100.0):
                            exit_reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                        elif exit_sig:
                            exit_reason = "STRATEGY_EXIT"

                    if exit_reason and abs_qty > 0:
                        exit_side = "COVER" if is_short_pos else "SELL"
                        ev = self._handle_signal_locked(rt, sym_u, exit_side, abs_qty, ltp, exit_reason)
                        if ev:
                            actions_taken.append(ev)
                elif enter_sig and len(rt["positions"]) < max_slots:
                    entry_side = "SHORT" if is_short_strat else "BUY"
                    sig_marker = f"{entry_side}:{bar_key}"
                    if st.get("last_sig_key") != sig_marker:
                        qty = self._compute_order_qty(rt, ltp)
                        if qty >= 1:
                            st["last_sig_key"] = sig_marker
                            ev = self._handle_signal_locked(rt, sym_u, entry_side, qty, ltp, "STRATEGY_ENTRY")
                            if ev:
                                actions_taken.append(ev)

                # Recompute live MTM unrealized P&L (supports both signed long >0 and short <0)
                unreal = 0.0
                for p in rt["positions"].values():
                    unreal += (float(p["last_price"]) - float(p["entry_price"])) * int(p["qty"])
                rt["unrealized_pnl"] = round(unreal, 2)
                self._persist_deployment_locked(rt)

        self._broadcast("deployments:tick", {"actions": actions_taken})
        return {"ok": True, "actions": actions_taken}

    def trigger_manual_signal(
        self,
        deployment_id: str,
        *,
        symbol: str,
        side: str,
        price: float,
        qty: Optional[int] = None,
        reason: str = "SIGNAL_TRIGGER",
        client_order_id: str = "",
    ) -> Dict[str, Any]:
        """Deterministic signal trigger (useful for testing & manual simulation)."""
        sym_u = symbol.strip().upper()
        side_u = side.strip().upper()
        with self._lock:
            rt = self._runtime.get(deployment_id)
            if not rt:
                raise ValueError(f"Deployment '{deployment_id}' not found.")
            if rt["status"] != "RUNNING":
                raise ValueError(f"Deployment '{deployment_id}' is {rt['status']}, not RUNNING.")
            eff_qty = int(qty) if (qty and int(qty) > 0) else (
                abs(int(rt["positions"][sym_u]["qty"]))
                if (side_u in {"SELL", "COVER"} and sym_u in rt["positions"])
                else max(1, self._compute_order_qty(rt, price))
            )
            ev = self._handle_signal_locked(
                rt, sym_u, side_u, eff_qty, float(price), reason, client_order_id=client_order_id
            )
            unreal = sum((float(p["last_price"]) - float(p["entry_price"])) * int(p["qty"]) for p in rt["positions"].values())
            rt["unrealized_pnl"] = round(unreal, 2)
            self._persist_deployment_locked(rt)
        self._broadcast("deployments:update")
        return ev or {"ok": False}

    def _handle_signal_locked(
        self,
        rt: Dict[str, Any],
        symbol: str,
        side: str,
        qty: int,
        price: float,
        reason: str,
        *,
        client_order_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        mode = rt["exec_mode"]
        cfg = self._charges_cfg_for(rt["spec"])

        if mode == "paper":
            # Route Paper orders through the exact same canonical OrderRequest + OMS pipeline
            req = OrderRequest(
                symbol=symbol,
                side=side,
                quantity=qty,
                exchange=rt["exchange"],
                instrument_key=rt["instrument_keys"].get(symbol, ""),
                order_type="MARKET",
                product=str(rt["spec"].get("product") or "MIS"),
                client_order_id=client_order_id,
                tag=f"paper:{rt['id']}",
                metadata={"reference_price": price, "exchange": rt["exchange"]},
            )
            order_rec, delta_qty, fill_px = self._oms.submit_order(
                req,
                self._paper_adapter,
                deployment_id=rt["id"],
                exec_mode="paper",
                reason=reason,
                reference_price=price,
            )
            if delta_qty > 0:
                res = self._apply_verified_fill_locked(
                    rt,
                    symbol,
                    side,
                    delta_qty,
                    fill_px or price,
                    reason,
                    cfg,
                    broker_order_id=order_rec["broker_order_id"],
                )
                res["order"] = order_rec
                return res
            return {"kind": "rejected", "order": order_rec, "error": order_rec.get("rejection_reason", "")}

        if mode == "live_alert":
            alert = self._create_alert_db(
                deployment_id=rt["id"],
                deployment_name=rt["name"],
                symbol=symbol,
                exchange=rt["exchange"],
                instrument_key=rt["instrument_keys"].get(symbol, ""),
                side=side,
                qty=qty,
                signal_price=price,
                reason=reason,
                product=str(rt["spec"].get("product") or "MIS"),
            )
            rt["last_note"] = f"1-Click Alert queued: {side} {qty} {symbol} @ ₹{price:.2f} ({reason})."
            self._audit.log(
                event_type="ACTIONABLE_ALERT_CREATED",
                actor="deploy_engine",
                payload={"deployment_id": rt["id"], "alert_id": alert["id"], "symbol": symbol, "side": side, "qty": qty, "price": price},
            )
            return {"kind": "alert", "alert": alert}

        # mode == "live_auto": Strictly gated by Kill Switch + RiskLimitManager + Live Master Lock
        open_count = len(rt["positions"])
        gross_exp = sum(abs(int(p["qty"])) * float(p["last_price"]) for p in rt["positions"].values())
        check = self._risk.validate_order(
            symbol=symbol,
            quantity=qty,
            price=price,
            transaction_type=side,
            current_open_positions=open_count,
            current_gross_exposure_inr=gross_exp,
        )
        if not check["allowed"]:
            block_reason = "; ".join(check["reasons"])
            rt["last_note"] = f"Auto-order BLOCKED by safety gate ({block_reason})."
            return {"kind": "blocked", "symbol": symbol, "side": side, "reasons": check["reasons"]}

        return self._dispatch_live_broker_order_locked(
            rt, symbol, side, qty, price, reason, cfg, client_order_id=client_order_id
        )

    def _apply_verified_fill_locked(
        self,
        rt: Dict[str, Any],
        symbol: str,
        side: str,
        qty: int,
        price: float,
        reason: str,
        cfg: ChargesConfig,
        broker_order_id: str = "",
    ) -> Dict[str, Any]:
        """
        Update deployment cash, signed position (+qty long, -qty short), charges,
        and realized P&L STRICTLY from a verified fill delta (`qty > 0`).
        Supports both full and partial fills cleanly.
        """
        clean_side = side.strip().upper()
        txn = "BUY" if clean_side in {"BUY", "COVER"} else "SELL"
        leg = charges_engine.calculate_leg(side=txn, price=price, quantity=qty, cfg=cfg, include_slippage=True)
        rt["total_charges"] = round(rt["total_charges"] + leg.total_charges, 2)

        pos = rt["positions"].get(symbol)
        if clean_side == "BUY":
            outlay = (qty * price) + leg.total_charges
            rt["cash"] = round(rt["cash"] - outlay, 2)
            if pos is not None and int(pos["qty"]) > 0:
                old_qty = int(pos["qty"])
                new_qty = old_qty + qty
                avg_px = ((old_qty * float(pos["entry_price"])) + (qty * price)) / float(new_qty)
                pos["qty"] = new_qty
                pos["entry_price"] = round(avg_px, 4)
                pos["last_price"] = price
                pos["highest_price"] = max(float(pos["highest_price"]), price)
                pos["entry_charges"] = float(pos.get("entry_charges", 0.0)) + leg.total_charges
            else:
                rt["positions"][symbol] = {
                    "symbol": symbol,
                    "qty": qty,
                    "entry_price": price,
                    "last_price": price,
                    "highest_price": price,
                    "entry_charges": leg.total_charges,
                    "entry_at_ist": _now_ist(),
                }
            rt["last_note"] = f"BUY {qty} {symbol} @ ₹{price:.2f} (Charges ₹{leg.total_charges:.2f})."
            return {"kind": "fill", "side": "BUY", "symbol": symbol, "qty": qty, "price": price, "charges": round(leg.total_charges, 2), "broker_order_id": broker_order_id}

        if clean_side == "SHORT":
            outlay = (qty * price) + leg.total_charges
            rt["cash"] = round(rt["cash"] - outlay, 2)
            if pos is not None and int(pos["qty"]) < 0:
                old_abs = abs(int(pos["qty"]))
                new_abs = old_abs + qty
                avg_px = ((old_abs * float(pos["entry_price"])) + (qty * price)) / float(new_abs)
                pos["qty"] = -new_abs
                pos["entry_price"] = round(avg_px, 4)
                pos["last_price"] = price
                pos["highest_price"] = min(float(pos["highest_price"]), price)
                pos["entry_charges"] = float(pos.get("entry_charges", 0.0)) + leg.total_charges
            else:
                rt["positions"][symbol] = {
                    "symbol": symbol,
                    "qty": -qty,
                    "entry_price": price,
                    "last_price": price,
                    "highest_price": price,
                    "entry_charges": leg.total_charges,
                    "entry_at_ist": _now_ist(),
                }
            rt["last_note"] = f"SHORT {qty} {symbol} @ ₹{price:.2f} (Charges ₹{leg.total_charges:.2f})."
            return {"kind": "fill", "side": "SHORT", "symbol": symbol, "qty": qty, "price": price, "charges": round(leg.total_charges, 2), "broker_order_id": broker_order_id}

        # Closing legs: SELL closes long (+qty), COVER closes short (-qty)
        if clean_side == "COVER" or (pos is not None and int(pos["qty"]) < 0):
            old_abs = abs(int(pos["qty"])) if pos else qty
            close_qty = min(qty, old_abs) if pos else qty
            entry_px = float(pos["entry_price"]) if pos else price
            prop_entry_chg = (float(pos.get("entry_charges", 0.0)) * (close_qty / float(old_abs))) if (pos and old_abs > 0) else 0.0
            gross_pnl = (entry_px - price) * close_qty
            total_rt_chg = prop_entry_chg + leg.total_charges
            net_pnl = gross_pnl - total_rt_chg
            rt["cash"] = round(rt["cash"] + (close_qty * entry_px) + gross_pnl - leg.total_charges, 2)
            rt["realized_pnl"] = round(rt["realized_pnl"] + net_pnl, 2)
            if pos is not None:
                rem_abs = old_abs - close_qty
                if rem_abs <= 0:
                    rt["positions"].pop(symbol, None)
                else:
                    pos["qty"] = -rem_abs
                    pos["entry_charges"] = max(0.0, float(pos.get("entry_charges", 0.0)) - prop_entry_chg)
            rt["last_note"] = f"COVER {close_qty} {symbol} @ ₹{price:.2f} | Net ₹{net_pnl:+,.2f} ({reason})."
            trade = self._record_trade_db(
                deployment_id=rt["id"],
                symbol=symbol,
                side="COVER",
                qty=close_qty,
                entry_price=entry_px,
                exit_price=price,
                gross_pnl=gross_pnl,
                charges=total_rt_chg,
                net_pnl=net_pnl,
                reason=reason,
                broker_order_id=broker_order_id,
            )
            return {"kind": "trade", "trade": trade, "broker_order_id": broker_order_id}

        # SELL closing LONG position (supports partial exit)
        old_qty = int(pos["qty"]) if pos else qty
        close_qty = min(qty, old_qty) if pos else qty
        entry_px = float(pos["entry_price"]) if pos else price
        prop_entry_chg = (float(pos.get("entry_charges", 0.0)) * (close_qty / float(old_qty))) if (pos and old_qty > 0) else 0.0
        gross_pnl = (price - entry_px) * close_qty
        total_rt_chg = prop_entry_chg + leg.total_charges
        net_pnl = gross_pnl - total_rt_chg
        rt["cash"] = round(rt["cash"] + (close_qty * price) - leg.total_charges, 2)
        rt["realized_pnl"] = round(rt["realized_pnl"] + net_pnl, 2)
        if pos is not None:
            rem_qty = old_qty - close_qty
            if rem_qty <= 0:
                rt["positions"].pop(symbol, None)
            else:
                pos["qty"] = rem_qty
                pos["entry_charges"] = max(0.0, float(pos.get("entry_charges", 0.0)) - prop_entry_chg)
        rt["last_note"] = f"SELL {close_qty} {symbol} @ ₹{price:.2f} | Net ₹{net_pnl:+,.2f} ({reason})."
        trade = self._record_trade_db(
            deployment_id=rt["id"],
            symbol=symbol,
            side="SELL",
            qty=close_qty,
            entry_price=entry_px,
            exit_price=price,
            gross_pnl=gross_pnl,
            charges=total_rt_chg,
            net_pnl=net_pnl,
            reason=reason,
            broker_order_id=broker_order_id,
        )
        return {"kind": "trade", "trade": trade, "broker_order_id": broker_order_id}

    def _execute_paper_fill_locked(
        self,
        rt: Dict[str, Any],
        symbol: str,
        side: str,
        qty: int,
        price: float,
        reason: str,
        cfg: ChargesConfig,
        broker_order_id: str = "",
    ) -> Dict[str, Any]:
        """Backwards-compatible alias delegating to `_apply_verified_fill_locked`."""
        return self._apply_verified_fill_locked(
            rt, symbol, side, qty, price, reason, cfg, broker_order_id=broker_order_id
        )

    def _dispatch_live_broker_order_locked(
        self,
        rt: Dict[str, Any],
        symbol: str,
        side: str,
        qty: int,
        price: float,
        reason: str,
        cfg: ChargesConfig,
        *,
        client_order_id: str = "",
    ) -> Dict[str, Any]:
        if self._require_live_env_lock and not is_live_trading_enabled():
            msg = "Live trading master lock is ON (set ALGODESK_ENABLE_LIVE_TRADING=true to enable real broker orders)."
            rt["last_note"] = f"Live order blocked: {msg}"
            self._audit.log(
                event_type="LIVE_ORDER_BLOCKED_MASTER_LOCK",
                actor="deploy_engine",
                payload={"deployment_id": rt["id"], "symbol": symbol, "side": side, "qty": qty},
            )
            return {"kind": "blocked", "symbol": symbol, "side": side, "reasons": [msg]}

        trading_broker = str(self._selections.load().get("trading_broker") or "upstox").strip().lower()
        adapter = self._broker_resolver(trading_broker)
        req = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=qty,
            exchange=rt["exchange"],
            instrument_key=rt["instrument_keys"].get(symbol, ""),
            order_type="MARKET",
            product=str(rt["spec"].get("product") or "MIS"),
            client_order_id=client_order_id,
            tag=f"adk:{rt['id'][:8]}",
            algo_tag=f"ALGODESK-{rt['strategy_id'][:12].upper()}",
            metadata={"exchange": rt["exchange"], "deployment_id": rt["id"], "reason": reason},
        )
        order_rec, delta_qty, fill_px = self._oms.submit_order(
            req,
            adapter,
            deployment_id=rt["id"],
            exec_mode=rt["exec_mode"],
            reason=reason,
            reference_price=price,
        )
        oid = str(order_rec.get("broker_order_id") or order_rec.get("client_order_id") or "")
        status = str(order_rec.get("status") or "")

        if status == OrderStatus.REJECTED.value:
            err_msg = str(order_rec.get("rejection_reason") or "Order rejected by broker")
            rt["last_note"] = f"Live order REJECTED on {trading_broker.upper()}: {err_msg}"
            return {
                "kind": "rejected",
                "symbol": symbol,
                "side": side,
                "broker_order_id": oid,
                "order": order_rec,
                "error": err_msg,
            }

        self._audit.log(
            event_type="LIVE_ORDER_SUBMITTED",
            actor="deploy_engine",
            payload={
                "deployment_id": rt["id"],
                "broker": trading_broker,
                "client_order_id": order_rec["client_order_id"],
                "broker_order_id": oid,
                "symbol": symbol,
                "exchange": rt["exchange"],
                "product": req.product,
                "side": side,
                "qty": qty,
                "status": status,
                "filled_qty": int(order_rec.get("filled_quantity") or 0),
                "reason": reason,
            },
        )

        # CRITICAL P0 INVARIANT: ONLY update positions/cash when broker explicitly reports a verified fill delta!
        if delta_qty > 0:
            res = self._apply_verified_fill_locked(
                rt,
                symbol,
                side,
                delta_qty,
                fill_px or price,
                reason,
                cfg,
                broker_order_id=oid,
            )
            res["order"] = order_rec
            return res

        rt["last_note"] = (
            f"Live order {status} on {trading_broker.upper()}: {side} {qty} {rt['exchange']}:{symbol} "
            f"(Order {oid}, awaiting broker fill confirmation)."
        )
        return {
            "kind": "submitted",
            "symbol": symbol,
            "side": side,
            "status": status,
            "broker_order_id": oid,
            "order": order_rec,
        }

    def reconcile_orders(
        self,
        *,
        deployment_id: Optional[str] = None,
        broker_adapter: Optional[BrokerBase] = None,
    ) -> Dict[str, Any]:
        """Reconcile active OMS orders against the authoritative broker order book and apply verified fill deltas."""
        trading_broker = str(self._selections.load().get("trading_broker") or "upstox").strip().lower()
        adapter = broker_adapter or self._broker_resolver(trading_broker)

        def _on_fill(order_rec: Dict[str, Any], delta_qty: int, fill_px: float) -> None:
            did = str(order_rec.get("deployment_id") or "")
            if not did:
                return
            with self._lock:
                rt = self._runtime.get(did)
                if not rt:
                    return
                cfg = self._charges_cfg_for(rt["spec"])
                self._apply_verified_fill_locked(
                    rt,
                    str(order_rec["symbol"]),
                    str(order_rec["side"]),
                    int(delta_qty),
                    float(fill_px or order_rec.get("limit_price") or 0.0),
                    str(order_rec.get("reason") or "BROKER_RECONCILED_FILL"),
                    cfg,
                    broker_order_id=str(order_rec.get("broker_order_id") or ""),
                )
                unreal = sum(
                    (float(p["last_price"]) - float(p["entry_price"])) * int(p["qty"])
                    for p in rt["positions"].values()
                )
                rt["unrealized_pnl"] = round(unreal, 2)
                self._persist_deployment_locked(rt)

        result = self._oms.reconcile_orders(adapter, deployment_id=deployment_id, on_fill=_on_fill)
        if result.get("fills_applied"):
            self._broadcast("deployments:update")
        return result

    # ── Operator Controls (Pause / Resume / Square-Off / Stop / Alerts) ──

    def control(self, deployment_id: str, action: str) -> Dict[str, Any]:
        act = (action or "").strip().lower()
        with self._lock:
            rt = self._runtime.get(deployment_id)
            if not rt:
                raise ValueError(f"Deployment '{deployment_id}' not found.")

            if act == "pause":
                rt["stop_event"].set()
                rt["status"] = "PAUSED"
                rt["last_note"] = "Paused by operator."
                self._persist_deployment_locked(rt)
            elif act == "resume":
                if rt["status"] == "STOPPED":
                    raise ValueError("Stopped deployments cannot be resumed; deploy a fresh instance.")
                rt["status"] = "WARMING_UP"
                rt["last_note"] = "Resuming strategy stream..."
                self._persist_deployment_locked(rt)
                self._start_worker(deployment_id)
            elif act == "square_off":
                cfg = self._charges_cfg_for(rt["spec"])
                for psym in list(rt["positions"].keys()):
                    pos = rt["positions"][psym]
                    px = float(pos["last_price"] or pos["entry_price"])
                    if rt["exec_mode"] == "live_auto":
                        self._dispatch_live_broker_order_locked(rt, psym, "SELL", int(pos["qty"]), px, "OPERATOR_SQUARE_OFF", cfg)
                    elif rt["exec_mode"] == "live_alert":
                        self._create_alert_db(
                            deployment_id=rt["id"],
                            deployment_name=rt["name"],
                            symbol=psym,
                            exchange=rt["exchange"],
                            instrument_key=rt["instrument_keys"].get(psym, ""),
                            side="SELL",
                            qty=int(pos["qty"]),
                            signal_price=px,
                            reason="OPERATOR_SQUARE_OFF",
                            product=str(rt["spec"].get("product") or "MIS"),
                        )
                    else:
                        self._execute_paper_fill_locked(rt, psym, "SELL", int(pos["qty"]), px, "OPERATOR_SQUARE_OFF", cfg)
                rt["unrealized_pnl"] = 0.0
                rt["last_note"] = "Square-off processed."
                self._persist_deployment_locked(rt)
            elif act == "stop":
                rt["stop_event"].set()
                rt["status"] = "STOPPED"
                rt["last_note"] = "Stopped by operator."
                self._persist_deployment_locked(rt)
            elif act == "delete":
                rt["stop_event"].set()
                self._runtime.pop(deployment_id, None)
                with self._connect() as conn:
                    conn.execute("DELETE FROM deployment_positions WHERE deployment_id=?", (deployment_id,))
                    conn.execute("DELETE FROM deployment_trades WHERE deployment_id=?", (deployment_id,))
                    conn.execute("DELETE FROM actionable_alerts WHERE deployment_id=?", (deployment_id,))
                    conn.execute("DELETE FROM deployments WHERE id=?", (deployment_id,))
                self._refresh_deploy_ws_scope()
                self._broadcast("deployments:update")
                return {"ok": True, "deleted": deployment_id}
            else:
                raise ValueError(f"Unsupported deployment action '{action}'.")

        self._refresh_deploy_ws_scope()
        self._broadcast("deployments:update")
        return self.get_deployment(deployment_id)

    def resolve_alert(self, alert_id: str, action: str) -> Dict[str, Any]:
        act = (action or "").strip().lower()
        if act not in {"execute", "dismiss"}:
            raise ValueError("Alert action must be 'execute' or 'dismiss'.")

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM actionable_alerts WHERE id=?", (alert_id,)).fetchone()
            if not row:
                raise ValueError(f"Alert '{alert_id}' not found.")
            if str(row["status"]) != "PENDING":
                raise ValueError(f"Alert '{alert_id}' is already {row['status']}.")
            alert = dict(row)

        now = _now_ist()
        if act == "dismiss":
            with self._connect() as conn:
                conn.execute(
                    "UPDATE actionable_alerts SET status='DISMISSED', note='Dismissed by operator', resolved_at_ist=? WHERE id=?",
                    (now, alert_id),
                )
            self._broadcast("deployments:update")
            return {"ok": True, "alert_id": alert_id, "status": "DISMISSED"}

        # Execute 1-Click Actionable Order (still strictly gated by Kill Switch & Risk Limits!)
        did = str(alert["deployment_id"])
        with self._lock:
            rt = self._runtime.get(did)
            if not rt:
                raise ValueError(f"Parent deployment '{did}' not found.")
            open_count = len(rt["positions"])
            gross_exp = sum(p["qty"] * p["last_price"] for p in rt["positions"].values())
            check = self._risk.validate_order(
                symbol=str(alert["symbol"]),
                quantity=int(alert["qty"]),
                price=float(alert["signal_price"]),
                transaction_type=str(alert["side"]),
                current_open_positions=open_count,
                current_gross_exposure_inr=gross_exp,
            )
            if not check["allowed"]:
                reason_txt = "; ".join(check["reasons"])
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE actionable_alerts SET status='BLOCKED', note=?, resolved_at_ist=? WHERE id=?",
                        (reason_txt, now, alert_id),
                    )
                self._broadcast("deployments:update")
                return {"ok": False, "alert_id": alert_id, "status": "BLOCKED", "reasons": check["reasons"]}

            cfg = self._charges_cfg_for(rt["spec"])
            exec_res = self._dispatch_live_broker_order_locked(
                rt,
                str(alert["symbol"]),
                str(alert["side"]),
                int(alert["qty"]),
                float(alert["signal_price"]),
                f"1CLICK_APPROVED:{alert['reason']}",
                cfg,
            )
            self._persist_deployment_locked(rt)

        if exec_res.get("kind") in {"error", "rejected", "blocked"}:
            err_txt = str(
                exec_res.get("error")
                or ("; ".join(exec_res.get("reasons") or []))
                or "Broker order failed"
            )
            st_val = "BLOCKED" if exec_res.get("kind") == "blocked" else "FAILED"
            with self._connect() as conn:
                conn.execute(
                    "UPDATE actionable_alerts SET status=?, note=?, resolved_at_ist=? WHERE id=?",
                    (st_val, err_txt, now, alert_id),
                )
            self._broadcast("deployments:update")
            return {"ok": False, "alert_id": alert_id, "status": st_val, "error": err_txt}

        oid = str(exec_res.get("broker_order_id") or "")
        with self._connect() as conn:
            conn.execute(
                "UPDATE actionable_alerts SET status='EXECUTED', broker_order_id=?, note='Submitted via 1-Click Live Order', resolved_at_ist=? WHERE id=?",
                (oid, now, alert_id),
            )
        self._broadcast("deployments:update")
        return {"ok": True, "alert_id": alert_id, "status": "EXECUTED", "broker_order_id": oid, "order": exec_res.get("order")}

    # ── Read Views ───────────────────────────────────────────────────────

    def _serialize_deployment_locked(
        self,
        rt: Dict[str, Any],
        trades: List[Dict[str, Any]],
        orders: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        positions = list(rt["positions"].values())
        equity = float(rt["cash"]) + sum(
            (abs(int(p["qty"])) * float(p["entry_price"]))
            + ((float(p["last_price"]) - float(p["entry_price"])) * int(p["qty"]))
            for p in positions
        )
        net_pnl = float(rt["realized_pnl"]) + float(rt["unrealized_pnl"])
        ret_pct = (net_pnl / max(1.0, float(rt["initial_capital"]))) * 100.0
        dep_orders = orders if orders is not None else self._oms.list_orders(deployment_id=rt["id"], limit=40)
        return {
            "id": rt["id"],
            "name": rt["name"],
            "exec_mode": rt["exec_mode"],
            "status": rt["status"],
            "strategy_id": rt["strategy_id"],
            "strategy_name": rt["spec"].get("strategy_name") or rt["strategy_id"],
            "strategy_mode": rt["strategy_mode"],
            "symbols": list(rt["spec"].get("symbols") or []),
            "exchange": rt["exchange"],
            "product": rt["spec"].get("product", "MIS"),
            "evaluation_mode": rt["spec"].get("evaluation_mode", "completed_bar"),
            "timeframe": rt["timeframe"],
            "allocation_mode": rt["spec"].get("allocation_mode", "shared_pool"),
            "charges_profile": rt["spec"].get("charges_profile", "zerodha"),
            "segment": rt["spec"].get("segment", "equity_intraday"),
            "stop_loss_pct": rt["spec"].get("stop_loss_pct", 0.0),
            "take_profit_pct": rt["spec"].get("take_profit_pct", 0.0),
            "trailing_sl_pct": rt["spec"].get("trailing_sl_pct", 0.0),
            "initial_capital": round(float(rt["initial_capital"]), 2),
            "cash": round(float(rt["cash"]), 2),
            "equity": round(equity, 2),
            "realized_pnl": round(float(rt["realized_pnl"]), 2),
            "unrealized_pnl": round(float(rt["unrealized_pnl"]), 2),
            "net_pnl": round(net_pnl, 2),
            "return_pct": round(ret_pct, 2),
            "total_charges": round(float(rt["total_charges"]), 2),
            "open_positions_count": len(positions),
            "positions": positions,
            "orders_count": len(dep_orders),
            "orders": dep_orders[:40],
            "trades_count": len(trades),
            "trades": trades[:40],
            "last_note": rt.get("last_note", ""),
            "created_at_ist": rt["created_at_ist"],
            "updated_at_ist": rt["updated_at_ist"],
        }

    def get_deployment(self, deployment_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            trade_rows = conn.execute(
                "SELECT * FROM deployment_trades WHERE deployment_id=? ORDER BY executed_at_ist DESC LIMIT 50",
                (deployment_id,),
            ).fetchall()
            trades = [dict(r) for r in trade_rows]
        dep_orders = self._oms.list_orders(deployment_id=deployment_id, limit=40)
        with self._lock:
            rt = self._runtime.get(deployment_id)
            if not rt:
                raise ValueError(f"Deployment '{deployment_id}' not found.")
            return {"ok": True, "deployment": self._serialize_deployment_locked(rt, trades, dep_orders)}

    def list_deployments(self) -> Dict[str, Any]:
        with self._connect() as conn:
            all_trades = conn.execute(
                "SELECT * FROM deployment_trades ORDER BY executed_at_ist DESC LIMIT 200"
            ).fetchall()
            alert_rows = conn.execute(
                "SELECT * FROM actionable_alerts ORDER BY created_at_ist DESC LIMIT 60"
            ).fetchall()

        all_orders = self._oms.list_orders(limit=200)
        orders_by_dep: Dict[str, List[Dict[str, Any]]] = {}
        for o in all_orders:
            orders_by_dep.setdefault(str(o.get("deployment_id") or ""), []).append(o)

        trades_by_dep: Dict[str, List[Dict[str, Any]]] = {}
        for tr in all_trades:
            d = dict(tr)
            trades_by_dep.setdefault(str(d["deployment_id"]), []).append(d)

        alerts = [dict(r) for r in alert_rows]
        pending_alerts = [a for a in alerts if a["status"] == "PENDING"]

        deployments: List[Dict[str, Any]] = []
        with self._lock:
            for rt in sorted(self._runtime.values(), key=lambda x: str(x["created_at_ist"]), reverse=True):
                deployments.append(
                    self._serialize_deployment_locked(
                        rt,
                        trades_by_dep.get(rt["id"], []),
                        orders_by_dep.get(rt["id"], []),
                    )
                )

        paper_deps = [d for d in deployments if d["exec_mode"] == "paper"]
        live_deps = [d for d in deployments if d["exec_mode"] != "paper"]

        return {
            "ok": True,
            "summary": {
                "total_deployments": len(deployments),
                "running_count": sum(1 for d in deployments if d["status"] == "RUNNING"),
                "paper_count": len(paper_deps),
                "live_count": len(live_deps),
                "paper_net_pnl": round(sum(d["net_pnl"] for d in paper_deps), 2),
                "live_net_pnl": round(sum(d["net_pnl"] for d in live_deps), 2),
                "total_charges": round(sum(d["total_charges"] for d in deployments), 2),
                "pending_alerts_count": len(pending_alerts),
                "active_oms_orders_count": sum(
                    1 for o in all_orders if str(o.get("status") or "") not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
                ),
            },
            "deployments": deployments,
            "orders": all_orders[:60],
            "alerts": alerts,
            "pending_alerts": pending_alerts,
            "kill_switch": self._kill_switch.status(),
            "live_trading_enabled": is_live_trading_enabled(),
            "generated_at_ist": _now_ist(),
        }


deployment_manager = DeploymentManager()

__all__ = ["DeploymentManager", "deployment_manager"]
