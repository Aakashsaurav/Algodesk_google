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

from broker.base import BrokerBase
from broker.registry import BrokerRegistry

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    trading_symbol: str
    exchange: str
    transaction_type: str  # BUY | SELL
    quantity: int
    order_type: str = "MARKET"  # MARKET | LIMIT | SL | SL-M
    product: str = "I"  # I / MIS / CNC / D
    price: float = 0.0
    trigger_price: float = 0.0
    tag: str = "algodesk"


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
    def on_bar(self, bar: Dict[str, Any]) -> Optional[OrderRequest]: ...


class CanonicalOrderManager:
    """Single canonical order router bound to BrokerRegistry."""

    def __init__(self, broker: Optional[BrokerBase] = None, broker_id: str = "upstox") -> None:
        self._broker = broker or BrokerRegistry.get(broker_id)

    def place(self, req: OrderRequest) -> Any:
        if not self._broker.capabilities.supports_trading:
            raise ValueError(f"{self._broker.display_name} does not support order execution.")
        return self._broker.place_order(
            symbol=req.trading_symbol,
            exchange=req.exchange,
            qty=req.quantity,
            side=req.transaction_type,
            order_type=req.order_type,
            price=req.price,
            trigger_price=req.trigger_price,
            product=req.product,
        )


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
    ) -> None:
        self.state = state
        self.broker = broker
        self.order_manager = order_manager or CanonicalOrderManager(broker=broker)
        self.risk_engine = risk_engine
        self.session_store = session_store

    def submit_signal_order(self, req: OrderRequest) -> Any:
        if self.state.kill_switch_engaged:
            raise RuntimeError("Trading kill switch is engaged; order rejected.")
        return self.order_manager.place(req)


class LiveBotEngine:
    """Consolidated runtime coordinator wiring BrokerRegistry + CanonicalSessionManager."""

    def __init__(
        self,
        trading_broker_id: str = "upstox",
        data_connection_id: str = "upstox",
        session_store: Optional[Any] = None,
        risk_engine: Optional[Any] = None,
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
