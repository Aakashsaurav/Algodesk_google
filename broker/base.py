"""
broker/base.py
==============
Broker-agnostic contract and connectivity health interface for AlgoDesk.

Preserves all execution models from Algodesk_R4 (OrderRequest, BrokerOrder,
BrokerPosition, AccountBalance, BrokerBase) while adding modular capability
metadata and live connectivity probing (BrokerCapabilities, ConnectionState,
BrokerHealthSnapshot) so multiple brokers (Upstox, Zerodha, Fyers) and
data-only providers (YFinance) plug into the registry and web UI seamlessly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from config import IST_TZ


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ist_iso_now() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


class BrokerError(RuntimeError):
    """Raised when a broker operation cannot be completed safely."""


class ConnectionState(str, Enum):
    """Normalized connectivity states surfaced by the UI indicator lights."""

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    EXPIRED = "EXPIRED"
    UNCONFIGURED = "UNCONFIGURED"
    PLANNED = "PLANNED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    """Declares what roles a broker or provider supports in AlgoDesk."""

    supports_trading: bool = True
    supports_market_data: bool = True
    supports_websocket: bool = False
    requires_oauth: bool = True

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass(slots=True)
class BrokerHealthSnapshot:
    """Live connectivity and capability snapshot for a broker or data source."""

    broker_id: str
    display_name: str
    stage: str  # "active" | "planned" | "standby"
    state: ConnectionState
    capabilities: BrokerCapabilities
    latency_ms: Optional[float] = None
    account_name: str = ""
    client_id: str = ""
    token_expires_at_ist: Optional[str] = None
    last_checked_at: str = field(default_factory=_ist_iso_now)
    message: str = ""
    can_reconnect: bool = False
    has_credentials: bool = False
    auth_mode: str = "oauth2"
    roadmap_note: str = ""

    @property
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker_id": self.broker_id,
            "display_name": self.display_name,
            "stage": self.stage,
            "state": self.state.value,
            "is_connected": self.is_connected,
            "capabilities": self.capabilities.to_dict(),
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "account_name": self.account_name,
            "client_id": self.client_id,
            "token_expires_at_ist": self.token_expires_at_ist,
            "last_checked_at": self.last_checked_at,
            "message": self.message,
            "can_reconnect": self.can_reconnect,
            "has_credentials": self.has_credentials,
            "auth_mode": self.auth_mode,
            "roadmap_note": self.roadmap_note,
        }


class OrderSide(str, Enum):
    """Canonical order directions supported by AlgoDesk."""

    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"
    COVER = "COVER"

    @classmethod
    def normalize(cls, value: str | "OrderSide") -> "OrderSide":
        if isinstance(value, cls):
            return value
        try:
            return cls(value.upper().strip())
        except Exception as exc:
            raise ValueError(
                f"Invalid order side: {value!r}. "
                f"Expected one of: {[item.value for item in cls]}"
            ) from exc


class OrderStatus(str, Enum):
    """Broker order lifecycle states."""

    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(slots=True)
class OrderRequest:
    """Generic broker order request with SEBI algo-tag & client order ID."""

    symbol: str
    side: str | OrderSide
    quantity: int
    instrument_key: str = ""
    order_type: str = "MARKET"
    product: str = "MIS"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    validity: str = "DAY"
    tag: str = ""
    client_order_id: str = ""
    algo_tag: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        import uuid

        self.symbol = self.symbol.strip().upper()
        self.side = OrderSide.normalize(self.side).value
        self.order_type = self.order_type.strip().upper()
        self.product = self.product.strip().upper()
        self.validity = self.validity.strip().upper()
        self.tag = self.tag.strip()
        self.client_order_id = self.client_order_id.strip() or str(uuid.uuid4())[:16]
        self.algo_tag = self.algo_tag.strip()

        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if self.order_type not in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}:
            raise ValueError(f"Unsupported order_type: {self.order_type!r}")
        if self.price is not None and self.price <= 0:
            raise ValueError(f"price must be > 0 when provided, got {self.price}")
        if self.trigger_price is not None and self.trigger_price <= 0:
            raise ValueError(
                f"trigger_price must be > 0 when provided, got {self.trigger_price}"
            )

    @property
    def is_exit(self) -> bool:
        return self.side in {OrderSide.SELL.value, OrderSide.COVER.value}


@dataclass(slots=True)
class BrokerOrder:
    """Order snapshot returned by place_order and get_orders."""

    order_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    average_fill_price: Optional[float] = None
    rejection_reason: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.request.quantity - self.filled_quantity)


@dataclass(slots=True)
class BrokerPosition:
    """Canonical position snapshot for broker-independent portfolio reads."""

    symbol: str
    quantity: int
    average_price: float
    direction: int = 1
    instrument_key: str = ""
    product: str = "MIS"
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    market_price: Optional[float] = None
    opened_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.symbol = self.symbol.strip().upper()
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if self.average_price <= 0:
            raise ValueError(f"average_price must be > 0, got {self.average_price}")
        if self.direction not in (-1, 1):
            raise ValueError("direction must be +1 (long) or -1 (short)")
        self.product = self.product.strip().upper()

    @property
    def signed_quantity(self) -> int:
        return self.quantity * self.direction

    @property
    def market_value(self) -> float:
        price = self.market_price if self.market_price is not None else self.average_price
        return price * self.quantity


@dataclass(slots=True)
class AccountBalance:
    """Broker balance snapshot."""

    cash: float
    available_cash: Optional[float] = None
    equity: Optional[float] = None
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    currency: str = "INR"
    updated_at: datetime = field(default_factory=_utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = float(self.cash)
        self.available_cash = (
            float(self.cash) if self.available_cash is None else float(self.available_cash)
        )
        self.equity = (
            float(self.cash + self.unrealized_pnl - self.margin_used)
            if self.equity is None
            else float(self.equity)
        )
        self.margin_used = float(self.margin_used)
        self.unrealized_pnl = float(self.unrealized_pnl)
        self.realized_pnl = float(self.realized_pnl)
        self.currency = self.currency.strip().upper() or "INR"

    @property
    def balance(self) -> float:
        return float(self.available_cash)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cash": self.cash,
            "available_cash": self.available_cash,
            "balance": self.balance,
            "equity": self.equity,
            "margin_used": self.margin_used,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "currency": self.currency,
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
        }


class BrokerBase(ABC):
    """
    Abstract broker & connectivity contract.

    Every broker adapter implements trading methods plus lightweight health/auth
    hooks so the UI and runtime never depend on broker-specific SDK internals.
    """

    name: str = "generic-broker"
    display_name: str = "Generic Broker"
    stage: str = "active"  # "active" | "planned" | "standby"
    capabilities: BrokerCapabilities = BrokerCapabilities()

    @abstractmethod
    def connect(self) -> bool:
        """Open/verify the broker session and return True when ready."""

    def disconnect(self) -> bool:
        """Close/logout the broker session cleanly."""
        return True

    def health_check(self, timeout: float = 3.5) -> BrokerHealthSnapshot:
        """Return a live connectivity snapshot without heavy instrument loads."""
        return BrokerHealthSnapshot(
            broker_id=self.name,
            display_name=self.display_name,
            stage=self.stage,
            state=ConnectionState.DISCONNECTED,
            capabilities=self.capabilities,
            message="Health check not implemented for this adapter.",
        )

    def get_login_url(self, redirect_uri: Optional[str] = None) -> str:
        """Return the broker OAuth authorization URL if applicable."""
        raise BrokerError(f"{self.display_name} does not support interactive OAuth login yet.")

    def complete_auth(self, code_or_url: str, redirect_uri: Optional[str] = None) -> BrokerHealthSnapshot:
        """Complete OAuth exchange and return updated health snapshot."""
        raise BrokerError(f"{self.display_name} does not support OAuth token exchange yet.")

    @abstractmethod
    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        """Submit an order to the broker."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a previously placed order."""

    @abstractmethod
    def get_positions(self) -> Sequence[BrokerPosition]:
        """Return all open positions known to the broker."""

    @abstractmethod
    def get_balance(self) -> AccountBalance:
        """Return the current account balance snapshot."""

    @abstractmethod
    def get_orders(self) -> Sequence[BrokerOrder]:
        """Return the current order book snapshot."""

    def safe_balance(self) -> float:
        try:
            return float(self.get_balance().balance)
        except Exception:
            return 0.0
