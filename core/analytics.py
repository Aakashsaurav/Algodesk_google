"""
core/analytics.py
=================
Phase 05 — Market Screener, O(N) Strategy Backtester, Portfolio Risk Guard &
Async Background Task Runner.

Design constraints for MacBook Air M2 (8GB RAM):
- Zero heavy array/dataframe dependencies (pure O(N) Python arithmetic + collections.deque).
- Reuses HistoricalDataManager gap-only SQLite caching (0 redundant broker API calls
  for already-cached dates) routed through the user's active Data Connection.
- Integrates live Risk Limit evaluation with Trading Broker funds/positions,
  SEBI SHA-256 AuditTrail, and Emergency KillSwitchController.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from broker.registry import BrokerRegistry
from config import IST_TZ, config
from core.audit import AuditTrail, audit_trail
from core.historical import HistoricalDataManager, historical_data_manager
from core.preflight import KillSwitchController, kill_switch_controller

logger = logging.getLogger(__name__)

DEFAULT_SCREENER_WATCHLIST: List[str] = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "AXISBANK",
]

SCREENER_PRESETS: Dict[str, str] = {
    "all": "All Watchlist Metrics",
    "momentum_breakout": "Momentum Breakout (Close > EMA20 > EMA50, RSI 55–80)",
    "vcp_contraction": "VCP Volatility Contraction (Tight Range near 50D High)",
    "oversold_rebound": "Oversold Mean Reversion (Wilder RSI <= 38)",
    "trend_following": "Uptrend Alignment (Close > EMA20 > EMA50, Rising EMA20)",
}

BACKTEST_STRATEGIES: Dict[str, str] = {
    "ema_crossover": "EMA 20 / 50 Crossover",
    "rsi_mean_reversion": "Wilder RSI (14) Mean Reversion",
    "vcp_breakout": "VCP Range Breakout",
    "supertrend_atr": "Supertrend (10, ATR 3.0x)",
}


# ── Pure O(N) Technical Indicators ────────────────────────────────────────────


def compute_ema_series(values: List[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n == 0:
        return out
    if n < period:
        return out

    seed = sum(values[:period]) / float(period)
    out[period - 1] = seed
    k = 2.0 / float(period + 1)
    prev = seed
    for i in range(period, n):
        ema = (values[i] - prev) * k + prev
        out[i] = ema
        prev = ema
    return out


def compute_rsi_wilder_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta

    avg_gain = gains / float(period)
    avg_loss = losses / float(period)
    out[period] = 100.0 if avg_loss == 0 else (100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))

    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / float(period)
        avg_loss = (avg_loss * (period - 1) + loss) / float(period)
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def compute_atr_series(
    candles: List[Dict[str, Any]],
    period: int = 14,
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    n = len(candles)
    atr_out: List[Optional[float]] = [None] * n
    atr_pct_out: List[Optional[float]] = [None] * n
    if n == 0 or period <= 0 or n < period:
        return atr_out, atr_pct_out

    trs: List[float] = [0.0] * n
    for i in range(n):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        if i == 0:
            trs[i] = max(0.0, h - l)
        else:
            pc = float(candles[i - 1]["close"])
            trs[i] = max(h - l, abs(h - pc), abs(l - pc))

    atr = sum(trs[:period]) / float(period)
    atr_out[period - 1] = atr
    c0 = float(candles[period - 1]["close"])
    atr_pct_out[period - 1] = (atr / c0) * 100.0 if c0 > 0 else None

    for i in range(period, n):
        atr = ((atr * (period - 1)) + trs[i]) / float(period)
        atr_out[i] = atr
        close_i = float(candles[i]["close"])
        atr_pct_out[i] = (atr / close_i) * 100.0 if close_i > 0 else None

    return atr_out, atr_pct_out


def compute_supertrend_series(
    candles: List[Dict[str, Any]],
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[List[int], List[Optional[float]]]:
    n = len(candles)
    dirs: List[int] = [0] * n
    stops: List[Optional[float]] = [None] * n
    if n < period + 1:
        return dirs, stops

    atr_series, _ = compute_atr_series(candles, period=period)
    final_upper: Optional[float] = None
    final_lower: Optional[float] = None
    prev_dir = 1

    for i in range(period - 1, n):
        atr = atr_series[i]
        if atr is None:
            continue
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        close = float(candles[i]["close"])
        hl2 = (high + low) * 0.5
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr

        if final_upper is None or final_lower is None:
            final_upper = basic_upper
            final_lower = basic_lower
            prev_dir = 1 if close >= hl2 else -1
            dirs[i] = prev_dir
            stops[i] = final_lower if prev_dir == 1 else final_upper
            continue

        prev_close = float(candles[i - 1]["close"])
        if basic_upper < final_upper or prev_close > final_upper:
            final_upper = basic_upper
        if basic_lower > final_lower or prev_close < final_lower:
            final_lower = basic_lower

        if prev_dir == 1:
            if close < final_lower:
                prev_dir = -1
        else:
            if close > final_upper:
                prev_dir = 1

        dirs[i] = prev_dir
        stops[i] = final_lower if prev_dir == 1 else final_upper

    return dirs, stops


def _rolling_extrema(values: List[float], window: int, find_max: bool) -> List[Optional[float]]:
    """Monotonic deque O(N) rolling high/low over strictly previous `window` bars."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if window <= 0 or n == 0:
        return out
    dq: deque[int] = deque()
    for i in range(n):
        if i >= 1:
            idx_add = i - 1
            val_add = values[idx_add]
            if find_max:
                while dq and values[dq[-1]] <= val_add:
                    dq.pop()
            else:
                while dq and values[dq[-1]] >= val_add:
                    dq.pop()
            dq.append(idx_add)
        while dq and dq[0] < i - window:
            dq.popleft()
        if i >= window and dq:
            out[i] = values[dq[0]]
    return out


