import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertCircle,
  BarChart3,
  Check,
  Download,
  Play,
  Radio,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  Square,
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
  supports_multi_timeframe?: boolean;
  default_timeframes?: string[];
  multi_row?: boolean;
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
  filename?: string;
  updated_at_ist?: string | null;
  synced?: boolean;
  source?: string;
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
  universes_dir?: string;
  default_watchlist: string[];
}

interface LiveScreenerRow extends ScreenerRow {
  ltp?: number;
  prev_close?: number;
  volume?: number;
  tick_direction?: 'up' | 'down' | 'flat';
  live_source?: string;
  live_updated_ist?: string;
  is_live?: boolean;
}

interface LiveScreenerAlert {
  id: string;
  ts_ist: string;
  symbol: string;
  transition: 'ENTERED' | 'EXITED';
  signal: string;
  ltp: number;
  change_pct: number;
  score: number;
  tags: string[];
}

interface LiveScreenerStatePayload {
  ok?: boolean;
  session_id: string;
  running: boolean;
  phase: 'idle' | 'warming_up' | 'streaming' | 'stopped' | 'error';
  error?: string;
  started_at_ist?: string | null;
  screen_id: string;
  screen_name?: string;
  params?: Record<string, unknown>;
  universe: string;
  universe_label?: string;
  exchange: string;
  timeframe: string;
  base_timeframe?: string;
  provider?: string;
  backend?: string;
  required_bars?: number;
  lookback_days?: number;
  poll_interval_sec?: number;
  columns?: ColumnSpec[];
  warmup?: {
    completed: number;
    total: number;
    pct: number;
    symbol?: string;
    message: string;
    error_count: number;
    errors?: Array<{ symbol: string; error: string }>;
  };
  live?: {
    active_feed: string;
    tick_seq: number;
    ticks_processed: number;
    latency_ms?: number | null;
    last_tick_ist?: string | null;
    symbols_ready: number;
    symbols_live: number;
  };
  matched_count: number;
  evaluated_count: number;
  results: LiveScreenerRow[];
  alerts?: LiveScreenerAlert[];
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

interface MissingDataEntry {
  symbol: string;
  exchange?: string;
  timeframe: string;
  canonical_timeframe?: string;
  reason: string;
}

interface ScreenerResultPayload {
  screen_id: string;
  screen_name?: string;
  preset: string;
  preset_label: string;
  multi_timeframe?: boolean;
  multi_row?: boolean;
  columns?: ColumnSpec[];
  universe: string;
  universe_label: string;
  as_of_date: string;
  from_date?: string;
  is_historical_as_of?: boolean;
  provider: string;
  exchange: string;
  timeframe: string;
  timeframes?: string[];
  base_timeframe?: string;
  resampled?: boolean;
  required_bars?: number;
  lookback_days?: number;
  storage_backend?: string;
  scanned_count: number;
  evaluated_count: number;
  matched_count: number;
  events_found?: number;
  major_count?: number;
  minor_count?: number;
  missing_data_count?: number;
  missing_data?: MissingDataEntry[];
  price_adjustment_note?: string;
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

const MTF_TIMEFRAME_OPTIONS: Array<{ id: string; label: string }> = [
  { id: '5minute', label: '5m' },
  { id: '15minute', label: '15m' },
  { id: '60minute', label: '1h' },
  { id: '1day', label: '1d' },
  { id: '1week', label: '1w' },
];

const SR_EVENT_OPTIONS: string[] = [
  'NEAR_SUPPORT',
  'NEAR_RESISTANCE',
  'SUPPORT_BOUNCE',
  'RESISTANCE_REJECTION',
  'RESISTANCE_BREAKOUT',
  'SUPPORT_BREAKDOWN',
  'BREAKOUT_RETEST',
  'BREAKDOWN_RETEST',
  'FALSE_BREAKOUT',
];

function srEventToneClass(eventName: string): string {
  const ev = eventName.toUpperCase();
  if (ev === 'SUPPORT_BOUNCE' || ev === 'RESISTANCE_BREAKOUT' || ev === 'BREAKOUT_RETEST') {
    return 'bg-emerald-500/15 text-emerald-300 border border-emerald-700/60';
  }
  if (ev === 'RESISTANCE_REJECTION' || ev === 'SUPPORT_BREAKDOWN' || ev === 'BREAKDOWN_RETEST') {
    return 'bg-red-500/15 text-red-300 border border-red-700/60';
  }
  if (ev === 'FALSE_BREAKOUT') {
    return 'bg-amber-500/15 text-amber-300 border border-amber-700/60';
  }
  if (ev === 'NEAR_SUPPORT') {
    return 'bg-sky-500/15 text-sky-300 border border-sky-700/60';
  }
  return 'bg-violet-500/15 text-violet-300 border border-violet-700/60';
}

const FALLBACK_SCREENS: ScreenMeta[] = [
  { id: 'all_metrics', name: 'All Watchlist Metrics', description: '', category: '', min_bars: 15, params: [] },
  { id: 'momentum_breakout', name: 'Momentum Breakout', description: '', category: '', min_bars: 50, params: [] },
  { id: 'vcp_contraction', name: 'VCP Contraction', description: '', category: '', min_bars: 30, params: [] },
  { id: 'oversold_rebound', name: 'Oversold Mean Reversion', description: '', category: '', min_bars: 20, params: [] },
  { id: 'trend_following', name: 'Uptrend Alignment (EMA + ADX)', description: '', category: '', min_bars: 50, params: [] },
  {
    id: 'support_resistance',
    name: 'Multi-Timeframe Support & Resistance',
    description: '',
    category: 'structure',
    min_bars: 25,
    supports_multi_timeframe: true,
    default_timeframes: ['5minute', '15minute', '60minute', '1day', '1week'],
    multi_row: true,
    params: [],
  },
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
  const [activeTab, setActiveTab] = useState<'screener' | 'live_screen' | 'backtest' | 'risk'>('screener');

  // Dynamic Plugin & Environment Metadata
  const [meta, setMeta] = useState<AnalyticsMetaPayload | null>(null);
  const [metaLoading, setMetaLoading] = useState<boolean>(false);
  const [syncingCsv, setSyncingCsv] = useState<boolean>(false);
  const [csvSyncBanner, setCsvSyncBanner] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // Screener state
  const [scUniverse, setScUniverse] = useState<string>('CUSTOM');
  const [scSymbols, setScSymbols] = useState<string>(DEFAULT_WATCHLIST);
  const [scExchange, setScExchange] = useState<string>('NSE');
  const [scTfNum, setScTfNum] = useState<string>('1');
  const [scTfUnit, setScTfUnit] = useState<'minute' | 'day'>('day');
  const scTimeframe = buildTimeframeString(scTfNum, scTfUnit);
  const [scSelectedTfs, setScSelectedTfs] = useState<string[]>(['5minute', '15minute', '60minute', '1day', '1week']);
  const [scAsOfDate, setScAsOfDate] = useState<string>('');
  const [scScreenId, setScScreenId] = useState<string>('all_metrics');
  const [scParams, setScParams] = useState<Record<string, number | boolean | string>>({});
  const [scProvider, setScProvider] = useState<string>('auto');
  const [scBackend, setScBackend] = useState<string>('auto');
  const [scJob, setScJob] = useState<AsyncJobStatus | null>(null);
  const [scBusy, setScBusy] = useState<boolean>(false);
  const [scError, setScError] = useState<string>('');
  const [scResult, setScResult] = useState<ScreenerResultPayload | null>(null);

  // Support & Resistance table filter & sort state
  const [srSearch, setSrSearch] = useState<string>('');
  const [srEventFilter, setSrEventFilter] = useState<string>('ALL');
  const [srTfFilter, setSrTfFilter] = useState<string>('ALL');
  const [srClassFilter, setSrClassFilter] = useState<string>('ALL');
  const [srSortBy, setSrSortBy] = useState<'strength_desc' | 'distance_asc' | 'rel_vol_desc' | 'touches_desc'>('strength_desc');
  const [srShowMissing, setSrShowMissing] = useState<boolean>(false);

  // Live Screen (real-time WebSocket) state
  const [lsUniverse, setLsUniverse] = useState<string>('DEFAULT_WATCHLIST');
  const [lsSymbols, setLsSymbols] = useState<string>('');
  const [lsExchange, setLsExchange] = useState<string>('NSE');
  const [lsTfNum, setLsTfNum] = useState<string>('1');
  const [lsTfUnit, setLsTfUnit] = useState<'minute' | 'day'>('day');
  const lsTimeframe = buildTimeframeString(lsTfNum, lsTfUnit);
  const [lsScreenId, setLsScreenId] = useState<string>('all_metrics');
  const [lsParams, setLsParams] = useState<Record<string, number | boolean | string>>({});
  const [lsProvider, setLsProvider] = useState<string>('auto');
  const [lsBackend, setLsBackend] = useState<string>('auto');
  const [lsIntervalSec, setLsIntervalSec] = useState<string>('2');
  const [lsMatchedOnly, setLsMatchedOnly] = useState<boolean>(false);
  const [lsShowAlerts, setLsShowAlerts] = useState<boolean>(true);
  const [lsBusy, setLsBusy] = useState<boolean>(false);
  const [lsError, setLsError] = useState<string>('');
  const [lsWsStatus, setLsWsStatus] = useState<'connecting' | 'connected' | 'disconnected'>('disconnected');
  const [lsState, setLsState] = useState<LiveScreenerStatePayload | null>(null);
  const lsWsRef = useRef<WebSocket | null>(null);
  const lsTableWrapRef = useRef<HTMLDivElement | null>(null);

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

  const activeLiveScreenMeta = useMemo(
    () => meta?.screens?.find((s) => s.id === lsScreenId) ?? null,
    [meta, lsScreenId]
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
    if (activeScreenMeta.supports_multi_timeframe && activeScreenMeta.default_timeframes?.length) {
      setScSelectedTfs(activeScreenMeta.default_timeframes);
    }
  }, [activeScreenMeta]);

