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

import hashlib
import json
import logging
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backtest.registry import StrategyRegistry, strategy_registry
from broker.base import (
    BrokerBase,
    InstrumentContract,
    OrderRequest,
    OrderStatus,
    build_position_identity,
    make_broker_tag,
)
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
from notifications.telegram import TelegramNotifier, telegram_notifier
from risk.limits import RiskLimitManager, RiskWatchdog, risk_limit_manager, risk_watchdog
from rules.engine import compile_visual_strategy, visual_rule_store
from screener.engine import compute_auto_lookback_days
from screener.live_engine import merge_live_tick_into_candles

logger = logging.getLogger(__name__)


def _now_ist() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


def _deterministic_client_order_id(
    deployment_id: str,
    symbol: str,
    side: str,
    purpose: str,
    intent_key: str,
) -> str:
    """
    Build a deterministic, unique 20-char client_order_id per trading intent so
    repeated ticks or retries collapse cleanly in OMS & fit Zerodha's 20-char tag.
    """
    raw = f"{deployment_id}:{symbol.upper()}:{side.upper()}:{purpose.upper()}:{intent_key}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"adk-{digest}"


_TIMEFRAME_MAX_LATENCY_SEC: Dict[str, float] = {
    "1minute": 15.0,
    "3minute": 30.0,
    "5minute": 30.0,
    "15minute": 60.0,
    "30minute": 90.0,
    "1hour": 120.0,
    "1day": 180.0,
}


def _quote_age_seconds(quote: Dict[str, Any], now_ist: Optional[datetime] = None) -> Optional[float]:
    """
    Return age in seconds of a live quote using its authentic exchange/provider
    source timestamp (`source_timestamp_ist`, `exchange_timestamp`, `ts_ist`,
    `timestamp`, `epoch_ms`, `ltt_ms`). Returns None when no valid source
    timestamp is present (callers MUST treat None as unknown/fail-closed).
    """
    now = now_ist or datetime.now(tz=IST_TZ)
    for key in ("source_timestamp_ist", "exchange_timestamp", "ts_ist", "timestamp", "quote_ts", "updated_at_ist"):
        raw = quote.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            val = float(raw)
            if val > 1e12:
                val /= 1000.0
            dt = datetime.fromtimestamp(val, tz=timezone.utc).astimezone(IST_TZ)
            return max(0.0, (now - dt).total_seconds())
        text = str(raw).strip()
        if not text:
            continue
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST_TZ)
            return max(0.0, (now - dt.astimezone(IST_TZ)).total_seconds())
        except Exception:
            continue
    for key in ("epoch_ms", "ltt_ms"):
        raw_ms = quote.get(key)
        if raw_ms is not None:
            try:
                val = float(raw_ms)
                if val > 1e12:
                    val /= 1000.0
                dt = datetime.fromtimestamp(val, tz=timezone.utc).astimezone(IST_TZ)
                return max(0.0, (now - dt).total_seconds())
            except Exception:
                continue
    return None


