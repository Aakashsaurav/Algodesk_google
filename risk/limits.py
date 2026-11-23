"""
risk/limits.py
==============
Portfolio Risk Limit persistence, pre-trade order validation (`validate_order`),
and live broker exposure + daily loss evaluation with SEBI SHA-256 audit & kill switch.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from broker.registry import BrokerRegistry
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from core.preflight import KillSwitchController, kill_switch_controller


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
            if "auto_engage_kill_switch" in updates and updates["auto_engage_kill_switch"] is not None:
                next_cfg["auto_engage_kill_switch"] = bool(updates["auto_engage_kill_switch"])

            next_cfg["updated_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            path = self.limits_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(next_cfg, indent=2), encoding="utf-8")

        self._audit.log(
            event_type="RISK_LIMITS_UPDATED",
            actor=actor,
            payload={
                "max_capital_per_trade_pct": next_cfg["max_capital_per_trade_pct"],
                "max_daily_loss_inr": next_cfg["max_daily_loss_inr"],
                "max_open_positions": next_cfg["max_open_positions"],
                "max_gross_exposure_inr": next_cfg["max_gross_exposure_inr"],
                "auto_engage_kill_switch": next_cfg["auto_engage_kill_switch"],
            },
        )
        return next_cfg

    def validate_order(
        self,
        *,
        symbol: str,
        quantity: int,
        price: float = 0.0,
        transaction_type: str = "BUY",
        current_open_positions: int = 0,
        current_gross_exposure_inr: float = 0.0,
    ) -> Dict[str, Any]:
        """Pre-trade risk gate invoked by `CanonicalSessionManager.submit_signal_order`."""
        limits = self.load_limits()
        reasons: List[str] = []

        if self._kill_switch.is_engaged():
            reasons.append("Emergency Kill Switch is engaged.")

        qty = max(0, int(quantity or 0))
        px = max(0.0, float(price or 0.0))
        notional = qty * px
        cap = float(limits["total_capital_inr"])
        max_trade_notional = cap * (float(limits["max_capital_per_trade_pct"]) / 100.0)

        if qty <= 0:
            reasons.append("Order quantity must be > 0.")
        if px > 0 and notional > max_trade_notional:
            reasons.append(
                f"Order notional ₹{notional:,.2f} exceeds per-trade cap ₹{max_trade_notional:,.2f} ({limits['max_capital_per_trade_pct']}%)."
            )
        if str(transaction_type).upper() in {"BUY", "SHORT"}:
            if current_open_positions >= int(limits["max_open_positions"]):
                reasons.append(
                    f"Open positions ({current_open_positions}) reached limit ({limits['max_open_positions']})."
                )
            if px > 0 and (current_gross_exposure_inr + notional) > float(limits["max_gross_exposure_inr"]):
                reasons.append(
                    f"Projected gross exposure ₹{current_gross_exposure_inr + notional:,.2f} exceeds cap ₹{limits['max_gross_exposure_inr']:,.2f}."
                )

        allowed = len(reasons) == 0
        if not allowed:
            try:
                self._audit.log(
                    event_type="PRETRADE_RISK_BLOCKED",
                    actor="risk_guard",
                    payload={"symbol": symbol, "quantity": qty, "notional": round(notional, 2), "reasons": reasons},
                )
            except Exception:
                pass
        return {"allowed": allowed, "reasons": reasons, "limits": limits}

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
    Headless continuous risk & reconciliation background thread.
    Runs independently of the web UI so daily-loss limits engage the kill switch
    and notify Telegram even when no browser is open.
    """

    def __init__(
        self,
        risk_mgr: Optional[RiskLimitManager] = None,
        *,
        interval_sec: float = 10.0,
        broker_provider: Optional[Any] = None,
        state_provider: Optional[Any] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        self._risk = risk_mgr or risk_limit_manager
        self._interval = max(1.0, float(interval_sec))
        self._broker_provider = broker_provider
        self._state_provider = state_provider
        self._notifier = notifier
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_eval: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()

    def run_once(self) -> Dict[str, Any]:
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
        with self._lock:
            self._last_eval = res
        return res

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
            try:
                self.run_once()
            except Exception:
                pass
            self._stop.wait(self._interval)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self._thread is not None and self._thread.is_alive()),
                "interval_sec": self._interval,
                "last_evaluation": self._last_eval,
            }


risk_limit_manager = RiskLimitManager()
risk_watchdog = RiskWatchdog(risk_limit_manager, interval_sec=15.0)

__all__ = ["RiskLimitManager", "RiskWatchdog", "risk_limit_manager", "risk_watchdog"]

