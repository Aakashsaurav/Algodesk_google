"""Canonical consolidated core modules for AlgoDesk (Phase 3)."""

from core.instruments import InstrumentIndex, InstrumentRecord, instrument_index
from core.runtime import (
    BaseFeedAdapter,
    BaseStrategy,
    CanonicalOrderManager,
    CanonicalSessionManager,
    LiveBotEngine,
    OrderRequest,
    TradingState,
)

__all__ = [
    "BaseFeedAdapter",
    "BaseStrategy",
    "CanonicalOrderManager",
    "CanonicalSessionManager",
    "InstrumentIndex",
    "InstrumentRecord",
    "LiveBotEngine",
    "OrderRequest",
    "TradingState",
    "instrument_index",
]
