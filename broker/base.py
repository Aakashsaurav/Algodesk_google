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
import hashlib
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from config import IST_TZ


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ist_iso_now() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


class BrokerError(RuntimeError):
    """Raised when a broker operation cannot be completed safely."""


class BrokerHttpError(BrokerError):
    """Typed HTTP error carrying the status code so adapters can distinguish 4xx vs 5xx/429."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


class AmbiguousSubmissionError(BrokerError):
    """
    Raised when an order submission fails with a network timeout, connection drop,
    or HTTP 5xx/429 where the broker MAY have accepted the order.
    Callers MUST NOT treat this as REJECTED and MUST NOT blindly re-submit;
    instead persist as SUBMITTED (uncertain) and reconcile via `find_order_by_tag`.
    """


class OrderRejectedError(BrokerError):
    """
    Raised when the broker or pre-flight contract validation DEFINITELY rejected
    an order (e.g. HTTP 400/403/422 validation error, insufficient funds, invalid lot size).
    Safe to mark REJECTED immediately without entering uncertain reconciliation.
    """


class TransientBrokerError(BrokerError):
    """
    Raised for non-order-modifying transient errors (e.g. quote/position read timeout)
    or pre-dispatch local/connection failures where no order reached the exchange.
    """


def build_position_identity(
    *,
    symbol: str,
    exchange: str = "NSE",
    segment: str = "",
    instrument_key: str = "",
    product: str = "",
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
) -> str:
    """
    Canonical composite position identity (H-02 / H-06):
    exchange|segment|instrument_key|symbol|product|expiry|strike|option_type
    Prevents cross-exchange, cross-product (MIS vs CNC), and derivative expiry/strike collisions.
    """
    ex = (exchange or "NSE").strip().upper()
    sym = (symbol or "").strip().upper()
    seg = (segment or "").strip().upper()
    if not seg:
        if ex in {"NFO", "BFO", "MCX"} or expiry or option_type:
            seg = "OPT" if option_type else "FUT"
        else:
            seg = "EQ"
    ikey = (instrument_key or "").strip().upper()
    prod = (product or "").strip().upper()
    exp = (expiry or "").strip()
    strike_str = f"{float(strike):g}" if strike is not None else ""
    opt = (option_type or "").strip().upper()
    return "|".join([ex, seg, ikey, sym, prod, exp, strike_str, opt])


def make_broker_tag(client_order_id: str, max_len: int = 20) -> str:
    """
    Encode a deterministic, unique per-order broker tag fitting strictly within
    Zerodha Kite's 20-character alphanumeric/hyphen tag limit (and Upstox's 40-char limit).
    """
    clean = (client_order_id or "").strip()
    if not clean:
        return ""
    if len(clean) <= max_len and all(ch.isalnum() or ch in "-_" for ch in clean):
        return clean
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[: max(4, max_len - 4)]
    return f"adk-{digest}"[:max_len]


class TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter for broker REST API throttling."""

    def __init__(self, rate_per_sec: float = 8.0, capacity: float = 12.0) -> None:
        self._rate = max(0.5, float(rate_per_sec))
        self._capacity = max(1.0, float(capacity))
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, timeout: float = 5.0) -> bool:
        need = max(0.1, float(tokens))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= need:
                    self._tokens -= need
                    return True
                wait_sec = (need - self._tokens) / self._rate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(wait_sec, remaining, 0.25))


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
    supports_historical: bool = True
    supports_instrument_master: bool = False
    max_depth_levels: int = 5
    execution_grade_feed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """Single level in a Level-2 / Level-5 / Level-30 market depth ladder."""

    price: float
    quantity: int
    orders: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": round(float(self.price), 4),
            "quantity": int(self.quantity),
            "orders": int(self.orders),
        }


