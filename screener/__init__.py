"""
screener package
================
Modular, hot-reloadable equity & derivative market screening engine.
"""

from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch
from screener.engine import ScreenerEngine, compute_auto_lookback_days, screener_engine
from screener.registry import ScreenRegistry, screen_registry
from screener.universes import DEFAULT_WATCHLIST, UniverseResolver, universe_resolver

__all__ = [
    "BaseScreen",
    "ColumnSpec",
    "DEFAULT_WATCHLIST",
    "ParamSpec",
    "ScreenMatch",
    "ScreenRegistry",
    "ScreenerEngine",
    "UniverseResolver",
    "compute_auto_lookback_days",
    "screen_registry",
    "screener_engine",
    "universe_resolver",
]
