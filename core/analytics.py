"""
core/analytics.py
=================
Backward-compatibility facade re-exporting modular Phase 05 packages:
- `indicators.toolkit` (`IndicatorToolkit`, `available_backends`, pure-Python series)
- `screener` (`ScreenerEngine`, `ScreenRegistry`, `UniverseResolver`, `DEFAULT_WATCHLIST`)
- `backtest` (`BacktestEngine`, `StrategyRegistry`)
- `risk` (`RiskLimitManager`, `risk_limit_manager`)
- `core.jobs` (`AsyncJobRunner`, `JobCancelledError`, `analytics_job_runner`)
"""

from __future__ import annotations

from typing import Dict

from backtest.engine import BacktestEngine, backtest_engine
from backtest.registry import strategy_registry
from core.jobs import AsyncJobRunner, JobCancelledError, analytics_job_runner
from indicators.toolkit import (
    IndicatorToolkit,
    available_backends,
    compute_atr_series,
    compute_ema_series,
    compute_rsi_wilder_series,
    compute_supertrend_series,
)
from risk.limits import RiskLimitManager, risk_limit_manager
from screener.engine import ScreenerEngine, screener_engine
from screener.registry import screen_registry
from screener.universes import DEFAULT_WATCHLIST, universe_resolver

MarketScreener = ScreenerEngine
market_screener = screener_engine
DEFAULT_SCREENER_WATCHLIST = DEFAULT_WATCHLIST

SCREENER_PRESETS: Dict[str, str] = {
    s["id"]: s["name"] for s in screen_registry.list_screens()
}
BACKTEST_STRATEGIES: Dict[str, str] = strategy_registry.list_strategy_labels()

__all__ = [
    "AsyncJobRunner",
    "BACKTEST_STRATEGIES",
    "BacktestEngine",
    "DEFAULT_SCREENER_WATCHLIST",
    "IndicatorToolkit",
    "JobCancelledError",
    "MarketScreener",
    "RiskLimitManager",
    "SCREENER_PRESETS",
    "ScreenerEngine",
    "analytics_job_runner",
    "available_backends",
    "backtest_engine",
    "compute_atr_series",
    "compute_ema_series",
    "compute_rsi_wilder_series",
    "compute_supertrend_series",
    "market_screener",
    "risk_limit_manager",
    "screen_registry",
    "screener_engine",
    "strategy_registry",
    "universe_resolver",
]
