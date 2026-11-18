import { useCallback, useEffect, useState } from 'react';
import { Activity, AlertCircle, BarChart3, Check, Play, ShieldAlert, ShieldCheck, Sliders } from 'lucide-react';

interface ScreenerRow {
  symbol: string;
  exchange: string;
  bars: number;
  last_date: string;
  close: number;
  change_pct: number;
  ema20: number | null;
  ema50: number | null;
  rsi14: number | null;
  atr_pct: number | null;
  vol_ratio: number;
  contraction_ratio: number | null;
  dist_50d_high_pct: number | null;
  supertrend: string;
  signal: string;
  tags: string[];
  score: number;
  matched: boolean;
  api_calls: number;
}

interface ScreenerResultPayload {
  preset: string;
  preset_label: string;
  provider: string;
  exchange: string;
  timeframe: string;
  scanned_count: number;
  evaluated_count: number;
  matched_count: number;
  api_calls_made: number;
  existing_days_skipped: number;
  generated_at_ist: string;
  results: ScreenerRow[];
  errors: Array<{ symbol: string; error: string }>;
}

interface EquityCurvePoint {
  date: string;
  equity: number;
  drawdown_pct: number;
}

interface BacktestTrade {
  entry_date: string;
  exit_date: string;
  qty: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  return_pct: number;
  exit_reason: string;
}

interface BacktestResultPayload {
  symbol: string;
  exchange: string;
  strategy: string;
  strategy_label: string;
  timeframe: string;
  provider: string;
  from_date: string;
  to_date: string;
  bars_evaluated: number;
  api_calls_made: number;
  existing_days_skipped: number;
  initial_capital: number;
  final_equity: number;
  net_pnl: number;
  total_return_pct: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  equity_curve: EquityCurvePoint[];
  trades: BacktestTrade[];
}

interface RiskLimitsConfig {
  total_capital_inr: number;
  max_capital_per_trade_pct: number;
  max_daily_loss_inr: number;
  max_open_positions: number;
  max_gross_exposure_inr: number;
  auto_engage_kill_switch: boolean;
  updated_at_ist: string | null;
}

interface RiskEvaluationPayload {
  ok: boolean;
  trading_broker: string;
  broker_connected: boolean;
  broker_note: string;
  limits: RiskLimitsConfig;
  telemetry: {
    capital_base_inr: number;
    available_margin_inr: number;
    used_margin_inr: number;
    open_positions_count: number;
    open_orders_count: number;
    gross_exposure_inr: number;
    exposure_utilization_pct: number;
    daily_pnl_inr: number;
    daily_loss_inr: number;
    daily_loss_utilization_pct: number;
    max_single_position_pct: number;
  };
  breaches: Array<{ rule: string; severity: string; message: string }>;
  status: 'HEALTHY' | 'STANDBY' | 'BREACHED';
  kill_switch_auto_triggered: boolean;
  evaluated_at_ist: string;
}

interface AsyncJobStatus {
  job_id: string;
  job_type: string;
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  progress_pct: number;
  progress_message: string;
  result?: unknown;
  error?: string | null;
}

interface AnalyticsConsoleProps {
  activeDataConnection: string;
  activeTradingBroker: string;
  onRiskStateChange?: () => void;
}

const DEFAULT_WATCHLIST = 'RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK, SBIN, BHARTIARTL, ITC, LT, AXISBANK';