  useEffect(() => {
    if (!activeLiveScreenMeta) return;
    const defaults: Record<string, number | boolean | string> = {};
    for (const p of activeLiveScreenMeta.params || []) {
      defaults[p.name] = p.default;
    }
    setLsParams(defaults);
  }, [activeLiveScreenMeta]);

  useEffect(() => {
    if (!activeStrategyMeta) return;
    const defaults: Record<string, number | boolean | string> = {};
    for (const p of activeStrategyMeta.params || []) {
      defaults[p.name] = p.default;
    }
    setBtParams(defaults);
  }, [activeStrategyMeta]);

  // Real-time WebSocket connection for Live Screen (/ws/live-screener)
  useEffect(() => {
    if (activeTab !== 'live_screen') {
      if (lsWsRef.current) {
        lsWsRef.current.close();
        lsWsRef.current = null;
      }
      setLsWsStatus('disconnected');
      return;
    }

    let cancelled = false;
    let reconnectTimer: number | null = null;

    const connectWs = () => {
      if (cancelled) return;
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const url = `${proto}//${window.location.host}/ws/live-screener`;
      setLsWsStatus('connecting');
      const ws = new WebSocket(url);
      lsWsRef.current = ws;

      ws.onopen = () => {
        if (!cancelled) setLsWsStatus('connected');
      };

      ws.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(String(ev.data || '{}')) as Record<string, unknown>;
          const mtype = String(msg.type || '');
          if (mtype === 'error') {
            setLsError(String(msg.detail || 'Live screener WebSocket error'));
            return;
          }
          if (mtype === 'live_screen:state' || mtype === 'live_screen:stopped') {
            setLsState(msg as unknown as LiveScreenerStatePayload);
            return;
          }
          if (mtype === 'live_screen:warmup') {
            setLsState((prev) => {
              const base: LiveScreenerStatePayload = prev || {
                session_id: String(msg.session_id || ''),
                running: true,
                phase: 'warming_up',
                screen_id: lsScreenId,
                universe: lsUniverse,
                exchange: lsExchange,
                timeframe: lsTimeframe,
                matched_count: 0,
                evaluated_count: 0,
                results: [],
              };
              const nextRows = Array.isArray(msg.results)
                ? (msg.results as LiveScreenerRow[])
                : base.results;
              return {
                ...base,
                session_id: String(msg.session_id || base.session_id),
                running: true,
                phase: 'warming_up',
                warmup: (msg.warmup as LiveScreenerStatePayload['warmup']) || base.warmup,
                columns: (msg.columns as ColumnSpec[]) || base.columns,
                results: nextRows,
                evaluated_count: nextRows.length,
                matched_count: nextRows.filter((r) => r.matched).length,
              };
            });
            return;
          }
          if (mtype === 'live_screen:tick') {
            setLsState((prev) => {
              const base: LiveScreenerStatePayload = prev || {
                session_id: String(msg.session_id || ''),
                running: true,
                phase: 'streaming',
                screen_id: lsScreenId,
                universe: lsUniverse,
                exchange: lsExchange,
                timeframe: lsTimeframe,
                matched_count: 0,
                evaluated_count: 0,
                results: [],
              };
              const nextRows = Array.isArray(msg.results)
                ? (msg.results as LiveScreenerRow[])
                : base.results;
              return {
                ...base,
                session_id: String(msg.session_id || base.session_id),
                running: Boolean(msg.running ?? true),
                phase: (msg.phase as LiveScreenerStatePayload['phase']) || 'streaming',
                columns: (msg.columns as ColumnSpec[]) || base.columns,
                results: nextRows,
                evaluated_count: Number(msg.evaluated_count ?? nextRows.length),
                matched_count: Number(msg.matched_count ?? nextRows.filter((r) => r.matched).length),
                alerts: (msg.alerts as LiveScreenerAlert[]) || base.alerts,
                live: {
                  active_feed: String(msg.active_feed ?? base.live?.active_feed ?? ''),
                  tick_seq: Number(msg.tick_seq ?? base.live?.tick_seq ?? 0),
                  ticks_processed: Number(msg.ticks_processed ?? base.live?.ticks_processed ?? 0),
                  latency_ms: (msg.latency_ms as number | null | undefined) ?? base.live?.latency_ms,
                  last_tick_ist: (msg.last_tick_ist as string | null | undefined) ?? base.live?.last_tick_ist,
                  symbols_ready: nextRows.length,
                  symbols_live: nextRows.filter((r) => r.is_live).length,
                },
              };
            });
          }
        } catch {
          // ignore malformed WS frames
        }
      };

      ws.onclose = () => {
        if (cancelled) return;
        setLsWsStatus('disconnected');
        reconnectTimer = window.setTimeout(connectWs, 2000);
      };

      ws.onerror = () => {
        if (!cancelled) setLsWsStatus('disconnected');
      };
    };

