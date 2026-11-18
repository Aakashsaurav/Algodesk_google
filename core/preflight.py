"""
core/preflight.py
=================
Phase 4 Preflight Safety Gate & Emergency Kill-Switch Controller.

Runs live verification checks before permitting automated strategy execution:
1. Trading Broker Auth & Live Reachability
2. Data Connection / Failover Readiness
3. Live Margin & Capital Check (Upstox / Zerodha funds API when connected)
4. SQLite Instrument Key / Token Mapping Check
5. NSE Market Hours (09:15–15:30 IST, Mon–Fri)
6. Emergency Kill-Switch Armed & Clear
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional

from broker.registry import BrokerRegistry
from broker.upstox.auth import auth_manager as upstox_auth
from broker.zerodha.auth import zerodha_auth_manager as zerodha_auth
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from core.instruments import instrument_index

logger = logging.getLogger(__name__)

_NSE_OPEN = time(9, 15)
_NSE_CLOSE = time(15, 30)


@dataclass
class PreflightCheckItem:
    id: str
    name: str
    passed: bool
    status: str  # "PASS" | "WARN" | "FAIL"
    severity: str  # "ERROR" | "WARNING"
    message: str
    detail: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class KillSwitchController:
    """Thread-safe persisted emergency trading kill switch backed by SEBI audit ledger."""

    def __init__(
        self,
        state_file: Optional[Path] = None,
        audit: Optional[AuditTrail] = None,
    ) -> None:
        self._explicit_path = state_file
        self._audit = audit or audit_trail
        self._lock = threading.RLock()

    @property
    def state_file(self) -> Path:
        return self._explicit_path or config.KILL_SWITCH_FILE

    def status(self) -> Dict[str, Any]:
        with self._lock:
            path = self.state_file
            if not path.exists():
                return {
                    "armed": True,
                    "engaged": False,
                    "reason": "",
                    "engaged_at_ist": None,
                    "actor": "system",
                }
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return {
                    "armed": True,
                    "engaged": bool(data.get("engaged", False)),
                    "reason": str(data.get("reason", "")),
                    "engaged_at_ist": data.get("engaged_at_ist"),
                    "actor": str(data.get("actor", "system")),
                }
            except (OSError, json.JSONDecodeError):
                return {
                    "armed": True,
                    "engaged": False,
                    "reason": "",
                    "engaged_at_ist": None,
                    "actor": "system",
                }

    def is_engaged(self) -> bool:
        return bool(self.status().get("engaged", False))

    def engage(
        self,
        *,
        reason: str = "Manual emergency stop triggered by operator",
        actor: str = "operator",
        trading_broker: str = "upstox",
    ) -> Dict[str, Any]:
        clean_reason = (reason or "").strip() or "Manual emergency kill switch triggered"
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")

        orders_cancelled = 0
        try:
            adapter = BrokerRegistry.get(trading_broker)
            if adapter.capabilities.supports_trading:
                open_orders = adapter.get_orders()
                for ord_obj in open_orders:
                    oid = getattr(ord_obj, "order_id", "")
                    if oid and adapter.cancel_order(oid):
                        orders_cancelled += 1
        except Exception as exc:
            logger.warning("Kill-switch open order cancel probe note: %s", exc)

        payload = {
            "armed": True,
            "engaged": True,
            "reason": clean_reason,
            "engaged_at_ist": now_ist,
            "actor": actor,
            "orders_cancelled": orders_cancelled,
        }
        with self._lock:
            path = self.state_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        audit_event = self._audit.log_kill_switch(
            engaged=True,
            reason=clean_reason,
            actor=actor,
            trading_broker=trading_broker,
            orders_cancelled=orders_cancelled,
        )
        return {**payload, "audit_event": audit_event}

    def reset(
        self,
        *,
        reason: str = "Operator cleared emergency kill switch after verification",
        actor: str = "operator",
        trading_broker: str = "upstox",
    ) -> Dict[str, Any]:
        clean_reason = (reason or "").strip() or "Operator cleared kill switch"
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        payload = {
            "armed": True,
            "engaged": False,
            "reason": "",
            "engaged_at_ist": None,
            "reset_at_ist": now_ist,
            "actor": actor,
        }
        with self._lock:
            path = self.state_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        audit_event = self._audit.log_kill_switch(
            engaged=False,
            reason=clean_reason,
            actor=actor,
            trading_broker=trading_broker,
            orders_cancelled=0,
        )
        return {**payload, "audit_event": audit_event}


def fetch_broker_margin_summary(broker_id: str, timeout: float = 3.5) -> Dict[str, Any]:
    """Fetch live margin/balance snapshot from the active trading broker if authenticated."""
    bid = (broker_id or "upstox").strip().lower()
    if bid == "upstox":
        token = upstox_auth.get_valid_token()
        url = f"{config.UPSTOX_BASE_URL}/v2/user/get-funds-and-margin?segment=SEC"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        eq = ((raw.get("data") or {}).get("equity")) or {}
        avail = float(eq.get("available_margin") or 0.0)
        used = float(eq.get("used_margin") or 0.0)
        return {"broker": "upstox", "available_margin": round(avail, 2), "used_margin": round(used, 2)}

    if bid == "zerodha":
        token = zerodha_auth.get_valid_token()
        api_key = zerodha_auth.api_key
        url = f"{config.ZERODHA_BASE_URL}/user/margins?segment=equity"
        req = urllib.request.Request(
            url,
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {api_key}:{token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        data = raw.get("data") or {}
        avail = float(((data.get("available") or {}).get("live_balance")) or data.get("net") or 0.0)
        used = float(((data.get("utilised") or {}).get("debits")) or 0.0)
        return {"broker": "zerodha", "available_margin": round(avail, 2), "used_margin": round(used, 2)}

    raise ValueError(f"Live margin check not supported for '{broker_id}'.")


class PreflightChecker:
    """Runs Phase 4 safety, margin, symbol mapping, and kill-switch preflight checks."""

    @staticmethod
    def run_all(
        *,
        trading_broker: str = "upstox",
        data_connection: str = "upstox",
        symbols: Optional[List[str]] = None,
        kill_switch: Optional[KillSwitchController] = None,
        now_ist: Optional[datetime] = None,
        audit: Optional[AuditTrail] = None,
        probe_brokers: bool = True,
    ) -> Dict[str, Any]:
        ks = kill_switch or kill_switch_controller
        trail = audit or audit_trail
        target_symbols = [s.strip().upper() for s in (symbols or ["RELIANCE", "NIFTY"]) if s.strip()]
        ref_dt = now_ist.astimezone(IST_TZ) if now_ist else datetime.now(tz=IST_TZ)

        checks: List[PreflightCheckItem] = []

        # 1. Kill switch status check
        ks_status = ks.status()
        ks_engaged = bool(ks_status.get("engaged", False))
        checks.append(
            PreflightCheckItem(
                id="kill_switch",
                name="Emergency Kill Switch",
                passed=not ks_engaged,
                status="FAIL" if ks_engaged else "PASS",
                severity="ERROR",
                message=(
                    f"Kill switch is ENGAGED ({ks_status.get('reason') or 'operator stop'}). Orders blocked."
                    if ks_engaged
                    else "Kill switch is armed and clear for execution."
                ),
                detail=ks_status,
            )
        )

        # 2. Trading Broker Live Auth Check
        t_adapter = BrokerRegistry.get(trading_broker)
        t_snap = t_adapter.health_check(timeout=3.0) if probe_brokers else None
        t_ok = bool(t_snap and t_snap.is_connected)
        checks.append(
            PreflightCheckItem(
                id="trading_auth",
                name=f"Trading Connection ({t_adapter.display_name})",
                passed=t_ok,
                status="PASS" if t_ok else "FAIL",
                severity="ERROR",
                message=(
                    f"Verified session for {t_snap.account_name} ({t_snap.client_id})."
                    if t_snap and t_ok
                    else (t_snap.message if t_snap else "Trading session not verified.")
                ),
            )
        )

        # 3. Data Connection Readiness Check
        d_adapter = BrokerRegistry.get(data_connection)
        d_snap = d_adapter.health_check(timeout=3.0) if probe_brokers else None
        d_ok = bool(d_snap and d_snap.is_connected)
        checks.append(
            PreflightCheckItem(
                id="data_connection",
                name=f"Data Feed ({d_adapter.display_name})",
                passed=d_ok,
                status="PASS" if d_ok else "WARN",
                severity="ERROR" if d_adapter.capabilities.requires_oauth else "WARNING",
                message=(
                    f"Data provider reachable ({round(d_snap.latency_ms or 0, 1)} ms)."
                    if d_snap and d_ok
                    else (d_snap.message if d_snap else "Data feed standby fallback ready (YFinance).")
                ),
            )
        )

        # 4. Broker Live Margin / Risk Capital Check
        if t_ok:
            try:
                margin = fetch_broker_margin_summary(trading_broker, timeout=3.0)
                avail = float(margin.get("available_margin", 0.0))
                checks.append(
                    PreflightCheckItem(
                        id="margin_balance",
                        name="Margin & Capital Check",
                        passed=avail > 0,
                        status="PASS" if avail > 0 else "WARN",
                        severity="WARNING",
                        message=f"Available equity margin: ₹{avail:,.2f} (Used: ₹{margin.get('used_margin', 0.0):,.2f}).",
                        detail=margin,
                    )
                )
            except Exception as exc:
                checks.append(
                    PreflightCheckItem(
                        id="margin_balance",
                        name="Margin & Capital Check",
                        passed=False,
                        status="WARN",
                        severity="WARNING",
                        message=f"Could not read live broker margin ({exc}). Configured risk cap: ₹{config.TOTAL_CAPITAL:,.0f}.",
                    )
                )
        else:
            checks.append(
                PreflightCheckItem(
                    id="margin_balance",
                    name="Margin & Capital Check",
                    passed=False,
                    status="WARN",
                    severity="WARNING",
                    message=f"Connect {t_adapter.display_name} to read live equity margin (Configured capital: ₹{config.TOTAL_CAPITAL:,.0f}).",
                )
            )

        # 5. SQLite Instrument Key Mapping Check
        mapped: Dict[str, Optional[str]] = {}
        missing: List[str] = []
        lookup_broker = trading_broker if trading_broker in {"upstox", "zerodha"} else "upstox"
        for sym in target_symbols:
            resolved = instrument_index.resolve_key(sym, broker_id=lookup_broker, exchange="NSE")
            mapped[sym] = resolved
            if not resolved:
                missing.append(sym)
        checks.append(
            PreflightCheckItem(
                id="symbol_mapping",
                name="SQLite Instrument Mapping",
                passed=len(missing) == 0,
                status="PASS" if len(missing) == 0 else "FAIL",
                severity="ERROR",
                message=(
                    f"Resolved {len(target_symbols)}/{len(target_symbols)} symbols in local SQLite for {lookup_broker}."
                    if not missing
                    else f"Missing SQLite key/token for: {', '.join(missing)}. Run instrument master sync."
                ),
                detail={"broker": lookup_broker, "mapped": mapped},
            )
        )

        # 6. NSE Market Hours (IST)
        is_weekday = ref_dt.weekday() < 5
        t_now = ref_dt.time()
        market_open = is_weekday and (_NSE_OPEN <= t_now <= _NSE_CLOSE)
        checks.append(
            PreflightCheckItem(
                id="market_hours",
                name="NSE Trading Hours (IST)",
                passed=market_open,
                status="PASS" if market_open else "WARN",
                severity="WARNING",
                message=(
                    f"NSE Cash/F&O session active ({ref_dt.strftime('%H:%M:%S IST')})."
                    if market_open
                    else f"Outside NSE hours 09:15–15:30 IST ({ref_dt.strftime('%a %H:%M IST')}). Safe for paper/dry runs."
                ),
            )
        )

        blocking_failures = [c for c in checks if c.severity == "ERROR" and not c.passed]
        passed_count = sum(1 for c in checks if c.passed)
        overall_ready = len(blocking_failures) == 0

        trail.log_preflight(
            overall_ready=overall_ready,
            passed_count=passed_count,
            total_count=len(checks),
            trading_broker=trading_broker,
            data_connection=data_connection,
        )

        return {
            "ok": True,
            "overall_ready": overall_ready,
            "passed_count": passed_count,
            "total_count": len(checks),
            "blocking_failures": len(blocking_failures),
            "checked_at_ist": ref_dt.isoformat(timespec="seconds"),
            "checks": [c.to_dict() for c in checks],
        }


kill_switch_controller = KillSwitchController()

__all__ = [
    "KillSwitchController",
    "PreflightCheckItem",
    "PreflightChecker",
    "fetch_broker_margin_summary",
    "kill_switch_controller",
]
