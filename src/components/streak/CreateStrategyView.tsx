import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
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

  // Backtest & Real Charges settings (also edited in Step 2: Execute Backtest modal)
  const [showAdvancedBt, setShowAdvancedBt] = useState(false);
  const [btMode, setBtMode] = useState<'portfolio' | 'individual'>('portfolio');
  const [allocationMode, setAllocationMode] = useState('shared_pool');
  const [initialCapital, setInitialCapital] = useState(100000);
  const [chargesProfile, setChargesProfile] = useState('zerodha');
  const [segment, setSegment] = useState('equity_delivery');
  const [holdingType, setHoldingType] = useState<'MIS' | 'CNC'>('CNC');
  const [strategyCycle, setStrategyCycle] = useState<string>('');
  const [startHour, setStartHour] = useState('9');
  const [startMinute, setStartMinute] = useState('15');
  const [startAmPm, setStartAmPm] = useState<'AM' | 'PM'>('AM');
  const [endHour, setEndHour] = useState('3');
  const [endMinute, setEndMinute] = useState('30');
  const [endAmPm, setEndAmPm] = useState<'AM' | 'PM'>('PM');
  const [fromDate, setFromDate] = useState(() =>
    new Date(Date.now() - 180 * 86400_000).toISOString().slice(0, 10)
  );
  const [toDate, setToDate] = useState(() => new Date().toISOString().slice(0, 10));

  // Execution, Step 2 Modal, Delete confirmation & Result view
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [exportingPy, setExportingPy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [backtestResult, setBacktestResult] = useState<BacktestFullResult | null>(null);
  const [myStrategiesOpen, setMyStrategiesOpen] = useState(false);
  const [executeModalOpen, setExecuteModalOpen] = useState(false);
  const [savedRuleIdForRun, setSavedRuleIdForRun] = useState<string | undefined>(undefined);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Apply draft when navigated from Discover Strategies / Copy
  useEffect(() => {
    if (!draft) return;
    setMode(draft.mode || 'basic');
    setName(draft.name || draft.rule?.name || 'Untitled Strategy');
    setDescription(draft.description || draft.rule?.description || '');
    setTimeframe(draft.timeframe || draft.rule?.timeframe || '1day');
    setChartType(draft.chartType || draft.rule?.chart_type || 'candlestick');
    if (draft.segment || draft.rule?.segment) {
      const seg = draft.segment || draft.rule?.segment || 'equity_delivery';
      setSegment(seg);
      setHoldingType(seg === 'equity_intraday' ? 'MIS' : 'CNC');
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
      const res = await apiFetch('/api/streak/rules', {
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
      const res = await apiFetch('/api/streak/rules/export-python', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rule_def: saved, save_to_disk: true }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Export failed.');
      setNotice(`Exported Python strategy: ${body.saved_path || body.filename || body.file || body.module_id || 'saved'}`);
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed.');
    } finally {
      setExportingPy(false);
    }
  };

  const to24HourTime = (hourStr: string, minStr: string, ampm: 'AM' | 'PM') => {
    let h = parseInt(hourStr, 10) || 9;
    if (ampm === 'PM' && h < 12) h += 12;
    if (ampm === 'AM' && h === 12) h = 0;
    const m = parseInt(minStr, 10) || 0;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
  };

  const applyQuickPeriod = (days: number) => {
    const end = new Date();
    const start = new Date(end.getTime() - days * 86400_000);
    setFromDate(start.toISOString().slice(0, 10));
    setToDate(end.toISOString().slice(0, 10));
  };

  const handleDeleteStrategyById = async (targetId: string) => {
    setDeletingId(targetId);
    setError('');
    setNotice('');
    try {
      const res = await apiFetch(`/api/streak/rules/${encodeURIComponent(targetId)}`, { method: 'DELETE' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not delete strategy.');
      setConfirmDeleteId(null);
      if (ruleId === targetId) {
        setRuleId(undefined);
        setName('Untitled Strategy');
        setDescription('');
        setEntryGroup(DEFAULT_ENTRY);
        setUseExitConditions(false);
        setNotice('Strategy deleted.');
      } else {
        setNotice('Strategy deleted from My Strategies.');
      }
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed.');
    } finally {
      setDeletingId(null);
    }
  };

  // Step 1 -> Step 2: Save strategy (if visual) then open Streak's Step 2: Execute Backtest modal
  const handleOpenExecuteModal = async () => {
    setError('');
    setNotice('');
    const useUniverse = basketOrUniverse.startsWith('universe:') ? basketOrUniverse.slice(9) : undefined;
    if (!useUniverse && symbols.length === 0) {
      setError('Add at least one instrument before running a backtest.');
      return;
    }
    let nextSavedId = ruleId;
    if (mode !== 'python') {
      const saved = await handleSaveStrategy();
      if (saved?.id) nextSavedId = saved.id;
    }
    setSavedRuleIdForRun(nextSavedId);
    // Sync holdingType with segment
    setHoldingType(segment === 'equity_intraday' ? 'MIS' : 'CNC');
    setExecuteModalOpen(true);
  };

  const handleExecuteBacktest = async () => {
    setRunning(true);
    setError('');
    setNotice('');
    try {
      const useUniverse = basketOrUniverse.startsWith('universe:') ? basketOrUniverse.slice(9) : undefined;
      if (!useUniverse && symbols.length === 0) {
        throw new Error('Add at least one instrument before running a backtest.');
      }
      const effRuleId = savedRuleIdForRun || ruleId;
      const effSegment = holdingType === 'MIS' ? 'equity_intraday' : segment === 'equity_intraday' ? 'equity_delivery' : segment;
      const payload: Record<string, unknown> = {
        mode: btMode,
        allocation_mode: allocationMode,
        strategy_mode: mode === 'python' ? 'python' : 'visual',
        strategy: mode === 'python' ? selectedPythonId : effRuleId ? `visual:${effRuleId}` : 'visual:inline',
        rule_def: mode !== 'python' ? { ...buildRuleDef(), segment: effSegment } : undefined,
        params: mode === 'python' ? pythonParams : undefined,
        symbols: useUniverse ? undefined : symbols,
        universe: useUniverse,
        exchange: 'NSE',
        timeframe,
        from_date: fromDate,
        to_date: toDate,
        start_time: to24HourTime(startHour, startMinute, startAmPm),
        end_time: to24HourTime(endHour, endMinute, endAmPm),
        holding_type: holdingType,
        strategy_cycle: strategyCycle ? Number(strategyCycle) : undefined,
        side,
        initial_capital: initialCapital,
        stop_loss_pct: stopLoss,
        take_profit_pct: takeProfit,
        trailing_sl_pct: trailingSl,
        charges_profile: chargesProfile,
        segment: effSegment,
      };
      const res = await apiFetch('/api/streak/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Backtest failed.');
      setExecuteModalOpen(false);
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
        <div className="flex flex-wrap items-center gap-2">
          {ruleId && mode !== 'python' && (
            confirmDeleteId === ruleId ? (
              <div className="inline-flex items-center gap-1.5 rounded-lg border border-red-800/80 bg-red-950/50 px-2.5 py-1">
                <span className="text-[11px] font-medium text-red-200">Delete strategy?</span>
                <button
                  type="button"
                  disabled={deletingId === ruleId}
                  onClick={() => void handleDeleteStrategyById(ruleId)}
                  className="rounded bg-red-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-red-500 disabled:opacity-50"
                >
                  {deletingId === ruleId ? 'Deleting...' : 'Confirm'}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmDeleteId(null)}
                  className="rounded px-1.5 py-0.5 text-[11px] text-slate-300 hover:text-white"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDeleteId(ruleId)}
                className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-red-900/70 bg-red-950/30 px-3 text-xs font-semibold text-red-300 hover:border-red-500 hover:text-red-200"
              >
                <Trash2 className="h-3.5 w-3.5" />
                <span>Delete</span>
              </button>
            )
          )}
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
              <p className="text-[11px] text-slate-500">
                Leave any field at <span className="font-mono text-slate-400">0</span> to disable it and exit only on your indicator Exit Conditions.
              </p>

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
                      { id: 'shared_pool', label: 'Shared Capital Pool' },
                      { id: 'equal_split', label: 'Equal Split Per Symbol' },
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
              onClick={() => void handleOpenExecuteModal()}
              disabled={running || saving}
              className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg bg-sky-500 px-5 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              <span>{saving ? 'Saving...' : 'Save & Backtest'}</span>
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
                    {r.id && (
                      confirmDeleteId === r.id ? (
                        <div className="inline-flex items-center gap-1">
                          <button
                            type="button"
                            disabled={deletingId === r.id}
                            onClick={() => void handleDeleteStrategyById(r.id!)}
                            className="rounded-md bg-red-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-red-500 disabled:opacity-50"
                          >
                            {deletingId === r.id ? '...' : 'Delete'}
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirmDeleteId(null)}
                            className="rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:text-white"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setConfirmDeleteId(r.id!)}
                          aria-label={`Delete ${r.name}`}
                          className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:border-red-700 hover:text-red-400"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Step 2: Execute Backtest Modal matching Streak Backtest date selection.png */}
      {executeModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/85 p-4 backdrop-blur-sm">
          <div className="flex w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-[#0f172a] shadow-2xl">
            {/* Modal Header */}
            <div className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
              <div className="flex items-center gap-2.5">
                <span className="text-base font-medium text-slate-400">Step 2:</span>
                <span className="text-base font-bold text-slate-100">Execute</span>
                <span className="rounded-md border border-slate-700 bg-[#1e293b] px-2.5 py-0.5 text-xs font-semibold text-sky-300">
                  Backtest
                </span>
              </div>
              <button
                type="button"
                onClick={() => setExecuteModalOpen(false)}
                aria-label="Close backtest period dialog"
                className="rounded-md p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {/* Modal Body */}
            <div className="space-y-5 p-6">
              <div className="space-y-6 rounded-xl border border-slate-800 bg-[#111827] p-6">
                {/* Row 1: Initial Capital, Start Date, End Date */}
                <div className="grid grid-cols-1 gap-5 sm:grid-cols-3">
                  <div>
                    <label className="mb-2 block text-xs font-medium text-slate-400">
                      Initial Capital (₹)
                    </label>
                    <input
                      type="number"
                      min={1000}
                      step={5000}
                      value={initialCapital}
                      onChange={(e) => setInitialCapital(Math.max(1000, Number(e.target.value) || 1000))}
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 font-mono text-sm font-semibold text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-medium text-slate-400">
                      Start Date
                    </label>
                    <input
                      type="date"
                      value={fromDate}
                      onChange={(e) => setFromDate(e.target.value)}
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-medium text-slate-400">
                      End Date
                    </label>
                    <input
                      type="date"
                      value={toDate}
                      onChange={(e) => setToDate(e.target.value)}
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>
                </div>

                {/* Quick period selector strip */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-4">
                  <span className="text-[11px] text-slate-400">
                    Quick Backtest Period ({timeframe} · {symbols.length || 'Universe'} instruments)
                  </span>
                  <div className="flex items-center gap-1.5">
                    {[
                      { label: '1M', days: 30 },
                      { label: '3M', days: 90 },
                      { label: '6M', days: 180 },
                      { label: '1Y', days: 365 },
                      { label: '3Y', days: 1095 },
                    ].map((p) => (
                      <button
                        key={p.label}
                        type="button"
                        onClick={() => applyQuickPeriod(p.days)}
                        className="rounded-md border border-slate-700 bg-[#0b1120] px-2.5 py-1 font-mono text-[11px] font-semibold text-slate-300 hover:border-sky-400 hover:text-sky-300"
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Row 2: Start Time & End Time (H : M AM/PM) */}
                <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
                  <div>
                    <label className="mb-2 block text-xs font-medium text-slate-400">Start Time</label>
                    <div className="flex items-center gap-2">
                      <select
                        value={startHour}
                        onChange={(e) => setStartHour(e.target.value)}
                        aria-label="Start hour"
                        className="min-h-[38px] w-20 rounded-full border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        {['9', '10', '11', '12', '1', '2', '3'].map((h) => (
                          <option key={h} value={h}>
                            {h}
                          </option>
                        ))}
                      </select>
                      <span className="font-mono text-slate-500">:</span>
                      <select
                        value={startMinute}
                        onChange={(e) => setStartMinute(e.target.value)}
                        aria-label="Start minute"
                        className="min-h-[38px] w-20 rounded-full border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        {['00', '05', '10', '15', '20', '30', '45'].map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => setStartAmPm((v) => (v === 'AM' ? 'PM' : 'AM'))}
                        className="min-h-[38px] rounded-full border border-slate-700 bg-[#0b1120] px-4 font-mono text-xs font-semibold text-sky-300 hover:border-sky-400"
                      >
                        {startAmPm}
                      </button>
                    </div>
                  </div>

                  <div>
                    <label className="mb-2 block text-xs font-medium text-slate-400">End Time</label>
                    <div className="flex items-center gap-2">
                      <select
                        value={endHour}
                        onChange={(e) => setEndHour(e.target.value)}
                        aria-label="End hour"
                        className="min-h-[38px] w-20 rounded-full border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        {['9', '10', '11', '12', '1', '2', '3'].map((h) => (
                          <option key={h} value={h}>
                            {h}
                          </option>
                        ))}
                      </select>
                      <span className="font-mono text-slate-500">:</span>
                      <select
                        value={endMinute}
                        onChange={(e) => setEndMinute(e.target.value)}
                        aria-label="End minute"
                        className="min-h-[38px] w-20 rounded-full border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        {['00', '05', '10', '15', '20', '25', '30', '45'].map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => setEndAmPm((v) => (v === 'AM' ? 'PM' : 'AM'))}
                        className="min-h-[38px] rounded-full border border-slate-700 bg-[#0b1120] px-4 font-mono text-xs font-semibold text-sky-300 hover:border-sky-400"
                      >
                        {endAmPm}
                      </button>
                    </div>
                  </div>
                </div>

                {/* Row 3: Holding Type (Intraday MIS / Longterm CNC) + Strategy Cycle */}
                <div className="flex flex-wrap items-end justify-between gap-4 pt-1">
                  <div>
                    <label className="mb-2.5 block text-xs font-medium text-slate-400">Holding Type</label>
                    <div className="flex items-center gap-6">
                      <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-slate-200">
                        <input
                          type="radio"
                          name="holding_type"
                          checked={holdingType === 'MIS'}
                          onChange={() => {
                            setHoldingType('MIS');
                            setSegment('equity_intraday');
                          }}
                          className="h-4 w-4 accent-sky-500"
                        />
                        <span className="font-medium">Intraday</span>
                        <span className="font-mono text-xs text-slate-400">MIS</span>
                      </label>
                      <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-slate-200">
                        <input
                          type="radio"
                          name="holding_type"
                          checked={holdingType === 'CNC'}
                          onChange={() => {
                            setHoldingType('CNC');
                            setSegment('equity_delivery');
                          }}
                          className="h-4 w-4 accent-sky-500"
                        />
                        <span className="font-medium">Longterm</span>
                        <span className="font-mono text-xs text-slate-400">CNC</span>
                      </label>
                    </div>
                  </div>

                  <div className="w-40">
                    <label className="mb-2 block text-xs font-medium text-slate-400">Strategy Cycle</label>
                    <select
                      value={strategyCycle}
                      onChange={(e) => setStrategyCycle(e.target.value)}
                      className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-200 focus:border-sky-400 focus:outline-none"
                    >
                      <option value="">- (Unlimited)</option>
                      <option value="1">1 Cycle</option>
                      <option value="2">2 Cycles</option>
                      <option value="3">3 Cycles</option>
                      <option value="5">5 Cycles</option>
                      <option value="10">10 Cycles</option>
                    </select>
                  </div>
                </div>

                {/* Secondary Execution & Charges strip */}
                <div className="grid grid-cols-1 gap-3 border-t border-slate-800/80 pt-4 text-xs sm:grid-cols-3">
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-400">Backtest Mode</label>
                    <select
                      value={btMode}
                      onChange={(e) => setBtMode(e.target.value as 'portfolio' | 'individual')}
                      className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                    >
                      <option value="portfolio">Portfolio (Shared Capital)</option>
                      <option value="individual">Individual (Per Symbol)</option>
                    </select>
                  </div>
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-400">Allocation Mode</label>
                    <select
                      value={allocationMode}
                      onChange={(e) => setAllocationMode(e.target.value)}
                      className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                    >
                      {(meta.allocation_modes || [
                        { id: 'shared_pool', label: 'Shared Capital Pool' },
                        { id: 'equal_split', label: 'Equal Split Per Symbol' },
                        { id: 'risk_based', label: 'Risk-Based Sizing' },
                      ]).map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="mb-1 block text-[11px] text-slate-400">Broker Charges</label>
                    <select
                      value={chargesProfile}
                      onChange={(e) => setChargesProfile(e.target.value)}
                      className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-slate-100"
                    >
                      <option value="zerodha">Zerodha (Real Indian Charges)</option>
                      <option value="upstox">Upstox (Real Indian Charges)</option>
                      <option value="zero">Zero Charges (Gross Only)</option>
                    </select>
                  </div>
                </div>
              </div>

              {error && (
                <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-4 py-2.5 text-xs text-red-200">
                  {error}
                </div>
              )}
            </div>

            {/* Modal Footer matching Streak Backtest date selection.png */}
            <div className="flex items-center justify-between border-t border-slate-800 bg-[#111827] px-6 py-4">
              <div className="flex items-center gap-1.5" aria-label="Step 2 of 2">
                <span className="h-1.5 w-1.5 rounded-full bg-slate-600" />
                <span className="h-1.5 w-7 rounded-full bg-sky-500" />
              </div>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => setExecuteModalOpen(false)}
                  disabled={running}
                  className="min-h-[38px] rounded-lg border border-slate-700 bg-[#0b1120] px-5 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
                >
                  Back
                </button>
                <button
                  type="button"
                  onClick={() => void handleExecuteBacktest()}
                  disabled={running}
                  className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg bg-sky-500 px-6 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
                >
                  <Play className="h-3.5 w-3.5" />
                  <span>{running ? 'Running Backtest...' : 'Run Backtest'}</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
