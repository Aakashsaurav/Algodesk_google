"""
technicals/analyzer.py
======================
Streak-style Multi-Timeframe Technicals Dashboard Engine.

Uses ONLY `HistoricalDataManager` (canonical 1m/1d cache + local resampling)
and `IndicatorToolkit` (`TA-Lib` / `pandas-ta` / `ta` / pure O(N)) plus
`screener/sr_engine.py` to compute:
- Summary Sentiment Gauge (`Strong Sell` -> `Strong Buy`)
- Moving Averages Table (SMA & EMA 10, 20, 50, 100, 200)
- Oscillators Table (RSI 14, MACD, ADX 14, Stochastic, ROC, Supertrend, ATR%)
- Classic & Camarilla Pivot Points
- Major / Minor Support & Resistance Zones
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from config import IST_TZ, config
from core.historical import (
    HistoricalDataManager,
    historical_data_manager,
    normalize_timeframe,
    resolve_base_timeframe,
)
from indicators.toolkit import IndicatorToolkit
from screener.sr_engine import analyze_single_timeframe


def _sentiment_label(buy_count: int, sell_count: int, total: int) -> str:
    if total <= 0:
        return "Neutral"
    score = (buy_count - sell_count) / float(total)
    if score >= 0.55:
        return "Strong Buy"
    if score >= 0.15:
        return "Buy"
    if score <= -0.55:
        return "Strong Sell"
    if score <= -0.15:
        return "Sell"
    return "Neutral"


def _compute_pivots(prev_bar: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    h = float(prev_bar["high"])
    l = float(prev_bar["low"])
    c = float(prev_bar["close"])
    rng = h - l
    p = (h + l + c) / 3.0
    classic = {
        "r3": round(h + 2.0 * (p - l), 2),
        "r2": round(p + rng, 2),
        "r1": round(2.0 * p - l, 2),
        "pivot": round(p, 2),
        "s1": round(2.0 * p - h, 2),
        "s2": round(p - rng, 2),
        "s3": round(l - 2.0 * (h - p), 2),
    }
    camarilla = {
        "r3": round(c + rng * 1.1 / 4.0, 2),
        "r2": round(c + rng * 1.1 / 6.0, 2),
        "r1": round(c + rng * 1.1 / 12.0, 2),
        "pivot": round(p, 2),
        "s1": round(c - rng * 1.1 / 12.0, 2),
        "s2": round(c - rng * 1.1 / 6.0, 2),
        "s3": round(c - rng * 1.1 / 4.0, 2),
    }
    return {"classic": classic, "camarilla": camarilla}


class TechnicalsAnalyzer:
    def __init__(self, hist_manager: Optional[HistoricalDataManager] = None) -> None:
        self._hist = hist_manager or historical_data_manager

    def analyze(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        provider: Optional[str] = None,
        backend: str = "auto",
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        clean_sym = (symbol or "").strip().upper()
        if not clean_sym:
            raise ValueError("symbol is required for technicals analysis.")
        clean_exch = (exchange or "NSE").strip().upper()
        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()

        today_ist = datetime.now(tz=IST_TZ).date()
        to_date = today_ist.isoformat()
        lookback_days = 420 if base_tf == "1day" else 30
        from_date = (today_ist - timedelta(days=lookback_days)).isoformat()

        dl_meta = self._hist.download(
            symbol=clean_sym,
            exchange=clean_exch,
            timeframe=tf,
            from_date=from_date,
            to_date=to_date,
            provider=eff_provider,
            fetcher=fetcher,
        )
        records = self._hist.get_records(
            symbol=dl_meta["symbol"],
            exchange=dl_meta["exchange"],
            timeframe=dl_meta["timeframe"],
            from_date=None,
            to_date=to_date,
            view="full",
        )
        rows = records.get("rows", [])
        if len(rows) < 20:
            raise ValueError(f"Insufficient historical bars ({len(rows)}) for technicals on {clean_sym} ({tf}).")

        tk = IndicatorToolkit(rows, backend=backend)
        last = rows[-1]
        prev = rows[-2]
        ltp = float(last["close"])
        prev_close = float(prev["close"])
        chg_pct = ((ltp - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

        # 1) Moving Averages (SMA & EMA 10, 20, 50, 100, 200)
        ma_rows: List[Dict[str, Any]] = []
        ma_buy = ma_sell = ma_neutral = 0
        for period in (10, 20, 50, 100, 200):
            for kind, series in (("EMA", tk.ema(period)), ("SMA", tk.sma(period))):
                val = series[-1]
                if val is None:
                    continue
                if ltp > val * 1.001:
                    sig = "Buy"
                    ma_buy += 1
                elif ltp < val * 0.999:
                    sig = "Sell"
                    ma_sell += 1
                else:
                    sig = "Neutral"
                    ma_neutral += 1
                ma_rows.append({"name": f"{kind} ({period})", "value": round(float(val), 2), "signal": sig})

        # 2) Oscillators
        osc_rows: List[Dict[str, Any]] = []
        osc_buy = osc_sell = osc_neutral = 0

        def _add_osc(name: str, val: Optional[float], sig: str) -> None:
            nonlocal osc_buy, osc_sell, osc_neutral
            if val is None:
                return
            if sig == "Buy":
                osc_buy += 1
            elif sig == "Sell":
                osc_sell += 1
            else:
                osc_neutral += 1
            osc_rows.append({"name": name, "value": round(float(val), 2), "signal": sig})

        rsi_v = tk.rsi(14)[-1]
        if rsi_v is not None:
            _add_osc("RSI (14)", rsi_v, "Buy" if rsi_v > 55 else ("Sell" if rsi_v < 45 else "Neutral"))

        macd_d = tk.macd()
        m_val, ms_val = macd_d["macd"][-1], macd_d["signal"][-1]
        if m_val is not None and ms_val is not None:
            _add_osc("MACD (12, 26, 9)", m_val, "Buy" if m_val > ms_val else ("Sell" if m_val < ms_val else "Neutral"))

        adx_d = tk.adx(14)
        adx_v, pdi_v, mdi_v = adx_d["adx"][-1], adx_d["plus_di"][-1], adx_d["minus_di"][-1]
        if adx_v is not None and pdi_v is not None and mdi_v is not None:
            adx_sig = "Neutral"
            if adx_v >= 20:
                adx_sig = "Buy" if pdi_v > mdi_v else "Sell"
            _add_osc("ADX (14)", adx_v, adx_sig)

        st_d = tk.stoch(14, 3)
        stk_v, std_v = st_d["k"][-1], st_d["d"][-1]
        if stk_v is not None and std_v is not None:
            _add_osc("Stochastic %K (14, 3)", stk_v, "Buy" if stk_v > std_v and stk_v < 80 else ("Sell" if stk_v < std_v and stk_v > 20 else "Neutral"))

        roc_v = tk.roc(10)[-1]
        if roc_v is not None:
            _add_osc("ROC (10)", roc_v, "Buy" if roc_v > 0.5 else ("Sell" if roc_v < -0.5 else "Neutral"))

        st_dirs, st_stops = tk.supertrend(10, 3.0)
        if st_stops[-1] is not None:
            _add_osc("Supertrend (10, 3)", st_stops[-1], "Buy" if st_dirs[-1] == 1 else ("Sell" if st_dirs[-1] == -1 else "Neutral"))

        atr_vals, atr_pcts = tk.atr(14)
        if atr_pcts[-1] is not None:
            _add_osc("ATR % (14)", atr_pcts[-1], "Neutral")

        tot_buy = ma_buy + osc_buy
        tot_sell = ma_sell + osc_sell
        tot_neutral = ma_neutral + osc_neutral
        tot_all = tot_buy + tot_sell + tot_neutral

        # 3) Pivot points + Support/Resistance major zones
        pivots = _compute_pivots(prev)
        sr_res = analyze_single_timeframe(rows, timeframe=tf)
        raw_zones = sr_res.get("zones", []) if isinstance(sr_res, dict) else getattr(sr_res, "zones", [])

        def _zf(obj: Any, key: str, default: Any = None) -> Any:
            return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

        sr_zones: List[Dict[str, Any]] = []
        major_supports: List[Dict[str, Any]] = []
        major_resistances: List[Dict[str, Any]] = []
        for z in raw_zones:
            z_type = str(_zf(z, "zone_type") or _zf(z, "type") or "").upper()
            z_cls = str(_zf(z, "classification") or "").upper()
            if z_cls not in {"MAJOR", "MINOR"}:
                continue
            rep = round(float(_zf(z, "representative_price", 0.0) or 0.0), 2)
            lo = round(float(_zf(z, "lower", rep) or rep), 2)
            hi = round(float(_zf(z, "upper", rep) or rep), 2)
            strength = round(float(_zf(z, "strength", _zf(z, "strength_score", 0.0)) or 0.0), 1)
            touches = int(_zf(z, "touch_count", _zf(z, "touches", 0)) or 0)
            entry = {
                "type": z_type,
                "classification": z_cls,
                "tier": z_cls,
                "price": rep,
                "representative_price": rep,
                "lower": lo,
                "upper": hi,
                "strength": strength,
                "touches": touches,
            }
            sr_zones.append(entry)
            if z_type == "SUPPORT" and len(major_supports) < 4:
                major_supports.append(entry)
            elif z_type == "RESISTANCE" and len(major_resistances) < 4:
                major_resistances.append(entry)

        sr_zones.sort(key=lambda x: x["strength"], reverse=True)
        tot_rating = _sentiment_label(tot_buy, tot_sell, tot_all)
        ma_rating = _sentiment_label(ma_buy, ma_sell, ma_buy + ma_sell + ma_neutral)
        osc_rating = _sentiment_label(osc_buy, osc_sell, osc_buy + osc_sell + osc_neutral)
        last_ts = str(last.get("ts_ist") or "")

        return {
            "ok": True,
            "symbol": dl_meta["symbol"],
            "exchange": dl_meta["exchange"],
            "timeframe": tf,
            "base_timeframe": base_tf,
            "provider": str(provider or dl_meta.get("provider") or "cache"),
            "bars_analyzed": len(rows),
            "ltp": round(ltp, 2),
            "close": round(ltp, 2),
            "change_pct": round(chg_pct, 2),
            "last_ts_ist": last_ts,
            "as_of_date": str(last.get("trade_date") or last_ts[:10]),
            "summary": {
                "sentiment": tot_rating,
                "rating": tot_rating,
                "buy": tot_buy,
                "neutral": tot_neutral,
                "sell": tot_sell,
                "score": round(((tot_buy - tot_sell) / max(1, tot_all)) * 100.0, 1),
            },
            "moving_averages": {
                "sentiment": ma_rating,
                "rating": ma_rating,
                "buy": ma_buy,
                "neutral": ma_neutral,
                "sell": ma_sell,
                "summary": {"rating": ma_rating, "buy": ma_buy, "neutral": ma_neutral, "sell": ma_sell},
                "rows": ma_rows,
            },
            "oscillators": {
                "sentiment": osc_rating,
                "rating": osc_rating,
                "buy": osc_buy,
                "neutral": osc_neutral,
                "sell": osc_sell,
                "summary": {"rating": osc_rating, "buy": osc_buy, "neutral": osc_neutral, "sell": osc_sell},
                "rows": osc_rows,
            },
            "pivots": pivots,
            "sr_zones": sr_zones[:8],
            "support_zones": major_supports,
            "resistance_zones": major_resistances,
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }


technicals_analyzer = TechnicalsAnalyzer()

__all__ = ["TechnicalsAnalyzer", "technicals_analyzer"]