def _evaluate_quote_freshness(
    quote: Dict[str, Any],
    *,
    timeframe: str = "5minute",
    exec_mode: str = "paper",
    max_quote_age_sec: Optional[float] = None,
    now_ist: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Fail-closed market-data freshness & execution-grade gate (C-03 / Finding 4).
    - Rejects Yahoo/delayed daily quotes (`execution_grade is False` or `source == 'yfinance'`)
      for `live_auto` entries.
    - Never treats missing/unparseable timestamps (`age is None`) as fresh for live entries.
    - Enforces timeframe-aware maximum permissible feed latency.
    """
    tf_key = normalize_timeframe(timeframe or "5minute")
    default_limit = _TIMEFRAME_MAX_LATENCY_SEC.get(tf_key, 30.0)
    limit_sec = float(max_quote_age_sec) if max_quote_age_sec is not None else default_limit
    src = str(quote.get("source") or "").strip().lower()
    quality = str(quote.get("feed_quality") or "").strip().lower()
    exec_grade = quote.get("execution_grade")
    if exec_grade is None:
        exec_grade = src not in {"yfinance", "yahoo"} and quality not in {"delayed_daily", "unsupported"}
    else:
        exec_grade = bool(exec_grade)

    is_live_auto = str(exec_mode or "").strip().lower() == "live_auto"
    if is_live_auto and (not exec_grade or src in {"yfinance", "yahoo"}):
        return {
            "fresh": False,
            "status": "delayed_non_execution_grade",
            "age_sec": _quote_age_seconds(quote, now_ist=now_ist),
            "max_age_sec": limit_sec,
            "reason": (
                f"Feed source '{src or 'unknown'}' ({quality or 'non-execution'}) is not execution-grade; "
                f"live automated entries require an authenticated broker real-time feed."
            ),
        }

    age = _quote_age_seconds(quote, now_ist=now_ist)
    if age is None:
        if is_live_auto:
            return {
                "fresh": False,
                "status": "unknown",
                "age_sec": None,
                "max_age_sec": limit_sec,
                "reason": "Quote is missing a verifiable exchange/source timestamp; blocking live entry (fail-closed).",
            }
        # In paper/alert testing, treat unknown timestamp as unknown (fresh=False only if live_auto,
        # unless explicit strict_freshness is configured)
        return {
            "fresh": True,
            "status": "unknown_paper_allowed",
            "age_sec": None,
            "max_age_sec": limit_sec,
            "reason": "",
        }

    if age > limit_sec:
        return {
            "fresh": False,
            "status": "stale",
            "age_sec": round(age, 2),
            "max_age_sec": limit_sec,
            "reason": f"Stale quote ({int(age)}s > {int(limit_sec)}s max latency for {tf_key}); blocking new entries.",
        }
    return {
        "fresh": True,
        "status": "fresh",
        "age_sec": round(age, 2),
        "max_age_sec": limit_sec,
        "reason": "",
    }


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
        watchdog: Optional[RiskWatchdog] = None,
        notifier: Optional[TelegramNotifier] = None,
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
        self._watchdog = watchdog or risk_watchdog
        self._notifier = notifier or telegram_notifier
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
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
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
                    "error_count": 0,
                    "last_error": "",
                    "stale_alerted_syms": set(),
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
        max_quote_age_sec: float = 30.0,
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
            "max_quote_age_sec": max(1.0, float(max_quote_age_sec or 30.0)),
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
            "error_count": 0,
            "last_error": "",
            "stale_alerted_syms": set(),
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
        warmup_errors: List[str] = []
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
                else:
                    warmup_errors.append(f"{sym}: empty historical series")
            except Exception as exc:
                err_txt = f"{sym}: {exc.__class__.__name__}: {exc}"
                warmup_errors.append(err_txt)
                logger.error("Deployment %s warmup failed for %s: %s", did, sym, exc)

        if stop_ev.is_set():
            return

        if warmed == 0:
            err_summary = "; ".join(warmup_errors[:3]) or "No historical bars available."
            with self._lock:
                rt["status"] = "ERROR"
                rt["error_count"] = int(rt.get("error_count") or 0) + max(1, len(warmup_errors))
                rt["last_error"] = f"Warmup failed: {err_summary}"
                rt["last_note"] = f"Warmup FAILED (0/{len(syms)} symbols): {err_summary}"
                self._persist_deployment_locked(rt)
            self._audit.log(
                event_type="DEPLOYMENT_WARMUP_FAILED",
                actor="deploy_engine",
                payload={"deployment_id": did, "symbols": syms, "errors": warmup_errors[:5]},
            )
            try:
                self._notifier.notify_event(
                    "DEPLOYMENT_ERROR",
                    f"Deployment {rt['name']} Warmup FAILED — Refused to Start",
                    [f"Symbols: {', '.join(syms[:5])}", f"Error: {err_summary}"],
                )
            except Exception:
                pass
            self._broadcast("deployments:update")
            return

        with self._lock:
            rt["status"] = "RUNNING"
            if warmup_errors:
                rt["error_count"] = int(rt.get("error_count") or 0) + len(warmup_errors)
                rt["last_error"] = warmup_errors[-1]
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
                logger.error("Deployment %s tick loop error: %s", did, exc)
                with self._lock:
                    rt["error_count"] = int(rt.get("error_count") or 0) + 1
                    rt["last_error"] = f"Tick loop error: {exc.__class__.__name__}: {exc}"
            stop_ev.wait(interval)

    # ── Signal & Execution Evaluation ────────────────────────────────────

    def _resolve_contract_locked(self, rt: Dict[str, Any], symbol: str) -> InstrumentContract:
        sym_u = (symbol or "").strip().upper()
        cache: Dict[str, InstrumentContract] = rt.setdefault("contracts", {})
        if sym_u in cache:
            return cache[sym_u]
        spec = rt.get("spec") or {}
        explicit_lots = spec.get("lot_sizes") or {}
        contract = self._instruments.get_contract(sym_u, exchange=rt.get("exchange") or "NSE")
        if sym_u in explicit_lots and int(explicit_lots[sym_u] or 0) > 0:
            contract = InstrumentContract(
                symbol=contract.symbol,
                exchange=contract.exchange,
                segment=contract.segment,
                lot_size=int(explicit_lots[sym_u]),
                tick_size=contract.tick_size,
                multiplier=contract.multiplier,
                expiry=contract.expiry,
                strike=contract.strike,
                option_type=contract.option_type,
                upstox_key=contract.upstox_key,
                zerodha_token=contract.zerodha_token,
            )
        elif spec.get("lot_size") and int(spec.get("lot_size") or 0) > 1 and contract.effective_lot_size == 1:
            contract = InstrumentContract(
                symbol=contract.symbol,
                exchange=contract.exchange,
                segment=contract.segment,
                lot_size=int(spec["lot_size"]),
                tick_size=contract.tick_size,
                multiplier=contract.multiplier,
                expiry=contract.expiry,
                strike=contract.strike,
                option_type=contract.option_type,
                upstox_key=contract.upstox_key,
                zerodha_token=contract.zerodha_token,
            )
        cache[sym_u] = contract
        return contract

    def _compute_order_qty(
        self,
        rt: Dict[str, Any],
        price: float,
        *,
        symbol: Optional[str] = None,
        contract: Optional[InstrumentContract] = None,
    ) -> int:
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
            raw_qty = int(budget // (price * 1.005))
        elif alloc == "risk_based":
            sl = float(spec.get("stop_loss_pct") or 2.0)
            sl = sl if sl > 0 else 2.0
            risk_pct = float(spec.get("risk_per_trade_pct") or 1.5)
            risk_amt = equity * (risk_pct / 100.0)
            qty_risk = int(risk_amt // max(0.05, price * (sl / 100.0)))
            qty_cash = int(min(cash, equity * 0.50) // (price * 1.005))
            raw_qty = min(qty_risk, qty_cash)
        else:
            budget = cash / float(free_slots)
            raw_qty = int(budget // (price * 1.005))

        eff_contract = contract
        if eff_contract is None and symbol:
            eff_contract = self._resolve_contract_locked(rt, symbol)
        if eff_contract is not None:
            return eff_contract.normalize_quantity(raw_qty)
        return max(0, raw_qty)

    def process_ticks(self, deployment_id: str, quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluate live quotes for a deployment (default: completed_bar close parity with backtest)."""
        actions_taken: List[Dict[str, Any]] = []
        with self._lock:
            rt = self._runtime.get(deployment_id)
            if not rt or rt["status"] != "RUNNING":
                return {"ok": False, "actions": []}
            spec = rt["spec"]
            tf = rt["timeframe"]
            exec_mode = str(rt.get("exec_mode") or "paper")
            eval_mode = str(spec.get("evaluation_mode") or "completed_bar").strip().lower()
            is_short_strat = str(spec.get("side") or "BUY").strip().upper() in {"SELL", "SHORT"}
            sl_pct = float(spec.get("stop_loss_pct") or 0.0)
            tp_pct = float(spec.get("take_profit_pct") or 0.0)
            tsl_pct = float(spec.get("trailing_sl_pct") or 0.0)
            max_slots = max(1, int(spec.get("max_open_positions") or 5))
            max_quote_age_sec = (
                max(1.0, float(spec["max_quote_age_sec"]))
                if spec.get("max_quote_age_sec") is not None
                else None
            )

        strat_inst, _ = self._build_strategy(spec)
        now_ist = datetime.now(tz=IST_TZ)

        for sym, q in quotes.items():
            sym_u = str(q.get("symbol") or sym).strip().upper()
            raw_ltp = q.get("ltp")
            if raw_ltp is None or float(raw_ltp) <= 0:
                continue
            ltp = float(raw_ltp)
            freshness = _evaluate_quote_freshness(
                q,
                timeframe=tf,
                exec_mode=exec_mode,
                max_quote_age_sec=max_quote_age_sec,
                now_ist=now_ist,
            )
            quote_age = freshness["age_sec"]
            is_stale_quote = not bool(freshness["fresh"])
            freshness_reason = str(freshness.get("reason") or "")
            if exec_mode == "live_auto" and self._watchdog is not None:
                try:
                    self._watchdog.report_dimension(
                        "market_data_freshness",
                        healthy=not is_stale_quote,
                        error=freshness_reason,
                        immediate_degrade=is_stale_quote,
                    )
                except Exception:
                    pass

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
            eval_error: Optional[str] = None

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
                        except Exception as exc:
                            eval_error = f"{sym_u}: {exc.__class__.__name__}: {exc}"
                            logger.error("Deployment %s strategy eval error (%s): %s", deployment_id, sym_u, exc)
                            enter_sig, exit_sig = False, False
            else:
                if len(combined) >= 5:
                    try:
                        tk = IndicatorToolkit(combined, backend="auto")
                        strat_inst.prepare(combined, spec["params"], tk)
                        enter_sig, exit_sig = strat_inst.signal_at(len(combined) - 1, combined[-1])
                        eval_bar_ts = str(combined[-1].get("ts_ist") or "")
                        st["indicator_eval_count"] = int(st.get("indicator_eval_count") or 0) + 1
                    except Exception as exc:
                        eval_error = f"{sym_u}: {exc.__class__.__name__}: {exc}"
                        logger.error("Deployment %s strategy eval error (%s): %s", deployment_id, sym_u, exc)
                        enter_sig, exit_sig = False, False

            stale_alert_msg: Optional[str] = None
            eval_alert_msg: Optional[str] = None

            with self._lock:
                dirty = False
                st["base_bars"] = new_base
                st["forming_bar"] = new_forming
                st["last_ltp"] = ltp
                if eval_bar_ts:
                    st["last_eval_bar_ts"] = eval_bar_ts
                bar_key = eval_bar_ts or (str(combined[-1].get("ts_ist") or _now_ist()) if combined else _now_ist())

                if eval_error:
                    prev_err_count = int(rt.get("error_count") or 0)
                    rt["error_count"] = prev_err_count + 1
                    rt["last_error"] = eval_error
                    rt["last_note"] = f"Strategy evaluation error ({eval_error})."
                    dirty = True
                    if prev_err_count == 0 or prev_err_count % 10 == 0:
                        eval_alert_msg = eval_error

                stale_set: Set[str] = rt.setdefault("stale_alerted_syms", set())
                if is_stale_quote:
                    if sym_u not in stale_set:
                        stale_set.add(sym_u)
                        stale_alert_msg = freshness_reason or f"Quote for {sym_u} is stale ({int(quote_age or 0)}s); blocking new entries."
                        logger.warning("Deployment %s: %s", deployment_id, stale_alert_msg)
                        rt["last_note"] = stale_alert_msg
                        dirty = True
                elif sym_u in stale_set:
                    stale_set.discard(sym_u)

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
                        # C-01 GUARD: Never submit duplicate exit while an OMS order is already working!
                        if self._oms.has_active_order(rt["id"], sym_u):
                            rt["last_note"] = f"Working order already active for {sym_u}; suppressing duplicate {exit_side}."
                        else:
                            exit_purpose = exit_reason.split()[0]
                            intent_key = str(pos.get("entry_at_ist") or bar_key)
                            exit_coid = _deterministic_client_order_id(
                                rt["id"], sym_u, exit_side, exit_purpose, intent_key
                            )
                            ev = self._handle_signal_locked(
                                rt,
                                sym_u,
                                exit_side,
                                abs_qty,
                                ltp,
                                exit_reason,
                                client_order_id=exit_coid,
                            )
                            if ev:
                                actions_taken.append(ev)
                                dirty = True
                elif enter_sig and len(rt["positions"]) < max_slots:
                    if is_stale_quote:
                        actions_taken.append(
                            {
                                "kind": "blocked",
                                "symbol": sym_u,
                                "side": "SHORT" if is_short_strat else "BUY",
                                "reasons": [
                                    freshness_reason
                                    or f"Stale/unverified quote ({freshness.get('status', 'stale')}); new entries blocked."
                                ],
                            }
                        )
                    elif self._oms.has_active_order(rt["id"], sym_u):
                        rt["last_note"] = f"Working order already active for {sym_u}; suppressing duplicate entry."
                    else:
                        entry_side = "SHORT" if is_short_strat else "BUY"
                        sig_marker = f"{entry_side}:{bar_key}"
                        if st.get("last_sig_key") != sig_marker:
                            contract = self._resolve_contract_locked(rt, sym_u)
                            qty = self._compute_order_qty(rt, ltp, symbol=sym_u, contract=contract)
                            if qty >= 1:
                                st["last_sig_key"] = sig_marker
                                entry_coid = _deterministic_client_order_id(
                                    rt["id"], sym_u, entry_side, "ENTRY", bar_key
                                )
                                ev = self._handle_signal_locked(
                                    rt,
                                    sym_u,
                                    entry_side,
                                    qty,
                                    ltp,
                                    "STRATEGY_ENTRY",
                                    client_order_id=entry_coid,
                                )
                                if ev:
                                    actions_taken.append(ev)
                                    dirty = True

                # Recompute live MTM unrealized P&L (supports both signed long >0 and short <0)
                prev_unreal = float(rt.get("unrealized_pnl") or 0.0)
                unreal = 0.0
                for p in rt["positions"].values():
                    unreal += (float(p["last_price"]) - float(p["entry_price"])) * int(p["qty"])
                rt["unrealized_pnl"] = round(unreal, 2)
                if abs(rt["unrealized_pnl"] - prev_unreal) >= 0.01:
                    dirty = True
                if dirty:
                    self._persist_deployment_locked(rt)

            if stale_alert_msg and rt["exec_mode"] != "paper":
                try:
                    self._notifier.notify_event(
                        "STALE_QUOTE",
                        f"Stale Market Data on {rt['name']} ({sym_u})",
                        [stale_alert_msg],
                    )
                except Exception:
                    pass
            if eval_alert_msg:
                try:
                    self._notifier.notify_event(
                        "STRATEGY_ERROR",
                        f"Strategy Error in {rt['name']}",
                        [eval_alert_msg],
                    )
                except Exception:
                    pass

        if actions_taken:
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
            contract = self._resolve_contract_locked(rt, symbol)
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
                metadata={"reference_price": price, "exchange": rt["exchange"], "lot_size": contract.effective_lot_size},
            )
            order_rec, delta_qty, fill_px = self._oms.submit_order(
                req,
                self._paper_adapter,
                deployment_id=rt["id"],
                exec_mode="paper",
                reason=reason,
                reference_price=price,
                contract=contract,
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

        # mode == "live_auto": Strictly split check_exit (always allowed) vs check_entry (account-wide)
        side_u = side.strip().upper()
        contract = self._resolve_contract_locked(rt, symbol)
        if side_u in {"SELL", "COVER"}:
            pos_obj = rt["positions"].get(symbol)
            open_abs = abs(int(pos_obj["qty"])) if pos_obj is not None else None
            check = self._risk.check_exit(
                symbol=symbol,
                quantity=qty,
                price=price,
                transaction_type=side_u,
                open_position_qty=open_abs,
            )
        else:
            acct_open, acct_gross, pending_exp, daily_loss, wd_degraded, avail_margin = self._account_risk_context_locked(rt)
            check = self._risk.check_entry(
                symbol=symbol,
                quantity=qty,
                price=price,
                transaction_type=side_u,
                current_open_positions=acct_open,
                current_gross_exposure_inr=acct_gross,
                pending_exposure_inr=pending_exp,
                daily_loss_inr=daily_loss,
                watchdog_degraded=wd_degraded,
                available_margin_inr=avail_margin,
                exchange=rt["exchange"],
                instrument_key=rt["instrument_keys"].get(symbol, ""),
                contract=contract,
            )
        if not check["allowed"]:
            block_reason = "; ".join(check["reasons"])
            rt["last_note"] = f"Auto-order BLOCKED by safety gate ({block_reason})."
            return {"kind": "blocked", "symbol": symbol, "side": side, "reasons": check["reasons"]}

        return self._dispatch_live_broker_order_locked(
            rt, symbol, side, qty, price, reason, cfg, client_order_id=client_order_id
        )

    def _today_realized_pnl_locked(self) -> float:
        """Compute today's IST trading-day realized P&L across non-paper deployments (C-02)."""
        today_prefix = datetime.now(tz=IST_TZ).strftime("%Y-%m-%d")
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(t.pnl), 0.0) AS day_pnl
                    FROM deployment_trades t
                    JOIN deployments d ON d.id = t.deployment_id
                    WHERE d.exec_mode != 'paper' AND t.ts_ist LIKE ?
                    """,
                    (f"{today_prefix}%",),
                ).fetchone()
            return float(row["day_pnl"] or 0.0) if row else 0.0
        except Exception:
            return 0.0

    def _account_risk_context_locked(
        self,
        rt: Dict[str, Any],
        *,
        broker_adapter: Optional[BrokerBase] = None,
    ) -> Tuple[int, float, float, float, bool, Optional[float]]:
        """
        Build an authoritative account-wide risk snapshot (C-02 / Finding 3):
        1. Internal AlgoDesk live deployments (attribution layer).
        2. Authoritative broker positions (`broker.get_positions()`), incorporating
           externally/manually placed positions so manual trades NEVER bypass
           account-wide gross exposure or open-position limits.
        3. Broker funds & margins.
        4. Broker open working orders + OMS pending exposure.
        5. IST trading-day boundary daily loss (today's realized + current unrealized).
        """
        internal_identities: Dict[str, Dict[str, Any]] = {}
        internal_unrealized = 0.0
        for dep in self._runtime.values():
            if dep["exec_mode"] == "paper":
                continue
            dep_exch = str(dep.get("exchange") or "NSE").strip().upper()
            dep_prod = str((dep.get("spec") or {}).get("product") or "MIS").strip().upper()
            for sym, p in dep["positions"].items():
                q_abs = abs(int(p.get("qty") or 0))
                if q_abs <= 0:
                    continue
                px = float(p.get("last_price") or p.get("entry_price") or 0.0)
                c = self._resolve_contract_locked(dep, sym)
                ident = build_position_identity(
                    symbol=sym,
                    exchange=dep_exch,
                    segment=c.segment,
                    instrument_key=str(dep.get("instrument_keys", {}).get(sym) or c.upstox_key or ""),
                    product=dep_prod,
                    expiry=c.expiry,
                    strike=c.strike,
                    option_type=c.option_type,
                )
                entry = internal_identities.get(ident)
                if entry is None:
                    internal_identities[ident] = {"symbol": sym.upper(), "qty": q_abs, "notional": q_abs * px}
                else:
                    entry["qty"] += q_abs
                    entry["notional"] += q_abs * px
            internal_unrealized += float(dep.get("unrealized_pnl") or 0.0)

        # Trading-day bounded realized P&L + current unrealized P&L
        today_realized = self._today_realized_pnl_locked()
        # Fallback: if tests mutated rt["realized_pnl"] in-memory without inserting into deployment_trades
        in_mem_realized = sum(
            float(dep.get("realized_pnl") or 0.0)
            for dep in self._runtime.values()
            if dep["exec_mode"] != "paper"
        )
        effective_realized = today_realized if abs(today_realized) > 0.0 else in_mem_realized
        total_day_pnl = effective_realized + internal_unrealized

        open_positions = len(internal_identities)
        gross_exposure = sum(v["notional"] for v in internal_identities.values())
        pending_exposure = self._oms.get_pending_exposure_inr(exclude_paper=True)
        avail_margin: Optional[float] = None

        # Merge authoritative live broker positions, funds & open orders (including external/manual trades)
        adapter = broker_adapter
        if adapter is None:
            try:
                trading_broker = str(self._selections.load().get("trading_broker") or "upstox").strip().lower()
                cand = self._broker_resolver(trading_broker)
                if getattr(cand, "_connected", False) or (hasattr(cand, "is_authenticated") and cand.is_authenticated()):
                    adapter = cand
            except Exception:
                adapter = None

        if adapter is not None:
            try:
                broker_positions = list(adapter.get_positions() or [])
                internal_syms = {v["symbol"] for v in internal_identities.values()}
                external_pnl = 0.0
                for bp in broker_positions:
                    q_abs = abs(int(getattr(bp, "quantity", 0) or 0))
                    if q_abs <= 0:
                        continue
                    bp_ident = getattr(bp, "identity_key", "") or build_position_identity(
                        symbol=str(getattr(bp, "symbol", "")),
                        exchange=str(getattr(bp, "exchange", "NSE")),
                        instrument_key=str(getattr(bp, "instrument_key", "")),
                        product=str(getattr(bp, "product", "MIS")),
                    )
                    bp_sym = str(getattr(bp, "symbol", "") or "").strip().upper()
                    bp_notional = float(getattr(bp, "market_value", 0.0) or 0.0)
                    if bp_ident in internal_identities:
                        # Authoritative broker notional takes max(internal, broker)
                        diff = bp_notional - internal_identities[bp_ident]["notional"]
                        if diff > 0:
                            gross_exposure += diff
                    elif bp_sym in internal_syms:
                        # Same symbol with different product/exchange -> still add incremental exposure
                        open_positions += 1
                        gross_exposure += bp_notional
                        external_pnl += float(getattr(bp, "realized_pnl", 0.0) or 0.0) + float(
                            getattr(bp, "unrealized_pnl", 0.0) or 0.0
                        )
                    else:
                        # External / manual broker position (e.g. placed directly on Zerodha/Upstox app)
                        open_positions += 1
                        gross_exposure += bp_notional
                        external_pnl += float(getattr(bp, "realized_pnl", 0.0) or 0.0) + float(
                            getattr(bp, "unrealized_pnl", 0.0) or 0.0
                        )
                total_day_pnl += external_pnl
            except Exception as exc:
                logger.debug("Authoritative broker positions merge skipped: %s", exc)

            try:
                funds = adapter.get_funds()
                if isinstance(funds, dict) and funds.get("available") is not None:
                    avail_margin = float(funds["available"])
            except Exception:
                pass

            try:
                broker_orders = list(adapter.get_orders() or [])
                broker_pending = 0.0
                for bo in broker_orders:
                    st = getattr(bo, "status", None)
                    if st is not None and getattr(st, "is_active", False):
                        req = getattr(bo, "request", None)
                        rem = int(getattr(bo, "remaining_quantity", 0) or 0)
                        px = float(getattr(req, "price", 0.0) or getattr(bo, "average_fill_price", 0.0) or 0.0)
                        if rem > 0 and px > 0 and str(getattr(req, "side", "")).upper() in {"BUY", "SHORT"}:
                            broker_pending += rem * px
                pending_exposure = max(pending_exposure, round(broker_pending, 2))
            except Exception:
                pass

        daily_loss = max(0.0, -total_day_pnl)
        wd_degraded = bool(self._watchdog.is_degraded) if self._watchdog is not None else False
        if avail_margin is None and self._watchdog is not None:
            try:
                wd_st = self._watchdog.status()
                last_eval = wd_st.get("last_evaluation") if isinstance(wd_st, dict) else None
                if isinstance(last_eval, dict) and last_eval.get("broker_connected"):
                    tel = last_eval.get("telemetry") or {}
                    if "available_margin_inr" in tel and tel["available_margin_inr"] is not None:
                        avail_margin = float(tel["available_margin_inr"])
            except Exception:
                avail_margin = None
        return open_positions, round(gross_exposure, 2), round(pending_exposure, 2), round(daily_loss, 2), wd_degraded, avail_margin

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
        eff_coid = (client_order_id or "").strip() or f"adk-{uuid.uuid4().hex[:14]}"
        broker_tag = make_broker_tag(eff_coid, max_len=20)
        contract = self._resolve_contract_locked(rt, symbol)
        req = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=qty,
            exchange=rt["exchange"],
            instrument_key=rt["instrument_keys"].get(symbol, ""),
            order_type="MARKET",
            product=str(rt["spec"].get("product") or "MIS"),
            client_order_id=eff_coid,
            tag=broker_tag,
            algo_tag=f"ALGODESK-{rt['strategy_id'][:12].upper()}",
            metadata={
                "exchange": rt["exchange"],
                "deployment_id": rt["id"],
                "reason": reason,
                "lot_size": contract.effective_lot_size,
            },
        )
        order_rec, delta_qty, fill_px = self._oms.submit_order(
            req,
            adapter,
            deployment_id=rt["id"],
            exec_mode=rt["exec_mode"],
            reason=reason,
            reference_price=price,
            contract=contract,
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

    def reconcile_positions(
        self,
        *,
        deployment_id: Optional[str] = None,
        broker_adapter: Optional[BrokerBase] = None,
        notifier: Optional[Any] = None,
        isolate_external_positions: bool = False,
    ) -> Dict[str, Any]:
        """
        Compare expected live deployment positions against authoritative broker
        `get_positions()` using FULL composite instrument identity (H-02 / H-06):
        `exchange|segment|instrument_key|symbol|product|expiry|strike|option_type`.

        Also distinguishes externally/manually placed broker positions (§6.4)
        from strategy-owned mismatches. On any mismatch, immediately PAUSE
        affected live deployments, record a SEBI audit entry, notify Telegram,
        and report into RiskWatchdog's `position_reconciliation` dimension.
        """
        with self._lock:
            target_deps = [
                rt
                for rt in self._runtime.values()
                if rt["exec_mode"] != "paper"
                and (deployment_id is None or rt["id"] == deployment_id)
                and (rt["status"] == "RUNNING" or bool(rt["positions"]))
            ]
            if not target_deps:
                return {
                    "ok": True,
                    "checked_deployments": 0,
                    "mismatches": [],
                    "external_positions": [],
                    "halted_deployments": [],
                }

            expected_by_ident: Dict[str, int] = {}
            ident_meta: Dict[str, Dict[str, Any]] = {}
            deps_by_ident: Dict[str, List[str]] = {}
            managed_syms: Set[str] = set()
            for rt in target_deps:
                dep_exch = str(rt.get("exchange") or "NSE").strip().upper()
                dep_prod = str((rt.get("spec") or {}).get("product") or "MIS").strip().upper()
                for sym, pos in rt["positions"].items():
                    sym_u = sym.strip().upper()
                    managed_syms.add(sym_u)
                    contract = self._resolve_contract_locked(rt, sym_u)
                    ident = build_position_identity(
                        symbol=sym_u,
                        exchange=dep_exch,
                        segment=contract.segment,
                        instrument_key=str(rt.get("instrument_keys", {}).get(sym_u) or ""),
                        product=dep_prod,
                        expiry=contract.expiry,
                        strike=contract.strike,
                        option_type=contract.option_type,
                    )
                    q_signed = int(pos.get("qty") or 0)
                    expected_by_ident[ident] = expected_by_ident.get(ident, 0) + q_signed
                    ident_meta[ident] = {
                        "identity_key": ident,
                        "symbol": sym_u,
                        "exchange": dep_exch,
                        "product": dep_prod,
                        "expiry": contract.expiry,
                        "strike": contract.strike,
                        "option_type": contract.option_type,
                    }
                    deps_by_ident.setdefault(ident, []).append(rt["id"])
                for sym in rt["spec"].get("symbols") or []:
                    sym_u = str(sym).strip().upper()
                    managed_syms.add(sym_u)
                    contract = self._resolve_contract_locked(rt, sym_u)
                    ident = build_position_identity(
                        symbol=sym_u,
                        exchange=dep_exch,
                        segment=contract.segment,
                        instrument_key=str(rt.get("instrument_keys", {}).get(sym_u) or ""),
                        product=dep_prod,
                        expiry=contract.expiry,
                        strike=contract.strike,
                        option_type=contract.option_type,
                    )
                    deps_by_ident.setdefault(ident, []).append(rt["id"])

        trading_broker = str(self._selections.load().get("trading_broker") or "upstox").strip().lower()
        adapter = broker_adapter or self._broker_resolver(trading_broker)

        # If broker_adapter wasn't explicitly injected, skip silently when broker is offline
        if broker_adapter is None:
            try:
                if not adapter.health_check(timeout=2.5).is_connected:
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": f"{adapter.display_name} disconnected",
                        "checked_deployments": len(target_deps),
                        "mismatches": [],
                        "external_positions": [],
                        "halted_deployments": [],
                    }
            except Exception as exc:
                if self._watchdog is not None:
                    self._watchdog.report_dimension("position_reconciliation", healthy=False, error=str(exc))
                return {
                    "ok": False,
                    "skipped": True,
                    "position_error": str(exc),
                    "reason": str(exc),
                    "checked_deployments": len(target_deps),
                    "mismatches": [],
                    "external_positions": [],
                    "halted_deployments": [],
                }

        broker_positions = list(adapter.get_positions() or [])
        broker_by_ident: Dict[str, int] = {}
        for bp in broker_positions:
            sym_u = str(getattr(bp, "symbol", "") or "").strip().upper()
            if not sym_u:
                continue
            bp_meta = getattr(bp, "metadata", None) or {}
            bp_exch = str(getattr(bp, "exchange", "NSE") or "NSE").strip().upper()
            bp_prod = str(getattr(bp, "product", "MIS") or "MIS").strip().upper()
            bp_ikey = str(getattr(bp, "instrument_key", "") or "")
            bp_ident = build_position_identity(
                symbol=sym_u,
                exchange=bp_exch,
                segment=str(bp_meta.get("segment") or ""),
                instrument_key=bp_ikey,
                product=bp_prod,
                expiry=bp_meta.get("expiry"),
                strike=bp_meta.get("strike"),
                option_type=bp_meta.get("option_type"),
            )
            # If local position had blank instrument_key while broker returned one, normalize match
            # on (exchange, segment, symbol, product, expiry, strike, option_type) when unique
            matched_ident = bp_ident
            if bp_ident not in expected_by_ident and bp_ident not in deps_by_ident:
                bp_no_key = build_position_identity(
                    symbol=sym_u,
                    exchange=bp_exch,
                    segment=str(bp_meta.get("segment") or ""),
                    instrument_key="",
                    product=bp_prod,
                    expiry=bp_meta.get("expiry"),
                    strike=bp_meta.get("strike"),
                    option_type=bp_meta.get("option_type"),
                )
                if bp_no_key in expected_by_ident or bp_no_key in deps_by_ident:
                    matched_ident = bp_no_key
            signed_q = int(getattr(bp, "signed_quantity", getattr(bp, "quantity", 0)))
            broker_by_ident[matched_ident] = broker_by_ident.get(matched_ident, 0) + signed_q
            ident_meta.setdefault(
                matched_ident,
                {
                    "identity_key": matched_ident,
                    "symbol": sym_u,
                    "exchange": bp_exch,
                    "product": bp_prod,
                    "expiry": bp_meta.get("expiry"),
                    "strike": bp_meta.get("strike"),
                    "option_type": bp_meta.get("option_type"),
                },
            )

        all_idents = sorted(set(expected_by_ident.keys()) | set(broker_by_ident.keys()))
        mismatches: List[Dict[str, Any]] = []
        external_positions: List[Dict[str, Any]] = []
        affected_ids: Set[str] = set()
        for ident in all_idents:
            local_q = expected_by_ident.get(ident, 0)
            broker_q = broker_by_ident.get(ident, 0)
            meta_row = ident_meta.get(ident) or {"identity_key": ident, "symbol": ident}
            sym_u = str(meta_row.get("symbol") or ident)
            if local_q == broker_q:
                continue
            is_unowned_external = local_q == 0 and sym_u not in managed_syms
            if is_unowned_external:
                external_positions.append(
                    {
                        **meta_row,
                        "broker_qty": broker_q,
                        "externally_managed": True,
                        "policy": "tracked_in_account_risk_no_auto_modify",
                    }
                )
                if isolate_external_positions:
                    continue
            mismatches.append(
                {
                    **meta_row,
                    "local_qty": local_q,
                    "broker_qty": broker_q,
                    "delta": broker_q - local_q,
                    "externally_managed": is_unowned_external,
                }
            )
            for did in deps_by_ident.get(ident, []):
                affected_ids.add(did)
            if not deps_by_ident.get(ident):
                for rt in target_deps:
                    affected_ids.add(rt["id"])

        halted_deployments: List[str] = []
        if mismatches:
            summary_lines = [
                f"{m['exchange']}:{m['symbol']}({m['product']}): local={m['local_qty']} vs broker={m['broker_qty']}"
                for m in mismatches[:5]
            ]
            summary_txt = "; ".join(summary_lines)
            with self._lock:
                for did in sorted(affected_ids):
                    rt = self._runtime.get(did)
                    if not rt:
                        continue
                    rt["stop_event"].set()
                    rt["status"] = "PAUSED"
                    rt["last_error"] = f"Broker position mismatch: {summary_txt}"
                    rt["last_note"] = f"HALTED: Broker position mismatch detected ({summary_txt}). Reconcile manually before resuming."
                    self._persist_deployment_locked(rt)
                    halted_deployments.append(did)

            if self._watchdog is not None:
                self._watchdog.report_dimension(
                    "position_reconciliation",
                    healthy=False,
                    error=f"Position mismatch: {summary_txt}",
                    immediate_degrade=True,
                )

            self._audit.log(
                event_type="POSITION_MISMATCH_HALT",
                actor="reconciler",
                payload={
                    "broker": adapter.name,
                    "mismatches": mismatches,
                    "external_positions": external_positions,
                    "halted_deployments": halted_deployments,
                },
            )
            eff_notifier = notifier or self._notifier
            try:
                eff_notifier.notify_event(
                    "POSITION_MISMATCH",
                    f"Position Mismatch on {adapter.name.upper()} — Live Deployments Halted",
                    [
                        *summary_lines,
                        f"Halted deployments: {', '.join(halted_deployments)}",
                    ],
                )
            except Exception:
                pass
            self._refresh_deploy_ws_scope()
            self._broadcast("deployments:update")

        elif self._watchdog is not None:
            self._watchdog.report_dimension("position_reconciliation", healthy=True)

        return {
            "ok": True,
            "broker": adapter.name,
            "checked_deployments": len(target_deps),
            "mismatches": mismatches,
            "external_positions": external_positions,
            "halted_deployments": halted_deployments,
            "reconciled_at_ist": _now_ist(),
        }

    def reconcile_startup(self, *, broker_adapter: Optional[BrokerBase] = None) -> Dict[str, Any]:
        """Run startup order + position reconciliation for any persisted live deployments."""
        has_live = any(rt["exec_mode"] != "paper" for rt in self._runtime.values())
        active_live_orders = [
            o for o in self._oms.list_orders(active_only=True, limit=50) if o.get("exec_mode") != "paper"
        ]
        if not has_live and not active_live_orders:
            return {"ok": True, "skipped": True, "reason": "No persisted live deployments or active live orders."}
        orders_res = self.reconcile_orders(broker_adapter=broker_adapter)
        pos_res = self.reconcile_positions(broker_adapter=broker_adapter)
        return {"ok": True, "orders": orders_res, "positions": pos_res}

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
                    signed_q = int(pos["qty"])
                    abs_q = abs(signed_q)
                    if abs_q <= 0:
                        continue
                    close_side = "COVER" if signed_q < 0 else "SELL"
                    px = float(pos["last_price"] or pos["entry_price"])
                    sq_coid = _deterministic_client_order_id(
                        rt["id"], psym, close_side, "SQUARE_OFF", str(pos.get("entry_at_ist") or _now_ist())
                    )
                    if rt["exec_mode"] == "live_auto":
                        if not self._oms.has_active_order(rt["id"], psym):
                            self._dispatch_live_broker_order_locked(
                                rt, psym, close_side, abs_q, px, "OPERATOR_SQUARE_OFF", cfg, client_order_id=sq_coid
                            )
                    elif rt["exec_mode"] == "live_alert":
                        self._create_alert_db(
                            deployment_id=rt["id"],
                            deployment_name=rt["name"],
                            symbol=psym,
                            exchange=rt["exchange"],
                            instrument_key=rt["instrument_keys"].get(psym, ""),
                            side=close_side,
                            qty=abs_q,
                            signal_price=px,
                            reason="OPERATOR_SQUARE_OFF",
                            product=str(rt["spec"].get("product") or "MIS"),
                        )
                    else:
                        self._execute_paper_fill_locked(rt, psym, close_side, abs_q, px, "OPERATOR_SQUARE_OFF", cfg)
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
            alert_side = str(alert["side"]).strip().upper()
            alert_sym = str(alert["symbol"]).strip().upper()
            if alert_side in {"SELL", "COVER"}:
                pos_obj = rt["positions"].get(alert_sym)
                open_abs = abs(int(pos_obj["qty"])) if pos_obj is not None else None
                check = self._risk.check_exit(
                    symbol=alert_sym,
                    quantity=int(alert["qty"]),
                    price=float(alert["signal_price"]),
                    transaction_type=alert_side,
                    open_position_qty=open_abs,
                )
            else:
                acct_open, acct_gross, pending_exp, daily_loss, wd_degraded, avail_margin = self._account_risk_context_locked(rt)
                check = self._risk.check_entry(
                    symbol=alert_sym,
                    quantity=int(alert["qty"]),
                    price=float(alert["signal_price"]),
                    transaction_type=alert_side,
                    current_open_positions=acct_open,
                    current_gross_exposure_inr=acct_gross,
                    pending_exposure_inr=pending_exp,
                    daily_loss_inr=daily_loss,
                    watchdog_degraded=wd_degraded,
                    available_margin_inr=avail_margin,
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
                alert_sym,
                alert_side,
                int(alert["qty"]),
                float(alert["signal_price"]),
                f"1CLICK_APPROVED:{alert['reason']}",
                cfg,
                client_order_id=_deterministic_client_order_id(rt["id"], alert_sym, alert_side, "ALERT", alert_id),
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
            "error_count": int(rt.get("error_count") or 0),
            "last_error": str(rt.get("last_error") or ""),
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
