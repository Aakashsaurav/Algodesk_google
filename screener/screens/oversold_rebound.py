"""
screener/screens/oversold_rebound.py
====================================
Drop-in Screen: Oversold Wilder RSI mean-reversion candidates.
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch


class OversoldReboundScreen(BaseScreen):
    id = "oversold_rebound"
    name = "Oversold Mean Reversion"
    description = "Identifies symbols in oversold Wilder RSI territory or rebounding cleanly off oversold levels."
    category = "mean_reversion"
    min_bars = 20
    params = [
        ParamSpec("rsi_period", "RSI Period", "int", 14, min_val=2, max_val=50, step=1),
        ParamSpec("rsi_oversold", "Oversold RSI <= ", "float", 38.0, min_val=10.0, max_val=55.0, step=1.0),
        ParamSpec("rsi_rebound_max", "Rebound Bounce RSI <= ", "float", 44.0, min_val=20.0, max_val=60.0, step=1.0),
        ParamSpec("require_positive_bounce", "Require Positive Day", "bool", False),
    ]

    def required_bars(self, params: Dict[str, Any]) -> int:
        rsi_p = int(params.get("rsi_period", 14))
        return max(45, rsi_p * 3 + 15)

    def get_columns(self, params: Dict[str, Any]) -> List[ColumnSpec]:
        return [
            ColumnSpec("rsi14", f"RSI ({int(params.get('rsi_period', 14))})", "number", 1),
            ColumnSpec("atr_pct", "ATR%", "pct", 2),
            ColumnSpec("ema20", "EMA 20", "price", 2),
            ColumnSpec("vol_ratio", "Vol Ratio", "ratio", 2),
            ColumnSpec("supertrend", "Supertrend", "badge", 0, "left"),
        ]

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        rsi_v = toolkit.rsi(int(params["rsi_period"]))[-1]
        ema20 = toolkit.ema(20)[-1]
        ema50 = toolkit.ema(50)[-1]
        _, atr_pct_s = toolkit.atr(14)
        st_dirs, _ = toolkit.supertrend(10, 3.0)
        vcp = toolkit.vcp()

        closes = toolkit.closes
        volumes = toolkit.volumes
        last_close = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else last_close
        chg_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
        vol_win = volumes[max(0, len(volumes) - 21) : -1]
        avg_vol = (sum(vol_win) / float(len(vol_win))) if vol_win else 0.0
        vol_ratio = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        deep_os = float(params["rsi_oversold"])
        bounce_max = float(params["rsi_rebound_max"])
        req_pos = bool(params["require_positive_bounce"])

        if rsi_v is None:
            matched = False
        elif req_pos:
            matched = bool(rsi_v <= bounce_max and chg_pct > 0)
        else:
            matched = bool(rsi_v <= deep_os or (rsi_v <= bounce_max and chg_pct > 0))

        score = 30 if matched else 0
        if matched and chg_pct > 0:
            score += 10
        tags = ["OVERSOLD"] if matched else ["NEUTRAL"]

        return ScreenMatch(
            matched=matched,
            signal=tags[0],
            tags=tags,
            score=score,
            metrics={
                "ema20": round(ema20, 2) if ema20 is not None else None,
                "ema50": round(ema50, 2) if ema50 is not None else None,
                "rsi14": round(rsi_v, 2) if rsi_v is not None else None,
                "atr_pct": round(atr_pct_s[-1], 2) if atr_pct_s[-1] is not None else None,
                "vol_ratio": round(vol_ratio, 2),
                "contraction_ratio": vcp["contraction_ratio"],
                "dist_50d_high_pct": vcp["dist_from_50d_high_pct"],
                "supertrend": "BULL" if st_dirs[-1] == 1 else ("BEAR" if st_dirs[-1] == -1 else "FLAT"),
            },
        )
