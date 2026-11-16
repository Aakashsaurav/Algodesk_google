"""
broker/data_sources.py
======================
Lightweight modular adapters for non-executing market data sources
(such as Yahoo Finance historical fallback) so they participate cleanly in
the Data Connection selector alongside full brokers.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Optional, Sequence

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
)


class YFinanceDataAdapter(BrokerBase):
    """
    Lightweight market-data-only connection adapter for Yahoo Finance.

    Does NOT import heavy pandas/yfinance packages during connectivity checks,
    preserving MacBook Air M2 memory until historical data is actually fetched.
    """

    name = "yfinance"
    display_name = "Yahoo Finance"
    stage = "active"
    capabilities = BrokerCapabilities(
        supports_trading=False,
        supports_market_data=True,
        supports_websocket=False,
        requires_oauth=False,
    )

    _PROBE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=1d&interval=1d"

    def __init__(self) -> None:
        self._last_connected = False

    def connect(self) -> bool:
        snap = self.health_check()
        self._last_connected = snap.is_connected
        return self._last_connected

    def disconnect(self) -> bool:
        self._last_connected = False
        return True

    def health_check(self, timeout: float = 3.5) -> BrokerHealthSnapshot:
        started = time.perf_counter()
        req = urllib.request.Request(
            self._PROBE_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if resp.status == 200:
                    payload = json.loads(resp.read(4096).decode("utf-8", errors="ignore"))
                    has_chart = bool(payload.get("chart"))
                    if has_chart:
                        self._last_connected = True
                        return BrokerHealthSnapshot(
                            broker_id=self.name,
                            display_name=self.display_name,
                            stage=self.stage,
                            state=ConnectionState.CONNECTED,
                            capabilities=self.capabilities,
                            latency_ms=elapsed_ms,
                            account_name="Public NSE/BSE EOD & Delayed",
                            client_id="No Auth Required",
                            token_expires_at_ist="No Daily Expiry",
                            message="Reachable for historical & daily OHLCV fallback.",
                            can_reconnect=False,
                            has_credentials=True,
                            auth_mode="public",
                            roadmap_note="Fallback historical provider when broker feed is idle.",
                        )
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.DISCONNECTED,
                capabilities=self.capabilities,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                message="Unexpected response from Yahoo Finance endpoint.",
                can_reconnect=True,
                has_credentials=True,
                auth_mode="public",
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return BrokerHealthSnapshot(
                broker_id=self.name,
                display_name=self.display_name,
                stage=self.stage,
                state=ConnectionState.DISCONNECTED,
                capabilities=self.capabilities,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                message=f"Feed unreachable ({exc.__class__.__name__}). Retry check.",
                can_reconnect=True,
                has_credentials=True,
                auth_mode="public",
            )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        raise BrokerError("Yahoo Finance is a read-only data provider; trading is not supported.")

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_positions(self) -> Sequence[BrokerPosition]:
        return []

    def get_balance(self) -> AccountBalance:
        return AccountBalance(cash=0.0)

    def get_orders(self) -> Sequence[BrokerOrder]:
        return []


__all__ = ["YFinanceDataAdapter"]
