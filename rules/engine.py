"""
rules/engine.py
===============
Streak-style Visual No-Code Rule Engine + 1-Click Python Script Exporter.

Compiles structured JSON rule definitions (`AND` / `OR` condition groups with
indicators, OHLCV fields, constants, candle offsets, SL/TP/TSL) into the exact
same `BaseScreen` and `BacktestStrategy` interfaces used by custom `.py` files.

That guarantees:
- Visual screens & Python screens run on the EXACT same Screener + Live WS engine.
- Visual strategies & Python strategies run on the EXACT same Individual/Portfolio
  Backtester, Real Charges Calculator, Paper Trade Engine, and Live Deployer.
- Any visual rule can be exported to a clean `.py` plugin with one click.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backtest.base import BacktestStrategy
from config import IST_TZ, config
from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch


SUPPORTED_OPERANDS: List[Dict[str, Any]] = [
    {"id": "close", "label": "Close", "kind": "price", "default_period": None},
    {"id": "open", "label": "Open", "kind": "price", "default_period": None},
    {"id": "high", "label": "High", "kind": "price", "default_period": None},
    {"id": "low", "label": "Low", "kind": "price", "default_period": None},
    {"id": "volume", "label": "Volume", "kind": "volume", "default_period": None},
    {"id": "number", "label": "Number (Constant)", "kind": "constant", "default_period": None},
    {"id": "sma", "label": "SMA", "kind": "indicator", "default_period": 20},
    {"id": "ema", "label": "EMA", "kind": "indicator", "default_period": 20},
    {"id": "rsi", "label": "RSI", "kind": "indicator", "default_period": 14},
    {"id": "atr", "label": "ATR", "kind": "indicator", "default_period": 14},
    {"id": "macd", "label": "MACD Line", "kind": "indicator", "default_period": 12},
    {"id": "macd_signal", "label": "MACD Signal", "kind": "indicator", "default_period": 9},
    {"id": "macd_hist", "label": "MACD Histogram", "kind": "indicator", "default_period": 9},
    {"id": "bb_upper", "label": "Bollinger Upper", "kind": "indicator", "default_period": 20},
    {"id": "bb_middle", "label": "Bollinger Middle", "kind": "indicator", "default_period": 20},
    {"id": "bb_lower", "label": "Bollinger Lower", "kind": "indicator", "default_period": 20},
    {"id": "adx", "label": "ADX", "kind": "indicator", "default_period": 14},
    {"id": "plus_di", "label": "+DI", "kind": "indicator", "default_period": 14},
    {"id": "minus_di", "label": "-DI", "kind": "indicator", "default_period": 14},
    {"id": "supertrend_stop", "label": "Supertrend Level", "kind": "indicator", "default_period": 10},
    {"id": "supertrend_dir", "label": "Supertrend Direction (+1/-1)", "kind": "indicator", "default_period": 10},
    {"id": "stoch_k", "label": "Stochastic %K", "kind": "indicator", "default_period": 14},
    {"id": "stoch_d", "label": "Stochastic %D", "kind": "indicator", "default_period": 3},
    {"id": "roc", "label": "ROC (%)", "kind": "indicator", "default_period": 10},
    {"id": "rolling_high", "label": "Rolling High", "kind": "indicator", "default_period": 20},
    {"id": "rolling_low", "label": "Rolling Low", "kind": "indicator", "default_period": 20},
    {"id": "vol_sma", "label": "Volume SMA", "kind": "volume", "default_period": 20},
]

SUPPORTED_OPERATORS: List[Dict[str, str]] = [
    {"id": ">", "label": "is greater than (>)"},
    {"id": "<", "label": "is lower than (<)"},
    {"id": ">=", "label": "is >= "},
    {"id": "<=", "label": "is <= "},
    {"id": "==", "label": "equals (==)"},
    {"id": "crosses_above", "label": "crosses above"},
    {"id": "crosses_below", "label": "crosses below"},
]


def _compute_operand_series(
    spec: Dict[str, Any],
    candles: List[Dict[str, Any]],
    tk: IndicatorToolkit,
) -> List[Optional[float]]:
    """Evaluate a visual operand specification into an O(N) numeric series."""
    n = len(candles)
    op_type = str(spec.get("type") or "close").strip().lower()
    period = int(spec.get("period") or 14)
    mult = float(spec.get("multiplier") or 1.0)
    param2 = float(spec.get("param2") or 0.0)

    series: List[Optional[float]]
    if op_type == "number":
        val = float(spec.get("value") or 0.0)
        return [val] * n
    if op_type == "close":
        series = [float(c["close"]) for c in candles]
    elif op_type == "open":
        series = [float(c["open"]) for c in candles]
    elif op_type == "high":
        series = [float(c["high"]) for c in candles]
    elif op_type == "low":
        series = [float(c["low"]) for c in candles]
    elif op_type == "volume":
        series = [float(c.get("volume") or 0.0) for c in candles]
    elif op_type == "sma":
        series = tk.sma(max(1, period))
    elif op_type == "ema":
        series = tk.ema(max(1, period))
    elif op_type == "rsi":
        series = tk.rsi(max(2, period))
    elif op_type == "atr":
        series = tk.atr(max(2, period))[0]
    elif op_type == "macd":
        series = tk.macd(fast=max(2, period), slow=max(5, int(param2) if param2 > 0 else 26))["macd"]
    elif op_type == "macd_signal":
        series = tk.macd(signal=max(2, period))["signal"]
    elif op_type == "macd_hist":
        series = tk.macd(signal=max(2, period))["hist"]
    elif op_type == "bb_upper":
        series = tk.bbands(period=max(2, period), std_dev=param2 if param2 > 0 else 2.0)["upper"]
    elif op_type == "bb_middle":
        series = tk.bbands(period=max(2, period), std_dev=param2 if param2 > 0 else 2.0)["middle"]
    elif op_type == "bb_lower":
        series = tk.bbands(period=max(2, period), std_dev=param2 if param2 > 0 else 2.0)["lower"]
    elif op_type == "adx":
        series = tk.adx(max(2, period))["adx"]
    elif op_type == "plus_di":
        series = tk.adx(max(2, period))["plus_di"]
    elif op_type == "minus_di":
        series = tk.adx(max(2, period))["minus_di"]
    elif op_type == "supertrend_stop":
        series = tk.supertrend(period=max(2, period), multiplier=param2 if param2 > 0 else 3.0)[1]
    elif op_type == "supertrend_dir":
        dirs = tk.supertrend(period=max(2, period), multiplier=param2 if param2 > 0 else 3.0)[0]
        series = [float(d) for d in dirs]
    elif op_type == "stoch_k":
        series = tk.stoch(k_period=max(2, period))["k"]
    elif op_type == "stoch_d":
        series = tk.stoch(d_period=max(2, period))["d"]
    elif op_type == "roc":
        series = tk.roc(max(1, period))
    elif op_type == "rolling_high":
        series = tk.rolling_high(max(2, period))
    elif op_type == "rolling_low":
        series = tk.rolling_low(max(2, period))
    elif op_type == "vol_sma":
        from indicators.toolkit import compute_sma_series

        vols = [float(c.get("volume") or 0.0) for c in candles]
        series = compute_sma_series(vols, max(1, period))
    else:
        series = [float(c["close"]) for c in candles]

    if mult != 1.0:
        series = [v * mult if v is not None else None for v in series]
    return series


def _series_val(series: List[Optional[float]], idx: int, offset: int = 0) -> Optional[float]:
    # offset is negative or 0 (e.g., 0 = current bar, -1 = previous bar; positive values treated as lookback too)
    shift = -abs(int(offset)) if int(offset) != 0 else 0
    pos = idx + shift
    if pos < 0 or pos >= len(series):
        return None
    return series[pos]


def _operand_label(spec: Dict[str, Any]) -> str:
    op = str(spec.get("type") or "close").strip().lower()
    if op == "number":
        return str(spec.get("value", 0))
    period = spec.get("period")
    mult = float(spec.get("multiplier") or 1.0)
    off = int(spec.get("offset") or 0)
    base = f"{op.upper()}({int(period)})" if period and op not in {"close", "open", "high", "low", "volume"} else op.upper()
    if mult != 1.0:
        base = f"{mult:g}*{base}"
    if off != 0:
        base = f"{base}[{-abs(off)}]"
    return base


def _estimate_required_bars_for_group(group: Optional[Dict[str, Any]]) -> int:
    if not group or not isinstance(group, dict):
        return 30
    req = 20
    for cond in group.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if "conditions" in cond:
            req = max(req, _estimate_required_bars_for_group(cond))
            continue
        for side_key in ("left", "right"):
            side = cond.get(side_key) or {}
            op = str(side.get("type") or "").lower()
            per = int(side.get("period") or 0)
            off = abs(int(side.get("offset") or 0))
            if op in {"ema", "rsi", "adx", "atr", "macd", "macd_signal", "macd_hist"}:
                req = max(req, per * 3 + off + 15)
            elif per > 0:
                req = max(req, per + off + 15)
    return req


class _PreparedConditionGroup:
    """Pre-computed O(N) evaluator for a visual condition tree (`AND` / `OR`)."""

    def __init__(self, group: Optional[Dict[str, Any]], candles: List[Dict[str, Any]], tk: IndicatorToolkit) -> None:
        self._group = group or {"logic": "AND", "conditions": []}
        self._logic = str(self._group.get("logic") or "AND").strip().upper()
        self._compiled: List[Any] = []
        self.watched_series: Dict[str, List[Optional[float]]] = {}

        for cond in self._group.get("conditions") or []:
            if not isinstance(cond, dict):
                continue
            if "conditions" in cond:
                sub = _PreparedConditionGroup(cond, candles, tk)
                self._compiled.append(("group", sub))
                self.watched_series.update(sub.watched_series)
                continue
            left_spec = cond.get("left") or {"type": "close"}
            right_spec = cond.get("right") or {"type": "number", "value": 0}
            operator = str(cond.get("operator") or ">").strip().lower()
            left_s = _compute_operand_series(left_spec, candles, tk)
            right_s = _compute_operand_series(right_spec, candles, tk)
            left_off = int(left_spec.get("offset") or 0)
            right_off = int(right_spec.get("offset") or 0)
            self._compiled.append(("leaf", left_s, left_off, operator, right_s, right_off))
            if str(left_spec.get("type") or "").lower() != "number":
                self.watched_series[_operand_label(left_spec)] = left_s
            if str(right_spec.get("type") or "").lower() != "number":
                self.watched_series[_operand_label(right_spec)] = right_s

    def is_empty(self) -> bool:
        return len(self._compiled) == 0

    def eval_at(self, idx: int) -> bool:
        if not self._compiled:
            return False
        results: List[bool] = []
        for item in self._compiled:
            if item[0] == "group":
                results.append(item[1].eval_at(idx))
                continue
            _, ls, loff, op, rs, roff = item
            lv = _series_val(ls, idx, loff)
            rv = _series_val(rs, idx, roff)
            if lv is None or rv is None:
                results.append(False)
                continue
            if op == ">":
                results.append(lv > rv)
            elif op == "<":
                results.append(lv < rv)
            elif op == ">=":
                results.append(lv >= rv)
            elif op == "<=":
                results.append(lv <= rv)
            elif op == "==":
                results.append(abs(lv - rv) <= 1e-9)
            elif op == "crosses_above":
                plv = _series_val(ls, idx - 1, loff)
                prv = _series_val(rs, idx - 1, roff)
                results.append(plv is not None and prv is not None and plv <= prv and lv > rv)
            elif op == "crosses_below":
                plv = _series_val(ls, idx - 1, loff)
                prv = _series_val(rs, idx - 1, roff)
                results.append(plv is not None and prv is not None and plv >= prv and lv < rv)
            else:
                results.append(False)

        if self._logic == "OR":
            return any(results)
        return all(results)


def evaluate_condition_group(
    group: Dict[str, Any],
    candles: List[Dict[str, Any]],
    tk: IndicatorToolkit,
    index: int = -1,
) -> bool:
    """Convenience single-bar evaluator for a visual condition tree."""
    if not candles:
        return False
    idx = index if index >= 0 else len(candles) + index
    prep = _PreparedConditionGroup(group, candles, tk)
    return prep.eval_at(idx)


class CompiledVisualScreen(BaseScreen):
    """Wraps a visual rule JSON definition into a first-class `BaseScreen`."""

    def __init__(self, rule_def: Dict[str, Any]) -> None:
        self.rule_def = dict(rule_def)
        self.id = str(rule_def.get("id") or f"visual_screen_{uuid.uuid4().hex[:6]}").strip().lower()
        self.name = str(rule_def.get("name") or "Visual Scanner")
        self.description = str(rule_def.get("description") or "Visual block scanner")
        self.min_bars = max(15, _estimate_required_bars_for_group(rule_def.get("entry") or rule_def.get("conditions")))
        self.params = []
        self._group = rule_def.get("entry") or rule_def.get("conditions") or {"logic": "AND", "conditions": []}

    def required_bars(self, params: Dict[str, Any]) -> int:
        return max(30, self.min_bars)

    def get_columns(self, params: Dict[str, Any]) -> List[ColumnSpec]:
        cols: List[ColumnSpec] = []
        seen: set[str] = set()
        for cond in (self._group.get("conditions") or []):
            if not isinstance(cond, dict):
                continue
            for side_k in ("left", "right"):
                side = cond.get(side_k) or {}
                if str(side.get("type") or "").lower() in {"number", "close", "open", "high", "low"}:
                    continue
                lbl = _operand_label(side)
                key = re.sub(r"[^a-z0-9]+", "_", lbl.lower()).strip("_")
                if key and key not in seen:
                    seen.add(key)
                    cols.append(ColumnSpec(key=key, label=lbl, format="number", digits=2))
        return cols[:8]

    def evaluate(self, candles: List[Dict[str, Any]], params: Dict[str, Any], tk: IndicatorToolkit) -> ScreenMatch:
        prep = _PreparedConditionGroup(self._group, candles, tk)
        idx = len(candles) - 1
        matched = prep.eval_at(idx)
        metrics: Dict[str, Any] = {}
        for lbl, series in prep.watched_series.items():
            key = re.sub(r"[^a-z0-9]+", "_", lbl.lower()).strip("_")
            v = series[idx] if idx >= 0 and idx < len(series) else None
            metrics[key] = round(float(v), 2) if v is not None else None
        return ScreenMatch(
            matched=matched,
            signal="MATCH" if matched else "WATCH",
            score=85 if matched else 25,
            tags=["VISUAL_RULE", "MATCHED"] if matched else ["VISUAL_RULE"],
            metrics=metrics,
        )


class CompiledVisualStrategy(BacktestStrategy):
    """Wraps a visual strategy JSON definition into a first-class `BacktestStrategy`."""

    def __init__(self, rule_def: Dict[str, Any]) -> None:
        super().__init__()
        self.rule_def = dict(rule_def)
        self.id = str(rule_def.get("id") or f"visual_strat_{uuid.uuid4().hex[:6]}").strip().lower()
        self.name = str(rule_def.get("name") or "Visual Strategy")
        self.description = str(rule_def.get("description") or "Visual block strategy")
        self.params = []
        self._entry_group = rule_def.get("entry") or {"logic": "AND", "conditions": []}
        self._exit_group = rule_def.get("exit") or {"logic": "OR", "conditions": []}
        self._entry_prep: Optional[_PreparedConditionGroup] = None
        self._exit_prep: Optional[_PreparedConditionGroup] = None

    def required_bars(self) -> int:
        return max(
            30,
            _estimate_required_bars_for_group(self._entry_group),
            _estimate_required_bars_for_group(self._exit_group),
        )

    def prepare(self, candles: List[Dict[str, Any]], params: Dict[str, Any], toolkit: IndicatorToolkit) -> None:
        self._entry_prep = _PreparedConditionGroup(self._entry_group, candles, toolkit)
        self._exit_prep = _PreparedConditionGroup(self._exit_group, candles, toolkit)

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        enter_sig = self._entry_prep.eval_at(index) if self._entry_prep else False
        exit_sig = self._exit_prep.eval_at(index) if (self._exit_prep and not self._exit_prep.is_empty()) else False
        return enter_sig, exit_sig


def compile_visual_screen(rule_def: Dict[str, Any]) -> CompiledVisualScreen:
    return CompiledVisualScreen(rule_def)


def compile_visual_strategy(rule_def: Dict[str, Any]) -> CompiledVisualStrategy:
    return CompiledVisualStrategy(rule_def)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "custom_rule").strip().lower()).strip("_")
    return slug or f"rule_{uuid.uuid4().hex[:6]}"


def export_rule_to_python(
    rule_def: Dict[str, Any],
    *,
    kind: str = "strategy",
    target_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Generate a clean, editable `.py` plugin file from any visual rule JSON
    definition so traders can graduate from blocks into custom Python code.
    """
    clean_kind = (kind or rule_def.get("kind") or "strategy").strip().lower()
    slug = _slugify(str(rule_def.get("id") or rule_def.get("name") or "visual_export"))
    class_name = "".join(part.capitalize() for part in slug.split("_")) + ("Screen" if clean_kind == "screen" else "Strategy")
    rule_json_literal = json.dumps(rule_def, indent=4)

    if clean_kind == "screen":
        out_dir = target_dir or (Path(__file__).resolve().parent.parent / "screener" / "screens")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{slug}.py"
        code = f'''"""
Auto-exported from AlgoDesk Visual Builder -> Editable Python Screen Plugin.
You can now add arbitrary Python math, custom indicators, or state logic below.
"""

from __future__ import annotations

from typing import Any, Dict, List

from indicators.toolkit import IndicatorToolkit
from rules.engine import CompiledVisualScreen
from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch

RULE_DEFINITION: Dict[str, Any] = {rule_json_literal}


class {class_name}(BaseScreen):
    id = "{slug}"
    name = {json.dumps(str(rule_def.get("name") or slug) + " (Python)")}
    description = {json.dumps(str(rule_def.get("description") or "Exported from visual rule builder"))}
    min_bars = 40
    params: List[ParamSpec] = []

    def __init__(self) -> None:
        self._compiled = CompiledVisualScreen(RULE_DEFINITION)

    def get_columns(self, params: Dict[str, Any]) -> List[ColumnSpec]:
        return self._compiled.get_columns(params)

    def evaluate(self, candles: List[Dict[str, Any]], params: Dict[str, Any], tk: IndicatorToolkit) -> ScreenMatch:
        # Tip: You can replace or extend this with custom Python logic using `tk` (TA-Lib / pandas-ta / O(N))
        return self._compiled.evaluate(candles, params, tk)
'''
    else:
        out_dir = target_dir or (Path(__file__).resolve().parent.parent / "backtest" / "strategies")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{slug}.py"
        code = f'''"""
Auto-exported from AlgoDesk Visual Builder -> Editable Python Strategy Plugin.
You can now add custom position filters, multi-bar state machines, or regime logic below.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from rules.engine import CompiledVisualStrategy
from screener.base import ParamSpec

RULE_DEFINITION: Dict[str, Any] = {rule_json_literal}


class {class_name}(BacktestStrategy):
    id = "{slug}"
    name = {json.dumps(str(rule_def.get("name") or slug) + " (Python)")}
    description = {json.dumps(str(rule_def.get("description") or "Exported from visual rule builder"))}
    params: List[ParamSpec] = []

    def __init__(self) -> None:
        super().__init__()
        self._compiled = CompiledVisualStrategy(RULE_DEFINITION)

    def prepare(self, candles: List[Dict[str, Any]], params: Dict[str, Any], toolkit: IndicatorToolkit) -> None:
        self._compiled.prepare(candles, params, toolkit)

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        # Tip: Customize enter_long / exit_long freely with any Python condition
        return self._compiled.signal_at(index, bar)
'''

    out_path.write_text(code, encoding="utf-8")
    return {
        "ok": True,
        "id": slug,
        "kind": clean_kind,
        "path": str(out_path),
        "filename": out_path.name,
        "code": code,
    }


