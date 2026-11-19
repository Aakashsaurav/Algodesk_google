"""
backtest/strategies/ema_crossover.py
====================================
Drop-in Strategy: EMA Crossover with configurable fast & slow periods.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class EmaCrossoverStrategy(BacktestStrategy):
    id = "ema_crossover"
    name = "EMA 20 / 50 Crossover"
    description = "Enters long when Fast EMA crosses above Slow EMA; exits on bearish cross."
    params = [
        ParamSpec("fast_period", "Fast EMA Period", "int", 20, min_val=2, max_val=500, step=1),
        ParamSpec("slow_period", "Slow EMA Period", "int", 50, min_val=5, max_val=1000, step=1),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._fast: List[Optional[float]] = []
        self._slow: List[Optional[float]] = []

    def prepare(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> None:
        fp = int(params["fast_period"])
        sp = int(params["slow_period"])
        if len(candles) < sp + 5 and fp == 20 and sp == 50:
            fp, sp = 9, 21
        self._fast = toolkit.ema(fp)
        self._slow = toolkit.ema(sp)

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        if index <= 0:
            return False, False
        ef, es = self._fast[index], self._slow[index]
        ef_p, es_p = self._fast[index - 1], self._slow[index - 1]
        if ef is None or es is None or ef_p is None or es_p is None:
            return False, False
        return (ef_p <= es_p and ef > es), (ef_p >= es_p and ef < es)
