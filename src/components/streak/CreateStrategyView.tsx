import { useEffect, useMemo, useState } from 'react';
import {
  ChevronDown,
  ChevronUp,
  Compass,
  FolderOpen,
  Play,
  Plus,
  Save,
  Trash2,
  X,
} from 'lucide-react';
import { BacktestResultsView } from './BacktestResultsView';
import { CandlestickChartPanel } from './CandlestickChartPanel';
import type { QuickOrderTarget } from './QuickOrderModal';
import { StreakConditionBuilder, type BuilderEngineMode } from './StreakConditionBuilder';
import type {
  BacktestFullResult,
  StrategyDraftPreset,
  StreakMetaPayload,
  VisualConditionGroup,
  VisualRuleDef,
} from './types';

interface CreateStrategyViewProps {
  meta: StreakMetaPayload;
  draft: StrategyDraftPreset | null;
  onRefreshMeta: () => void;
  onNavigateDiscover: () => void;
  onDeployed: () => void;
  onQuickOrder: (target: QuickOrderTarget) => void;
}

const DEFAULT_ENTRY: VisualConditionGroup = {
  logic: 'AND',
  conditions: [
    {
      left: { type: 'ema', period: 20, offset: 0 },
      operator: 'crosses_above',
      right: { type: 'ema', period: 50, offset: 0 },
      enabled: true,
      timeframe: '1day',
    },
  ],
};

const DEFAULT_EXIT: VisualConditionGroup = {
  logic: 'OR',
  conditions: [
    {
      left: { type: 'close', offset: 0 },
      operator: '<',
      right: { type: 'ema', period: 20, offset: 0 },
      enabled: true,
      timeframe: '1day',
    },
  ],
};

const TIMEFRAME_OPTIONS = [
  { id: '1minute', label: '1 Minute' },
  { id: '5minute', label: '5 Minutes' },
  { id: '15minute', label: '15 Minutes' },
  { id: '60minute', label: '1 Hour' },
  { id: '1day', label: '1 Day' },
];

