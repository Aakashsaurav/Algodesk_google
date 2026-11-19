"""indicators package exposing unified IndicatorToolkit & pure-Python fallbacks."""

from indicators.toolkit import (
    IndicatorToolkit,
    available_backends,
    compute_adx_series,
    compute_atr_series,
    compute_bbands_series,
    compute_ema_series,
    compute_macd_series,
    compute_roc_series,
    compute_rsi_wilder_series,
    compute_sma_series,
    compute_stoch_series,
    compute_supertrend_series,
    compute_vcp_metrics,
    rolling_extrema,
)

__all__ = [
    "IndicatorToolkit",
    "available_backends",
    "compute_adx_series",
    "compute_atr_series",
    "compute_bbands_series",
    "compute_ema_series",
    "compute_macd_series",
    "compute_roc_series",
    "compute_rsi_wilder_series",
    "compute_sma_series",
    "compute_stoch_series",
    "compute_supertrend_series",
    "compute_vcp_metrics",
    "rolling_extrema",
]
