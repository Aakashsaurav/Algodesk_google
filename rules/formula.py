"""
rules/formula.py
================
Safe, zero-eval AST compiler and evaluator for Streak-style mathematical
expressions and condition formulas used in Advance mode for both Scanners
and Strategies.

Supported examples:
  - Full boolean formula conditions:
      High(-2) > Max(6, High(0))
      EMA(high, 20, 0) >= Period max(14, Close(-2))
      (Close(0) - Open(0)) / Open(0) * 100 > 1.5 and Volume(0) > 1.5 * Vol_SMA(20, 0)
      Crosses_Above(EMA(close, 20, 0), EMA(close, 50, 0))
  - Value expressions (inside Left / Right operand pills):
      EMA(high, 20, 0)
      Period max(14, Close(-2))
      (High(0) + Low(0)) / 2
      Close(0) * 1.02

Security:
  - Uses `ast.parse(..., mode="eval")` with an explicit strict whitelist of
    AST node types and pure numerical functions.
  - NEVER executes `eval()`, `exec()`, attribute access, imports, or arbitrary
    Python code.
"""

from __future__ import annotations

import ast
import math
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple, Union

from indicators.toolkit import (
    IndicatorToolkit,
    compute_ema_series,
    compute_rsi_wilder_series,
    compute_sma_series,
)


SeriesVal = List[Optional[float]]
BoolSeries = List[bool]

_MAX_FORMULA_LEN = 1200
_MAX_AST_NODES = 200

# Source aliases allowed as the first argument to EMA/SMA/WMA/RSI, e.g. EMA(high, 20, 0)
_SOURCE_NAMES = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"}


def _clean_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        fv = float(v)
        if math.isnan(fv) or math.isinf(fv):
            return None
        return fv
    except (TypeError, ValueError):
        return None


def _shift_series(series: SeriesVal, offset: int) -> SeriesVal:
    """
    Shift a series by candle offset where:
      - 0  = current bar
      - -1 = previous bar (1 bar ago)
      - -2 = 2 bars ago
    Any positive integer is also defensively treated as past lag (`-abs(offset)`)
    so formulas can never introduce future look-ahead bias.
    """
    n = len(series)
    off = -abs(int(offset))
    if off == 0:
        return list(series)
    out: SeriesVal = [None] * n
    for i in range(n):
        src = i + off
        if 0 <= src < n:
            out[i] = series[src]
    return out


