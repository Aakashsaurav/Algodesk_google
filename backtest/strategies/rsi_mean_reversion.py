"""
backtest/strategies/rsi_mean_reversion.py
=========================================
Drop-in Strategy: Wilder RSI Mean Reversion.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class RsiMeanReversionStrategy(BacktestStrategy):
    id = "rsi_mean_reversion"
    name = "Wilder RSI (14) Mean Reversion"
    description = "Enters when Wilder RSI dips into oversold territory; exits on overbought recovery."
    params = [
        ParamSpec("rsi_period", "RSI Period", "int", 14, min_val=2, max_val=50, step=1),
        ParamSpec("oversold", "Entry RSI <= ", "float", 35.0, min_val=10.0, max_val=50.0, step=1.0),
        ParamSpec("overbought", "Exit RSI >= ", "float", 65.0, min_val=50.0, max_val=90.0, step=1.0),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rsi: List[Optional[float]] = []
        self._oversold = 35.0
        self._overbought = 65.0

    def prepare(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> None:
        self._rsi = toolkit.rsi(int(params["rsi_period"]))
        self._oversold = float(params["oversold"])
        self._overbought = float(params["overbought"])

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        rv = self._rsi[index]
        if rv is None:
            return False, False
        return (rv <= self._oversold), (rv >= self._overbought)
