"""
backtest/strategies/vcp_breakout.py
===================================
Drop-in Strategy: Volatility Contraction Pattern (VCP) Range Breakout.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class VcpBreakoutStrategy(BacktestStrategy):
    id = "vcp_breakout"
    name = "VCP Range Breakout"
    description = "Enters on tight 7D/30D contraction breakout above previous 15-bar high; trails below Fast EMA."
    params = [
        ParamSpec("breakout_window", "Breakout High Window", "int", 15, min_val=5, max_val=60, step=1),
        ParamSpec("max_contraction_ratio", "Max 7D/30D Ratio", "float", 0.78, min_val=0.2, max_val=1.2, step=0.05),
        ParamSpec("exit_ema", "Trailing Exit EMA", "int", 20, min_val=5, max_val=100, step=1),
        ParamSpec("trend_ema", "Trend Filter EMA", "int", 50, min_val=15, max_val=200, step=5),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._hi_brk: List[Optional[float]] = []
        self._h30: List[Optional[float]] = []
        self._l30: List[Optional[float]] = []
        self._h7: List[Optional[float]] = []
        self._l7: List[Optional[float]] = []
        self._ef: List[Optional[float]] = []
        self._es: List[Optional[float]] = []
        self._max_ratio = 0.78

    def prepare(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> None:
        self._max_ratio = float(params["max_contraction_ratio"])
        self._hi_brk = toolkit.rolling_high(int(params["breakout_window"]))
        self._h30 = toolkit.rolling_high(30)
        self._l30 = toolkit.rolling_low(30)
        self._h7 = toolkit.rolling_high(7)
        self._l7 = toolkit.rolling_low(7)
        self._ef = toolkit.ema(int(params["exit_ema"]))
        self._es = toolkit.ema(int(params["trend_ema"]))

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        close = float(bar["close"])
        hi15 = self._hi_brk[index]
        h30, l30 = self._h30[index], self._l30[index]
        h7, l7 = self._h7[index], self._l7[index]
        es = self._es[index]
        ef = self._ef[index]

        enter_sig = False
        if hi15 is not None and h30 and l30 is not None and h7 and l7 is not None:
            r30 = (h30 - l30) / h30
            r7 = (h7 - l7) / h7
            if close > hi15 and r30 > 0 and (r7 / r30) < self._max_ratio and (es is None or close > es):
                enter_sig = True
        exit_sig = bool(ef is not None and close < ef)
        return enter_sig, exit_sig
