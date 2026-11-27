"""
risk/limits.py
==============
Portfolio Risk Limit persistence, pre-trade order validation (`validate_order`),
and live broker exposure + daily loss evaluation with SEBI SHA-256 audit & kill switch.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from broker.registry import BrokerRegistry
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from core.preflight import KillSwitchController, _atomic_write_json, kill_switch_controller

logger = logging.getLogger(__name__)


class RiskLimitManager:
    """Persists portfolio risk limits & evaluates live broker exposure + pre-trade checks."""

    def __init__(
        self,
        limits_file: Optional[Path] = None,
        audit: Optional[AuditTrail] = None,
        kill_switch: Optional[KillSwitchController] = None,
    ) -> None:
        self._explicit_path = limits_file
        self._audit = audit or audit_trail
        self._kill_switch = kill_switch or kill_switch_controller
        self._lock = threading.RLock()

    @property
    def limits_file(self) -> Path:
        return self._explicit_path or config.RISK_LIMITS_FILE

    def _defaults(self) -> Dict[str, Any]:
        cap = float(config.TOTAL_CAPITAL or 500000.0)
        return {
            "total_capital_inr": round(cap, 2),
            "max_capital_per_trade_pct": 15.0,
            "max_daily_loss_inr": round(max(5000.0, cap * 0.03), 2),
            "max_open_positions": 5,
            "max_gross_exposure_inr": round(cap * 0.80, 2),
            "min_margin_buffer_pct": 10.0,
            "auto_engage_kill_switch": True,
            "updated_at_ist": None,
        }

    def load_limits(self) -> Dict[str, Any]:
        with self._lock:
            defaults = self._defaults()
            path = self.limits_file
            if not path.exists():
                return defaults
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return defaults
                merged = dict(defaults)
                for k in (
                    "total_capital_inr",
                    "max_capital_per_trade_pct",
                    "max_daily_loss_inr",
                    "max_open_positions",
                    "max_gross_exposure_inr",
                    "min_margin_buffer_pct",
                    "auto_engage_kill_switch",
                    "updated_at_ist",
                ):
                    if k in raw and raw[k] is not None:
                        merged[k] = raw[k]
                return merged
            except (OSError, json.JSONDecodeError):
                return defaults

    def save_limits(self, updates: Dict[str, Any], actor: str = "operator") -> Dict[str, Any]:
        with self._lock:
            cur = self.load_limits()
            next_cfg = dict(cur)
            if "total_capital_inr" in updates and updates["total_capital_inr"] is not None:
                val = float(updates["total_capital_inr"])
                if val <= 0:
                    raise ValueError("total_capital_inr must be > 0.")
                next_cfg["total_capital_inr"] = round(val, 2)
            if "max_capital_per_trade_pct" in updates and updates["max_capital_per_trade_pct"] is not None:
                val = float(updates["max_capital_per_trade_pct"])
                if not (0.1 <= val <= 100.0):
                    raise ValueError("max_capital_per_trade_pct must be between 0.1 and 100.")
                next_cfg["max_capital_per_trade_pct"] = round(val, 2)
            if "max_daily_loss_inr" in updates and updates["max_daily_loss_inr"] is not None:
                val = float(updates["max_daily_loss_inr"])
                if val <= 0:
                    raise ValueError("max_daily_loss_inr must be > 0.")
                next_cfg["max_daily_loss_inr"] = round(val, 2)
            if "max_open_positions" in updates and updates["max_open_positions"] is not None:
                val_i = int(updates["max_open_positions"])
                if val_i < 1:
                    raise ValueError("max_open_positions must be >= 1.")
                next_cfg["max_open_positions"] = val_i
            if "max_gross_exposure_inr" in updates and updates["max_gross_exposure_inr"] is not None:
                val = float(updates["max_gross_exposure_inr"])
                if val <= 0:
                    raise ValueError("max_gross_exposure_inr must be > 0.")
                next_cfg["max_gross_exposure_inr"] = round(val, 2)
            if "min_margin_buffer_pct" in updates and updates["min_margin_buffer_pct"] is not None:
                val = float(updates["min_margin_buffer_pct"])
                if not (0.0 <= val <= 95.0):
                    raise ValueError("min_margin_buffer_pct must be between 0 and 95.")
                next_cfg["min_margin_buffer_pct"] = round(val, 2)
            if "auto_engage_kill_switch" in updates and updates["auto_engage_kill_switch"] is not None:
                next_cfg["auto_engage_kill_switch"] = bool(updates["auto_engage_kill_switch"])

            next_cfg["updated_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            _atomic_write_json(self.limits_file, next_cfg)

        self._audit.log(
            event_type="RISK_LIMITS_UPDATED",
            actor=actor,
            payload={
                "max_capital_per_trade_pct": next_cfg["max_capital_per_trade_pct"],
                "max_daily_loss_inr": next_cfg["max_daily_loss_inr"],
                "max_open_positions": next_cfg["max_open_positions"],
                "max_gross_exposure_inr": next_cfg["max_gross_exposure_inr"],
                "min_margin_buffer_pct": next_cfg["min_margin_buffer_pct"],
                "auto_engage_kill_switch": next_cfg["auto_engage_kill_switch"],
            },
        )
        return next_cfg

    def check_entry(
        self,
        *,
        symbol: str,
        quantity: int,
        price: Optional[float] = None,
        transaction_type: str = "BUY",
        current_open_positions: int = 0,
        current_gross_exposure_inr: float = 0.0,
        pending_exposure_inr: float = 0.0,
        daily_loss_inr: Optional[float] = None,
        watchdog_degraded: bool = False,
        available_margin_inr: Optional[float] = None,
        exchange: str = "",
        instrument_key: str = "",
        lot_size: Optional[int] = None,
        tick_size: Optional[float] = None,
        contract: Optional[Any] = None,
        enforce_tick_size: bool = False,
    ) -> Dict[str, Any]:
        """
        Fail-closed pre-trade risk gate for risk-increasing ENTRY orders (BUY / SHORT).
        Enforces kill switch, watchdog health, valid finite positive reference price
        (never bypassing exposure checks on missing/zero/NaN/inf prices), instrument
        identity, contract lot-size & tick-size validation, daily loss cap, per-trade
        notional cap, max open positions, account-wide gross + pending exposure cap,
        and real-time available broker margin reserve sufficiency.
        """
        import math

        limits = self.load_limits()
        reasons: List[str] = []

        if self._kill_switch.is_engaged():
            reasons.append("Emergency Kill Switch is engaged.")

        if watchdog_degraded:
            reasons.append("Risk watchdog is degraded after repeated probe failures; new entries paused.")

        sym_clean = str(symbol or "").strip().upper()
        if not sym_clean:
            reasons.append("Instrument symbol is required for risk-increasing entry orders.")

        qty = max(0, int(quantity or 0))
        if qty <= 0:
            reasons.append("Order quantity must be > 0.")

        # Contract & lot-size validation (C-04 / Finding 1)
        eff_lot = int(lot_size or 0)
        eff_tick = tick_size
        if contract is not None:
            if eff_lot <= 0:
                eff_lot = int(getattr(contract, "effective_lot_size", getattr(contract, "lot_size", 1)) or 1)
            if eff_tick is None:
                eff_tick = getattr(contract, "effective_tick_size", getattr(contract, "tick_size", None))
            if hasattr(contract, "validate_quantity"):
                q_err = contract.validate_quantity(qty)
                if q_err and qty > 0:
                    reasons.append(q_err)
        elif eff_lot > 1 and qty > 0 and (qty % eff_lot) != 0:
            reasons.append(
                f"Order quantity {qty} for {sym_clean or 'UNKNOWN'} is not a valid multiple of lot_size={eff_lot}."
            )

        # Fail-closed price validation (C-01 / Finding 2): never silently treat missing/0/NaN/inf as 0 exposure
        px_valid = False
        px = 0.0
        if price is None:
            reasons.append(f"Reference price is missing for {sym_clean or 'UNKNOWN'}; failing closed.")
        else:
            try:
                raw_px = float(price)
            except (TypeError, ValueError):
                reasons.append(f"Invalid non-numeric price {price!r} for {sym_clean or 'UNKNOWN'}; failing closed.")
            else:
                if not math.isfinite(raw_px) or raw_px <= 0.0:
                    reasons.append(
                        f"Reference price must be finite and > 0 for {sym_clean or 'UNKNOWN'}, got {price!r}; failing closed."
                    )
                else:
                    px = raw_px
                    px_valid = True
                    if enforce_tick_size and eff_tick is not None and float(eff_tick) > 0:
                        ts = float(eff_tick)
                        nearest = round(px / ts) * ts
                        if abs(px - nearest) > max(1e-6, ts * 1e-3):
                            reasons.append(
                                f"Order price {px} for {sym_clean} is not aligned to tick_size={ts} (nearest: {nearest:.4f})."
                            )

        notional = (qty * px) if px_valid else 0.0
        cap = float(limits["total_capital_inr"])
        max_trade_notional = cap * (float(limits["max_capital_per_trade_pct"]) / 100.0)

        if daily_loss_inr is not None and float(daily_loss_inr) >= float(limits["max_daily_loss_inr"]):
            reasons.append(
                f"Daily loss ₹{float(daily_loss_inr):,.2f} reached/exceeded cap ₹{limits['max_daily_loss_inr']:,.2f}."
            )

        if px_valid and notional > max_trade_notional:
            reasons.append(
                f"Order notional ₹{notional:,.2f} exceeds per-trade cap ₹{max_trade_notional:,.2f} ({limits['max_capital_per_trade_pct']}%)."
            )

        if current_open_positions >= int(limits["max_open_positions"]):
            reasons.append(
                f"Open positions ({current_open_positions}) reached limit ({limits['max_open_positions']})."
            )

        projected_exposure = float(current_gross_exposure_inr) + max(0.0, float(pending_exposure_inr)) + notional
        if px_valid and projected_exposure > float(limits["max_gross_exposure_inr"]):
            reasons.append(
                f"Projected gross exposure ₹{projected_exposure:,.2f} exceeds cap ₹{limits['max_gross_exposure_inr']:,.2f}."
            )

        if available_margin_inr is not None and px_valid:
            avail = max(0.0, float(available_margin_inr))
            buf_pct = max(0.0, min(95.0, float(limits.get("min_margin_buffer_pct", 10.0))))
            usable_margin = avail * (1.0 - buf_pct / 100.0)
            required_margin = notional + max(0.0, float(pending_exposure_inr))
            if required_margin > usable_margin:
                reasons.append(
                    f"Insufficient broker margin: required ₹{required_margin:,.2f} exceeds usable margin ₹{usable_margin:,.2f} "
                    f"(available ₹{avail:,.2f} with {buf_pct:.1f}% reserve buffer)."
                )

        allowed = len(reasons) == 0
        if not allowed:
            try:
                self._audit.log(
                    event_type="PRETRADE_RISK_BLOCKED",
                    actor="risk_guard",
                    payload={
                        "symbol": sym_clean,
                        "exchange": str(exchange or "").strip().upper(),
                        "instrument_key": str(instrument_key or "").strip(),
                        "side": str(transaction_type).upper(),
                        "quantity": qty,
                        "price": px if px_valid else price,
                        "notional": round(notional, 2),
                        "reasons": reasons,
                    },
                )
            except Exception:
                pass
        return {"allowed": allowed, "reasons": reasons, "limits": limits, "kind": "entry"}

    def check_exit(
        self,
        *,
        symbol: str,
        quantity: int,
        price: float = 0.0,
        transaction_type: str = "SELL",
        open_position_qty: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Pre-trade risk gate for risk-REDUCING EXIT orders (SELL / COVER).

        CRITICAL SAFETY INVARIANT:
        - Exits are ALWAYS permitted even when the Kill Switch is engaged,
          daily loss limit is breached, watchdog is degraded, or the appreciated
          position notional exceeds `max_capital_per_trade_pct`.
        - Never allows an exit order to flip a position when `open_position_qty`
          is supplied (`quantity` must not exceed `open_position_qty`).
        """
        limits = self.load_limits()
        reasons: List[str] = []
        qty = max(0, int(quantity or 0))
        if qty <= 0:
            reasons.append("Order quantity must be > 0.")
        if open_position_qty is not None:
            open_abs = abs(int(open_position_qty))
            if open_abs == 0:
                reasons.append(f"No open position in {symbol} to exit.")
            elif qty > open_abs:
                reasons.append(
                    f"Exit quantity ({qty}) exceeds open position ({open_abs}) for {symbol}; refusing accidental flip."
                )

        allowed = len(reasons) == 0
        if not allowed:
            try:
                self._audit.log(
                    event_type="PRETRADE_EXIT_BLOCKED",
                    actor="risk_guard",
                    payload={
                        "symbol": symbol,
                        "side": str(transaction_type).upper(),
                        "quantity": qty,
                        "open_position_qty": open_position_qty,
                        "reasons": reasons,
                    },
                )
            except Exception:
                pass
        return {"allowed": allowed, "reasons": reasons, "limits": limits, "kind": "exit"}

    def validate_order(
        self,
        *,
        symbol: str,
        quantity: int,
        price: float = 0.0,
        transaction_type: str = "BUY",
        current_open_positions: int = 0,
        current_gross_exposure_inr: float = 0.0,
        pending_exposure_inr: float = 0.0,
        daily_loss_inr: Optional[float] = None,
        watchdog_degraded: bool = False,
        open_position_qty: Optional[int] = None,
        available_margin_inr: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Pre-trade risk gate routing exits to `check_exit` and entries to `check_entry`."""
        side_u = str(transaction_type or "BUY").strip().upper()
        if side_u in {"SELL", "COVER"}:
            return self.check_exit(
                symbol=symbol,
                quantity=quantity,
                price=price,
                transaction_type=side_u,
                open_position_qty=open_position_qty,
            )
        return self.check_entry(
            symbol=symbol,
            quantity=quantity,
            price=price,
            transaction_type=side_u,
            current_open_positions=current_open_positions,
            current_gross_exposure_inr=current_gross_exposure_inr,
            pending_exposure_inr=pending_exposure_inr,
            daily_loss_inr=daily_loss_inr,
            watchdog_degraded=watchdog_degraded,
            available_margin_inr=available_margin_inr,
        )

    def evaluate(
        self,
        *,
        trading_broker: str = "upstox",
        funds: Optional[Dict[str, float]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        broker_adapter: Optional[Any] = None,
        notifier: Optional[Any] = None,
    ) -> Dict[str, Any]:
        from notifications.telegram import telegram_notifier

        limits = self.load_limits()
        broker_id = (trading_broker or "upstox").strip().lower()
        broker_connected = False
        broker_note = ""

        live_funds: Dict[str, float] = {}
        live_positions: List[Dict[str, Any]] = []
        live_orders: List[Dict[str, Any]] = []

        if funds is not None or positions is not None or orders is not None:
            broker_connected = True
            live_funds = dict(funds or {})
            live_positions = list(positions or [])
            live_orders = list(orders or [])
            broker_note = f"Evaluated against {broker_id.upper()} live state."
        else:
            try:
                adapter = broker_adapter or BrokerRegistry.get(broker_id)
                snap = adapter.health_check(timeout=2.5)
                broker_connected = bool(snap.is_connected)
                if broker_connected and adapter.capabilities.supports_trading:
                    live_funds = dict(adapter.get_funds() or {})
                    raw_pos = adapter.get_positions() or []
                    for p in raw_pos:
                        live_positions.append(
                            {
                                "symbol": getattr(p, "symbol", ""),
                                "quantity": int(getattr(p, "signed_quantity", getattr(p, "quantity", 0))),
                                "average_price": float(getattr(p, "average_price", 0.0)),
                                "last_price": float(getattr(p, "last_price", getattr(p, "average_price", 0.0))),
                                "realized_pnl": float(getattr(p, "realized_pnl", 0.0)),
                                "unrealized_pnl": float(getattr(p, "unrealized_pnl", 0.0)),
                            }
                        )
                    raw_ord = adapter.get_orders() or []
                    for o in raw_ord:
                        req_obj = getattr(o, "request", None)
                        live_orders.append(
                            {
                                "order_id": getattr(o, "order_id", ""),
                                "symbol": getattr(o, "symbol", getattr(req_obj, "symbol", "")),
                                "status": str(getattr(o, "status", "")),
                                "remaining_quantity": int(getattr(o, "remaining_quantity", 0)),
                                "price": float(getattr(req_obj, "price", 0.0) or 0.0) if req_obj is not None else 0.0,
                            }
                        )
                    broker_note = f"Connected to {adapter.display_name} ({snap.account_name or snap.client_id})."
                else:
                    broker_note = f"{adapter.display_name} disconnected ({snap.message}). Evaluating configured capital envelope."
            except Exception as exc:
                broker_note = f"Broker probe note ({exc}). Evaluating configured capital envelope."

        avail_margin = float(live_funds.get("available", 0.0) or 0.0)
        used_margin = float(live_funds.get("used", 0.0) or 0.0)
        live_cap = avail_margin + used_margin
        capital_base = live_cap if live_cap > 0 else float(limits["total_capital_inr"])

        open_positions = [p for p in live_positions if int(p.get("quantity", 0)) != 0]
        gross_exposure = 0.0
        max_single_pos_val = 0.0
        daily_pnl = 0.0
        for p in live_positions:
            qty = abs(int(p.get("quantity", 0)))
            px = float(p.get("last_price") or p.get("average_price") or 0.0)
            notional = qty * px
            if qty > 0:
                gross_exposure += notional
                if notional > max_single_pos_val:
                    max_single_pos_val = notional
            daily_pnl += float(p.get("realized_pnl", 0.0)) + float(p.get("unrealized_pnl", 0.0))

        # Include pending open order notional exposure
        pending_order_exposure = 0.0
        active_order_count = 0
        for o in live_orders:
            st = str(o.get("status") or "").upper()
            if any(term in st for term in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED", "COMPLETE")):
                continue
            active_order_count += 1
            rem_qty = abs(int(o.get("remaining_quantity") or o.get("pending_quantity") or 0))
            o_px = float(o.get("price") or o.get("limit_price") or 0.0)
            pending_order_exposure += rem_qty * o_px

        total_exposure = gross_exposure + pending_order_exposure
        max_single_pos_pct = (max_single_pos_val / capital_base) * 100.0 if capital_base > 0 else 0.0
        daily_loss = max(0.0, -daily_pnl)
        exposure_util_pct = (total_exposure / float(limits["max_gross_exposure_inr"])) * 100.0
        daily_loss_util_pct = (daily_loss / float(limits["max_daily_loss_inr"])) * 100.0

        breaches: List[Dict[str, Any]] = []
        if daily_loss >= float(limits["max_daily_loss_inr"]):
            breaches.append(
                {
                    "rule": "max_daily_loss_inr",
                    "severity": "CRITICAL",
                    "message": f"Daily loss ₹{daily_loss:,.2f} reached/exceeded limit ₹{limits['max_daily_loss_inr']:,.2f}.",
                }
            )
        if len(open_positions) > int(limits["max_open_positions"]):
            breaches.append(
                {
                    "rule": "max_open_positions",
                    "severity": "WARNING",
                    "message": f"Open positions ({len(open_positions)}) exceeded limit ({limits['max_open_positions']}).",
                }
            )
        if total_exposure > float(limits["max_gross_exposure_inr"]):
            breaches.append(
                {
                    "rule": "max_gross_exposure_inr",
                    "severity": "CRITICAL",
                    "message": f"Total exposure ₹{total_exposure:,.2f} (positions + pending orders) exceeded cap ₹{limits['max_gross_exposure_inr']:,.2f}.",
                }
            )
        if max_single_pos_pct > float(limits["max_capital_per_trade_pct"]):
            breaches.append(
                {
                    "rule": "max_capital_per_trade_pct",
                    "severity": "WARNING",
                    "message": f"Largest position is {max_single_pos_pct:.1f}% of capital (limit {limits['max_capital_per_trade_pct']:.1f}%).",
                }
            )

        kill_auto_triggered = False
        has_daily_loss_breach = any(b["rule"] == "max_daily_loss_inr" for b in breaches)
        if has_daily_loss_breach and bool(limits.get("auto_engage_kill_switch", True)):
            if not self._kill_switch.is_engaged():
                self._kill_switch.engage(
                    reason=f"Auto-halt: daily loss ₹{daily_loss:,.2f} breached cap ₹{limits['max_daily_loss_inr']:,.2f}",
                    actor="risk_guard",
                    trading_broker=broker_id,
                    broker_adapter=broker_adapter,
                )
                kill_auto_triggered = True
            self._audit.log(
                event_type="RISK_LIMIT_BREACH",
                actor="risk_guard",
                payload={
                    "trading_broker": broker_id,
                    "daily_loss_inr": round(daily_loss, 2),
                    "limit_inr": limits["max_daily_loss_inr"],
                    "auto_kill_switch": True,
                },
            )
            eff_notifier = notifier or telegram_notifier
            try:
                eff_notifier.notify_event(
                    "DAILY_LOSS_BREACH",
                    f"Daily Loss Breach on {broker_id.upper()} — Kill Switch Engaged",
                    [
                        f"Daily Loss: ₹{daily_loss:,.2f} (Limit: ₹{limits['max_daily_loss_inr']:,.2f})",
                        f"Open Positions: {len(open_positions)} | Pending Orders: {active_order_count}",
                    ],
                )
            except Exception:
                pass

        return {
            "ok": True,
            "trading_broker": broker_id,
            "broker_connected": broker_connected,
            "broker_note": broker_note,
            "limits": limits,
            "telemetry": {
                "capital_base_inr": round(capital_base, 2),
                "available_margin_inr": round(avail_margin, 2),
                "used_margin_inr": round(used_margin, 2),
                "open_positions_count": len(open_positions),
                "open_orders_count": active_order_count,
                "pending_order_exposure_inr": round(pending_order_exposure, 2),
                "gross_exposure_inr": round(total_exposure, 2),
                "exposure_utilization_pct": round(min(exposure_util_pct, 999.0), 1),
                "daily_pnl_inr": round(daily_pnl, 2),
                "daily_loss_inr": round(daily_loss, 2),
                "daily_loss_utilization_pct": round(min(daily_loss_util_pct, 999.0), 1),
                "max_single_position_pct": round(max_single_pos_pct, 2),
            },
            "breaches": breaches,
            "status": "BREACHED" if breaches else ("HEALTHY" if broker_connected else "STANDBY"),
            "kill_switch": self._kill_switch.status(),
            "kill_switch_auto_triggered": kill_auto_triggered,
            "evaluated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }


class RiskWatchdog:
    """
    Headless continuous risk & reconciliation background thread with independent
    health dimensions (C-07 / Finding 6).

    Health dimensions tracked independently:
    - broker_connectivity
    - market_data_freshness
    - oms_reconciliation
    - position_reconciliation
    - order_recovery
    - risk_calculations

    A passing risk calculation NEVER clears a failing reconciliation, broker
    disconnect, or stale market-data health state. Any critical failure blocks
    new live entries while keeping risk-reducing exits available.
    """

    HEALTH_DIMENSIONS = (
        "broker_connectivity",
        "market_data_freshness",
        "oms_reconciliation",
        "position_reconciliation",
        "order_recovery",
        "risk_calculations",
    )

    def __init__(
        self,
        risk_mgr: Optional[RiskLimitManager] = None,
        *,
        interval_sec: float = 10.0,
        broker_provider: Optional[Any] = None,
        state_provider: Optional[Any] = None,
        notifier: Optional[Any] = None,
        reconcile_callback: Optional[Callable[[], Any]] = None,
        reconcile_every_n: int = 4,
        degraded_threshold: int = 3,
        is_active_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._risk = risk_mgr or risk_limit_manager
        self._interval = max(1.0, float(interval_sec))
        self._broker_provider = broker_provider
        self._state_provider = state_provider
        self._notifier = notifier
        self._reconcile_callback = reconcile_callback
        self._reconcile_every_n = max(1, int(reconcile_every_n))
        self._degraded_threshold = max(1, int(degraded_threshold))
        self._is_active_callback = is_active_callback
        self._idle_standby: bool = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_eval: Optional[Dict[str, Any]] = None
        self._consecutive_failures: int = 0
        self._degraded: bool = False
        self._degraded_alerted: bool = False
        self._last_error: str = ""
        self._tick_count: int = 0
        self._lock = threading.RLock()
        now_iso = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        self._dimensions: Dict[str, Dict[str, Any]] = {
            dim: {
                "healthy": True,
                "consecutive_failures": 0,
                "last_error": "",
                "updated_at_ist": now_iso,
            }
            for dim in self.HEALTH_DIMENSIONS
        }

    def _recompute_degraded_locked(self) -> bool:
        any_dim_failed = any(not d["healthy"] for d in self._dimensions.values())
        self._degraded = any_dim_failed or (self._consecutive_failures >= self._degraded_threshold)
        if not self._degraded:
            self._degraded_alerted = False
        return self._degraded

    @property
    def is_degraded(self) -> bool:
        with self._lock:
            return self._degraded

    def unhealthy_dimensions(self) -> List[str]:
        with self._lock:
            return [k for k, v in self._dimensions.items() if not v["healthy"]]

    def report_dimension(
        self,
        dimension: str,
        *,
        healthy: bool,
        error: str = "",
        immediate_degrade: bool = False,
    ) -> Dict[str, Any]:
        """Update an independent health dimension without resetting unrelated dimensions."""
        from notifications.telegram import telegram_notifier

        dim = str(dimension or "").strip().lower()
        if dim not in self._dimensions:
            raise ValueError(f"Unknown health dimension: {dimension!r}")
        now_iso = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        should_alert = False
        with self._lock:
            entry = self._dimensions[dim]
            entry["updated_at_ist"] = now_iso
            if healthy:
                entry["healthy"] = True
                entry["consecutive_failures"] = 0
                entry["last_error"] = ""
            else:
                entry["consecutive_failures"] = int(entry["consecutive_failures"]) + 1
                entry["last_error"] = str(error or f"{dim} unhealthy")
                if immediate_degrade or entry["consecutive_failures"] >= self._degraded_threshold:
                    entry["healthy"] = False
            degraded_now = self._recompute_degraded_locked()
            if degraded_now and not self._degraded_alerted:
                self._degraded_alerted = True
                should_alert = True
            if not healthy:
                self._last_error = f"[{dim}] {entry['last_error']}"
            snap = dict(entry)
            unhealthy = [k for k, v in self._dimensions.items() if not v["healthy"]]

        if should_alert:
            eff_notifier = self._notifier or telegram_notifier
            try:
                eff_notifier.notify_event(
                    "WATCHDOG_DEGRADED",
                    "Risk Watchdog DEGRADED — New Entries Paused",
                    [
                        f"Unhealthy dimensions: {', '.join(unhealthy) or dim}",
                        f"Last error: {snap['last_error']}",
                    ],
                )
            except Exception:
                pass
        return snap

    def set_reconcile_callback(self, cb: Optional[Callable[[], Any]]) -> None:
        with self._lock:
            self._reconcile_callback = cb

    def set_active_callback(self, cb: Optional[Callable[[], bool]]) -> None:
        with self._lock:
            self._is_active_callback = cb

    def set_broker_provider(self, provider: Optional[Callable[[], Any]]) -> None:
        with self._lock:
            self._broker_provider = provider

    def _record_failure(self, exc: Exception, *, dimension: str = "risk_calculations") -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            fails = self._consecutive_failures
            err_msg = self._last_error
        logger.error("RiskWatchdog failure #%d [%s]: %s", fails, dimension, err_msg)
        self.report_dimension(dimension, healthy=False, error=err_msg)

    def _record_success(self, res: Dict[str, Any]) -> None:
        with self._lock:
            self._last_eval = res
            self._consecutive_failures = 0
            # Only mark risk_calculations healthy; do NOT clear reconciliation or feed failures!
            self._dimensions["risk_calculations"]["healthy"] = True
            self._dimensions["risk_calculations"]["consecutive_failures"] = 0
            self._dimensions["risk_calculations"]["last_error"] = ""
            self._dimensions["risk_calculations"]["updated_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            if "broker_connected" in res:
                b_ok = bool(res.get("broker_connected"))
                b_dim = self._dimensions["broker_connectivity"]
                b_dim["updated_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
                if b_ok:
                    b_dim["healthy"] = True
                    b_dim["consecutive_failures"] = 0
                    b_dim["last_error"] = ""
                else:
                    # Only mark broker_connectivity unhealthy if live broker was actually expected (not standby with no adapter)
                    if self._broker_provider is not None or self._state_provider is not None:
                        b_dim["consecutive_failures"] = int(b_dim["consecutive_failures"]) + 1
                        b_dim["last_error"] = "Trading broker disconnected"
                        if b_dim["consecutive_failures"] >= self._degraded_threshold:
                            b_dim["healthy"] = False
            self._recompute_degraded_locked()
            if not self._degraded:
                self._last_error = ""

    def run_reconciliation_once(self) -> Optional[Dict[str, Any]]:
        """Execute reconciliation callback and propagate failures into OMS/position health dimensions."""
        cb = self._reconcile_callback
        if cb is None:
            return None
        try:
            res = cb()
            if isinstance(res, dict):
                has_granular = any(
                    k in res for k in ("oms_error", "position_error", "order_recovery_error")
                )
                if not has_granular and (res.get("error") or res.get("ok") is False):
                    err = str(res.get("error") or "Reconciliation returned error")
                    self.report_dimension("oms_reconciliation", healthy=False, error=err)
                    self.report_dimension("position_reconciliation", healthy=False, error=err)
                    return res
                # Inspect granular OMS / position / recovery errors if returned
                oms_err = res.get("oms_error")
                pos_err = res.get("position_error")
                if oms_err:
                    self.report_dimension("oms_reconciliation", healthy=False, error=str(oms_err))
                else:
                    self.report_dimension("oms_reconciliation", healthy=True)
                if pos_err:
                    self.report_dimension("position_reconciliation", healthy=False, error=str(pos_err))
                else:
                    self.report_dimension("position_reconciliation", healthy=True)
                rec_err = res.get("order_recovery_error")
                if rec_err:
                    self.report_dimension("order_recovery", healthy=False, error=str(rec_err))
                else:
                    self.report_dimension("order_recovery", healthy=True)
            else:
                self.report_dimension("oms_reconciliation", healthy=True)
                self.report_dimension("position_reconciliation", healthy=True)
                self.report_dimension("order_recovery", healthy=True)
            return res if isinstance(res, dict) else {"ok": True}
        except Exception as exc:
            logger.warning("RiskWatchdog periodic reconciliation failed: %s", exc)
            self._record_failure(exc, dimension="oms_reconciliation")
            self.report_dimension("position_reconciliation", healthy=False, error=f"{exc.__class__.__name__}: {exc}")
            return {"ok": False, "error": str(exc)}

    def run_once(self) -> Dict[str, Any]:
        try:
            if self._state_provider is not None:
                snapshot = self._state_provider() or {}
                res = self._risk.evaluate(
                    trading_broker=str(snapshot.get("trading_broker") or "upstox"),
                    funds=snapshot.get("funds"),
                    positions=snapshot.get("positions"),
                    orders=snapshot.get("orders"),
                    broker_adapter=snapshot.get("broker_adapter"),
                    notifier=self._notifier,
                )
            else:
                broker_id = "upstox"
                adapter = None
                if callable(self._broker_provider):
                    broker_id, adapter = self._broker_provider()
                res = self._risk.evaluate(
                    trading_broker=broker_id,
                    broker_adapter=adapter,
                    notifier=self._notifier,
                )
            self._record_success(res)
            return res
        except Exception as exc:
            self._record_failure(exc, dimension="risk_calculations")
            raise

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="algodesk-risk-watchdog",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            is_active = True
            active_cb = self._is_active_callback
            if active_cb is not None:
                try:
                    is_active = bool(active_cb())
                except Exception:
                    is_active = True

            if not is_active:
                with self._lock:
                    self._idle_standby = True
                self._stop.wait(self._interval)
                continue

            with self._lock:
                self._idle_standby = False

            try:
                self.run_once()
            except Exception:
                # Failure count, degraded flag, logging & Telegram alert already recorded in run_once()
                pass

            self._tick_count += 1
            if self._reconcile_callback is not None and (self._tick_count % self._reconcile_every_n == 0):
                self.run_reconciliation_once()

            self._stop.wait(self._interval)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self._thread is not None and self._thread.is_alive()),
                "idle_standby": self._idle_standby,
                "interval_sec": self._interval,
                "degraded": self._degraded,
                "blocks_live_entries": self._degraded,
                "unhealthy_dimensions": [k for k, v in self._dimensions.items() if not v["healthy"]],
                "dimensions": {k: dict(v) for k, v in self._dimensions.items()},
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_evaluation": self._last_eval,
            }


risk_limit_manager = RiskLimitManager()
risk_watchdog = RiskWatchdog(risk_limit_manager, interval_sec=15.0)

__all__ = ["RiskLimitManager", "RiskWatchdog", "risk_limit_manager", "risk_watchdog"]