def _compute_wma_from_values(values: List[Optional[float]], period: int) -> SeriesVal:
    n = len(values)
    out: SeriesVal = [None] * n
    p = max(1, int(period))
    if n < p:
        return out
    denom = float(p * (p + 1) // 2)
    for i in range(p - 1, n):
        acc = 0.0
        ok = True
        for k in range(p):
            v = values[i - p + 1 + k]
            if v is None:
                ok = False
                break
            acc += float(v) * float(k + 1)
        if ok:
            out[i] = acc / denom
    return out


def _compute_sma_opt(values: SeriesVal, period: int) -> SeriesVal:
    n = len(values)
    p = max(1, int(period))
    out: SeriesVal = [None] * n
    if n < p:
        return out
    # Fast path when no None values
    if all(v is not None for v in values):
        return compute_sma_series([float(v) for v in values], p)  # type: ignore[arg-type]
    for i in range(p - 1, n):
        win = values[i - p + 1 : i + 1]
        if any(v is None for v in win):
            continue
        out[i] = sum(float(v) for v in win) / float(p)  # type: ignore[arg-type]
    return out


def _compute_ema_opt(values: SeriesVal, period: int) -> SeriesVal:
    n = len(values)
    p = max(1, int(period))
    out: SeriesVal = [None] * n
    if n < p:
        return out
    # Find first contiguous run of p valid values to seed EMA
    start = -1
    run = 0
    for i in range(n):
        if values[i] is not None:
            run += 1
            if run == p:
                start = i - p + 1
                break
        else:
            run = 0
    if start < 0:
        return out
    if start == 0 and all(v is not None for v in values):
        return compute_ema_series([float(v) for v in values], p)  # type: ignore[arg-type]
    seed = sum(float(values[j]) for j in range(start, start + p)) / float(p)  # type: ignore[arg-type]
    out[start + p - 1] = seed
    k = 2.0 / float(p + 1)
    prev = seed
    for i in range(start + p, n):
        v = values[i]
        if v is None:
            out[i] = prev
        else:
            prev = (float(v) - prev) * k + prev
            out[i] = prev
    return out


def _compute_rsi_opt(values: SeriesVal, period: int) -> SeriesVal:
    n = len(values)
    p = max(1, int(period))
    if n <= p:
        return [None] * n
    if all(v is not None for v in values):
        return compute_rsi_wilder_series([float(v) for v in values], p)  # type: ignore[arg-type]
    out: SeriesVal = [None] * n
    # Find first valid index
    first = next((i for i, v in enumerate(values) if v is not None), n)
    clean = [float(values[i]) for i in range(first, n) if values[i] is not None]
    if len(clean) <= p:
        return out
    sub = compute_rsi_wilder_series(clean, p)
    k = 0
    for i in range(first, n):
        if values[i] is not None:
            out[i] = sub[k]
            k += 1
    return out


def _rolling_extrema_inclusive(values: SeriesVal, period: int, find_max: bool) -> SeriesVal:
    """
    O(N) rolling maximum or minimum over `period` bars inclusive of bar `i`:
      out[i] = max/min(values[i - period + 1 .. i])
    Matches Streak's `Period max(14, Close(-2))` and `Max(6, High(0))`.
    """
    n = len(values)
    p = max(1, int(period))
    out: SeriesVal = [None] * n
    if n == 0:
        return out
    if p == 1:
        return list(values)
    dq: deque[int] = deque()
    valid_count = 0
    for i in range(n):
        v = values[i]
        if v is not None:
            valid_count += 1
            if find_max:
                while dq and (values[dq[-1]] is None or float(values[dq[-1]]) <= float(v)):  # type: ignore[arg-type]
                    dq.pop()
            else:
                while dq and (values[dq[-1]] is None or float(values[dq[-1]]) >= float(v)):  # type: ignore[arg-type]
                    dq.pop()
            dq.append(i)
        if i >= p and values[i - p] is not None:
            valid_count -= 1
        while dq and dq[0] <= i - p:
            dq.popleft()
        if i >= p - 1 and dq and valid_count > 0:
            out[i] = values[dq[0]]
    return out


def _rolling_sum(values: SeriesVal, period: int) -> SeriesVal:
    n = len(values)
    p = max(1, int(period))
    out: SeriesVal = [None] * n
    if n < p:
        return out
    for i in range(p - 1, n):
        win = values[i - p + 1 : i + 1]
        if any(v is None for v in win):
            continue
        out[i] = sum(float(v) for v in win)  # type: ignore[arg-type]
    return out


def _compute_vwap_series(candles: List[Dict[str, Any]], period: int = 20) -> SeriesVal:
    n = len(candles)
    p = max(1, int(period))
    out: SeriesVal = [None] * n
    if n == 0:
        return out
    tpv = [0.0] * n
    vols = [0.0] * n
    for i, c in enumerate(candles):
        tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        vol = max(0.0, float(c.get("volume") or 0.0))
        tpv[i] = tp * vol
        vols[i] = vol
    sum_tpv = 0.0
    sum_vol = 0.0
    for i in range(n):
        sum_tpv += tpv[i]
        sum_vol += vols[i]
        if i >= p:
            sum_tpv -= tpv[i - p]
            sum_vol -= vols[i - p]
        out[i] = (sum_tpv / sum_vol) if sum_vol > 0 else float(candles[i]["close"])
    return out


def normalize_formula_text(expr: str) -> str:
    """
    Normalize human-readable Streak phrasing into valid Python-AST arithmetic &
    comparison syntax while preserving user intent.
    """
    text = (expr or "").strip()
    if not text:
        raise ValueError("Formula expression cannot be empty.")
    if len(text) > _MAX_FORMULA_LEN:
        raise ValueError(f"Formula exceeds maximum length of {_MAX_FORMULA_LEN} characters.")

    # Replace multi-word Streak function names with safe identifiers
    replacements = [
        (r"\bperiod\s+max\s*\(", "period_max("),
        (r"\bperiod\s+min\s*\(", "period_min("),
        (r"\bvol\s+sma\s*\(", "vol_sma("),
        (r"\bvolume\s+sma\s*\(", "vol_sma("),
        (r"\brsi\s+ma\s*\(", "rsi_ma("),
        (r"\bmacd\s+signal\s*\(", "macd_signal("),
        (r"\bmacd\s+hist\s*\(", "macd_hist("),
        (r"\bbb\s+upper\s*\(", "bb_upper("),
        (r"\bbb\s+lower\s*\(", "bb_lower("),
        (r"\bbb\s+middle\s*\(", "bb_middle("),
        (r"\bsupertrend\s+dir\s*\(", "supertrend_dir("),
        (r"\bcrosses\s+above\s*\(", "crosses_above("),
        (r"\bcrosses\s+below\s*\(", "crosses_below("),
        # Multi-word Streak comparators
        (r"\bhigher\s+than\s+equal\s+to\b", ">="),
        (r"\bgreater\s+than\s+or\s+equal\s+to\b", ">="),
        (r"\bgreater\s+or\s+equal\b", ">="),
        (r"\blower\s+than\s+equal\s+to\b", "<="),
        (r"\bless\s+than\s+or\s+equal\s+to\b", "<="),
        (r"\blower\s+or\s+equal\b", "<="),
        (r"\bhigher\s+than\b", ">"),
        (r"\bgreater\s+than\b", ">"),
        (r"\blower\s+than\b", "<"),
        (r"\bless\s+than\b", "<"),
        (r"\bnot\s+equal\s+to\b", "!="),
        (r"\bequal\s+to\b", "=="),
        # Case-insensitive boolean operators
        (r"\bAND\b", "and"),
        (r"\bOR\b", "or"),
        (r"\bNOT\b", "not"),
    ]
    for pat, repl in replacements:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)

    # Support infix `A crosses above B` -> `crosses_above(A, B)` when not already parenthesized as call
    for kw, fn in (("crosses above", "crosses_above"), ("crosses below", "crosses_below")):
        m = re.search(rf"\b{kw}\b", text, flags=re.IGNORECASE)
        if m:
            lhs = text[: m.start()].strip()
            rhs = text[m.end() :].strip()
            if lhs and rhs:
                text = f"{fn}({lhs}, {rhs})"
                break

    return text


