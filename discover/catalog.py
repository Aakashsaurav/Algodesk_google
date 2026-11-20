"""
discover/catalog.py
===================
Streak-style Discover Marketplace Catalog mixing:
1. Visual Block Scanners & Strategies (editable in UI, 1-click Backtest / Deploy / Clone)
2. Advanced Custom Python `.py` Scanners & Strategies (MTF Support & Resistance,
   VCP Contraction, Supertrend, RSI Reversion, EMA Crossover)
3. Multi-Leg Options Templates (Iron Condor, Bull Call Spread, Short Strangle)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_DISCOVER_ITEMS: List[Dict[str, Any]] = [
    {
        "id": "disc_ema_rsi_momentum",
        "category": "strategy",
        "mode": "visual",
        "name": "EMA 20/50 Crossover + RSI Momentum",
        "tagline": "Classic institutional trend follower using EMA(20) crossing above EMA(50) confirmed by RSI(14) > 55.",
        "timeframe": "1day",
        "segment": "equity_delivery",
        "tags": ["Trend Following", "Visual Blocks", "Swing"],
        "target_id": "vis_ema_rsi_momentum",
        "default_symbols": ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"],
        "stop_loss_pct": 4.0,
        "take_profit_pct": 12.0,
    },
    {
        "id": "disc_supertrend_adx",
        "category": "strategy",
        "mode": "visual",
        "name": "Supertrend + ADX Intraday Directional",
        "tagline": "Captures intraday momentum surges on 15m bars only when ADX(14) confirms trend strength above 22.",
        "timeframe": "15minute",
        "segment": "equity_intraday",
        "tags": ["Intraday Breakout", "Visual Blocks", "ADX"],
        "target_id": "vis_supertrend_adx",
        "default_symbols": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
        "stop_loss_pct": 1.5,
        "take_profit_pct": 3.5,
    },
    {
        "id": "disc_py_ema_crossover",
        "category": "strategy",
        "mode": "python",
        "name": "Fast/Slow EMA Crossover (Python Plugin)",
        "tagline": "Editable Python strategy plugin supporting custom fast/slow EMA periods and O(N) TA-Lib acceleration.",
        "timeframe": "1day",
        "segment": "equity_delivery",
        "tags": ["Trend Following", "Python (.py)", "Portfolio"],
        "target_id": "ema_crossover",
        "default_symbols": ["RELIANCE", "TCS", "INFY", "LT", "BHARTIARTL"],
        "stop_loss_pct": 5.0,
        "take_profit_pct": 12.0,
    },
    {
        "id": "disc_py_supertrend",
        "category": "strategy",
        "mode": "python",
        "name": "ATR Supertrend Flip (Python Plugin)",
        "tagline": "Python implementation of ATR-based Supertrend reversals with configurable period and band multiplier.",
        "timeframe": "15minute",
        "segment": "equity_intraday",
        "tags": ["Momentum", "Python (.py)", "ATR"],
        "target_id": "supertrend_trend",
        "default_symbols": ["SBIN", "ICICIBANK", "TATAMOTORS", "AXISBANK"],
        "stop_loss_pct": 2.0,
        "take_profit_pct": 5.0,
    },
    {
        "id": "disc_py_rsi_reversion",
        "category": "strategy",
        "mode": "python",
        "name": "RSI Oversold Rebound (Python Plugin)",
        "tagline": "Counter-trend mean reversion entering extreme RSI dips and exiting on mean recovery.",
        "timeframe": "1day",
        "segment": "equity_delivery",
        "tags": ["Mean Reversion", "Python (.py)", "RSI"],
        "target_id": "rsi_mean_reversion",
        "default_symbols": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
        "stop_loss_pct": 4.0,
        "take_profit_pct": 8.0,
    },
    {
        "id": "disc_scan_mtf_sr",
        "category": "screen",
        "mode": "python",
        "name": "Multi-Timeframe Support & Resistance Scanner",
        "tagline": "Advanced Python scanner using confirmed fractal pivots, ATR clustering, 0–100 zone strength scoring, and 5m–1w confluence.",
        "timeframe": "1day",
        "segment": "equity_delivery",
        "tags": ["Price Action", "Python (.py)", "MTF S&R"],
        "target_id": "support_resistance",
        "default_symbols": ["RELIANCE", "TCS", "HDFCBANK", "INFY"],
    },
    {
        "id": "disc_scan_vcp",
        "category": "screen",
        "mode": "python",
        "name": "VCP Volatility Contraction Scanner",
        "tagline": "Minervini-style volatility contraction detector identifying tight range squeezes near 50-day highs.",
        "timeframe": "1day",
        "segment": "equity_delivery",
        "tags": ["Breakout", "Python (.py)", "VCP"],
        "target_id": "vcp_contraction",
        "default_symbols": ["RELIANCE", "LT", "BHARTIARTL", "SUNPHARMA"],
    },
    {
        "id": "disc_scan_vis_rsi_vol",
        "category": "screen",
        "mode": "visual",
        "name": "RSI Momentum + 1.5x Volume Surge",
        "tagline": "Visual block scanner catching liquid equities above EMA(20) with RSI > 60 and institutional volume.",
        "timeframe": "1day",
        "segment": "equity_intraday",
        "tags": ["Breakout", "Visual Blocks", "Volume"],
        "target_id": "vis_scan_rsi_breakout",
        "default_symbols": ["RELIANCE", "TCS", "SBIN", "ITC"],
    },
    {
        "id": "disc_opt_iron_condor",
        "category": "options",
        "mode": "options",
        "name": "NIFTY Weekly Iron Condor (Defined Risk)",
        "tagline": "Sell OTM Call & Put (+200 / -200 pts) hedged with wings (+400 / -400 pts) for theta decay in rangebound regimes.",
        "timeframe": "15minute",
        "segment": "equity_options",
        "tags": ["Options Income", "Defined Risk", "NIFTY"],
        "target_id": "iron_condor",
        "default_spot": 24200,
        "default_step": 100,
        "default_lot_size": 75,
    },
    {
        "id": "disc_opt_bull_call",
        "category": "options",
        "mode": "options",
        "name": "BANKNIFTY Bull Call Spread",
        "tagline": "Buy ATM Call and sell OTM Call (+200 pts) to cap debit outlay while capturing upside breakouts.",
        "timeframe": "15minute",
        "segment": "equity_options",
        "tags": ["Options Directional", "Debit Spread", "BANKNIFTY"],
        "target_id": "bull_call_spread",
        "default_spot": 51800,
        "default_step": 100,
        "default_lot_size": 30,
    },
]


def list_discover_catalog(
    category: Optional[str] = None,
    mode: Optional[str] = None,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    items = [dict(x) for x in _DISCOVER_ITEMS]
    eff_cat = (category or kind or "").strip().lower()
    if eff_cat and eff_cat != "all":
        items = [x for x in items if x["category"].lower() == eff_cat]
    eff_mode = (mode or "").strip().lower()
    if eff_mode and eff_mode != "all":
        items = [x for x in items if x["mode"].lower() == eff_mode]
    return items


__all__ = ["list_discover_catalog"]