    connectWs();

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (lsWsRef.current) {
        lsWsRef.current.close();
        lsWsRef.current = null;
      }
    };
  }, [activeTab]);

  async function handleSyncNseCsvs() {
    setSyncingCsv(true);
    setCsvSyncBanner(null);
    try {
      const res = await fetch('/api/analytics/universes/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const body = (await res.json().catch(() => ({}))) as {
        ok?: boolean;
        synced_count?: number;
        failed_count?: number;
        updated_at_ist?: string;
        universes?: UniverseMeta[];
        detail?: string;
      };
      if (!res.ok) {
        throw new Error(body.detail || 'Failed to sync NSE constituent CSVs.');
      }
      if (Array.isArray(body.universes)) {
        setMeta((prev) => (prev ? { ...prev, universes: body.universes! } : prev));
      } else {
        await fetchAnalyticsMeta(false);
      }
      setCsvSyncBanner({
        type: body.failed_count ? 'err' : 'ok',
        text: `Synced ${body.synced_count ?? 0} NSE CSV universe file(s)${
          body.failed_count ? ` (${body.failed_count} failed)` : ''
        } at ${body.updated_at_ist || 'now'}.`,
      });
    } catch (err) {
      setCsvSyncBanner({
        type: 'err',
        text: err instanceof Error ? err.message : 'NSE CSV sync failed.',
      });
    } finally {
      setSyncingCsv(false);
    }
  }

  async function handleStartLiveScreen() {
    const syms = lsSymbols
      .split(/[\s,]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    setLsBusy(true);
    setLsError('');
    try {
      const res = await fetch('/api/analytics/live-screener/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbols: syms.length > 0 ? syms : undefined,
          universe: lsUniverse,
          exchange: lsExchange,
          timeframe: lsTimeframe,
          screen_id: lsScreenId,
          preset: lsScreenId,
          params: lsParams,
          provider: lsProvider === 'auto' ? undefined : lsProvider,
          backend: lsBackend,
          poll_interval_sec: Math.max(1, Number(lsIntervalSec) || 2),
        }),
      });
      const body = (await res.json().catch(() => ({}))) as LiveScreenerStatePayload & { detail?: string };
      if (!res.ok) {
        throw new Error(body.detail || 'Failed to start Live Screen.');
      }
      setLsState(body);
    } catch (err) {
      setLsError(err instanceof Error ? err.message : 'Failed to start Live Screen.');
    } finally {
      setLsBusy(false);
    }
  }

  async function handleStopLiveScreen() {
    setLsBusy(true);
    setLsError('');
    try {
      const res = await fetch('/api/analytics/live-screener/stop', { method: 'POST' });
      const body = (await res.json().catch(() => ({}))) as LiveScreenerStatePayload & { detail?: string };
      if (res.ok) {
        setLsState(body);
      }
    } catch (err) {
      setLsError(err instanceof Error ? err.message : 'Failed to stop Live Screen.');
    } finally {
      setLsBusy(false);
    }
  }

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
    const isMtfScreen = Boolean(activeScreenMeta?.supports_multi_timeframe || scScreenId === 'support_resistance');
    if (isMtfScreen && scSelectedTfs.length === 0) {
      setScError('Select at least one timeframe (5m, 15m, 1h, 1d, 1w) for the Multi-Timeframe scan.');
      return;
    }
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
          timeframes: isMtfScreen ? scSelectedTfs : undefined,
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
            onClick={() => void handleSyncNseCsvs()}
            disabled={syncingCsv}
            className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3 py-1.5 text-xs font-semibold text-emerald-300 transition-colors hover:border-emerald-400 hover:text-emerald-200 disabled:opacity-50"
            title="Download latest official Index & F&O constituent CSVs from NSE into data/universes/"
          >
            <Download className={`h-3.5 w-3.5 ${syncingCsv ? 'animate-bounce' : ''}`} />
            <span>{syncingCsv ? 'Syncing NSE CSVs…' : 'Sync NSE CSVs'}</span>
          </button>

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
              onClick={() => setActiveTab('live_screen')}
              className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold transition-colors ${
                activeTab === 'live_screen' ? 'bg-emerald-400 text-emerald-950' : 'text-slate-300 hover:text-slate-100'
              }`}
            >
              <Radio className={`h-3.5 w-3.5 ${lsState?.running ? 'animate-pulse text-emerald-400' : ''}`} />
              <span>Live Screen</span>
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

      {csvSyncBanner && (
        <div
          className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs ${
            csvSyncBanner.type === 'ok'
              ? 'border-emerald-800/70 bg-emerald-950/40 text-emerald-200'
              : 'border-amber-800/70 bg-amber-950/40 text-amber-200'
          }`}
        >
          <span className="font-mono">{csvSyncBanner.text}</span>
          <button
            type="button"
            onClick={() => setCsvSyncBanner(null)}
            className="text-slate-400 hover:text-slate-100"
          >
            Dismiss
          </button>
        </div>
      )}

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
                Symbol Group / Universe (NSE CSV)
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
                {(meta?.universes && meta.universes.length > 0
                  ? meta.universes
                  : [
                      { id: 'DEFAULT_WATCHLIST', label: 'Default Watchlist (10)', count: 10 },
                      { id: 'CUSTOM', label: 'Custom (Comma-Separated)', count: 0 },
                    ]
                ).map((u) => (
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
            {activeScreenMeta?.supports_multi_timeframe || scScreenId === 'support_resistance' ? (
              <div>
                <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
                  <span>Multi-Timeframe Scan (reuses 1m / 1d base)</span>
                  <button
                    type="button"
                    onClick={() =>
                      setScSelectedTfs(
                        scSelectedTfs.length === MTF_TIMEFRAME_OPTIONS.length
                          ? ['1day', '1week']
                          : MTF_TIMEFRAME_OPTIONS.map((t) => t.id)
                      )
                    }
                    className="text-[11px] font-semibold text-sky-400 hover:text-sky-300"
                  >
                    {scSelectedTfs.length === MTF_TIMEFRAME_OPTIONS.length ? 'D+W Only' : 'Select All 5'}
                  </button>
                </div>
                <div className="flex min-h-[38px] flex-wrap items-center gap-1.5 rounded-lg border border-slate-700 bg-[#0b1120] px-2 py-1">
                  {MTF_TIMEFRAME_OPTIONS.map((opt) => {
                    const active = scSelectedTfs.includes(opt.id);
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() => {
                          setScSelectedTfs((prev) =>
                            prev.includes(opt.id)
                              ? prev.filter((item) => item !== opt.id)
                              : [...prev, opt.id]
                          );
                        }}
                        className={`rounded px-2.5 py-1 font-mono text-xs font-semibold transition-colors ${
                          active
                            ? 'bg-sky-400 text-sky-950'
                            : 'border border-slate-700 bg-[#111827] text-slate-400 hover:text-slate-200'
                        }`}
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            ) : (
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
            )}

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
              {scResult.multi_timeframe || scResult.screen_id === 'support_resistance' ? (
                (() => {
                  const rawRows = scResult.results || [];
                  const q = srSearch.trim().toUpperCase();
                  const filteredRows = rawRows
                    .filter((r) => {
                      if (q && !String(r.symbol || '').toUpperCase().includes(q)) return false;
                      if (srEventFilter !== 'ALL' && String(r.event || '') !== srEventFilter) return false;
                      if (srTfFilter !== 'ALL' && String(r.timeframe || '') !== srTfFilter) return false;
                      if (srClassFilter !== 'ALL' && String(r.classification || '') !== srClassFilter) return false;
                      return true;
                    })
                    .sort((a, b) => {
                      if (srSortBy === 'distance_asc') {
                        return Math.abs(Number(a.distance_pct ?? 999)) - Math.abs(Number(b.distance_pct ?? 999));
                      }
                      if (srSortBy === 'rel_vol_desc') {
                        return Number(b.relative_volume ?? 0) - Number(a.relative_volume ?? 0);
                      }
                      if (srSortBy === 'touches_desc') {
                        return Number(b.touches ?? 0) - Number(a.touches ?? 0);
                      }
                      return Number(b.strength ?? 0) - Number(a.strength ?? 0);
                    });

                  const handleExportSrCsv = () => {
                    const headers = [
                      'Symbol',
                      'Exchange',
                      'F&O Eligible',
                      'Timeframe',
                      'Event',
                      'Level Type',
                      'Classification',
                      'Current Price',
                      'Zone Low',
                      'Zone High',
                      'Distance %',
                      'Strength',
                      'Touches',
                      'Reactions',
                      'Relative Volume',
                      'Confluence',
                    ];
                    const escapeCsv = (val: unknown) => {
                      const s = String(val ?? '');
                      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
                    };
                    const csvLines = [
                      headers.join(','),
                      ...filteredRows.map((r) =>
                        [
                          r.symbol,
                          r.exchange,
                          r.fno_eligible ? 'YES' : 'NO',
                          r.timeframe,
                          r.event,
                          r.level_type,
                          r.classification,
                          Number(r.current_price ?? r.close ?? 0).toFixed(2),
                          Number(r.zone_low ?? 0).toFixed(2),
                          Number(r.zone_high ?? 0).toFixed(2),
                          Number(r.distance_pct ?? 0).toFixed(2),
                          Number(r.strength ?? 0).toFixed(1),
                          r.touches ?? 0,
                          r.reactions ?? 0,
                          Number(r.relative_volume ?? 0).toFixed(2),
                          r.confluence ?? '',
                        ]
                          .map(escapeCsv)
                          .join(',')
                      ),
                    ];
                    const blob = new Blob([csvLines.join('\n')], { type: 'text/csv;charset=utf-8;' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `algodesk_sr_scan_${scResult.as_of_date || 'latest'}.csv`;
                    a.click();
                    URL.revokeObjectURL(url);
                  };

                  const majorCnt = scResult.major_count ?? rawRows.filter((r) => r.classification === 'Major').length;
                  const minorCnt = scResult.minor_count ?? rawRows.filter((r) => r.classification === 'Minor').length;
                  const missingList = scResult.missing_data || [];

                  return (
                    <>
                      {/* S&R Summary Bar */}
                      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-5">
                        <div className="rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2">
                          <div className="text-[11px] text-slate-400">Instruments Scanned</div>
                          <div className="font-mono text-base font-semibold text-slate-100">{scResult.scanned_count}</div>
                        </div>
                        <div className="rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2">
                          <div className="text-[11px] text-slate-400">Evaluated</div>
                          <div className="font-mono text-base font-semibold text-sky-300">{scResult.evaluated_count}</div>
                        </div>
                        <div className="rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2">
                          <div className="text-[11px] text-slate-400">Events Found</div>
                          <div className="font-mono text-base font-semibold text-emerald-300">
                            {scResult.events_found ?? rawRows.length}
                          </div>
                        </div>
                        <div className="rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2">
                          <div className="text-[11px] text-slate-400">Major / Minor</div>
                          <div className="font-mono text-base font-semibold text-slate-100">
                            <span className="text-emerald-400">{majorCnt}</span>
                            <span className="mx-1 text-slate-600">/</span>
                            <span className="text-amber-300">{minorCnt}</span>
                          </div>
                        </div>
                        <div className="rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2">
                          <div className="flex items-center justify-between text-[11px] text-slate-400">
                            <span>Missing TF Data</span>
                            {missingList.length > 0 && (
                              <button
                                type="button"
                                onClick={() => setSrShowMissing((v) => !v)}
                                className="text-[10px] font-semibold text-amber-300 hover:underline"
                              >
                                {srShowMissing ? 'Hide' : 'Details'}
                              </button>
                            )}
                          </div>
                          <div className={`font-mono text-base font-semibold ${missingList.length > 0 ? 'text-amber-300' : 'text-slate-300'}`}>
                            {scResult.missing_data_count ?? missingList.length}
                          </div>
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-slate-400">
                        <span>
                          {scResult.universe_label} · As Of <strong className="text-slate-200">{scResult.as_of_date}</strong>
                          {' · '}TFs: <strong className="text-sky-300">{(scResult.timeframes || []).join(', ') || scResult.timeframe}</strong>
                        </span>
                        <span>
                          Source: {scResult.provider.toUpperCase()} · Base: {scResult.base_timeframe || '1m+1d'} · API Calls: {scResult.api_calls_made} · Cached Days Skipped: {scResult.existing_days_skipped}
                        </span>
                      </div>

                      {/* Filter, Sort & CSV Export Controls */}
                      <div className="grid grid-cols-1 gap-2 rounded-lg border border-slate-800 bg-[#0b1120] p-2.5 sm:grid-cols-2 lg:grid-cols-[1.2fr_1.1fr_0.7fr_0.8fr_1fr_auto]">
                        <input
                          type="text"
                          value={srSearch}
                          onChange={(e) => setSrSearch(e.target.value)}
                          placeholder="Search symbol..."
                          aria-label="Filter S&R results by symbol"
                          className="min-h-[34px] rounded border border-slate-700 bg-[#111827] px-2.5 py-1 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                        />
                        <select
                          value={srEventFilter}
                          onChange={(e) => setSrEventFilter(e.target.value)}
                          aria-label="Filter by S&R event"
                          className="min-h-[34px] rounded border border-slate-700 bg-[#111827] px-2.5 py-1 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                        >
                          <option value="ALL">All Events</option>
                          {SR_EVENT_OPTIONS.map((ev) => (
                            <option key={ev} value={ev}>
                              {ev}
                            </option>
                          ))}
                        </select>
                        <select
                          value={srTfFilter}
                          onChange={(e) => setSrTfFilter(e.target.value)}
                          aria-label="Filter by timeframe"
                          className="min-h-[34px] rounded border border-slate-700 bg-[#111827] px-2.5 py-1 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                        >
                          <option value="ALL">All TFs</option>
                          {MTF_TIMEFRAME_OPTIONS.map((t) => (
                            <option key={t.label} value={t.label}>
                              {t.label}
                            </option>
                          ))}
                        </select>
                        <select
                          value={srClassFilter}
                          onChange={(e) => setSrClassFilter(e.target.value)}
                          aria-label="Filter by zone classification"
                          className="min-h-[34px] rounded border border-slate-700 bg-[#111827] px-2.5 py-1 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                        >
                          <option value="ALL">Major &amp; Minor</option>
                          <option value="Major">Major Only</option>
                          <option value="Minor">Minor Only</option>
                        </select>
                        <select
                          value={srSortBy}
                          onChange={(e) =>
                            setSrSortBy(e.target.value as 'strength_desc' | 'distance_asc' | 'rel_vol_desc' | 'touches_desc')
                          }
                          aria-label="Sort S&R results"
                          className="min-h-[34px] rounded border border-slate-700 bg-[#111827] px-2.5 py-1 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                        >
                          <option value="strength_desc">Sort: Strength ↓</option>
                          <option value="distance_asc">Sort: Nearest Distance ↑</option>
                          <option value="rel_vol_desc">Sort: Relative Volume ↓</option>
                          <option value="touches_desc">Sort: Touches ↓</option>
                        </select>
                        <button
                          type="button"
                          onClick={handleExportSrCsv}
                          disabled={filteredRows.length === 0}
                          className="inline-flex min-h-[34px] items-center justify-center gap-1.5 rounded border border-slate-700 bg-[#111827] px-3 py-1 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-40"
                        >
                          <Download className="h-3.5 w-3.5" />
                          <span>Export CSV ({filteredRows.length})</span>
                        </button>
                      </div>

                      {filteredRows.length === 0 ? (
                        <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-4 text-xs text-slate-400">
                          No Support &amp; Resistance events match the current filters as of {scResult.as_of_date}.
                        </div>
                      ) : (
                        <div className="max-h-[440px] overflow-auto rounded-lg border border-slate-800">
                          <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                            <thead className="sticky top-0 z-10 bg-[#0b1120] text-[11px] text-slate-400">
                              <tr className="border-b border-slate-800">
                                <th className="px-2.5 py-2 font-semibold">Symbol</th>
                                <th className="px-2 py-2 font-semibold">Exch</th>
                                <th className="px-2 py-2 font-semibold">F&amp;O</th>
                                <th className="px-2 py-2 font-semibold">TF</th>
                                <th className="px-2.5 py-2 font-semibold">Event</th>
                                <th className="px-2 py-2 font-semibold">Level Type</th>
                                <th className="px-2 py-2 font-semibold">Class</th>
                                <th className="px-2.5 py-2 text-right font-semibold">Current Price</th>
                                <th className="px-2.5 py-2 text-right font-semibold">Zone Low</th>
                                <th className="px-2.5 py-2 text-right font-semibold">Zone High</th>
                                <th className="px-2.5 py-2 text-right font-semibold">Distance %</th>
                                <th className="px-2 py-2 text-right font-semibold">Strength</th>
                                <th className="px-2 py-2 text-right font-semibold">Touches</th>
                                <th className="px-2 py-2 text-right font-semibold">Reactions</th>
                                <th className="px-2 py-2 text-right font-semibold">Rel Vol</th>
                                <th className="px-2.5 py-2 font-semibold">Confluence</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-800/80 bg-[#111827] text-slate-200">
                              {filteredRows.map((r, idx) => {
                                const distPct = Number(r.distance_pct ?? 0);
                                const strVal = Number(r.strength ?? 0);
                                const rvVal = Number(r.relative_volume ?? 0);
                                const isMajor = String(r.classification) === 'Major';
                                const isSupport = String(r.level_type) === 'Support';
                                return (
                                  <tr
                                    key={String(r.row_id || `${r.symbol}-${r.timeframe}-${r.zone_id}-${idx}`)}
                                    className="hover:bg-slate-900/50"
                                  >
                                    <td className="px-2.5 py-2 font-semibold text-slate-100">{r.symbol}</td>
                                    <td className="px-2 py-2 text-slate-400">{r.exchange}</td>
                                    <td className="px-2 py-2">
                                      <span className={r.fno_eligible ? 'text-sky-300' : 'text-slate-500'}>
                                        {r.fno_eligible ? 'YES' : 'NO'}
                                      </span>
                                    </td>
                                    <td className="px-2 py-2 font-semibold text-slate-200">{String(r.timeframe || '—')}</td>
                                    <td className="px-2.5 py-2">
                                      <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold ${srEventToneClass(String(r.event || ''))}`}>
                                        {String(r.event || '—')}
                                      </span>
                                    </td>
                                    <td className={`px-2 py-2 font-semibold ${isSupport ? 'text-emerald-400' : 'text-red-400'}`}>
                                      {String(r.level_type || '—')}
                                    </td>
                                    <td className="px-2 py-2">
                                      <span
                                        className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                                          isMajor
                                            ? 'bg-emerald-500/15 text-emerald-300'
                                            : 'bg-amber-500/15 text-amber-300'
                                        }`}
                                      >
                                        {String(r.classification || '—')}
                                      </span>
                                    </td>
                                    <td className="px-2.5 py-2 text-right">{Number(r.current_price ?? r.close ?? 0).toFixed(2)}</td>
                                    <td className="px-2.5 py-2 text-right text-slate-300">{Number(r.zone_low ?? 0).toFixed(2)}</td>
                                    <td className="px-2.5 py-2 text-right text-slate-300">{Number(r.zone_high ?? 0).toFixed(2)}</td>
                                    <td className={`px-2.5 py-2 text-right ${distPct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                      {distPct >= 0 ? '+' : ''}
                                      {distPct.toFixed(2)}%
                                    </td>
                                    <td className={`px-2 py-2 text-right font-semibold ${strVal >= 70 ? 'text-emerald-400' : 'text-amber-300'}`}>
                                      {strVal.toFixed(1)}
                                    </td>
                                    <td className="px-2 py-2 text-right">{Number(r.touches ?? 0)}</td>
                                    <td className="px-2 py-2 text-right">{Number(r.reactions ?? 0)}</td>
                                    <td className={`px-2 py-2 text-right ${r.volume_confirmed ? 'font-semibold text-sky-300' : 'text-slate-300'}`}>
                                      {rvVal.toFixed(2)}x
                                    </td>
                                    <td className="px-2.5 py-2 text-slate-300">{String(r.confluence || '—')}</td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}

                      {srShowMissing && missingList.length > 0 && (
                        <div className="max-h-[140px] overflow-auto rounded-lg border border-amber-800/60 bg-amber-950/30 p-3 font-mono text-[11px] text-amber-200">
                          <div className="mb-1 font-semibold">
                            Missing Timeframe Diagnostics ({missingList.length}) — never fabricated or substituted:
                          </div>
                          {missingList.slice(0, 40).map((m, i) => (
                            <div key={`${m.symbol}-${m.timeframe}-${i}`}>
                              {m.symbol} [{m.timeframe}]: {m.reason}
                            </div>
                          ))}
                          {missingList.length > 40 && <div>+{missingList.length - 40} more missing entries</div>}
                        </div>
                      )}
                    </>
                  );
                })()
              ) : (
                <>
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
                </>
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

      {/* TAB 1B: LIVE WEBSOCKET SCREEN (Historical Warmup -> Live Tick Inspection) */}
      {activeTab === 'live_screen' && (
        <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-6">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
            <div>
              <h3 className="text-sm font-semibold text-slate-100">
                Real-Time Live Screen (Historical Warmup + Live WebSocket Ticks)
              </h3>
              <p className="text-xs text-slate-400">
                Warms up indicator history once from canonical OHLCV cache, then continuously evaluates selected assets on live WebSocket ticks.
              </p>
            </div>
            <div className="flex items-center gap-2 font-mono text-[11px]">
              <span
                className={`rounded px-2 py-0.5 font-semibold ${
                  lsWsStatus === 'connected'
                    ? 'bg-emerald-500/15 text-emerald-300'
                    : lsWsStatus === 'connecting'
                      ? 'bg-amber-500/15 text-amber-300'
                      : 'bg-slate-800 text-slate-400'
                }`}
              >
                WS: {lsWsStatus.toUpperCase()}
              </span>
              <span
                className={`rounded px-2 py-0.5 font-semibold ${
                  lsState?.phase === 'streaming'
                    ? 'bg-emerald-500/20 text-emerald-300'
                    : lsState?.phase === 'warming_up'
                      ? 'bg-sky-500/20 text-sky-300'
                      : 'bg-slate-800 text-slate-400'
                }`}
              >
                {(lsState?.phase || 'idle').replace('_', ' ').toUpperCase()}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-1 items-end gap-3 md:grid-cols-2 lg:grid-cols-[0.95fr_1.35fr_1.05fr_0.85fr_auto]">
            <div>
              <label htmlFor="ls-universe" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Universe (NSE CSV)
              </label>
              <select
                id="ls-universe"
                value={lsUniverse}
                onChange={(e) => {
                  const val = e.target.value;
                  setLsUniverse(val);
                  if (val === 'CUSTOM' && !lsSymbols.trim()) {
                    setLsSymbols(DEFAULT_WATCHLIST);
                  } else if (val !== 'CUSTOM') {
                    setLsSymbols('');
                  }
                }}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
              >
                {(meta?.universes && meta.universes.length > 0
                  ? meta.universes
                  : [
                      { id: 'DEFAULT_WATCHLIST', label: 'Default Watchlist (10)', count: 10 },
                      { id: 'CUSTOM', label: 'Custom (Comma-Separated)', count: 0 },
                    ]
                ).map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="ls-symbols" className="mb-1.5 block text-xs font-semibold text-slate-300">
                {lsUniverse === 'CUSTOM'
                  ? 'Selected Assets (comma-separated)'
                  : `Extra Assets (appended to ${lsUniverse})`}
              </label>
              <input
                id="ls-symbols"
                type="text"
                value={lsSymbols}
                onChange={(e) => setLsSymbols(e.target.value.toUpperCase())}
                placeholder={
                  lsUniverse === 'CUSTOM' ? DEFAULT_WATCHLIST : `Optional extra symbols alongside ${lsUniverse}...`
                }
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="ls-screen" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Screen (.py plugin)
              </label>
              <select
                id="ls-screen"
                value={lsScreenId}
                onChange={(e) => setLsScreenId(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
              >
                {(meta?.screens && meta.screens.length > 0 ? meta.screens : FALLBACK_SCREENS).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="ls-tf-num" className="mb-1.5 block text-xs font-semibold text-slate-300">
                Timeframe
              </label>
              <div className="flex gap-1.5">
                <input
                  id="ls-tf-num"
                  type="number"
                  min={1}
                  max={lsTfUnit === 'minute' ? 1440 : 365}
                  step={1}
                  value={lsTfNum}
                  onChange={(e) => setLsTfNum(e.target.value)}
                  onBlur={() => {
                    const n = Number.parseInt(lsTfNum, 10);
                    setLsTfNum(String(Number.isFinite(n) && n >= 1 ? n : 1));
                  }}
                  aria-label="Live screen timeframe interval"
                  className="min-h-[40px] w-16 rounded-lg border border-slate-700 bg-[#0b1120] px-2 py-2 font-mono text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
                />
                <select
                  id="ls-tf-unit"
                  value={lsTfUnit}
                  onChange={(e) => setLsTfUnit(e.target.value === 'minute' ? 'minute' : 'day')}
                  aria-label="Live screen timeframe unit"
                  className="min-h-[40px] flex-1 rounded-lg border border-slate-700 bg-[#0b1120] px-2 py-2 text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
                >
                  <option value="minute">minute</option>
                  <option value="day">day</option>
                </select>
              </div>
            </div>

            <div className="flex items-center gap-2">
              {!lsState?.running ? (
                <button
                  type="button"
                  onClick={() => void handleStartLiveScreen()}
                  disabled={lsBusy}
                  className="inline-flex min-h-[40px] items-center justify-center gap-2 rounded-lg bg-emerald-400 px-4 py-2 text-xs font-semibold text-emerald-950 transition-colors hover:bg-emerald-300 disabled:opacity-50"
                >
                  <Play className="h-3.5 w-3.5" />
                  <span>{lsBusy ? 'Starting…' : 'Start Live Screen'}</span>
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void handleStopLiveScreen()}
                  disabled={lsBusy}
                  className="inline-flex min-h-[40px] items-center justify-center gap-2 rounded-lg border border-red-700 bg-red-950/60 px-4 py-2 text-xs font-semibold text-red-200 transition-colors hover:bg-red-900/70 disabled:opacity-50"
                >
                  <Square className="h-3.5 w-3.5" />
                  <span>Stop Live Screen</span>
                </button>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div>
              <label htmlFor="ls-provider" className="mb-1 block text-[11px] font-semibold text-slate-400">
                Data / Live Feed Provider
              </label>
              <select
                id="ls-provider"
                value={lsProvider}
                onChange={(e) => setLsProvider(e.target.value)}
                className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 py-1.5 text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
              >
                <option value="auto">Active ({activeDataConnection.toUpperCase()})</option>
                <option value="upstox">Upstox v3</option>
                <option value="zerodha">Zerodha Kite</option>
                <option value="yfinance">Yahoo Finance</option>
              </select>
            </div>
            <div>
              <label htmlFor="ls-backend" className="mb-1 block text-[11px] font-semibold text-slate-400">
                Indicator Engine
              </label>
              <select
                id="ls-backend"
                value={lsBackend}
                onChange={(e) => setLsBackend(e.target.value)}
                className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 py-1.5 text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
              >
                <option value="auto">Auto (TA-Lib / Built-in)</option>
                <option value="builtin">Built-in Pure O(N)</option>
                {meta?.indicator_backends?.talib && <option value="talib">TA-Lib C Engine</option>}
                {meta?.indicator_backends?.pandas_ta && <option value="pandas_ta">pandas-ta</option>}
              </select>
            </div>
            <div>
              <label htmlFor="ls-interval" className="mb-1 block text-[11px] font-semibold text-slate-400">
                Live Tick Cadence (sec)
              </label>
              <input
                id="ls-interval"
                type="number"
                min={1}
                max={30}
                step={1}
                value={lsIntervalSec}
                onChange={(e) => setLsIntervalSec(e.target.value)}
                className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 py-1.5 font-mono text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
              />
            </div>
            <div className="flex items-end justify-between gap-2">
              <div className="flex items-center gap-1.5">
                {['NSE', 'BSE'].map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => setLsExchange(ex)}
                    className={`min-h-[36px] rounded-lg border px-3 font-mono text-[11px] ${
                      lsExchange === ex
                        ? 'border-emerald-400 bg-emerald-400/15 text-emerald-300'
                        : 'border-slate-700 bg-[#0b1120] text-slate-400'
                    }`}
                  >
                    {ex}
                  </button>
                ))}
              </div>
              <label className="flex min-h-[36px] cursor-pointer items-center gap-1.5 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={lsMatchedOnly}
                  onChange={(e) => setLsMatchedOnly(e.target.checked)}
                  className="h-3.5 w-3.5 accent-emerald-400"
                />
                <span>Matched Only</span>
              </label>
            </div>
          </div>

          {activeLiveScreenMeta && activeLiveScreenMeta.params && activeLiveScreenMeta.params.length > 0 && (
            <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3.5">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  Live Screen Parameters — {activeLiveScreenMeta.name}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    const reset: Record<string, number | boolean | string> = {};
                    activeLiveScreenMeta.params.forEach((p) => {
                      reset[p.name] = p.default;
                    });
                    setLsParams(reset);
                  }}
                  className="text-[11px] font-semibold text-emerald-400 hover:text-emerald-300"
                >
                  Reset Defaults
                </button>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                {activeLiveScreenMeta.params.map((spec) => (
                  <div key={`ls-${spec.name}`}>
                    <label className="mb-1 block text-[11px] text-slate-400">{spec.label}</label>
                    {spec.type === 'bool' ? (
                      <label className="flex min-h-[34px] cursor-pointer items-center gap-2 rounded border border-slate-700 bg-[#111827] px-2.5 text-xs text-slate-200">
                        <input
                          type="checkbox"
                          checked={Boolean(lsParams[spec.name] ?? spec.default)}
                          onChange={(e) =>
                            setLsParams((prev) => ({ ...prev, [spec.name]: e.target.checked }))
                          }
                          className="h-3.5 w-3.5 accent-emerald-400"
                        />
                        <span>Enabled</span>
                      </label>
                    ) : (
                      <input
                        type={spec.type === 'str' ? 'text' : 'number'}
                        min={spec.min_val ?? undefined}
                        max={spec.max_val ?? undefined}
                        step={spec.step ?? (spec.type === 'int' ? 1 : 0.1)}
                        value={String(lsParams[spec.name] ?? spec.default)}
                        onChange={(e) =>
                          setLsParams((prev) => ({
                            ...prev,
                            [spec.name]: spec.type === 'str' ? e.target.value : Number(e.target.value),
                          }))
                        }
                        className="min-h-[34px] w-full rounded border border-slate-700 bg-[#111827] px-2.5 py-1 font-mono text-xs text-slate-100 focus:border-emerald-400 focus:outline-none"
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {lsError && (
            <div className="flex items-center gap-2 text-xs text-red-400">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{lsError}</span>
            </div>
          )}

          {lsState && (lsState.phase !== 'idle' || lsState.results.length > 0) && (
            <div className="space-y-3 border-t border-slate-800 pt-4">
              {lsState.phase === 'warming_up' && lsState.warmup && (
                <div className="space-y-1.5 rounded-lg border border-sky-800/60 bg-sky-950/30 p-3">
                  <div className="flex items-center justify-between font-mono text-xs text-sky-200">
                    <span>{lsState.warmup.message}</span>
                    <span>
                      {lsState.warmup.completed}/{lsState.warmup.total} ({lsState.warmup.pct}%)
                    </span>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                    <div
                      className="h-full bg-sky-400 transition-all duration-300"
                      style={{ width: `${Math.min(100, lsState.warmup.pct)}%` }}
                    />
                  </div>
                </div>
              )}

              <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs text-slate-400">
                <span>
                  Matched <strong className="text-emerald-300">{lsState.matched_count}</strong> of{' '}
                  <strong className="text-slate-200">{lsState.evaluated_count}</strong> ({lsState.universe_label || lsState.universe} ·{' '}
                  {lsState.screen_name || lsState.screen_id} · {lsState.timeframe})
                </span>
                <span>
                  Feed: <strong className="text-slate-200">{(lsState.live?.active_feed || lsState.provider || '—').toUpperCase()}</strong>
                  {lsState.live?.latency_ms != null ? ` · ${lsState.live.latency_ms} ms` : ''}
                  {' · '}Ticks: {lsState.live?.tick_seq ?? 0}
                  {lsState.live?.last_tick_ist ? ` · Last: ${lsState.live.last_tick_ist.slice(11, 19)} IST` : ''}
                </span>
              </div>

              {lsState.alerts && lsState.alerts.length > 0 && (
                <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                  <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      Live Signal Transitions ({lsState.alerts.length})
                      <span className="ml-2 font-normal normal-case tracking-normal text-slate-500">
                        — real-time log of symbols newly matching (ENTERED) or leaving (EXITED) the active screen
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={() => setLsShowAlerts((prev) => !prev)}
                      className="text-[11px] font-semibold text-sky-400 hover:text-sky-300"
                    >
                      {lsShowAlerts ? 'Hide' : 'Show'}
                    </button>
                  </div>
                  {lsShowAlerts && (
                    <div className="flex max-h-[84px] flex-wrap gap-2 overflow-auto font-mono text-[11px]">
                      {lsState.alerts.slice(0, 12).map((al) => (
                        <div
                          key={al.id}
                          className={`flex items-center gap-1.5 rounded border px-2 py-1 ${
                            al.transition === 'ENTERED'
                              ? 'border-emerald-700/70 bg-emerald-950/50 text-emerald-200'
                              : 'border-amber-700/70 bg-amber-950/50 text-amber-200'
                          }`}
                        >
                          <span className="font-bold">{al.transition}</span>
                          <span>{al.symbol}</span>
                          <span>@{al.ltp.toFixed(2)}</span>
                          <span className="text-slate-400">{al.ts_ist.slice(11, 19)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {(() => {
                const activeCols =
                  lsState.columns && lsState.columns.length > 0 ? lsState.columns : DEFAULT_FALLBACK_COLUMNS;
                const visibleRows = lsMatchedOnly
                  ? lsState.results.filter((r) => r.matched)
                  : lsState.results;
                if (visibleRows.length === 0) {
                  return (
                    <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-4 text-xs text-slate-400">
                      {lsState.phase === 'warming_up'
                        ? 'Warming up historical candles for initial indicator computation…'
                        : 'No matching assets right now. Live stream continues inspecting incoming WebSocket ticks.'}
                    </div>
                  );
                }
                return (
                  <div
                    ref={lsTableWrapRef}
                    className="max-h-[420px] overflow-auto rounded-lg border border-slate-800 [overflow-anchor:none]"
                  >
                    <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                      <thead className="sticky top-0 bg-[#0b1120] text-slate-400">
                        <tr className="border-b border-slate-800">
                          <th className="px-3 py-2.5 font-semibold">Symbol</th>
                          <th className="px-3 py-2.5 text-right font-semibold">Live LTP</th>
                          <th className="px-3 py-2.5 text-right font-semibold">Chg%</th>
                          {activeCols.map((col) => (
                            <th
                              key={`ls-col-${col.key}`}
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
                        {visibleRows.map((r) => {
                          const ltpVal = r.ltp ?? r.close;
                          const dirIcon =
                            r.tick_direction === 'up' ? '▲' : r.tick_direction === 'down' ? '▼' : '';
                          const dirClass =
                            r.tick_direction === 'up'
                              ? 'text-emerald-400'
                              : r.tick_direction === 'down'
                                ? 'text-red-400'
                                : 'text-slate-100';
                          return (
                            <tr
                              key={`ls-${r.exchange}-${r.symbol}`}
                              className={`hover:bg-slate-900/50 ${r.matched ? 'bg-emerald-950/15' : ''}`}
                            >
                              <td className="px-3 py-2 font-semibold text-slate-100">
                                <span
                                  className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${
                                    r.is_live ? 'bg-emerald-400' : 'bg-slate-500'
                                  }`}
                                  title={r.is_live ? `Live via ${r.live_source}` : 'Historical warmup baseline'}
                                />
                                {r.symbol}
                                <span className="ml-1.5 text-[10px] font-normal text-slate-500">
                                  {r.live_updated_ist ? r.live_updated_ist.slice(11, 19) : r.last_date}
                                </span>
                              </td>
                              <td className={`px-3 py-2 text-right font-semibold ${dirClass}`}>
                                {dirIcon ? `${dirIcon} ` : ''}
                                {Number(ltpVal).toFixed(2)}
                              </td>
                              <td className={`px-3 py-2 text-right ${r.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                {r.change_pct >= 0 ? '+' : ''}
                                {r.change_pct.toFixed(2)}%
                              </td>
                              {activeCols.map((col) => {
                                const cell = formatDynamicCell(r[col.key], col);
                                return (
                                  <td
                                    key={`ls-c-${r.symbol}-${col.key}`}
                                    className={`px-3 py-2 ${col.align === 'left' ? 'text-left' : 'text-right'} ${cell.toneClass}`}
                                  >
                                    {cell.text}
                                  </td>
                                );
                              })}
                              <td className="px-3 py-2">
                                <span className={r.matched ? 'font-semibold text-emerald-300' : 'text-slate-400'}>
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
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                );
              })()}
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
