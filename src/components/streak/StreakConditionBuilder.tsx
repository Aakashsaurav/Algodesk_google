import { useState } from 'react';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Code2,
  Copy,
  ClipboardPaste,
  FileCode2,
  FolderPlus,
  FunctionSquare,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from 'lucide-react';
import type {
  ParamSpecMeta,
  ScreenSpecMeta,
  StrategySpecMeta,
  VisualCondition,
  VisualConditionGroup,
  VisualOperandSpec,
} from './types';

export type BuilderEngineMode = 'basic' | 'advance' | 'python';

interface OperandOption {
  id: string;
  label: string;
  kind: string;
  default_period: number | null;
}

interface OperatorOption {
  id: string;
  label: string;
}

interface PrebuiltSetup {
  id: string;
  label: string;
  glyph: 'ma' | 'osc' | 'band' | 'vol';
  condition: VisualCondition;
}

const PREBUILT_SETUPS: PrebuiltSetup[] = [
  {
    id: 'sma',
    label: 'SMA',
    glyph: 'ma',
    condition: { left: { type: 'close', offset: 0 }, operator: '>', right: { type: 'sma', period: 20, offset: 0 }, enabled: true },
  },
  {
    id: 'rsi',
    label: 'RSI',
    glyph: 'osc',
    condition: { left: { type: 'rsi', period: 14, offset: 0 }, operator: '>', right: { type: 'number', value: 60 }, enabled: true },
  },
  {
    id: 'rsi_ma',
    label: 'RSI Moving Average',
    glyph: 'osc',
    condition: { left: { type: 'rsi', period: 14, offset: 0 }, operator: 'crosses_above', right: { type: 'rsi_ma', period: 14, param2: 9, offset: 0 }, enabled: true },
  },
  {
    id: 'ema',
    label: 'EMA',
    glyph: 'ma',
    condition: { left: { type: 'ema', period: 20, offset: 0 }, operator: 'crosses_above', right: { type: 'ema', period: 50, offset: 0 }, enabled: true },
  },
  {
    id: 'wma',
    label: 'WMA',
    glyph: 'ma',
    condition: { left: { type: 'close', offset: 0 }, operator: '>', right: { type: 'wma', period: 20, offset: 0 }, enabled: true },
  },
  {
    id: 'supertrend',
    label: 'Supertrend',
    glyph: 'band',
    condition: { left: { type: 'supertrend_dir', period: 10, param2: 3, offset: 0 }, operator: '==', right: { type: 'number', value: 1 }, enabled: true },
  },
  {
    id: 'vwap',
    label: 'VWAP',
    glyph: 'ma',
    condition: { left: { type: 'close', offset: 0 }, operator: '>', right: { type: 'vwap', period: 20, offset: 0 }, enabled: true },
  },
  {
    id: 'macd',
    label: 'MACD',
    glyph: 'osc',
    condition: { left: { type: 'macd', period: 12, param2: 26, offset: 0 }, operator: 'crosses_above', right: { type: 'macd_signal', period: 9, offset: 0 }, enabled: true },
  },
  {
    id: 'bbands',
    label: 'Bollinger',
    glyph: 'band',
    condition: { left: { type: 'close', offset: 0 }, operator: '>', right: { type: 'bb_upper', period: 20, param2: 2, offset: 0 }, enabled: true },
  },
  {
    id: 'vol_surge',
    label: 'Volume Surge',
    glyph: 'vol',
    condition: { left: { type: 'volume', offset: 0 }, operator: '>', right: { type: 'vol_sma', period: 20, multiplier: 1.5, offset: 0 }, enabled: true },
  },
];

const DEFAULT_OPERANDS: OperandOption[] = [
  { id: 'close', label: 'Close', kind: 'price', default_period: null },
  { id: 'open', label: 'Open', kind: 'price', default_period: null },
  { id: 'high', label: 'High', kind: 'price', default_period: null },
  { id: 'low', label: 'Low', kind: 'price', default_period: null },
  { id: 'volume', label: 'Volume', kind: 'volume', default_period: null },
  { id: 'number', label: 'Number', kind: 'constant', default_period: null },
  { id: 'sma', label: 'SMA', kind: 'indicator', default_period: 20 },
  { id: 'ema', label: 'EMA', kind: 'indicator', default_period: 20 },
  { id: 'wma', label: 'WMA', kind: 'indicator', default_period: 20 },
  { id: 'vwap', label: 'VWAP', kind: 'indicator', default_period: 20 },
  { id: 'rsi', label: 'RSI', kind: 'indicator', default_period: 14 },
  { id: 'rsi_ma', label: 'RSI Moving Average', kind: 'indicator', default_period: 14 },
  { id: 'supertrend_dir', label: 'Supertrend Dir (+1/-1)', kind: 'indicator', default_period: 10 },
  { id: 'macd', label: 'MACD', kind: 'indicator', default_period: 12 },
  { id: 'macd_signal', label: 'MACD Signal', kind: 'indicator', default_period: 9 },
  { id: 'bb_upper', label: 'Bollinger Upper', kind: 'indicator', default_period: 20 },
  { id: 'bb_lower', label: 'Bollinger Lower', kind: 'indicator', default_period: 20 },
  { id: 'adx', label: 'ADX', kind: 'indicator', default_period: 14 },
  { id: 'atr', label: 'ATR', kind: 'indicator', default_period: 14 },
  { id: 'rolling_high', label: 'Rolling High', kind: 'indicator', default_period: 20 },
  { id: 'rolling_low', label: 'Rolling Low', kind: 'indicator', default_period: 20 },
  { id: 'vol_sma', label: 'Volume SMA', kind: 'volume', default_period: 20 },
];

const DEFAULT_OPERATORS: OperatorOption[] = [
  { id: '>', label: 'higher than (>)' },
  { id: '<', label: 'lower than (<)' },
  { id: '>=', label: 'greater or equal (>=)' },
  { id: '<=', label: 'lower or equal (<=)' },
  { id: '==', label: 'equal to (==)' },
  { id: '!=', label: 'not equal (!=)' },
  { id: 'crosses_above', label: 'crosses above' },
  { id: 'crosses_below', label: 'crosses below' },
  { id: 'up_by_pct', label: 'up by % from' },
  { id: 'down_by_pct', label: 'down by % from' },
];

const ADVANCE_TFS = [
  { id: '1minute', label: '1min' },
  { id: '5minute', label: '5min' },
  { id: '15minute', label: '15min' },
  { id: '60minute', label: '1hour' },
  { id: '1day', label: '1day' },
];

const MTF_TFS = [
  { id: '5m', label: '5m' },
  { id: '15m', label: '15m' },
  { id: '1h', label: '1h' },
  { id: '1d', label: '1D' },
  { id: '1w', label: '1W' },
];

