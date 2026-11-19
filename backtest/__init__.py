"""
backtest package
================
Modular, O(N) strategy backtesting engine with hot-reloaded `.py` strategies
reusing `core.runtime.BaseStrategy` and `core.runtime.OrderRequest`.
"""

from backtest.base import BacktestStrategy
from backtest.engine import BacktestEngine, backtest_engine
from backtest.registry import StrategyRegistry, strategy_registry

__all__ = [
    "BacktestEngine",
    "BacktestStrategy",
    "StrategyRegistry",
    "backtest_engine",
    "strategy_registry",
]