export function CreateStrategyView({
  meta,
  draft,
  onRefreshMeta,
  onNavigateDiscover,
  onDeployed,
  onQuickOrder,
}: CreateStrategyViewProps) {
  const [ruleId, setRuleId] = useState<string | undefined>(undefined);
  const [name, setName] = useState('Untitled Strategy');
  const [description, setDescription] = useState('');
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY');
  const [mode, setMode] = useState<BuilderEngineMode>('basic');

  // Instruments
  const [symbols, setSymbols] = useState<string[]>(['RELIANCE', 'TCS', 'HDFCBANK', 'INFY']);
  const [activeChartSymbol, setActiveChartSymbol] = useState('RELIANCE');
  const [instrumentInput, setInstrumentInput] = useState('');
  const [basketOrUniverse, setBasketOrUniverse] = useState('');

  // Chart settings
  const [chartType, setChartType] = useState<'candlestick' | 'heikin_ashi'>('candlestick');
  const [timeframe, setTimeframe] = useState('1day');

  // Visual Entry + Optional Exit conditions
  const [entryGroup, setEntryGroup] = useState<VisualConditionGroup>(DEFAULT_ENTRY);
  const [useExitConditions, setUseExitConditions] = useState(false);
  const [exitGroup, setExitGroup] = useState<VisualConditionGroup>(DEFAULT_EXIT);

  // Python (.py) strategy state
  const [selectedPythonId, setSelectedPythonId] = useState(meta.strategy_specs?.[0]?.id || 'ema_crossover');
  const [pythonParams, setPythonParams] = useState<Record<string, unknown>>({});

  // Exit risk parameters (Streak Exit card)
  const [stopLoss, setStopLoss] = useState(2.0);
  const [takeProfit, setTakeProfit] = useState(4.0);
  const [trailingSl, setTrailingSl] = useState(0);
  const [exitUnit, setExitUnit] = useState<'pct' | 'points' | 'pnl'>('pct');

  // Backtest & Real Charges settings
  const [showAdvancedBt, setShowAdvancedBt] = useState(false);
  const [btMode, setBtMode] = useState<'portfolio' | 'individual'>('portfolio');
  const [allocationMode, setAllocationMode] = useState('equal_weight');
  const [initialCapital, setInitialCapital] = useState(500000);
  const [chargesProfile, setChargesProfile] = useState('zerodha');
  const [segment, setSegment] = useState('equity_delivery');
  const [fromDate, setFromDate] = useState(() =>
    new Date(Date.now() - 180 * 86400_000).toISOString().slice(0, 10)
  );
  const [toDate, setToDate] = useState(() => new Date().toISOString().slice(0, 10));

  // Execution & Result view
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [exportingPy, setExportingPy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [backtestResult, setBacktestResult] = useState<BacktestFullResult | null>(null);
  const [myStrategiesOpen, setMyStrategiesOpen] = useState(false);

  // Apply draft when navigated from Discover Strategies / Copy
  useEffect(() => {
    if (!draft) return;
    setMode(draft.mode || 'basic');
    setName(draft.name || draft.rule?.name || 'Untitled Strategy');
    setDescription(draft.description || draft.rule?.description || '');
    setTimeframe(draft.timeframe || draft.rule?.timeframe || '1day');
    setChartType(draft.chartType || draft.rule?.chart_type || 'candlestick');
    if (draft.segment || draft.rule?.segment) {
      setSegment(draft.segment || draft.rule?.segment || 'equity_delivery');
    }
    if (draft.symbols && draft.symbols.length > 0) {
      setSymbols(draft.symbols);
      setActiveChartSymbol(draft.symbols[0]);
    } else if (draft.rule?.default_symbols && draft.rule.default_symbols.length > 0) {
      setSymbols(draft.rule.default_symbols);
      setActiveChartSymbol(draft.rule.default_symbols[0]);
    }
    if (draft.rule) {
      setRuleId(draft.isCopy ? undefined : draft.rule.id);
      setSide(draft.rule.side || 'BUY');
      setStopLoss(draft.rule.stop_loss_pct ?? 2.0);
      setTakeProfit(draft.rule.take_profit_pct ?? 4.0);
      setTrailingSl(draft.rule.trailing_sl_pct ?? 0);
      setExitUnit(draft.rule.exit_unit || 'pct');
      setEntryGroup(
        draft.rule.entry?.conditions?.length
          ? JSON.parse(JSON.stringify(draft.rule.entry))
          : DEFAULT_ENTRY
      );
      if (draft.rule.exit?.conditions?.length) {
        setUseExitConditions(true);
        setExitGroup(JSON.parse(JSON.stringify(draft.rule.exit)));
      } else {
        setUseExitConditions(false);
      }
    } else {
      setRuleId(undefined);
    }
    if (draft.pythonStrategyId) {
      setSelectedPythonId(draft.pythonStrategyId);
    }
    if (draft.openFullResult) {
      setBacktestResult(draft.openFullResult);
    } else {
      setBacktestResult(null);
    }
  }, [draft]);

  // Populate default params when Python strategy changes
  useEffect(() => {
    const spec = (meta.strategy_specs || []).find((s) => s.id === selectedPythonId);
    if (!spec?.params) return;
    const defaults: Record<string, unknown> = {};
    for (const p of spec.params) defaults[p.name] = p.default;
    setPythonParams(defaults);
  }, [selectedPythonId, meta.strategy_specs]);

  const addSymbolFromInput = () => {
    const added = instrumentInput
      .split(/[\s,]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!added.length) return;
    setSymbols((prev) => {
      const merged = Array.from(new Set([...prev, ...added]));
      if (!activeChartSymbol && merged[0]) setActiveChartSymbol(merged[0]);
      return merged;
    });
    setInstrumentInput('');
  };

  const removeSymbol = (sym: string) => {
    setSymbols((prev) => {
      const next = prev.filter((s) => s !== sym);
      if (activeChartSymbol === sym) setActiveChartSymbol(next[0] || '');
      return next;
    });
  };

  const loadFromBasket = (val: string) => {
    setBasketOrUniverse(val);
    if (val.startsWith('basket:')) {
      const b = (meta.baskets || []).find((x) => x.id === val.slice(7));
      if (b && b.symbols.length > 0) {
        setSymbols(b.symbols);
        setActiveChartSymbol(b.symbols[0]);
      }
    }
  };

  const buildRuleDef = (): VisualRuleDef => ({
    id: ruleId,
    kind: 'strategy',
    mode: 'visual',
    builder_mode: mode === 'advance' ? 'advance' : 'basic',
    name: name.trim() || 'Untitled Strategy',
    description: description.trim() || 'Custom visual strategy',
    default_symbols: symbols,
    timeframe,
    chart_type: chartType,
    side,
    segment,
    stop_loss_pct: stopLoss,
    take_profit_pct: takeProfit,
    trailing_sl_pct: trailingSl,
    exit_unit: exitUnit,
    entry: entryGroup,
    exit: useExitConditions ? exitGroup : undefined,
  });

  const handleSaveStrategy = async (): Promise<VisualRuleDef | null> => {
    if (mode === 'python') {
      setNotice('Python (.py) strategies are saved inside the Python code editor above.');
      return null;
    }
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const res = await fetch('/api/streak/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildRuleDef()),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not save strategy.');
      const saved = body.rule as VisualRuleDef;
      setRuleId(saved.id);
      setNotice(`Saved "${saved.name}" to My Strategies.`);
      onRefreshMeta();
      return saved;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.');
      return null;
    } finally {
      setSaving(false);
    }
  };

  const handleExportToPython = async () => {
    setExportingPy(true);
    setError('');
    try {
      const saved = await handleSaveStrategy();
      if (!saved?.id) return;
      const res = await fetch('/api/streak/rules/export-python', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rule_id: saved.id }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Export failed.');
      setNotice(`Exported Python strategy: ${body.file || body.module_id}`);
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed.');
    } finally {
      setExportingPy(false);
    }
  };

  const handleSaveAndBacktest = async () => {
    setRunning(true);
    setError('');
    setNotice('');
    try {
      let savedId = ruleId;
      if (mode !== 'python') {
        const saved = await handleSaveStrategy();
        if (saved?.id) savedId = saved.id;
      }
      const useUniverse = basketOrUniverse.startsWith('universe:') ? basketOrUniverse.slice(9) : undefined;
      if (!useUniverse && symbols.length === 0) {
        throw new Error('Add at least one instrument before running a backtest.');
      }
      const payload: Record<string, unknown> = {
        mode: btMode,
        allocation_mode: allocationMode,
        strategy_mode: mode === 'python' ? 'python' : 'visual',
        strategy: mode === 'python' ? selectedPythonId : savedId ? `visual:${savedId}` : 'visual:inline',
        rule_def: mode !== 'python' ? buildRuleDef() : undefined,
        params: mode === 'python' ? pythonParams : undefined,
        symbols: useUniverse ? undefined : symbols,
        universe: useUniverse,
        exchange: 'NSE',
        timeframe,
        from_date: fromDate,
        to_date: toDate,
        initial_capital: initialCapital,
        stop_loss_pct: stopLoss,
        take_profit_pct: takeProfit,
        trailing_sl_pct: trailingSl,
        charges_profile: chargesProfile,
        segment,
      };
      const res = await fetch('/api/streak/backtest/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Backtest failed.');
      setBacktestResult(body as BacktestFullResult);
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backtest execution failed.');
    } finally {
      setRunning(false);
    }
  };

  const mySavedStrategies = useMemo(
    () => (meta.visual_rules || []).filter((r) => r.kind === 'strategy'),
    [meta.visual_rules]
  );

  // Switch cleanly to full Backtest Results page when a backtest completes
  if (backtestResult) {
    return (
      <BacktestResultsView
        result={backtestResult}
        onBack={() => setBacktestResult(null)}
        onViewStrategy={() => setBacktestResult(null)}
        onCopyStrategy={() => {
          setRuleId(undefined);
          setName(`${name} (Copy)`);
          setBacktestResult(null);
        }}
        onDeployed={onDeployed}
        onQuickOrder={onQuickOrder}
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* Top Bar matching Streak Create Equity Strategy-1.png */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Strategy name"
            className="min-h-[40px] w-full max-w-md rounded-lg border border-transparent bg-transparent px-2 text-xl font-bold tracking-tight text-slate-100 hover:border-slate-700 focus:border-sky-400 focus:bg-[#111827] focus:outline-none"
          />
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onNavigateDiscover}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3.5 text-xs font-semibold text-slate-200 hover:border-sky-400"
          >
            <Compass className="h-3.5 w-3.5 text-sky-400" />
            <span>Discover</span>
          </button>
          <button
            type="button"
            onClick={() => setMyStrategiesOpen(true)}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
          >
            <FolderOpen className="h-3.5 w-3.5" />
            <span>My Strategies</span>
          </button>
        </div>
      </div>

      {/* Main Split-Pane: Left Entry/Exit Builder (6 cols) + Right Live OHLCV Candlestick Chart (6 cols) */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        {/* Left Builder Column */}
        <div className="flex flex-col gap-4 xl:col-span-6">
          {/* ENTRY CARD */}
          <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
              <div className="flex items-center gap-3">
                <h3 className="text-sm font-semibold text-slate-100">Entry</h3>
                <div className="inline-flex rounded-md border border-slate-800 bg-[#0b1120] p-0.5">
                  <button
                    type="button"
                    onClick={() => setSide('BUY')}
                    className={`min-h-[26px] rounded px-3 text-xs font-semibold ${
                      side === 'BUY' ? 'bg-sky-500 text-slate-950' : 'text-slate-400 hover:text-slate-100'
                    }`}
                  >
                    Buy
                  </button>
                  <button
                    type="button"
                    onClick={() => setSide('SELL')}
                    className={`min-h-[26px] rounded px-3 text-xs font-semibold ${
                      side === 'SELL' ? 'bg-red-500 text-white' : 'text-slate-400 hover:text-slate-100'
                    }`}
                  >
                    Sell
                  </button>
                </div>
              </div>
              <span className="font-mono text-[11px] text-slate-500">
                {symbols.length} instruments
              </span>
            </div>

            <div className="space-y-5 p-5">
              {/* Instruments & Chart Settings */}
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-slate-300">
                    Instruments
                  </label>
                  <div className="flex gap-1.5">
                    <input
                      type="text"
                      value={instrumentInput}
                      onChange={(e) => setInstrumentInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          addSymbolFromInput();
                        }
                      }}
                      placeholder="Add symbol (e.g. SBIN)"
                      className="min-h-[36px] flex-1 rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                    <button
                      type="button"
                      onClick={addSymbolFromInput}
                      className="inline-flex min-h-[36px] items-center gap-1 rounded-lg border border-sky-700/70 bg-sky-950/30 px-2.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      <span>Add</span>
                    </button>
                  </div>
                  <select
                    value={basketOrUniverse}
                    onChange={(e) => loadFromBasket(e.target.value)}
                    aria-label="Load from Basket or Universe"
                    className="mt-2 min-h-[34px] w-full rounded-lg border border-slate-800 bg-[#0b1120] px-2.5 text-xs text-slate-300 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="">Or load Basket / NSE Universe...</option>
                    {(meta.baskets || []).map((b) => (
                      <option key={b.id} value={`basket:${b.id}`}>
                        Basket: {b.name} ({b.symbols.length})
                      </option>
                    ))}
                    {(meta.universes || []).map((u) => (
                      <option key={u.id} value={`universe:${u.id}`}>
                        Universe: {u.label} ({u.count})
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-slate-300">
                    Chart Settings
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <select
                      value={chartType}
                      onChange={(e) => setChartType(e.target.value as 'candlestick' | 'heikin_ashi')}
                      className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                    >
                      <option value="candlestick">Candlestick</option>
                      <option value="heikin_ashi">Heikin-Ashi</option>
                    </select>
                    <select
                      value={timeframe}
                      onChange={(e) => setTimeframe(e.target.value)}
                      className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                    >
                      {TIMEFRAME_OPTIONS.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </div>

              {/* Selected Symbols removable chips */}
              {symbols.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {symbols.map((sym) => (
                    <span
                      key={sym}
                      className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-[11px] font-semibold ${
                        activeChartSymbol === sym
                          ? 'border-sky-500/80 bg-sky-950/50 text-sky-300'
                          : 'border-slate-700 bg-[#0b1120] text-slate-200'
                      }`}
                    >
                      <button type="button" onClick={() => setActiveChartSymbol(sym)}>
                        {sym}
                      </button>
                      <button
                        type="button"
                        onClick={() => removeSymbol(sym)}
                        aria-label={`Remove ${sym}`}
                        className="text-slate-500 hover:text-red-400"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  ))}
                </div>
              )}

              {/* Condition Builder: Basic | Advance | Python (.py) */}
              <div className="border-t border-slate-800 pt-4">
                <StreakConditionBuilder
                  kind="strategy"
                  mode={mode}
                  onModeChange={setMode}
                  group={entryGroup}
                  onGroupChange={setEntryGroup}
                  defaultTimeframe={timeframe}
                  operands={meta.visual_operands}
                  operators={meta.visual_operators}
                  pythonPlugins={meta.strategy_specs || []}
                  selectedPythonId={selectedPythonId}
                  onSelectPythonId={setSelectedPythonId}
                  pythonParams={pythonParams}
                  onPythonParamsChange={setPythonParams}
                  onRefreshPlugins={onRefreshMeta}
                  onExportVisualToPython={handleExportToPython}
                  exportingPython={exportingPy}
                  titleLabel="Entry Conditions"
                />
              </div>
            </div>
          </div>

          {/* EXIT CARD matching Streak Create Equity Strategy-2.png */}
          <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
              <h3 className="text-sm font-semibold text-slate-100">Exit</h3>
              <div className="inline-flex rounded-md border border-slate-800 bg-[#0b1120] p-0.5">
                {[
                  { id: 'pct' as const, label: '%' },
                  { id: 'points' as const, label: 'Points' },
                  { id: 'pnl' as const, label: 'PNL' },
                ].map((u) => (
                  <button
                    key={u.id}
                    type="button"
                    onClick={() => setExitUnit(u.id)}
                    className={`min-h-[24px] rounded px-2.5 text-[11px] font-semibold ${
                      exitUnit === u.id ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {u.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-4 p-5">
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-300">
                    Stop Loss ({exitUnit === 'pct' ? '%' : exitUnit})
                  </label>
                  <input
                    type="number"
                    step="0.25"
                    min={0}
                    value={stopLoss}
                    onChange={(e) => setStopLoss(Math.max(0, Number(e.target.value)))}
                    className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-sm text-red-300 tabular-nums focus:border-sky-400 focus:outline-none"
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-300">
                    Target Profit ({exitUnit === 'pct' ? '%' : exitUnit})
                  </label>
                  <input
                    type="number"
                    step="0.25"
                    min={0}
                    value={takeProfit}
                    onChange={(e) => setTakeProfit(Math.max(0, Number(e.target.value)))}
                    className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-sm text-emerald-300 tabular-nums focus:border-sky-400 focus:outline-none"
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-300">
                    Trailing SL ({exitUnit === 'pct' ? '%' : exitUnit})
                  </label>
                  <input
                    type="number"
                    step="0.25"
                    min={0}
                    value={trailingSl}
                    onChange={(e) => setTrailingSl(Math.max(0, Number(e.target.value)))}
                    className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-sm text-amber-300 tabular-nums focus:border-sky-400 focus:outline-none"
                  />
                </div>
              </div>

              {mode !== 'python' && (
                <div className="space-y-3 border-t border-slate-800 pt-3">
                  <label className="inline-flex cursor-pointer items-center gap-2 text-xs font-medium text-slate-300">
                    <input
                      type="checkbox"
                      checked={useExitConditions}
                      onChange={(e) => setUseExitConditions(e.target.checked)}
                      className="rounded border-slate-700 bg-[#0b1120] text-sky-500"
                    />
                    <span>Enable indicator-based Exit Conditions (optional)</span>
                  </label>

                  {useExitConditions && (
                    <StreakConditionBuilder
                      kind="strategy"
                      mode={mode}
                      onModeChange={(next) => {
                        if (next !== 'python') setMode(next);
                      }}
                      group={exitGroup}
                      onGroupChange={setExitGroup}
                      defaultTimeframe={timeframe}
                      operands={meta.visual_operands}
                      operators={meta.visual_operators}
                      pythonPlugins={[]}
                      selectedPythonId=""
                      onSelectPythonId={() => {}}
                      pythonParams={{}}
                      onPythonParamsChange={() => {}}
                      hidePythonMode
                      titleLabel="Exit Conditions"
                    />
                  )}
                </div>
              )}
            </div>
          </div>

          {/* BACKTEST & REAL TRADING CHARGES CONFIG */}
          <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
            <button
              type="button"
              onClick={() => setShowAdvancedBt((v) => !v)}
              className="flex w-full items-center justify-between px-5 py-3 text-left text-xs font-semibold text-slate-200 hover:bg-slate-800/40"
            >
              <span>
                Backtest Parameters &amp; Real Charges ({btMode === 'portfolio' ? 'Portfolio' : 'Individual'} · ₹
                {initialCapital.toLocaleString('en-IN')} · {chargesProfile.toUpperCase()})
              </span>
              {showAdvancedBt ? <ChevronUp className="h-4 w-4 text-slate-400" /> : <ChevronDown className="h-4 w-4 text-slate-400" />}
            </button>

            {showAdvancedBt && (
              <div className="grid grid-cols-1 gap-3 border-t border-slate-800 p-5 text-xs sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <label className="mb-1 block text-slate-400">Backtest Mode</label>
                  <select
                    value={btMode}
                    onChange={(e) => setBtMode(e.target.value as 'portfolio' | 'individual')}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                  >
                    <option value="portfolio">Portfolio (Shared Capital)</option>
                    <option value="individual">Individual (Per Symbol)</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-slate-400">Allocation Mode</label>
                  <select
                    value={allocationMode}
                    onChange={(e) => setAllocationMode(e.target.value)}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                  >
                    {(meta.allocation_modes || [
                      { id: 'equal_weight', label: 'Equal Weight' },
                      { id: 'fixed_capital', label: 'Fixed Capital' },
                      { id: 'risk_based', label: 'Risk-Based Sizing' },
                    ]).map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-slate-400">Initial Capital (₹)</label>
                  <input
                    type="number"
                    min={10000}
                    step={25000}
                    value={initialCapital}
                    onChange={(e) => setInitialCapital(Math.max(10000, Number(e.target.value)))}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-slate-100"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-slate-400">Broker Charges Preset</label>
                  <select
                    value={chargesProfile}
                    onChange={(e) => setChargesProfile(e.target.value)}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                  >
                    <option value="zerodha">Zerodha (Real Indian Charges)</option>
                    <option value="upstox">Upstox (Real Indian Charges)</option>
                    <option value="zero">Zero Charges (Gross Only)</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-slate-400">Market Segment</label>
                  <select
                    value={segment}
                    onChange={(e) => setSegment(e.target.value)}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                  >
                    <option value="equity_delivery">Equity Delivery (CNC)</option>
                    <option value="equity_intraday">Equity Intraday (MIS)</option>
                    <option value="fno_futures">F&amp;O Futures</option>
                    <option value="fno_options">F&amp;O Options</option>
                  </select>
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                  <div>
                    <label className="mb-1 block text-slate-400">From</label>
                    <input
                      type="date"
                      value={fromDate}
                      onChange={(e) => setFromDate(e.target.value)}
                      className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2 font-mono text-[11px] text-slate-100"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-slate-400">To</label>
                    <input
                      type="date"
                      value={toDate}
                      onChange={(e) => setToDate(e.target.value)}
                      className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2 font-mono text-[11px] text-slate-100"
                    />
                  </div>
                </div>
              </div>
            )}
          </div>

          {error && (
            <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-4 py-2.5 text-xs text-red-200">
              {error}
            </div>
          )}
          {notice && (
            <div className="rounded-lg border border-emerald-800/70 bg-emerald-950/40 px-4 py-2.5 text-xs text-emerald-200">
              {notice}
            </div>
          )}

          {/* Sticky Bottom Action Bar */}
          <div className="flex flex-wrap items-center justify-end gap-2.5 rounded-xl border border-slate-800 bg-[#111827] px-5 py-3.5">
            {mode !== 'python' && (
              <button
                type="button"
                onClick={() => void handleSaveStrategy()}
                disabled={saving}
                className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-4 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                <span>{saving ? 'Saving...' : 'Save'}</span>
              </button>
            )}
            <button
              type="button"
              onClick={() => void handleSaveAndBacktest()}
              disabled={running}
              className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg bg-sky-500 px-5 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              <span>{running ? 'Running Backtest...' : 'Save & Backtest'}</span>
            </button>
          </div>
        </div>

        {/* Right Candlestick Chart Column matching Streak Create Equity Strategy */}
        <div className="xl:col-span-6">
          <div className="sticky top-20">
            <CandlestickChartPanel
              symbols={symbols}
              activeSymbol={activeChartSymbol || symbols[0] || 'RELIANCE'}
              onSelectSymbol={setActiveChartSymbol}
              timeframe={timeframe}
              onTimeframeChange={setTimeframe}
              onQuickOrder={onQuickOrder}
              height={480}
            />
          </div>
        </div>
      </div>

      {/* My Strategies Modal */}
      {myStrategiesOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
              <h3 className="text-sm font-semibold text-slate-100">
                My Strategies ({mySavedStrategies.length})
              </h3>
              <button
                type="button"
                onClick={() => setMyStrategiesOpen(false)}
                className="text-slate-400 hover:text-slate-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 divide-y divide-slate-800 overflow-y-auto">
              {mySavedStrategies.map((r) => (
                <div key={r.id || r.name} className="flex items-center justify-between gap-3 px-5 py-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-slate-100">{r.name}</div>
                    <div className="mt-0.5 font-mono text-xs text-slate-400">
                      {r.timeframe || '1day'} · SL {r.stop_loss_pct ?? 2}% / TP {r.take_profit_pct ?? 4}%
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setRuleId(r.id);
                        setName(r.name);
                        setDescription(r.description || '');
                        setMode(r.builder_mode === 'advance' ? 'advance' : 'basic');
                        setTimeframe(r.timeframe || '1day');
                        setChartType(r.chart_type || 'candlestick');
                        setSide(r.side || 'BUY');
                        setStopLoss(r.stop_loss_pct ?? 2);
                        setTakeProfit(r.take_profit_pct ?? 4);
                        setTrailingSl(r.trailing_sl_pct ?? 0);
                        setEntryGroup(r.entry ? JSON.parse(JSON.stringify(r.entry)) : DEFAULT_ENTRY);
                        if (r.default_symbols?.length) {
                          setSymbols(r.default_symbols);
                          setActiveChartSymbol(r.default_symbols[0]);
                        }
                        setMyStrategiesOpen(false);
                      }}
                      className="rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 py-1.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
                    >
                      Load
                    </button>
                    {r.id && !r.seeded && (
                      <button
                        type="button"
                        onClick={async () => {
                          await fetch(`/api/streak/rules/${encodeURIComponent(r.id!)}`, { method: 'DELETE' });
                          onRefreshMeta();
                        }}
                        className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:border-red-700 hover:text-red-400"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