def _compute_vcp_metrics(candles: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    n = len(candles)
    if n < 15:
        return {
            "range_30d_pct": None,
            "range_7d_pct": None,
            "contraction_ratio": None,
            "dist_from_50d_high_pct": None,
        }
    last_close = float(candles[-1]["close"])
    win30 = candles[max(0, n - 30) :]
    win7 = candles[max(0, n - 7) :]
    win50 = candles[max(0, n - 50) :]

    hi30 = max(float(c["high"]) for c in win30)
    lo30 = min(float(c["low"]) for c in win30)
    hi7 = max(float(c["high"]) for c in win7)
    lo7 = min(float(c["low"]) for c in win7)
    hi50 = max(float(c["high"]) for c in win50)

    r30 = ((hi30 - lo30) / hi30) * 100.0 if hi30 > 0 else None
    r7 = ((hi7 - lo7) / hi7) * 100.0 if hi7 > 0 else None
    ratio = (r7 / r30) if (r30 and r30 > 0 and r7 is not None) else None
    dist50 = ((last_close - hi50) / hi50) * 100.0 if hi50 > 0 else None

    return {
        "range_30d_pct": round(r30, 2) if r30 is not None else None,
        "range_7d_pct": round(r7, 2) if r7 is not None else None,
        "contraction_ratio": round(ratio, 3) if ratio is not None else None,
        "dist_from_50d_high_pct": round(dist50, 2) if dist50 is not None else None,
    }


def _load_candles_gap_only(
    *,
    hist_mgr: HistoricalDataManager,
    symbol: str,
    exchange: str,
    timeframe: str,
    from_date: str,
    to_date: str,
    provider: str,
    fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    instrument_type: Optional[str] = None,
    instrument_key: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    dl_meta = hist_mgr.download(
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
        from_date=from_date,
        to_date=to_date,
        provider=provider,
        instrument_type=instrument_type,
        instrument_key=instrument_key,
        fetcher=fetcher,
    )
    records = hist_mgr.get_records(
        symbol=dl_meta["symbol"],
        exchange=dl_meta["exchange"],
        timeframe=dl_meta["timeframe"],
        from_date=from_date,
        to_date=to_date,
        view="full",
    )
    return records.get("rows", []), dl_meta


# ── 1. Market Screener Engine ─────────────────────────────────────────────────


class MarketScreener:
    """Pure O(N) rule-based technical screener powered by active Data Connection."""

    def __init__(self, hist_manager: Optional[HistoricalDataManager] = None) -> None:
        self._hist = hist_manager or historical_data_manager

    def scan(
        self,
        *,
        symbols: Optional[List[str]] = None,
        exchange: str = "NSE",
        timeframe: str = "1day",
        lookback_days: int = 120,
        preset: str = "all",
        provider: Optional[str] = None,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        clean_preset = (preset or "all").strip().lower()
        if clean_preset not in SCREENER_PRESETS:
            clean_preset = "all"

        tf = (timeframe or "1day").strip().lower()
        eff_exch = (exchange or "NSE").strip().upper()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()
        days = max(30, min(int(lookback_days or 120), 730))

        raw_list = symbols if symbols else list(DEFAULT_SCREENER_WATCHLIST)
        clean_symbols: List[str] = []
        seen: set[str] = set()
        for s in raw_list:
            sym = str(s or "").strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                clean_symbols.append(sym)
        if not clean_symbols:
            clean_symbols = list(DEFAULT_SCREENER_WATCHLIST)

        today_ist = datetime.now(tz=IST_TZ).date()
        from_date = (today_ist - timedelta(days=days)).isoformat()
        to_date = today_ist.isoformat()

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        total_api_calls = 0
        total_cached_skipped = 0

        total = len(clean_symbols)
        for idx, sym in enumerate(clean_symbols):
            if progress_cb:
                progress_cb(idx, total, f"Scanning {sym} ({idx + 1}/{total}) via {eff_provider.upper()}...")
            try:
                rows, dl_meta = _load_candles_gap_only(
                    hist_mgr=self._hist,
                    symbol=sym,
                    exchange=eff_exch,
                    timeframe=tf,
                    from_date=from_date,
                    to_date=to_date,
                    provider=eff_provider,
                    fetcher=fetcher,
                )
                total_api_calls += int(dl_meta.get("api_calls_made", 0))
                total_cached_skipped += int(dl_meta.get("existing_days_skipped", 0))
                if len(rows) < 15:
                    errors.append({"symbol": sym, "error": f"Insufficient bars ({len(rows)} < 15) for indicators."})
                    continue

                closes = [float(r["close"]) for r in rows]
                volumes = [float(r.get("volume") or 0) for r in rows]
                ema20_s = compute_ema_series(closes, 20)
                ema50_s = compute_ema_series(closes, 50)
                rsi14_s = compute_rsi_wilder_series(closes, 14)
                _, atr_pct_s = compute_atr_series(rows, 14)
                st_dirs, _ = compute_supertrend_series(rows, 10, 3.0)
                vcp = _compute_vcp_metrics(rows)

                last = rows[-1]
                prev = rows[-2] if len(rows) >= 2 else last
                last_close = closes[-1]
                prev_close = float(prev["close"])
                day_chg_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

                vol_win = volumes[max(0, len(volumes) - 21) : -1]
                avg_vol20 = (sum(vol_win) / float(len(vol_win))) if vol_win else 0.0
                vol_ratio = (volumes[-1] / avg_vol20) if avg_vol20 > 0 else 1.0

                ema20 = ema20_s[-1]
                ema20_prev5 = ema20_s[-6] if len(ema20_s) >= 6 else None
                ema50 = ema50_s[-1]
                rsi14 = rsi14_s[-1]
                atr_pct = atr_pct_s[-1]
                st_dir = st_dirs[-1]

                is_momentum = bool(
                    ema20 is not None
                    and ema50 is not None
                    and last_close > ema20 > ema50
                    and rsi14 is not None
                    and 55.0 <= rsi14 <= 80.0
                    and (vol_ratio >= 1.2 or day_chg_pct >= 1.5)
                )
                is_vcp = bool(
                    vcp["range_7d_pct"] is not None
                    and vcp["contraction_ratio"] is not None
                    and vcp["dist_from_50d_high_pct"] is not None
                    and vcp["range_7d_pct"] <= 7.0
                    and vcp["contraction_ratio"] < 0.72
                    and vcp["dist_from_50d_high_pct"] >= -6.5
                    and (ema50 is None or last_close >= ema50)
                )
                is_oversold = bool(rsi14 is not None and (rsi14 <= 38.0 or (rsi14 <= 44.0 and day_chg_pct > 0)))
                is_uptrend = bool(
                    ema20 is not None
                    and ema50 is not None
                    and last_close > ema20 > ema50
                    and (ema20_prev5 is None or ema20 > ema20_prev5)
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

                matched = True
                if clean_preset == "momentum_breakout":
                    matched = is_momentum
                elif clean_preset == "vcp_contraction":
                    matched = is_vcp
                elif clean_preset == "oversold_rebound":
                    matched = is_oversold
                elif clean_preset == "trend_following":
                    matched = is_uptrend

                signal = tags[0]
                if "MOMENTUM" in tags and "VCP" in tags:
                    signal = "BREAKOUT+VCP"

                results.append(
                    {
                        "symbol": dl_meta["symbol"],
                        "exchange": dl_meta["exchange"],
                        "bars": len(rows),
                        "last_date": str(last["trade_date"]),
                        "close": round(last_close, 2),
                        "change_pct": round(day_chg_pct, 2),
                        "ema20": round(ema20, 2) if ema20 is not None else None,
                        "ema50": round(ema50, 2) if ema50 is not None else None,
                        "rsi14": round(rsi14, 2) if rsi14 is not None else None,
                        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
                        "vol_ratio": round(vol_ratio, 2),
                        "contraction_ratio": vcp["contraction_ratio"],
                        "dist_50d_high_pct": vcp["dist_from_50d_high_pct"],
                        "supertrend": "BULL" if st_dir == 1 else ("BEAR" if st_dir == -1 else "FLAT"),
                        "signal": signal,
                        "tags": tags,
                        "score": score,
                        "matched": matched,
                        "api_calls": int(dl_meta.get("api_calls_made", 0)),
                    }
                )
            except Exception as exc:
                errors.append({"symbol": sym, "error": str(exc)})

        if progress_cb:
            progress_cb(total, total, f"Screener completed ({len(results)} symbols evaluated).")

        filtered = [r for r in results if r["matched"]] if clean_preset != "all" else results
        filtered.sort(key=lambda r: (r["score"], abs(r["change_pct"])), reverse=True)

        return {
            "ok": True,
            "preset": clean_preset,
            "preset_label": SCREENER_PRESETS[clean_preset],
            "provider": eff_provider,
            "exchange": eff_exch,
            "timeframe": tf,
            "lookback_days": days,
            "scanned_count": len(clean_symbols),
            "evaluated_count": len(results),
            "matched_count": len(filtered),
            "api_calls_made": total_api_calls,
            "existing_days_skipped": total_cached_skipped,
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
            "results": filtered,
            "all_evaluated_count": len(results),
            "errors": errors,
        }


# ── 2. O(N) Strategy Backtester Engine ────────────────────────────────────────


class BacktestEngine:
    """Single-pass O(N) backtester with slippage, fees, SL/TP, and equity curve."""

    def __init__(self, hist_manager: Optional[HistoricalDataManager] = None) -> None:
        self._hist = hist_manager or historical_data_manager

    def run(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        strategy: str = "ema_crossover",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        initial_capital: float = 500000.0,
        slippage_bps: float = 5.0,
        fee_bps: float = 5.0,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 12.0,
        provider: Optional[str] = None,
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        clean_sym = (symbol or "").strip().upper()
        if not clean_sym:
            raise ValueError("symbol is required for backtesting.")
        clean_exch = (exchange or "NSE").strip().upper()
        tf = (timeframe or "1day").strip().lower()
        strat = (strategy or "ema_crossover").strip().lower()
        if strat not in BACKTEST_STRATEGIES:
            raise ValueError(f"Unknown strategy '{strategy}'. Supported: {', '.join(BACKTEST_STRATEGIES)}")

        cap = max(1000.0, float(initial_capital or 500000.0))
        cost_rate = max(0.0, (float(slippage_bps or 0.0) + float(fee_bps or 0.0)) / 10000.0)
        sl_pct = max(0.0, float(stop_loss_pct or 0.0))
        tp_pct = max(0.0, float(take_profit_pct or 0.0))

        today_ist = datetime.now(tz=IST_TZ).date()
        end_str = (to_date or "").strip() or today_ist.isoformat()
        start_str = (from_date or "").strip() or (today_ist - timedelta(days=365)).isoformat()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()

        if progress_cb:
            progress_cb(10, 100, f"Loading OHLCV for {clean_sym} via {eff_provider.upper()} (gap-only)...")

        rows, dl_meta = _load_candles_gap_only(
            hist_mgr=self._hist,
            symbol=clean_sym,
            exchange=clean_exch,
            timeframe=tf,
            from_date=start_str,
            to_date=end_str,
            provider=eff_provider,
            fetcher=fetcher,
            instrument_type=instrument_type,
            instrument_key=instrument_key,
        )

        if len(rows) < 15:
            raise ValueError(
                f"Need at least 15 bars to run backtest on {dl_meta['symbol']} (found {len(rows)}). "
                "Expand the date range or verify provider connection."
            )

        if progress_cb:
            progress_cb(45, 100, f"Computing O(N) indicators on {len(rows)} bars...")

        closes = [float(r["close"]) for r in rows]
        highs = [float(r["high"]) for r in rows]
        lows = [float(r["low"]) for r in rows]

        fast_p = 20 if len(rows) >= 60 else 9
        slow_p = 50 if len(rows) >= 60 else 21
        ema_fast = compute_ema_series(closes, fast_p)
        ema_slow = compute_ema_series(closes, slow_p)
        rsi14 = compute_rsi_wilder_series(closes, 14)
        st_dirs, _ = compute_supertrend_series(rows, 10, 3.0)
        prev15_high = _rolling_extrema(highs, 15, find_max=True)
        prev30_high = _rolling_extrema(highs, 30, find_max=True)
        prev30_low = _rolling_extrema(lows, 30, find_max=False)
        prev7_high = _rolling_extrema(highs, 7, find_max=True)
        prev7_low = _rolling_extrema(lows, 7, find_max=False)

        if progress_cb:
            progress_cb(70, 100, f"Simulating {BACKTEST_STRATEGIES[strat]}...")

        cash = cap
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_cost = 0.0
        trades: List[Dict[str, Any]] = []
        raw_curve: List[Dict[str, Any]] = []

        peak_equity = cap
        max_drawdown_pct = 0.0
        daily_returns: List[float] = []
        prev_equity = cap

        for i, bar in enumerate(rows):
            bar_close = closes[i]
            bar_high = highs[i]
            bar_low = lows[i]
            bar_date = str(bar.get("trade_date") or str(bar.get("ts_ist", ""))[:10])

            # Check SL/TP first when holding position
            exit_reason = ""
            exit_px = bar_close
            if shares > 0:
                if sl_pct > 0 and bar_low <= entry_price * (1.0 - sl_pct / 100.0):
                    exit_px = entry_price * (1.0 - sl_pct / 100.0)
                    exit_reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                elif tp_pct > 0 and bar_high >= entry_price * (1.0 + tp_pct / 100.0):
                    exit_px = entry_price * (1.0 + tp_pct / 100.0)
                    exit_reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"

            # Strategy signals on bar i
            enter_sig = False
            exit_sig = False

            if strat == "ema_crossover":
                ef, es = ema_fast[i], ema_slow[i]
                ef_p, es_p = (ema_fast[i - 1], ema_slow[i - 1]) if i > 0 else (None, None)
                if ef is not None and es is not None and ef_p is not None and es_p is not None:
                    if ef_p <= es_p and ef > es:
                        enter_sig = True
                    elif ef_p >= es_p and ef < es:
                        exit_sig = True
            elif strat == "rsi_mean_reversion":
                r_cur = rsi14[i]
                if r_cur is not None:
                    if r_cur <= 35.0:
                        enter_sig = True
                    elif r_cur >= 65.0:
                        exit_sig = True
            elif strat == "vcp_breakout":
                hi15 = prev15_high[i]
                h30, l30 = prev30_high[i], prev30_low[i]
                h7, l7 = prev7_high[i], prev7_low[i]
                es = ema_slow[i]
                ef = ema_fast[i]
                if hi15 is not None and h30 and l30 is not None and h7 and l7 is not None:
                    r30 = (h30 - l30) / h30
                    r7 = (h7 - l7) / h7
                    if bar_close > hi15 and r30 > 0 and (r7 / r30) < 0.78 and (es is None or bar_close > es):
                        enter_sig = True
                if ef is not None and bar_close < ef:
                    exit_sig = True
            elif strat == "supertrend_atr":
                if i > 0 and st_dirs[i] != 0 and st_dirs[i - 1] != 0:
                    if st_dirs[i - 1] == -1 and st_dirs[i] == 1:
                        enter_sig = True
                    elif st_dirs[i - 1] == 1 and st_dirs[i] == -1:
                        exit_sig = True

            if shares > 0 and (exit_reason or exit_sig):
                sell_px = exit_px * (1.0 - cost_rate)
                proceeds = shares * sell_px
                pnl = proceeds - (shares * entry_price) - entry_cost
                ret_pct = ((sell_px - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
                cash += proceeds
                trades.append(
                    {
                        "entry_date": entry_date,
                        "exit_date": bar_date,
                        "qty": shares,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_px, 2),
                        "pnl": round(pnl, 2),
                        "return_pct": round(ret_pct, 2),
                        "exit_reason": exit_reason or "STRATEGY_EXIT",
                    }
                )
                shares = 0
                entry_price = 0.0
                entry_date = ""
                entry_cost = 0.0
            elif shares == 0 and enter_sig and i < len(rows) - 1:
                buy_eff = bar_close * (1.0 + cost_rate)
                qty = int(cash // buy_eff)
                if qty >= 1:
                    shares = qty
                    entry_price = bar_close
                    entry_date = bar_date
                    gross = shares * bar_close
                    entry_cost = gross * cost_rate
                    cash -= gross + entry_cost

            equity = cash + shares * bar_close
            if equity > peak_equity:
                peak_equity = equity
            dd_pct = ((peak_equity - equity) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct
            if prev_equity > 0:
                daily_returns.append((equity - prev_equity) / prev_equity)
            prev_equity = equity
            raw_curve.append({"date": bar_date, "equity": round(equity, 2), "drawdown_pct": round(dd_pct, 2)})

        # Close any open position on final bar so realized metrics stay consistent
        if shares > 0:
            last_close = closes[-1]
            last_date = str(rows[-1].get("trade_date") or str(rows[-1].get("ts_ist", ""))[:10])
            sell_px = last_close * (1.0 - cost_rate)
            proceeds = shares * sell_px
            pnl = proceeds - (shares * entry_price) - entry_cost
            ret_pct = ((sell_px - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
            cash += proceeds
            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": last_date,
                    "qty": shares,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(last_close, 2),
                    "pnl": round(pnl, 2),
                    "return_pct": round(ret_pct, 2),
                    "exit_reason": "END_OF_WINDOW",
                }
            )
            shares = 0

        final_equity = cash
        net_pnl = final_equity - cap
        total_return_pct = (net_pnl / cap) * 100.0

        # Annualized CAGR & Sharpe ratio (252 trading sessions/yr for daily)
        start_d = date.fromisoformat(str(rows[0]["trade_date"]))
        end_d = date.fromisoformat(str(rows[-1]["trade_date"]))
        span_days = max(1, (end_d - start_d).days)
        years = max(span_days / 365.25, 1.0 / 252.0)
        cagr_pct = ((max(final_equity, 1.0) / cap) ** (1.0 / years) - 1.0) * 100.0 if years >= 0.08 else total_return_pct

        sharpe = 0.0
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / float(len(daily_returns))
            var_r = sum((r - mean_r) ** 2 for r in daily_returns) / float(len(daily_returns) - 1)
            std_r = math.sqrt(max(0.0, var_r))
            if std_r > 0:
                ann_factor = 252.0 if tf == "1day" else (252.0 * 12.0)
                sharpe = (mean_r / std_r) * math.sqrt(ann_factor)

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        win_rate = (len(wins) / float(len(trades))) * 100.0 if trades else 0.0
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

        # Downsample curve to <= 160 points for smooth SVG chart
        curve = raw_curve
        if len(raw_curve) > 160:
            step = max(1, len(raw_curve) // 160)
            sampled = [raw_curve[j] for j in range(0, len(raw_curve), step)]
            if sampled[-1]["date"] != raw_curve[-1]["date"]:
                sampled.append(raw_curve[-1])
            curve = sampled

        if progress_cb:
            progress_cb(100, 100, f"Backtest finished ({len(trades)} trades, Net PnL ₹{net_pnl:,.2f}).")

        return {
            "ok": True,
            "symbol": dl_meta["symbol"],
            "exchange": dl_meta["exchange"],
            "strategy": strat,
            "strategy_label": BACKTEST_STRATEGIES[strat],
            "timeframe": tf,
            "provider": eff_provider,
            "from_date": str(rows[0]["trade_date"]),
            "to_date": str(rows[-1]["trade_date"]),
            "bars_evaluated": len(rows),
            "api_calls_made": int(dl_meta.get("api_calls_made", 0)),
            "existing_days_skipped": int(dl_meta.get("existing_days_skipped", 0)),
            "initial_capital": round(cap, 2),
            "final_equity": round(final_equity, 2),
            "net_pnl": round(net_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "cagr_pct": round(cagr_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "total_trades": len(trades),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "equity_curve": curve,
            "trades": trades,
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }


# ── 3. Portfolio Risk Limits & Exposure Guard ─────────────────────────────────


class RiskLimitManager:
    """Persists portfolio risk limits & evaluates live broker exposure + P&L."""

    def __init__(
        self,
        limits_file: Optional[Path] = None,
        audit: Optional[AuditTrail] = None,
        kill_switch: Optional[KillSwitchController] = None,
    ) -> None:
        self._explicit_path = limits_file
        self._audit = audit or audit_trail
        self._kill_switch = kill_switch or kill_switch_controller
        self._lock = threading.RLock()

    @property
    def limits_file(self) -> Path:
        return self._explicit_path or (config.DATA_DIR / "risk_limits.json")

    def _defaults(self) -> Dict[str, Any]:
        cap = float(config.TOTAL_CAPITAL or 500000.0)
        return {
            "total_capital_inr": round(cap, 2),
            "max_capital_per_trade_pct": 15.0,
            "max_daily_loss_inr": round(max(5000.0, cap * 0.03), 2),
            "max_open_positions": 5,
            "max_gross_exposure_inr": round(cap * 0.80, 2),
            "auto_engage_kill_switch": True,
            "updated_at_ist": None,
        }

    def load_limits(self) -> Dict[str, Any]:
        with self._lock:
            defaults = self._defaults()
            path = self.limits_file
            if not path.exists():
                return defaults
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return defaults
                merged = dict(defaults)
                for k in (
                    "total_capital_inr",
                    "max_capital_per_trade_pct",
                    "max_daily_loss_inr",
                    "max_open_positions",
                    "max_gross_exposure_inr",
                    "auto_engage_kill_switch",
                    "updated_at_ist",
                ):
                    if k in raw and raw[k] is not None:
                        merged[k] = raw[k]
                return merged
            except (OSError, json.JSONDecodeError):
                return defaults

    def save_limits(self, updates: Dict[str, Any], actor: str = "operator") -> Dict[str, Any]:
        with self._lock:
            cur = self.load_limits()
            next_cfg = dict(cur)
            if "total_capital_inr" in updates and updates["total_capital_inr"] is not None:
                val = float(updates["total_capital_inr"])
                if val <= 0:
                    raise ValueError("total_capital_inr must be > 0.")
                next_cfg["total_capital_inr"] = round(val, 2)
            if "max_capital_per_trade_pct" in updates and updates["max_capital_per_trade_pct"] is not None:
                val = float(updates["max_capital_per_trade_pct"])
                if not (0.1 <= val <= 100.0):
                    raise ValueError("max_capital_per_trade_pct must be between 0.1 and 100.")
                next_cfg["max_capital_per_trade_pct"] = round(val, 2)
            if "max_daily_loss_inr" in updates and updates["max_daily_loss_inr"] is not None:
                val = float(updates["max_daily_loss_inr"])
                if val <= 0:
                    raise ValueError("max_daily_loss_inr must be > 0.")
                next_cfg["max_daily_loss_inr"] = round(val, 2)
            if "max_open_positions" in updates and updates["max_open_positions"] is not None:
                val_i = int(updates["max_open_positions"])
                if val_i < 1:
                    raise ValueError("max_open_positions must be >= 1.")
                next_cfg["max_open_positions"] = val_i
            if "max_gross_exposure_inr" in updates and updates["max_gross_exposure_inr"] is not None:
                val = float(updates["max_gross_exposure_inr"])
                if val <= 0:
                    raise ValueError("max_gross_exposure_inr must be > 0.")
                next_cfg["max_gross_exposure_inr"] = round(val, 2)
            if "auto_engage_kill_switch" in updates and updates["auto_engage_kill_switch"] is not None:
                next_cfg["auto_engage_kill_switch"] = bool(updates["auto_engage_kill_switch"])

            next_cfg["updated_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            path = self.limits_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(next_cfg, indent=2), encoding="utf-8")

        self._audit.log(
            event_type="RISK_LIMITS_UPDATED",
            actor=actor,
            payload={
                "max_capital_per_trade_pct": next_cfg["max_capital_per_trade_pct"],
                "max_daily_loss_inr": next_cfg["max_daily_loss_inr"],
                "max_open_positions": next_cfg["max_open_positions"],
                "max_gross_exposure_inr": next_cfg["max_gross_exposure_inr"],
                "auto_engage_kill_switch": next_cfg["auto_engage_kill_switch"],
            },
        )
        return next_cfg

    def evaluate(
        self,
        *,
        trading_broker: str = "upstox",
        funds: Optional[Dict[str, float]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        limits = self.load_limits()
        broker_id = (trading_broker or "upstox").strip().lower()
        broker_connected = False
        broker_note = ""

        live_funds: Dict[str, float] = {}
        live_positions: List[Dict[str, Any]] = []
        live_orders: List[Dict[str, Any]] = []

        if funds is not None or positions is not None or orders is not None:
            broker_connected = True
            live_funds = dict(funds or {})
            live_positions = list(positions or [])
            live_orders = list(orders or [])
            broker_note = f"Evaluated against {broker_id.upper()} live state."
        else:
            try:
                adapter = BrokerRegistry.get(broker_id)
                snap = adapter.health_check(timeout=2.5)
                broker_connected = bool(snap.is_connected)
                if broker_connected and adapter.capabilities.supports_trading:
                    live_funds = dict(adapter.get_funds() or {})
                    raw_pos = adapter.get_positions() or []
                    for p in raw_pos:
                        live_positions.append(
                            {
                                "symbol": getattr(p, "symbol", ""),
                                "quantity": int(getattr(p, "quantity", 0)),
                                "average_price": float(getattr(p, "average_price", 0.0)),
                                "last_price": float(getattr(p, "last_price", 0.0)),
                                "realized_pnl": float(getattr(p, "realized_pnl", 0.0)),
                                "unrealized_pnl": float(getattr(p, "unrealized_pnl", 0.0)),
                            }
                        )
                    raw_ord = adapter.get_orders() or []
                    live_orders = [
                        {
                            "order_id": getattr(o, "order_id", ""),
                            "symbol": getattr(o, "symbol", ""),
                            "status": str(getattr(o, "status", "")),
                        }
                        for o in raw_ord
                    ]
                    broker_note = f"Connected to {adapter.display_name} ({snap.account_name or snap.client_id})."
                else:
                    broker_note = f"{adapter.display_name} disconnected ({snap.message}). Evaluating configured capital envelope."
            except Exception as exc:
                broker_note = f"Broker probe note ({exc}). Evaluating configured capital envelope."

        avail_margin = float(live_funds.get("available", 0.0) or 0.0)
        used_margin = float(live_funds.get("used", 0.0) or 0.0)
        live_cap = avail_margin + used_margin
        capital_base = live_cap if live_cap > 0 else float(limits["total_capital_inr"])

        open_positions = [p for p in live_positions if int(p.get("quantity", 0)) != 0]
        gross_exposure = 0.0
        max_single_pos_val = 0.0
        daily_pnl = 0.0
        for p in live_positions:
            qty = abs(int(p.get("quantity", 0)))
            px = float(p.get("last_price") or p.get("average_price") or 0.0)
            notional = qty * px
            if qty > 0:
                gross_exposure += notional
                if notional > max_single_pos_val:
                    max_single_pos_val = notional
            daily_pnl += float(p.get("realized_pnl", 0.0)) + float(p.get("unrealized_pnl", 0.0))

        max_single_pos_pct = (max_single_pos_val / capital_base) * 100.0 if capital_base > 0 else 0.0
        daily_loss = max(0.0, -daily_pnl)
        exposure_util_pct = (gross_exposure / float(limits["max_gross_exposure_inr"])) * 100.0
        daily_loss_util_pct = (daily_loss / float(limits["max_daily_loss_inr"])) * 100.0

        breaches: List[Dict[str, Any]] = []
        if daily_loss >= float(limits["max_daily_loss_inr"]):
            breaches.append(
                {
                    "rule": "max_daily_loss_inr",
                    "severity": "CRITICAL",
                    "message": f"Daily loss ₹{daily_loss:,.2f} reached/exceeded limit ₹{limits['max_daily_loss_inr']:,.2f}.",
                }
            )
        if len(open_positions) > int(limits["max_open_positions"]):
            breaches.append(
                {
                    "rule": "max_open_positions",
                    "severity": "WARNING",
                    "message": f"Open positions ({len(open_positions)}) exceeded limit ({limits['max_open_positions']}).",
                }
            )
        if gross_exposure > float(limits["max_gross_exposure_inr"]):
            breaches.append(
                {
                    "rule": "max_gross_exposure_inr",
                    "severity": "CRITICAL",
                    "message": f"Gross exposure ₹{gross_exposure:,.2f} exceeded cap ₹{limits['max_gross_exposure_inr']:,.2f}.",
                }
            )
        if max_single_pos_pct > float(limits["max_capital_per_trade_pct"]):
            breaches.append(
                {
                    "rule": "max_capital_per_trade_pct",
                    "severity": "WARNING",
                    "message": f"Largest position is {max_single_pos_pct:.1f}% of capital (limit {limits['max_capital_per_trade_pct']:.1f}%).",
                }
            )

        kill_auto_triggered = False
        has_daily_loss_breach = any(b["rule"] == "max_daily_loss_inr" for b in breaches)
        if has_daily_loss_breach and bool(limits.get("auto_engage_kill_switch", True)):
            if not self._kill_switch.is_engaged():
                self._kill_switch.engage(
                    reason=f"Auto-halt: daily loss ₹{daily_loss:,.2f} breached cap ₹{limits['max_daily_loss_inr']:,.2f}",
                    actor="risk_guard",
                    trading_broker=broker_id,
                )
                kill_auto_triggered = True
            self._audit.log(
                event_type="RISK_LIMIT_BREACH",
                actor="risk_guard",
                payload={
                    "trading_broker": broker_id,
                    "daily_loss_inr": round(daily_loss, 2),
                    "limit_inr": limits["max_daily_loss_inr"],
                    "auto_kill_switch": True,
                },
            )

        return {
            "ok": True,
            "trading_broker": broker_id,
            "broker_connected": broker_connected,
            "broker_note": broker_note,
            "limits": limits,
            "telemetry": {
                "capital_base_inr": round(capital_base, 2),
                "available_margin_inr": round(avail_margin, 2),
                "used_margin_inr": round(used_margin, 2),
                "open_positions_count": len(open_positions),
                "open_orders_count": len(live_orders),
                "gross_exposure_inr": round(gross_exposure, 2),
                "exposure_utilization_pct": round(min(exposure_util_pct, 999.0), 1),
                "daily_pnl_inr": round(daily_pnl, 2),
                "daily_loss_inr": round(daily_loss, 2),
                "daily_loss_utilization_pct": round(min(daily_loss_util_pct, 999.0), 1),
                "max_single_position_pct": round(max_single_pos_pct, 2),
            },
            "breaches": breaches,
            "status": "BREACHED" if breaches else ("HEALTHY" if broker_connected else "STANDBY"),
            "kill_switch": self._kill_switch.status(),
            "kill_switch_auto_triggered": kill_auto_triggered,
            "evaluated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }


# ── 4. Lightweight Async Background Job Runner ────────────────────────────────


class AsyncJobRunner:
    """2-worker thread pool with bounded in-memory ring buffer for Phase 5 tasks."""

    def __init__(self, max_workers: int = 2, max_history: int = 40) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="AlgoDeskP5")
        self._lock = threading.RLock()
        self._jobs: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._max_history = max_history

    def _prune_locked(self) -> None:
        while len(self._jobs) > self._max_history:
            self._jobs.popitem(last=False)

    def submit(
        self,
        job_type: str,
        meta: Dict[str, Any],
        fn: Callable[[Callable[[int, int, str], None]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        record: Dict[str, Any] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "QUEUED",
            "progress_pct": 0,
            "progress_message": "Queued...",
            "created_at_ist": now_ist,
            "finished_at_ist": None,
            "meta": dict(meta),
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = record
            self._prune_locked()

        def _progress(done: int, total: int, message: str) -> None:
            pct = int(round((done / float(total)) * 100.0)) if total > 0 else 0
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "RUNNING"
                    self._jobs[job_id]["progress_pct"] = max(0, min(100, pct))
                    self._jobs[job_id]["progress_message"] = message

        def _worker() -> None:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "RUNNING"
                    self._jobs[job_id]["progress_message"] = f"Starting {job_type}..."
            try:
                res = fn(_progress)
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "COMPLETED"
                        self._jobs[job_id]["progress_pct"] = 100
                        self._jobs[job_id]["progress_message"] = "Completed"
                        self._jobs[job_id]["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
                        self._jobs[job_id]["result"] = res
            except Exception as exc:
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "FAILED"
                        self._jobs[job_id]["progress_pct"] = 100
                        self._jobs[job_id]["progress_message"] = str(exc)
                        self._jobs[job_id]["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
                        self._jobs[job_id]["error"] = str(exc)

        self._pool.submit(_worker)
        return self.get_job(job_id) or record

    def run_sync(
        self,
        job_type: str,
        meta: Dict[str, Any],
        fn: Callable[[Callable[[int, int, str], None]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        record: Dict[str, Any] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "RUNNING",
            "progress_pct": 0,
            "progress_message": f"Running {job_type}...",
            "created_at_ist": now_ist,
            "finished_at_ist": None,
            "meta": dict(meta),
            "result": None,
            "error": None,
        }
        res = fn(lambda _d, _t, _m: None)
        record["status"] = "COMPLETED"
        record["progress_pct"] = 100
        record["progress_message"] = "Completed"
        record["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        record["result"] = res
        with self._lock:
            self._jobs[job_id] = record
            self._prune_locked()
        return record

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._jobs.values())
        items.reverse()
        capped = max(1, min(int(limit), 40))
        out: List[Dict[str, Any]] = []
        for item in items[:capped]:
            slim = dict(item)
            # Keep summary listing compact without full candle arrays
            if isinstance(slim.get("result"), dict) and "equity_curve" in slim["result"]:
                slim["result"] = {k: v for k, v in slim["result"].items() if k not in {"equity_curve", "trades"}}
            out.append(slim)
        return out


market_screener = MarketScreener()
backtest_engine = BacktestEngine()
risk_limit_manager = RiskLimitManager()
analytics_job_runner = AsyncJobRunner()

__all__ = [
    "AsyncJobRunner",
    "BACKTEST_STRATEGIES",
    "BacktestEngine",
    "DEFAULT_SCREENER_WATCHLIST",
    "MarketScreener",
    "RiskLimitManager",
    "SCREENER_PRESETS",
    "analytics_job_runner",
    "backtest_engine",
    "compute_atr_series",
    "compute_ema_series",
    "compute_rsi_wilder_series",
    "compute_supertrend_series",
    "market_screener",
    "risk_limit_manager",
]