_SEED_VISUAL_RULES: List[Dict[str, Any]] = [
    {
        "id": "vis_ema_rsi_momentum",
        "kind": "strategy",
        "mode": "visual",
        "name": "EMA 20/50 Crossover + RSI Filter",
        "description": "Enters when EMA(20) crosses above EMA(50) with RSI(14) > 55; exits on bearish crossover or SL/TP.",
        "timeframe": "1day",
        "segment": "equity_delivery",
        "stop_loss_pct": 4.0,
        "take_profit_pct": 12.0,
        "trailing_sl_pct": 0.0,
        "entry": {
            "logic": "AND",
            "conditions": [
                {"left": {"type": "ema", "period": 20, "offset": 0}, "operator": "crosses_above", "right": {"type": "ema", "period": 50, "offset": 0}},
                {"left": {"type": "rsi", "period": 14, "offset": 0}, "operator": ">", "right": {"type": "number", "value": 55}},
            ],
        },
        "exit": {
            "logic": "OR",
            "conditions": [
                {"left": {"type": "ema", "period": 20, "offset": 0}, "operator": "crosses_below", "right": {"type": "ema", "period": 50, "offset": 0}},
            ],
        },
    },
    {
        "id": "vis_supertrend_adx",
        "kind": "strategy",
        "mode": "visual",
        "name": "Supertrend Bullish + ADX Trend",
        "description": "Captures strong directional trends when Supertrend flips bullish and ADX(14) > 22.",
        "timeframe": "15minute",
        "segment": "equity_intraday",
        "stop_loss_pct": 1.5,
        "take_profit_pct": 3.5,
        "trailing_sl_pct": 1.0,
        "entry": {
            "logic": "AND",
            "conditions": [
                {"left": {"type": "supertrend_dir", "period": 10, "param2": 3.0}, "operator": "==", "right": {"type": "number", "value": 1}},
                {"left": {"type": "adx", "period": 14}, "operator": ">", "right": {"type": "number", "value": 22}},
            ],
        },
        "exit": {
            "logic": "OR",
            "conditions": [
                {"left": {"type": "supertrend_dir", "period": 10, "param2": 3.0}, "operator": "==", "right": {"type": "number", "value": -1}},
            ],
        },
    },
    {
        "id": "vis_scan_rsi_breakout",
        "kind": "screen",
        "mode": "visual",
        "name": "RSI Momentum + Volume Surge Scanner",
        "description": "Finds stocks trading above EMA(20) with RSI(14) > 60 and volume > 1.5x 20-bar average.",
        "timeframe": "1day",
        "entry": {
            "logic": "AND",
            "conditions": [
                {"left": {"type": "close"}, "operator": ">", "right": {"type": "ema", "period": 20}},
                {"left": {"type": "rsi", "period": 14}, "operator": ">", "right": {"type": "number", "value": 60}},
                {"left": {"type": "volume"}, "operator": ">", "right": {"type": "vol_sma", "period": 20, "multiplier": 1.5}},
            ],
        },
    },
]


