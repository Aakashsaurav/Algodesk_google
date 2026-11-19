"""
screener/screens/support_resistance.py
======================================
Drop-in Multi-Timeframe Support & Resistance (S&R) Scanner for AlgoDesk.

Evaluates 5m, 15m, 1h, 1d, and 1w independently using confirmed fractal pivots,
ATR-normalized bounded clustering, historical completed-candle interaction
scoring (0-100), Major/Minor classification, multi-timeframe confluence, and
9 actionable interaction events (including FALSE_BREAKOUT).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch
from screener.sr_engine import (
    SR_DEFAULT_TIMEFRAMES,
    evaluate_symbol_mtf,
)


class SupportResistanceScreen(BaseScreen):
    id = "support_resistance"
    name = "Multi-Timeframe Support & Resistance"
    description = (
        "Confirmed fractal pivot S&R zones across 5m, 15m, 1h, 1d, and 1w with "
        "ATR clustering, 0-100 strength scoring, Major/Minor tiers, MTF confluence, "
        "and bounce/rejection/breakout/retest/false-breakout events."
    )
    category = "structure"
    min_bars = 25
    supports_multi_timeframe = True
    default_timeframes = list(SR_DEFAULT_TIMEFRAMES)
    multi_row = True

    params = [
        ParamSpec(
            "left_bars",
            "Left Pivot Bars",
            "int",
            5,
            min_val=2,
            max_val=20,
            step=1,
            description="Candles required to the left of a fractal swing high/low.",
        ),
        ParamSpec(
            "right_bars",
            "Right Pivot Bars",
            "int",
            5,
            min_val=2,
            max_val=20,
            step=1,
            description="Right-side confirmation candles required before pivot becomes valid.",
        ),
        ParamSpec(
            "min_pivot_separation",
            "Min Pivot Separation",
            "int",
            3,
            min_val=1,
            max_val=20,
            step=1,
            description="Minimum candle separation between consecutive pivots of the same side.",
        ),
        ParamSpec(
            "atr_period",
            "ATR Period",
            "int",
            14,
            min_val=5,
            max_val=50,
            step=1,
            description="Wilder ATR lookback period used for volatility normalization.",
        ),
        ParamSpec(
            "cluster_atr_multiplier",
            "Cluster ATR Mult",
            "float",
            0.50,
            min_val=0.10,
            max_val=2.50,
            step=0.05,
            description="ATR multiplier in D = max(ATR * mult, Price * min_pct%).",
        ),
        ParamSpec(
            "cluster_min_pct",
            "Cluster Min %",
            "float",
            0.25,
            min_val=0.05,
            max_val=3.00,
            step=0.05,
            description="Minimum percentage price tolerance for clustering pivots.",
        ),
        ParamSpec(
            "major_strength",
            "Major Strength >=",
            "int",
            70,
            min_val=50,
            max_val=95,
            step=1,
            description="Minimum 0-100 score for a zone to be classified as Major.",
        ),
        ParamSpec(
            "minor_strength",
            "Minor Strength >=",
            "int",
            40,
            min_val=10,
            max_val=85,
            step=1,
            description="Minimum 0-100 score for Minor zones (zones below are Weak and hidden).",
        ),
        ParamSpec(
            "minimum_touches",
            "Minimum Touches",
            "int",
            2,
            min_val=1,
            max_val=10,
            step=1,
            description="Minimum independent touch episodes required to surface a zone.",
        ),
        ParamSpec(
            "near_distance_atr",
            "Near Distance (ATR)",
            "float",
            0.50,
            min_val=0.10,
            max_val=2.50,
            step=0.05,
            description="Volatility-adjusted distance threshold for NEAR_SUPPORT / NEAR_RESISTANCE.",
        ),
        ParamSpec(
            "breakout_buffer_atr",
            "Breakout Buffer (ATR)",
            "float",
            0.20,
            min_val=0.05,
            max_val=1.50,
            step=0.05,
            description="Completed close buffer beyond zone boundary for Breakout / Breakdown.",
        ),
        ParamSpec(
            "reversal_distance_atr",
            "Reversal Distance (ATR)",
            "float",
            0.50,
            min_val=0.15,
            max_val=2.50,
            step=0.05,
            description="Minimum post-touch price recovery required for Bounce / Rejection.",
        ),
        ParamSpec(
            "retest_tolerance_atr",
            "Retest Tolerance (ATR)",
            "float",
            0.25,
            min_val=0.05,
            max_val=1.50,
            step=0.05,
            description="Maximum distance to broken zone boundary for Breakout / Breakdown Retest.",
        ),
        ParamSpec(
            "relative_volume_threshold",
            "Rel Volume Threshold",
            "float",
            1.50,
            min_val=0.50,
            max_val=5.00,
            step=0.10,
            description="Relative volume threshold vs time-of-day / rolling baseline for confirmation.",
        ),
    ]

    columns = [
        ColumnSpec("exchange", "Exchange", "text", 0, "left"),
        ColumnSpec("fno_eligible", "F&O Eligible", "badge", 0, "left"),
        ColumnSpec("timeframe", "Timeframe", "text", 0, "left"),
        ColumnSpec("event", "Event", "badge", 0, "left"),
        ColumnSpec("level_type", "Level Type", "badge", 0, "left"),
        ColumnSpec("classification", "Classification", "badge", 0, "left"),
        ColumnSpec("current_price", "Current Price", "price", 2, "right"),
        ColumnSpec("zone_low", "Zone Low", "price", 2, "right"),
        ColumnSpec("zone_high", "Zone High", "price", 2, "right"),
        ColumnSpec("distance_pct", "Distance %", "signed_pct", 2, "right"),
        ColumnSpec("strength", "Strength", "number", 1, "right"),
        ColumnSpec("touches", "Touches", "number", 0, "right"),
        ColumnSpec("reactions", "Reactions", "number", 0, "right"),
        ColumnSpec("relative_volume", "Relative Volume", "ratio", 2, "right"),
        ColumnSpec("confluence", "Confluence", "text", 0, "left"),
    ]

    def required_bars(self, params: Dict[str, Any]) -> int:
        """Enough bars for reliable fractal pivots, multi-touch history, and ATR."""
        lb = int(params.get("left_bars", 5) or 5)
        rb = int(params.get("right_bars", 5) or 5)
        atr_p = int(params.get("atr_period", 14) or 14)
        return max(120, atr_p * 4 + (lb + rb) * 8)

    def required_bars_for_timeframe(self, timeframe: str, params: Dict[str, Any]) -> int:
        """Per-timeframe bar targets balanced for structural depth and provider retention."""
        tf = str(timeframe or "1day").strip().lower()
        if tf == "1week":
            return 104  # ~2 years of weekly candles
        if tf == "1day":
            return 250  # ~1 year of daily candles
        if tf in {"60minute", "1h"}:
            return 200  # ~30-35 trading days of hourly bars
        if tf in {"15minute", "15m"}:
            return 220  # ~9-10 trading days of 15m bars
        return 250  # ~4-5 trading days of 5m bars

    def evaluate_multi_timeframe(
        self,
        *,
        symbol: str,
        exchange: str,
        candles_by_tf: Dict[str, Sequence[Dict[str, Any]]],
        params: Dict[str, Any],
        as_of_date: Optional[str] = None,
        fno_eligible: bool = False,
        api_calls: int = 0,
    ) -> Dict[str, Any]:
        return evaluate_symbol_mtf(
            symbol=symbol,
            exchange=exchange,
            candles_by_tf=candles_by_tf,
            params=params,
            as_of_date=as_of_date,
            fno_eligible=fno_eligible,
            api_calls=api_calls,
            include_weak=False,
        )

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        """
        Backward-compatible single-timeframe fallback (e.g., if run inside
        Live Screen or single-TF callers). Evaluates the supplied candles as 1d
        and returns the highest-strength qualifying zone/event.
        """
        eval_out = evaluate_symbol_mtf(
            symbol="SINGLE",
            exchange="NSE",
            candles_by_tf={"1day": candles},
            params=params,
            as_of_date=None,
            fno_eligible=False,
            include_weak=False,
        )
        rows = eval_out.get("rows", [])
        if not rows:
            return ScreenMatch(
                matched=False,
                signal="NO_SR_EVENT",
                tags=[],
                score=0,
                metrics={},
            )
        top = rows[0]
        return ScreenMatch(
            matched=True,
            signal=str(top["event"]),
            tags=list(top.get("tags", [])),
            score=int(top.get("score", 0)),
            metrics={
                "timeframe": top["timeframe"],
                "event": top["event"],
                "level_type": top["level_type"],
                "classification": top["classification"],
                "current_price": top["current_price"],
                "zone_low": top["zone_low"],
                "zone_high": top["zone_high"],
                "distance_pct": top["distance_pct"],
                "strength": top["strength"],
                "touches": top["touches"],
                "reactions": top["reactions"],
                "relative_volume": top["relative_volume"],
                "confluence": top["confluence"],
            },
        )
