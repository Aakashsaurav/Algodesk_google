"""
broker/upstox/broker.py
=======================
Refactored Upstox broker adapter implementing BrokerBase with live server-side
connectivity verification, zero eager 100k-instrument loading, and clean
OAuth reconnect hooks.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
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
from broker.upstox.auth import auth_manager, current_upstox_session_bounds
from config import config

try:
    import requests
except ImportError:  # pragma: no cover - optional dependency
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_product(product: str) -> str:
    value = (product or "").strip().upper()
    if value in {"I", "MIS", "INTRADAY", "DAY"}:
        return "I"
    if value in {"D", "CNC", "DELIVERY", "NRML", "POS", "POSITIONAL"}:
        return "D"
    raise ValueError(f"Unsupported product value: {product!r}")


def _status_from_value(value: Any) -> OrderStatus:
    if isinstance(value, OrderStatus):
        return value
    text = str(value or "").upper().strip()
    for status in OrderStatus:
        if status.value == text:
            return status
    return OrderStatus.PENDING


def _mask_client_id(value: str) -> str:
    clean = (value or "").strip()
    if len(clean) <= 4:
        return clean
    return f"{clean[:2]}{'•' * max(2, len(clean) - 4)}{clean[-2:]}"


class UpstoxBroker(BrokerBase):
    """Concrete Upstox broker adapter with live profile verification."""

    name = "upstox"
    display_name = "Upstox"
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
        self.product = _normalize_product(product)
        self.access_token = access_token
        self._sdk = None
        self._connected = False

        starting_cash = (
            float(initial_balance)
            if initial_balance is not None
            else float(config.TOTAL_CAPITAL)
        )
        self._balance = AccountBalance(cash=starting_cash)
        self._orders: List[BrokerOrder] = []
        self._positions: Dict[str, BrokerPosition] = {}

        self._api_client = None
        self._order_api = None
        self._portfolio_api = None
        self._funds_api = None

    def _probe_profile(self, token: str, timeout: float = 3.5) -> tuple[bool, int, Dict[str, Any], str]:
        """Verify token against Upstox /v2/user/profile with strict timeout."""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        url = config.UPSTOX_PROFILE_URL
        if requests is not None:
            try:
                resp = requests.get(url, headers=headers, timeout=timeout)
                data = {}
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
        """
        Perform real live reachability & token validity verification.
        Never touches the 100k-instrument list, keeping memory footprint minimal.
        """
        has_creds = auth_manager.has_credentials()
        _, next_expiry = current_upstox_session_bounds()
        expiry_iso = next_expiry.isoformat(timespec="seconds")

        token = self.access_token
        if not token:
            try:
                token = auth_manager.get_valid_token()
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
                    message="API key & secret required before Upstox OAuth login.",
                    can_reconnect=True,
                    has_credentials=False,
                    auth_mode="oauth2",
                )
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.DISCONNECTED,
                capabilities=self.capabilities,
                token_expires_at_ist=expiry_iso,
                message="No active Upstox token for current trading session. Reconnect required.",
                can_reconnect=True,
                has_credentials=True,
                auth_mode="oauth2",
            )

        started = time.perf_counter()
        ok, status_code, profile, err_msg = self._probe_profile(token, timeout=timeout)
        latency_ms = (time.perf_counter() - started) * 1000.0

        if ok:
            self._connected = True
            self.access_token = token
            token_info = auth_manager.get_token_info()
            user_name = str(profile.get("user_name") or token_info.get("user_name") or "Authenticated User")
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
                message="Live session verified against Upstox profile API.",
                can_reconnect=False,
                has_credentials=has_creds or True,
                auth_mode="oauth2",
            )

        self._connected = False
        if status_code in (401, 403):
            auth_manager.invalidate_local()
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.EXPIRED,
                capabilities=self.capabilities,
                latency_ms=latency_ms,
                token_expires_at_ist=expiry_iso,
                message="Session token expired or revoked by Upstox (HTTP 401). Reconnect required.",
                can_reconnect=True,
                has_credentials=has_creds,
                auth_mode="oauth2",
            )

        return BrokerHealthSnapshot(
            broker_id=self.name,
            display_name=self.display_name,
            stage=self.stage,
            state=ConnectionState.DISCONNECTED,
            capabilities=self.capabilities,
            latency_ms=latency_ms,
            token_expires_at_ist=expiry_iso,
            message=f"Unable to verify Upstox session ({err_msg}).",
            can_reconnect=True,
            has_credentials=has_creds,
            auth_mode="oauth2",
        )

    def get_login_url(self, redirect_uri: Optional[str] = None) -> str:
        return auth_manager.get_login_url(redirect_uri=redirect_uri)

    def complete_auth(self, code_or_url: str, redirect_uri: Optional[str] = None) -> BrokerHealthSnapshot:
        token_data = auth_manager.generate_token_from_url(code_or_url, redirect_uri=redirect_uri)
        self.access_token = token_data.get("access_token")
        return self.health_check()

    def connect(self) -> bool:
        """
        Verify live session connectivity and lazily bind Upstox SDK clients
        if upstox_client is installed.
        """
        snapshot = self.health_check()
        if not snapshot.is_connected:
            return False

        sdk = self._load_sdk()
        if sdk is None:
            # Session is live-verified via REST even if upstox_client SDK is absent
            return True

        try:
            configuration = sdk.Configuration()
            configuration.access_token = self.access_token
            self._api_client = sdk.ApiClient(configuration)
            self._order_api = self._build_api(sdk, ("OrderApiV3", "OrderApi"), self._api_client)
            self._portfolio_api = self._build_api(sdk, ("PortfolioApiV2", "PortfolioApi"), self._api_client)
            self._funds_api = self._build_api(sdk, ("FundApi", "FundsApi", "UserApi"), self._api_client)
            self._sdk = sdk
        except Exception as exc:
            logger.warning("SDK client initialization warning: %s", exc)
        return True

    def disconnect(self) -> bool:
        self._connected = False
        self.access_token = None
        self._api_client = None
        self._order_api = None
        self._portfolio_api = None
        self._funds_api = None
        return auth_manager.logout()

    def _load_sdk(self):
        try:
            return importlib.import_module("upstox_client")
        except Exception:
            return None

    @staticmethod
    def _build_api(sdk: Any, class_names: Sequence[str], api_client: Any) -> Any:
        for name in class_names:
            api_cls = getattr(sdk, name, None)
            if api_cls is None:
                continue
            try:
                return api_cls(api_client)
            except Exception:
                pass
        return None

    # ── Execution Surface (preserved from Algodesk_R4) ──────────────────────

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        if not self._connected and not self.connect():
            return BrokerOrder(
                order_id=order.client_order_id or uuid.uuid4().hex[:16],
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="Upstox broker is not connected",
            )
        if self._sdk is None or self._order_api is None:
            return BrokerOrder(
                order_id=order.client_order_id or uuid.uuid4().hex[:16],
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="upstox_client SDK is not installed",
            )

        payload = {
            "quantity": order.quantity,
            "product": self.product,
            "validity": order.validity,
            "price": float(order.price or 0.0),
            "tag": order.tag or None,
            "slice": False,
            "instrument_token": order.instrument_key or order.symbol,
            "order_type": order.order_type,
            "transaction_type": "BUY" if order.side in {"BUY", "SHORT"} else "SELL",
            "disclosed_quantity": 0,
            "trigger_price": float(order.trigger_price or 0.0),
            "is_amo": False,
        }
        try:
            resp = self._order_api.place_order(payload)
            oid = str(_safe_get(resp, "order_id") or _safe_get(resp, "id") or order.client_order_id)
            broker_order = BrokerOrder(
                order_id=oid,
                request=order,
                status=_status_from_value(_safe_get(resp, "status")),
                raw=resp if isinstance(resp, dict) else {},
            )
            self._orders.append(broker_order)
            return broker_order
        except Exception as exc:
            return BrokerOrder(
                order_id=order.client_order_id,
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason=str(exc),
            )

    def cancel_order(self, order_id: str) -> bool:
        if not order_id:
            return False
        if self._connected and self._order_api is not None:
            try:
                self._order_api.cancel_order(order_id)
                return True
            except Exception:
                return False
        return False

    def get_positions(self) -> Sequence[BrokerPosition]:
        return list(self._positions.values())

    def get_balance(self) -> AccountBalance:
        return self._balance

    def get_orders(self) -> Sequence[BrokerOrder]:
        return list(self._orders)


__all__ = ["UpstoxBroker"]