const FORMULA_EXAMPLES = [
  { label: 'High(-2) > Max(6, High(0))', expr: 'High(-2) > Max(6, High(0))' },
  { label: 'EMA(high, 20, 0) >= Period max(14, Close(-2))', expr: 'EMA(high, 20, 0) >= Period max(14, Close(-2))' },
  { label: 'Close(0) > SMA(20) * 1.02', expr: 'Close(0) > SMA(20) * 1.02' },
  { label: 'Crosses_Above(EMA(20), EMA(50))', expr: 'Crosses_Above(EMA(20), EMA(50))' },
  { label: 'RSI(14) > 60 and Volume(0) > Vol_SMA(20) * 1.5', expr: 'RSI(14) > 60 and Volume(0) > Vol_SMA(20) * 1.5' },
];

function isConditionGroup(node: VisualCondition | VisualConditionGroup): node is VisualConditionGroup {
  return 'conditions' in node && Array.isArray(node.conditions);
}

function isFormulaCondition(node: VisualCondition): boolean {
  return node.kind === 'formula' || (typeof node.expression === 'string' && node.expression.trim().length > 0);
}

function defaultCondition(tf = '1day'): VisualCondition {
  return {
    left: { type: 'close', offset: 0 },
    operator: '>',
    right: { type: 'ema', period: 20, offset: 0 },
    enabled: true,
    timeframe: tf,
  };
}

function defaultFormulaCondition(tf = '1day', expr = 'High(-2) > Max(6, High(0))'): VisualCondition {
  return {
    kind: 'formula',
    expression: expr,
    left: { type: 'close', offset: 0 },
    operator: '>',
    right: { type: 'number', value: 0 },
    enabled: true,
    timeframe: tf,
  };
}

