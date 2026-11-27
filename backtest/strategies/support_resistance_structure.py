"""
backtest/strategies/support_resistance_structure.py
===================================================
Drop-in Built-in Strategy: Support & Resistance Structure (Causal, Zero-Lookahead).

Trades confirmed horizontal S&R structure events computed strictly point-in-time:
- `breakout_and_retest`: Enters on confirmed Resistance Breakout or Breakout Retest
  with minimum zone strength & optional HTF confluence / volume confirmation.
- `support_bounce`: Enters on confirmed Support Bounce off qualifying support zones.
- `both`: Combines Breakout/Retest and Support Bounce entries.

Exits when price decisively breaks below active Support (Support Breakdown),
triggers a False Breakout / Resistance Rejection on strong resistance, or
trails below optional EMA exit / configured SL/TP/TSL.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class SupportResistanceStrategy(BacktestStrategy):
    id = "support_resistance_structure"
    name = "Support & Resistance Structure"
    description = (
        "Strictly-causal fractal S&R strategy trading confirmed Resistance Breakouts, "
        "Breakout Retests, and Support Bounces with ATR-clustered zones, 0-100 strength "
        "scoring, and optional higher-timeframe confluence."
    )
    params = [
        ParamSpec(
            "setup_mode",
            "Setup Mode",
            "str",
            "both",
            description="Bullish S&R setups: 'both', 'breakout_and_retest', or 'support_bounce'.",
        ),
        ParamSpec("left_bars", "Left Pivot Bars", "int", 5, min_val=2, max_val=15, step=1),
        ParamSpec("right_bars", "Right Pivot Bars", "int", 5, min_val=2, max_val=15, step=1),
        ParamSpec("atr_period", "ATR Period", "int", 14, min_val=5, max_val=50, step=1),
        ParamSpec("cluster_atr_multiplier", "Cluster ATR Mult", "float", 0.50, min_val=0.15, max_val=2.0, step=0.05),
        ParamSpec("min_strength", "Min Zone Strength", "float", 55.0, min_val=20.0, max_val=95.0, step=5.0),
        ParamSpec("breakout_buffer_atr", "Breakout Buffer (ATR)", "float", 0.20, min_val=0.05, max_val=1.5, step=0.05),
        ParamSpec("reversal_distance_atr", "Reversal Distance (ATR)", "float", 0.50, min_val=0.15, max_val=2.0, step=0.05),
        ParamSpec("htf_confluence", "HTF Confluence", "bool", True, description="Boost zone strength with completed higher-timeframe overlapping zones."),
        ParamSpec("exit_on_rejection", "Exit on Resistance Rejection", "bool", True, description="Exit open longs on confirmed Resistance Rejection or False Breakout."),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._mode = "both"
        self._exit_on_rejection = True
        self._sr: Dict[str, Any] = {}

    def required_bars(self, params: Dict[str, Any]) -> int:
        lb = int(params.get("left_bars", 5) or 5)
        rb = int(params.get("right_bars", 5) or 5)
        atr_p = int(params.get("atr_period", 14) or 14)
        return max(80, atr_p * 3 + (lb + rb) * 6)

    def prepare(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> None:
        self._mode = str(params.get("setup_mode") or "both").strip().lower()
        self._exit_on_rejection = bool(params.get("exit_on_rejection", True))
        extra: Dict[str, Any] = {
            "breakout_buffer_atr": float(params.get("breakout_buffer_atr", 0.20)),
            "reversal_distance_atr": float(params.get("reversal_distance_atr", 0.50)),
        }
        for wk in (
            "weight_touches",
            "weight_reactions",
            "weight_volume",
            "weight_recency",
            "weight_age",
            "weight_confluence",
            "weight_reversal",
        ):
            if wk in params:
                extra[wk] = params[wk]

        self._sr = toolkit.support_resistance(
            left_bars=int(params.get("left_bars", 5)),
            right_bars=int(params.get("right_bars", 5)),
            atr_period=int(params.get("atr_period", 14)),
            cluster_atr_multiplier=float(params.get("cluster_atr_multiplier", 0.50)),
            min_strength=float(params.get("min_strength", 55.0)),
            htf_confluence=bool(params.get("htf_confluence", True)),
            extra_params=extra,
            record_overlays=True,
        )

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        if not self._sr:
            return False, False
        bo = (self._sr.get("sr_resistance_breakout") or [0.0])[index] or 0.0
        retest = (self._sr.get("sr_breakout_retest") or [0.0])[index] or 0.0
        bounce = (self._sr.get("sr_support_bounce") or [0.0])[index] or 0.0
        bd = (self._sr.get("sr_support_breakdown") or [0.0])[index] or 0.0
        rej = (self._sr.get("sr_resistance_rejection") or [0.0])[index] or 0.0
        false_bo = (self._sr.get("sr_false_breakout") or [0.0])[index] or 0.0

        enter_sig = False
        if self._mode == "breakout_and_retest":
            enter_sig = bool(bo >= 1.0 or retest >= 1.0)
        elif self._mode == "support_bounce":
            enter_sig = bool(bounce >= 1.0)
        else:
            enter_sig = bool(bo >= 1.0 or retest >= 1.0 or bounce >= 1.0)

        exit_sig = bool(bd >= 1.0)
        if self._exit_on_rejection and (rej >= 1.0 or false_bo >= 1.0):
            exit_sig = True
        return enter_sig, exit_sig
