"""
screener package
================
Modular, hot-reloadable equity & derivative market screening engine,
NSE CSV universe synchronization module, and real-time Live Screener engine.
"""

from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch
from screener.engine import ScreenerEngine, compute_auto_lookback_days, screener_engine
from screener.live_engine import LiveScreenerEngine, live_screener_engine, merge_live_tick_into_candles
from screener.nse_universe_sync import (
    NSE_UNIVERSE_SOURCES,
    NseUniverseSource,
    NseUniverseSync,
    nse_universe_sync,
    parse_symbols_from_csv_text,
)
from screener.registry import ScreenRegistry, screen_registry
from screener.universes import DEFAULT_WATCHLIST, UniverseResolver, universe_resolver

__all__ = [
    "BaseScreen",
    "ColumnSpec",
    "DEFAULT_WATCHLIST",
    "LiveScreenerEngine",
    "NSE_UNIVERSE_SOURCES",
    "NseUniverseSource",
    "NseUniverseSync",
    "ParamSpec",
    "ScreenMatch",
    "ScreenRegistry",
    "ScreenerEngine",
    "UniverseResolver",
    "compute_auto_lookback_days",
    "live_screener_engine",
    "merge_live_tick_into_candles",
    "nse_universe_sync",
    "parse_symbols_from_csv_text",
    "screen_registry",
    "screener_engine",
    "universe_resolver",
]
