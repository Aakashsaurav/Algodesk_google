"""
indicators/toolkit.py
=====================
Unified Technical Analysis Indicator Toolkit supporting:
1. TA-Lib (`talib`) when installed
2. `pandas-ta` (`pandas_ta`) when installed
3. `ta` (`ta` library) when installed
4. Pure O(N) Python fallbacks (zero dependencies, MacBook Air M2 RAM-friendly)

Custom screens in `screener/screens/*.py` and strategies in
`backtest/strategies/*.py` can use high-level methods (`tk.rsi(14)`,
`tk.macd()`, `tk.bbands()`, `tk.supertrend()`, `tk.adx()`, etc.) OR access
`tk.talib`, `tk.pta`, `tk.ta`, `tk.df`, `tk.np` directly when installed.
"""

from __future__ import annotations

import importlib
import math
import platform
import sys
from collections import deque
from typing import Any, Dict, List, Optional, Tuple


_MODULE_PROBE_CACHE: Dict[str, Tuple[Any, str, str]] = {}


def _probe_module(name: str, *, force: bool = False) -> Tuple[Any, str, str]:
    """
    Safely import an optional indicator/dataframe library and return
    `(module_or_None, version_str, error_str)`.

    Catches *all* exceptions (not just `ImportError`) so ABI mismatches,
    missing shared C libraries, or NumPy 2.x deprecations never crash startup
    and surface clear diagnostics in the UI.
    """
    if not force and name in _MODULE_PROBE_CACHE:
        return _MODULE_PROBE_CACHE[name]

    # Compatibility shim: pandas-ta <= 0.3.14b imports `from numpy import NaN as npNaN`,
    # which was removed in NumPy 2.0. Restore the alias safely before importing pandas_ta.
    if name == "pandas_ta":
        np_mod, _, _ = _probe_module("numpy", force=force)
        if np_mod is not None and not hasattr(np_mod, "NaN"):
            try:
                setattr(np_mod, "NaN", np_mod.nan)
            except Exception:
                pass

    try:
        mod = importlib.import_module(name)
        ver = str(
            getattr(mod, "__version__", "")
            or getattr(mod, "version", "")
            or ""
        ).strip()
        result = (mod, ver, "")
    except Exception as exc:
        result = (None, "", f"{type(exc).__name__}: {exc}")

    _MODULE_PROBE_CACHE[name] = result
    return result


def refresh_backend_cache() -> None:
    """Clear cached import probe results so newly installed packages are re-detected."""
    _MODULE_PROBE_CACHE.clear()


def _try_import(name: str) -> Any:
    mod, _, _ = _probe_module(name)
    return mod


def available_backends() -> Dict[str, bool]:
    """Probe installed optional TA and DataFrame libraries dynamically."""
    has_np = _try_import("numpy") is not None
    has_pd = _try_import("pandas") is not None
    return {
        "talib": (_try_import("talib") is not None) and has_np,
        "pandas_ta": (_try_import("pandas_ta") is not None) and has_pd,
        "ta": (_try_import("ta") is not None) and has_pd,
        "pandas": has_pd,
        "numpy": has_np,
        "builtin": True,
    }


def backend_diagnostics() -> Dict[str, Any]:
    """Return active Python interpreter path plus per-library import status & errors."""
    np_mod, np_ver, np_err = _probe_module("numpy")
    pd_mod, pd_ver, pd_err = _probe_module("pandas")
    tl_mod, tl_ver, tl_err = _probe_module("talib")
    pta_mod, pta_ver, pta_err = _probe_module("pandas_ta")
    ta_mod, ta_ver, ta_err = _probe_module("ta")

    def _entry(mod: Any, ver: str, err: str, dep_ok: bool = True, dep_name: str = "") -> Dict[str, Any]:
        avail = (mod is not None) and dep_ok
        reason = err
        if mod is not None and not dep_ok:
            reason = f"Requires {dep_name} to be installed alongside this library"
        return {"available": avail, "version": ver, "error": reason}

    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "backends": {
            "talib": _entry(tl_mod, tl_ver, tl_err, dep_ok=np_mod is not None, dep_name="numpy"),
            "pandas_ta": _entry(pta_mod, pta_ver, pta_err, dep_ok=pd_mod is not None, dep_name="pandas"),
            "ta": _entry(ta_mod, ta_ver, ta_err, dep_ok=pd_mod is not None, dep_name="pandas"),
            "pandas": _entry(pd_mod, pd_ver, pd_err),
            "numpy": _entry(np_mod, np_ver, np_err),
            "builtin": {"available": True, "version": "pure-python-O(N)", "error": ""},
        },
    }


