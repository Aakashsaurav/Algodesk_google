import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertCircle,
  BarChart3,
  Check,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  XCircle,
} from 'lucide-react';

interface ParamSpec {
  name: string;
  label: string;
  type: 'int' | 'float' | 'bool' | 'str';
  default: number | boolean | string;
  min_val?: number | null;
  max_val?: number | null;
  step?: number | null;
  description?: string;
}

interface ColumnSpec {
  key: string;
  label: string;
  fmt: 'price' | 'number' | 'pct' | 'signed_pct' | 'ratio' | 'badge' | 'text';
  decimals: number;
  align: 'left' | 'right';
}

interface TimeframeMeta {
  id: string;
  label: string;
  base: string;
  resampled: boolean;
}

interface ScreenMeta {
  id: string;
  name: string;
  description: string;
  category: string;
  min_bars: number;
  required_bars?: number;
  params: ParamSpec[];
  columns?: ColumnSpec[];
}

interface StrategyMeta {
  id: string;
  name: string;
  description: string;
  params: ParamSpec[];
}

interface UniverseMeta {
  id: string;
  label: string;
  count: number;
}

interface AnalyticsMetaPayload {
  ok: boolean;
  screens: ScreenMeta[];
  strategy_specs: StrategyMeta[];
  universes: UniverseMeta[];
  timeframes?: TimeframeMeta[];
  indicator_backends: Record<string, boolean>;
  storage: {
    configured_mode: string;
    active_mode: string;
    parquet_available: boolean;
    parquet_dir: string;
    parquet_files: number;
  };
  file_errors: {
    screens: Record<string, string>;
    strategies: Record<string, string>;
  };
  default_watchlist: string[];
}

interface ScreenerRow {
  symbol: string;
  exchange: string;
  bars: number;
  last_date: string;
  close: number;
  change_pct: number;
  signal: string;
  tags: string[];
  score: number;
  matched: boolean;
  api_calls: number;
  [key: string]: unknown;
}

