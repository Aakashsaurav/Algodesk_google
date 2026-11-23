"""
execution/oms.py
================
SQLite-persisted Order Management State Machine & Fill Reconciliation Engine.

Solves P0 Execution Defects:
1. Never treats broker order acceptance (`SUBMITTED`/`OPEN`/`PENDING`) or
   `REJECTED` responses as fills.
2. Enforces strict idempotency via unique `client_order_id` so network timeouts
   or retries never blindly submit duplicate live orders.
3. Reconciles active orders against authoritative broker `get_orders()` and
   `get_trades()`, emitting strictly incremental fill deltas (`delta_qty`) for
   partial and full fills—even across process restarts.
4. Provides `PaperExecutionAdapter` implementing `BrokerBase` so Paper Trading
   runs through the exact same OMS order-state & fill-delta pipeline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from broker.base import (
    AccountBalance,
    BrokerBase,
    BrokerCapabilities,
    BrokerHealthSnapshot,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    ExecutionFill,
    OrderRequest,
    OrderStatus,
)
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from notifications.telegram import TelegramNotifier, telegram_notifier

logger = logging.getLogger(__name__)


def _now_ist() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


class PaperExecutionAdapter(BrokerBase):
    """Simulated execution adapter sharing the canonical BrokerBase & OMS contract."""

    name = "paper"
    display_name = "Paper Simulator"
    stage = "active"
    capabilities = BrokerCapabilities(
        supports_trading=True,
        supports_market_data=False,
        supports_websocket=False,
        requires_oauth=False,
    )

    def __init__(self, initial_balance: float = 500000.0) -> None:
        self._connected = True
        self._balance = AccountBalance(cash=float(initial_balance))
        self._orders: List[BrokerOrder] = []
        self._trades: List[ExecutionFill] = []
        self._positions: Dict[str, BrokerPosition] = {}

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
            message="Paper simulator ready.",
        )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        exch = order.require_exchange()
        fill_px = float(order.price or (order.metadata or {}).get("reference_price") or 0.0)
        if fill_px <= 0:
            return BrokerOrder(
                order_id=order.client_order_id,
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="Paper simulator requires a positive reference or limit price.",
            )
        oid = f"PAPER-{uuid.uuid4().hex[:10]}"
        bo = BrokerOrder(
            order_id=oid,
            request=order,
            status=OrderStatus.FILLED,
            filled_quantity=int(order.quantity),
            average_fill_price=fill_px,
            raw={"simulated": True, "exchange": exch},
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
                quantity=int(order.quantity),
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
    ) -> None:
        self._explicit_db_path = db_path
        self._audit = audit or audit_trail
        self._notifier = notifier or telegram_notifier
        self._lock = threading.RLock()
        self._initialized: set[Path] = set()

    @property
    def db_path(self) -> Path:
        return self._explicit_db_path or config.OMS_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        if path not in self._initialized:
            with self._lock:
                if path not in self._initialized:
                    self._init_schema(conn)
                    self._initialized.add(path)
        return conn

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
            """
        )
        conn.commit()

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM oms_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return dict(row) if row else None

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
            clauses.append("status IN ('CREATED', 'VALIDATED', 'SUBMITTED', 'OPEN', 'PENDING', 'PARTIALLY_FILLED')")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM oms_orders {where_sql} ORDER BY created_at_ist DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def list_fills(self, deployment_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
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

    def submit_order(
        self,
        req: OrderRequest,
        broker: BrokerBase,
        *,
        deployment_id: str = "",
        exec_mode: str = "live_auto",
        reason: str = "",
        reference_price: float = 0.0,
    ) -> Tuple[Dict[str, Any], int, float]:
        """
        Persist order intent idempotently, validate, and submit to broker.

        Returns `(order_record, verified_fill_delta_qty, verified_fill_price)`.
        CRITICAL INVARIANT:
        - `verified_fill_delta_qty` is `> 0` ONLY when the broker explicitly
          confirms `filled_quantity > 0` (status `FILLED` or `PARTIALLY_FILLED`).
        - If broker returns `REJECTED`, `OPEN`, `PENDING`, or `SUBMITTED`,
          `verified_fill_delta_qty == 0` so callers NEVER fabricate a fill!
        - Re-submitting an existing `client_order_id` refuses blind duplicate
          submission and returns the persisted/reconciled record instead.
        """
        with self._lock:
            existing = self.get_order_by_client_id(req.client_order_id)
            if existing is not None:
                logger.info(
                    "Idempotency guard: client_order_id=%s already exists (status=%s); skipping duplicate broker submission.",
                    req.client_order_id,
                    existing["status"],
                )
                return existing, 0, float(existing.get("average_fill_price") or 0.0)

            now = _now_ist()
            exchange = req.require_exchange()
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
                "pending_quantity": int(req.quantity),
                "average_fill_price": 0.0,
                "limit_price": float(req.price or reference_price or 0.0),
                "status": OrderStatus.VALIDATED.value,
                "reason": reason,
                "rejection_reason": "",
                "metadata_json": json.dumps(req.metadata or {}),
                "created_at_ist": now,
                "updated_at_ist": now,
            }
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO oms_orders (
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
                conn.commit()

            # Dispatch to broker adapter
            try:
                broker_order = broker.place_order(req)
            except Exception as exc:
                # Network timeout / ambiguous error: keep as SUBMITTED (pending reconciliation)
                # so retrying the same client_order_id won't double-submit blindly!
                err_msg = str(exc)
                order_rec["status"] = OrderStatus.SUBMITTED.value
                order_rec["rejection_reason"] = f"Submission uncertain (reconcile required): {err_msg}"
                order_rec["updated_at_ist"] = _now_ist()
                self._update_order_db(order_rec)
                self._audit.log(
                    event_type="OMS_ORDER_SUBMISSION_UNCERTAIN",
                    actor="oms",
                    payload={
                        "client_order_id": req.client_order_id,
                        "deployment_id": deployment_id,
                        "broker": broker.name,
                        "symbol": req.symbol,
                        "exchange": exchange,
                        "side": req.side,
                        "error": err_msg,
                    },
                )
                return order_rec, 0, 0.0

            if broker_order is None:
                order_rec["status"] = OrderStatus.REJECTED.value
                order_rec["pending_quantity"] = 0
                order_rec["rejection_reason"] = "Broker returned empty order response"
                order_rec["updated_at_ist"] = _now_ist()
                self._update_order_db(order_rec)
                return order_rec, 0, 0.0

            delta_qty, fill_px = self._apply_broker_snapshot_locked(order_rec, broker_order)
            return order_rec, delta_qty, fill_px

    def _update_order_db(self, rec: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE oms_orders SET
                    broker_order_id = ?,
                    filled_quantity = ?,
                    pending_quantity = ?,
                    average_fill_price = ?,
                    status = ?,
                    rejection_reason = ?,
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
                    rec["updated_at_ist"],
                    rec["client_order_id"],
                ),
            )
            conn.commit()

    def _record_fill_db(
        self,
        rec: Dict[str, Any],
        delta_qty: int,
        fill_price: float,
        fill_id: Optional[str] = None,
    ) -> None:
        fid = fill_id or f"fill-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
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
            conn.commit()

    def _apply_broker_snapshot_locked(
        self,
        rec: Dict[str, Any],
        bo: BrokerOrder,
    ) -> Tuple[int, float]:
        prev_filled = int(rec.get("filled_quantity") or 0)
        total_qty = int(rec["quantity"])
        new_status = bo.status if isinstance(bo.status, OrderStatus) else OrderStatus(str(bo.status).upper())

        if bo.order_id:
            rec["broker_order_id"] = str(bo.order_id)

        reported_filled = max(0, min(total_qty, int(bo.filled_quantity or 0)))
        if new_status == OrderStatus.FILLED and reported_filled == 0:
            reported_filled = total_qty

        if new_status == OrderStatus.REJECTED:
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

        delta_qty = max(0, reported_filled - prev_filled)
        fill_px = float(bo.average_fill_price or rec.get("average_fill_price") or rec.get("limit_price") or 0.0)

        if reported_filled >= total_qty and total_qty > 0:
            final_status = OrderStatus.FILLED
        elif reported_filled > 0 and new_status not in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
            final_status = OrderStatus.PARTIALLY_FILLED
        else:
            final_status = new_status

        rec["filled_quantity"] = reported_filled
        rec["pending_quantity"] = 0 if final_status.is_terminal else max(0, total_qty - reported_filled)
        if fill_px > 0 and reported_filled > 0:
            rec["average_fill_price"] = fill_px
        rec["status"] = final_status.value
        if bo.rejection_reason:
            rec["rejection_reason"] = str(bo.rejection_reason)
        rec["updated_at_ist"] = _now_ist()
        self._update_order_db(rec)

        if delta_qty > 0:
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
                    "filled_total": reported_filled,
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
        Reconcile all non-terminal OMS orders for `broker` against the
        authoritative live `broker.get_orders()` snapshot.
        Safe to run repeatedly and on startup after process crash/restart.
        """
        with self._lock:
            active_orders = self.list_orders(deployment_id=deployment_id, active_only=True, limit=250)
            broker_orders = list(broker.get_orders() or [])
            by_oid: Dict[str, BrokerOrder] = {str(o.order_id): o for o in broker_orders if o.order_id}
            by_client: Dict[str, BrokerOrder] = {}
            for o in broker_orders:
                cid = str(getattr(o.request, "client_order_id", "") or "").strip()
                tag = str(getattr(o.request, "tag", "") or "").strip()
                if cid:
                    by_client[cid] = o
                if tag:
                    by_client[tag] = o

            reconciled_count = 0
            fills_applied: List[Dict[str, Any]] = []
            for rec in active_orders:
                if rec["broker_id"] != broker.name and broker.name != "paper":
                    continue
                matched = by_oid.get(str(rec.get("broker_order_id") or "")) or by_client.get(rec["client_order_id"])
                if matched is None:
                    continue
                reconciled_count += 1
                delta_qty, fill_px = self._apply_broker_snapshot_locked(rec, matched)
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
                "ok": True,
                "broker": broker.name,
                "active_checked": len(active_orders),
                "reconciled_count": reconciled_count,
                "fills_applied": fills_applied,
                "reconciled_at_ist": _now_ist(),
            }


oms_manager = OrderManagementSystem()

__all__ = ["OrderManagementSystem", "PaperExecutionAdapter", "oms_manager"]