def _to_clean_list(seq: Any, expected_len: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * expected_len
    if seq is None:
        return out
    vals = list(seq)
    for i in range(min(expected_len, len(vals))):
        v = vals[i]
        if v is None:
            continue
        try:
            fv = float(v)
            if not (math.isnan(fv) or math.isinf(fv)):
                out[i] = fv
        except (TypeError, ValueError):
            continue
    return out


# ── Pure O(N) Built-In Fallbacks ──────────────────────────────────────────────


def compute_sma_series(values: List[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / float(period)
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / float(period)
    return out


def compute_ema_series(values: List[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
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
        out[i] = 100.0 if avg_loss == 0 else (100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))
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
        if prev_dir == 1 and close < final_lower:
            prev_dir = -1
        elif prev_dir == -1 and close > final_upper:
            prev_dir = 1
        dirs[i] = prev_dir
        stops[i] = final_lower if prev_dir == 1 else final_upper
    return dirs, stops


def compute_macd_series(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, List[Optional[float]]]:
    n = len(closes)
    ema_fast = compute_ema_series(closes, fast)
    ema_slow = compute_ema_series(closes, slow)
    macd_line: List[Optional[float]] = [None] * n
    valid_vals: List[float] = []
    valid_indices: List[int] = []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            val = float(ema_fast[i]) - float(ema_slow[i])
            macd_line[i] = val
            valid_vals.append(val)
            valid_indices.append(i)
    sig_line: List[Optional[float]] = [None] * n
    hist_line: List[Optional[float]] = [None] * n
    if len(valid_vals) >= signal:
        ema_sig = compute_ema_series(valid_vals, signal)
        for j, orig_i in enumerate(valid_indices):
            s_val = ema_sig[j]
            if s_val is not None:
                sig_line[orig_i] = s_val
                m_val = macd_line[orig_i]
                if m_val is not None:
                    hist_line[orig_i] = m_val - s_val
    return {"macd": macd_line, "signal": sig_line, "hist": hist_line}


def compute_bbands_series(
    closes: List[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> Dict[str, List[Optional[float]]]:
    n = len(closes)
    upper: List[Optional[float]] = [None] * n
    middle = compute_sma_series(closes, period)
    lower: List[Optional[float]] = [None] * n
    if period <= 1 or n < period:
        return {"upper": upper, "middle": middle, "lower": lower}
    sum_x = sum(closes[:period])
    sum_x2 = sum(x * x for x in closes[:period])
    for i in range(period - 1, n):
        if i >= period:
            add_x = closes[i]
            rem_x = closes[i - period]
            sum_x += add_x - rem_x
            sum_x2 += (add_x * add_x) - (rem_x * rem_x)
        mean = sum_x / float(period)
        var = max(0.0, (sum_x2 / float(period)) - (mean * mean))
        sd = math.sqrt(var)
        upper[i] = mean + std_dev * sd
        lower[i] = mean - std_dev * sd
    return {"upper": upper, "middle": middle, "lower": lower}


def compute_adx_series(
    candles: List[Dict[str, Any]],
    period: int = 14,
) -> Dict[str, List[Optional[float]]]:
    n = len(candles)
    adx: List[Optional[float]] = [None] * n
    plus_di: List[Optional[float]] = [None] * n
    minus_di: List[Optional[float]] = [None] * n
    if n < period * 2:
        return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}

    trs = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        ph = float(candles[i - 1]["high"])
        pl = float(candles[i - 1]["low"])
        pc = float(candles[i - 1]["close"])
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        down = pl - l
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr_s = sum(trs[1 : period + 1])
    pdm_s = sum(plus_dm[1 : period + 1])
    mdm_s = sum(minus_dm[1 : period + 1])
    dx_vals: List[float] = []

    for i in range(period, n):
        if i > period:
            tr_s = tr_s - (tr_s / float(period)) + trs[i]
            pdm_s = pdm_s - (pdm_s / float(period)) + plus_dm[i]
            mdm_s = mdm_s - (mdm_s / float(period)) + minus_dm[i]
        pdi = (100.0 * pdm_s / tr_s) if tr_s > 0 else 0.0
        mdi = (100.0 * mdm_s / tr_s) if tr_s > 0 else 0.0
        plus_di[i] = pdi
        minus_di[i] = mdi
        dx = (100.0 * abs(pdi - mdi) / (pdi + mdi)) if (pdi + mdi) > 0 else 0.0
        dx_vals.append(dx)
        if len(dx_vals) == period:
            adx[i] = sum(dx_vals) / float(period)
        elif len(dx_vals) > period and adx[i - 1] is not None:
            adx[i] = ((float(adx[i - 1]) * (period - 1)) + dx) / float(period)

    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def compute_stoch_series(
    candles: List[Dict[str, Any]],
    k_period: int = 14,
    d_period: int = 3,
) -> Dict[str, List[Optional[float]]]:
    n = len(candles)
    k_out: List[Optional[float]] = [None] * n
    d_out: List[Optional[float]] = [None] * n
    if n < k_period or k_period <= 0:
        return {"k": k_out, "d": d_out}
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    dq_max: deque[int] = deque()
    dq_min: deque[int] = deque()
    k_valid: List[Tuple[int, float]] = []
    for i in range(n):
        while dq_max and highs[dq_max[-1]] <= highs[i]:
            dq_max.pop()
        dq_max.append(i)
        while dq_min and lows[dq_min[-1]] >= lows[i]:
            dq_min.pop()
        dq_min.append(i)
        while dq_max and dq_max[0] <= i - k_period:
            dq_max.popleft()
        while dq_min and dq_min[0] <= i - k_period:
            dq_min.popleft()
        if i >= k_period - 1:
            hh = highs[dq_max[0]]
            ll = lows[dq_min[0]]
            c_i = float(candles[i]["close"])
            kv = ((c_i - ll) / (hh - ll)) * 100.0 if hh > ll else 50.0
            k_out[i] = kv
            k_valid.append((i, kv))
    if len(k_valid) >= d_period and d_period > 0:
        win = sum(x[1] for x in k_valid[:d_period])
        d_out[k_valid[d_period - 1][0]] = win / float(d_period)
        for j in range(d_period, len(k_valid)):
            win += k_valid[j][1] - k_valid[j - d_period][1]
            d_out[k_valid[j][0]] = win / float(d_period)
    return {"k": k_out, "d": d_out}


def compute_roc_series(closes: List[float], period: int = 10) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if period <= 0:
        return out
    for i in range(period, n):
        prev = closes[i - period]
        if prev != 0:
            out[i] = ((closes[i] - prev) / prev) * 100.0
    return out


def rolling_extrema(values: List[float], window: int, find_max: bool) -> List[Optional[float]]:
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


def compute_vcp_metrics(candles: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
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


# ── Unified IndicatorToolkit (TA-Lib -> pandas-ta -> ta -> Builtin) ───────────


class IndicatorToolkit:
    """
    Facade over OHLCV bars providing consistent technical indicators backed by:
    TA-Lib -> pandas-ta -> `ta` -> pure-Python O(N) fallbacks.
    Also caches series per indicator call during a screen/backtest run.
    """

    def __init__(self, candles: List[Dict[str, Any]], backend: str = "auto") -> None:
        self.candles = candles
        self.n = len(candles)
        self.backend_pref = (backend or "auto").strip().lower()
        self.opens = [float(c["open"]) for c in candles]
        self.highs = [float(c["high"]) for c in candles]
        self.lows = [float(c["low"]) for c in candles]
        self.closes = [float(c["close"]) for c in candles]
        self.volumes = [float(c.get("volume") or 0) for c in candles]
        self._cache: Dict[Tuple[Any, ...], Any] = {}
        self.recorded_series: Dict[str, List[Optional[float]]] = {}

        self._talib = _try_import("talib")
        self._pta = _try_import("pandas_ta")
        self._ta = _try_import("ta")
        self._pd = _try_import("pandas")
        self._np = _try_import("numpy")
        self._df_cached: Any = None

    @property
    def has_talib(self) -> bool:
        return self._talib is not None and self._np is not None

    @property
    def has_pandas_ta(self) -> bool:
        return self._pta is not None and self._pd is not None

    @property
    def has_ta(self) -> bool:
        return self._ta is not None and self._pd is not None

    @property
    def talib(self) -> Any:
        return self._talib

    @property
    def pta(self) -> Any:
        return self._pta

    @property
    def ta(self) -> Any:
        return self._ta

    @property
    def np(self) -> Any:
        return self._np

    @property
    def df(self) -> Any:
        if self._df_cached is not None:
            return self._df_cached
        if self._pd is None:
            raise RuntimeError("pandas is not installed; install requirements-analytics.txt for DataFrame mode.")
        self._df_cached = self._pd.DataFrame(self.candles)
        return self._df_cached

    def _np_arr(self, data: List[float]) -> Any:
        return self._np.asarray(data, dtype=float)

    def _use(self, lib_name: str) -> bool:
        if self.backend_pref == "builtin":
            return False
        if self.backend_pref != "auto":
            return self.backend_pref == lib_name
        return True

    def _record_series(self, label: str, values: List[Optional[float]]) -> None:
        if label not in self.recorded_series:
            self.recorded_series[label] = values

    def get_recorded_series(self) -> Dict[str, List[Optional[float]]]:
        return dict(self.recorded_series)

    def sma(self, period: int = 20) -> List[Optional[float]]:
        key = ("sma", period)
        if key in self._cache:
            return self._cache[key]
        out: Optional[List[Optional[float]]] = None
        if self.has_talib and self._use("talib"):
            out = _to_clean_list(self._talib.SMA(self._np_arr(self.closes), timeperiod=period), self.n)
        elif self.has_pandas_ta and self._use("pandas_ta"):
            s = self._pta.sma(self.df["close"].astype(float), length=period)
            if s is not None:
                out = _to_clean_list(s.tolist(), self.n)
        elif self.has_ta and self._use("ta"):
            s = self._ta.trend.SMAIndicator(close=self.df["close"].astype(float), window=period).sma_indicator()
            out = _to_clean_list(s.tolist(), self.n)
        if out is None:
            out = compute_sma_series(self.closes, period)
        self._cache[key] = out
        self._record_series(f"SMA({period})", out)
        return out

    def ema(self, period: int = 20) -> List[Optional[float]]:
        key = ("ema", period)
        if key in self._cache:
            return self._cache[key]
        out: Optional[List[Optional[float]]] = None
        if self.has_talib and self._use("talib"):
            out = _to_clean_list(self._talib.EMA(self._np_arr(self.closes), timeperiod=period), self.n)
        elif self.has_pandas_ta and self._use("pandas_ta"):
            s = self._pta.ema(self.df["close"].astype(float), length=period)
            if s is not None:
                out = _to_clean_list(s.tolist(), self.n)
        elif self.has_ta and self._use("ta"):
            s = self._ta.trend.EMAIndicator(close=self.df["close"].astype(float), window=period).ema_indicator()
            out = _to_clean_list(s.tolist(), self.n)
        if out is None:
            out = compute_ema_series(self.closes, period)
        self._cache[key] = out
        self._record_series(f"EMA({period})", out)
        return out

    def rsi(self, period: int = 14) -> List[Optional[float]]:
        key = ("rsi", period)
        if key in self._cache:
            return self._cache[key]
        out: Optional[List[Optional[float]]] = None
        if self.has_talib and self._use("talib"):
            out = _to_clean_list(self._talib.RSI(self._np_arr(self.closes), timeperiod=period), self.n)
        elif self.has_pandas_ta and self._use("pandas_ta"):
            s = self._pta.rsi(self.df["close"].astype(float), length=period)
            if s is not None:
                out = _to_clean_list(s.tolist(), self.n)
        elif self.has_ta and self._use("ta"):
            s = self._ta.momentum.RSIIndicator(close=self.df["close"].astype(float), window=period).rsi()
            out = _to_clean_list(s.tolist(), self.n)
        if out is None:
            out = compute_rsi_wilder_series(self.closes, period)
        self._cache[key] = out
        self._record_series(f"RSI({period})", out)
        return out

    def atr(self, period: int = 14) -> Tuple[List[Optional[float]], List[Optional[float]]]:
        key = ("atr", period)
        if key in self._cache:
            return self._cache[key]
        atr_vals: Optional[List[Optional[float]]] = None
        if self.has_talib and self._use("talib"):
            arr = self._talib.ATR(
                self._np_arr(self.highs),
                self._np_arr(self.lows),
                self._np_arr(self.closes),
                timeperiod=period,
            )
            atr_vals = _to_clean_list(arr, self.n)
        elif self.has_pandas_ta and self._use("pandas_ta"):
            s = self._pta.atr(
                self.df["high"].astype(float),
                self.df["low"].astype(float),
                self.df["close"].astype(float),
                length=period,
            )
            if s is not None:
                atr_vals = _to_clean_list(s.tolist(), self.n)
        elif self.has_ta and self._use("ta") and self.n >= period:
            s = self._ta.volatility.AverageTrueRange(
                high=self.df["high"].astype(float),
                low=self.df["low"].astype(float),
                close=self.df["close"].astype(float),
                window=period,
            ).average_true_range()
            atr_vals = _to_clean_list(s.tolist(), self.n)
        if atr_vals is None:
            res = compute_atr_series(self.candles, period)
            self._cache[key] = res
            self._record_series(f"ATR({period})", res[0])
            self._record_series(f"ATR_Pct({period})", res[1])
            return res
        atr_pct = [
            ((atr_vals[i] / self.closes[i]) * 100.0 if (atr_vals[i] is not None and self.closes[i] > 0) else None)
            for i in range(self.n)
        ]
        res = (atr_vals, atr_pct)
        self._cache[key] = res
        self._record_series(f"ATR({period})", atr_vals)
        self._record_series(f"ATR_Pct({period})", atr_pct)
        return res

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, List[Optional[float]]]:
        key = ("macd", fast, slow, signal)
        if key in self._cache:
            return self._cache[key]
        out: Optional[Dict[str, List[Optional[float]]]] = None
        if self.has_talib and self._use("talib"):
            m, s, h = self._talib.MACD(
                self._np_arr(self.closes),
                fastperiod=fast,
                slowperiod=slow,
                signalperiod=signal,
            )
            out = {
                "macd": _to_clean_list(m, self.n),
                "signal": _to_clean_list(s, self.n),
                "hist": _to_clean_list(h, self.n),
            }
        elif self.has_pandas_ta and self._use("pandas_ta"):
            mdf = self._pta.macd(self.df["close"].astype(float), fast=fast, slow=slow, signal=signal)
            if mdf is not None and len(mdf.columns) >= 3:
                out = {
                    "macd": _to_clean_list(mdf.iloc[:, 0].tolist(), self.n),
                    "hist": _to_clean_list(mdf.iloc[:, 1].tolist(), self.n),
                    "signal": _to_clean_list(mdf.iloc[:, 2].tolist(), self.n),
                }
        elif self.has_ta and self._use("ta"):
            mi = self._ta.trend.MACD(
                close=self.df["close"].astype(float),
                window_slow=slow,
                window_fast=fast,
                window_sign=signal,
            )
            out = {
                "macd": _to_clean_list(mi.macd().tolist(), self.n),
                "signal": _to_clean_list(mi.macd_signal().tolist(), self.n),
                "hist": _to_clean_list(mi.macd_diff().tolist(), self.n),
            }
        if out is None:
            out = compute_macd_series(self.closes, fast=fast, slow=slow, signal=signal)
        self._cache[key] = out
        self._record_series(f"MACD({fast},{slow},{signal})", out["macd"])
        self._record_series(f"MACD_Signal({fast},{slow},{signal})", out["signal"])
        self._record_series(f"MACD_Hist({fast},{slow},{signal})", out["hist"])
        return out

    def bbands(self, period: int = 20, std_dev: float = 2.0) -> Dict[str, List[Optional[float]]]:
        key = ("bbands", period, std_dev)
        if key in self._cache:
            return self._cache[key]
        out: Optional[Dict[str, List[Optional[float]]]] = None
        if self.has_talib and self._use("talib"):
            u, m, l = self._talib.BBANDS(
                self._np_arr(self.closes),
                timeperiod=period,
                nbdevup=std_dev,
                nbdevdn=std_dev,
            )
            out = {
                "upper": _to_clean_list(u, self.n),
                "middle": _to_clean_list(m, self.n),
                "lower": _to_clean_list(l, self.n),
            }
        elif self.has_pandas_ta and self._use("pandas_ta"):
            bdf = self._pta.bbands(self.df["close"].astype(float), length=period, std=std_dev)
            if bdf is not None and len(bdf.columns) >= 3:
                out = {
                    "lower": _to_clean_list(bdf.iloc[:, 0].tolist(), self.n),
                    "middle": _to_clean_list(bdf.iloc[:, 1].tolist(), self.n),
                    "upper": _to_clean_list(bdf.iloc[:, 2].tolist(), self.n),
                }
        elif self.has_ta and self._use("ta"):
            bb = self._ta.volatility.BollingerBands(
                close=self.df["close"].astype(float),
                window=period,
                window_dev=std_dev,
            )
            out = {
                "upper": _to_clean_list(bb.bollinger_hband().tolist(), self.n),
                "middle": _to_clean_list(bb.bollinger_mavg().tolist(), self.n),
                "lower": _to_clean_list(bb.bollinger_lband().tolist(), self.n),
            }
        if out is None:
            out = compute_bbands_series(self.closes, period=period, std_dev=std_dev)
        self._cache[key] = out
        self._record_series(f"BB_Upper({period},{std_dev:g})", out["upper"])
        self._record_series(f"BB_Middle({period},{std_dev:g})", out["middle"])
        self._record_series(f"BB_Lower({period},{std_dev:g})", out["lower"])
        return out

    def supertrend(self, period: int = 10, multiplier: float = 3.0) -> Tuple[List[int], List[Optional[float]]]:
        key = ("supertrend", period, multiplier)
        if key in self._cache:
            return self._cache[key]
        res = compute_supertrend_series(self.candles, period=period, multiplier=multiplier)
        self._cache[key] = res
        self._record_series(f"Supertrend_Dir({period},{multiplier:g})", [float(d) if d != 0 else None for d in res[0]])
        self._record_series(f"Supertrend({period},{multiplier:g})", res[1])
        return res

    def adx(self, period: int = 14) -> Dict[str, List[Optional[float]]]:
        key = ("adx", period)
        if key in self._cache:
            return self._cache[key]
        out: Optional[Dict[str, List[Optional[float]]]] = None
        if self.has_talib and self._use("talib"):
            adx_v = self._talib.ADX(self._np_arr(self.highs), self._np_arr(self.lows), self._np_arr(self.closes), timeperiod=period)
            pdi_v = self._talib.PLUS_DI(self._np_arr(self.highs), self._np_arr(self.lows), self._np_arr(self.closes), timeperiod=period)
            mdi_v = self._talib.MINUS_DI(self._np_arr(self.highs), self._np_arr(self.lows), self._np_arr(self.closes), timeperiod=period)
            out = {
                "adx": _to_clean_list(adx_v, self.n),
                "plus_di": _to_clean_list(pdi_v, self.n),
                "minus_di": _to_clean_list(mdi_v, self.n),
            }
        elif self.has_ta and self._use("ta") and self.n >= period * 2:
            ai = self._ta.trend.ADXIndicator(
                high=self.df["high"].astype(float),
                low=self.df["low"].astype(float),
                close=self.df["close"].astype(float),
                window=period,
            )
            out = {
                "adx": _to_clean_list(ai.adx().tolist(), self.n),
                "plus_di": _to_clean_list(ai.adx_pos().tolist(), self.n),
                "minus_di": _to_clean_list(ai.adx_neg().tolist(), self.n),
            }
        if out is None:
            out = compute_adx_series(self.candles, period=period)
        self._cache[key] = out
        self._record_series(f"ADX({period})", out["adx"])
        self._record_series(f"Plus_DI({period})", out["plus_di"])
        self._record_series(f"Minus_DI({period})", out["minus_di"])
        return out

    def stoch(self, k_period: int = 14, d_period: int = 3) -> Dict[str, List[Optional[float]]]:
        key = ("stoch", k_period, d_period)
        if key in self._cache:
            return self._cache[key]
        res = compute_stoch_series(self.candles, k_period=k_period, d_period=d_period)
        self._cache[key] = res
        self._record_series(f"Stoch_K({k_period},{d_period})", res["k"])
        self._record_series(f"Stoch_D({k_period},{d_period})", res["d"])
        return res

    def roc(self, period: int = 10) -> List[Optional[float]]:
        key = ("roc", period)
        if key in self._cache:
            return self._cache[key]
        out: Optional[List[Optional[float]]] = None
        if self.has_talib and self._use("talib"):
            out = _to_clean_list(self._talib.ROC(self._np_arr(self.closes), timeperiod=period), self.n)
        elif self.has_ta and self._use("ta"):
            s = self._ta.momentum.ROCIndicator(close=self.df["close"].astype(float), window=period).roc()
            out = _to_clean_list(s.tolist(), self.n)
        if out is None:
            out = compute_roc_series(self.closes, period=period)
        self._cache[key] = out
        self._record_series(f"ROC({period})", out)
        return out

    def rolling_high(self, window: int) -> List[Optional[float]]:
        key = ("rolling_high", window)
        if key not in self._cache:
            self._cache[key] = rolling_extrema(self.highs, window, find_max=True)
            self._record_series(f"RollingHigh({window})", self._cache[key])
        return self._cache[key]

    def rolling_low(self, window: int) -> List[Optional[float]]:
        key = ("rolling_low", window)
        if key not in self._cache:
            self._cache[key] = rolling_extrema(self.lows, window, find_max=False)
            self._record_series(f"RollingLow({window})", self._cache[key])
        return self._cache[key]

    def vcp(self) -> Dict[str, Optional[float]]:
        if "vcp" not in self._cache:
            self._cache["vcp"] = compute_vcp_metrics(self.candles)
        return self._cache["vcp"]


__all__ = [
    "IndicatorToolkit",
    "available_backends",
    "backend_diagnostics",
    "refresh_backend_cache",
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
