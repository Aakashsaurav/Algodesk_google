"""
broker/zerodha/broker.py
========================
Modular Zerodha Kite Connect v3 adapter slot for AlgoDesk.

Phase 1 Status:
    Registered in BrokerRegistry with stage="planned" so the Broker Hub UI
    displays Zerodha's readiness slot and capability boundaries now, while
    keeping trading/data assignment disabled until Phase 2 implements
    KiteConnect OAuth checksum exchange and KiteTicker streaming.
"""

from __future__ import annotations

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


class ZerodhaBroker(BrokerBase):
    """Zerodha Kite Connect adapter (scheduled for Phase 2 activation)."""

    name = "zerodha"
    display_name = "Zerodha Kite"
    stage = "planned"
    capabilities = BrokerCapabilities(
        supports_trading=True,
        supports_market_data=True,
        supports_websocket=True,
        requires_oauth=True,
    )

    def connect(self) -> bool:
        return False

    def disconnect(self) -> bool:
        return True

    def health_check(self, timeout: float = 3.5) -> BrokerHealthSnapshot:
        return BrokerHealthSnapshot(
            broker_id=self.name,
            display_name=self.display_name,
            stage=self.stage,
            state=ConnectionState.PLANNED,
            capabilities=self.capabilities,
            latency_ms=None,
            account_name="Phase 2 Adapter Slot",
            client_id="Not Activated",
            token_expires_at_ist="06:00 IST Daily (Kite Spec)",
            message="Kite Connect v3 adapter scheduled for Phase 2 modular rollout.",
            can_reconnect=False,
            has_credentials=False,
            auth_mode="kite_oauth",
            roadmap_note="Phase 2: Kite OAuth SHA-256 checksum login, KiteTicker WS, SQLite instrument map.",
        )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        raise BrokerError("Zerodha Kite adapter is planned for Phase 2 and not yet active.")

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_positions(self) -> Sequence[BrokerPosition]:
        return []

    def get_balance(self) -> AccountBalance:
        return AccountBalance(cash=0.0)

    def get_orders(self) -> Sequence[BrokerOrder]:
        return []


__all__ = ["ZerodhaBroker"]