interface ScreenerResultPayload {
  screen_id: string;
  screen_name?: string;
  preset: string;
  preset_label: string;
  columns?: ColumnSpec[];
  universe: string;
  universe_label: string;
  as_of_date: string;
  from_date?: string;
  is_historical_as_of?: boolean;
  provider: string;
  exchange: string;
  timeframe: string;
  base_timeframe?: string;
  resampled?: boolean;
  required_bars?: number;
  lookback_days?: number;
  storage_backend?: string;
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
  storage_backend?: string;
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
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
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

const FALLBACK_SCREENS: ScreenMeta[] = [
  { id: 'all_metrics', name: 'All Watchlist Metrics', description: '', category: '', min_bars: 15, params: [] },
  { id: 'momentum_breakout', name: 'Momentum Breakout', description: '', category: '', min_bars: 50, params: [] },
  { id: 'vcp_contraction', name: 'VCP Contraction', description: '', category: '', min_bars: 30, params: [] },
  { id: 'oversold_rebound', name: 'Oversold Mean Reversion', description: '', category: '', min_bars: 20, params: [] },
  { id: 'trend_following', name: 'Uptrend Alignment (EMA + ADX)', description: '', category: '', min_bars: 50, params: [] },
];

const FALLBACK_STRATEGIES: StrategyMeta[] = [
  { id: 'ema_crossover', name: 'EMA 20 / 50 Crossover', description: '', params: [] },
  { id: 'rsi_mean_reversion', name: 'Wilder RSI (14) Mean Reversion', description: '', params: [] },
  { id: 'vcp_breakout', name: 'VCP Range Breakout', description: '', params: [] },
  { id: 'supertrend_atr', name: 'Supertrend (10, ATR 3.0x)', description: '', params: [] },
];

function buildTimeframeString(numRaw: string, unit: 'minute' | 'day'): string {
  const parsed = Number.parseInt(numRaw, 10);
  const count = Number.isFinite(parsed) && parsed >= 1 ? Math.min(unit === 'minute' ? 1440 : 365, parsed) : 1;
  return `${count}${unit}`;
}

const DEFAULT_FALLBACK_COLUMNS: ColumnSpec[] = [
  { key: 'ema20', label: 'EMA 20', fmt: 'price', decimals: 2, align: 'right' },
  { key: 'ema50', label: 'EMA 50', fmt: 'price', decimals: 2, align: 'right' },
  { key: 'rsi14', label: 'RSI (14)', fmt: 'number', decimals: 1, align: 'right' },
  { key: 'atr_pct', label: 'ATR%', fmt: 'pct', decimals: 2, align: 'right' },
  { key: 'vol_ratio', label: 'Vol Ratio', fmt: 'ratio', decimals: 2, align: 'right' },
  { key: 'supertrend', label: 'Supertrend', fmt: 'badge', decimals: 0, align: 'left' },
];

function formatDynamicCell(value: unknown, col: ColumnSpec): { text: string; toneClass: string } {
  if (value === null || value === undefined || value === '') {
    return { text: '—', toneClass: 'text-slate-500' };
  }
  if (col.fmt === 'badge') {
    const raw = String(value).toUpperCase();
    const toneClass =
      raw === 'BULL' || raw === 'UP' || raw === 'STRONG'
        ? 'font-semibold text-emerald-400'
        : raw === 'BEAR' || raw === 'DOWN'
          ? 'font-semibold text-red-400'
          : 'text-slate-400';
    return { text: raw, toneClass };
  }
  if (col.fmt === 'text') {
    return { text: String(value), toneClass: 'text-slate-300' };
  }
  const num = Number(value);
  if (Number.isNaN(num)) {
    return { text: String(value), toneClass: 'text-slate-300' };
  }
  const dec = Number.isFinite(col.decimals) ? col.decimals : 2;
  if (col.fmt === 'signed_pct') {
    const sign = num >= 0 ? '+' : '';
    return {
      text: `${sign}${num.toFixed(dec)}%`,
      toneClass: num >= 0 ? 'text-emerald-400' : 'text-red-400',
    };
  }
  if (col.fmt === 'pct') {
    return { text: `${num.toFixed(dec)}%`, toneClass: 'text-slate-300' };
  }
  if (col.fmt === 'ratio') {
    return { text: `${num.toFixed(dec)}x`, toneClass: num >= 1.2 ? 'text-sky-300' : 'text-slate-300' };
  }
  return { text: num.toFixed(dec), toneClass: 'text-slate-200' };
}

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

  // Dynamic Plugin & Environment Metadata
  const [meta, setMeta] = useState<AnalyticsMetaPayload | null>(null);
  const [metaLoading, setMetaLoading] = useState<boolean>(false);

  // Screener state
  const [scUniverse, setScUniverse] = useState<string>('CUSTOM');
  const [scSymbols, setScSymbols] = useState<string>(DEFAULT_WATCHLIST);
  const [scExchange, setScExchange] = useState<string>('NSE');
  const [scTfNum, setScTfNum] = useState<string>('1');
  const [scTfUnit, setScTfUnit] = useState<'minute' | 'day'>('day');
  const scTimeframe = buildTimeframeString(scTfNum, scTfUnit);
  const [scAsOfDate, setScAsOfDate] = useState<string>('');
  const [scScreenId, setScScreenId] = useState<string>('all_metrics');
  const [scParams, setScParams] = useState<Record<string, number | boolean | string>>({});
  const [scProvider, setScProvider] = useState<string>('auto');
  const [scBackend, setScBackend] = useState<string>('auto');
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
  const [btParams, setBtParams] = useState<Record<string, number | boolean | string>>({});
  const [btTfNum, setBtTfNum] = useState<string>('1');
  const [btTfUnit, setBtTfUnit] = useState<'minute' | 'day'>('day');
  const btTimeframe = buildTimeframeString(btTfNum, btTfUnit);
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

