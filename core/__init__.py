"""Canonical consolidated core modules for AlgoDesk (Phase 3)."""

from core.historical import (
    HistoricalDataManager,
    VALID_TIMEFRAMES,
    compute_missing_date_ranges,
    historical_data_manager,
)
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
    "HistoricalDataManager",
    "InstrumentIndex",
    "InstrumentRecord",
    "LiveBotEngine",
    "OrderRequest",
    "TradingState",
    "VALID_TIMEFRAMES",
    "compute_missing_date_ranges",
    "historical_data_manager",
    "instrument_index",
]