class _SafeFormulaEvaluator:
    """AST walker evaluating Streak mathematical formulas into O(N) bar series."""

    def __init__(self, candles: List[Dict[str, Any]], tk: IndicatorToolkit) -> None:
        self.candles = candles
        self.tk = tk
        self.n = len(candles)
        self.watched_series: Dict[str, SeriesVal] = {}
        self._node_count = 0

    def _source_series(self, name: str) -> SeriesVal:
        key = name.lower()
        if key == "open":
            return [float(c["open"]) for c in self.candles]
        if key == "high":
            return [float(c["high"]) for c in self.candles]
        if key == "low":
            return [float(c["low"]) for c in self.candles]
        if key == "close":
            return [float(c["close"]) for c in self.candles]
        if key == "volume":
            return [float(c.get("volume") or 0.0) for c in self.candles]
        if key == "hl2":
            return [(float(c["high"]) + float(c["low"])) * 0.5 for c in self.candles]
        if key == "hlc3":
            return [(float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0 for c in self.candles]
        if key == "ohlc4":
            return [
                (float(c["open"]) + float(c["high"]) + float(c["low"]) + float(c["close"])) * 0.25
                for c in self.candles
            ]
        raise ValueError(f"Unknown price/volume source '{name}'.")

    def _const_series(self, val: float) -> SeriesVal:
        return [float(val)] * self.n

    def _is_scalar_node(self, node: ast.AST) -> Optional[float]:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = self._is_scalar_node(node.operand)
            if inner is not None:
                return -inner if isinstance(node.op, ast.USub) else inner
        return None

    def _record(self, label: str, series: SeriesVal) -> SeriesVal:
        if label and label not in self.watched_series:
            self.watched_series[label] = series
        return series

    def _tick(self) -> None:
        self._node_count += 1
        if self._node_count > _MAX_AST_NODES:
            raise ValueError("Formula is too complex (exceeds AST node safety limit).")

    def eval_numeric(self, node: ast.AST) -> SeriesVal:
        self._tick()

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"Unsupported literal value: {node.value!r}")
            return self._const_series(float(node.value))

        if isinstance(node, ast.Name):
            low = node.id.lower()
            if low in _SOURCE_NAMES:
                return self._source_series(low)
            raise ValueError(
                f"Unknown identifier '{node.id}'. Use Open(0), High(0), Low(0), Close(0), Volume(0), "
                f"or functions like EMA(close, 20, 0), SMA(20), RSI(14), Max(6, High(0))."
            )

        if isinstance(node, ast.UnaryOp):
            val = self.eval_numeric(node.operand)
            if isinstance(node.op, ast.USub):
                return [(-v if v is not None else None) for v in val]
            if isinstance(node.op, ast.UAdd):
                return list(val)
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")

        if isinstance(node, ast.BinOp):
            lhs = self.eval_numeric(node.left)
            rhs = self.eval_numeric(node.right)
            out: SeriesVal = [None] * self.n
            op = node.op
            for i in range(self.n):
                a, b = lhs[i], rhs[i]
                if a is None or b is None:
                    continue
                if isinstance(op, ast.Add):
                    out[i] = a + b
                elif isinstance(op, ast.Sub):
                    out[i] = a - b
                elif isinstance(op, ast.Mult):
                    out[i] = a * b
                elif isinstance(op, ast.Div):
                    out[i] = (a / b) if abs(b) > 1e-12 else None
                elif isinstance(op, ast.Mod):
                    out[i] = (a % b) if abs(b) > 1e-12 else None
                elif isinstance(op, ast.Pow):
                    try:
                        pv = math.pow(a, b)
                        out[i] = pv if not (math.isnan(pv) or math.isinf(pv)) else None
                    except (ValueError, OverflowError):
                        out[i] = None
                else:
                    raise ValueError(f"Unsupported arithmetic operator: {type(op).__name__}")
            return out

        if isinstance(node, ast.Call):
            return self._eval_call_numeric(node)

        raise ValueError(f"Expected numeric expression, got {type(node).__name__}.")

    def _eval_call_numeric(self, node: ast.Call) -> SeriesVal:
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls are permitted in formulas (no attribute access).")
        if node.keywords:
            raise ValueError("Keyword arguments are not supported in formulas; use positional arguments.")

        fn = node.func.id.lower()
        args = node.args

        # 1. Price / Volume series with optional offset: High(-2), Close(0), Open(-1)
        if fn in _SOURCE_NAMES:
            if len(args) > 1:
                raise ValueError(f"{fn.capitalize()}() accepts at most 1 argument: offset (e.g. {fn.capitalize()}(-1)).")
            offset = 0
            if len(args) == 1:
                sc = self._is_scalar_node(args[0])
                if sc is None:
                    raise ValueError(f"{fn.capitalize()}() offset must be an integer constant (e.g. 0, -1, -2).")
                offset = int(sc)
            res = _shift_series(self._source_series(fn), offset)
            if offset != 0:
                self._record(f"{fn.capitalize()}({offset})", res)
            return res

        # 2. Rolling Max / Min & Elementwise Max / Min
        #    - Max(6, High(0)) or Period_max(14, Close(-2)) -> rolling max over N bars
        #    - Max(Open(0), Close(0)) -> elementwise max
        if fn in {"max", "period_max", "periodmax", "highest", "rolling_high", "min", "period_min", "periodmin", "lowest", "rolling_low"}:
            is_max = fn in {"max", "period_max", "periodmax", "highest", "rolling_high"}
            force_rolling = fn not in {"max", "min"}
            if fn in {"rolling_high", "rolling_low"} and len(args) == 1:
                sc = self._is_scalar_node(args[0])
                if sc is None or int(sc) < 1:
                    raise ValueError(f"{fn}() requires a positive integer period.")
                p = int(sc)
                base = self._source_series("high" if is_max else "low")
                res = _rolling_extrema_inclusive(base, p, find_max=is_max)
                return self._record(f"{'Max' if is_max else 'Min'}({p},{'High' if is_max else 'Low'})", res)
            if len(args) < 2:
                raise ValueError(f"{fn}() requires at least 2 arguments, e.g. {'Max(6, High(0))' if is_max else 'Min(6, Low(0))'}.")
            first_scalar = self._is_scalar_node(args[0])
            second_scalar = self._is_scalar_node(args[1])
            if (force_rolling or (len(args) == 2 and first_scalar is not None and second_scalar is None)):
                if first_scalar is None or int(first_scalar) < 1:
                    raise ValueError(f"{fn}() first argument must be a positive integer period.")
                p = int(first_scalar)
                if len(args) > 3:
                    raise ValueError(f"{fn}() accepts (period, expr[, offset]).")
                series = self.eval_numeric(args[1])
                offset = 0
                if len(args) == 3:
                    off_sc = self._is_scalar_node(args[2])
                    if off_sc is None:
                        raise ValueError(f"{fn}() offset must be an integer.")
                    offset = int(off_sc)
                res = _rolling_extrema_inclusive(series, p, find_max=is_max)
                if offset != 0:
                    res = _shift_series(res, offset)
                lbl = f"{'PeriodMax' if is_max else 'PeriodMin'}({p})"
                return self._record(lbl, res)
            # Elementwise max/min across 2+ expressions
            evaled = [self.eval_numeric(a) for a in args]
            out: SeriesVal = [None] * self.n
            for i in range(self.n):
                vals = [s[i] for s in evaled]
                if any(v is None for v in vals):
                    continue
                out[i] = max(vals) if is_max else min(vals)  # type: ignore[type-var]
            return out

        # 3. Rolling Sum / Avg: Sum(5, Volume(0)), Avg(10, Close(0))
        if fn in {"sum", "period_sum", "avg", "mean", "period_avg"}:
            if len(args) != 2:
                raise ValueError(f"{fn}() requires (period, expr).")
            p_sc = self._is_scalar_node(args[0])
            if p_sc is None or int(p_sc) < 1:
                raise ValueError(f"{fn}() period must be a positive integer.")
            p = int(p_sc)
            series = self.eval_numeric(args[1])
            res = _rolling_sum(series, p) if fn in {"sum", "period_sum"} else _compute_sma_opt(series, p)
            return self._record(f"{fn.upper()}({p})", res)

        # 4. Moving averages & RSI supporting BOTH:
        #      EMA(20) / EMA(20, -1)  AND  EMA(high, 20) / EMA(high, 20, 0) / EMA(Close(-1), 20, 0)
        if fn in {"sma", "ema", "wma", "rsi"}:
            source_label = "close"
            src_series: Optional[SeriesVal] = None
            period = 14 if fn == "rsi" else 20
            offset = 0

            if len(args) == 0:
                raise ValueError(f"{fn.upper()}() requires at least a period, e.g. {fn.upper()}(20) or {fn.upper()}(close, 20, 0).")

            arg0_scalar = self._is_scalar_node(args[0])
            if arg0_scalar is not None:
                # Form: EMA(period[, offset])
                if len(args) > 2:
                    raise ValueError(f"{fn.upper()}(period[, offset]) takes 1 or 2 arguments.")
                period = max(1, int(arg0_scalar))
                if len(args) == 2:
                    off_sc = self._is_scalar_node(args[1])
                    if off_sc is None:
                        raise ValueError(f"{fn.upper()}() offset must be an integer.")
                    offset = int(off_sc)
            else:
                # Form: EMA(source_expr, period[, offset])
                if len(args) < 2 or len(args) > 3:
                    raise ValueError(f"{fn.upper()}(source, period[, offset]) takes 2 or 3 arguments.")
                if isinstance(args[0], ast.Name) and args[0].id.lower() in _SOURCE_NAMES:
                    source_label = args[0].id.lower()
                    src_series = self._source_series(source_label)
                else:
                    source_label = "expr"
                    src_series = self.eval_numeric(args[0])
                p_sc = self._is_scalar_node(args[1])
                if p_sc is None or int(p_sc) < 1:
                    raise ValueError(f"{fn.upper()}() period must be a positive integer.")
                period = int(p_sc)
                if len(args) == 3:
                    off_sc = self._is_scalar_node(args[2])
                    if off_sc is None:
                        raise ValueError(f"{fn.upper()}() offset must be an integer.")
                    offset = int(off_sc)

            if src_series is None:
                if fn == "sma":
                    res = self.tk.sma(period)
                elif fn == "ema":
                    res = self.tk.ema(period)
                elif fn == "wma":
                    res = _compute_wma_from_values([float(x) for x in self.tk.closes], period)
                else:
                    res = self.tk.rsi(period)
            else:
                if fn == "sma":
                    res = _compute_sma_opt(src_series, period)
                elif fn == "ema":
                    res = _compute_ema_opt(src_series, period)
                elif fn == "wma":
                    res = _compute_wma_from_values(src_series, period)
                else:
                    res = _compute_rsi_opt(src_series, period)

            res = _shift_series(res, offset)
            tag = f"{fn.upper()}({source_label},{period})" if source_label != "close" else f"{fn.upper()}({period})"
            if offset != 0:
                tag = f"{tag}[{offset}]"
            return self._record(tag, res)

        # 5. RSI Moving Average: RSI_MA(rsi_period=14, ma_period=9, offset=0)
        if fn == "rsi_ma":
            p1 = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 14
            p2 = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 9
            off = int(self._is_scalar_node(args[2])) if len(args) >= 3 and self._is_scalar_node(args[2]) is not None else 0
            res = _shift_series(_compute_sma_opt(self.tk.rsi(max(1, p1)), max(1, p2)), off)
            return self._record(f"RSI_MA({p1},{p2})", res)

        # 6. ATR / ADX / VWAP / Vol_SMA / ROC
        if fn == "atr":
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 14
            off = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 0
            atr_vals, _ = self.tk.atr(max(1, p))
            return self._record(f"ATR({p})", _shift_series(atr_vals, off))

        if fn == "adx":
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 14
            off = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 0
            return self._record(f"ADX({p})", _shift_series(self.tk.adx(max(1, p))["adx"], off))

        if fn == "vwap":
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 20
            off = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 0
            return self._record(f"VWAP({p})", _shift_series(_compute_vwap_series(self.candles, max(1, p)), off))

        if fn in {"vol_sma", "volsma"}:
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 20
            off = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 0
            res = compute_sma_series(self.tk.volumes, max(1, p))
            return self._record(f"VolSMA({p})", _shift_series(res, off))

        if fn == "roc":
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 10
            off = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 0
            return self._record(f"ROC({p})", _shift_series(self.tk.roc(max(1, p)), off))

        # 7. Bollinger Bands: BB_Upper(20, 2, 0)
        if fn in {"bb_upper", "bbupper", "bb_middle", "bbmiddle", "bb_lower", "bblower"}:
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 20
            sd = float(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 2.0
            off = int(self._is_scalar_node(args[2])) if len(args) >= 3 and self._is_scalar_node(args[2]) is not None else 0
            band_key = "upper" if "upper" in fn else ("lower" if "lower" in fn else "middle")
            res = _shift_series(self.tk.bbands(max(2, p), sd)[band_key], off)
            return self._record(f"BB_{band_key.capitalize()}({p},{sd:g})", res)

        # 8. MACD: MACD(12, 26, 9, 0) / MACD_Signal(...) / MACD_Hist(...)
        if fn in {"macd", "macd_signal", "macdsignal", "macd_hist", "macdhist"}:
            fast = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 12
            slow = int(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 26
            sig = int(self._is_scalar_node(args[2])) if len(args) >= 3 and self._is_scalar_node(args[2]) is not None else 9
            off = int(self._is_scalar_node(args[3])) if len(args) >= 4 and self._is_scalar_node(args[3]) is not None else 0
            m_key = "signal" if "signal" in fn else ("hist" if "hist" in fn else "macd")
            res = _shift_series(self.tk.macd(max(1, fast), max(2, slow), max(1, sig))[m_key], off)
            return self._record(f"MACD_{m_key}({fast},{slow},{sig})", res)

        # 9. Supertrend: Supertrend(10, 3, 0) / Supertrend_Dir(10, 3, 0)
        if fn in {"supertrend", "supertrend_stop", "supertrend_dir"}:
            p = int(self._is_scalar_node(args[0])) if len(args) >= 1 and self._is_scalar_node(args[0]) is not None else 10
            mult = float(self._is_scalar_node(args[1])) if len(args) >= 2 and self._is_scalar_node(args[1]) is not None else 3.0
            off = int(self._is_scalar_node(args[2])) if len(args) >= 3 and self._is_scalar_node(args[2]) is not None else 0
            dirs, stops = self.tk.supertrend(max(2, p), mult)
            if fn == "supertrend_dir":
                res: SeriesVal = [float(d) if d != 0 else None for d in dirs]
                return self._record(f"SupertrendDir({p},{mult:g})", _shift_series(res, off))
            return self._record(f"Supertrend({p},{mult:g})", _shift_series(stops, off))

        # 10. Math helpers: Abs(x), Sqrt(x), Log(x), Round(x, d)
        if fn == "abs":
            if len(args) != 1:
                raise ValueError("Abs() requires 1 argument.")
            s = self.eval_numeric(args[0])
            return [abs(v) if v is not None else None for v in s]

        if fn == "sqrt":
            if len(args) != 1:
                raise ValueError("Sqrt() requires 1 argument.")
            s = self.eval_numeric(args[0])
            return [math.sqrt(v) if (v is not None and v >= 0) else None for v in s]

        if fn == "log":
            if len(args) != 1:
                raise ValueError("Log() requires 1 argument.")
            s = self.eval_numeric(args[0])
            return [math.log(v) if (v is not None and v > 0) else None for v in s]

        if fn == "round":
            if len(args) not in {1, 2}:
                raise ValueError("Round() requires 1 or 2 arguments.")
            s = self.eval_numeric(args[0])
            d = int(self._is_scalar_node(args[1])) if len(args) == 2 and self._is_scalar_node(args[1]) is not None else 2
            return [round(v, d) if v is not None else None for v in s]

        raise ValueError(
            f"Unsupported function '{node.func.id}()'. Supported: Open, High, Low, Close, Volume, "
            f"Max/Period max, Min/Period min, SMA, EMA, WMA, RSI, ATR, ADX, VWAP, Vol_SMA, "
            f"MACD, BB_Upper/Lower, Supertrend, ROC, Abs, Sqrt, Log, Crosses_Above, Crosses_Below."
        )

    def eval_boolean(self, node: ast.AST) -> BoolSeries:
        self._tick()

        if isinstance(node, ast.BoolOp):
            parts = [self.eval_boolean(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return [all(p[i] for p in parts) for i in range(self.n)]
            if isinstance(node.op, ast.Or):
                return [any(p[i] for p in parts) for i in range(self.n)]
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = self.eval_boolean(node.operand)
            return [not x for x in inner]

        if isinstance(node, ast.Compare):
            left_s = self.eval_numeric(node.left)
            out = [True] * self.n
            cur_left = left_s
            for op, comp_node in zip(node.ops, node.comparators):
                cur_right = self.eval_numeric(comp_node)
                for i in range(self.n):
                    if not out[i]:
                        continue
                    a, b = cur_left[i], cur_right[i]
                    if a is None or b is None:
                        out[i] = False
                        continue
                    if isinstance(op, ast.Gt):
                        out[i] = a > b
                    elif isinstance(op, ast.GtE):
                        out[i] = a >= b
                    elif isinstance(op, ast.Lt):
                        out[i] = a < b
                    elif isinstance(op, ast.LtE):
                        out[i] = a <= b
                    elif isinstance(op, ast.Eq):
                        out[i] = abs(a - b) <= 1e-6
                    elif isinstance(op, ast.NotEq):
                        out[i] = abs(a - b) > 1e-6
                    else:
                        raise ValueError(f"Unsupported comparison operator: {type(op).__name__}")
                cur_left = cur_right
            return out

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = node.func.id.lower()
            if fn in {"crosses_above", "crossesabove", "crosses_below", "crossesbelow"}:
                if len(node.args) != 2:
                    raise ValueError(f"{node.func.id}() requires exactly 2 series arguments.")
                lhs = self.eval_numeric(node.args[0])
                rhs = self.eval_numeric(node.args[1])
                is_above = fn in {"crosses_above", "crossesabove"}
                out = [False] * self.n
                for i in range(1, self.n):
                    a, b, pa, pb = lhs[i], rhs[i], lhs[i - 1], rhs[i - 1]
                    if a is None or b is None or pa is None or pb is None:
                        continue
                    if is_above:
                        out[i] = pa <= pb and a > b
                    else:
                        out[i] = pa >= pb and a < b
                return out

        raise ValueError(
            "Condition formula must evaluate to a comparison or crossover (e.g. "
            "'High(-2) > Max(6, High(0))' or 'EMA(high, 20, 0) >= Period max(14, Close(-2))')."
        )


def parse_formula_ast(expr: str) -> ast.Expression:
    """Normalize and parse a formula string safely via `ast.parse(..., mode='eval')`."""
    normalized = normalize_formula_text(expr)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid formula syntax in '{expr}': {exc.msg}") from exc
    return tree


def evaluate_formula_series(
    expr: str,
    candles: List[Dict[str, Any]],
    tk: IndicatorToolkit,
) -> Tuple[SeriesVal, Dict[str, SeriesVal]]:
    """Evaluate a numeric mathematical expression into a per-bar series + watched sub-series."""
    tree = parse_formula_ast(expr)
    evaluator = _SafeFormulaEvaluator(candles, tk)
    series = evaluator.eval_numeric(tree.body)
    return series, evaluator.watched_series


def evaluate_formula_condition(
    expr: str,
    candles: List[Dict[str, Any]],
    tk: IndicatorToolkit,
) -> Tuple[BoolSeries, Dict[str, SeriesVal]]:
    """Evaluate a boolean mathematical condition formula into per-bar True/False + watched series."""
    tree = parse_formula_ast(expr)
    evaluator = _SafeFormulaEvaluator(candles, tk)
    flags = evaluator.eval_boolean(tree.body)
    return flags, evaluator.watched_series


def validate_formula(expr: str, *, expect_boolean: bool = False) -> Dict[str, Any]:
    """Validate formula syntax and allowed functions on a minimal synthetic bar window."""
    if not isinstance(expr, str) or not expr.strip():
        return {"ok": False, "valid": False, "error": "Formula expression cannot be empty.", "series_keys": []}
    sample = [
        {"date": f"2025-01-{i+1:02d}", "open": 100.0 + i, "high": 102.0 + i, "low": 99.0 + i, "close": 101.0 + i, "volume": 100000 + i * 1000}
        for i in range(30)
    ]
    tk = IndicatorToolkit(sample, backend="builtin")
    try:
        if expect_boolean:
            flags, watched = evaluate_formula_condition(expr, sample, tk)
            return {
                "ok": True,
                "valid": True,
                "normalized": normalize_formula_text(expr),
                "kind": "boolean",
                "series_keys": list(watched.keys()),
                "sample_matches": sum(1 for x in flags if x),
            }
        series, watched = evaluate_formula_series(expr, sample, tk)
        return {
            "ok": True,
            "valid": True,
            "normalized": normalize_formula_text(expr),
            "kind": "numeric",
            "series_keys": list(watched.keys()),
            "last_sample_value": _clean_float(series[-1]) if series else None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "valid": False,
            "error": str(exc),
            "series_keys": [],
        }


__all__ = [
    "evaluate_formula_condition",
    "evaluate_formula_series",
    "normalize_formula_text",
    "parse_formula_ast",
    "validate_formula",
]
