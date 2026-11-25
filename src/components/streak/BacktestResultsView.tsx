import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import {
  ArrowLeft,
  Bell,
  Calendar,
  Copy,
  Download,
  Eye,
  FileSpreadsheet,
  Lock,
  Receipt,
  Rocket,
  X,
} from 'lucide-react';
import { CandlestickChartPanel } from './CandlestickChartPanel';
import { PortfolioPnlChart } from './PortfolioPnlChart';
import type { QuickOrderTarget } from './QuickOrderModal';
import type { BacktestFullResult, BacktestTradeRow } from './types';
import { fetchSecurityStatus, type SecurityStatus } from '../../utils/authClient';

interface BacktestResultsViewProps {
  result: BacktestFullResult;
  onBack?: () => void;
  onViewStrategy?: () => void;
  onCopyStrategy?: () => void;
  onDeployed?: () => void;
  onQuickOrder?: (target: QuickOrderTarget) => void;
}

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function computeTradeAnalytics(trades: BacktestTradeRow[], includeCharges: boolean) {
  let totalPnl = 0;
  let wins = 0;
  let losses = 0;
  let maxGain = 0;
  let maxLoss = 0;
  let winSum = 0;
  let lossSum = 0;
  let curW = 0;
  let curL = 0;
  let maxWStreak = 0;
  let maxLStreak = 0;

  let peak = 0;
  let cum = 0;
  let maxDdAbs = 0;

  const dayBuckets: Record<number, number[]> = { 1: [], 2: [], 3: [], 4: [], 5: [] };
  const monthBuckets: Record<number, { pnl: number; retSum: number; count: number }> = {};
  for (let m = 0; m < 12; m += 1) monthBuckets[m] = { pnl: 0, retSum: 0, count: 0 };
  const yearMonthBuckets: Record<string, Record<number, { pnl: number; retSum: number; count: number }>> = {};

  const cumSeries: Array<{ label: string; pnl: number; symbol?: string; tradePnl?: number }> = [
    { label: 'Start', pnl: 0 },
  ];

  for (const t of trades) {
    const val = includeCharges ? Number(t.pnl ?? 0) : Number(t.gross_pnl ?? t.pnl ?? 0);
    const notional = Math.max(1, Number(t.entry_price || 0) * Math.max(1, Number(t.qty || 1)));
    const retPct = (val / notional) * 100;

    totalPnl += val;
    cum += val;
    if (cum > peak) peak = cum;
    const dd = peak - cum;
    if (dd > maxDdAbs) maxDdAbs = dd;
    cumSeries.push({
      label: (t.exit_date || t.entry_date || '').slice(0, 10),
      pnl: cum,
      symbol: t.symbol,
      tradePnl: val,
    });

    if (val >= 0) {
      wins += 1;
      winSum += val;
      if (val > maxGain) maxGain = val;
      curW += 1;
      curL = 0;
      if (curW > maxWStreak) maxWStreak = curW;
    } else {
      losses += 1;
      lossSum += val;
      if (val < maxLoss) maxLoss = val;
      curL += 1;
      curW = 0;
      if (curL > maxLStreak) maxLStreak = curL;
    }

    const dtStr = (t.exit_date || t.entry_date || '').slice(0, 10);
    if (dtStr.length === 10) {
      const dt = new Date(`${dtStr}T00:00:00Z`);
      if (!Number.isNaN(dt.getTime())) {
        const dow = dt.getUTCDay();
        if (dayBuckets[dow]) dayBuckets[dow].push(retPct);
        const mo = dt.getUTCMonth();
        const yr = dtStr.slice(0, 4);
        if (monthBuckets[mo]) {
          monthBuckets[mo].pnl += val;
          monthBuckets[mo].retSum += retPct;
          monthBuckets[mo].count += 1;
        }
        if (!yearMonthBuckets[yr]) {
          yearMonthBuckets[yr] = {};
          for (let m = 0; m < 12; m += 1) yearMonthBuckets[yr][m] = { pnl: 0, retSum: 0, count: 0 };
        }
        if (yearMonthBuckets[yr][mo]) {
          yearMonthBuckets[yr][mo].pnl += val;
          yearMonthBuckets[yr][mo].retSum += retPct;
          yearMonthBuckets[yr][mo].count += 1;
        }
      }
    }
  }

  const dailySummary = WEEKDAYS.map((day, i) => {
    const arr = dayBuckets[i + 1] || [];
    if (!arr.length) return { day, avg: 0, maxProfit: 0, maxLoss: 0, count: 0 };
    const avg = arr.reduce((a, b) => a + b, 0) / arr.length;
    const maxProfit = Math.max(0, ...arr);
    const maxLossDay = Math.min(0, ...arr);
    return { day, avg, maxProfit, maxLoss: maxLossDay, count: arr.length };
  });

  const monthlySummary = MONTHS.map((label, m) => ({
    label,
    pnl: monthBuckets[m].pnl,
    retPct: monthBuckets[m].retSum,
    count: monthBuckets[m].count,
  }));

  const availableYears = Object.keys(yearMonthBuckets).sort();
  const monthlyByYear: Record<string, typeof monthlySummary> = {};
  for (const yr of availableYears) {
    monthlyByYear[yr] = MONTHS.map((label, m) => ({
      label,
      pnl: yearMonthBuckets[yr][m].pnl,
      retPct: yearMonthBuckets[yr][m].retSum,
      count: yearMonthBuckets[yr][m].count,
    }));
  }

  return {
    totalPnl,
    wins,
    losses,
    totalTrades: trades.length,
    maxWStreak,
    maxLStreak,
    maxGain,
    maxLoss,
    winAvg: wins > 0 ? winSum / wins : 0,
    lossAvg: losses > 0 ? lossSum / losses : 0,
    maxDdAbs,
    dailySummary,
    monthlySummary,
    availableYears,
    monthlyByYear,
    cumSeries,
  };
}

