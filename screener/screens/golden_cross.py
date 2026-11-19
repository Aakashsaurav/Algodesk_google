"""
screener/screens/golden_cross.py
================================
Screen: SMA 50 > SMA 200 (Golden Cross indicator).
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch


class GoldenCrossScreen(BaseScreen):
    id = "golden_cross"
    name = "Golden Cross (SMA 50 > SMA 200)"
    description = "Checks if the 50-day Simple Moving Average is greater than the 200-day Simple Moving Average."
    category = "trend"
    min_bars = 210

    params = [
        ParamSpec("sma_fast", "Fast SMA Period", "int", 50, min_val=10, max_val=200, step=1),
        ParamSpec("sma_slow", "Slow SMA Period", "int", 200, min_val=50, max_val=500, step=1),
    ]

    def required_bars(self, params: Dict[str, Any]) -> int:
        return int(params.get("sma_slow", 200)) + 10

    def get_columns(self, params: Dict[str, Any]) -> List[ColumnSpec]:
        return [
            ColumnSpec("sma50", f"SMA {int(params.get('sma_fast', 50))}", "price", 2),
            ColumnSpec("sma200", f"SMA {int(params.get('sma_slow', 200))}", "price", 2),
        ]

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        sma_f_s = toolkit.sma(int(params["sma_fast"]))
        sma_s_s = toolkit.sma(int(params["sma_slow"]))

        sma_f = sma_f_s[-1] if sma_f_s else None
        sma_s = sma_s_s[-1] if sma_s_s else None

        matched = bool(
            sma_f is not None
            and sma_s is not None
            and sma_f > sma_s
        )

        tags = ["GOLDEN_CROSS"] if matched else ["NEUTRAL"]
        score = 50 if matched else 0

        return ScreenMatch(
            matched=matched,
            signal=tags[0],
            tags=tags,
            score=score,
            metrics={
                "sma50": round(sma_f, 2) if sma_f is not None else None,
                "sma200": round(sma_s, 2) if sma_s is not None else None,
            },
        )
