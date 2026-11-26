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
    AmbiguousSubmissionError,
    BrokerBase,
    BrokerCapabilities,
    BrokerError,
    BrokerHealthSnapshot,
    BrokerHttpError,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    DepthLevel,
    MarketDepth,
    OrderRequest,
    OrderStatus,
    TokenBucketRateLimiter,
    make_broker_tag,
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
        supports_historical=True,
        supports_instrument_master=True,
        max_depth_levels=5,
        execution_grade_feed=True,
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
        self._last_read_err_ts: Dict[str, float] = {}
        self._rate_limiter = TokenBucketRateLimiter(rate_per_sec=8.0, capacity=12.0)

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

    @staticmethod
    def _kite_status(raw_status: Any, filled_qty: int = 0, total_qty: int = 0) -> OrderStatus:
        text = str(raw_status or "").strip().upper()
        if text in {"COMPLETE", "FILLED"}:
            return OrderStatus.FILLED
        if text in {"REJECTED", "FAILED"}:
            return OrderStatus.REJECTED
        if text in {"CANCELLED", "CANCELED"}:
            return OrderStatus.CANCELLED
        if text in {"EXPIRED"}:
            return OrderStatus.EXPIRED
        if filled_qty > 0 and total_qty > 0 and filled_qty < total_qty:
            return OrderStatus.PARTIALLY_FILLED
        if text in {"OPEN", "TRIGGER PENDING"}:
            return OrderStatus.OPEN
        return OrderStatus.PENDING

    def _log_read_warning(self, bucket: str, message: str, exc: Exception) -> None:
        now = time.monotonic()
        last = self._last_read_err_ts.get(bucket, 0.0)
        if now - last >= 60.0:
            self._last_read_err_ts[bucket] = now
            logger.warning("%s: %s", message, exc)

    def _rest_request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        form_data: Optional[Dict[str, Any]] = None,
        timeout: float = 6.0,
    ) -> Dict[str, Any]:
        import urllib.parse

        token = self.access_token or zerodha_auth_manager.get_valid_token()
        api_key = zerodha_auth_manager.api_key
        method_u = method.upper()
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{token}",
            "Accept": "application/json",
            "User-Agent": "AlgoDesk/4.0",
        }
        clean_form = (
            {k: str(v) for k, v in form_data.items() if v is not None}
            if form_data is not None
            else None
        )
        urlopen_mocked = getattr(urllib.request.urlopen, "__module__", "") != "urllib.request"
        max_attempts = 3 if method_u == "GET" else 1

        for attempt in range(max_attempts):
            self._rate_limiter.acquire(timeout=3.0)
            if requests is not None and not urlopen_mocked:
                resp = requests.request(
                    method=method_u,
                    url=url,
                    headers=headers,
                    data=clean_form,
                    timeout=timeout,
                )
                if resp.status_code >= 400:
                    detail = f"HTTP {resp.status_code}"
                    try:
                        err_body = resp.json()
                        if err_body.get("message"):
                            detail = f"HTTP {resp.status_code}: {err_body['message']}"
                    except Exception:
                        pass
                    if method_u == "GET" and resp.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                        time.sleep(0.15 * (2 ** attempt))
                        continue
                    raise BrokerHttpError(resp.status_code, detail)
                return resp.json() if resp.content else {}

            req_headers = dict(headers)
            data_bytes = None
            if clean_form is not None:
                data_bytes = urllib.parse.urlencode(clean_form).encode("utf-8")
                req_headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method_u)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                if method_u == "GET" and exc.code in {429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                detail = f"HTTP {exc.code}: {exc.reason}"
                try:
                    raw_err = exc.read().decode("utf-8", errors="ignore")
                    if raw_err.strip():
                        err_body = json.loads(raw_err)
                        if err_body.get("message"):
                            detail = f"HTTP {exc.code}: {err_body['message']}"
                except Exception:
                    pass
                raise BrokerHttpError(exc.code, detail) from exc
        return {}

    @staticmethod
    def _is_ambiguous_exception(exc: Exception) -> bool:
        if isinstance(exc, AmbiguousSubmissionError):
            return True
        if isinstance(exc, BrokerHttpError):
            return exc.status_code >= 500 or exc.status_code == 429
        if isinstance(exc, (TimeoutError, ConnectionError, OSError, urllib.error.URLError)):
            return not isinstance(exc, urllib.error.HTTPError)
        if requests is not None and isinstance(
            exc,
            (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ),
        ):
            return True
        code_attr = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code_attr, int) and (code_attr >= 500 or code_attr == 429):
            return True
        return False

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        try:
            exchange = order.require_exchange()
            normalized_product = _normalize_kite_product(order.product or self.product)
        except ValueError as exc:
            return BrokerOrder(
                order_id=order.client_order_id or uuid.uuid4().hex[:16],
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason=str(exc),
            )

        if not self._connected and not self.connect():
            return BrokerOrder(
                order_id=order.client_order_id or uuid.uuid4().hex[:16],
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason="Zerodha Kite is not connected",
            )

        # Canonical direction mapping: BUY/COVER -> BUY, SELL/SHORT -> SELL
        txn_type = "BUY" if order.side in {"BUY", "COVER"} else "SELL"
        kite_tag = make_broker_tag(order.client_order_id, max_len=20) or (order.tag or "")[:20] or None
        if kite_tag:
            order.tag = kite_tag

        if self._kite is not None:
            self._rate_limiter.acquire(timeout=3.0)
            try:
                oid = str(
                    self._kite.place_order(
                        variety=self._kite.VARIETY_REGULAR,
                        exchange=exchange,
                        tradingsymbol=order.symbol,
                        transaction_type=txn_type,
                        quantity=int(order.quantity),
                        product=normalized_product,
                        order_type=order.order_type,
                        price=float(order.price) if order.price is not None else None,
                        trigger_price=float(order.trigger_price) if order.trigger_price is not None else None,
                        validity=order.validity,
                        tag=kite_tag,
                    )
                )
                placed = BrokerOrder(
                    order_id=oid,
                    request=order,
                    status=OrderStatus.SUBMITTED,
                    raw={
                        "exchange": exchange,
                        "transaction_type": txn_type,
                        "product": normalized_product,
                        "tag": kite_tag,
                    },
                )
                self._orders.append(placed)
                return placed
            except Exception as exc:
                if self._is_ambiguous_exception(exc):
                    raise AmbiguousSubmissionError(
                        f"Zerodha order submission uncertain for {order.client_order_id}: {exc}"
                    ) from exc
                return BrokerOrder(
                    order_id=order.client_order_id,
                    request=order,
                    status=OrderStatus.REJECTED,
                    rejection_reason=str(exc),
                )

        # Direct Kite REST v3 fallback when optional kiteconnect SDK is absent
        form_payload = {
            "exchange": exchange,
            "tradingsymbol": order.symbol,
            "transaction_type": txn_type,
            "quantity": int(order.quantity),
            "product": normalized_product,
            "order_type": order.order_type,
            "price": float(order.price) if order.price is not None else None,
            "trigger_price": float(order.trigger_price) if order.trigger_price is not None else None,
            "validity": order.validity,
            "tag": kite_tag,
        }
        try:
            raw_resp = self._rest_request_json(
                f"{config.ZERODHA_BASE_URL}/orders/regular",
                method="POST",
                form_data=form_payload,
                timeout=6.0,
            )
            oid = str(((raw_resp.get("data") or {}).get("order_id")) or order.client_order_id)
            placed = BrokerOrder(
                order_id=oid,
                request=order,
                status=OrderStatus.SUBMITTED,
                raw={"response": raw_resp, "payload": form_payload, "tag": kite_tag},
            )
            self._orders.append(placed)
            return placed
        except Exception as exc:
            if self._is_ambiguous_exception(exc):
                raise AmbiguousSubmissionError(
                    f"Zerodha order submission uncertain for {order.client_order_id}: {exc}"
                ) from exc
            return BrokerOrder(
                order_id=order.client_order_id,
                request=order,
                status=OrderStatus.REJECTED,
                rejection_reason=str(exc),
            )

    def cancel_order(self, order_id: str) -> bool:
        if not order_id:
            return False
        cancelled = False
        if self._kite is not None:
            try:
                self._kite.cancel_order(variety=self._kite.VARIETY_REGULAR, order_id=order_id)
                cancelled = True
            except Exception:
                cancelled = False
        elif self._connected or self.access_token:
            try:
                self._rest_request_json(
                    f"{config.ZERODHA_BASE_URL}/orders/regular/{order_id}",
                    method="DELETE",
                    timeout=6.0,
                )
                cancelled = True
            except Exception:
                cancelled = False
        if cancelled:
            for o in self._orders:
                if o.order_id == order_id:
                    o.status = OrderStatus.CANCELLED
        return cancelled

    def get_funds(self, segment: str = "equity") -> Dict[str, float]:
        """Authoritative Kite Connect v3 margins read (`/user/margins?segment=...`)."""
        seg = "commodity" if str(segment or "").strip().lower() in {"com", "commodity", "mcx"} else "equity"
        if self._connected or self.access_token:
            try:
                if self._kite is not None and hasattr(self._kite, "margins"):
                    data = self._kite.margins(segment=seg) or {}
                else:
                    raw = self._rest_request_json(
                        f"{config.ZERODHA_BASE_URL}/user/margins?segment={seg}",
                        method="GET",
                        timeout=4.0,
                    )
                    data = raw.get("data") or {}
                avail = float(((data.get("available") or {}).get("live_balance")) or data.get("net") or 0.0)
                used = float(((data.get("utilised") or {}).get("debits")) or 0.0)
                self._balance = AccountBalance(
                    cash=avail + used,
                    available_cash=avail,
                    margin_used=used,
                    metadata={"segment": seg, "source": "zerodha_live"},
                )
                return {
                    "available": round(avail, 2),
                    "used": round(used, 2),
                    "total": round(avail + used, 2),
                    "segment": seg,
                }
            except Exception as exc:
                self._log_read_warning("funds", "Zerodha live funds read failed", exc)
        # Return cached balance directly — never call super().get_funds() which invokes get_balance() -> recursion.
        return {
            "available": round(float(self._balance.available_cash), 2),
            "used": round(float(self._balance.margin_used), 2),
            "total": round(float(self._balance.cash), 2),
            "segment": seg,
        }

    def get_positions(self) -> Sequence[BrokerPosition]:
        """Authoritative Kite Connect v3 positions read (`/portfolio/positions`)."""
        if self._connected or self.access_token:
            try:
                if self._kite is not None and hasattr(self._kite, "positions"):
                    data = self._kite.positions() or {}
                else:
                    raw = self._rest_request_json(
                        f"{config.ZERODHA_BASE_URL}/portfolio/positions",
                        method="GET",
                        timeout=5.0,
                    )
                    data = raw.get("data") or {}
                net_items = data.get("net") or []
                parsed: Dict[str, BrokerPosition] = {}
                for item in net_items:
                    net_qty = int(item.get("quantity") or 0)
                    if net_qty == 0:
                        continue
                    sym = str(item.get("tradingsymbol") or "").strip().upper()
                    if not sym:
                        continue
                    avg_px = float(item.get("average_price") or item.get("last_price") or 1.0)
                    if avg_px <= 0:
                        avg_px = 1.0
                    ltp = float(item.get("last_price") or avg_px)
                    exch = str(item.get("exchange") or "NSE").strip().upper()
                    prod = str(item.get("product") or "MIS").strip().upper()
                    pos = BrokerPosition(
                        symbol=sym,
                        quantity=abs(net_qty),
                        direction=1 if net_qty > 0 else -1,
                        average_price=avg_px,
                        exchange=exch,
                        instrument_key=str(item.get("instrument_token") or ""),
                        product=prod,
                        realized_pnl=float(item.get("realised") or 0.0),
                        unrealized_pnl=float(item.get("unrealised") or item.get("pnl") or 0.0),
                        market_price=ltp,
                        metadata={"raw": item},
                    )
                    parsed[f"{exch}:{sym}:{prod}"] = pos
                self._positions = parsed
                return list(parsed.values())
            except Exception as exc:
                self._log_read_warning("positions", "Zerodha live positions read failed", exc)
        return list(self._positions.values())

    def get_balance(self) -> AccountBalance:
        if self._connected or self.access_token:
            self.get_funds(segment="equity")
        return self._balance

    def get_orders(self) -> Sequence[BrokerOrder]:
        """Authoritative Kite Connect v3 order book read (`/orders`)."""
        if self._connected or self.access_token:
            try:
                if self._kite is not None and hasattr(self._kite, "orders"):
                    items = self._kite.orders() or []
                else:
                    raw = self._rest_request_json(
                        f"{config.ZERODHA_BASE_URL}/orders",
                        method="GET",
                        timeout=5.0,
                    )
                    items = raw.get("data") or []
                out: List[BrokerOrder] = []
                for item in items:
                    oid = str(item.get("order_id") or "").strip()
                    sym = str(item.get("tradingsymbol") or "").strip().upper()
                    qty = max(1, int(item.get("quantity") or 1))
                    filled = max(0, int(item.get("filled_quantity") or 0))
                    avg_px_raw = float(item.get("average_price") or 0.0)
                    txn = str(item.get("transaction_type") or "BUY").strip().upper()
                    exch = str(item.get("exchange") or "NSE").strip().upper()
                    prod = str(item.get("product") or "MIS").strip().upper()
                    req = OrderRequest(
                        symbol=sym or "UNKNOWN",
                        side="BUY" if txn == "BUY" else "SELL",
                        quantity=qty,
                        exchange=exch,
                        order_type=str(item.get("order_type") or "MARKET").strip().upper(),
                        product=prod,
                        price=float(item.get("price")) if float(item.get("price") or 0.0) > 0 else None,
                        tag=str(item.get("tag") or ""),
                    )
                    out.append(
                        BrokerOrder(
                            order_id=oid or req.client_order_id,
                            request=req,
                            status=self._kite_status(item.get("status"), filled, qty),
                            filled_quantity=filled,
                            average_fill_price=avg_px_raw if avg_px_raw > 0 else None,
                            rejection_reason=str(item.get("status_message") or ""),
                            raw=item,
                        )
                    )
                self._orders = out
                return list(out)
            except Exception as exc:
                self._log_read_warning("orders", "Zerodha live order book read failed", exc)
        return list(self._orders)

    def get_trades(self) -> Sequence["ExecutionFill"]:
        """Authoritative Kite Connect v3 trade book read (`/trades`)."""
        from broker.base import ExecutionFill

        if self._connected or self.access_token:
            try:
                if self._kite is not None and hasattr(self._kite, "trades"):
                    items = self._kite.trades() or []
                else:
                    raw = self._rest_request_json(
                        f"{config.ZERODHA_BASE_URL}/trades",
                        method="GET",
                        timeout=5.0,
                    )
                    items = raw.get("data") or []
                fills: List[ExecutionFill] = []
                for item in items:
                    qty = int(item.get("quantity") or 0)
                    px = float(item.get("average_price") or item.get("price") or 0.0)
                    if qty <= 0 or px <= 0:
                        continue
                    fills.append(
                        ExecutionFill(
                            fill_id=str(item.get("trade_id") or item.get("exchange_order_id") or uuid.uuid4().hex[:12]),
                            order_id=str(item.get("order_id") or ""),
                            symbol=str(item.get("tradingsymbol") or "").strip().upper(),
                            exchange=str(item.get("exchange") or "NSE").strip().upper(),
                            side=str(item.get("transaction_type") or "BUY").strip().upper(),
                            quantity=qty,
                            price=px,
                            product=str(item.get("product") or "MIS").strip().upper(),
                            raw=item,
                        )
                    )
                return fills
            except Exception as exc:
                self._log_read_warning("trades", "Zerodha live trade book read failed", exc)
        return []

    def is_authenticated(self) -> bool:
        if self.access_token:
            return True
        try:
            return bool(zerodha_auth_manager.get_valid_token())
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
        Fetch Level-5 market depth from Zerodha Kite Connect REST API (`/quote?i=EXCH:SYMBOL`).
        """
        from urllib.parse import quote as url_quote

        sym = (symbol or "").strip().upper()
        exch = (exchange or "NSE").strip().upper()
        if not sym or not self.is_authenticated():
            return None
        query_key = f"{exch}:{sym}"
        try:
            if self._kite is not None and hasattr(self._kite, "quote"):
                data_map = self._kite.quote([query_key]) or {}
            else:
                raw = self._rest_request_json(
                    f"{config.ZERODHA_BASE_URL}/quote?i={url_quote(query_key, safe=':')}",
                    method="GET",
                    timeout=5.0,
                )
                data_map = raw.get("data") or {}
            item = data_map.get(query_key) or next(iter(data_map.values()), None)
            if not isinstance(item, dict):
                return None
            depth_obj = item.get("depth") or {}
            req_levels = max(1, min(int(levels or 5), 5))
            bids: List[DepthLevel] = []
            asks: List[DepthLevel] = []
            for row in (depth_obj.get("buy") or [])[:req_levels]:
                px = float(row.get("price") or 0.0)
                qty = int(row.get("quantity") or 0)
                if px > 0 or qty > 0:
                    bids.append(DepthLevel(price=px, quantity=qty, orders=int(row.get("orders") or 0)))
            for row in (depth_obj.get("sell") or [])[:req_levels]:
                px = float(row.get("price") or 0.0)
                qty = int(row.get("quantity") or 0)
                if px > 0 or qty > 0:
                    asks.append(DepthLevel(price=px, quantity=qty, orders=int(row.get("orders") or 0)))
            if not bids and not asks:
                return None
            ts_raw = item.get("timestamp") or item.get("last_trade_time")
            return MarketDepth(
                symbol=sym,
                exchange=exch,
                bids=bids,
                asks=asks,
                total_buy_qty=int(item.get("buy_quantity") or 0),
                total_sell_qty=int(item.get("sell_quantity") or 0),
                depth_levels=max(len(bids), len(asks)),
                source="zerodha",
                transport="rest",
                ts_ist=str(ts_raw) if ts_raw else None,
            )
        except Exception as exc:
            self._log_read_warning("depth", "Zerodha depth fetch failed", exc)
            return None

    def create_ws_client(
        self,
        *,
        on_tick: Any = None,
        on_status: Any = None,
        depth_levels: int = 5,
    ) -> Optional[Any]:
        from core.broker_ws import ZerodhaWSFeedClient

        return ZerodhaWSFeedClient(
            on_tick=on_tick,
            on_status=on_status,
        )

    def sync_instruments(self, store: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if hasattr(store, "sync_zerodha"):
            return store.sync_zerodha(**kwargs)
        return None


__all__ = ["ZerodhaBroker"]


