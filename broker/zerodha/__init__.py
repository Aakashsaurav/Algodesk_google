"""Zerodha Kite Connect broker module (Phase 2 active integration)."""

from broker.zerodha.auth import ZerodhaAuthManager, current_zerodha_session_bounds, zerodha_auth_manager
from broker.zerodha.broker import ZerodhaBroker
from broker.zerodha.feed import KiteTickerAdapter

__all__ = [
    "KiteTickerAdapter",
    "ZerodhaAuthManager",
    "ZerodhaBroker",
    "current_zerodha_session_bounds",
    "zerodha_auth_manager",
]