@dataclass(slots=True)
class MarketDepth:
    """
    Normalized Level-5 / Level-20 / Level-30 order-book snapshot exposed across
    broker WebSockets (Upstox v3 `full` / `full_d30`, Zerodha KiteTicker) and REST APIs.
    """

    symbol: str
    exchange: str = "NSE"
    bids: List[DepthLevel] = field(default_factory=list)
    asks: List[DepthLevel] = field(default_factory=list)
    total_buy_qty: int = 0
    total_sell_qty: int = 0
    depth_levels: int = 0
    source: str = ""
    transport: str = ""
    ts_ist: Optional[str] = None

    def __post_init__(self) -> None:
        self.symbol = (self.symbol or "").strip().upper()
        self.exchange = (self.exchange or "NSE").strip().upper()
        if self.depth_levels <= 0:
            self.depth_levels = max(len(self.bids), len(self.asks))
        if self.total_buy_qty <= 0 and self.bids:
            self.total_buy_qty = sum(max(0, int(b.quantity)) for b in self.bids)
        if self.total_sell_qty <= 0 and self.asks:
            self.total_sell_qty = sum(max(0, int(a.quantity)) for a in self.asks)

    @property
    def best_bid(self) -> Optional[float]:
        for lvl in self.bids:
            if lvl.price > 0:
                return round(float(lvl.price), 4)
        return None

    @property
    def best_ask(self) -> Optional[float]:
        for lvl in self.asks:
            if lvl.price > 0:
                return round(float(lvl.price), 4)
        return None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return round(ba - bb, 4)

    @property
    def spread_bps(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None or (bb + ba) <= 0:
            return None
        mid = (bb + ba) / 2.0
        return round(((ba - bb) / mid) * 10000.0, 2)

    @property
    def imbalance_ratio(self) -> float:
        tot = self.total_buy_qty + self.total_sell_qty
        if tot <= 0:
            return 0.0
        return round((self.total_buy_qty - self.total_sell_qty) / float(tot), 4)

    def walk_book(self, side: str, quantity: int) -> Dict[str, Any]:
        """
        Walk available order-book levels for a market order of `quantity` shares/contracts.
        BUY/COVER consumes asks; SELL/SHORT consumes bids.
        Returns VWAP fill price, fillable quantity, remaining unfilled quantity, and levels consumed.
        """
        qty_needed = max(0, int(quantity or 0))
        if qty_needed <= 0:
            return {"filled_qty": 0, "unfilled_qty": 0, "vwap": 0.0, "levels_used": 0, "slippage_bps": 0.0}
        side_u = (side or "BUY").strip().upper()
        book = self.asks if side_u in {"BUY", "COVER"} else self.bids
        top_px = book[0].price if (book and book[0].price > 0) else 0.0
        filled = 0
        notional = 0.0
        levels_used = 0
        for lvl in book:
            if lvl.price <= 0 or lvl.quantity <= 0:
                continue
            take = min(qty_needed - filled, int(lvl.quantity))
            if take > 0:
                filled += take
                notional += take * float(lvl.price)
                levels_used += 1
            if filled >= qty_needed:
                break
        vwap = (notional / float(filled)) if filled > 0 else 0.0
        slip_bps = 0.0
        if filled > 0 and top_px > 0:
            diff = (vwap - top_px) if side_u in {"BUY", "COVER"} else (top_px - vwap)
            slip_bps = round((diff / top_px) * 10000.0, 2)
        return {
            "filled_qty": filled,
            "unfilled_qty": max(0, qty_needed - filled),
            "vwap": round(vwap, 4),
            "levels_used": levels_used,
            "slippage_bps": max(0.0, slip_bps),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "depth_levels": self.depth_levels,
            "bids": [b.to_dict() for b in self.bids],
            "asks": [a.to_dict() for a in self.asks],
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "spread_bps": self.spread_bps,
            "total_buy_qty": int(self.total_buy_qty),
            "total_sell_qty": int(self.total_sell_qty),
            "imbalance_ratio": self.imbalance_ratio,
            "source": self.source,
            "transport": self.transport,
            "ts_ist": self.ts_ist,
        }


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
    """Canonical Order Management & broker order lifecycle states."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

    @property
    def is_active(self) -> bool:
        return self in {
            OrderStatus.CREATED,
            OrderStatus.VALIDATED,
            OrderStatus.SUBMITTED,
            OrderStatus.OPEN,
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
        }


@dataclass(frozen=True, slots=True)
class InstrumentContract:
    """Canonical instrument & contract specification across NSE/BSE/NFO/BFO/MCX."""

    symbol: str
    exchange: str = "NSE"
    segment: str = "EQUITY"  # EQUITY | FUT | OPT | COMMODITY
    lot_size: int = 1
    tick_size: float = 0.05
    multiplier: float = 1.0
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None  # CE | PE | None
    upstox_key: str = ""
    zerodha_token: str = ""

    @property
    def effective_lot_size(self) -> int:
        return max(1, int(self.lot_size or 1))

    @property
    def effective_tick_size(self) -> float:
        ts = float(self.tick_size or 0.05)
        return ts if ts > 0 else 0.05

    @property
    def is_derivative(self) -> bool:
        ex = (self.exchange or "").strip().upper()
        seg = (self.segment or "").strip().upper()
        if ex in {"NFO", "BFO", "MCX", "NSE_FO", "BSE_FO", "MCX_FO"}:
            return True
        if seg in {"FUT", "OPT", "FO", "FNO", "NSE_FO", "BSE_FO", "MCX_FO"}:
            return True
        return bool(self.expiry or self.option_type or self.effective_lot_size > 1)

    def identity_key(self, product: str = "") -> str:
        """Full canonical contract identity key avoiding cross-exchange/product/expiry collisions."""
        ex = (self.exchange or "NSE").strip().upper()
        sym = (self.symbol or "").strip().upper()
        prod = (product or "").strip().upper()
        exp = (self.expiry or "").strip()
        strike_str = f"{float(self.strike):g}" if self.strike is not None else ""
        opt = (self.option_type or "").strip().upper()
        parts = [ex, sym]
        if prod:
            parts.append(prod)
        if exp or strike_str or opt:
            parts.extend([exp, strike_str, opt])
        return ":".join(parts)

    def normalize_quantity(self, raw_qty: int) -> int:
        """Floor raw quantity to the nearest valid contract lot-size multiple (0 if below 1 lot)."""
        q = max(0, int(raw_qty or 0))
        lot = self.effective_lot_size
        if lot <= 1:
            return q
        return (q // lot) * lot

    def validate_quantity(self, qty: int) -> Optional[str]:
        """Return None if qty is valid for this contract, else an error message."""
        q = int(qty or 0)
        if q <= 0:
            return f"Order quantity must be > 0 for {self.exchange}:{self.symbol}."
        lot = self.effective_lot_size
        if lot > 1 and (q % lot) != 0:
            return (
                f"Invalid quantity {q} for {self.exchange}:{self.symbol}: "
                f"must be an exact multiple of lot_size={lot}."
            )
        return None

    def normalize_price(self, price: float) -> float:
        """Snap price to the contract's exchange tick size."""
        ts = self.effective_tick_size
        if price <= 0:
            return 0.0
        steps = round(float(price) / ts)
        return round(steps * ts, 4)

    def validate_price(self, price: Optional[float], *, allow_none: bool = False) -> Optional[str]:
        """Validate price finiteness, positivity, and tick-size alignment."""
        import math

        if price is None:
            return None if allow_none else f"Price is required for {self.exchange}:{self.symbol}."
        try:
            px = float(price)
        except (TypeError, ValueError):
            return f"Invalid non-numeric price {price!r} for {self.exchange}:{self.symbol}."
        if not math.isfinite(px) or px <= 0:
            return f"Price must be finite and > 0 for {self.exchange}:{self.symbol}, got {price!r}."
        ts = self.effective_tick_size
        if ts > 0:
            nearest = round(px / ts) * ts
            if abs(px - nearest) > max(1e-6, ts * 1e-3):
                return (
                    f"Price {px} for {self.exchange}:{self.symbol} is not aligned to "
                    f"tick_size={ts} (nearest valid: {nearest:.4f})."
                )
        return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OrderRequest:
    """Single canonical broker order request with mandatory exchange/product validation & idempotency key."""

    symbol: str
    side: str | OrderSide
    quantity: int
    exchange: str = ""
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
        meta_exch = str((self.metadata or {}).get("exchange") or "").strip().upper()
        self.exchange = (self.exchange or meta_exch or "").strip().upper()
        if self.exchange:
            self.metadata["exchange"] = self.exchange
        self.order_type = self.order_type.strip().upper()
        self.product = self.product.strip().upper()
        self.validity = self.validity.strip().upper()
        self.tag = self.tag.strip()
        self.client_order_id = self.client_order_id.strip() or f"adk-{uuid.uuid4().hex[:14]}"
        self.algo_tag = self.algo_tag.strip()

        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if self.order_type not in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT", "SL", "SL-M"}:
            raise ValueError(f"Unsupported order_type: {self.order_type!r}")
        if self.price is not None and self.price < 0:
            raise ValueError(f"price must be >= 0 when provided, got {self.price}")
        if self.price == 0 and self.order_type == "MARKET":
            self.price = None
        if self.trigger_price is not None and self.trigger_price < 0:
            raise ValueError(
                f"trigger_price must be >= 0 when provided, got {self.trigger_price}"
            )
        if self.trigger_price == 0 and self.order_type == "MARKET":
            self.trigger_price = None

    def require_exchange(self) -> str:
        """Return validated exchange or raise ValueError for live broker dispatch."""
        ex = (self.exchange or str((self.metadata or {}).get("exchange") or "")).strip().upper()
        if not ex and self.instrument_key and "|" in self.instrument_key:
            prefix = self.instrument_key.split("|", 1)[0].strip().upper()
            prefix_map = {
                "NSE_EQ": "NSE",
                "NSE_INDEX": "NSE",
                "NSE_FO": "NFO",
                "BSE_EQ": "BSE",
                "BSE_INDEX": "BSE",
                "BSE_FO": "BFO",
                "MCX_FO": "MCX",
                "MCX_EQ": "MCX",
                "NFO": "NFO",
                "BFO": "BFO",
                "MCX": "MCX",
                "NSE": "NSE",
                "BSE": "BSE",
            }
            ex = prefix_map.get(prefix, "")
            if ex:
                self.exchange = ex
                self.metadata["exchange"] = ex
        if not ex:
            raise ValueError(
                f"Exchange is mandatory for live order on {self.symbol}; refusing silent default."
            )
        return ex

    @property
    def trading_symbol(self) -> str:
        return self.symbol

    @property
    def transaction_type(self) -> str:
        """Canonical broker transaction side: BUY/COVER -> BUY, SELL/SHORT -> SELL."""
        return "BUY" if self.side in {OrderSide.BUY.value, OrderSide.COVER.value} else "SELL"

    @property
    def is_exit(self) -> bool:
        return self.side in {OrderSide.SELL.value, OrderSide.COVER.value}


@dataclass(slots=True)
class ExecutionFill:
    """Verified broker or paper fill event used to update position ledgers."""

    fill_id: str
    order_id: str
    symbol: str
    exchange: str
    side: str
    quantity: int
    price: float
    product: str = "MIS"
    client_order_id: str = ""
    timestamp: datetime = field(default_factory=_utc_now)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side,
            "quantity": int(self.quantity),
            "price": float(self.price),
            "product": self.product,
            "timestamp": self.timestamp.isoformat(),
        }


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
    def symbol(self) -> str:
        return self.request.symbol

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
    exchange: str = "NSE"
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
        self.exchange = (self.exchange or "NSE").strip().upper()
        if self.quantity < 0:
            self.direction = -1
            self.quantity = abs(self.quantity)
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
    def last_price(self) -> float:
        return float(self.market_price if self.market_price is not None else self.average_price)

    @property
    def market_value(self) -> float:
        return self.last_price * self.quantity

    @property
    def identity_key(self) -> str:
        """Full composite instrument+product position identity for reconciliation."""
        meta = self.metadata or {}
        return build_position_identity(
            symbol=self.symbol,
            exchange=self.exchange,
            segment=str(meta.get("segment") or ""),
            instrument_key=self.instrument_key or str(meta.get("instrument_key") or ""),
            product=self.product,
            expiry=meta.get("expiry"),
            strike=meta.get("strike"),
            option_type=meta.get("option_type"),
        )


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

    Every broker adapter implements trading methods plus authoritative account/
    order/trade/position reads so the OMS, risk watchdog, and kill switch never
    rely on stale in-memory state.
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

    def get_trades(self) -> Sequence[ExecutionFill]:
        """Return verified fills/trades for the current trading day."""
        return []

    def get_funds(self, segment: str = "equity") -> Dict[str, float]:
        """Return authoritative broker funds (`available`, `used`, `total`)."""
        bal = self.get_balance()
        avail = float(bal.available_cash or 0.0)
        used = float(bal.margin_used or 0.0)
        return {
            "available": round(avail, 2),
            "used": round(used, 2),
            "total": round(avail + used, 2),
            "segment": segment,
        }

    def get_order(self, order_id: str) -> Optional[BrokerOrder]:
        """Fetch latest status of a specific broker order."""
        oid = str(order_id or "").strip()
        if not oid:
            return None
        for o in self.get_orders():
            if str(o.order_id) == oid:
                return o
        return None

    def find_order_by_tag(self, tag: str) -> Optional[BrokerOrder]:
        """
        Look up an order in the broker order book by its unique per-order tag or
        client_order_id (used to recover after an AmbiguousSubmissionError).
        """
        needle = str(tag or "").strip()
        if not needle:
            return None
        for o in self.get_orders():
            req = getattr(o, "request", None)
            o_tag = str(getattr(req, "tag", "") or "").strip()
            o_cid = str(getattr(req, "client_order_id", "") or "").strip()
            raw_tag = str((getattr(o, "raw", None) or {}).get("tag") or "").strip()
            if needle in {o_tag, o_cid, raw_tag}:
                return o
        return None

    def safe_balance(self) -> float:
        try:
            return float(self.get_balance().balance)
        except Exception:
            return 0.0

    # ── Modular provider hooks (plug-and-play market data, depth, historical, instruments, WS) ──
    def is_authenticated(self) -> bool:
        """Return True if this provider currently holds valid credentials/session."""
        try:
            return bool(self.health_check(timeout=2.0).is_connected)
        except Exception:
            return False

    def fetch_market_depth(
        self,
        symbol: str,
        exchange: str = "NSE",
        instrument_key: str = "",
        levels: int = 5,
    ) -> Optional[MarketDepth]:
        """
        Fetch Level-5 or Level-30 market depth via REST API.
        Brokers that expose order-book depth (Upstox L5/L30, Zerodha L5, etc.) override this.
        """
        return None

    def fetch_live_quote(
        self,
        symbol: str,
        exchange: str = "NSE",
        instrument_key: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a single normalized live quote dict with authentic exchange/provider
        timestamp (`source_timestamp_ist` / `exchange_timestamp`) and optional `depth`.
        """
        return None

    def fetch_historical_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        *,
        instrument_key: str = "",
        adjustment_mode: str = "raw",
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch historical OHLCV candles from this provider.
        Returning None indicates this adapter delegates to core/historical fallback.
        """
        return None

    def sync_instruments(self, store: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """
        Synchronize this provider's instrument master into `store`.
        Returning None indicates no custom instrument sync hook is implemented.
        """
        return None

    def create_ws_client(
        self,
        *,
        on_tick: Any = None,
        on_status: Any = None,
        depth_levels: int = 5,
    ) -> Optional[Any]:
        """
        Create a provider-specific WebSocket feed client supporting L5/L30 depth.
        Returning None indicates this provider does not offer a WebSocket stream.
        """
        return None


class MarketDataProviderBase(BrokerBase):
    """
    Convenience base class for data-only providers (e.g. Yahoo Finance, NSE/BSE feeds,
    Polygon, TrueData) that do not support live order placement.
    Adding a new data provider only requires subclassing this and registering via
    `BrokerRegistry.register_data_provider("name", MyProvider)`.
    """

    capabilities: BrokerCapabilities = BrokerCapabilities(
        supports_trading=False,
        supports_market_data=True,
        supports_websocket=False,
        requires_oauth=False,
        supports_historical=True,
        supports_instrument_master=False,
        max_depth_levels=0,
        execution_grade_feed=False,
    )

    def connect(self) -> bool:
        return True

    def health_check(self, timeout: float = 2.0) -> BrokerHealthSnapshot:
        return BrokerHealthSnapshot(
            broker_id=self.broker_id,
            display_name=self.display_name,
            stage="active",
            state=ConnectionState.CONNECTED,
            capabilities=self.capabilities,
            message=f"{self.display_name} market data provider ready.",
            has_credentials=True,
            auth_mode="public" if not self.capabilities.requires_oauth else "oauth2",
        )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        raise OrderRejectedError(f"{self.display_name} is a market-data provider and does not support trading.")

    def cancel_order(self, order_id: str) -> bool:
        raise OrderRejectedError(f"{self.display_name} is a market-data provider and does not support order cancellation.")

    def get_positions(self) -> Sequence[BrokerPosition]:
        return []

    def get_balance(self) -> AccountBalance:
        return AccountBalance(cash=0.0, available_cash=0.0, equity=0.0, currency="INR")

    def get_orders(self) -> Sequence[BrokerOrder]:
        return []