function formatInr(val: number): string {
  return `₹${val.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function EquityCurveSvg({ curve, initialCapital }: { curve: EquityCurvePoint[]; initialCapital: number }) {
  if (!curve || curve.length < 2) {
    return (
      <div className="flex h-[200px] items-center justify-center rounded-lg border border-slate-800 bg-[#0b1120] text-xs text-slate-500">
        Not enough curve points to plot equity chart.
      </div>
    );
  }

  const width = 820;
  const height = 210;
  const padX = 12;
  const padY = 18;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;

  const equities = curve.map((p) => p.equity);
  const minEq = Math.min(initialCapital, ...equities);
  const maxEq = Math.max(initialCapital, ...equities);
  const span = maxEq - minEq || 1;

  const xAt = (idx: number) => padX + (idx / Math.max(1, curve.length - 1)) * innerW;
  const yAt = (eq: number) => padY + innerH - ((eq - minEq) / span) * innerH;

  const pointsAttr = curve.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p.equity).toFixed(1)}`).join(' ');
  const areaAttr = `${padX},${padY + innerH} ${pointsAttr} ${(padX + innerW).toFixed(1)},${padY + innerH}`;
  const baselineY = yAt(initialCapital);
  const finalEq = curve[curve.length - 1].equity;
  const isGain = finalEq >= initialCapital;
  const strokeColor = isGain ? '#34d399' : '#f87171';
  const fillColor = isGain ? 'rgba(52, 211, 153, 0.14)' : 'rgba(248, 113, 113, 0.14)';

  return (
    <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 font-mono text-xs text-slate-400">
        <span>
          Equity Curve ({curve[0].date} → {curve[curve.length - 1].date})
        </span>
        <span>
          Low: {formatInr(minEq)} · Baseline: {formatInr(initialCapital)} · Peak: {formatInr(maxEq)}
        </span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="h-[190px] w-full overflow-visible" role="img" aria-label="Backtest Equity Curve">
        <line
          x1={padX}
          y1={baselineY}
          x2={width - padX}
          y2={baselineY}
          stroke="#334155"
          strokeWidth="1"
          strokeDasharray="4 4"
        />
        <polygon points={areaAttr} fill={fillColor} />
        <polyline fill="none" stroke={strokeColor} strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" points={pointsAttr} />
      </svg>
    </div>
  );
}