class VisualRuleStore:
    """Thread-safe JSON persistence for user-created visual screens and strategies."""

    def __init__(self, store_file: Optional[Path] = None) -> None:
        self._file = store_file or (config.DATA_DIR / "visual_rules.json")
        self._lock = threading.RLock()
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        with self._lock:
            if self._file.exists():
                return
            self._file.parent.mkdir(parents=True, exist_ok=True)
            now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            seeded = [{**r, "updated_at_ist": now_ist} for r in _SEED_VISUAL_RULES]
            self._file.write_text(json.dumps(seeded, indent=2), encoding="utf-8")

    def _read_all_locked(self) -> List[Dict[str, Any]]:
        self._ensure_seeded()
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [dict(item) for item in raw if isinstance(item, dict) and item.get("id")]
        except Exception:
            pass
        return [dict(r) for r in _SEED_VISUAL_RULES]

    def _write_all_locked(self, items: List[Dict[str, Any]]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def list_rules(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = self._read_all_locked()
        if kind:
            ck = kind.strip().lower()
            items = [r for r in items if str(r.get("kind") or "strategy").lower() == ck]
        return items

    def get(self, rule_id: str) -> Optional[Dict[str, Any]]:
        clean = (rule_id or "").strip().lower()
        with self._lock:
            for r in self._read_all_locked():
                if str(r.get("id") or "").strip().lower() == clean:
                    return dict(r)
        return None

    def save(self, rule_def: Dict[str, Any]) -> Dict[str, Any]:
        name = str(rule_def.get("name") or "").strip()
        if not name:
            raise ValueError("Rule name is required.")
        kind = str(rule_def.get("kind") or "strategy").strip().lower()
        if kind not in {"screen", "strategy"}:
            kind = "strategy"
        rid = str(rule_def.get("id") or "").strip().lower()
        if not rid:
            prefix = "vis_scan_" if kind == "screen" else "vis_"
            rid = f"{prefix}{_slugify(name)}"
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        record = {
            **rule_def,
            "id": rid,
            "kind": kind,
            "mode": "visual",
            "name": name,
            "description": str(rule_def.get("description") or "").strip(),
            "updated_at_ist": now_ist,
        }
        with self._lock:
            items = [r for r in self._read_all_locked() if str(r.get("id") or "").lower() != rid]
            items.insert(0, record)
            self._write_all_locked(items)
        return record

    def delete(self, rule_id: str) -> Dict[str, Any]:
        clean = (rule_id or "").strip().lower()
        with self._lock:
            items = self._read_all_locked()
            kept = [r for r in items if str(r.get("id") or "").lower() != clean]
            removed = len(items) - len(kept)
            self._write_all_locked(kept)
        return {"ok": True, "removed": removed, "id": clean}


visual_rule_store = VisualRuleStore()

__all__ = [
    "CompiledVisualScreen",
    "CompiledVisualStrategy",
    "SUPPORTED_OPERANDS",
    "SUPPORTED_OPERATORS",
    "VisualRuleStore",
    "compile_visual_screen",
    "compile_visual_strategy",
    "evaluate_condition_group",
    "export_rule_to_python",
    "visual_rule_store",
]
