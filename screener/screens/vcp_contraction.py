"""
screener/screens/vcp_contraction.py
===================================
Drop-in Screen: Volatility Contraction Pattern (VCP) near 50-day highs.
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ParamSpec, ScreenMatch


class VcpContractionScreen(BaseScreen):
    id = "vcp_contraction"
    name = "VCP Contraction"
    description = "Tight 7-day range contracting vs 30-day range within striking distance of 50-day highs."
    category = "breakout"
    min_bars = 30
    params = [
        ParamSpec("max_range_7d_pct", "Max 7D Range %", "float", 7.0, min_val=1.0, max_val=25.0, step=0.5),
        ParamSpec("max_contraction_ratio", "Max 7D/30D Ratio", "float", 0.72, min_val=0.2, max_val=1.2, step=0.05),
        ParamSpec("min_dist_50d_high_pct", "Min Dist From 50D High %", "float", -6.5, min_val=-25.0, max_val=0.0, step=0.5),
        ParamSpec("ema_support", "Support EMA Period", "int", 50, min_val=10, max_val=200, step=5),
    ]

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        ema20 = toolkit.ema(20)[-1]
        ema_sup = toolkit.ema(int(params["ema_support"]))[-1]
        rsi14 = toolkit.rsi(14)[-1]
        _, atr_pct_s = toolkit.atr(14)
        st_dirs, _ = toolkit.supertrend(10, 3.0)
        vcp = toolkit.vcp()

        volumes = toolkit.volumes
        last_close = toolkit.closes[-1]
        vol_win = volumes[max(0, len(volumes) - 21) : -1]
        avg_vol = (sum(vol_win) / float(len(vol_win))) if vol_win else 0.0
        vol_ratio = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        matched = bool(
            vcp["range_7d_pct"] is not None
            and vcp["contraction_ratio"] is not None
            and vcp["dist_from_50d_high_pct"] is not None
            and vcp["range_7d_pct"] <= float(params["max_range_7d_pct"])
            and vcp["contraction_ratio"] < float(params["max_contraction_ratio"])
            and vcp["dist_from_50d_high_pct"] >= float(params["min_dist_50d_high_pct"])
            and (ema_sup is None or last_close >= ema_sup)
        )
        score = 35 if matched else 0
        if matched and st_dirs[-1] == 1:
            score += 10
        tags = ["VCP"] if matched else ["NEUTRAL"]

        return ScreenMatch(
            matched=matched,
            signal=tags[0],
            tags=tags,
            score=score,
            metrics={
                "ema20": round(ema20, 2) if ema20 is not None else None,
                "ema50": round(ema_sup, 2) if ema_sup is not None else None,
                "rsi14": round(rsi14, 2) if rsi14 is not None else None,
                "atr_pct": round(atr_pct_s[-1], 2) if atr_pct_s[-1] is not None else None,
                "vol_ratio": round(vol_ratio, 2),
                "contraction_ratio": vcp["contraction_ratio"],
                "dist_50d_high_pct": vcp["dist_from_50d_high_pct"],
                "supertrend": "BULL" if st_dirs[-1] == 1 else ("BEAR" if st_dirs[-1] == -1 else "FLAT"),
            },
        )
