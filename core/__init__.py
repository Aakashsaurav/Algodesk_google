"""Canonical consolidated core modules for AlgoDesk (Phases 3–4)."""

from core.audit import AuditTrail, GENESIS_HASH, audit_trail
from core.feed_manager import FeedSlotHealth, HotStandbyFeedManager, feed_manager
from core.historical import (
    HistoricalDataManager,
    VALID_TIMEFRAMES,
    compute_missing_date_ranges,
    historical_data_manager,
)
from core.instruments import InstrumentIndex, InstrumentRecord, instrument_index
from core.preflight import (
    KillSwitchController,
    PreflightCheckItem,
    PreflightChecker,
    fetch_broker_margin_summary,
    kill_switch_controller,
)
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
    "AuditTrail",
    "BaseFeedAdapter",
    "BaseStrategy",
    "CanonicalOrderManager",
    "CanonicalSessionManager",
    "FeedSlotHealth",
    "GENESIS_HASH",
    "HistoricalDataManager",
    "HotStandbyFeedManager",
    "InstrumentIndex",
    "InstrumentRecord",
    "KillSwitchController",
    "LiveBotEngine",
    "OrderRequest",
    "PreflightCheckItem",
    "PreflightChecker",
    "TradingState",
    "VALID_TIMEFRAMES",
    "audit_trail",
    "compute_missing_date_ranges",
    "feed_manager",
    "fetch_broker_margin_summary",
    "historical_data_manager",
    "instrument_index",
    "kill_switch_controller",
]
