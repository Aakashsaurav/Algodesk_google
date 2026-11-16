"""
broker/zerodha/broker.py
========================
Phase 2 Active Zerodha Kite Connect v3 adapter implementing BrokerBase with:
- Live profile verification via `/user/profile` (`X-Kite-Version: 3`).
- SHA-256 OAuth checksum reconnect & IST 06:00 AM daily boundary validation.
- Lazy-loaded KiteConnect SDK + KiteTicker WebSocket data-feed creation.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Sequence

from broker.base import (
    AccountBalance,
    BrokerBase,
    BrokerCapabilities,
    BrokerError,
    BrokerHealthSnapshot,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    OrderRequest,
    OrderStatus,
)
from broker.zerodha.auth import current_zerodha_session_bounds, zerodha_auth_manager
from broker.zerodha.feed import KiteTickerAdapter
from config import config

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _mask_client_id(value: str) -> str:
    clean = (value or "").strip()
    if len(clean) <= 4:
        return clean
    return f"{clean[:2]}{'•' * max(2, len(clean) - 4)}{clean[-2:]}"


def _normalize_kite_product(product: str) -> str:
    value = (product or "").strip().upper()
    if value in {"MIS", "INTRADAY", "DAY", "I"}:
        return "MIS"
    if value in {"CNC", "DELIVERY", "D"}:
        return "CNC"
    if value in {"NRML", "POSITIONAL", "POS"}:
        return "NRML"
    raise ValueError(f"Unsupported Kite product value: {product!r}")


class ZerodhaBroker(BrokerBase):
    """Active Phase 2 Zerodha Kite Connect v3 broker & market-data adapter."""

    name = "zerodha"
    display_name = "Zerodha Kite"
    stage = "active"
    capabilities = BrokerCapabilities(
        supports_trading=True,
        supports_market_data=True,
        supports_websocket=True,
        requires_oauth=True,
    )

    def __init__(
        self,
        product: str = "MIS",
        access_token: Optional[str] = None,
        initial_balance: Optional[float] = None,
    ) -> None:
        self.product = _normalize_kite_product(product)
        self.access_token = access_token
        self._connected = False
        self._kite: Any = None
        starting_cash = (
            float(initial_balance)
            if initial_balance is not None
            else float(config.TOTAL_CAPITAL)
        )
        self._balance = AccountBalance(cash=starting_cash)
        self._orders: List[BrokerOrder] = []
        self._positions: Dict[str, BrokerPosition] = {}

    def _probe_profile(self, token: str, timeout: float = 3.5) -> tuple[bool, int, Dict[str, Any], str]:
        """Verify Kite token against /user/profile with strict timeout."""
        api_key = zerodha_auth_manager.api_key
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{token}",
            "Accept": "application/json",
        }
        url = config.ZERODHA_PROFILE_URL
        if requests is not None:
            try:
                resp = requests.get(url, headers=headers, timeout=timeout)
                data: Dict[str, Any] = {}
                try:
                    data = resp.json().get("data", {}) if resp.content else {}
                except Exception:
                    data = {}
                if resp.status_code == 200:
                    return True, 200, data, ""
                return False, resp.status_code, data, f"HTTP {resp.status_code}"
            except requests.exceptions.Timeout:
                return False, 0, {}, "Profile probe timed out"
            except Exception as exc:
                return False, 0, {}, f"Network error: {exc.__class__.__name__}"

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return True, resp.status, payload.get("data", {}), ""
        except urllib.error.HTTPError as exc:
            return False, exc.code, {}, f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, 0, {}, f"Network error: {exc.__class__.__name__}"

    def health_check(self, timeout: float = 3.5) -> BrokerHealthSnapshot:
        """Probe Zerodha Kite live reachability without downloading instruments."""
        has_creds = zerodha_auth_manager.has_credentials()
        _, next_expiry = current_zerodha_session_bounds()
        expiry_iso = next_expiry.isoformat(timespec="seconds")

        token = self.access_token
        if not token:
            try:
                token = zerodha_auth_manager.get_valid_token()
            except RuntimeError:
                token = None

        if not token:
            self._connected = False
            if not has_creds:
                return BrokerHealthSnapshot(
                    broker_id=self.name,
                    display_name=self.display_name,
                    stage=self.stage,
                    state=ConnectionState.UNCONFIGURED,
                    capabilities=self.capabilities,
                    token_expires_at_ist=expiry_iso,
                    message="API key & secret required before Zerodha Kite OAuth login.",
                    can_reconnect=True,
                    has_credentials=False,
                    auth_mode="kite_oauth",
                )
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.DISCONNECTED,
                capabilities=self.capabilities,
                token_expires_at_ist=expiry_iso,
                message="No active Kite session for today (06:00 IST boundary). Reconnect required.",
                can_reconnect=True,
                has_credentials=True,
                auth_mode="kite_oauth",
            )

        started = time.perf_counter()
        ok, status_code, profile, err_msg = self._probe_profile(token, timeout=timeout)
        latency_ms = (time.perf_counter() - started) * 1000.0

        if ok:
            self._connected = True
            self.access_token = token
            token_info = zerodha_auth_manager.get_token_info()
            user_name = str(profile.get("user_name") or token_info.get("user_name") or "Kite Trader")
            user_id = str(profile.get("user_id") or token_info.get("user_id") or "")
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.CONNECTED,
                capabilities=self.capabilities,
                latency_ms=latency_ms,
                account_name=user_name,
                client_id=_mask_client_id(user_id) if user_id else "Verified",
                token_expires_at_ist=token_info.get("expires_at_ist") or expiry_iso,
                message="Live session verified against Zerodha Kite profile API.",
                can_reconnect=False,
                has_credentials=has_creds or True,
                auth_mode="kite_oauth",
            )

        self._connected = False
        if status_code in (401, 403):
            zerodha_auth_manager.invalidate_local()
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.EXPIRED,
                capabilities=self.capabilities,
                latency_ms=latency_ms,
                token_expires_at_ist=expiry_iso,
                message="Kite session expired or revoked (HTTP 401/403). Reconnect required.",
                can_reconnect=True,
                has_credentials=has_creds,
                auth_mode="kite_oauth",
            )

        return BrokerHealthSnapshot(
            broker_id=self.name,
            display_name=self.display_name,
            stage=self.stage,
            state=ConnectionState.DISCONNECTED,
            capabilities=self.capabilities,
            latency_ms=latency_ms,
            token_expires_at_ist=expiry_iso,
            message=f"Unable to verify Zerodha Kite session ({err_msg}).",
            can_reconnect=True,
            has_credentials=has_creds,
            auth_mode="kite_oauth",
        )

    def get_login_url(self, redirect_uri: Optional[str] = None) -> str:
        return zerodha_auth_manager.get_login_url(redirect_uri=redirect_uri)

    def complete_auth(self, code_or_url: str, redirect_uri: Optional[str] = None) -> BrokerHealthSnapshot:
        token_data = zerodha_auth_manager.generate_token_from_url(code_or_url, redirect_uri=redirect_uri)
        self.access_token = token_data.get("access_token")
        return self.health_check()

    def connect(self) -> bool:
        snapshot = self.health_check()
        if not snapshot.is_connected:
            return False

        try:
            kc = importlib.import_module("kiteconnect")
            kite_cls = getattr(kc, "KiteConnect", None)
            if kite_cls is not None:
                kite = kite_cls(api_key=zerodha_auth_manager.api_key)
                kite.set_access_token(self.access_token)
                self._kite = kite
        except Exception:
            # REST probes/auth still operate even when optional kiteconnect SDK is absent
            self._kite = None
        return True

    def disconnect(self) -> bool:
        self._connected = False
        self.access_token = None
        self._kite = None
        return zerodha_auth_manager.logout()

    def create_ticker_feed(self) -> KiteTickerAdapter:
        token = self.access_token or zerodha_auth_manager.get_valid_token()
        return KiteTickerAdapter(api_key=zerodha_auth_manager.api_key, access_token=token)

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        if not self._connected and not self.connect():
            return BrokerOrder(
                order_id=order.client_order_id or uuid.uuid4().hex[:16],
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="Zerodha Kite is not connected",
            )
        if self._kite is None:
            return BrokerOrder(
                order_id=order.client_order_id or uuid.uuid4().hex[:16],
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="kiteconnect SDK is not installed",
            )
        try:
            oid = str(
                self._kite.place_order(
                    variety=self._kite.VARIETY_REGULAR,
                    exchange=order.metadata.get("exchange", "NSE"),
                    tradingsymbol=order.symbol,
                    transaction_type="BUY" if order.side in {"BUY", "SHORT"} else "SELL",
                    quantity=order.quantity,
                    product=self.product,
                    order_type=order.order_type,
                    price=float(order.price) if order.price is not None else None,
                    trigger_price=float(order.trigger_price) if order.trigger_price is not None else None,
                    validity=order.validity,
                    tag=(order.tag or "")[:20] or None,
                )
            )
            placed = BrokerOrder(order_id=oid, request=order, status=OrderStatus.PENDING)
            self._orders.append(placed)
            return placed
        except Exception as exc:
            return BrokerOrder(
                order_id=order.client_order_id,
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason=str(exc),
            )

    def cancel_order(self, order_id: str) -> bool:
        if not order_id or self._kite is None:
            return False
        try:
            self._kite.cancel_order(variety=self._kite.VARIETY_REGULAR, order_id=order_id)
            return True
        except Exception:
            return False

    def get_positions(self) -> Sequence[BrokerPosition]:
        return list(self._positions.values())

    def get_balance(self) -> AccountBalance:
        return self._balance

    def get_orders(self) -> Sequence[BrokerOrder]:
        return list(self._orders)


__all__ = ["ZerodhaBroker"]
