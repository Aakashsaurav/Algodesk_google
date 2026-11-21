import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowRight,
  Bell,
  Code2,
  Copy,
  Eye,
  FolderOpen,
  Play,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import { BacktestResultsView, computeTradeAnalytics } from './BacktestResultsView';
import type { QuickOrderTarget } from './QuickOrderModal';
import type {
  BacktestFullResult,
  StrategyDraftPreset,
  StrategySpecMeta,
  StreakMetaPayload,
  VisualRuleDef,
} from './types';

interface DiscoverStrategiesViewProps {
  meta: StreakMetaPayload;
  onRefreshMeta: () => void;
  onOpenInCreateStrategy: (draft: StrategyDraftPreset) => void;
  onDeployed: () => void;
  onQuickOrder: (target: QuickOrderTarget) => void;
}

interface UnifiedStrategyItem {
  key: string;
  id: string;
  name: string;
  description: string;
  engine: 'basic' | 'advance' | 'python';
  category: 'equity_swing' | 'equity_intraday' | 'python';
  timeframe: string;
  symbols: string[];
  segment: string;
  rule?: VisualRuleDef;
  pySpec?: StrategySpecMeta;
}

const CAT_TABS = [
  { id: 'all', label: 'All' },
  { id: 'equity_swing', label: 'Equity Swing' },
  { id: 'equity_intraday', label: 'Equity Intraday' },
  { id: 'python', label: 'Python (.py)' },
];