function SetupGlyph({ kind }: { kind: PrebuiltSetup['glyph'] }) {
  if (kind === 'osc') {
    return (
      <svg viewBox="0 0 44 26" className="h-6 w-11">
        <line x1="2" y1="7" x2="42" y2="7" stroke="#334155" strokeDasharray="2 2" strokeWidth="1" />
        <line x1="2" y1="19" x2="42" y2="19" stroke="#334155" strokeDasharray="2 2" strokeWidth="1" />
        <polyline points="2,18 10,14 17,17 25,9 33,12 42,6" fill="none" stroke="#38bdf8" strokeWidth="1.6" />
      </svg>
    );
  }
  if (kind === 'band') {
    return (
      <svg viewBox="0 0 44 26" className="h-6 w-11">
        <polyline points="2,10 14,7 28,9 42,4" fill="none" stroke="#10b981" strokeWidth="1.2" opacity="0.8" />
        <polyline points="2,20 14,18 28,21 42,16" fill="none" stroke="#ef4444" strokeWidth="1.2" opacity="0.8" />
        <polyline points="2,15 14,13 28,14 42,9" fill="none" stroke="#e2e8f0" strokeWidth="1.5" />
      </svg>
    );
  }
  if (kind === 'vol') {
    return (
      <svg viewBox="0 0 44 26" className="h-6 w-11">
        <rect x="4" y="16" width="4" height="8" fill="#475569" rx="1" />
        <rect x="11" y="14" width="4" height="10" fill="#475569" rx="1" />
        <rect x="18" y="17" width="4" height="7" fill="#ef4444" rx="1" />
        <rect x="25" y="11" width="4" height="13" fill="#10b981" rx="1" />
        <rect x="32" y="4" width="5" height="20" fill="#38bdf8" rx="1" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 44 26" className="h-6 w-11">
      <line x1="8" y1="12" x2="8" y2="22" stroke="#ef4444" strokeWidth="1.5" />
      <line x1="16" y1="10" x2="16" y2="20" stroke="#10b981" strokeWidth="1.5" />
      <line x1="24" y1="7" x2="24" y2="17" stroke="#10b981" strokeWidth="1.5" />
      <line x1="32" y1="5" x2="32" y2="14" stroke="#10b981" strokeWidth="1.5" />
      <polyline points="2,21 14,16 26,12 40,6" fill="none" stroke="#38bdf8" strokeWidth="1.6" />
    </svg>
  );
}

interface StreakConditionBuilderProps {
  kind: 'screen' | 'strategy';
  mode: BuilderEngineMode;
  onModeChange: (next: BuilderEngineMode) => void;
  group: VisualConditionGroup;
  onGroupChange: (next: VisualConditionGroup) => void;
  defaultTimeframe: string;
  operands?: OperandOption[];
  operators?: OperatorOption[];
  // Python plugin props
  pythonPlugins: Array<ScreenSpecMeta | StrategySpecMeta>;
  selectedPythonId: string;
  onSelectPythonId: (id: string) => void;
  pythonParams: Record<string, unknown>;
  onPythonParamsChange: (next: Record<string, unknown>) => void;
  mtfTimeframes?: string[];
  onMtfTimeframesChange?: (next: string[]) => void;
  onRefreshPlugins?: () => void;
  onExportVisualToPython?: () => Promise<void>;
  exportingPython?: boolean;
  hidePythonMode?: boolean;
  titleLabel?: string;
}

const PYTHON_SCREEN_TEMPLATE = `"""Custom AlgoDesk Python Screen Plugin."""
from __future__ import annotations
from typing import Any, Dict, List
from indicators.toolkit import IndicatorToolkit
from screener.base import BaseScreen, ColumnSpec, ParamSpec, ScreenMatch


class CustomMomentumScreen(BaseScreen):
    id = "custom_momentum_py"
    name = "Custom Momentum (Python)"
    description = "Matches symbols where Close > EMA(fast) and RSI(14) > threshold."
    min_bars = 40
    params = [
        ParamSpec(name="fast_ema", label="Fast EMA", kind="int", default=20, min=5, max=200),
        ParamSpec(name="min_rsi", label="Min RSI", kind="float", default=58.0, min=10.0, max=90.0),
    ]

    def get_columns(self, params: Dict[str, Any]) -> List[ColumnSpec]:
        return [
            ColumnSpec(key="ema", label="EMA", fmt="number", decimals=2),
            ColumnSpec(key="rsi", label="RSI(14)", fmt="number", decimals=1),
        ]

    def evaluate(self, candles: List[Dict[str, Any]], params: Dict[str, Any], tk: IndicatorToolkit) -> ScreenMatch:
        ema = tk.ema(int(params.get("fast_ema", 20)))[-1]
        rsi = tk.rsi(14)[-1]
        close = float(candles[-1]["close"])
        matched = bool(ema is not None and rsi is not None and close > ema and rsi >= float(params.get("min_rsi", 58.0)))
        return ScreenMatch(
            matched=matched,
            signal="BULLISH" if matched else "NEUTRAL",
            score=85 if matched else 20,
            tags=["CUSTOM_PY"],
            metrics={"ema": round(ema, 2) if ema else None, "rsi": round(rsi, 2) if rsi else None},
        )
`;

const PYTHON_STRATEGY_TEMPLATE = `"""Custom AlgoDesk Python Strategy Plugin."""
from __future__ import annotations
from typing import Any, Dict, List, Tuple
from backtest.base import BacktestStrategy
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class CustomBreakoutStrategy(BacktestStrategy):
    id = "custom_breakout_py"
    name = "Custom Breakout (Python)"
    description = "Enters when Close breaks 20-bar high with RSI > 55; exits below EMA(20)."
    params = [
        ParamSpec(name="lookback", label="Breakout Lookback", kind="int", default=20, min=5, max=120),
        ParamSpec(name="ema_period", label="Exit EMA", kind="int", default=20, min=5, max=200),
    ]

    def prepare(self, candles: List[Dict[str, Any]], params: Dict[str, Any], toolkit: IndicatorToolkit) -> None:
        self._highs = toolkit.rolling_high(int(params.get("lookback", 20)))
        self._ema = toolkit.ema(int(params.get("ema_period", 20)))
        self._rsi = toolkit.rsi(14)

    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        if index < 2:
            return False, False
        prev_hi = self._highs[index - 1]
        ema_v = self._ema[index]
        rsi_v = self._rsi[index]
        c = float(bar["close"])
        enter = bool(prev_hi is not None and rsi_v is not None and c > prev_hi and rsi_v > 55.0)
        exit_sig = bool(ema_v is not None and c < ema_v)
        return enter, exit_sig
`;

export function StreakConditionBuilder({
  kind,
  mode,
  onModeChange,
  group,
  onGroupChange,
  defaultTimeframe,
  operands,
  operators,
  pythonPlugins,
  selectedPythonId,
  onSelectPythonId,
  pythonParams,
  onPythonParamsChange,
  mtfTimeframes = ['5m', '15m', '1h', '1d', '1w'],
  onMtfTimeframesChange,
  onRefreshPlugins,
  onExportVisualToPython,
  exportingPython,
  hidePythonMode = false,
  titleLabel = 'Conditions',
}: StreakConditionBuilderProps) {
  const opList = operands && operands.length > 0 ? operands : DEFAULT_OPERANDS;
  const cmpList = operators && operators.length > 0 ? operators : DEFAULT_OPERATORS;

  const [clipboardNode, setClipboardNode] = useState<string>('');
  const [pasteNotice, setPasteNotice] = useState('');
  const [formulaStatus, setFormulaStatus] = useState<
    Record<string, { checking?: boolean; valid?: boolean; error?: string; series?: string[] }>
  >({});

  // Python code editor state
  const [pyEditorOpen, setPyEditorOpen] = useState(false);
  const [pyFilename, setPyFilename] = useState(kind === 'screen' ? 'custom_momentum_py.py' : 'custom_breakout_py.py');
  const [pyCode, setPyCode] = useState(kind === 'screen' ? PYTHON_SCREEN_TEMPLATE : PYTHON_STRATEGY_TEMPLATE);
  const [pyBusy, setPyBusy] = useState(false);
  const [pyFeedback, setPyFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const selectedPlugin = pythonPlugins.find((p) => p.id === selectedPythonId) || pythonPlugins[0];
  const isMtfPlugin =
    kind === 'screen' &&
    Boolean((selectedPlugin as ScreenSpecMeta | undefined)?.supports_multi_timeframe || selectedPythonId === 'support_resistance');

  const validateFormulaExpr = async (key: string, expression: string, expectBoolean: boolean) => {
    const trimmed = expression.trim();
    if (!trimmed) {
      setFormulaStatus((prev) => ({ ...prev, [key]: { valid: false, error: 'Formula cannot be empty.' } }));
      return;
    }
    setFormulaStatus((prev) => ({ ...prev, [key]: { checking: true } }));
    try {
      const res = await fetch('/api/streak/formula/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expression: trimmed, expect_boolean: expectBoolean }),
      });
      const body = await res.json().catch(() => ({}));
      if (body.valid) {
        setFormulaStatus((prev) => ({
          ...prev,
          [key]: { valid: true, series: Array.isArray(body.series_keys) ? body.series_keys : [] },
        }));
      } else {
        setFormulaStatus((prev) => ({
          ...prev,
          [key]: { valid: false, error: body.error || body.detail || 'Invalid formula syntax.' },
        }));
      }
    } catch {
      setFormulaStatus((prev) => ({ ...prev, [key]: { valid: false, error: 'Validation request failed.' } }));
    }
  };

  const updateConditionAt = (idx: number, patch: Partial<VisualCondition>) => {
    const next = [...group.conditions];
    const cur = next[idx];
    if (!cur || isConditionGroup(cur)) return;
    next[idx] = { ...cur, ...patch };
    onGroupChange({ ...group, conditions: next });
  };

  const updateOperandAt = (idx: number, side: 'left' | 'right', patch: Partial<VisualOperandSpec>) => {
    const cur = group.conditions[idx];
    if (!cur || isConditionGroup(cur)) return;
    const prevSide = cur[side] || { type: 'close' };
    let nextSide: VisualOperandSpec = { ...prevSide, ...patch };
    if (patch.type && patch.type !== prevSide.type) {
      const meta = opList.find((o) => o.id === patch.type);
      if (patch.type === 'formula') {
        nextSide = {
          type: 'formula',
          expr:
            prevSide.expr ||
            (side === 'left' ? 'EMA(high, 20, 0)' : 'Period max(14, Close(-2))'),
        };
      } else if (patch.type === 'number') {
        nextSide = { type: 'number', value: prevSide.value ?? 50 };
      } else if (meta?.default_period) {
        nextSide = { type: patch.type, period: meta.default_period, offset: prevSide.offset ?? 0 };
      } else {
        nextSide = { type: patch.type, offset: prevSide.offset ?? 0 };
      }
    }
    updateConditionAt(idx, { [side]: nextSide });
  };

  const removeNodeAt = (idx: number) => {
    const next = group.conditions.filter((_, i) => i !== idx);
    onGroupChange({ ...group, conditions: next });
  };

  const duplicateNodeAt = (idx: number) => {
    const target = group.conditions[idx];
    if (!target) return;
    const clone = JSON.parse(JSON.stringify(target)) as VisualCondition | VisualConditionGroup;
    const next = [...group.conditions];
    next.splice(idx + 1, 0, clone);
    onGroupChange({ ...group, conditions: next });
  };

  const addCondition = (preset?: VisualCondition) => {
    const cond = preset
      ? (JSON.parse(JSON.stringify({ ...preset, timeframe: defaultTimeframe })) as VisualCondition)
      : defaultCondition(defaultTimeframe);
    onGroupChange({ ...group, conditions: [...group.conditions, cond] });
  };

  const addFormulaCondition = (expr = 'High(-2) > Max(6, High(0))') => {
    onGroupChange({ ...group, conditions: [...group.conditions, defaultFormulaCondition(defaultTimeframe, expr)] });
  };

  const addNestedGroup = () => {
    const sub: VisualConditionGroup = {
      logic: group.logic === 'AND' ? 'OR' : 'AND',
      enabled: true,
      conditions: [defaultCondition(defaultTimeframe)],
    };
    onGroupChange({ ...group, conditions: [...group.conditions, sub] });
  };

  const handleCopyAll = () => {
    const serialized = JSON.stringify(group);
    setClipboardNode(serialized);
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      void navigator.clipboard.writeText(serialized).catch(() => {});
    }
    setPasteNotice('Copied all conditions');
    window.setTimeout(() => setPasteNotice(''), 2000);
  };

  const handlePasteAll = () => {
    if (!clipboardNode) {
      setPasteNotice('Nothing copied yet');
      window.setTimeout(() => setPasteNotice(''), 2000);
      return;
    }
    try {
      const parsed = JSON.parse(clipboardNode) as VisualConditionGroup | VisualCondition;
      if ('conditions' in parsed && Array.isArray(parsed.conditions)) {
        onGroupChange(parsed);
      } else {
        onGroupChange({ ...group, conditions: [...group.conditions, parsed] });
      }
      setPasteNotice('Pasted conditions');
      window.setTimeout(() => setPasteNotice(''), 2000);
    } catch {
      setPasteNotice('Invalid clipboard condition');
    }
  };

  const handleLoadPythonSource = async () => {
    if (!selectedPythonId) return;
    setPyBusy(true);
    setPyFeedback(null);
    try {
      const res = await fetch(
        `/api/streak/plugins/source?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(selectedPythonId)}`
      );
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not load source file.');
      setPyFilename(body.filename || `${selectedPythonId}.py`);
      setPyCode(body.code || '');
      setPyEditorOpen(true);
    } catch (err) {
      setPyFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Load failed.' });
    } finally {
      setPyBusy(false);
    }
  };

  const handleSavePythonFile = async () => {
    setPyBusy(true);
    setPyFeedback(null);
    try {
      const res = await fetch('/api/streak/plugins/python', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, filename: pyFilename, code: pyCode }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Save failed.');
      setPyFeedback({ type: 'ok', text: `Saved ${body.filename} and hot-reloaded registry.` });
      if (onRefreshPlugins) onRefreshPlugins();
    } catch (err) {
      setPyFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Save failed.' });
    } finally {
      setPyBusy(false);
    }
  };

  const renderOperandControls = (
    spec: VisualOperandSpec,
    onChange: (patch: Partial<VisualOperandSpec>) => void,
    compact = false,
    allowFormula = false,
    statusKey?: string
  ) => {
    const safeSpec: VisualOperandSpec = spec || { type: 'close', offset: 0 };
    const isFormula = safeSpec.type === 'formula';
    const meta = opList.find((o) => o.id === safeSpec.type);
    const isNum = safeSpec.type === 'number';
    const needsPeriod = Boolean(meta?.default_period);
    const st = statusKey ? formulaStatus[statusKey] : undefined;

    if (isFormula) {
      return (
        <div className={`flex flex-1 flex-col gap-1 ${compact ? '' : 'min-w-[180px]'}`}>
          <div className="flex items-center gap-1">
            <span className="shrink-0 rounded bg-sky-950/80 px-1.5 py-0.5 font-mono text-[10px] font-bold text-sky-300">
              fx
            </span>
            <input
              type="text"
              value={safeSpec.expr || ''}
              onChange={(e) => onChange({ expr: e.target.value })}
              onBlur={() => {
                if (statusKey && safeSpec.expr?.trim()) {
                  void validateFormulaExpr(statusKey, safeSpec.expr, false);
                }
              }}
              placeholder="e.g. Period max(14, Close(-2))"
              aria-label="Formula expression operand"
              className="min-h-[32px] w-full flex-1 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-sky-200 placeholder-slate-500 focus:border-sky-400 focus:outline-none"
            />
            <button
              type="button"
              onClick={() => onChange({ type: 'close', offset: 0 })}
              title="Switch back to dropdown operand"
              className="shrink-0 rounded border border-slate-700 bg-[#0b1120] px-1.5 py-1 text-[10px] font-semibold text-slate-400 hover:border-sky-400 hover:text-slate-100"
            >
              Block
            </button>
          </div>
          {st && !st.checking && (
            <div className={`font-mono text-[10px] ${st.valid ? 'text-emerald-400' : 'text-red-400'}`}>
              {st.valid ? 'Valid expression' : st.error}
            </div>
          )}
        </div>
      );
    }

    return (
      <div className={`flex flex-wrap items-center gap-1.5 ${compact ? '' : 'flex-1'}`}>
        <select
          value={safeSpec.type}
          onChange={(e) => onChange({ type: e.target.value })}
          className="min-h-[34px] min-w-[112px] flex-1 rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
        >
          {opList.map((op) => (
            <option key={op.id} value={op.id}>
              {op.label}
            </option>
          ))}
          {allowFormula && <option value="formula">Formula (Math Expr)</option>}
        </select>
        {isNum ? (
          <input
            type="number"
            step="0.5"
            value={safeSpec.value ?? 0}
            onChange={(e) => onChange({ value: Number(e.target.value) })}
            aria-label="Constant value"
            className="min-h-[34px] w-20 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
          />
        ) : (
          <>
            {needsPeriod && (
              <input
                type="number"
                min={1}
                max={400}
                value={safeSpec.period ?? meta?.default_period ?? 14}
                onChange={(e) => onChange({ period: Math.max(1, Number(e.target.value) || 1) })}
                title="Period"
                aria-label="Indicator period"
                className="min-h-[34px] w-14 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
              />
            )}
            <select
              value={String(safeSpec.offset ?? 0)}
              onChange={(e) => onChange({ offset: Number(e.target.value) })}
              title="Candle offset (0 = current, -1 = previous)"
              aria-label="Candle offset"
              className="min-h-[34px] w-14 rounded-md border border-slate-700 bg-[#0b1120] px-1.5 font-mono text-[11px] text-slate-300 focus:border-sky-400 focus:outline-none"
            >
              <option value="0">(0)</option>
              <option value="-1">(-1)</option>
              <option value="-2">(-2)</option>
              <option value="-3">(-3)</option>
              <option value="-5">(-5)</option>
              <option value="-6">(-6)</option>
            </select>
          </>
        )}
        {allowFormula && (
          <button
            type="button"
            onClick={() => onChange({ type: 'formula' })}
            title="Switch to math formula expression (e.g. Period max(14, Close(-2)))"
            className="min-h-[30px] shrink-0 rounded border border-slate-700 bg-[#0b1120] px-1.5 font-mono text-[10px] font-bold text-sky-400 hover:border-sky-400"
          >
            fx
          </button>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* Header row: Conditions + Basic / Advance / Python(.py) segmented selector */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <span className="text-xs font-semibold text-slate-200">{titleLabel}</span>
          <div className="inline-flex items-center rounded-lg border border-slate-800 bg-[#0b1120] p-0.5">
            {(['basic', 'advance', ...(hidePythonMode ? [] : (['python'] as const))] as BuilderEngineMode[]).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => onModeChange(m)}
                className={`min-h-[28px] whitespace-nowrap rounded-md px-3 text-xs font-semibold transition-colors ${
                  mode === m
                    ? 'bg-sky-500 text-slate-950'
                    : 'text-slate-400 hover:text-slate-100'
                }`}
              >
                {m === 'basic' ? 'Basic' : m === 'advance' ? 'Advance' : 'Python (.py)'}
              </button>
            ))}
          </div>
        </div>

        {mode !== 'python' && onExportVisualToPython && (
          <button
            type="button"
            onClick={() => void onExportVisualToPython()}
            disabled={exportingPython}
            className="inline-flex min-h-[30px] items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-[11px] font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
          >
            <Code2 className="h-3.5 w-3.5" />
            <span>{exportingPython ? 'Exporting...' : 'Export to .py'}</span>
          </button>
        )}
      </div>

      {/* ── MODE 1: BASIC ────────────────────────────────────────────── */}
      {mode === 'basic' && (
        <div className="space-y-3">
          {group.conditions.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-700 bg-[#0b1120]/60 p-4 text-center text-xs text-slate-400">
              Pick a Pre-built Setup below or click <strong className="text-slate-200">+ Add Another Condition</strong>.
            </div>
          ) : (
            <div className="space-y-2.5">
              {group.conditions.map((node, idx) => {
                if (isConditionGroup(node)) {
                  return (
                    <div
                      key={`grp-${idx}`}
                      className="flex items-center justify-between rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-300"
                    >
                      <span>Nested ({node.logic}) Group — {node.conditions.length} conditions (switch to Advance to edit)</span>
                      <button
                        type="button"
                        onClick={() => removeNodeAt(idx)}
                        className="text-slate-400 hover:text-red-400"
                        aria-label="Remove nested group"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  );
                }
                if (isFormulaCondition(node)) {
                  return (
                    <div
                      key={`basic-formula-${idx}`}
                      className="flex items-center justify-between gap-2 rounded-lg border border-sky-800/60 bg-sky-950/20 px-3 py-2 text-xs text-sky-200"
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="rounded bg-sky-900/70 px-1.5 py-0.5 font-mono text-[10px] font-bold text-sky-300">
                          fx
                        </span>
                        <span className="truncate font-mono">{node.expression}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => onModeChange('advance')}
                          className="text-[11px] font-semibold text-sky-400 hover:underline"
                        >
                          Edit in Advance
                        </button>
                        <button
                          type="button"
                          onClick={() => removeNodeAt(idx)}
                          className="text-slate-400 hover:text-red-400"
                          aria-label="Remove formula condition"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  );
                }
                const isPctOp = node.operator === 'up_by_pct' || node.operator === 'down_by_pct';
                return (
                  <div key={`basic-${idx}`} className="space-y-2">
                    {idx > 0 && (
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => onGroupChange({ ...group, logic: group.logic === 'AND' ? 'OR' : 'AND' })}
                          className="rounded border border-sky-800/70 bg-sky-950/50 px-2.5 py-0.5 font-mono text-[11px] font-semibold uppercase text-sky-300 hover:border-sky-400"
                        >
                          {group.logic.toLowerCase()}
                        </button>
                      </div>
                    )}
                    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-800 bg-[#0b1120]/70 p-2.5">
                      <span className="w-4 shrink-0 text-xs font-medium text-slate-400">{idx === 0 ? 'If' : ''}</span>
                      {renderOperandControls(node.left, (p) => updateOperandAt(idx, 'left', p))}

                      <div className="flex items-center gap-1.5">
                        <select
                          value={node.operator}
                          onChange={(e) => {
                            const op = e.target.value;
                            updateConditionAt(idx, {
                              operator: op,
                              pct: op === 'up_by_pct' || op === 'down_by_pct' ? (node.pct ?? 1.0) : undefined,
                            });
                          }}
                          className="min-h-[34px] rounded-md border border-slate-700 bg-[#111827] px-2 text-xs font-medium text-amber-300 focus:border-sky-400 focus:outline-none"
                        >
                          {cmpList.map((c) => (
                            <option key={c.id} value={c.id}>
                              {c.label}
                            </option>
                          ))}
                        </select>
                        {isPctOp && (
                          <div className="flex items-center rounded-md border border-slate-700 bg-[#0b1120] px-1.5">
                            <input
                              type="number"
                              step="0.25"
                              min={0}
                              value={node.pct ?? 1}
                              onChange={(e) => updateConditionAt(idx, { pct: Number(e.target.value) })}
                              aria-label="Percentage threshold"
                              className="min-h-[32px] w-12 bg-transparent font-mono text-xs text-slate-100 tabular-nums focus:outline-none"
                            />
                            <span className="text-[11px] text-slate-400">%</span>
                          </div>
                        )}
                      </div>

                      {renderOperandControls(node.right, (p) => updateOperandAt(idx, 'right', p))}

                      <button
                        type="button"
                        onClick={() => removeNodeAt(idx)}
                        aria-label="Delete condition"
                        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-slate-500 hover:bg-red-950/50 hover:text-red-400"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <div className="flex items-center justify-between pt-1">
            <span className="text-xs font-medium text-slate-400">Pre-built Setups</span>
            <button
              type="button"
              onClick={() => addCondition()}
              className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 text-xs font-semibold text-sky-300 hover:border-sky-400 hover:bg-sky-950/60"
            >
              <Plus className="h-3.5 w-3.5" />
              <span>Add Another Condition</span>
            </button>
          </div>

          {/* Pre-built Setups scrollable card rail */}
          <div className="flex gap-2 overflow-x-auto pb-1">
            {PREBUILT_SETUPS.map((setup) => (
              <button
                key={setup.id}
                type="button"
                onClick={() => addCondition(setup.condition)}
                className="flex min-w-[96px] shrink-0 flex-col items-center justify-center gap-1.5 rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2.5 text-center transition-colors hover:border-sky-500/70 hover:bg-slate-900"
              >
                <SetupGlyph kind={setup.glyph} />
                <span className="whitespace-nowrap text-[11px] font-semibold text-slate-200">{setup.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── MODE 2: ADVANCE ──────────────────────────────────────────── */}
      {mode === 'advance' && (
        <div className="space-y-3">
          {group.conditions.map((node, idx) => {
            if (isConditionGroup(node)) {
              return (
                <div key={`adv-grp-${idx}`} className="space-y-2 rounded-lg border border-dashed border-sky-800/70 bg-[#0b1120]/60 p-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-bold text-sky-400">( Group )</span>
                      <button
                        type="button"
                        onClick={() => {
                          const next = [...group.conditions];
                          next[idx] = { ...node, logic: node.logic === 'AND' ? 'OR' : 'AND' };
                          onGroupChange({ ...group, conditions: next });
                        }}
                        className="rounded border border-slate-700 bg-[#111827] px-2 py-0.5 font-mono text-[11px] font-semibold text-sky-300"
                      >
                        {node.logic}
                      </button>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => {
                          const next = [...group.conditions];
                          next[idx] = {
                            ...node,
                            conditions: [...node.conditions, defaultCondition(defaultTimeframe)],
                          };
                          onGroupChange({ ...group, conditions: next });
                        }}
                        className="rounded border border-slate-700 px-2 py-1 text-[11px] font-medium text-slate-300 hover:border-sky-400"
                      >
                        + Row
                      </button>
                      <button
                        type="button"
                        onClick={() => removeNodeAt(idx)}
                        className="p-1 text-slate-400 hover:text-red-400"
                        aria-label="Delete group"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                  {node.conditions.map((subNode, sIdx) => {
                    if (isConditionGroup(subNode)) return null;
                    return (
                      <div key={`sub-${sIdx}`} className="flex flex-wrap items-center gap-2 rounded-md border border-slate-800 bg-[#111827] p-2">
                        {renderOperandControls(subNode.left, (p) => {
                          const nextSub = [...node.conditions];
                          const curSub = nextSub[sIdx] as VisualCondition;
                          nextSub[sIdx] = { ...curSub, left: { ...curSub.left, ...p } };
                          const nextTop = [...group.conditions];
                          nextTop[idx] = { ...node, conditions: nextSub };
                          onGroupChange({ ...group, conditions: nextTop });
                        }, true)}
                        <select
                          value={subNode.operator}
                          onChange={(e) => {
                            const nextSub = [...node.conditions];
                            const curSub = nextSub[sIdx] as VisualCondition;
                            nextSub[sIdx] = { ...curSub, operator: e.target.value };
                            const nextTop = [...group.conditions];
                            nextTop[idx] = { ...node, conditions: nextSub };
                            onGroupChange({ ...group, conditions: nextTop });
                          }}
                          className="min-h-[32px] rounded border border-slate-700 bg-[#0b1120] px-2 text-xs text-amber-300"
                        >
                          {cmpList.map((c) => (
                            <option key={c.id} value={c.id}>{c.label}</option>
                          ))}
                        </select>
                        {renderOperandControls(subNode.right, (p) => {
                          const nextSub = [...node.conditions];
                          const curSub = nextSub[sIdx] as VisualCondition;
                          nextSub[sIdx] = { ...curSub, right: { ...curSub.right, ...p } };
                          const nextTop = [...group.conditions];
                          nextTop[idx] = { ...node, conditions: nextSub };
                          onGroupChange({ ...group, conditions: nextTop });
                        }, true)}
                        <button
                          type="button"
                          onClick={() => {
                            const nextSub = node.conditions.filter((_, k) => k !== sIdx);
                            const nextTop = [...group.conditions];
                            nextTop[idx] = { ...node, conditions: nextSub };
                            onGroupChange({ ...group, conditions: nextTop });
                          }}
                          className="p-1 text-slate-500 hover:text-red-400"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              );
            }

            const enabled = node.enabled !== false;
            const isFormulaRow = isFormulaCondition(node);
            const isPctOp = node.operator === 'up_by_pct' || node.operator === 'down_by_pct';
            const rowKey = `adv-row-${idx}`;
            const rowStatus = formulaStatus[rowKey];

            return (
              <div key={`adv-${idx}`} className="space-y-2">
                {idx > 0 && (
                  <button
                    type="button"
                    onClick={() => onGroupChange({ ...group, logic: group.logic === 'AND' ? 'OR' : 'AND' })}
                    className="rounded border border-sky-800/70 bg-sky-950/50 px-2.5 py-0.5 font-mono text-[11px] font-semibold uppercase text-sky-300 hover:border-sky-400"
                  >
                    {group.logic.toLowerCase()}
                  </button>
                )}

                {isFormulaRow ? (
                  <div
                    className={`space-y-2 rounded-lg border p-3 transition-opacity ${
                      enabled
                        ? 'border-sky-700/70 bg-[#0b1120]/90'
                        : 'border-slate-800/60 bg-[#0b1120]/30 opacity-50'
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <select
                        value={node.timeframe || defaultTimeframe}
                        onChange={(e) => updateConditionAt(idx, { timeframe: e.target.value })}
                        aria-label="Formula condition timeframe"
                        className="min-h-[34px] rounded-md border border-slate-700 bg-[#111827] px-2 font-mono text-[11px] text-slate-300 focus:border-sky-400 focus:outline-none"
                      >
                        {ADVANCE_TFS.map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.label}
                          </option>
                        ))}
                      </select>

                      <span className="inline-flex items-center gap-1 rounded bg-sky-950/80 px-2 py-1 font-mono text-[11px] font-bold text-sky-300">
                        <FunctionSquare className="h-3.5 w-3.5" />
                        <span>Formula</span>
                      </span>

                      <input
                        type="text"
                        value={node.expression || ''}
                        onChange={(e) => updateConditionAt(idx, { kind: 'formula', expression: e.target.value })}
                        onBlur={() => void validateFormulaExpr(rowKey, node.expression || '', true)}
                        placeholder="High(-2) > Max(6, High(0))"
                        aria-label="Mathematical formula expression"
                        className="min-h-[36px] min-w-[220px] flex-1 rounded-md border border-dashed border-sky-700/80 bg-[#111827] px-3 font-mono text-xs text-sky-200 placeholder-slate-500 focus:border-sky-400 focus:outline-none"
                      />

                      <button
                        type="button"
                        onClick={() => void validateFormulaExpr(rowKey, node.expression || '', true)}
                        disabled={rowStatus?.checking}
                        className="inline-flex min-h-[34px] items-center gap-1 rounded-md border border-slate-700 bg-[#111827] px-2.5 text-[11px] font-semibold text-slate-200 hover:border-sky-400"
                      >
                        <Check className="h-3 w-3 text-sky-400" />
                        <span>{rowStatus?.checking ? 'Checking...' : 'Validate'}</span>
                      </button>

                      {/* Row controls */}
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          role="switch"
                          aria-checked={enabled}
                          title={enabled ? 'Disable condition' : 'Enable condition'}
                          onClick={() => updateConditionAt(idx, { enabled: !enabled })}
                          className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
                            enabled ? 'bg-sky-500' : 'bg-slate-700'
                          }`}
                        >
                          <span
                            className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                              enabled ? 'translate-x-4' : 'translate-x-1'
                            }`}
                          />
                        </button>
                        <button
                          type="button"
                          onClick={() => duplicateNodeAt(idx)}
                          title="Duplicate formula"
                          className="flex h-7 w-7 items-center justify-center rounded text-slate-400 hover:bg-slate-800 hover:text-slate-100"
                        >
                          <Copy className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => removeNodeAt(idx)}
                          title="Delete formula"
                          className="flex h-7 w-7 items-center justify-center rounded text-slate-400 hover:bg-red-950/60 hover:text-red-400"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>

                    {rowStatus && !rowStatus.checking && (
                      <div
                        className={`flex items-center gap-1.5 font-mono text-[11px] ${
                          rowStatus.valid ? 'text-emerald-400' : 'text-red-400'
                        }`}
                      >
                        {rowStatus.valid ? (
                          <>
                            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                            <span>
                              Valid formula
                              {rowStatus.series && rowStatus.series.length > 0
                                ? ` · Indicators: ${rowStatus.series.join(', ')}`
                                : ''}
                            </span>
                          </>
                        ) : (
                          <>
                            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                            <span>{rowStatus.error}</span>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <div
                    className={`flex flex-wrap items-center gap-2 rounded-lg border p-2.5 transition-opacity ${
                      enabled ? 'border-slate-700 bg-[#0b1120]/80' : 'border-slate-800/60 bg-[#0b1120]/30 opacity-50'
                    }`}
                  >
                    {/* Timeframe badge */}
                    <select
                      value={node.timeframe || defaultTimeframe}
                      onChange={(e) => updateConditionAt(idx, { timeframe: e.target.value })}
                      aria-label="Condition timeframe"
                      className="min-h-[34px] rounded-md border border-slate-700 bg-[#111827] px-2 font-mono text-[11px] text-slate-300 focus:border-sky-400 focus:outline-none"
                    >
                      {ADVANCE_TFS.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.label}
                        </option>
                      ))}
                    </select>

                    {/* Left expression (dashed Streak style with fx math support) */}
                    <div className="flex flex-1 items-center gap-1 rounded-md border border-dashed border-slate-700 bg-[#111827]/70 p-1">
                      {renderOperandControls(
                        node.left,
                        (p) => updateOperandAt(idx, 'left', p),
                        false,
                        true,
                        `${rowKey}-left`
                      )}
                    </div>

                    {/* Comparator */}
                    <div className="flex items-center gap-1 rounded-md border border-dashed border-slate-700 bg-[#111827]/70 p-1">
                      <select
                        value={node.operator}
                        onChange={(e) => {
                          const op = e.target.value;
                          updateConditionAt(idx, {
                            operator: op,
                            pct: op === 'up_by_pct' || op === 'down_by_pct' ? (node.pct ?? 1.0) : undefined,
                          });
                        }}
                        className="min-h-[32px] bg-transparent px-1.5 text-xs font-medium text-amber-300 focus:outline-none"
                      >
                        {cmpList.map((c) => (
                          <option key={c.id} value={c.id} className="bg-[#111827]">
                            {c.label}
                          </option>
                        ))}
                      </select>
                      {isPctOp && (
                        <input
                          type="number"
                          step="0.25"
                          value={node.pct ?? 1}
                          onChange={(e) => updateConditionAt(idx, { pct: Number(e.target.value) })}
                          className="w-12 rounded border border-slate-700 bg-[#0b1120] px-1.5 py-1 font-mono text-xs text-slate-100"
                        />
                      )}
                    </div>

                    {/* Right expression (dashed Streak style with fx math support) */}
                    <div className="flex flex-1 items-center gap-1 rounded-md border border-dashed border-slate-700 bg-[#111827]/70 p-1">
                      {renderOperandControls(
                        node.right,
                        (p) => updateOperandAt(idx, 'right', p),
                        false,
                        true,
                        `${rowKey}-right`
                      )}
                    </div>

                    {/* Row controls: Enable toggle, Duplicate, Delete */}
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        role="switch"
                        aria-checked={enabled}
                        title={enabled ? 'Disable condition' : 'Enable condition'}
                        onClick={() => updateConditionAt(idx, { enabled: !enabled })}
                        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
                          enabled ? 'bg-sky-500' : 'bg-slate-700'
                        }`}
                      >
                        <span
                          className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                            enabled ? 'translate-x-4' : 'translate-x-1'
                          }`}
                        />
                      </button>
                      <button
                        type="button"
                        onClick={() => duplicateNodeAt(idx)}
                        title="Duplicate condition"
                        className="flex h-7 w-7 items-center justify-center rounded text-slate-400 hover:bg-slate-800 hover:text-slate-100"
                      >
                        <Copy className="h-3.5 w-3.5" />
                      </button>
                      <button
                        type="button"
                        onClick={() => removeNodeAt(idx)}
                        title="Delete condition"
                        className="flex h-7 w-7 items-center justify-center rounded text-slate-400 hover:bg-red-950/60 hover:text-red-400"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {/* Advance bottom action bar matching Streak + Mathematical Formula support */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              onClick={() => addCondition()}
              className="inline-flex min-h-[34px] items-center gap-1 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 text-xs font-semibold text-sky-300 hover:border-sky-400"
            >
              <Plus className="h-3.5 w-3.5" />
              <span>and / or</span>
            </button>
            <button
              type="button"
              onClick={() => addFormulaCondition()}
              className="inline-flex min-h-[34px] items-center gap-1 rounded-lg border border-sky-600/80 bg-sky-500/15 px-3 text-xs font-semibold text-sky-200 hover:border-sky-400 hover:bg-sky-500/25"
            >
              <FunctionSquare className="h-3.5 w-3.5 text-sky-400" />
              <span>+ Math Formula</span>
            </button>
            <button
              type="button"
              onClick={addNestedGroup}
              className="inline-flex min-h-[34px] items-center gap-1 rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
            >
              <FolderPlus className="h-3.5 w-3.5" />
              <span>Group ( )</span>
            </button>
            <button
              type="button"
              onClick={handleCopyAll}
              className="inline-flex min-h-[34px] items-center gap-1 rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
            >
              <Copy className="h-3.5 w-3.5" />
              <span>Copy All</span>
            </button>
            <button
              type="button"
              onClick={handlePasteAll}
              className="inline-flex min-h-[34px] items-center gap-1 rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
            >
              <ClipboardPaste className="h-3.5 w-3.5" />
              <span>Paste All</span>
            </button>
            {pasteNotice && <span className="text-xs text-emerald-400">{pasteNotice}</span>}
          </div>

          {/* Mathematical Formula quick-insert reference bar */}
          <div className="rounded-lg border border-slate-800 bg-[#0b1120]/60 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400">
              <span className="font-semibold text-slate-300">
                Mathematical Formula Templates (click to insert)
              </span>
              <span className="font-mono text-[10px] text-slate-500">
                Supports High(-2), Max(6, High(0)), Period max(14, Close(-2)), EMA(high, 20, 0), SMA, RSI, ATR, VWAP, +, -, *, /
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {FORMULA_EXAMPLES.map((ex) => (
                <button
                  key={ex.label}
                  type="button"
                  onClick={() => addFormulaCondition(ex.expr)}
                  className="rounded border border-slate-700 bg-[#111827] px-2 py-1 font-mono text-[11px] text-sky-300 hover:border-sky-500 hover:text-sky-200"
                >
                  {ex.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── MODE 3: PYTHON (.py) ─────────────────────────────────────── */}
      {mode === 'python' && !hidePythonMode && (
        <div className="space-y-4 rounded-lg border border-slate-800 bg-[#0b1120]/70 p-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="min-w-[220px] flex-1">
              <label className="mb-1.5 block text-xs font-semibold text-slate-300">
                Python {kind === 'screen' ? 'Screen' : 'Strategy'} Plugin (.py)
              </label>
              <select
                value={selectedPythonId}
                onChange={(e) => onSelectPythonId(e.target.value)}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#111827] px-3 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                {pythonPlugins.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} {p.source_file ? `(${p.source_file})` : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => void handleLoadPythonSource()}
                disabled={pyBusy}
                className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
              >
                <FileCode2 className="h-3.5 w-3.5 text-sky-400" />
                <span>View / Edit .py</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setPyFilename(kind === 'screen' ? 'custom_screen_py.py' : 'custom_strategy_py.py');
                  setPyCode(kind === 'screen' ? PYTHON_SCREEN_TEMPLATE : PYTHON_STRATEGY_TEMPLATE);
                  setPyEditorOpen((o) => !o);
                }}
                className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 text-xs font-semibold text-sky-300 hover:border-sky-400"
              >
                <Plus className="h-3.5 w-3.5" />
                <span>New .py File</span>
              </button>
              {onRefreshPlugins && (
                <button
                  type="button"
                  onClick={onRefreshPlugins}
                  title="Hot-reload Python plugins from disk"
                  className="flex h-[38px] w-[38px] items-center justify-center rounded-lg border border-slate-700 bg-[#111827] text-slate-400 hover:border-sky-400 hover:text-slate-100"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>

          {selectedPlugin && (
            <p className="text-xs text-slate-400">
              {selectedPlugin.description}
              {selectedPlugin.source_file && (
                <span className="ml-2 font-mono text-[11px] text-slate-500">· {selectedPlugin.source_file}</span>
              )}
            </p>
          )}

          {/* Multi-Timeframe checkboxes for Support & Resistance screen */}
          {isMtfPlugin && onMtfTimeframesChange && (
            <div className="space-y-1.5">
              <div className="text-xs font-semibold text-slate-300">Active Timeframes (Multi-TF Support &amp; Resistance)</div>
              <div className="flex flex-wrap gap-2">
                {MTF_TFS.map((tf) => {
                  const active = mtfTimeframes.includes(tf.id);
                  return (
                    <button
                      key={tf.id}
                      type="button"
                      onClick={() => {
                        const next = active
                          ? mtfTimeframes.filter((x) => x !== tf.id)
                          : [...mtfTimeframes, tf.id];
                        onMtfTimeframesChange(next);
                      }}
                      className={`min-h-[32px] rounded-md border px-3 font-mono text-xs font-semibold transition-colors ${
                        active
                          ? 'border-sky-500 bg-sky-500/20 text-sky-300'
                          : 'border-slate-700 bg-[#111827] text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      {tf.label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Dynamic ParamSpec controls */}
          {selectedPlugin && Array.isArray(selectedPlugin.params) && selectedPlugin.params.length > 0 && (
            <div className="grid grid-cols-1 gap-3 border-t border-slate-800 pt-3 sm:grid-cols-2">
              {(selectedPlugin.params as ParamSpecMeta[]).map((ps) => {
                const rawVal = pythonParams[ps.name] ?? ps.default;
                if (ps.kind === 'bool') {
                  return (
                    <label
                      key={ps.name}
                      className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-800 bg-[#111827] px-3 py-2 text-xs text-slate-200"
                    >
                      <input
                        type="checkbox"
                        checked={Boolean(rawVal)}
                        onChange={(e) => onPythonParamsChange({ ...pythonParams, [ps.name]: e.target.checked })}
                        className="h-4 w-4 accent-sky-400"
                      />
                      <span>{ps.label}</span>
                    </label>
                  );
                }
                if (ps.kind === 'choice' && ps.options) {
                  return (
                    <div key={ps.name}>
                      <label className="mb-1 block text-[11px] font-medium text-slate-400">{ps.label}</label>
                      <select
                        value={String(rawVal ?? '')}
                        onChange={(e) => onPythonParamsChange({ ...pythonParams, [ps.name]: e.target.value })}
                        className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#111827] px-2.5 text-xs text-slate-100"
                      >
                        {ps.options.map((opt) => (
                          <option key={opt} value={opt}>{opt}</option>
                        ))}
                      </select>
                    </div>
                  );
                }
                return (
                  <div key={ps.name}>
                    <label className="mb-1 block text-[11px] font-medium text-slate-400">{ps.label}</label>
                    <input
                      type="number"
                      min={ps.min}
                      max={ps.max}
                      step={ps.step ?? (ps.kind === 'float' ? 0.1 : 1)}
                      value={String(rawVal ?? '')}
                      onChange={(e) =>
                        onPythonParamsChange({
                          ...pythonParams,
                          [ps.name]: ps.kind === 'int' ? Number.parseInt(e.target.value, 10) || 0 : Number(e.target.value) || 0,
                        })
                      }
                      className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#111827] px-2.5 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
                    />
                  </div>
                );
              })}
            </div>
          )}

          {/* Embedded Python File Editor */}
          {pyEditorOpen && (
            <div className="space-y-2.5 border-t border-slate-800 pt-3">
              <div className="rounded-lg border border-amber-800/70 bg-amber-950/30 px-3 py-2 text-[11px] text-amber-200">
                <span className="font-semibold">Admin-Only Trusted Code:</span> Python plugins execute inside the local
                AlgoDesk server process. Static AST checks are defense-in-depth only — upload only code you wrote or trust.
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold text-slate-300">Filename:</span>
                  <input
                    type="text"
                    value={pyFilename}
                    onChange={(e) => setPyFilename(e.target.value)}
                    className="min-h-[32px] rounded-md border border-slate-700 bg-[#111827] px-2.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void handleSavePythonFile()}
                    disabled={pyBusy}
                    className="inline-flex min-h-[32px] items-center gap-1.5 rounded-md bg-sky-500 px-3 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" />
                    <span>{pyBusy ? 'Validating...' : 'Save & Hot-Reload .py'}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setPyEditorOpen(false)}
                    className="min-h-[32px] rounded-md border border-slate-700 px-2.5 text-xs text-slate-400 hover:text-slate-100"
                  >
                    Close
                  </button>
                </div>
              </div>
              <textarea
                value={pyCode}
                onChange={(e) => setPyCode(e.target.value)}
                rows={12}
                spellCheck={false}
                className="w-full rounded-lg border border-slate-700 bg-[#070b14] p-3 font-mono text-xs leading-relaxed text-slate-200 focus:border-sky-400 focus:outline-none"
              />
              {pyFeedback && (
                <div
                  className={`flex items-center gap-1.5 text-xs ${
                    pyFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                  }`}
                >
                  {pyFeedback.type === 'ok' && <Check className="h-3.5 w-3.5" />}
                  <span>{pyFeedback.text}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
