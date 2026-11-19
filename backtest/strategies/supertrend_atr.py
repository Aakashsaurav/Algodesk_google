"""
backtest/strategies/supertrend_atr.py
=====================================
Drop-in Strategy: Supertrend ATR Trend Follower.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class SupertrendAtrStrategy(BacktestStrategy):
    id = "supertrend_atr"
    name = "Supertrend (10, ATR 3.0x)"
    description = "Enters when Supertrend flips bullish (-1 -> +1); exits on flip bearish (+1 -> -1)."
    params = [
        ParamSpec("period", "ATR Period", "int", 10, min_val=3, max_val=50, step=1),
        ParamSpec("multiplier", "ATR Multiplier", "float", 3.0, min_val=1.0, max_val=8.0, step=0.5),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._dirs: List[int] = []

    def prepare(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> None:
        dirs, _stops = toolkit.supertrend(int(params["period"]), float(params["multiplier"]))
        self._dirs = dirs

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        if index <= 0 or not self._dirs:
            return False, False
        cur = self._dirs[index]
        prev = self._dirs[index - 1]
        if cur == 0 or prev == 0:
            return False, False
        return (prev == -1 and cur == 1), (prev == 1 and cur == -1)