export function DiscoverStrategiesView({
  meta,
  onRefreshMeta,
  onOpenInCreateStrategy,
  onDeployed,
  onQuickOrder,
}: DiscoverStrategiesViewProps) {
  const [search, setSearch] = useState('');
  const [activeTab, setActiveTab] = useState('all');
  const [selectedKey, setSelectedKey] = useState('');

  // Backtest preview cache per strategy key
  const [resultsByKey, setResultsByKey] = useState<Record<string, BacktestFullResult>>({});
  const [loadingKey, setLoadingKey] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [previewSymbol, setPreviewSymbol] = useState<string>('COMBINED');

  // Full result drill-down mode
  const [fullViewResult, setFullViewResult] = useState<BacktestFullResult | null>(null);

  // Modals & Delete confirmation
  const [myStrategiesOpen, setMyStrategiesOpen] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const allStrategies = useMemo<UnifiedStrategyItem[]>(() => {
    const visuals: UnifiedStrategyItem[] = (meta.visual_rules || [])
      .filter((r) => r.kind === 'strategy')
      .map((r) => {
        const tf = r.timeframe || '1day';
        const isIntraday = tf.includes('minute') || r.segment === 'equity_intraday';
        return {
          key: `visual:${r.id || r.name}`,
          id: r.id || r.name,
          name: r.name,
          description: r.description || 'Visual multi-condition strategy',
          engine: r.builder_mode === 'advance' ? 'advance' : 'basic',
          category: isIntraday ? 'equity_intraday' : 'equity_swing',
          timeframe: tf,
          symbols:
            r.default_symbols && r.default_symbols.length > 0
              ? r.default_symbols
              : ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'SBIN'],
          segment: r.segment || (isIntraday ? 'equity_intraday' : 'equity_delivery'),
          rule: r,
        };
      });

    const pythons: UnifiedStrategyItem[] = (meta.strategy_specs || []).map((s) => ({
      key: `python:${s.id}`,
      id: s.id,
      name: s.name,
      description: s.description || 'Python (.py) algorithmic strategy plugin',
      engine: 'python',
      category: 'python',
      timeframe: '1day',
      symbols: ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'SBIN'],
      segment: 'equity_delivery',
      pySpec: s,
    }));

    return [...visuals, ...pythons];
  }, [meta.visual_rules, meta.strategy_specs]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allStrategies.filter((s) => {
      if (activeTab !== 'all' && s.category !== activeTab) return false;
      if (q && !s.name.toLowerCase().includes(q) && !s.description.toLowerCase().includes(q)) {
        return false;
      }
      return true;
    });
  }, [allStrategies, activeTab, search]);

  const selected = useMemo(() => {
    return (
      allStrategies.find((s) => s.key === selectedKey) ||
      filtered[0] ||
      allStrategies[0] ||
      null
    );
  }, [allStrategies, filtered, selectedKey]);

  const runPreviewBacktest = useCallback(async (item: UnifiedStrategyItem) => {
    setLoadingKey(item.key);
    setError('');
    setPreviewSymbol('COMBINED');
    try {
      const toIso = new Date().toISOString().slice(0, 10);
      const isIntraday = item.timeframe.includes('minute');
      const fromIso = new Date(Date.now() - (isIntraday ? 35 : 180) * 86400_000)
        .toISOString()
        .slice(0, 10);

      const payload: Record<string, unknown> = {
        mode: 'portfolio',
        allocation_mode: 'shared_pool',
        strategy_mode: item.engine === 'python' ? 'python' : 'visual',
        strategy: item.engine === 'python' ? item.id : `visual:${item.id}`,
        rule_def: item.engine !== 'python' ? item.rule : undefined,
        symbols: item.symbols.slice(0, 6),
        exchange: 'NSE',
        timeframe: item.timeframe,
        from_date: fromIso,
        to_date: toIso,
        side: item.rule?.side || 'BUY',
        holding_type: item.segment === 'equity_intraday' ? 'MIS' : 'CNC',
        initial_capital: 500000,
        stop_loss_pct: item.rule?.stop_loss_pct ?? 2.0,
        take_profit_pct: item.rule?.take_profit_pct ?? 4.0,
        trailing_sl_pct: item.rule?.trailing_sl_pct ?? 0,
        charges_profile: 'zerodha',
        segment: item.segment,
      };
      const res = await fetch('/api/streak/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Preview backtest failed.');
      setResultsByKey((prev) => ({ ...prev, [item.key]: body as BacktestFullResult }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backtest preview failed.');
    } finally {
      setLoadingKey(null);
    }
  }, []);

  useEffect(() => {
    if (selected && !selectedKey) {
      setSelectedKey(selected.key);
    }
  }, [selected, selectedKey]);

  // Auto-run preview backtest once per selected strategy if not cached yet
  useEffect(() => {
    if (selected && !resultsByKey[selected.key] && loadingKey !== selected.key) {
      void runPreviewBacktest(selected);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.key]);

  const activeResult = selected ? resultsByKey[selected.key] || null : null;

  const activeTrades = useMemo(() => {
    if (!activeResult) return [];
    if (previewSymbol === 'COMBINED') return activeResult.trades || [];
    return (activeResult.trades || []).filter(
      (t) => (t.symbol || '').toUpperCase() === previewSymbol.toUpperCase()
    );
  }, [activeResult, previewSymbol]);

  const stats = useMemo(() => computeTradeAnalytics(activeTrades, true), [activeTrades]);

  const handleDeleteRule = async (ruleIdToDelete: string) => {
    setDeletingId(ruleIdToDelete);
    setError('');
    try {
      const res = await fetch(`/api/streak/rules/${encodeURIComponent(ruleIdToDelete)}`, {
        method: 'DELETE',
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not delete strategy.');
      setConfirmDeleteId(null);
      if (selected?.rule?.id === ruleIdToDelete) {
        setSelectedKey('');
      }
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed.');
    } finally {
      setDeletingId(null);
    }
  };

  const openStrategyInBuilder = (item: UnifiedStrategyItem, isCopy: boolean) => {
    onOpenInCreateStrategy({
      mode: item.engine,
      rule: item.rule ? JSON.parse(JSON.stringify(item.rule)) : undefined,
      pythonStrategyId: item.engine === 'python' ? item.id : undefined,
      name: isCopy ? `${item.name} (Copy)` : item.name,
      description: item.description,
      symbols: item.symbols,
      timeframe: item.timeframe,
      chartType: item.rule?.chart_type || 'candlestick',
      segment: item.segment,
      isCopy,
    });
  };

  if (fullViewResult && selected) {
    return (
      <BacktestResultsView
        result={fullViewResult}
        onBack={() => setFullViewResult(null)}
        onViewStrategy={() => openStrategyInBuilder(selected, false)}
        onCopyStrategy={() => openStrategyInBuilder(selected, true)}
        onDeployed={onDeployed}
        onQuickOrder={onQuickOrder}
      />
    );
  }

  const mySavedStrategies = (meta.visual_rules || []).filter((r) => r.kind === 'strategy');

  return (
    <div className="space-y-4">
      {/* Top Action Header matching Streak Strategy-1.png */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-100">Discover Strategies</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            Explore ready-to-backtest visual strategies and custom Python (.py) quant plugins with real Indian trading charges.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() =>
              onOpenInCreateStrategy({
                mode: 'basic',
                name: 'Untitled Strategy',
                symbols: ['RELIANCE', 'TCS', 'HDFCBANK'],
                timeframe: '1day',
              })
            }
            className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
          >
            <Plus className="h-4 w-4" />
            <span>Create Strategy</span>
          </button>
          <button
            type="button"
            onClick={() => setMyStrategiesOpen(true)}
            className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-4 text-xs font-semibold text-sky-300 hover:border-sky-400"
          >
            <FolderOpen className="h-4 w-4" />
            <span>My Strategies ({mySavedStrategies.length})</span>
          </button>
        </div>
      </div>

      {/* Main Split-Pane matching Streak Strategy-1.png & Strategy-2.png */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Left List Pane (4 cols) */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827] lg:col-span-4">
          <div className="space-y-3 border-b border-slate-800 p-3.5">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search strategies"
                className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] pl-8 pr-3 text-xs text-slate-100 placeholder-slate-500 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div className="flex items-center gap-1 overflow-x-auto pb-0.5">
              {CAT_TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setActiveTab(t.id)}
                  className={`min-h-[28px] shrink-0 whitespace-nowrap rounded-md px-2.5 text-xs font-semibold transition-colors ${
                    activeTab === t.id
                      ? 'bg-sky-500/20 text-sky-300'
                      : 'text-slate-400 hover:bg-slate-800/80 hover:text-slate-200'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>

          <div className="max-h-[680px] flex-1 divide-y divide-slate-800/70 overflow-y-auto">
            {filtered.map((item) => {
              const isSel = selected?.key === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setSelectedKey(item.key)}
                  className={`flex w-full items-start justify-between gap-3 px-4 py-3.5 text-left transition-colors ${
                    isSel ? 'bg-sky-950/40' : 'hover:bg-slate-800/40'
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className={`truncate text-sm font-semibold ${isSel ? 'text-sky-300' : 'text-slate-100'}`}>
                        {item.name}
                      </span>
                      {item.engine === 'python' && <Code2 className="h-3.5 w-3.5 shrink-0 text-amber-400" />}
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs text-slate-400">{item.description}</p>
                  </div>
                  <div className="shrink-0 text-right font-mono text-[11px] text-slate-400">
                    <div>{item.timeframe}</div>
                    <div className="mt-0.5 text-[10px] uppercase text-slate-500">
                      {item.engine === 'python' ? '.py' : item.category.replace('equity_', '')}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Right Preview Backtest Pane (8 cols) */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827] lg:col-span-8">
          {selected ? (
            <>
              {/* Strategy Title Bar */}
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-lg font-bold text-slate-100">{selected.name}</h3>
                    <span className="font-mono text-xs text-slate-400">
                      · {selected.timeframe} · {selected.engine === 'python' ? 'Python (.py)' : 'Visual'}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-slate-400">{selected.description}</p>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  {selected.rule?.id && (
                    confirmDeleteId === selected.rule.id ? (
                      <div className="inline-flex items-center gap-1.5 rounded-lg border border-red-800/80 bg-red-950/50 px-2.5 py-1">
                        <span className="text-[11px] text-red-200">Delete?</span>
                        <button
                          type="button"
                          disabled={deletingId === selected.rule.id}
                          onClick={() => void handleDeleteRule(selected.rule!.id!)}
                          className="rounded bg-red-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-red-500 disabled:opacity-50"
                        >
                          {deletingId === selected.rule.id ? '...' : 'Confirm'}
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmDeleteId(null)}
                          className="text-[11px] text-slate-300 hover:text-white"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirmDeleteId(selected.rule!.id!)}
                        className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-red-900/70 bg-red-950/30 px-2.5 text-xs font-semibold text-red-300 hover:border-red-500 hover:text-red-200"
                        title="Delete strategy"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        <span>Delete</span>
                      </button>
                    )
                  )}
                  <button
                    type="button"
                    onClick={() => openStrategyInBuilder(selected, false)}
                    className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
                  >
                    <Eye className="h-3.5 w-3.5 text-sky-400" />
                    <span>View Strategy</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => void runPreviewBacktest(selected)}
                    disabled={loadingKey === selected.key}
                    className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
                  >
                    <Play className="h-3.5 w-3.5" />
                    <span>{loadingKey === selected.key ? 'Backtesting...' : 'Re-run Backtest'}</span>
                  </button>
                  {activeResult && (
                    <button
                      type="button"
                      onClick={() => setFullViewResult(activeResult)}
                      className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg bg-sky-500 px-3.5 text-xs font-semibold text-slate-950 hover:bg-sky-400"
                    >
                      <span>Full Backtest Results</span>
                      <ArrowRight className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>

              <div className="flex-1 space-y-5 p-5">
                {error && (
                  <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-4 py-2.5 text-xs text-red-200">
                    {error}
                  </div>
                )}

                {loadingKey === selected.key && !activeResult ? (
                  <div className="flex h-72 flex-col items-center justify-center gap-2 text-xs text-slate-400">
                    <div className="h-6 w-6 animate-spin rounded-full border-2 border-slate-700 border-t-sky-400" />
                    <span>Running portfolio backtest with real Zerodha charges...</span>
                  </div>
                ) : activeResult ? (
                  <>
                    {/* Horizontal Instrument Tiles (Cumulative + Per-Symbol) matching Strategy-1.png */}
                    <div className="flex gap-2.5 overflow-x-auto pb-1">
                      <button
                        type="button"
                        onClick={() => setPreviewSymbol('COMBINED')}
                        className={`min-w-[148px] shrink-0 rounded-lg border p-3 text-left transition-colors ${
                          previewSymbol === 'COMBINED'
                            ? 'border-sky-500 bg-sky-950/40'
                            : 'border-slate-800 bg-[#0b1120] hover:border-slate-700'
                        }`}
                      >
                        <div className="text-xs font-semibold text-slate-200">Cumulative</div>
                        <div
                          className={`mt-1 font-mono text-sm font-bold tabular-nums ${
                            activeResult.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                          }`}
                        >
                          {activeResult.net_pnl >= 0 ? '+' : ''}₹{activeResult.net_pnl.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                        </div>
                        <div className="mt-0.5 font-mono text-[10px] text-slate-400">
                          {activeResult.total_trades} trades · {activeResult.win_rate_pct.toFixed(0)}% win
                        </div>
                      </button>

                      {(activeResult.per_symbol || []).map((row) => (
                        <button
                          key={row.symbol}
                          type="button"
                          onClick={() => setPreviewSymbol(row.symbol)}
                          className={`min-w-[136px] shrink-0 rounded-lg border p-3 text-left transition-colors ${
                            previewSymbol === row.symbol
                              ? 'border-sky-500 bg-sky-950/40'
                              : 'border-slate-800 bg-[#0b1120] hover:border-slate-700'
                          }`}
                        >
                          <div className="font-mono text-xs font-semibold text-slate-200">{row.symbol}</div>
                          <div
                            className={`mt-1 font-mono text-sm font-bold tabular-nums ${
                              row.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                            }`}
                          >
                            {row.net_pnl >= 0 ? '+' : ''}₹{row.net_pnl.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                          </div>
                          <div className="mt-0.5 font-mono text-[10px] text-slate-400">
                            {row.trades} trades · {row.win_rate_pct.toFixed(0)}% win
                          </div>
                        </button>
                      ))}
                    </div>

                    {/* Metrics Row */}
                    <div className="grid grid-cols-3 gap-3 rounded-lg border border-slate-800 bg-[#0b1120] p-4 sm:grid-cols-6">
                      <div>
                        <div className="text-[11px] text-slate-400">Net P&amp;L</div>
                        <div
                          className={`mt-1 font-mono text-sm font-bold tabular-nums ${
                            stats.totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                          }`}
                        >
                          {stats.totalPnl >= 0 ? '+' : ''}₹{stats.totalPnl.toFixed(0)}
                        </div>
                      </div>
                      <div>
                        <div className="text-[11px] text-slate-400">W / L Count</div>
                        <div className="mt-1 font-mono text-sm font-semibold tabular-nums">
                          <span className="text-emerald-400">{stats.wins}</span> /{' '}
                          <span className="text-red-400">{stats.losses}</span>
                        </div>
                      </div>
                      <div>
                        <div className="text-[11px] text-slate-400">Trades</div>
                        <div className="mt-1 font-mono text-sm font-semibold text-slate-100">{stats.totalTrades}</div>
                      </div>
                      <div>
                        <div className="text-[11px] text-slate-400">Max Gain</div>
                        <div className="mt-1 font-mono text-sm font-semibold text-emerald-400">
                          {stats.maxGain.toFixed(1)}
                        </div>
                      </div>
                      <div>
                        <div className="text-[11px] text-slate-400">Max Loss</div>
                        <div className="mt-1 font-mono text-sm font-semibold text-red-400">
                          {stats.maxLoss.toFixed(1)}
                        </div>
                      </div>
                      <div>
                        <div className="text-[11px] text-slate-400">Max DD</div>
                        <div className="mt-1 font-mono text-sm font-semibold text-amber-300">
                          {activeResult.max_drawdown_pct.toFixed(2)}%
                        </div>
                      </div>
                    </div>

                    {/* Daily Summary + Monthly Heatmap */}
                    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                      <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3.5">
                        <h4 className="mb-2.5 text-xs font-semibold text-slate-300">Hypothetical Daily Summary</h4>
                        <table className="w-full border-collapse text-center text-xs">
                          <thead className="border-b border-slate-800 text-[11px] text-slate-400">
                            <tr>
                              <th className="py-1.5 text-left">Day</th>
                              <th className="py-1.5">Avg. Return</th>
                              <th className="py-1.5">Max Profit</th>
                              <th className="py-1.5">Max Loss</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-slate-800/60 font-mono text-[11px] tabular-nums">
                            {stats.dailySummary.map((d) => (
                              <tr key={d.day}>
                                <td className="py-1.5 text-left font-sans text-slate-300">{d.day}</td>
                                <td className={d.avg >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                                  {d.avg >= 0 ? '+' : ''}
                                  {d.avg.toFixed(2)}%
                                </td>
                                <td className="text-emerald-400">{d.maxProfit.toFixed(2)}%</td>
                                <td className="text-red-400">{d.maxLoss.toFixed(2)}%</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>

                      <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3.5">
                        <h4 className="mb-2.5 text-xs font-semibold text-slate-300">Hypothetical Monthly Performance</h4>
                        <div className="grid grid-cols-6 gap-2">
                          {stats.monthlySummary.map((m) => {
                            const has = m.count > 0;
                            const pos = m.retPct >= 0;
                            return (
                              <div key={m.label} className="flex flex-col items-center gap-1">
                                <div
                                  className={`flex h-11 w-full items-center justify-center rounded border font-mono text-[10px] font-bold ${
                                    !has
                                      ? 'border-slate-800 bg-[#111827] text-slate-600'
                                      : pos
                                        ? 'border-emerald-700/70 bg-emerald-600/25 text-emerald-300'
                                        : 'border-red-800/70 bg-red-600/25 text-red-300'
                                  }`}
                                >
                                  {has ? `${pos ? '+' : ''}${m.retPct.toFixed(1)}%` : '—'}
                                </div>
                                <span className="text-[10px] text-slate-500">{m.label}</span>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  </>
                ) : null}
              </div>

              {/* Bottom Sticky Bar matching Streak Strategy-1.png */}
              <div className="flex flex-wrap items-center justify-end gap-2.5 border-t border-slate-800 bg-[#0b1120]/70 px-5 py-3.5">
                <button
                  type="button"
                  onClick={() => openStrategyInBuilder(selected, true)}
                  className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-[#111827] px-4 text-xs font-semibold text-sky-300 hover:border-sky-400"
                >
                  <Copy className="h-3.5 w-3.5" />
                  <span>Copy</span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (activeResult) {
                      setFullViewResult(activeResult);
                    } else {
                      openStrategyInBuilder(selected, false);
                    }
                  }}
                  className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
                >
                  <Bell className="h-3.5 w-3.5" />
                  <span>Notification Based Trading</span>
                </button>
              </div>
            </>
          ) : (
            <div className="flex h-80 items-center justify-center text-xs text-slate-500">
              Select a strategy from the left panel to preview backtest results.
            </div>
          )}
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
                        setMyStrategiesOpen(false);
                        onOpenInCreateStrategy({
                          mode: r.builder_mode === 'advance' ? 'advance' : 'basic',
                          rule: JSON.parse(JSON.stringify(r)),
                          name: r.name,
                          description: r.description,
                          symbols: r.default_symbols,
                          timeframe: r.timeframe,
                          chartType: r.chart_type || 'candlestick',
                          segment: r.segment,
                        });
                      }}
                      className="rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 py-1.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
                    >
                      Edit
                    </button>
                    {r.id && (
                      confirmDeleteId === r.id ? (
                        <div className="inline-flex items-center gap-1">
                          <button
                            type="button"
                            disabled={deletingId === r.id}
                            onClick={() => void handleDeleteRule(r.id!)}
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
    </div>
  );
}
