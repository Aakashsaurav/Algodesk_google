"""
screener/screens/trend_following.py
===================================
Drop-in Screen: Sustained uptrend alignment with rising EMA + optional ADX & Supertrend filter.
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ParamSpec, ScreenMatch


class TrendFollowingScreen(BaseScreen):
    id = "trend_following"
    name = "Uptrend Alignment (EMA + ADX)"
    description = "Close > rising Fast EMA > Slow EMA, with configurable ADX trend strength & Supertrend confirmation."
    category = "trend"
    min_bars = 50
    params = [
        ParamSpec("ema_fast", "Fast EMA Period", "int", 20, min_val=5, max_val=100, step=1),
        ParamSpec("ema_slow", "Slow EMA Period", "int", 50, min_val=15, max_val=200, step=1),
        ParamSpec("adx_period", "ADX Period", "int", 14, min_val=5, max_val=30, step=1),
        ParamSpec("min_adx", "Min ADX (0 = ignore)", "float", 15.0, min_val=0.0, max_val=60.0, step=1.0),
        ParamSpec("require_supertrend_bull", "Require Supertrend BULL", "bool", True),
    ]

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        ema_f_s = toolkit.ema(int(params["ema_fast"]))
        ema_s_s = toolkit.ema(int(params["ema_slow"]))
        rsi14 = toolkit.rsi(14)[-1]
        _, atr_pct_s = toolkit.atr(14)
        st_dirs, _ = toolkit.supertrend(10, 3.0)
        adx_data = toolkit.adx(int(params["adx_period"]))
        vcp = toolkit.vcp()

        volumes = toolkit.volumes
        last_close = toolkit.closes[-1]
        vol_win = volumes[max(0, len(volumes) - 21) : -1]
        avg_vol = (sum(vol_win) / float(len(vol_win))) if vol_win else 0.0
        vol_ratio = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        ema_f = ema_f_s[-1]
        ema_f_prev5 = ema_f_s[-6] if len(ema_f_s) >= 6 else None
        ema_s = ema_s_s[-1]
        adx_val = adx_data["adx"][-1]
        min_adx = float(params["min_adx"])
        req_st = bool(params["require_supertrend_bull"])

        adx_ok = True if min_adx <= 0 else (adx_val is not None and adx_val >= min_adx)
        st_ok = (st_dirs[-1] == 1) if req_st else True

        matched = bool(
            ema_f is not None
            and ema_s is not None
            and last_close > ema_f > ema_s
            and (ema_f_prev5 is None or ema_f > ema_f_prev5)
            and adx_ok
            and st_ok
        )

        tags: List[str] = []
        score = 0
        if matched:
            tags.append("UPTREND")
            score += 35
            if st_dirs[-1] == 1:
                tags.append("ST_BULL")
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
                "rsi14": round(rsi14, 2) if rsi14 is not None else None,
                "atr_pct": round(atr_pct_s[-1], 2) if atr_pct_s[-1] is not None else None,
                "adx": round(adx_val, 2) if adx_val is not None else None,
                "vol_ratio": round(vol_ratio, 2),
                "contraction_ratio": vcp["contraction_ratio"],
                "dist_50d_high_pct": vcp["dist_from_50d_high_pct"],
                "supertrend": "BULL" if st_dirs[-1] == 1 else ("BEAR" if st_dirs[-1] == -1 else "FLAT"),
            },
        )