  const fetchAnalyticsMeta = useCallback(async (forceReload = false) => {
    setMetaLoading(true);
    try {
      const res = await fetch(forceReload ? '/api/analytics/reload' : '/api/analytics/meta', {
        method: forceReload ? 'POST' : 'GET',
      });
      if (!res.ok) return;
      const body = (await res.json()) as AnalyticsMetaPayload;
      setMeta(body);
    } catch {
      // keep existing UI options on transient failure
    } finally {
      setMetaLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAnalyticsMeta(false);
  }, [fetchAnalyticsMeta]);

  const activeScreenMeta = useMemo(
    () => meta?.screens?.find((s) => s.id === scScreenId) ?? null,
    [meta, scScreenId]
  );

  const activeStrategyMeta = useMemo(
    () => meta?.strategy_specs?.find((s) => s.id === btStrategy) ?? null,
    [meta, btStrategy]
  );

  useEffect(() => {
    if (!activeScreenMeta) return;
    const defaults: Record<string, number | boolean | string> = {};
    for (const p of activeScreenMeta.params || []) {
      defaults[p.name] = p.default;
    }
    setScParams(defaults);
  }, [activeScreenMeta]);

  useEffect(() => {
    if (!activeStrategyMeta) return;
    const defaults: Record<string, number | boolean | string> = {};
    for (const p of activeStrategyMeta.params || []) {
      defaults[p.name] = p.default;
    }
    setBtParams(defaults);
  }, [activeStrategyMeta]);

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
    for (let attempt = 0; attempt < 1500; attempt += 1) {
      const res = await fetch(`/api/analytics/jobs/${encodeURIComponent(jobId)}`);
      const body = (await res.json().catch(() => ({}))) as { job?: AsyncJobStatus; detail?: string };
      if (!res.ok || !body.job) {
        throw new Error(body.detail || 'Could not query background job status.');
      }
      onProgress(body.job);
      if (body.job.status === 'COMPLETED' || body.job.status === 'FAILED' || body.job.status === 'CANCELLED') {
        return body.job;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 450));
    }
    throw new Error('Background analytics task timed out while polling.');
  }

  async function handleCancelJob(jobId: string) {
    try {
      await fetch(`/api/analytics/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
    } catch {
      // ignore cancel errors
    }
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
          universe: scUniverse,
          exchange: scExchange,
          timeframe: scTimeframe,
          as_of_date: scAsOfDate || null,
          screen_id: scScreenId,
          preset: scScreenId,
          params: scParams,
          provider: scProvider === 'auto' ? null : scProvider,
          backend: scBackend,
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
      if (doneJob.status === 'CANCELLED') {
        setScError(doneJob.progress_message || 'Screener scan cancelled.');
        return;
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
          params: btParams,
          timeframe: btTimeframe,
          from_date: btFrom,
          to_date: btTo,
          initial_capital: Number(btCapital) || 500000,
          stop_loss_pct: Number(btSl) || 0,
          take_profit_pct: Number(btTp) || 0,
          provider: btProvider === 'auto' ? null : btProvider,
          backend: scBackend,
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
      if (doneJob.status === 'CANCELLED') {
        setBtError(doneJob.progress_message || 'Backtest cancelled.');
        return;
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

  const pluginErrorEntries = [
    ...Object.entries(meta?.file_errors?.screens || {}).map(([f, msg]) => ({ kind: 'Screen', f, msg })),
    ...Object.entries(meta?.file_errors?.strategies || {}).map(([f, msg]) => ({ kind: 'Strategy', f, msg })),
  ];

  const storageLabel = meta?.storage?.parquet_available ? 'HYBRID PARQUET' : 'SQLITE FALLBACK';
  const activeTaLibs = Object.entries(meta?.indicator_backends || {})
    .filter(([, ok]) => ok)
    .map(([name]) => name)
    .join(', ');

  return (
    <section id="analytics-console" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-100">
            Phase 05 — Screener, Backtester &amp; Portfolio Risk Console
          </h2>
          <p className="text-sm text-slate-400">
            Hot-reloaded <span className="font-mono text-slate-200">.py</span> screens &amp; strategies via{' '}
            <span className="font-mono text-slate-200">{activeDataConnection.toUpperCase()}</span> · Storage:{' '}
            <span className="font-mono text-sky-300">{storageLabel}</span>
            {activeTaLibs ? ` · TA: ${activeTaLibs}` : ''} · Risk Guard:{' '}
            <span className="font-mono text-slate-200">{activeTradingBroker.toUpperCase()}</span>
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void fetchAnalyticsMeta(true)}
            disabled={metaLoading}
            className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:border-sky-400 hover:text-slate-100 disabled:opacity-50"
            title="Re-scan screener/screens/*.py and backtest/strategies/*.py"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${metaLoading ? 'animate-spin' : ''}`} />
            <span>Reload .py Files</span>
          </button>

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
      </div>

      {pluginErrorEntries.length > 0 && (
        <div className="rounded-lg border border-amber-800/70 bg-amber-950/40 p-3 text-xs text-amber-200">
          {pluginErrorEntries.map((item) => (
            <div key={`${item.kind}-${item.f}`} className="font-mono">
              [{item.kind} Plugin Error in {item.f}]: {item.msg.split('\n')[0]}
            </div>
          ))}
        </div>
      )}

      {/* TAB 1: MARKET SCREENER */}
      {activeTab === 'screener' && (
        <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-6">
          <div className="grid grid-cols-1 items-end gap-3 md:grid-cols-2 lg:grid-cols-[0.95fr_1.4fr_1.05fr_0.85fr_auto]">
            <div>
              <label htmlFor="sc-universe" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Symbol Group / Universe
              </label>
              <select
                id="sc-universe"
                value={scUniverse}
                onChange={(e) => {
                  const val = e.target.value;
                  setScUniverse(val);
                  if (val === 'CUSTOM' && !scSymbols.trim()) {
                    setScSymbols(DEFAULT_WATCHLIST);
                  } else if (val !== 'CUSTOM') {
                    setScSymbols('');
                  }
                }}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                {(meta?.universes || [
                  { id: 'DEFAULT_WATCHLIST', label: 'Default Watchlist (10)', count: 10 },
                  { id: 'NIFTY_50', label: 'NIFTY 50', count: 50 },
                  { id: 'NIFTY_BANK', label: 'NIFTY Bank', count: 12 },
                  { id: 'NIFTY_IT', label: 'NIFTY IT', count: 10 },
                  { id: 'NIFTY_FNO', label: 'NIFTY F&O', count: 185 },
                  { id: 'NIFTY_500', label: 'NIFTY 500', count: 380 },
                  { id: 'CACHED_SQLITE', label: 'Local Cached', count: 0 },
                  { id: 'CUSTOM', label: 'Custom (Comma-Separated)', count: 0 },
                ]).map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="sc-symbols" className="mb-1.5 block text-xs font-semibold text-slate-300">
                {scUniverse === 'CUSTOM'
                  ? 'Watchlist Symbols (comma-separated)'
                  : `Extra Symbols (appended to ${scUniverse})`}
              </label>
              <input
                id="sc-symbols"
                type="text"
                value={scSymbols}
                onChange={(e) => setScSymbols(e.target.value.toUpperCase())}
                placeholder={
                  scUniverse === 'CUSTOM'
                    ? DEFAULT_WATCHLIST
                    : `Optional extra symbols alongside ${scUniverse}...`
                }
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="sc-preset" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Screen (.py plugin)
              </label>
              <select
                id="sc-preset"
                value={scScreenId}
                onChange={(e) => setScScreenId(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                {(meta?.screens && meta.screens.length > 0 ? meta.screens : FALLBACK_SCREENS).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="sc-asof" className="mb-1.5 block text-xs font-semibold text-slate-300">
                As-Of Date (Point-in-Time)
              </label>
              <div className="flex gap-1.5">
                <input
                  id="sc-asof"
                  type="date"
                  max={todayIso}
                  value={scAsOfDate}
                  onChange={(e) => setScAsOfDate(e.target.value)}
                  className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                />
                {scAsOfDate && (
                  <button
                    type="button"
                    onClick={() => setScAsOfDate('')}
                    className="rounded-lg border border-slate-700 bg-[#0b1120] px-2 text-[11px] text-slate-400 hover:text-slate-100"
                    title="Reset to Latest"
                  >
                    Latest
                  </button>
                )}
              </div>
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => void handleRunScreener()}
                disabled={scBusy}
                className="inline-flex min-h-[40px] shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-xs font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                <span>{scBusy ? 'Scanning...' : 'Run Scan'}</span>
              </button>
              {scBusy && scJob && (
                <button
                  type="button"
                  onClick={() => void handleCancelJob(scJob.job_id)}
                  className="inline-flex min-h-[40px] items-center gap-1 rounded-lg border border-red-800 bg-red-950/60 px-3 py-2 text-xs font-semibold text-red-200 hover:bg-red-900/60"
                >
                  <XCircle className="h-3.5 w-3.5" />
                  <span>Cancel</span>
                </button>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-3">
            <div>
              <label htmlFor="sc-tf-num" className="mb-1 block text-xs text-slate-400">
                Timeframe (API: 1m / 1d only)
              </label>
              <div className="flex gap-1.5">
                <input
                  id="sc-tf-num"
                  type="number"
                  min={1}
                  max={scTfUnit === 'minute' ? 1440 : 365}
                  step={1}
                  value={scTfNum}
                  onChange={(e) => setScTfNum(e.target.value)}
                  onBlur={() => {
                    const n = Number.parseInt(scTfNum, 10);
                    setScTfNum(String(Number.isFinite(n) && n >= 1 ? n : 1));
                  }}
                  aria-label="Screener timeframe interval"
                  className="min-h-[38px] w-20 rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 py-1.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                />
                <select
                  id="sc-tf-unit"
                  value={scTfUnit}
                  onChange={(e) => setScTfUnit(e.target.value === 'minute' ? 'minute' : 'day')}
                  aria-label="Screener timeframe unit"
                  className="min-h-[38px] flex-1 rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                >
                  <option value="minute">minute</option>
                  <option value="day">day</option>
                </select>
              </div>
            </div>

            <div>
              <label htmlFor="sc-provider" className="mb-1 block text-xs text-slate-400">
                Data Source
              </label>
              <select
                id="sc-provider"
                value={scProvider}
                onChange={(e) => setScProvider(e.target.value)}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="auto">Assigned ({activeDataConnection})</option>
                <option value="yfinance">Yahoo Finance</option>
                <option value="upstox">Upstox</option>
                <option value="zerodha">Zerodha Kite</option>
              </select>
            </div>

            <div>
              <label htmlFor="sc-ta-backend" className="mb-1 block text-xs text-slate-400">
                Indicator Engine
              </label>
              <select
                id="sc-ta-backend"
                value={scBackend}
                onChange={(e) => setScBackend(e.target.value)}
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="auto">Auto (TA-Lib → pandas-ta → Built-in)</option>
                <option value="talib" disabled={!meta?.indicator_backends?.talib}>
                  TA-Lib {meta?.indicator_backends?.talib ? '' : '(not installed)'}
                </option>
                <option value="pandas_ta" disabled={!meta?.indicator_backends?.pandas_ta}>
                  pandas-ta {meta?.indicator_backends?.pandas_ta ? '' : '(not installed)'}
                </option>
                <option value="builtin">Built-in Pure Python O(N)</option>
              </select>
            </div>
          </div>

          {activeScreenMeta && activeScreenMeta.params.length > 0 && (
            <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3.5">
              <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2 text-xs">
                <div>
                  <span className="font-semibold text-slate-200">{activeScreenMeta.name} Parameters</span>
                  {activeScreenMeta.description && (
                    <span className="ml-2 text-slate-400">— {activeScreenMeta.description}</span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => {
                    const reset: Record<string, number | boolean | string> = {};
                    activeScreenMeta.params.forEach((p) => {
                      reset[p.name] = p.default;
                    });
                    setScParams(reset);
                  }}
                  className="text-[11px] font-semibold text-sky-400 hover:text-sky-300"
                >
                  Reset Defaults
                </button>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                {activeScreenMeta.params.map((spec) => (
                  <div key={spec.name}>
                    <label className="mb-1 block text-[11px] text-slate-400">{spec.label}</label>
                    {spec.type === 'bool' ? (
                      <label className="flex min-h-[34px] cursor-pointer items-center gap-2 rounded border border-slate-700 bg-[#111827] px-2.5 text-xs text-slate-200">
                        <input
                          type="checkbox"
                          checked={Boolean(scParams[spec.name] ?? spec.default)}
                          onChange={(e) =>
                            setScParams((prev) => ({ ...prev, [spec.name]: e.target.checked }))
                          }
                          className="h-3.5 w-3.5 accent-sky-400"
                        />
                        <span>Enabled</span>
                      </label>
                    ) : (
                      <input
                        type={spec.type === 'str' ? 'text' : 'number'}
                        min={spec.min_val ?? undefined}
                        max={spec.max_val ?? undefined}
                        step={spec.step ?? (spec.type === 'int' ? 1 : 0.1)}
                        value={String(scParams[spec.name] ?? spec.default)}
                        onChange={(e) =>
                          setScParams((prev) => ({
                            ...prev,
                            [spec.name]:
                              spec.type === 'str' ? e.target.value : Number(e.target.value),
                          }))
                        }
                        className="min-h-[34px] w-full rounded border border-slate-700 bg-[#111827] px-2.5 py-1 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

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
                  Matched {scResult.matched_count} of {scResult.evaluated_count} evaluated ({scResult.universe_label} · {scResult.screen_name || scResult.preset_label}) · As Of{' '}
                  <strong className="text-slate-200">{scResult.as_of_date}</strong>
                  {scResult.from_date ? ` (Auto window: ${scResult.from_date} → ${scResult.as_of_date})` : ''}
                </span>
                <span>
                  Source: {scResult.provider.toUpperCase()}
                  {scResult.resampled && scResult.base_timeframe
                    ? ` · ${scResult.timeframe} resampled from ${scResult.base_timeframe}`
                    : ` · ${scResult.timeframe}`}
                  {' · '}API Calls: {scResult.api_calls_made} · Cached Days Skipped: {scResult.existing_days_skipped}
                </span>
              </div>

              {scResult.results.length === 0 ? (
                <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-4 text-xs text-slate-400">
                  No symbols matched &ldquo;{scResult.preset_label}&rdquo; as of {scResult.as_of_date}. Try &ldquo;All Watchlist Metrics&rdquo; or adjust indicator parameters above.
                </div>
              ) : (
                (() => {
                  const activeCols =
                    scResult.columns && scResult.columns.length > 0
                      ? scResult.columns
                      : DEFAULT_FALLBACK_COLUMNS;
                  return (
                    <div className="max-h-[360px] overflow-auto rounded-lg border border-slate-800">
                      <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                        <thead className="sticky top-0 bg-[#0b1120] text-slate-400">
                          <tr className="border-b border-slate-800">
                            <th className="px-3 py-2.5 font-semibold">Symbol</th>
                            <th className="px-3 py-2.5 text-right font-semibold">Close</th>
                            <th className="px-3 py-2.5 text-right font-semibold">Chg%</th>
                            {activeCols.map((col) => (
                              <th
                                key={col.key}
                                className={`px-3 py-2.5 font-semibold ${col.align === 'left' ? 'text-left' : 'text-right'}`}
                              >
                                {col.label}
                              </th>
                            ))}
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
                              {activeCols.map((col) => {
                                const cell = formatDynamicCell(r[col.key], col);
                                return (
                                  <td
                                    key={col.key}
                                    className={`px-3 py-2 ${col.align === 'left' ? 'text-left' : 'text-right'} ${cell.toneClass}`}
                                  >
                                    {cell.text}
                                  </td>
                                );
                              })}
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
                  );
                })()
              )}

              {scResult.errors && scResult.errors.length > 0 && (
                <div className="max-h-[90px] overflow-auto text-xs text-amber-400">
                  Skipped {scResult.errors.length} symbol(s): {scResult.errors.slice(0, 15).map((e) => `${e.symbol} (${e.error})`).join(' · ')}
                  {scResult.errors.length > 15 ? ` · +${scResult.errors.length - 15} more` : ''}
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
                Strategy (.py plugin)
              </label>
              <select
                id="bt-strategy"
                value={btStrategy}
                onChange={(e) => setBtStrategy(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                {(meta?.strategy_specs && meta.strategy_specs.length > 0 ? meta.strategy_specs : FALLBACK_STRATEGIES).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="bt-tf-num" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Timeframe
              </label>
              <div className="flex gap-1.5">
                <input
                  id="bt-tf-num"
                  type="number"
                  min={1}
                  max={btTfUnit === 'minute' ? 1440 : 365}
                  step={1}
                  value={btTfNum}
                  onChange={(e) => setBtTfNum(e.target.value)}
                  onBlur={() => {
                    const n = Number.parseInt(btTfNum, 10);
                    setBtTfNum(String(Number.isFinite(n) && n >= 1 ? n : 1));
                  }}
                  aria-label="Backtest timeframe interval"
                  className="min-h-[40px] w-16 rounded-lg border border-slate-700 bg-[#0b1120] px-2 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                />
                <select
                  id="bt-tf-unit"
                  value={btTfUnit}
                  onChange={(e) => setBtTfUnit(e.target.value === 'minute' ? 'minute' : 'day')}
                  aria-label="Backtest timeframe unit"
                  className="min-h-[40px] flex-1 rounded-lg border border-slate-700 bg-[#0b1120] px-2 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                >
                  <option value="minute">minute</option>
                  <option value="day">day</option>
                </select>
              </div>
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

            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => void handleRunBacktest()}
                disabled={btBusy}
                className="inline-flex min-h-[40px] shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-xs font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                <span>{btBusy ? 'Simulating...' : 'Run Backtest'}</span>
              </button>
              {btBusy && btJob && (
                <button
                  type="button"
                  onClick={() => void handleCancelJob(btJob.job_id)}
                  className="inline-flex min-h-[40px] items-center gap-1 rounded-lg border border-red-800 bg-red-950/60 px-3 py-2 text-xs font-semibold text-red-200 hover:bg-red-900/60"
                >
                  <XCircle className="h-3.5 w-3.5" />
                  <span>Cancel</span>
                </button>
              )}
            </div>
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

          {activeStrategyMeta && activeStrategyMeta.params.length > 0 && (
            <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3.5">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
                <div>
                  <span className="font-semibold text-slate-200">{activeStrategyMeta.name} Parameters</span>
                  {activeStrategyMeta.description && (
                    <span className="ml-2 text-slate-400">— {activeStrategyMeta.description}</span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => {
                    const reset: Record<string, number | boolean | string> = {};
                    activeStrategyMeta.params.forEach((p) => {
                      reset[p.name] = p.default;
                    });
                    setBtParams(reset);
                  }}
                  className="text-[11px] font-semibold text-sky-400 hover:text-sky-300"
                >
                  Reset Defaults
                </button>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {activeStrategyMeta.params.map((spec) => (
                  <div key={spec.name}>
                    <label className="mb-1 block text-[11px] text-slate-400">{spec.label}</label>
                    <input
                      type={spec.type === 'str' ? 'text' : 'number'}
                      min={spec.min_val ?? undefined}
                      max={spec.max_val ?? undefined}
                      step={spec.step ?? (spec.type === 'int' ? 1 : 0.1)}
                      value={String(btParams[spec.name] ?? spec.default)}
                      onChange={(e) =>
                        setBtParams((prev) => ({
                          ...prev,
                          [spec.name]:
                            spec.type === 'str' ? e.target.value : Number(e.target.value),
                        }))
                      }
                      className="min-h-[34px] w-full rounded border border-slate-700 bg-[#111827] px-2.5 py-1 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

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