export function AnalyticsConsole({
  activeDataConnection,
  activeTradingBroker,
  onRiskStateChange,
}: AnalyticsConsoleProps) {
  const [activeTab, setActiveTab] = useState<'screener' | 'backtest' | 'risk'>('screener');

  // Screener state
  const [scSymbols, setScSymbols] = useState<string>(DEFAULT_WATCHLIST);
  const [scExchange, setScExchange] = useState<string>('NSE');
  const [scTimeframe, setScTimeframe] = useState<string>('1day');
  const [scLookback, setScLookback] = useState<number>(120);
  const [scPreset, setScPreset] = useState<string>('all');
  const [scProvider, setScProvider] = useState<string>('auto');
  const [scJob, setScJob] = useState<AsyncJobStatus | null>(null);
  const [scBusy, setScBusy] = useState<boolean>(false);
  const [scError, setScError] = useState<string>('');
  const [scResult, setScResult] = useState<ScreenerResultPayload | null>(null);

  // Backtest state
  const todayIso = new Date().toISOString().slice(0, 10);
  const yearAgoIso = new Date(Date.now() - 365 * 86400000).toISOString().slice(0, 10);
  const [btSymbol, setBtSymbol] = useState<string>('RELIANCE');
  const [btExchange, setBtExchange] = useState<string>('NSE');
  const [btStrategy, setBtStrategy] = useState<string>('ema_crossover');
  const [btTimeframe, setBtTimeframe] = useState<string>('1day');
  const [btFrom, setBtFrom] = useState<string>(yearAgoIso);
  const [btTo, setBtTo] = useState<string>(todayIso);
  const [btCapital, setBtCapital] = useState<number>(500000);
  const [btSl, setBtSl] = useState<number>(5.0);
  const [btTp, setBtTp] = useState<number>(12.0);
  const [btProvider, setBtProvider] = useState<string>('auto');
  const [btJob, setBtJob] = useState<AsyncJobStatus | null>(null);
  const [btBusy, setBtBusy] = useState<boolean>(false);
  const [btError, setBtError] = useState<string>('');
  const [btResult, setBtResult] = useState<BacktestResultPayload | null>(null);

  // Risk Console state
  const [riskData, setRiskData] = useState<RiskEvaluationPayload | null>(null);
  const [riskBusy, setRiskBusy] = useState<boolean>(false);
  const [riskFeedback, setRiskFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [limTotalCap, setLimTotalCap] = useState<string>('500000');
  const [limPerTradePct, setLimPerTradePct] = useState<string>('15');
  const [limDailyLoss, setLimDailyLoss] = useState<string>('15000');
  const [limMaxPos, setLimMaxPos] = useState<string>('5');
  const [limMaxExp, setLimMaxExp] = useState<string>('400000');
  const [limAutoKill, setLimAutoKill] = useState<boolean>(true);

  const loadRiskStatus = useCallback(async () => {
    setRiskBusy(true);
    try {
      const res = await fetch('/api/analytics/risk');
      if (!res.ok) return;
      const body = (await res.json()) as RiskEvaluationPayload;
      setRiskData(body);
      if (body.limits) {
        setLimTotalCap(String(body.limits.total_capital_inr));
        setLimPerTradePct(String(body.limits.max_capital_per_trade_pct));
        setLimDailyLoss(String(body.limits.max_daily_loss_inr));
        setLimMaxPos(String(body.limits.max_open_positions));
        setLimMaxExp(String(body.limits.max_gross_exposure_inr));
        setLimAutoKill(Boolean(body.limits.auto_engage_kill_switch));
      }
    } catch {
      // keep silent until manual retry
    } finally {
      setRiskBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadRiskStatus();
  }, [loadRiskStatus, activeTradingBroker]);

  async function pollJobUntilDone(
    jobId: string,
    onProgress: (job: AsyncJobStatus) => void
  ): Promise<AsyncJobStatus> {
    for (let attempt = 0; attempt < 180; attempt += 1) {
      const res = await fetch(`/api/analytics/jobs/${encodeURIComponent(jobId)}`);
      const body = (await res.json().catch(() => ({}))) as { job?: AsyncJobStatus; detail?: string };
      if (!res.ok || !body.job) {
        throw new Error(body.detail || 'Could not query background job status.');
      }
      onProgress(body.job);
      if (body.job.status === 'COMPLETED' || body.job.status === 'FAILED') {
        return body.job;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 450));
    }
    throw new Error('Background analytics task timed out while polling.');
  }

  async function handleRunScreener() {
    const syms = scSymbols
      .split(/[,\s]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    setScBusy(true);
    setScError('');
    try {
      const res = await fetch('/api/analytics/screener', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbols: syms.length > 0 ? syms : null,
          exchange: scExchange,
          timeframe: scTimeframe,
          lookback_days: Number(scLookback) || 120,
          preset: scPreset,
          provider: scProvider === 'auto' ? null : scProvider,
          async_job: true,
        }),
      });
      const body = (await res.json().catch(() => ({}))) as {
        job?: AsyncJobStatus;
        detail?: string;
      };
      if (!res.ok || !body.job) {
        throw new Error(body.detail || 'Could not start screener scan.');
      }
      setScJob(body.job);
      const doneJob = await pollJobUntilDone(body.job.job_id, (j) => setScJob(j));
      if (doneJob.status === 'FAILED') {
        throw new Error(doneJob.error || doneJob.progress_message || 'Screener scan failed.');
      }
      setScResult(doneJob.result as ScreenerResultPayload);
    } catch (err) {
      setScError(err instanceof Error ? err.message : 'Screener failed');
    } finally {
      setScBusy(false);
    }
  }

  async function handleRunBacktest(overrideSym?: string, overrideExch?: string) {
    const sym = (overrideSym ?? btSymbol).trim().toUpperCase();
    const exch = overrideExch ?? btExchange;
    if (!sym) {
      setBtError('Enter a trading symbol to backtest.');
      return;
    }
    setBtBusy(true);
    setBtError('');
    try {
      const res = await fetch('/api/analytics/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: sym,
          exchange: exch,
          strategy: btStrategy,
          timeframe: btTimeframe,
          from_date: btFrom,
          to_date: btTo,
          initial_capital: Number(btCapital) || 500000,
          stop_loss_pct: Number(btSl) || 0,
          take_profit_pct: Number(btTp) || 0,
          provider: btProvider === 'auto' ? null : btProvider,
          async_job: true,
        }),
      });
      const body = (await res.json().catch(() => ({}))) as {
        job?: AsyncJobStatus;
        detail?: string;
      };
      if (!res.ok || !body.job) {
        throw new Error(body.detail || 'Could not start backtest job.');
      }
      setBtJob(body.job);
      const doneJob = await pollJobUntilDone(body.job.job_id, (j) => setBtJob(j));
      if (doneJob.status === 'FAILED') {
        throw new Error(doneJob.error || doneJob.progress_message || 'Backtest failed.');
      }
      setBtResult(doneJob.result as BacktestResultPayload);
    } catch (err) {
      setBtError(err instanceof Error ? err.message : 'Backtest failed');
    } finally {
      setBtBusy(false);
    }
  }

  async function handleSaveRiskLimits() {
    setRiskBusy(true);
    setRiskFeedback(null);
    try {
      const res = await fetch('/api/analytics/risk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          total_capital_inr: Number(limTotalCap),
          max_capital_per_trade_pct: Number(limPerTradePct),
          max_daily_loss_inr: Number(limDailyLoss),
          max_open_positions: Number(limMaxPos),
          max_gross_exposure_inr: Number(limMaxExp),
          auto_engage_kill_switch: limAutoKill,
        }),
      });
      const body = (await res.json().catch(() => ({}))) as RiskEvaluationPayload & { detail?: string };
      if (!res.ok) {
        throw new Error(body.detail || 'Failed to update risk limits.');
      }
      setRiskData(body);
      setRiskFeedback({
        type: body.breaches.length > 0 ? 'err' : 'ok',
        text:
          body.breaches.length > 0
            ? `Risk limits saved (${body.breaches.length} active breach detected).`
            : 'Risk limits saved and verified against active broker exposure.',
      });
      if (body.kill_switch_auto_triggered && onRiskStateChange) {
        onRiskStateChange();
      }
    } catch (err) {
      setRiskFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Failed to update risk limits',
      });
    } finally {
      setRiskBusy(false);
    }
  }

  return (
    <section id="analytics-console" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-100">
            Phase 05 — Screener, Backtester &amp; Portfolio Risk Console
          </h2>
          <p className="text-sm text-slate-400">
            Pure O(N) asynchronous screener and strategy backtester routed via <span className="font-mono text-slate-200">{activeDataConnection.toUpperCase()}</span> with gap-only SQLite caching, plus live <span className="font-mono text-slate-200">{activeTradingBroker.toUpperCase()}</span> exposure limits.
          </p>
        </div>

        <div className="inline-flex rounded-lg border border-slate-800 bg-[#111827] p-1">
          <button
            type="button"
            onClick={() => setActiveTab('screener')}
            className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              activeTab === 'screener' ? 'bg-sky-400 text-sky-950' : 'text-slate-300 hover:text-slate-100'
            }`}
          >
            <Activity className="h-3.5 w-3.5" />
            <span>Market Screener</span>
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('backtest')}
            className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              activeTab === 'backtest' ? 'bg-sky-400 text-sky-950' : 'text-slate-300 hover:text-slate-100'
            }`}
          >
            <BarChart3 className="h-3.5 w-3.5" />
            <span>Strategy Backtester</span>
          </button>
          <button
            type="button"
            onClick={() => {
              setActiveTab('risk');
              void loadRiskStatus();
            }}
            className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              activeTab === 'risk' ? 'bg-sky-400 text-sky-950' : 'text-slate-300 hover:text-slate-100'
            }`}
          >
            <Sliders className="h-3.5 w-3.5" />
            <span>Risk Limits</span>
          </button>
        </div>
      </div>

      {/* TAB 1: MARKET SCREENER */}
      {activeTab === 'screener' && (
        <div className="space-y-5 rounded-xl border border-slate-800 bg-[#111827] p-6">
          <div className="grid grid-cols-1 items-end gap-4 md:grid-cols-[1.6fr_0.75fr_0.8fr_0.75fr_1fr_auto]">
            <div>
              <label htmlFor="sc-symbols" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Watchlist Symbols (comma-separated)
              </label>
              <input
                id="sc-symbols"
                type="text"
                value={scSymbols}
                onChange={(e) => setScSymbols(e.target.value.toUpperCase())}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="sc-preset" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Rule Preset
              </label>
              <select
                id="sc-preset"
                value={scPreset}
                onChange={(e) => setScPreset(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="all">All Metrics</option>
                <option value="momentum_breakout">Momentum Breakout</option>
                <option value="vcp_contraction">VCP Contraction</option>
                <option value="oversold_rebound">Oversold RSI</option>
                <option value="trend_following">Trend Following</option>
              </select>
            </div>

            <div>
              <label htmlFor="sc-tf" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Timeframe
              </label>
              <select
                id="sc-tf"
                value={scTimeframe}
                onChange={(e) => setScTimeframe(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="1day">1 Day</option>
                <option value="30minute">30 Min</option>
              </select>
            </div>

            <div>
              <label htmlFor="sc-lookback" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Lookback (Days)
              </label>
              <input
                id="sc-lookback"
                type="number"
                min={30}
                max={730}
                value={scLookback}
                onChange={(e) => setScLookback(Number(e.target.value))}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="sc-provider" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Data Source
              </label>
              <select
                id="sc-provider"
                value={scProvider}
                onChange={(e) => setScProvider(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="auto">Assigned ({activeDataConnection})</option>
                <option value="yfinance">Yahoo Finance</option>
                <option value="upstox">Upstox</option>
                <option value="zerodha">Zerodha Kite</option>
              </select>
            </div>

            <button
              type="button"
              onClick={() => void handleRunScreener()}
              disabled={scBusy}
              className="inline-flex min-h-[40px] shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-xs font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              <span>{scBusy ? 'Scanning...' : 'Run Async Scan'}</span>
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
            <span>Exchange:</span>
            {['NSE', 'BSE'].map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => setScExchange(ex)}
                className={`rounded-md border px-2.5 py-1 font-mono text-[11px] ${
                  scExchange === ex
                    ? 'border-sky-400 bg-sky-400/15 text-sky-300'
                    : 'border-slate-700 bg-[#0b1120] text-slate-400'
                }`}
              >
                {ex}
              </button>
            ))}
            {scJob && (
              <span className="ml-auto font-mono text-slate-300">
                Job {scJob.job_id}: {scJob.status} ({scJob.progress_pct}%) — {scJob.progress_message}
              </span>
            )}
          </div>

          {scError && (
            <div className="flex items-center gap-2 text-xs text-red-400">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{scError}</span>
            </div>
          )}

          {scResult && (
            <div className="space-y-3 border-t border-slate-800 pt-4">
              <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs text-slate-400">
                <span>
                  Matched {scResult.matched_count} of {scResult.evaluated_count} evaluated symbols ({scResult.preset_label})
                </span>
                <span>
                  Source: {scResult.provider.toUpperCase()} · API Calls: {scResult.api_calls_made} · Cached Days Skipped: {scResult.existing_days_skipped}
                </span>
              </div>

              {scResult.results.length === 0 ? (
                <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-4 text-xs text-slate-400">
                  No symbols matched "{scResult.preset_label}". Try "All Metrics" to inspect live technical readings across the watchlist.
                </div>
              ) : (
                <div className="max-h-[340px] overflow-auto rounded-lg border border-slate-800">
                  <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                    <thead className="sticky top-0 bg-[#0b1120] text-slate-400">
                      <tr className="border-b border-slate-800">
                        <th className="px-3 py-2.5 font-semibold">Symbol</th>
                        <th className="px-3 py-2.5 text-right font-semibold">Close</th>
                        <th className="px-3 py-2.5 text-right font-semibold">Chg%</th>
                        <th className="px-3 py-2.5 text-right font-semibold">EMA20</th>
                        <th className="px-3 py-2.5 text-right font-semibold">EMA50</th>
                        <th className="px-3 py-2.5 text-right font-semibold">RSI(14)</th>
                        <th className="px-3 py-2.5 text-right font-semibold">ATR%</th>
                        <th className="px-3 py-2.5 text-right font-semibold">Vol Ratio</th>
                        <th className="px-3 py-2.5 font-semibold">Signal</th>
                        <th className="px-3 py-2.5 text-right font-semibold">Action</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/80 bg-[#111827] text-slate-200">
                      {scResult.results.map((r) => (
                        <tr key={`${r.exchange}-${r.symbol}`} className="hover:bg-slate-900/50">
                          <td className="px-3 py-2 font-semibold text-slate-100">
                            {r.symbol}
                            <span className="ml-1.5 text-[10px] font-normal text-slate-500">{r.last_date}</span>
                          </td>
                          <td className="px-3 py-2 text-right">{r.close.toFixed(2)}</td>
                          <td className={`px-3 py-2 text-right ${r.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {r.change_pct >= 0 ? '+' : ''}
                            {r.change_pct.toFixed(2)}%
                          </td>
                          <td className="px-3 py-2 text-right text-slate-300">{r.ema20 !== null ? r.ema20.toFixed(2) : '—'}</td>
                          <td className="px-3 py-2 text-right text-slate-400">{r.ema50 !== null ? r.ema50.toFixed(2) : '—'}</td>
                          <td className="px-3 py-2 text-right">{r.rsi14 !== null ? r.rsi14.toFixed(1) : '—'}</td>
                          <td className="px-3 py-2 text-right text-slate-400">{r.atr_pct !== null ? `${r.atr_pct.toFixed(2)}%` : '—'}</td>
                          <td className="px-3 py-2 text-right">{r.vol_ratio.toFixed(2)}x</td>
                          <td className="px-3 py-2">
                            <span className={r.score >= 30 ? 'font-semibold text-sky-400' : 'text-slate-400'}>
                              {r.signal}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-right">
                            <button
                              type="button"
                              onClick={() => {
                                setBtSymbol(r.symbol);
                                setBtExchange(r.exchange);
                                setActiveTab('backtest');
                                void handleRunBacktest(r.symbol, r.exchange);
                              }}
                              className="rounded border border-slate-700 bg-[#0b1120] px-2.5 py-1 font-sans text-[11px] font-semibold text-sky-300 hover:border-sky-400"
                            >
                              Backtest →
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {scResult.errors && scResult.errors.length > 0 && (
                <div className="text-xs text-amber-400">
                  Skipped {scResult.errors.length} symbol(s): {scResult.errors.map((e) => `${e.symbol} (${e.error})`).join(' · ')}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* TAB 2: O(N) STRATEGY BACKTESTER */}
      {activeTab === 'backtest' && (
        <div className="space-y-5 rounded-xl border border-slate-800 bg-[#111827] p-6">
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-2 lg:grid-cols-[1fr_0.7fr_1.15fr_0.75fr_0.9fr_0.9fr_auto]">
            <div>
              <label htmlFor="bt-symbol" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Symbol
              </label>
              <input
                id="bt-symbol"
                type="text"
                value={btSymbol}
                onChange={(e) => setBtSymbol(e.target.value.toUpperCase())}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="bt-exch" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Exchange
              </label>
              <select
                id="bt-exch"
                value={btExchange}
                onChange={(e) => setBtExchange(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="NSE">NSE</option>
                <option value="BSE">BSE</option>
                <option value="NFO">NFO</option>
              </select>
            </div>

            <div>
              <label htmlFor="bt-strategy" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Strategy
              </label>
              <select
                id="bt-strategy"
                value={btStrategy}
                onChange={(e) => setBtStrategy(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="ema_crossover">EMA 20 / 50 Crossover</option>
                <option value="rsi_mean_reversion">Wilder RSI (14) Mean Reversion</option>
                <option value="vcp_breakout">VCP Range Breakout</option>
                <option value="supertrend_atr">Supertrend (10, ATR 3.0x)</option>
              </select>
            </div>

            <div>
              <label htmlFor="bt-tf" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Timeframe
              </label>
              <select
                id="bt-tf"
                value={btTimeframe}
                onChange={(e) => setBtTimeframe(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="1day">1 Day</option>
                <option value="30minute">30 Min</option>
              </select>
            </div>

            <div>
              <label htmlFor="bt-from" className="mb-1.5 block text-xs font-semibold text-slate-300">
                From Date
              </label>
              <input
                id="bt-from"
                type="date"
                value={btFrom}
                onChange={(e) => setBtFrom(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="bt-to" className="mb-1.5 block text-xs font-semibold text-slate-300">
                To Date
              </label>
              <input
                id="bt-to"
                type="date"
                value={btTo}
                onChange={(e) => setBtTo(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <button
              type="button"
              onClick={() => void handleRunBacktest()}
              disabled={btBusy}
              className="inline-flex min-h-[40px] shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-xs font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              <span>{btBusy ? 'Simulating...' : 'Run Backtest'}</span>
            </button>
          </div>

          <div className="grid grid-cols-2 items-end gap-3 sm:grid-cols-4">
            <div>
              <label htmlFor="bt-cap" className="mb-1 block text-xs text-slate-400">
                Initial Capital (₹)
              </label>
              <input
                id="bt-cap"
                type="number"
                value={btCapital}
                onChange={(e) => setBtCapital(Number(e.target.value))}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
            <div>
              <label htmlFor="bt-sl" className="mb-1 block text-xs text-slate-400">
                Stop Loss % (0 = off)
              </label>
              <input
                id="bt-sl"
                type="number"
                step="0.5"
                value={btSl}
                onChange={(e) => setBtSl(Number(e.target.value))}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
            <div>
              <label htmlFor="bt-tp" className="mb-1 block text-xs text-slate-400">
                Take Profit % (0 = off)
              </label>
              <input
                id="bt-tp"
                type="number"
                step="0.5"
                value={btTp}
                onChange={(e) => setBtTp(Number(e.target.value))}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
            <div>
              <label htmlFor="bt-provider" className="mb-1 block text-xs text-slate-400">
                Data Source
              </label>
              <select
                id="bt-provider"
                value={btProvider}
                onChange={(e) => setBtProvider(e.target.value)}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="auto">Assigned ({activeDataConnection})</option>
                <option value="yfinance">Yahoo Finance</option>
                <option value="upstox">Upstox</option>
                <option value="zerodha">Zerodha Kite</option>
              </select>
            </div>
          </div>

          {btJob && (
            <div className="font-mono text-xs text-slate-300">
              Job {btJob.job_id}: {btJob.status} ({btJob.progress_pct}%) — {btJob.progress_message}
            </div>
          )}

          {btError && (
            <div className="flex items-center gap-2 text-xs text-red-400">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{btError}</span>
            </div>
          )}

          {btResult && (
            <div className="space-y-4 border-t border-slate-800 pt-4">
              <div className="grid grid-cols-2 gap-3 rounded-lg border border-slate-800 bg-[#0b1120] p-4 font-mono text-xs tabular-nums sm:grid-cols-4 lg:grid-cols-8">
                <div>
                  <div className="text-slate-500">Net P&amp;L</div>
                  <div className={`mt-1 text-sm font-semibold ${btResult.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {btResult.net_pnl >= 0 ? '+' : ''}
                    {formatInr(btResult.net_pnl)}
                  </div>
                </div>
                <div>
                  <div className="text-slate-500">Return</div>
                  <div className={`mt-1 text-sm font-semibold ${btResult.total_return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {btResult.total_return_pct >= 0 ? '+' : ''}
                    {btResult.total_return_pct}%
                  </div>
                </div>
                <div>
                  <div className="text-slate-500">CAGR</div>
                  <div className="mt-1 text-sm font-semibold text-slate-100">{btResult.cagr_pct}%</div>
                </div>
                <div>
                  <div className="text-slate-500">Max DD</div>
                  <div className="mt-1 text-sm font-semibold text-amber-400">-{btResult.max_drawdown_pct}%</div>
                </div>
                <div>
                  <div className="text-slate-500">Sharpe</div>
                  <div className="mt-1 text-sm font-semibold text-slate-100">{btResult.sharpe_ratio}</div>
                </div>
                <div>
                  <div className="text-slate-500">Trades</div>
                  <div className="mt-1 text-sm font-semibold text-slate-100">{btResult.total_trades}</div>
                </div>
                <div>
                  <div className="text-slate-500">Win Rate</div>
                  <div className="mt-1 text-sm font-semibold text-slate-100">{btResult.win_rate_pct}%</div>
                </div>
                <div>
                  <div className="text-slate-500">Profit Factor</div>
                  <div className="mt-1 text-sm font-semibold text-sky-400">{btResult.profit_factor}</div>
                </div>
              </div>

              <EquityCurveSvg curve={btResult.equity_curve} initialCapital={btResult.initial_capital} />

              <div className="max-h-[240px] overflow-auto rounded-lg border border-slate-800">
                <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                  <thead className="sticky top-0 bg-[#0b1120] text-slate-400">
                    <tr className="border-b border-slate-800">
                      <th className="px-3 py-2 font-semibold">Entry Date</th>
                      <th className="px-3 py-2 font-semibold">Exit Date</th>
                      <th className="px-3 py-2 text-right font-semibold">Qty</th>
                      <th className="px-3 py-2 text-right font-semibold">Entry ₹</th>
                      <th className="px-3 py-2 text-right font-semibold">Exit ₹</th>
                      <th className="px-3 py-2 text-right font-semibold">P&amp;L ₹</th>
                      <th className="px-3 py-2 text-right font-semibold">Return</th>
                      <th className="px-3 py-2 font-semibold">Exit Reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/80 bg-[#111827] text-slate-200">
                    {btResult.trades.length === 0 ? (
                      <tr>
                        <td colSpan={8} className="px-3 py-4 text-center font-sans text-slate-500">
                          No entries triggered during this window ({btResult.bars_evaluated} bars evaluated). Try another strategy or wider date range.
                        </td>
                      </tr>
                    ) : (
                      btResult.trades.map((t, i) => (
                        <tr key={`${t.entry_date}-${i}`}>
                          <td className="px-3 py-2 text-slate-400">{t.entry_date}</td>
                          <td className="px-3 py-2 text-slate-400">{t.exit_date}</td>
                          <td className="px-3 py-2 text-right">{t.qty}</td>
                          <td className="px-3 py-2 text-right">{t.entry_price.toFixed(2)}</td>
                          <td className="px-3 py-2 text-right">{t.exit_price.toFixed(2)}</td>
                          <td className={`px-3 py-2 text-right font-semibold ${t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {t.pnl >= 0 ? '+' : ''}
                            {t.pnl.toFixed(2)}
                          </td>
                          <td className={`px-3 py-2 text-right ${t.return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {t.return_pct >= 0 ? '+' : ''}
                            {t.return_pct.toFixed(2)}%
                          </td>
                          <td className="px-3 py-2 text-slate-400">{t.exit_reason}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* TAB 3: PORTFOLIO RISK LIMITS & EXPOSURE GUARD */}
      {activeTab === 'risk' && (
        <div className="space-y-5 rounded-xl border border-slate-800 bg-[#111827] p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-100">
                {riskData?.status === 'BREACHED' ? (
                  <ShieldAlert className="h-4 w-4 text-red-400" />
                ) : (
                  <ShieldCheck className="h-4 w-4 text-emerald-400" />
                )}
                <span>Live Exposure &amp; Daily Loss Guard ({activeTradingBroker.toUpperCase()})</span>
              </div>
              <p className="text-xs text-slate-400">
                {riskData?.broker_note || 'Evaluating configured portfolio risk thresholds.'}
              </p>
            </div>
            <button
              type="button"
              onClick={() => void loadRiskStatus()}
              disabled={riskBusy}
              className="min-h-[36px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-1.5 text-xs font-semibold text-slate-200 hover:border-sky-400 disabled:opacity-50"
            >
              {riskBusy ? 'Checking...' : 'Evaluate Live Exposure'}
            </button>
          </div>

          {riskData && riskData.breaches.length > 0 && (
            <div className="space-y-1 rounded-lg border border-red-900/70 bg-red-950/40 p-3.5 text-xs text-red-200">
              {riskData.breaches.map((b) => (
                <div key={b.rule} className="font-semibold">
                  [{b.severity}] {b.message}
                </div>
              ))}
            </div>
          )}

          {riskData && (
            <div className="grid grid-cols-2 gap-3 rounded-lg border border-slate-800 bg-[#0b1120] p-4 font-mono text-xs tabular-nums sm:grid-cols-4">
              <div>
                <div className="text-slate-500">Capital Base</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">
                  {formatInr(riskData.telemetry.capital_base_inr)}
                </div>
              </div>
              <div>
                <div className="text-slate-500">Gross Exposure</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">
                  {formatInr(riskData.telemetry.gross_exposure_inr)} ({riskData.telemetry.exposure_utilization_pct}%)
                </div>
              </div>
              <div>
                <div className="text-slate-500">Daily P&amp;L</div>
                <div
                  className={`mt-1 text-sm font-semibold ${
                    riskData.telemetry.daily_pnl_inr >= 0 ? 'text-emerald-400' : 'text-red-400'
                  }`}
                >
                  {riskData.telemetry.daily_pnl_inr >= 0 ? '+' : ''}
                  {formatInr(riskData.telemetry.daily_pnl_inr)}
                </div>
              </div>
              <div>
                <div className="text-slate-500">Open Positions</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">
                  {riskData.telemetry.open_positions_count} / {riskData.limits.max_open_positions}
                </div>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 border-t border-slate-800 pt-4 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label htmlFor="rk-cap" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Total Risk Capital (₹)
              </label>
              <input
                id="rk-cap"
                type="number"
                value={limTotalCap}
                onChange={(e) => setLimTotalCap(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="rk-pct" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Max Capital Per Trade (%)
              </label>
              <input
                id="rk-pct"
                type="number"
                step="0.5"
                value={limPerTradePct}
                onChange={(e) => setLimPerTradePct(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="rk-loss" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Max Daily Loss Cap (₹)
              </label>
              <input
                id="rk-loss"
                type="number"
                value={limDailyLoss}
                onChange={(e) => setLimDailyLoss(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="rk-pos" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Max Open Positions
              </label>
              <input
                id="rk-pos"
                type="number"
                min={1}
                value={limMaxPos}
                onChange={(e) => setLimMaxPos(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="rk-exp" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Max Gross Exposure (₹)
              </label>
              <input
                id="rk-exp"
                type="number"
                value={limMaxExp}
                onChange={(e) => setLimMaxExp(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div className="flex items-end">
              <label className="flex min-h-[40px] w-full cursor-pointer items-center gap-2.5 rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-200">
                <input
                  type="checkbox"
                  checked={limAutoKill}
                  onChange={(e) => setLimAutoKill(e.target.checked)}
                  className="h-4 w-4 accent-sky-400"
                />
                <span>Auto-engage Kill Switch on daily loss breach</span>
              </label>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            {riskFeedback ? (
              <div
                className={`flex items-center gap-2 text-xs ${
                  riskFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                }`}
              >
                {riskFeedback.type === 'ok' ? <Check className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                <span>{riskFeedback.text}</span>
              </div>
            ) : (
              <span className="text-xs text-slate-500">
                Persisted locally to data/risk_limits.json and audited to SHA-256 chain.
              </span>
            )}
            <button
              type="button"
              onClick={() => void handleSaveRiskLimits()}
              disabled={riskBusy}
              className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-xs font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
            >
              {riskBusy ? 'Saving...' : 'Save Risk Limits & Evaluate'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
