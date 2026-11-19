"""
screener/screens/all_metrics.py
===============================
Drop-in Screen: Displays full technical readings & composite tags for every symbol.
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ParamSpec, ScreenMatch


class AllMetricsScreen(BaseScreen):
    id = "all_metrics"
    name = "All Watchlist Metrics"
    description = "Evaluates EMA trend, RSI, ATR volatility, Volume Surge, VCP & Supertrend without filtering."
    category = "overview"
    min_bars = 15
    params = [
        ParamSpec("ema_fast", "Fast EMA Period", "int", 20, min_val=5, max_val=100, step=1),
        ParamSpec("ema_slow", "Slow EMA Period", "int", 50, min_val=10, max_val=200, step=1),
        ParamSpec("rsi_period", "RSI Period", "int", 14, min_val=2, max_val=50, step=1),
        ParamSpec("supertrend_period", "Supertrend ATR Period", "int", 10, min_val=3, max_val=50, step=1),
        ParamSpec("supertrend_mult", "Supertrend Multiplier", "float", 3.0, min_val=1.0, max_val=8.0, step=0.5),
    ]

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        ema_fast_p = int(params["ema_fast"])
        ema_slow_p = int(params["ema_slow"])
        rsi_p = int(params["rsi_period"])
        st_p = int(params["supertrend_period"])
        st_m = float(params["supertrend_mult"])

        ema_f_s = toolkit.ema(ema_fast_p)
        ema_s_s = toolkit.ema(ema_slow_p)
        rsi_s = toolkit.rsi(rsi_p)
        _, atr_pct_s = toolkit.atr(14)
        st_dirs, _ = toolkit.supertrend(st_p, st_m)
        vcp = toolkit.vcp()

        closes = toolkit.closes
        volumes = toolkit.volumes
        last_close = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else last_close
        chg_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

        vol_win = volumes[max(0, len(volumes) - 21) : -1]
        avg_vol = (sum(vol_win) / float(len(vol_win))) if vol_win else 0.0
        vol_ratio = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        ema_f = ema_f_s[-1]
        ema_f_prev5 = ema_f_s[-6] if len(ema_f_s) >= 6 else None
        ema_s = ema_s_s[-1]
        rsi_v = rsi_s[-1]
        atr_pct = atr_pct_s[-1]
        st_dir = st_dirs[-1]

        is_momentum = bool(
            ema_f is not None
            and ema_s is not None
            and last_close > ema_f > ema_s
            and rsi_v is not None
            and 55.0 <= rsi_v <= 80.0
            and (vol_ratio >= 1.2 or chg_pct >= 1.5)
        )
        is_vcp = bool(
            vcp["range_7d_pct"] is not None
            and vcp["contraction_ratio"] is not None
            and vcp["dist_from_50d_high_pct"] is not None
            and vcp["range_7d_pct"] <= 7.0
            and vcp["contraction_ratio"] < 0.72
            and vcp["dist_from_50d_high_pct"] >= -6.5
            and (ema_s is None or last_close >= ema_s)
        )
        is_oversold = bool(rsi_v is not None and (rsi_v <= 38.0 or (rsi_v <= 44.0 and chg_pct > 0)))
        is_uptrend = bool(
            ema_f is not None
            and ema_s is not None
            and last_close > ema_f > ema_s
            and (ema_f_prev5 is None or ema_f > ema_f_prev5)
        )

        tags: List[str] = []
        score = 0
        if is_momentum:
            tags.append("MOMENTUM")
            score += 35
        if is_vcp:
            tags.append("VCP")
            score += 30
        if is_uptrend:
            tags.append("UPTREND")
            score += 20
        if is_oversold:
            tags.append("OVERSOLD")
            score += 25
        if st_dir == 1:
            tags.append("ST_BULL")
            score += 10
        if not tags:
            tags.append("NEUTRAL")

        signal = "BREAKOUT+VCP" if ("MOMENTUM" in tags and "VCP" in tags) else tags[0]
        return ScreenMatch(
            matched=True,
            signal=signal,
            tags=tags,
            score=score,
            metrics={
                "ema20": round(ema_f, 2) if ema_f is not None else None,
                "ema50": round(ema_s, 2) if ema_s is not None else None,
                "rsi14": round(rsi_v, 2) if rsi_v is not None else None,
                "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
                "vol_ratio": round(vol_ratio, 2),
                "contraction_ratio": vcp["contraction_ratio"],
                "dist_50d_high_pct": vcp["dist_from_50d_high_pct"],
                "supertrend": "BULL" if st_dir == 1 else ("BEAR" if st_dir == -1 else "FLAT"),
            },
        )