export function BacktestResultsView({
  result,
  onBack,
  onViewStrategy,
  onCopyStrategy,
  onDeployed,
  onQuickOrder,
}: BacktestResultsViewProps) {
  const [selectedSymbol, setSelectedSymbol] = useState<string>('COMBINED');
  const [includeCharges, setIncludeCharges] = useState<boolean>(true);
  const [showChargesBreakdown, setShowChargesBreakdown] = useState<boolean>(false);
  const [showAllTrades, setShowAllTrades] = useState<boolean>(false);
  const [selectedYear, setSelectedYear] = useState<string>('ALL');

  // Deploy modal state
  const [deployOpen, setDeployOpen] = useState(false);
  const [deployExecMode, setDeployExecMode] = useState<'live_alert' | 'paper' | 'live_auto'>('paper');
  const [deployEvalMode, setDeployEvalMode] = useState<'completed_bar' | 'intrabar'>('completed_bar');
  const [deployExchange, setDeployExchange] = useState<string>(result.deploy_spec?.exchange || 'NSE');
  const [deployProduct, setDeployProduct] = useState<string>(
    result.deploy_spec?.product || (result.holding_type === 'CNC' ? 'CNC' : 'MIS')
  );
  const [deployName, setDeployName] = useState(`${result.strategy_label} — Live`);
  const [deployBusy, setDeployBusy] = useState(false);
  const [deployMsg, setDeployMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [securityStatus, setSecurityStatus] = useState<SecurityStatus | null>(null);
  const [ohlcvExporting, setOhlcvExporting] = useState(false);
  const [ohlcvError, setOhlcvError] = useState('');

  useEffect(() => {
    if (!deployOpen) return;
    void fetchSecurityStatus().then((st) => {
      if (st) setSecurityStatus(st);
    });
  }, [deployOpen]);

  const combinedStats = useMemo(
    () => computeTradeAnalytics(result.trades || [], includeCharges),
    [result.trades, includeCharges]
  );

  const activeTrades = useMemo(() => {
    const all = result.trades || [];
    if (selectedSymbol === 'COMBINED') return all;
    return all.filter((t) => (t.symbol || '').toUpperCase() === selectedSymbol.toUpperCase());
  }, [result.trades, selectedSymbol]);

  const activeStats = useMemo(
    () => computeTradeAnalytics(activeTrades, includeCharges),
    [activeTrades, includeCharges]
  );

  const chartSymbol = useMemo(() => {
    if (selectedSymbol !== 'COMBINED') return selectedSymbol;
    return result.per_symbol?.[0]?.symbol || result.symbols?.[0] || 'RELIANCE';
  }, [selectedSymbol, result.per_symbol, result.symbols]);

  const activeSymbolRow = useMemo(
    () => (result.per_symbol || []).find((s) => s.symbol === selectedSymbol),
    [result.per_symbol, selectedSymbol]
  );

  const maxDdPctDisplay = useMemo(() => {
    if (selectedSymbol === 'COMBINED') return result.max_drawdown_pct ?? 0;
    if (activeSymbolRow?.max_drawdown_pct !== undefined) return activeSymbolRow.max_drawdown_pct;
    return result.initial_capital > 0 ? (activeStats.maxDdAbs / result.initial_capital) * 100 : 0;
  }, [selectedSymbol, result.max_drawdown_pct, result.initial_capital, activeSymbolRow, activeStats.maxDdAbs]);

  const yearLabel = (result.to_date || result.from_date || new Date().toISOString()).slice(0, 4);

  const handleDownloadCsv = () => {
    if (!activeTrades.length) return;
    const headers = [
      'Symbol',
      'Entry Date',
      'Exit Date',
      'Qty',
      'Entry Price',
      'Exit Price',
      'Gross P&L',
      'Charges',
      'Net P&L',
      'Return %',
      'Exit Reason',
    ];
    const lines = [headers.join(',')];
    for (const t of activeTrades) {
      lines.push(
        [
          t.symbol || chartSymbol,
          t.entry_date,
          t.exit_date,
          t.qty,
          t.entry_price.toFixed(2),
          t.exit_price.toFixed(2),
          t.gross_pnl.toFixed(2),
          t.charges.toFixed(2),
          t.pnl.toFixed(2),
          t.return_pct.toFixed(2),
          `"${(t.exit_reason || '').replace(/"/g, '""')}"`,
        ].join(',')
      );
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `trade_log_${result.strategy}_${selectedSymbol}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleDeploySubmit = async () => {
    setDeployBusy(true);
    setDeployMsg(null);
    try {
      const res = await apiFetch('/api/streak/deployments/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...(result.deploy_spec || {}),
          name: deployName.trim() || `${result.strategy_label} Live`,
          exec_mode: deployExecMode,
          evaluation_mode: deployEvalMode,
          exchange: deployExchange,
          product: deployProduct,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Deployment failed.');
      setDeployMsg({ type: 'ok', text: `Deployed "${deployName}" in ${deployExecMode.replace('_', ' ')} mode.` });
      window.setTimeout(() => {
        setDeployOpen(false);
        setDeployMsg(null);
        onDeployed?.();
      }, 700);
    } catch (err) {
      setDeployMsg({ type: 'err', text: err instanceof Error ? err.message : 'Deployment failed.' });
    } finally {
      setDeployBusy(false);
    }
  };

  const handleDownloadOhlcvCsv = async (scope: 'active' | 'all') => {
    if (!result.run_id) return;
    setOhlcvExporting(true);
    setOhlcvError('');
    try {
      const symParam = scope === 'all' || selectedSymbol === 'COMBINED' ? 'ALL' : selectedSymbol;
      const res = await apiFetch(
        `/api/streak/backtest/${encodeURIComponent(result.run_id)}/ohlcv.csv?symbol=${encodeURIComponent(symParam)}`
      );
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.detail || 'OHLCV + Indicator export not available for this run.');
      }
      const csvText = await res.text();
      const blob = new Blob([csvText], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const slug = (result.strategy || 'strategy').replace(/[^a-zA-Z0-9_-]+/g, '_');
      a.download = `${slug}_${symParam.toLowerCase()}_ohlcv_indicators.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setOhlcvError(err instanceof Error ? err.message : 'Could not download OHLCV CSV.');
    } finally {
      setOhlcvExporting(false);
    }
  };

  const displayedMonthly = useMemo(() => {
    if (selectedYear !== 'ALL' && activeStats.monthlyByYear[selectedYear]) {
      return activeStats.monthlyByYear[selectedYear];
    }
    return activeStats.monthlySummary;
  }, [selectedYear, activeStats.monthlyByYear, activeStats.monthlySummary]);

  const displayedTrades = showAllTrades ? activeTrades : activeTrades.slice(0, 15);
  const cb = result.charges_breakdown;

  return (
    <div className="space-y-4">
      {/* Top Header matching Streak Backtest Result-1.png */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 bg-[#111827] text-slate-300 hover:border-sky-400 hover:text-slate-100"
              title="Back to editor"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
          )}
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-bold tracking-tight text-slate-100">
                {result.strategy_label}
              </h2>
              <span className="font-mono text-xs text-slate-400">
                · {result.mode === 'portfolio' ? `Portfolio (${result.allocation_mode || 'equal'})` : 'Individual'} · {result.timeframe}
              </span>
            </div>
            <p className="mt-0.5 font-mono text-xs text-slate-400">
              {result.from_date} to {result.to_date} · Capital ₹{result.initial_capital.toLocaleString('en-IN')} · Charges: {result.charges_profile.toUpperCase()} ({result.segment})
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {onViewStrategy && (
            <button
              type="button"
              onClick={onViewStrategy}
              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
            >
              <Eye className="h-3.5 w-3.5" />
              <span>View Strategy</span>
            </button>
          )}
          {onCopyStrategy && (
            <button
              type="button"
              onClick={onCopyStrategy}
              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
            >
              <Copy className="h-3.5 w-3.5" />
              <span>Copy Strategy</span>
            </button>
          )}
          <button
            type="button"
            onClick={() => setDeployOpen(true)}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
          >
            <Bell className="h-3.5 w-3.5" />
            <span>Notification Based Trading</span>
          </button>
        </div>
      </div>

      {/* Main Split Grid: Left Combined + Instrument List (3 cols) | Right Analytics & Charts (9 cols) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Left Instrument Rail */}
        <div className="flex flex-col gap-3 lg:col-span-3">
          {/* Combined Results Card */}
          <button
            type="button"
            onClick={() => setSelectedSymbol('COMBINED')}
            className={`w-full rounded-xl border p-4 text-left transition-colors ${
              selectedSymbol === 'COMBINED'
                ? 'border-sky-500/80 bg-sky-950/30'
                : 'border-slate-800 bg-[#111827] hover:border-slate-700'
            }`}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-bold text-slate-100">Combined Results</span>
              <span
                className={`font-mono text-sm font-bold tabular-nums ${
                  combinedStats.totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                }`}
              >
                {combinedStats.totalPnl >= 0 ? '+' : ''}
                {combinedStats.totalPnl.toLocaleString('en-IN', { maximumFractionDigits: 2 })} ₹
              </span>
            </div>
            <div className="mt-2 flex items-center justify-between font-mono text-xs text-slate-400">
              <span>
                Wins: <strong className="text-emerald-400">{combinedStats.wins}</strong>, Losses:{' '}
                <strong className="text-red-400">{combinedStats.losses}</strong>
              </span>
              <span>
                Trades: <strong className="text-slate-200">{combinedStats.totalTrades}</strong>
              </span>
            </div>
          </button>

          {/* Per-Instrument P&L List */}
          <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
            <div className="flex items-center justify-between border-b border-slate-800 bg-[#0b1120] px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              <span>Instrument</span>
              <span>P&amp;L</span>
            </div>
            <div className="max-h-[680px] divide-y divide-slate-800/70 overflow-y-auto">
              {(result.per_symbol || []).map((row) => {
                const pnl = includeCharges ? row.net_pnl : row.gross_pnl;
                const isSel = selectedSymbol === row.symbol;
                return (
                  <button
                    key={row.symbol}
                    type="button"
                    onClick={() => setSelectedSymbol(row.symbol)}
                    className={`flex w-full items-center justify-between px-4 py-3 text-left transition-colors ${
                      isSel ? 'bg-sky-950/40' : 'hover:bg-slate-800/40'
                    }`}
                  >
                    <div>
                      <div className={`font-mono text-xs font-semibold ${isSel ? 'text-sky-300' : 'text-slate-100'}`}>
                        {row.symbol}
                      </div>
                      <div className="mt-0.5 font-mono text-[10px] text-slate-500">
                        {row.trades} trades · {row.win_rate_pct.toFixed(0)}% win
                      </div>
                    </div>
                    <span
                      className={`font-mono text-xs font-semibold tabular-nums ${
                        pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                      }`}
                    >
                      {pnl >= 0 ? '+' : ''}
                      {pnl.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Right Detailed Analytics Pane */}
        <div className="space-y-4 lg:col-span-9">
          {/* Header + Trading Charges Gross/Net Toggle + 9-Column Metrics Strip */}
          <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3.5">
              <div className="flex flex-wrap items-center gap-2.5">
                <h3 className="font-mono text-base font-bold text-slate-100">
                  {selectedSymbol === 'COMBINED' ? 'COMBINED PORTFOLIO' : selectedSymbol}
                </h3>
                <span className="text-xs text-slate-500">{result.deploy_spec?.exchange || 'NSE'}</span>
                <span className="font-mono text-xs text-slate-400">
                  · {includeCharges ? 'Net (After Real Charges)' : 'Gross (Before Charges)'}
                </span>
                {(result.from_date || result.to_date) && (
                  <span className="inline-flex items-center gap-1 rounded-md border border-slate-800 bg-[#0b1120] px-2 py-0.5 font-mono text-[11px] text-slate-300">
                    <Calendar className="h-3 w-3 text-sky-400" />
                    {result.from_date} → {result.to_date}
                  </span>
                )}
              </div>

              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => setShowChargesBreakdown((v) => !v)}
                  className="inline-flex items-center gap-1 font-mono text-xs text-slate-400 hover:text-sky-300"
                >
                  <Receipt className="h-3.5 w-3.5" />
                  <span>Charges: ₹{(result.total_charges || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}</span>
                </button>
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-slate-300">Trading Charges</span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={includeCharges}
                    onClick={() => setIncludeCharges((v) => !v)}
                    className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                      includeCharges ? 'bg-sky-500' : 'bg-slate-700'
                    }`}
                  >
                    <span
                      className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                        includeCharges ? 'translate-x-4' : 'translate-x-1'
                      }`}
                    />
                  </button>
                </div>
              </div>
            </div>

            {showChargesBreakdown && cb && (
              <div className="grid grid-cols-2 gap-3 border-b border-slate-800 bg-[#0b1120] px-5 py-3 font-mono text-[11px] sm:grid-cols-4 lg:grid-cols-7">
                <div>
                  <span className="text-slate-500">Brokerage</span>
                  <div className="text-slate-200">₹{cb.brokerage.toFixed(2)}</div>
                </div>
                <div>
                  <span className="text-slate-500">STT</span>
                  <div className="text-slate-200">₹{cb.stt.toFixed(2)}</div>
                </div>
                <div>
                  <span className="text-slate-500">Exchange Txn</span>
                  <div className="text-slate-200">₹{cb.exchange_txn.toFixed(2)}</div>
                </div>
                <div>
                  <span className="text-slate-500">SEBI Fee</span>
                  <div className="text-slate-200">₹{cb.sebi_fee.toFixed(2)}</div>
                </div>
                <div>
                  <span className="text-slate-500">GST</span>
                  <div className="text-slate-200">₹{cb.gst.toFixed(2)}</div>
                </div>
                <div>
                  <span className="text-slate-500">Stamp + DP</span>
                  <div className="text-slate-200">₹{(cb.stamp_duty + cb.dp_charges).toFixed(2)}</div>
                </div>
                <div>
                  <span className="text-slate-500">Slippage</span>
                  <div className="text-slate-200">₹{cb.slippage_cost.toFixed(2)}</div>
                </div>
              </div>
            )}

            {/* 9-Metric Streak Strip */}
            <div className="grid grid-cols-3 gap-4 px-5 py-4 sm:grid-cols-5 lg:grid-cols-9">
              <div>
                <div className="text-[11px] text-slate-400">Total P&amp;L</div>
                <div
                  className={`mt-1 font-mono text-sm font-bold tabular-nums ${
                    activeStats.totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                  }`}
                >
                  {activeStats.totalPnl >= 0 ? '+' : ''}
                  {activeStats.totalPnl.toLocaleString('en-IN', { maximumFractionDigits: 2 })} ₹
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">W / L Count</div>
                <div className="mt-1 font-mono text-sm font-semibold tabular-nums">
                  <span className="text-emerald-400">{activeStats.wins}</span>
                  <span className="mx-1 text-slate-600">/</span>
                  <span className="text-red-400">{activeStats.losses}</span>
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">Trades</div>
                <div className="mt-1 font-mono text-sm font-semibold text-slate-100 tabular-nums">
                  {activeStats.totalTrades}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">W / L Streak</div>
                <div className="mt-1 font-mono text-sm font-semibold tabular-nums">
                  <span className="text-emerald-400">{activeStats.maxWStreak}</span>
                  <span className="mx-1 text-slate-600">/</span>
                  <span className="text-red-400">{activeStats.maxLStreak}</span>
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">Max Gain</div>
                <div className="mt-1 font-mono text-sm font-semibold text-emerald-400 tabular-nums">
                  {activeStats.maxGain.toFixed(2)}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">Max Loss</div>
                <div className="mt-1 font-mono text-sm font-semibold text-red-400 tabular-nums">
                  {activeStats.maxLoss.toFixed(2)}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">Winning Avg.</div>
                <div className="mt-1 font-mono text-sm font-semibold text-slate-100 tabular-nums">
                  {activeStats.winAvg.toFixed(2)}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">Losing Avg.</div>
                <div className="mt-1 font-mono text-sm font-semibold text-slate-100 tabular-nums">
                  {activeStats.lossAvg.toFixed(2)}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-400">Max DD</div>
                <div className="mt-1 font-mono text-sm font-semibold text-amber-300 tabular-nums">
                  {maxDdPctDisplay.toFixed(2)}%
                </div>
              </div>
            </div>

            {/* Sharpe (Daily √252) & Execution Realism Footnote */}
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800/80 bg-[#0b1120]/70 px-5 py-2 font-mono text-[11px] text-slate-400">
              <div className="flex flex-wrap items-center gap-3">
                <span>
                  Sharpe (Daily √252):{' '}
                  <strong className="text-slate-200">
                    {Number(result.daily_sharpe_ratio ?? result.sharpe_daily ?? result.sharpe_ratio ?? 0).toFixed(2)}
                  </strong>
                </span>
                {typeof (result.bar_sharpe_ratio ?? result.sharpe_bar) === 'number' && (
                  <span>
                    Bar Sharpe:{' '}
                    <strong className="text-slate-300">
                      {Number(result.bar_sharpe_ratio ?? result.sharpe_bar).toFixed(2)}
                    </strong>
                  </span>
                )}
                <span>
                  Profit Factor: <strong className="text-slate-200">{Number(result.profit_factor ?? 0).toFixed(2)}</strong>
                </span>
              </div>
              <span className="text-[10px] text-slate-500">
                Next-bar open fills · Gap-through stops · No same-bar exit
              </span>
            </div>
          </div>

          {/* Hypothetical Daily Summary + Hypothetical Monthly Performance */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {/* Daily Summary */}
            <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827] p-4">
              <h4 className="mb-3 text-xs font-semibold text-slate-300">Hypothetical Daily Summary</h4>
              <div className="overflow-hidden rounded-lg border border-slate-800">
                <table className="w-full border-collapse text-center text-xs">
                  <thead className="bg-[#0b1120] text-[11px] font-semibold text-slate-400">
                    <tr>
                      <th className="px-3 py-2 text-left">Day</th>
                      <th className="px-3 py-2">Avg. Return %</th>
                      <th className="px-3 py-2">Max Profit %</th>
                      <th className="px-3 py-2">Max Loss %</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
                    {activeStats.dailySummary.map((d) => (
                      <tr key={d.day} className="hover:bg-slate-800/30">
                        <td className="px-3 py-2 text-left font-sans font-medium text-slate-300">{d.day}</td>
                        <td
                          className={`px-3 py-2 font-semibold ${
                            d.avg > 0 ? 'text-emerald-400' : d.avg < 0 ? 'text-red-400' : 'text-slate-400'
                          }`}
                        >
                          {d.avg >= 0 ? '+' : ''}
                          {d.avg.toFixed(2)}%
                        </td>
                        <td className="px-3 py-2 text-emerald-400">{d.maxProfit.toFixed(2)}%</td>
                        <td className="px-3 py-2 text-red-400">{d.maxLoss.toFixed(2)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Monthly Performance Heatmap */}
            <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827] p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h4 className="text-xs font-semibold text-slate-300">Hypothetical Monthly Performance</h4>
                {activeStats.availableYears.length > 1 ? (
                  <div className="flex items-center gap-1 font-mono text-[11px]">
                    <button
                      type="button"
                      onClick={() => setSelectedYear('ALL')}
                      className={`rounded px-2 py-0.5 font-semibold ${
                        selectedYear === 'ALL'
                          ? 'bg-sky-500/20 text-sky-300'
                          : 'text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      All
                    </button>
                    {activeStats.availableYears.map((yr) => (
                      <button
                        key={yr}
                        type="button"
                        onClick={() => setSelectedYear(yr)}
                        className={`rounded px-2 py-0.5 font-semibold ${
                          selectedYear === yr
                            ? 'bg-sky-500/20 text-sky-300'
                            : 'text-slate-400 hover:text-slate-200'
                        }`}
                      >
                        {yr}
                      </button>
                    ))}
                  </div>
                ) : (
                  <span className="font-mono text-xs font-semibold text-slate-400">
                    {activeStats.availableYears[0] || yearLabel}
                  </span>
                )}
              </div>
              <div className="grid grid-cols-6 gap-2.5">
                {displayedMonthly.map((m) => {
                  const hasData = m.count > 0;
                  const pos = m.retPct >= 0;
                  const bg = !hasData
                    ? 'border-slate-800 bg-[#0b1120] text-slate-600'
                    : pos
                      ? 'border-emerald-700/70 bg-emerald-600/25 text-emerald-300'
                      : 'border-red-800/70 bg-red-600/25 text-red-300';
                  return (
                    <div key={m.label} className="flex flex-col items-center gap-1">
                      <div
                        className={`flex h-12 w-full items-center justify-center rounded-lg border font-mono text-[11px] font-bold tabular-nums ${bg}`}
                        title={`${m.label} (${selectedYear === 'ALL' ? activeStats.availableYears.join(', ') || yearLabel : selectedYear}): ${m.count} trades, ₹${m.pnl.toFixed(2)}`}
                      >
                        {hasData ? `${pos ? '+' : ''}${m.retPct.toFixed(1)}%` : '—'}
                      </div>
                      <span className="text-[11px] text-slate-400">{m.label}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Chart / Trade Log Section */}
          <div className="space-y-3 rounded-xl border border-slate-800 bg-[#111827] p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="text-xs font-semibold text-slate-200">Chart / Trade Log ({chartSymbol})</h4>
                {result.indicator_columns && result.indicator_columns.length > 0 && (
                  <span className="font-mono text-[10px] text-slate-500">
                    Indicators: {result.indicator_columns.slice(0, 5).join(', ')}
                    {result.indicator_columns.length > 5 ? ` +${result.indicator_columns.length - 5}` : ''}
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => void handleDownloadOhlcvCsv('active')}
                  disabled={ohlcvExporting || !result.run_id}
                  className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-emerald-700/70 bg-emerald-950/30 px-3 text-xs font-semibold text-emerald-300 hover:border-emerald-400 disabled:opacity-40"
                >
                  <FileSpreadsheet className="h-3.5 w-3.5" />
                  <span>
                    {ohlcvExporting
                      ? 'Exporting...'
                      : selectedSymbol === 'COMBINED'
                        ? 'Download All OHLCV + Indicators CSV'
                        : `Download ${selectedSymbol} OHLCV + Indicators CSV`}
                  </span>
                </button>
                {selectedSymbol !== 'COMBINED' && (result.symbols?.length || 0) > 1 && (
                  <button
                    type="button"
                    onClick={() => void handleDownloadOhlcvCsv('all')}
                    disabled={ohlcvExporting || !result.run_id}
                    className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-semibold text-slate-300 hover:border-emerald-400 hover:text-emerald-300 disabled:opacity-40"
                  >
                    <Download className="h-3.5 w-3.5" />
                    <span>All Symbols CSV</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleDownloadCsv}
                  disabled={activeTrades.length === 0}
                  className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-40"
                >
                  <Download className="h-3.5 w-3.5" />
                  <span>Download Trade Log</span>
                </button>
              </div>
            </div>
            {ohlcvError && (
              <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-3 py-2 text-xs text-red-200">
                {ohlcvError}
              </div>
            )}

            <CandlestickChartPanel
              symbols={result.symbols || [chartSymbol]}
              activeSymbol={chartSymbol}
              onSelectSymbol={(sym) => setSelectedSymbol(sym)}
              exchange={result.deploy_spec?.exchange || 'NSE'}
              timeframe={result.timeframe || '1day'}
              fromDate={result.from_date}
              toDate={result.to_date}
              trades={activeTrades}
              onQuickOrder={onQuickOrder}
              height={320}
              compactHeader
            />

            {/* Trade Log Table */}
            <div className="overflow-x-auto rounded-lg border border-slate-800">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-3 py-2.5">Symbol</th>
                    <th className="px-3 py-2.5">Entry Date</th>
                    <th className="px-3 py-2.5">Exit Date</th>
                    <th className="px-3 py-2.5">Signal</th>
                    <th className="px-3 py-2.5 text-right">Qty</th>
                    <th className="px-3 py-2.5 text-right">Entry</th>
                    <th className="px-3 py-2.5 text-right">Exit</th>
                    <th className="px-3 py-2.5 text-right">Charges</th>
                    <th className="px-3 py-2.5 text-right">P&amp;L</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
                  {displayedTrades.length === 0 ? (
                    <tr>
                      <td colSpan={9} className="px-4 py-6 text-center font-sans text-slate-500">
                        No trades executed for this selection.
                      </td>
                    </tr>
                  ) : (
                    displayedTrades.map((tr, i) => {
                      const pnl = includeCharges ? tr.pnl : tr.gross_pnl;
                      const reason = (tr.exit_reason || 'EXIT').toUpperCase();
                      const tagColor = reason.includes('TP') || reason.includes('TARGET')
                        ? 'text-emerald-400'
                        : reason.includes('SL') || reason.includes('STOP')
                          ? 'text-red-400'
                          : 'text-sky-300';
                      return (
                        <tr key={`${tr.symbol}-${tr.entry_date}-${i}`} className="hover:bg-slate-800/40">
                          <td className="px-3 py-2 font-semibold text-slate-100">{tr.symbol || chartSymbol}</td>
                          <td className="px-3 py-2 text-slate-400">{tr.entry_date.slice(0, 16).replace('T', ' ')}</td>
                          <td className="px-3 py-2 text-slate-400">{tr.exit_date.slice(0, 16).replace('T', ' ')}</td>
                          <td className={`px-3 py-2 font-semibold ${tagColor}`}>{reason}</td>
                          <td className="px-3 py-2 text-right text-slate-300">{tr.qty}</td>
                          <td className="px-3 py-2 text-right text-slate-200">{tr.entry_price.toFixed(2)}</td>
                          <td className="px-3 py-2 text-right text-slate-200">{tr.exit_price.toFixed(2)}</td>
                          <td className="px-3 py-2 text-right text-slate-400">₹{tr.charges.toFixed(2)}</td>
                          <td
                            className={`px-3 py-2 text-right font-semibold ${
                              pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                            }`}
                          >
                            {pnl >= 0 ? '+' : ''}₹{pnl.toFixed(2)}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>

            {activeTrades.length > 15 && (
              <div className="text-right">
                <button
                  type="button"
                  onClick={() => setShowAllTrades((v) => !v)}
                  className="text-xs font-semibold text-sky-400 hover:text-sky-300"
                >
                  {showAllTrades ? 'Show Top 15 Trades' : `See All Transactions (${activeTrades.length})`}
                </button>
              </div>
            )}
          </div>

          {/* Interactive Portfolio Equity & Cumulative P&L Chart with Dates */}
          <PortfolioPnlChart
            title={`${selectedSymbol === 'COMBINED' ? 'Portfolio Cumulative P&L' : `${selectedSymbol} Cumulative P&L`} (${includeCharges ? 'Net' : 'Gross'})`}
            initialCapital={result.initial_capital}
            fromDate={result.from_date}
            toDate={result.to_date}
            includeCharges={includeCharges}
            isCombined={selectedSymbol === 'COMBINED'}
            equityCurve={result.equity_curve}
            tradeSeries={activeStats.cumSeries}
            totalPnl={activeStats.totalPnl}
          />
        </div>
      </div>

      {/* Deploy / Notification Based Trading Modal */}
      {deployOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="w-full max-w-md rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
              <div className="flex items-center gap-2">
                <Rocket className="h-4 w-4 text-sky-400" />
                <h3 className="text-sm font-semibold text-slate-100">Deploy Strategy Live</h3>
              </div>
              <button
                type="button"
                onClick={() => setDeployOpen(false)}
                className="text-slate-400 hover:text-slate-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-4 p-5 text-xs">
              <div>
                <label className="mb-1.5 block font-semibold text-slate-300">Deployment Name</label>
                <input
                  type="text"
                  value={deployName}
                  onChange={(e) => setDeployName(e.target.value)}
                  className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                />
              </div>

              <div>
                <label className="mb-1.5 block font-semibold text-slate-300">Execution Workflow</label>
                <div className="space-y-2">
                  {[
                    {
                      id: 'live_alert' as const,
                      title: 'Notification Based Trading (1-Click Alerts)',
                      desc: 'Generates actionable BUY/SELL alerts; you approve execution in 1 click.',
                    },
                    {
                      id: 'paper' as const,
                      title: 'Paper Trading (Virtual Portfolio)',
                      desc: 'Simulates real-time fills with Zerodha/Upstox real trading charges.',
                    },
                    {
                      id: 'live_auto' as const,
                      title: 'Gated Auto-Execute (Broker Live)',
                      desc: 'Routes orders automatically after passing Kill Switch, Risk Limits & 6-Gate Preflight.',
                    },
                  ].map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => setDeployExecMode(opt.id)}
                      className={`w-full rounded-lg border p-3 text-left transition-colors ${
                        deployExecMode === opt.id
                          ? 'border-sky-500 bg-sky-950/40'
                          : 'border-slate-800 bg-[#0b1120] hover:border-slate-700'
                      }`}
                    >
                      <div className="font-semibold text-slate-100">{opt.title}</div>
                      <div className="mt-0.5 text-[11px] text-slate-400">{opt.desc}</div>
                    </button>
                  ))}
                </div>
              </div>

              {/* Evaluation Mode, Exchange & Product */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="sm:col-span-1">
                  <label className="mb-1.5 block font-semibold text-slate-300">Exchange</label>
                  <select
                    value={deployExchange}
                    onChange={(e) => setDeployExchange(e.target.value)}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="NSE">NSE</option>
                    <option value="BSE">BSE</option>
                    <option value="NFO">NFO</option>
                    <option value="BFO">BFO</option>
                    <option value="MCX">MCX</option>
                  </select>
                </div>
                <div className="sm:col-span-1">
                  <label className="mb-1.5 block font-semibold text-slate-300">Product</label>
                  <select
                    value={deployProduct}
                    onChange={(e) => setDeployProduct(e.target.value)}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="MIS">MIS (Intraday)</option>
                    <option value="CNC">CNC (Delivery)</option>
                    <option value="NRML">NRML (F&amp;O/MCX)</option>
                  </select>
                </div>
                <div className="sm:col-span-1">
                  <label className="mb-1.5 block font-semibold text-slate-300">Signal Evaluation</label>
                  <select
                    value={deployEvalMode}
                    onChange={(e) => setDeployEvalMode(e.target.value as 'completed_bar' | 'intrabar')}
                    className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="completed_bar">Completed Candle Close (Recommended)</option>
                    <option value="intrabar">Intrabar Live Tick</option>
                  </select>
                </div>
              </div>

              {securityStatus && !securityStatus.live_trading_enabled && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-800/70 bg-amber-950/30 px-3 py-2 text-[11px] text-amber-200">
                  <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
                  <span>
                    <strong>Live Trading Locked</strong> (<code>ALGODESK_ENABLE_LIVE_TRADING=false</code>). Paper Trading and 1-Click Alerts are ready; enable the server env flag before Gated Auto-Execute.
                  </span>
                </div>
              )}

              {deployMsg && (
                <div
                  className={`rounded-lg border px-3 py-2 text-xs ${
                    deployMsg.type === 'ok'
                      ? 'border-emerald-800 bg-emerald-950/40 text-emerald-200'
                      : 'border-red-900 bg-red-950/40 text-red-200'
                  }`}
                >
                  {deployMsg.text}
                </div>
              )}

              <div className="flex justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => setDeployOpen(false)}
                  className="min-h-[36px] rounded-lg border border-slate-700 px-4 font-semibold text-slate-300"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void handleDeploySubmit()}
                  disabled={deployBusy}
                  className="min-h-[36px] rounded-lg bg-sky-500 px-5 font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
                >
                  {deployBusy ? 'Deploying...' : 'Take Live'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
