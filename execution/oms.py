"""
execution/oms.py
================
SQLite-persisted Order Management State Machine & Fill Reconciliation Engine.

Production Execution Guarantees:
1. Never treats broker order acceptance (`SUBMITTED`/`OPEN`/`PENDING`) or
   `REJECTED` responses as fills.
2. Enforces strict idempotency via unique `client_order_id` and active-order
   guards (`has_active_order`) so retries or repeated ticks never duplicate orders.
3. Strict state-transition machine: terminal states (`FILLED`, `REJECTED`,
   `CANCELLED`, `EXPIRED`) can NEVER be reopened by stale broker snapshots;
   `filled_quantity` is strictly monotonic.
4. Accurate per-fill pricing: matches `broker.get_trades()` fills by
   `broker_order_id` (falling back to marginal average-price math when trade
   book records aren't yet present).
5. Ambiguous failure recovery: catches `AmbiguousSubmissionError`, persists
   order as `SUBMITTED` (uncertain), recovers `broker_order_id` via
   `broker.find_order_by_tag()`, and flags/alerts stale unmatched orders.
6. Thread-local SQLite connections with WAL + `busy_timeout` to avoid per-call
   connection leaks under concurrent reconciliation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from broker.base import (
    AccountBalance,
    AmbiguousSubmissionError,
    BrokerBase,
    BrokerCapabilities,
    BrokerHealthSnapshot,
    BrokerHttpError,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    ExecutionFill,
    InstrumentContract,
    MarketDepth,
    OrderRejectedError,
    OrderRequest,
    OrderStatus,
    TransientBrokerError,
    make_broker_tag,
)
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from notifications.telegram import TelegramNotifier, telegram_notifier

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES: Tuple[str, ...] = (
    OrderStatus.CREATED.value,
    OrderStatus.VALIDATED.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.OPEN.value,
    OrderStatus.PENDING.value,
    OrderStatus.PARTIALLY_FILLED.value,
)

_TERMINAL_STATUSES: Set[OrderStatus] = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}

# Strict allowed transitions: terminal states have NO outgoing transitions.
_ALLOWED_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.CREATED: {
        OrderStatus.VALIDATED,
        OrderStatus.SUBMITTED,
        OrderStatus.OPEN,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.VALIDATED: {
        OrderStatus.SUBMITTED,
        OrderStatus.OPEN,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.SUBMITTED,
        OrderStatus.OPEN,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.OPEN: {
        OrderStatus.OPEN,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PENDING: {
        OrderStatus.PENDING,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


def _now_ist() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


def _parse_iso_ist(ts: str) -> Optional[datetime]:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        return dt.astimezone(IST_TZ)
    except Exception:
        return None


def _parse_meta(rec: Dict[str, Any]) -> Dict[str, Any]:
    raw = rec.get("metadata_json")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


class PaperExecutionAdapter(BrokerBase):
    """
    Simulated execution adapter sharing the canonical BrokerBase & OMS contract.
    Supports configurable simulation profiles (§6.3):
    - `instant`: immediate full fill at reference/limit price (deterministic baseline)
    - `realistic_spread`: applies half-spread slippage (`spread_bps`) and optional partial fills
    - `depth_aware`: walks live L5/L30 `MarketDepth` order book when available
    """

    name = "paper"
    display_name = "Paper Simulator"
    stage = "active"
    capabilities = BrokerCapabilities(
        supports_trading=True,
        supports_market_data=False,
        supports_websocket=False,
        requires_oauth=False,
        supports_historical=False,
        supports_instrument_master=False,
        max_depth_levels=30,
        execution_grade_feed=False,
    )

    def __init__(
        self,
        initial_balance: float = 500000.0,
        *,
        profile: str = "instant",
        spread_bps: float = 5.0,
        partial_fill_ratio: float = 1.0,
        depth_provider: Optional[Callable[[str, str], Optional[MarketDepth]]] = None,
    ) -> None:
        self._connected = True
        self._balance = AccountBalance(cash=float(initial_balance))
        self._orders: List[BrokerOrder] = []
        self._trades: List[ExecutionFill] = []
        self._positions: Dict[str, BrokerPosition] = {}
        self.profile = str(profile or "instant").strip().lower()
        self.spread_bps = max(0.0, float(spread_bps or 0.0))
        self.partial_fill_ratio = max(0.05, min(1.0, float(partial_fill_ratio or 1.0)))
        self._depth_provider = depth_provider

    def connect(self) -> bool:
        self._connected = True
        return True

    def health_check(self, timeout: float = 1.0) -> BrokerHealthSnapshot:
        return BrokerHealthSnapshot(
            broker_id=self.name,
            display_name=self.display_name,
            stage=self.stage,
            state=ConnectionState.CONNECTED,
            capabilities=self.capabilities,
            message=f"Paper simulator ready (profile={self.profile}).",
        )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        exch = order.require_exchange()
        ref_px = float(order.price or (order.metadata or {}).get("reference_price") or 0.0)
        if ref_px <= 0:
            return BrokerOrder(
                order_id=order.client_order_id,
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="Paper simulator requires a positive reference or limit price.",
            )

        req_qty = int(order.quantity)
        fill_qty = req_qty
        fill_px = ref_px
        slip_bps = 0.0

        if self.profile == "depth_aware" and callable(self._depth_provider):
            try:
                depth = self._depth_provider(order.symbol, exch)
                if isinstance(depth, MarketDepth):
                    walked = depth.walk_book(order.side, req_qty)
                    if int(walked.get("filled_qty") or 0) > 0 and float(walked.get("vwap") or 0.0) > 0:
                        fill_qty = int(walked["filled_qty"])
                        fill_px = float(walked["vwap"])
                        slip_bps = float(walked.get("slippage_bps") or 0.0)
            except Exception:
                pass
        elif self.profile == "realistic_spread":
            half_spread = self.spread_bps / 20000.0
            if order.side in {"BUY", "COVER"}:
                fill_px = round(ref_px * (1.0 + half_spread), 4)
            else:
                fill_px = round(max(0.01, ref_px * (1.0 - half_spread)), 4)
            slip_bps = self.spread_bps / 2.0
            if self.partial_fill_ratio < 0.999 and req_qty > 1:
                fill_qty = max(1, int(req_qty * self.partial_fill_ratio))

        # Respect limit price if specified
        if order.order_type == "LIMIT" and order.price is not None and float(order.price) > 0:
            lim = float(order.price)
            if (order.side in {"BUY", "COVER"} and fill_px > lim) or (
                order.side in {"SELL", "SHORT"} and fill_px < lim
            ):
                oid = f"PAPER-{uuid.uuid4().hex[:10]}"
                bo = BrokerOrder(
                    order_id=oid,
                    request=order,
                    status=OrderStatus.OPEN,
                    filled_quantity=0,
                    average_fill_price=None,
                    raw={"simulated": True, "profile": self.profile, "exchange": exch, "tag": order.tag},
                )
                self._orders.append(bo)
                return bo

        oid = f"PAPER-{uuid.uuid4().hex[:10]}"
        status = OrderStatus.FILLED if fill_qty >= req_qty else OrderStatus.PARTIALLY_FILLED
        bo = BrokerOrder(
            order_id=oid,
            request=order,
            status=status,
            filled_quantity=fill_qty,
            average_fill_price=fill_px,
            raw={
                "simulated": True,
                "profile": self.profile,
                "slippage_bps": round(slip_bps, 2),
                "exchange": exch,
                "tag": order.tag,
            },
        )
        self._orders.append(bo)
        self._trades.append(
            ExecutionFill(
                fill_id=f"PF-{uuid.uuid4().hex[:10]}",
                order_id=oid,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                exchange=exch,
                side=order.side,
                quantity=fill_qty,
                price=fill_px,
                product=order.product,
            )
        )
        return bo

    def cancel_order(self, order_id: str) -> bool:
        for o in self._orders:
            if o.order_id == order_id and o.status.is_active:
                o.status = OrderStatus.CANCELLED
                return True
        return False

    def get_positions(self) -> Sequence[BrokerPosition]:
        return list(self._positions.values())

    def get_balance(self) -> AccountBalance:
        return self._balance

    def get_orders(self) -> Sequence[BrokerOrder]:
        return list(self._orders)

    def get_trades(self) -> Sequence[ExecutionFill]:
        return list(self._trades)


class OrderManagementSystem:
    """Thread-safe SQLite OMS managing canonical order lifecycle & verified fill deltas."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        audit: Optional[AuditTrail] = None,
        notifier: Optional[TelegramNotifier] = None,
        stale_unmatched_sec: float = 120.0,
        manual_intervention_sec: float = 300.0,
    ) -> None:
        self._explicit_db_path = db_path
        self._audit = audit or audit_trail
        self._notifier = notifier or telegram_notifier
        self._stale_unmatched_sec = max(1.0, float(stale_unmatched_sec))
        self._manual_intervention_sec = max(self._stale_unmatched_sec, float(manual_intervention_sec))
        self._lock = threading.RLock()
        self._local = threading.local()
        self._all_conns: List[sqlite3.Connection] = []
        self._initialized: Set[Path] = set()

    @property
    def db_path(self) -> Path:
        return self._explicit_db_path or config.OMS_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path.resolve()
        conn_map: Dict[Path, sqlite3.Connection] = getattr(self._local, "conns", None)
        if conn_map is None:
            conn_map = {}
            self._local.conns = conn_map

        conn = conn_map.get(path)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                conn_map.pop(path, None)

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn_map[path] = conn
        with self._lock:
            self._all_conns.append(conn)
            if path not in self._initialized:
                self._init_schema(conn)
                self._initialized.add(path)
        return conn

    def close_connections(self) -> None:
        """Close thread-local SQLite connections cleanly (useful in tests & shutdown)."""
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        conn_map = getattr(self._local, "conns", None)
        if isinstance(conn_map, dict):
            conn_map.clear()

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS oms_orders (
                client_order_id TEXT PRIMARY KEY,
                broker_order_id TEXT DEFAULT '',
                deployment_id TEXT DEFAULT '',
                broker_id TEXT NOT NULL,
                exec_mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                side TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                product TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                filled_quantity INTEGER NOT NULL DEFAULT 0,
                pending_quantity INTEGER NOT NULL,
                average_fill_price REAL DEFAULT 0.0,
                limit_price REAL DEFAULT 0.0,
                status TEXT NOT NULL,
                reason TEXT DEFAULT '',
                rejection_reason TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                created_at_ist TEXT NOT NULL,
                updated_at_ist TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_oms_orders_dep ON oms_orders (deployment_id, status);
            CREATE INDEX IF NOT EXISTS idx_oms_orders_broker_oid ON oms_orders (broker_order_id);

            CREATE TABLE IF NOT EXISTS oms_fills (
                fill_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL,
                broker_order_id TEXT DEFAULT '',
                deployment_id TEXT DEFAULT '',
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                recorded_at_ist TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_oms_fills_order ON oms_fills (client_order_id);

            CREATE TABLE IF NOT EXISTS oms_order_events (
                event_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL,
                broker_order_id TEXT DEFAULT '',
                from_status TEXT DEFAULT '',
                to_status TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT DEFAULT '',
                recorded_at_ist TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_oms_events_order ON oms_order_events (client_order_id, recorded_at_ist);
            """
        )
        conn.commit()

    def _record_order_event(
        self,
        *,
        client_order_id: str,
        broker_order_id: str = "",
        from_status: str = "",
        to_status: str = "",
        event_type: str = "STATE_TRANSITION",
        detail: str = "",
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO oms_order_events (
                        event_id, client_order_id, broker_order_id, from_status,
                        to_status, event_type, detail, recorded_at_ist
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"oe-{uuid.uuid4().hex[:14]}",
                        client_order_id,
                        broker_order_id or "",
                        from_status or "",
                        to_status or "",
                        event_type,
                        str(detail or "")[:1000],
                        _now_ist(),
                    ),
                )
        except Exception as exc:
            logger.debug("OMS order event write skipped: %s", exc)

    def list_order_events(
        self,
        client_order_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            if client_order_id:
                rows = conn.execute(
                    "SELECT * FROM oms_order_events WHERE client_order_id = ? ORDER BY recorded_at_ist ASC LIMIT ?",
                    (client_order_id, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM oms_order_events ORDER BY recorded_at_ist DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [dict(r) for r in rows]

    def resolve_manual_order(
        self,
        client_order_id: str,
        resolution_status: str,
        *,
        actor: str = "operator",
        note: str = "",
        broker_order_id: str = "",
        filled_quantity: Optional[int] = None,
        average_fill_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Explicit operator resolution for orders escalated to manual intervention."""
        target = OrderStatus(str(resolution_status or "").strip().upper())
        if target not in _TERMINAL_STATUSES:
            raise ValueError(f"Manual resolution requires a terminal status, got {target.value}")
        with self._lock:
            rec = self.get_order_by_client_id(client_order_id)
            if rec is None:
                raise ValueError(f"OMS order '{client_order_id}' not found.")
            prev_status = str(rec["status"])
            meta = _parse_meta(rec)
            meta["needs_manual_intervention"] = False
            meta["uncertain"] = False
            meta["manual_resolution"] = {
                "actor": actor,
                "note": note,
                "resolved_at_ist": _now_ist(),
                "to_status": target.value,
            }
            if broker_order_id:
                rec["broker_order_id"] = broker_order_id
            if filled_quantity is not None:
                rec["filled_quantity"] = max(int(rec.get("filled_quantity") or 0), min(int(rec["quantity"]), int(filled_quantity)))
            if average_fill_price is not None and float(average_fill_price) > 0:
                rec["average_fill_price"] = round(float(average_fill_price), 4)
            rec["pending_quantity"] = 0
            rec["status"] = target.value
            rec["rejection_reason"] = note or rec.get("rejection_reason") or ""
            rec["metadata_json"] = json.dumps(meta)
            rec["updated_at_ist"] = _now_ist()
            self._update_order_db(rec)
            self._record_order_event(
                client_order_id=client_order_id,
                broker_order_id=rec["broker_order_id"],
                from_status=prev_status,
                to_status=target.value,
                event_type="MANUAL_RESOLUTION",
                detail=f"Resolved by {actor}: {note}",
            )
            self._audit.log(
                event_type="OMS_MANUAL_ORDER_RESOLVED",
                actor=actor,
                payload={
                    "client_order_id": client_order_id,
                    "from_status": prev_status,
                    "to_status": target.value,
                    "note": note,
                },
            )
            return rec

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM oms_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_active_order(
        self,
        deployment_id: str,
        symbol: str,
        side: Optional[str] = None,
    ) -> bool:
        """Return True if any non-terminal OMS order is working for (deployment_id, symbol[, side])."""
        sym_u = (symbol or "").strip().upper()
        dep = (deployment_id or "").strip()
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        sql = f"SELECT 1 FROM oms_orders WHERE deployment_id = ? AND symbol = ? AND status IN ({placeholders})"
        params: List[Any] = [dep, sym_u, *_ACTIVE_STATUSES]
        if side:
            sql += " AND side = ?"
            params.append(side.strip().upper())
        sql += " LIMIT 1"
        with self._lock:
            conn = self._connect()
            row = conn.execute(sql, params).fetchone()
        return row is not None

    def get_pending_exposure_inr(
        self,
        *,
        broker_id: Optional[str] = None,
        exclude_paper: bool = True,
    ) -> float:
        """Sum notional value of all pending non-terminal BUY/SHORT OMS orders for account-wide risk gates."""
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        clauses = [f"status IN ({placeholders})", "side IN ('BUY', 'SHORT')"]
        params: List[Any] = list(_ACTIVE_STATUSES)
        if exclude_paper:
            clauses.append("exec_mode != 'paper'")
        if broker_id:
            clauses.append("broker_id = ?")
            params.append(broker_id.strip().lower())
        sql = f"SELECT pending_quantity, limit_price FROM oms_orders WHERE {' AND '.join(clauses)}"
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        total = 0.0
        for r in rows:
            qty = max(0, int(r["pending_quantity"] or 0))
            px = max(0.0, float(r["limit_price"] or 0.0))
            total += qty * px
        return round(total, 2)

    def list_orders(
        self,
        *,
        deployment_id: Optional[str] = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if deployment_id:
            clauses.append("deployment_id = ?")
            params.append(deployment_id)
        if active_only:
            placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(_ACTIVE_STATUSES)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"SELECT * FROM oms_orders {where_sql} ORDER BY created_at_ist DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def list_fills(self, deployment_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            if deployment_id:
                rows = conn.execute(
                    "SELECT * FROM oms_fills WHERE deployment_id = ? ORDER BY recorded_at_ist DESC LIMIT ?",
                    (deployment_id, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM oms_fills ORDER BY recorded_at_ist DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [dict(r) for r in rows]

    def _recorded_fill_ids(self, client_order_id: str) -> Set[str]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT fill_id FROM oms_fills WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchall()
        return {str(r["fill_id"]) for r in rows}

    @staticmethod
    def _classify_submission_error(exc: Exception) -> str:
        """
        Separate definite broker rejection, ambiguous network/5xx submission,
        and local programming/validation errors (Finding 8 / §6.2).
        """
        if isinstance(exc, (OrderRejectedError, ValueError)):
            return "definite_rejection"
        if isinstance(exc, BrokerHttpError):
            if exc.status_code == 429 or exc.status_code >= 500:
                return "ambiguous"
            if 400 <= exc.status_code < 500:
                return "definite_rejection"
        if isinstance(exc, AmbiguousSubmissionError):
            return "ambiguous"
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return "ambiguous"
        if isinstance(exc, TransientBrokerError):
            return "ambiguous"
        # Check optional requests exceptions without requiring requests installed
        cls_name = exc.__class__.__name__.lower()
        mod_name = (exc.__class__.__module__ or "").lower()
        if "requests" in mod_name or "urllib" in mod_name or "http" in mod_name or "socket" in mod_name:
            if "timeout" in cls_name or "connection" in cls_name or "urlerror" in cls_name:
                return "ambiguous"
        return "local_error"

    def submit_order(
        self,
        req: OrderRequest,
        broker: BrokerBase,
        *,
        deployment_id: str = "",
        exec_mode: str = "live_auto",
        reason: str = "",
        reference_price: float = 0.0,
        contract: Optional[InstrumentContract] = None,
    ) -> Tuple[Dict[str, Any], int, float]:
        """
        Persist order intent idempotently (with atomic SQLite cross-process guard),
        independently validate contract lot-size/tick-size/expiry (C-04),
        and submit to broker with clean error classification (Finding 8).

        Returns `(order_record, verified_fill_delta_qty, verified_fill_price)`.
        """
        with self._lock:
            now = _now_ist()
            exchange = req.require_exchange()
            broker_tag = make_broker_tag(req.client_order_id, max_len=20)
            if broker_tag and not req.tag:
                req.tag = broker_tag
            meta = dict(req.metadata or {})
            meta["broker_tag"] = broker_tag
            meta["uncertain"] = False
            meta["needs_manual_intervention"] = False

            # Independent OMS contract validation (C-04 / Finding 1)
            contract_err: Optional[str] = None
            if contract is not None:
                meta["lot_size"] = contract.effective_lot_size
                meta["tick_size"] = contract.effective_tick_size
                if contract.expiry:
                    meta["expiry"] = contract.expiry
                    today_ist = datetime.now(tz=IST_TZ).strftime("%Y-%m-%d")
                    if str(contract.expiry).strip() < today_ist:
                        contract_err = f"Contract {contract.exchange}:{contract.symbol} expired on {contract.expiry}."
                if contract_err is None:
                    contract_err = contract.validate_quantity(int(req.quantity))
                if contract_err is None and req.order_type in {"LIMIT", "STOP_LIMIT", "SL"} and req.price is not None:
                    contract_err = contract.validate_price(req.price, allow_none=False)
            elif meta.get("lot_size"):
                lot_sz = max(1, int(meta.get("lot_size") or 1))
                if lot_sz > 1 and (int(req.quantity) % lot_sz) != 0:
                    contract_err = (
                        f"Order quantity {req.quantity} for {exchange}:{req.symbol} "
                        f"is not an exact multiple of lot_size={lot_sz}."
                    )

            order_rec: Dict[str, Any] = {
                "client_order_id": req.client_order_id,
                "broker_order_id": "",
                "deployment_id": deployment_id,
                "broker_id": broker.name,
                "exec_mode": exec_mode,
                "symbol": req.symbol,
                "exchange": exchange,
                "side": req.side,
                "transaction_type": req.transaction_type,
                "product": req.product,
                "order_type": req.order_type,
                "quantity": int(req.quantity),
                "filled_quantity": 0,
                "pending_quantity": 0 if contract_err else int(req.quantity),
                "average_fill_price": 0.0,
                "limit_price": float(req.price or reference_price or 0.0),
                "status": OrderStatus.REJECTED.value if contract_err else OrderStatus.VALIDATED.value,
                "reason": reason,
                "rejection_reason": contract_err or "",
                "metadata_json": json.dumps(meta),
                "created_at_ist": now,
                "updated_at_ist": now,
            }

            # Atomic cross-process idempotency reservation using INSERT OR IGNORE
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO oms_orders (
                        client_order_id, broker_order_id, deployment_id, broker_id, exec_mode,
                        symbol, exchange, side, transaction_type, product, order_type,
                        quantity, filled_quantity, pending_quantity, average_fill_price,
                        limit_price, status, reason, rejection_reason, metadata_json,
                        created_at_ist, updated_at_ist
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_rec["client_order_id"],
                        order_rec["broker_order_id"],
                        order_rec["deployment_id"],
                        order_rec["broker_id"],
                        order_rec["exec_mode"],
                        order_rec["symbol"],
                        order_rec["exchange"],
                        order_rec["side"],
                        order_rec["transaction_type"],
                        order_rec["product"],
                        order_rec["order_type"],
                        order_rec["quantity"],
                        order_rec["filled_quantity"],
                        order_rec["pending_quantity"],
                        order_rec["average_fill_price"],
                        order_rec["limit_price"],
                        order_rec["status"],
                        order_rec["reason"],
                        order_rec["rejection_reason"],
                        order_rec["metadata_json"],
                        order_rec["created_at_ist"],
                        order_rec["updated_at_ist"],
                    ),
                )
                inserted = cur.rowcount == 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            if not inserted:
                existing = self.get_order_by_client_id(req.client_order_id) or order_rec
                logger.info(
                    "Idempotency guard: client_order_id=%s already exists (status=%s); skipping duplicate broker submission.",
                    req.client_order_id,
                    existing["status"],
                )
                return existing, 0, float(existing.get("average_fill_price") or 0.0)

            self._record_order_event(
                client_order_id=req.client_order_id,
                from_status=OrderStatus.CREATED.value,
                to_status=order_rec["status"],
                event_type="CONTRACT_REJECTED" if contract_err else "INTENT_PERSISTED",
                detail=contract_err or f"Persisted {req.side} {req.quantity} {exchange}:{req.symbol}",
            )

            if contract_err:
                self._audit.log(
                    event_type="OMS_CONTRACT_VALIDATION_REJECTED",
                    actor="oms",
                    payload={
                        "client_order_id": req.client_order_id,
                        "deployment_id": deployment_id,
                        "symbol": req.symbol,
                        "exchange": exchange,
                        "quantity": int(req.quantity),
                        "reason": contract_err,
                    },
                )
                return order_rec, 0, 0.0

            # Dispatch to broker adapter with clean error classification
            try:
                broker_order = broker.place_order(req)
            except Exception as exc:
                err_msg = str(exc)
                err_class = self._classify_submission_error(exc)
                if err_class in {"definite_rejection", "local_error"}:
                    meta["uncertain"] = False
                    meta["failure_class"] = err_class
                    order_rec["metadata_json"] = json.dumps(meta)
                    order_rec["status"] = OrderStatus.REJECTED.value
                    order_rec["pending_quantity"] = 0
                    order_rec["rejection_reason"] = (
                        f"Local dispatch error (not sent): {err_msg}"
                        if err_class == "local_error"
                        else f"Broker rejected order: {err_msg}"
                    )
                    order_rec["updated_at_ist"] = _now_ist()
                    self._update_order_db(order_rec)
                    self._record_order_event(
                        client_order_id=req.client_order_id,
                        from_status=OrderStatus.VALIDATED.value,
                        to_status=OrderStatus.REJECTED.value,
                        event_type="LOCAL_DISPATCH_ERROR" if err_class == "local_error" else "DEFINITE_REJECTION",
                        detail=err_msg,
                    )
                    self._audit.log(
                        event_type="OMS_ORDER_REJECTED",
                        actor="oms",
                        payload={
                            "client_order_id": req.client_order_id,
                            "deployment_id": deployment_id,
                            "broker": broker.name,
                            "symbol": req.symbol,
                            "exchange": exchange,
                            "side": req.side,
                            "failure_class": err_class,
                            "error": err_msg,
                        },
                    )
                    return order_rec, 0, 0.0

                # Network timeout / 5xx / 429 / AmbiguousSubmissionError:
                # keep as SUBMITTED (uncertain) so retrying the same client_order_id won't double-submit blindly!
                meta["uncertain"] = True
                meta["failure_class"] = "ambiguous"
                meta["uncertain_error"] = err_msg
                order_rec["metadata_json"] = json.dumps(meta)
                order_rec["status"] = OrderStatus.SUBMITTED.value
                order_rec["rejection_reason"] = f"Submission uncertain (reconcile required): {err_msg}"
                order_rec["updated_at_ist"] = _now_ist()
                self._update_order_db(order_rec)
                self._record_order_event(
                    client_order_id=req.client_order_id,
                    from_status=OrderStatus.VALIDATED.value,
                    to_status=OrderStatus.SUBMITTED.value,
                    event_type="AMBIGUOUS_SUBMISSION",
                    detail=err_msg,
                )
                self._audit.log(
                    event_type="OMS_ORDER_SUBMISSION_UNCERTAIN",
                    actor="oms",
                    payload={
                        "client_order_id": req.client_order_id,
                        "broker_tag": broker_tag,
                        "deployment_id": deployment_id,
                        "broker": broker.name,
                        "symbol": req.symbol,
                        "exchange": exchange,
                        "side": req.side,
                        "error": err_msg,
                    },
                )
                if exec_mode != "paper":
                    try:
                        self._notifier.notify_event(
                            "ORDER_UNCERTAIN",
                            f"{req.side} {req.quantity} {exchange}:{req.symbol} SUBMISSION UNCERTAIN",
                            [
                                f"Broker: {broker.name.upper()} | Tag: {broker_tag}",
                                f"Error: {err_msg}",
                                "Pending automatic tag-based broker reconciliation.",
                            ],
                        )
                    except Exception:
                        pass
                return order_rec, 0, 0.0

            if broker_order is None:
                order_rec["status"] = OrderStatus.REJECTED.value
                order_rec["pending_quantity"] = 0
                order_rec["rejection_reason"] = "Broker returned empty order response"
                order_rec["updated_at_ist"] = _now_ist()
                self._update_order_db(order_rec)
                self._record_order_event(
                    client_order_id=req.client_order_id,
                    from_status=OrderStatus.VALIDATED.value,
                    to_status=OrderStatus.REJECTED.value,
                    event_type="EMPTY_BROKER_RESPONSE",
                    detail="Broker returned None",
                )
                return order_rec, 0, 0.0

            delta_qty, fill_px = self._apply_broker_snapshot_locked(order_rec, broker_order)
            return order_rec, delta_qty, fill_px

    def _update_order_db(self, rec: Dict[str, Any]) -> None:
        conn = self._connect()
        with conn:
            conn.execute(
                """
                UPDATE oms_orders SET
                    broker_order_id = ?,
                    filled_quantity = ?,
                    pending_quantity = ?,
                    average_fill_price = ?,
                    status = ?,
                    rejection_reason = ?,
                    metadata_json = ?,
                    updated_at_ist = ?
                WHERE client_order_id = ?
                """,
                (
                    rec["broker_order_id"],
                    int(rec["filled_quantity"]),
                    int(rec["pending_quantity"]),
                    float(rec["average_fill_price"] or 0.0),
                    rec["status"],
                    rec["rejection_reason"],
                    rec.get("metadata_json", "{}"),
                    rec["updated_at_ist"],
                    rec["client_order_id"],
                ),
            )

    def _record_fill_db(
        self,
        rec: Dict[str, Any],
        delta_qty: int,
        fill_price: float,
        fill_id: Optional[str] = None,
    ) -> None:
        fid = fill_id or f"fill-{uuid.uuid4().hex[:12]}"
        conn = self._connect()
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO oms_fills (
                    fill_id, client_order_id, broker_order_id, deployment_id,
                    symbol, exchange, side, quantity, price, recorded_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fid,
                    rec["client_order_id"],
                    rec["broker_order_id"],
                    rec["deployment_id"],
                    rec["symbol"],
                    rec["exchange"],
                    rec["side"],
                    int(delta_qty),
                    float(fill_price),
                    _now_ist(),
                ),
            )

    def _apply_broker_snapshot_locked(
        self,
        rec: Dict[str, Any],
        bo: BrokerOrder,
        *,
        order_trades: Optional[Sequence[ExecutionFill]] = None,
    ) -> Tuple[int, float]:
        current_status = OrderStatus(str(rec.get("status") or OrderStatus.VALIDATED.value).upper())
        # STRICT STATE TRANSITION INVARIANT: terminal states can NEVER be reopened!
        if current_status in _TERMINAL_STATUSES:
            logger.debug(
                "Ignoring broker snapshot for terminal order %s (current=%s, incoming=%s).",
                rec["client_order_id"],
                current_status.value,
                bo.status,
            )
            return 0, 0.0

        prev_filled = int(rec.get("filled_quantity") or 0)
        prev_avg_px = float(rec.get("average_fill_price") or 0.0)
        total_qty = int(rec["quantity"])
        new_status = bo.status if isinstance(bo.status, OrderStatus) else OrderStatus(str(bo.status).upper())

        if bo.order_id:
            rec["broker_order_id"] = str(bo.order_id)

        # Clear uncertain marker once matched to a real broker snapshot
        meta = _parse_meta(rec)
        if meta.get("uncertain"):
            meta["uncertain"] = False
            meta["recovered_at_ist"] = _now_ist()
            rec["metadata_json"] = json.dumps(meta)
            if str(rec.get("rejection_reason") or "").startswith("Submission uncertain"):
                rec["rejection_reason"] = ""

        # Rejections cannot override an order that already received partial fills
        if new_status == OrderStatus.REJECTED and prev_filled == 0:
            if new_status not in _ALLOWED_TRANSITIONS.get(current_status, set()):
                return 0, 0.0
            rec["status"] = OrderStatus.REJECTED.value
            rec["rejection_reason"] = str(bo.rejection_reason or "Rejected by broker")
            rec["pending_quantity"] = 0
            rec["updated_at_ist"] = _now_ist()
            self._update_order_db(rec)
            self._audit.log(
                event_type="OMS_ORDER_REJECTED",
                actor="oms",
                payload={
                    "client_order_id": rec["client_order_id"],
                    "broker_order_id": rec["broker_order_id"],
                    "deployment_id": rec["deployment_id"],
                    "symbol": rec["symbol"],
                    "exchange": rec["exchange"],
                    "side": rec["side"],
                    "reason": rec["rejection_reason"],
                },
            )
            if rec["exec_mode"] != "paper":
                self._notifier.notify_event(
                    "ORDER_REJECTED",
                    f"{rec['side']} {rec['quantity']} {rec['exchange']}:{rec['symbol']} REJECTED",
                    [f"Broker: {rec['broker_id'].upper()}", f"Reason: {rec['rejection_reason']}"],
                )
            return 0, 0.0

        # Check broker trade book fills matched by broker_order_id for exact per-fill pricing
        new_trade_fills: List[ExecutionFill] = []
        if order_trades:
            recorded_ids = self._recorded_fill_ids(rec["client_order_id"])
            for tr in order_trades:
                if tr.fill_id and tr.fill_id not in recorded_ids and int(tr.quantity) > 0 and float(tr.price) > 0:
                    new_trade_fills.append(tr)

        reported_filled = max(0, min(total_qty, int(bo.filled_quantity or 0)))
        if new_status == OrderStatus.FILLED and reported_filled == 0:
            reported_filled = total_qty
        # Monotonic filled_quantity: never shrink due to stale broker snapshots!
        effective_filled = max(prev_filled, reported_filled)
        if new_trade_fills:
            trades_qty_sum = sum(int(t.quantity) for t in new_trade_fills)
            # If broker returns full order trades (not just unseen ones), effective_filled
            # is bounded by max(effective_filled, trades_qty_sum) when prior fills lacked trade IDs.
            effective_filled = min(total_qty, max(effective_filled, trades_qty_sum))

        delta_qty = max(0, effective_filled - prev_filled)
        reported_avg_px = float(bo.average_fill_price or 0.0)

        # Isolate only the incremental trade fills corresponding to delta_qty
        # (prevents double-counting early partial fills booked before trade IDs appeared)
        incremental_trade_slices: List[Tuple[ExecutionFill, int]] = []
        if delta_qty > 0 and new_trade_fills:
            tr_qty_total = sum(int(t.quantity) for t in new_trade_fills)
            skip_qty = max(0, tr_qty_total - delta_qty)
            remaining_need = delta_qty
            for tr in new_trade_fills:
                q_tr = int(tr.quantity)
                if skip_qty >= q_tr:
                    skip_qty -= q_tr
                    continue
                usable = q_tr - skip_qty
                skip_qty = 0
                take = min(usable, remaining_need)
                if take > 0:
                    incremental_trade_slices.append((tr, take))
                    remaining_need -= take
                if remaining_need <= 0:
                    break

        # Compute exact incremental fill price (prefer individual trade fills; fallback to marginal average math)
        fill_px = 0.0
        if delta_qty > 0 and incremental_trade_slices:
            tr_qty = sum(q for _, q in incremental_trade_slices)
            tr_val = sum(q * float(t.price) for t, q in incremental_trade_slices)
            fill_px = (tr_val / float(tr_qty)) if tr_qty > 0 else reported_avg_px
        elif delta_qty > 0 and reported_avg_px > 0:
            if prev_filled > 0 and prev_avg_px > 0 and effective_filled > prev_filled:
                marginal = ((effective_filled * reported_avg_px) - (prev_filled * prev_avg_px)) / float(delta_qty)
                fill_px = marginal if marginal > 0 else reported_avg_px
            else:
                fill_px = reported_avg_px
        else:
            fill_px = float(reported_avg_px or prev_avg_px or rec.get("limit_price") or 0.0)

        if effective_filled >= total_qty and total_qty > 0:
            final_status = OrderStatus.FILLED
        elif effective_filled > 0 and new_status not in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
            final_status = OrderStatus.PARTIALLY_FILLED
        else:
            final_status = new_status

        # Validate state transition legality
        if final_status != current_status and final_status not in _ALLOWED_TRANSITIONS.get(current_status, set()):
            logger.debug(
                "Blocked illegal OMS state transition %s -> %s for %s",
                current_status.value,
                final_status.value,
                rec["client_order_id"],
            )
            return 0, 0.0

        rec["filled_quantity"] = effective_filled
        rec["pending_quantity"] = 0 if final_status.is_terminal else max(0, total_qty - effective_filled)
        if effective_filled > 0:
            if reported_avg_px > 0:
                rec["average_fill_price"] = round(reported_avg_px, 4)
            elif delta_qty > 0 and fill_px > 0:
                cum_val = (prev_filled * prev_avg_px) + (delta_qty * fill_px)
                rec["average_fill_price"] = round(cum_val / float(effective_filled), 4)
        rec["status"] = final_status.value
        if bo.rejection_reason:
            rec["rejection_reason"] = str(bo.rejection_reason)
        rec["updated_at_ist"] = _now_ist()
        self._update_order_db(rec)
        if final_status != current_status or delta_qty > 0:
            self._record_order_event(
                client_order_id=rec["client_order_id"],
                broker_order_id=rec["broker_order_id"],
                from_status=current_status.value,
                to_status=final_status.value,
                event_type="FILL_DELTA" if delta_qty > 0 else "BROKER_STATUS_UPDATE",
                detail=(
                    f"filled +{delta_qty} ({effective_filled}/{total_qty}) @ {fill_px:.4f}"
                    if delta_qty > 0
                    else (bo.rejection_reason or f"status={final_status.value}")
                ),
            )

        if delta_qty > 0:
            if incremental_trade_slices:
                for tr, take_qty in incremental_trade_slices:
                    self._record_fill_db(rec, int(take_qty), float(tr.price), fill_id=tr.fill_id)
            else:
                self._record_fill_db(rec, delta_qty, fill_px)
            self._audit.log(
                event_type="OMS_ORDER_FILL_DELTA",
                actor="oms",
                payload={
                    "client_order_id": rec["client_order_id"],
                    "broker_order_id": rec["broker_order_id"],
                    "deployment_id": rec["deployment_id"],
                    "symbol": rec["symbol"],
                    "exchange": rec["exchange"],
                    "side": rec["side"],
                    "delta_qty": delta_qty,
                    "filled_total": effective_filled,
                    "order_qty": total_qty,
                    "fill_price": round(fill_px, 4),
                    "status": final_status.value,
                },
            )
            if rec["exec_mode"] != "paper":
                self._notifier.notify_event(
                    "FILL",
                    f"{rec['side']} {delta_qty}/{total_qty} {rec['exchange']}:{rec['symbol']} @ ₹{fill_px:,.2f}",
                    [f"Status: {final_status.value}", f"Order ID: {rec['broker_order_id']}"],
                )
        return delta_qty, fill_px

    def reconcile_orders(
        self,
        broker: BrokerBase,
        *,
        deployment_id: Optional[str] = None,
        on_fill: Optional[Callable[[Dict[str, Any], int, float], None]] = None,
    ) -> Dict[str, Any]:
        """
        Reconcile all non-terminal OMS orders for `broker` against the authoritative
        live `broker.get_orders()` & `broker.get_trades()` snapshots.
        Also recovers uncertain submissions via `broker.find_order_by_tag()` and
        alerts on stale unmatched SUBMITTED orders.
        """
        with self._lock:
            active_orders = self.list_orders(deployment_id=deployment_id, active_only=True, limit=250)
            matching_active = [
                rec
                for rec in active_orders
                if rec["broker_id"] == broker.name or broker.name == "paper"
            ]
            if not matching_active:
                return {
                    "ok": True,
                    "checked": 0,
                    "reconciled": 0,
                    "recovered": 0,
                    "fills_applied": [],
                    "stale_unmatched": [],
                    "manual_intervention_required": [],
                    "skipped_idle": True,
                }
            broker_orders = list(broker.get_orders() or [])
            try:
                broker_trades = list(broker.get_trades() or [])
            except Exception:
                broker_trades = []

            trades_by_oid: Dict[str, List[ExecutionFill]] = {}
            for tr in broker_trades:
                if tr.order_id:
                    trades_by_oid.setdefault(str(tr.order_id), []).append(tr)

            by_oid: Dict[str, BrokerOrder] = {str(o.order_id): o for o in broker_orders if o.order_id}
            by_client: Dict[str, BrokerOrder] = {}
            for o in broker_orders:
                cid = str(getattr(o.request, "client_order_id", "") or "").strip()
                tag = str(getattr(o.request, "tag", "") or "").strip()
                raw_tag = str((getattr(o, "raw", None) or {}).get("tag") or "").strip()
                if cid:
                    by_client[cid] = o
                if tag:
                    by_client[tag] = o
                if raw_tag:
                    by_client[raw_tag] = o

            now_dt = datetime.now(tz=IST_TZ)
            reconciled_count = 0
            recovered_count = 0
            fills_applied: List[Dict[str, Any]] = []
            stale_unmatched: List[Dict[str, Any]] = []
            manual_intervention_required: List[Dict[str, Any]] = []

            for rec in active_orders:
                if rec["broker_id"] != broker.name and broker.name != "paper":
                    continue
                meta = _parse_meta(rec)
                broker_tag = str(meta.get("broker_tag") or make_broker_tag(rec["client_order_id"], max_len=20))
                was_uncertain = bool(meta.get("uncertain")) or not str(rec.get("broker_order_id") or "").strip()

                matched = (
                    by_oid.get(str(rec.get("broker_order_id") or ""))
                    or by_client.get(rec["client_order_id"])
                    or by_client.get(broker_tag)
                )
                if matched is None and was_uncertain and broker_tag:
                    try:
                        matched = broker.find_order_by_tag(broker_tag)
                    except Exception:
                        matched = None

                if matched is None:
                    # Detect stale unmatched SUBMITTED orders & escalate past manual_intervention_sec
                    created_dt = _parse_iso_ist(str(rec.get("created_at_ist") or ""))
                    age_sec = (now_dt - created_dt).total_seconds() if created_dt is not None else 0.0
                    if age_sec >= self._stale_unmatched_sec:
                        needs_manual = age_sec >= self._manual_intervention_sec
                        stale_info = {
                            "client_order_id": rec["client_order_id"],
                            "broker_tag": broker_tag,
                            "symbol": rec["symbol"],
                            "side": rec["side"],
                            "status": rec["status"],
                            "age_sec": round(age_sec, 1),
                            "needs_manual_intervention": needs_manual,
                        }
                        stale_unmatched.append(stale_info)
                        if needs_manual:
                            manual_intervention_required.append(stale_info)
                        meta_dirty = False
                        if not meta.get("stale_alerted"):
                            meta["stale_alerted"] = True
                            meta_dirty = True
                            self._audit.log(
                                event_type="OMS_ORDER_STALE_UNMATCHED",
                                actor="oms",
                                payload=stale_info,
                            )
                            self._record_order_event(
                                client_order_id=rec["client_order_id"],
                                from_status=rec["status"],
                                to_status=rec["status"],
                                event_type="STALE_UNMATCHED",
                                detail=f"Unmatched for {int(age_sec)}s (tag={broker_tag})",
                            )
                        if needs_manual and not meta.get("needs_manual_intervention"):
                            meta["needs_manual_intervention"] = True
                            meta["escalated_at_ist"] = _now_ist()
                            meta_dirty = True
                            self._audit.log(
                                event_type="OMS_MANUAL_INTERVENTION_REQUIRED",
                                actor="oms",
                                payload=stale_info,
                            )
                            self._record_order_event(
                                client_order_id=rec["client_order_id"],
                                from_status=rec["status"],
                                to_status=rec["status"],
                                event_type="MANUAL_INTERVENTION_REQUIRED",
                                detail=f"Exceeded reconciliation deadline ({int(self._manual_intervention_sec)}s); operator resolution required.",
                            )
                            if rec["exec_mode"] != "paper":
                                try:
                                    self._notifier.notify_event(
                                        "MANUAL_INTERVENTION",
                                        f"ACTION REQUIRED: Unresolved {rec['side']} {rec['symbol']} order ({int(age_sec)}s)",
                                        [
                                            f"Client ID: {rec['client_order_id']} | Tag: {broker_tag}",
                                            "Exceeded OMS reconciliation deadline; verify broker terminal & resolve manually.",
                                        ],
                                    )
                                except Exception:
                                    pass
                        if meta_dirty:
                            rec["metadata_json"] = json.dumps(meta)
                            rec["updated_at_ist"] = _now_ist()
                            self._update_order_db(rec)
                    continue

                reconciled_count += 1
                if was_uncertain and matched.order_id:
                    recovered_count += 1
                oid_key = str(matched.order_id or rec.get("broker_order_id") or "")
                delta_qty, fill_px = self._apply_broker_snapshot_locked(
                    rec,
                    matched,
                    order_trades=trades_by_oid.get(oid_key),
                )
                if delta_qty > 0:
                    fills_applied.append(
                        {
                            "client_order_id": rec["client_order_id"],
                            "broker_order_id": rec["broker_order_id"],
                            "deployment_id": rec["deployment_id"],
                            "symbol": rec["symbol"],
                            "side": rec["side"],
                            "delta_qty": delta_qty,
                            "fill_price": fill_px,
                            "status": rec["status"],
                        }
                    )
                    if on_fill is not None:
                        on_fill(rec, delta_qty, fill_px)

            return {
                "ok": len(manual_intervention_required) == 0,
                "order_recovery_error": (
                    f"{len(manual_intervention_required)} order(s) require manual intervention."
                    if manual_intervention_required
                    else ""
                ),
                "broker": broker.name,
                "active_checked": len(active_orders),
                "reconciled_count": reconciled_count,
                "recovered_count": recovered_count,
                "fills_applied": fills_applied,
                "stale_unmatched": stale_unmatched,
                "manual_intervention_required": manual_intervention_required,
                "reconciled_at_ist": _now_ist(),
            }


oms_manager = OrderManagementSystem()

__all__ = ["OrderManagementSystem", "PaperExecutionAdapter", "oms_manager"]
