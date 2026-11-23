"""
core/runtime.py
---------------
Phase 3 Core Consolidation:
Reconciles duplicate abstractions found during the Algodesk_R4 audit:
- Single canonical BaseFeedAdapter & BaseStrategy contracts.
- Single CanonicalOrderManager delegating directly to active BrokerRegistry.
- Repaired CanonicalSessionManager & LiveBotEngine wiring (resolving the
  SessionManager constructor signature mismatch identified in Algodesk_R4).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from broker.base import BrokerBase, OrderRequest as CanonicalOrderRequest
from broker.registry import BrokerRegistry
from core.audit import AuditTrail, audit_trail
from core.preflight import KillSwitchController, kill_switch_controller

logger = logging.getLogger(__name__)


def OrderRequest(
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    quantity: int = 1,
    exchange: str = "",
    order_type: str = "MARKET",
    product: str = "MIS",
    price: Optional[float] = None,
    trigger_price: Optional[float] = None,
    tag: str = "algodesk",
    *,
    trading_symbol: Optional[str] = None,
    transaction_type: Optional[str] = None,
    instrument_key: str = "",
    validity: str = "DAY",
    client_order_id: str = "",
    algo_tag: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> CanonicalOrderRequest:
    """
    Backwards-compatible constructor returning the single canonical
    `broker.base.OrderRequest` model (accepting either `symbol`/`side` or
    legacy `trading_symbol`/`transaction_type` arguments).
    """
    eff_symbol = (symbol or trading_symbol or "").strip().upper()
    eff_side = (side or transaction_type or "BUY").strip().upper()
    eff_price = float(price) if (price is not None and float(price) > 0) else None
    eff_trigger = float(trigger_price) if (trigger_price is not None and float(trigger_price) > 0) else None
    return CanonicalOrderRequest(
        symbol=eff_symbol,
        side=eff_side,
        quantity=int(quantity),
        exchange=(exchange or "").strip().upper(),
        instrument_key=instrument_key,
        order_type=order_type,
        product=product,
        price=eff_price,
        trigger_price=eff_trigger,
        validity=validity,
        tag=tag,
        client_order_id=client_order_id,
        algo_tag=algo_tag,
        metadata=dict(metadata or {}),
    )


@dataclass
class TradingState:
    active_trading_broker: str = "upstox"
    active_data_connection: str = "upstox"
    kill_switch_engaged: bool = False
    open_orders: List[Dict[str, Any]] = field(default_factory=list)


class BaseFeedAdapter(ABC):
    """Canonical feed contract implemented by Upstox WS, KiteTicker, and polling feeds."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def subscribe(self, instruments: List[Any], mode: str = "ltpc") -> None: ...

    @abstractmethod
    def status(self) -> Dict[str, Any]: ...


class BaseStrategy(ABC):
    """Canonical strategy lifecycle contract shared by live engine & backtester."""

    name: str = "base_strategy"

    @abstractmethod
    def on_bar(self, bar: Dict[str, Any]) -> Optional[CanonicalOrderRequest]: ...


class CanonicalOrderManager:
    """Single canonical order router bound to BrokerRegistry."""

    def __init__(self, broker: Optional[BrokerBase] = None, broker_id: str = "upstox") -> None:
        self._broker = broker or BrokerRegistry.get(broker_id)

    def place(self, req: CanonicalOrderRequest) -> Any:
        if not self._broker.capabilities.supports_trading:
            raise ValueError(f"{self._broker.display_name} does not support order execution.")
        req.require_exchange()
        return self._broker.place_order(req)


class CanonicalSessionManager:
    """
    Unified SessionManager accepting explicit, unambiguous dependencies.
    Resolves the Algodesk_R4 LiveBotEngine -> SessionManager positional argument mismatch.
    """

    def __init__(
        self,
        *,
        state: TradingState,
        broker: BrokerBase,
        order_manager: Optional[CanonicalOrderManager] = None,
        risk_engine: Optional[Any] = None,
        session_store: Optional[Any] = None,
        kill_switch: Optional[KillSwitchController] = None,
        audit: Optional[AuditTrail] = None,
    ) -> None:
        self.state = state
        self.broker = broker
        self.order_manager = order_manager or CanonicalOrderManager(broker=broker)
        self.risk_engine = risk_engine
        self.session_store = session_store
        self._kill_switch = kill_switch or kill_switch_controller
        self._audit = audit or audit_trail

    def submit_signal_order(self, req: CanonicalOrderRequest) -> Any:
        if self.state.kill_switch_engaged or self._kill_switch.is_engaged():
            try:
                self._audit.log(
                    event_type="ORDER_BLOCKED_KILL_SWITCH",
                    actor="session_manager",
                    payload={
                        "trading_symbol": req.symbol,
                        "exchange": req.exchange,
                        "transaction_type": req.transaction_type,
                        "side": req.side,
                        "quantity": req.quantity,
                    },
                )
            except Exception:
                pass
            raise RuntimeError("Trading kill switch is engaged; order rejected.")
        if self.risk_engine is not None and hasattr(self.risk_engine, "validate_order"):
            verdict = self.risk_engine.validate_order(
                symbol=req.symbol,
                quantity=req.quantity,
                price=float(req.price or 0.0),
                transaction_type=req.transaction_type,
                current_open_positions=len(self.state.open_orders),
            )
            if not verdict.get("allowed", True):
                reasons = "; ".join(verdict.get("reasons", []) or ["Blocked by portfolio risk limits"])
                raise RuntimeError(f"Pre-trade risk limit rejected order: {reasons}")
        res = self.order_manager.place(req)
        try:
            self._audit.log(
                event_type="ORDER_SUBMITTED",
                actor="session_manager",
                payload={
                    "trading_symbol": req.symbol,
                    "exchange": req.exchange,
                    "transaction_type": req.transaction_type,
                    "side": req.side,
                    "product": req.product,
                    "quantity": req.quantity,
                    "order_type": req.order_type,
                    "client_order_id": req.client_order_id,
                },
            )
        except Exception:
            pass
        return res


class LiveBotEngine:
    """Consolidated runtime coordinator wiring BrokerRegistry + CanonicalSessionManager."""

    def __init__(
        self,
        trading_broker_id: str = "upstox",
        data_connection_id: str = "upstox",
        session_store: Optional[Any] = None,
        risk_engine: Optional[Any] = None,
        kill_switch: Optional[KillSwitchController] = None,
        audit: Optional[AuditTrail] = None,
    ) -> None:
        self.state = TradingState(
            active_trading_broker=trading_broker_id,
            active_data_connection=data_connection_id,
        )
        self.broker = BrokerRegistry.get(trading_broker_id)
        self.order_manager = CanonicalOrderManager(broker=self.broker)
        self.session_manager = CanonicalSessionManager(
            state=self.state,
            broker=self.broker,
            order_manager=self.order_manager,
            risk_engine=risk_engine,
            session_store=session_store,
            kill_switch=kill_switch,
            audit=audit,
        )


__all__ = [
    "BaseFeedAdapter",
    "BaseStrategy",
    "CanonicalOrderManager",
    "CanonicalSessionManager",
    "LiveBotEngine",
    "OrderRequest",
    "TradingState",
]
