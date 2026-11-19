"""
screener/screens/momentum_breakout.py
=====================================
Drop-in Screen: Momentum Breakout with editable EMA, RSI & Volume filters.
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ParamSpec, ScreenMatch


class MomentumBreakoutScreen(BaseScreen):
    id = "momentum_breakout"
    name = "Momentum Breakout"
    description = "Close > Fast EMA > Slow EMA, RSI in bullish zone, backed by volume expansion or strong daily gain."
    category = "momentum"
    min_bars = 50
    params = [
        ParamSpec("ema_fast", "Fast EMA Period", "int", 20, min_val=5, max_val=100, step=1),
        ParamSpec("ema_slow", "Slow EMA Period", "int", 50, min_val=15, max_val=200, step=1),
        ParamSpec("rsi_period", "RSI Period", "int", 14, min_val=2, max_val=50, step=1),
        ParamSpec("rsi_min", "Min RSI", "float", 55.0, min_val=30.0, max_val=85.0, step=1.0),
        ParamSpec("rsi_max", "Max RSI", "float", 80.0, min_val=50.0, max_val=98.0, step=1.0),
        ParamSpec("min_vol_ratio", "Min Vol Ratio (vs 20D)", "float", 1.2, min_val=0.5, max_val=10.0, step=0.1),
        ParamSpec("min_day_chg_pct", "Or Min Day Chg %", "float", 1.5, min_val=0.0, max_val=15.0, step=0.25),
    ]

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        ema_f = toolkit.ema(int(params["ema_fast"]))[-1]
        ema_s = toolkit.ema(int(params["ema_slow"]))[-1]
        rsi_v = toolkit.rsi(int(params["rsi_period"]))[-1]
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

        matched = bool(
            ema_f is not None
            and ema_s is not None
            and last_close > ema_f > ema_s
            and rsi_v is not None
            and float(params["rsi_min"]) <= rsi_v <= float(params["rsi_max"])
            and (vol_ratio >= float(params["min_vol_ratio"]) or chg_pct >= float(params["min_day_chg_pct"]))
        )
        score = 0
        tags: List[str] = []
        if matched:
            tags.append("MOMENTUM")
            score += 40
            if st_dirs[-1] == 1:
                tags.append("ST_BULL")
                score += 15
            if vol_ratio >= float(params["min_vol_ratio"]):
                score += 10
        else:
            tags.append("NEUTRAL")

        return ScreenMatch(
            matched=matched,
            signal=tags[0],
            tags=tags,
            score=score,
            metrics={
                "ema20": round(ema_f, 2) if ema_f is not None else None,
                "ema50": round(ema_s, 2) if ema_s is not None else None,
                "rsi14": round(rsi_v, 2) if rsi_v is not None else None,
                "atr_pct": round(atr_pct_s[-1], 2) if atr_pct_s[-1] is not None else None,
                "vol_ratio": round(vol_ratio, 2),
                "contraction_ratio": vcp["contraction_ratio"],
                "dist_50d_high_pct": vcp["dist_from_50d_high_pct"],
                "supertrend": "BULL" if st_dirs[-1] == 1 else ("BEAR" if st_dirs[-1] == -1 else "FLAT"),
            },
        )
