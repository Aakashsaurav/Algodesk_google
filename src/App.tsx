import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, ExternalLink, X, Check, AlertCircle, Download, Search, Database, ShieldAlert, ShieldCheck, Activity } from 'lucide-react';
import { LiveTerminal } from './components/LiveTerminal';
import { AnalyticsConsole } from './components/AnalyticsConsole';

type ConnectionState =
  | 'CONNECTED'
  | 'DISCONNECTED'
  | 'CONNECTING'
  | 'EXPIRED'
  | 'UNCONFIGURED'
  | 'PLANNED'
  | 'ERROR';

interface BrokerCapabilities {
  supports_trading: boolean;
  supports_market_data: boolean;
  supports_websocket: boolean;
  requires_oauth: boolean;
}

interface BrokerSnapshot {
  broker_id: string;
  display_name: string;
  stage: 'active' | 'planned' | 'standby';
  state: ConnectionState;
  is_connected: boolean;
  capabilities: BrokerCapabilities;
  latency_ms: number | null;
  account_name: string;
  client_id: string;
  token_expires_at_ist: string | null;
  last_checked_at: string;
  message: string;
  can_reconnect: boolean;
  has_credentials: boolean;
  auth_mode: string;
  roadmap_note: string;
}

interface BrokerCredSummary {
  has_api_key: boolean;
  has_api_secret: boolean;
  api_key_preview: string;
  redirect_uri: string;
  key_source?: 'env' | 'local_vault' | 'keychain' | 'none';
  secret_source?: 'env' | 'local_vault' | 'keychain' | 'none';
  redirect_source?: 'env' | 'local_vault' | 'keychain' | 'none';
}

interface OverviewPayload {
  brokers: BrokerSnapshot[];
  selections: {
    trading_broker: string;
    data_connection: string;
    updated_at: string;
  };
  credentials: Record<string, BrokerCredSummary | undefined>;
  runtime: {
    python_version: string;
    machine: string;
    system: string;
    ist_time: string;
    eager_instruments_loaded: boolean;
  };
}

function formatCredentialSource(src?: string): string {
  if (src === 'env') return '.env';
  if (src === 'local_vault') return 'Local 0o600 Vault';
  if (src === 'keychain') return 'macOS Keychain';
  return 'Default';
}

interface InstrumentStats {
  db_path: string;
  db_size_mb: number;
  total_instruments: number;
  fno_instruments?: number;
  counts_by_broker: Record<string, number>;
}

interface InstrumentItem {
  broker_id: string;
  exchange: string;
  segment: string;
  trading_symbol: string;
  asset_symbol?: string;
  name: string;
  instrument_type: string;
  instrument_key: string;
  instrument_token: string | null;
  expiry?: string | null;
  strike?: number | null;
  lot_size?: number;
  tick_size?: number;
}

interface FnoContractsPayload {
  underlying: string;
  exchange: string;
  contract_type: string;
  underlyings: string[];
  expiries: string[];
  strikes: number[];
  contracts: InstrumentItem[];
  count: number;
}

interface OhlcvRow {
  ts_ist: string;
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  oi: number;
  source: string;
}

interface DataRecordsPayload {
  symbol: string;
  exchange: string;
  timeframe: string;
  view_mode: 'snapshot' | 'full';
  total_rows: number;
  returned_rows: number;
  is_truncated: boolean;
  summary: {
    start_ts: string | null;
    end_ts: string | null;
    min_low: number | null;
    max_high: number | null;
    latest_close: number | null;
    change_pct: number | null;
    total_volume: number;
    latest_oi?: number;
  };
  rows: OhlcvRow[];
}

interface DownloadResultPayload {
  ok: boolean;
  symbol: string;
  exchange: string;
  timeframe: string;
  provider: string;
  total_weekdays_in_range: number;
  existing_days_skipped: number;
  missing_days_count: number;
  missing_ranges_fetched: Array<{ from: string; to: string }>;
  api_calls_made: number;
  rows_inserted: number;
  cache_hit_only: boolean;
  message: string;
  snapshot: DataRecordsPayload;
}

interface CatalogDataset {
  symbol: string;
  exchange: string;
  timeframe: string;
  row_count: number;
  start_date: string;
  end_date: string;
  provider: string;
}

interface FeedSlotHealth {
  feed_id: string;
  display_name: string;
  role: 'primary' | 'standby';
  transport: string;
  status: 'CONNECTED' | 'STANDBY' | 'FAILOVER' | 'DISCONNECTED';
  active: boolean;
  ticks_received: number;
  error_count: number;
  reconnect_count: number;
  latency_ms: number | null;
  last_tick_ist: string | null;
  last_error: string;
}

interface LiveQuote {
  symbol: string;
  exchange?: string;
  ltp: number | null;
  change_pct: number | null;
  source: string;
  note?: string;
  ts_ist: string;
}

interface FeedManagerStatus {
  primary_feed: string;
  active_feed: string;
  in_failover: boolean;
  forced_failover: boolean;
  failover_count: number;
  last_failover_reason: string;
  watchlist: string[];
  slots: FeedSlotHealth[];
  quotes: LiveQuote[];
}

interface KillSwitchState {
  armed: boolean;
  engaged: boolean;
  reason: string;
  engaged_at_ist: string | null;
  actor: string;
}

interface AuditVerifyResult {
  valid: boolean;
  total_events: number;
  broken_index: number | null;
  broken_event_id: string | null;
  reason: string;
  head_hash: string;
}

interface AuditEvent {
  id: string;
  ts_ist: string;
  event_type: string;
  actor: string;
  payload: Record<string, unknown>;
  payload_hash: string;
  prev_hash: string;
  chain_hash: string;
}

interface LiveStatusPayload {
  ok: boolean;
  feeds: FeedManagerStatus;
  kill_switch: KillSwitchState;
  audit_verify: AuditVerifyResult;
  audit_events: AuditEvent[];
}

interface PreflightCheck {
  id: string;
  name: string;
  passed: boolean;
  status: 'PASS' | 'WARN' | 'FAIL';
  severity: 'ERROR' | 'WARNING';
  message: string;
}

interface PreflightPayload {
  ok: boolean;
  overall_ready: boolean;
  passed_count: number;
  total_count: number;
  blocking_failures: number;
  checked_at_ist: string;
  checks: PreflightCheck[];
}

const ROADMAP_PHASES = [
  {
    phase: 'Phase 01',
    title: 'Hardened Upstox Hub & Security Fixes',
    detail:
      'Fixed Upstox logout endpoint, IST 03:30 AM daily reset boundary validation, 0o600 local vault secret storage (opt-in Keychain), and decoupled M2-friendly broker health probing.',
  },
  {
    phase: 'Phase 02',
    title: 'Zerodha Kite Connect Adapter (Active)',
    detail:
      'Implemented Kite OAuth SHA-256 checksum login, 06:00 AM IST boundary validation, lazy KiteTicker WebSocket feed, and active BrokerRegistry routing.',
  },
  {
    phase: 'Phase 03',
    title: 'SQLite Instrument Index & Gap-Only Data Hub (Active)',
    detail:
      'Indexed SQLite instrument master under 5 MB RSS, consolidated canonical core runtime interfaces, gap-only historical OHLCV downloads (0 API calls for cached dates), and Snapshot/Full data inspection.',
  },
  {
    phase: 'Phase 04',
    title: 'Hot-Standby Feed Failover & Kill Switch (Active)',
    detail:
      'Hot-standby primary-to-fallback market feed failover, 6-gate preflight safety & margin checks, persisted emergency kill switch, and SEBI SHA-256 hash-chained audit ledger.',
  },
  {
    phase: 'Phase 05',
    title: 'Screener, Backtester & Risk Console (Active)',
    detail:
      'Pure O(N) asynchronous multi-symbol technical screener, strategy backtester with equity/drawdown curve, and persistent portfolio risk limits integrated with live broker exposure and kill switch.',
  },
];

function getStatusLabel(state: ConnectionState, isProbing: boolean): string {
  if (isProbing) return 'Checking';
  switch (state) {
    case 'CONNECTED':
      return 'Connected';
    case 'DISCONNECTED':
      return 'Disconnected';
    case 'EXPIRED':
      return 'Token Expired';
    case 'UNCONFIGURED':
      return 'Credentials Needed';
    case 'PLANNED':
      return 'Planned (Phase 2)';
    case 'CONNECTING':
      return 'Connecting';
    default:
      return 'Error';
  }
}

function getLedColorClass(state: ConnectionState, isProbing: boolean): string {
  if (isProbing || state === 'CONNECTING') {
    return 'bg-amber-600 shadow-[0_0_10px_rgba(217,119,6,0.7)]';
  }
  switch (state) {
    case 'CONNECTED':
      return 'bg-emerald-600 shadow-[0_0_10px_rgba(22,163,74,0.7)]';
    case 'PLANNED':
      return 'bg-slate-600';
    default:
      return 'bg-red-600 shadow-[0_0_10px_rgba(220,38,38,0.65)]';
  }
}

export default function App() {
  const [routePath, setRoutePath] = useState<string>(() =>
    typeof window !== 'undefined' ? window.location.pathname.replace(/\/+$/, '') || '/' : '/'
  );
  const [overview, setOverview] = useState<OverviewPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    const onPop = () => setRoutePath(window.location.pathname.replace(/\/+$/, '') || '/');
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const navigateTo = useCallback((nextPath: string) => {
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, '', nextPath);
    }
    setRoutePath(nextPath);
    window.scrollTo({ top: 0, behavior: 'auto' });
  }, []);

  const [tradingSelection, setTradingSelection] = useState('upstox');
  const [dataSelection, setDataSelection] = useState('upstox');
  const [savingRouting, setSavingRouting] = useState(false);
  const [routingFeedback, setRoutingFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const [probingIds, setProbingIds] = useState<Record<string, boolean>>({});

  // Reconnect / OAuth modal state
  const [modalBrokerId, setModalBrokerId] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [redirectUri, setRedirectUri] = useState('');
  const [credsExpanded, setCredsExpanded] = useState(false);
  const [oauthUrl, setOauthUrl] = useState('');
  const [captureActive, setCaptureActive] = useState(false);
  const [captureStatus, setCaptureStatus] = useState<{
    status: string;
    mode?: string;
    redirectUri?: string;
    message: string;
  } | null>(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [callbackInput, setCallbackInput] = useState('');
  const [modalFeedback, setModalFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [exchanging, setExchanging] = useState(false);

  // Phase 3: SQLite Instrument Index & Gap-Only Historical Data state
  const todayIso = new Date().toISOString().slice(0, 10);
  const thirtyDaysAgoIso = new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10);

  const [instrQuery, setInstrQuery] = useState('');
  const [instrTypeFilter, setInstrTypeFilter] = useState('ALL');
  const [instrResults, setInstrResults] = useState<InstrumentItem[]>([]);
  const [instrStats, setInstrStats] = useState<InstrumentStats | null>(null);
  const [syncingMaster, setSyncingMaster] = useState<string | null>(null);
  const [instrFeedback, setInstrFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // F&O Contract Finder state
  const [fnoOpen, setFnoOpen] = useState(false);
  const [fnoExchange, setFnoExchange] = useState('NFO');
  const [fnoUnderlying, setFnoUnderlying] = useState('');
  const [fnoContractType, setFnoContractType] = useState('FUT');
  const [fnoExpiry, setFnoExpiry] = useState('');
  const [fnoStrike, setFnoStrike] = useState('');
  const [fnoData, setFnoData] = useState<FnoContractsPayload | null>(null);
  const [loadingFno, setLoadingFno] = useState(false);

  const [histSymbol, setHistSymbol] = useState('RELIANCE');
  const [histExchange, setHistExchange] = useState('NSE');
  const [histContractType, setHistContractType] = useState('');
  const [histInstrumentKey, setHistInstrumentKey] = useState('');
  const [histTimeframe, setHistTimeframe] = useState('1day');
  const [histFrom, setHistFrom] = useState(thirtyDaysAgoIso);
  const [histTo, setHistTo] = useState(todayIso);
  const [histProvider, setHistProvider] = useState('auto');
  const [downloadingHist, setDownloadingHist] = useState(false);
  const [downloadAudit, setDownloadAudit] = useState<DownloadResultPayload | null>(null);
  const [dataFeedback, setDataFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const [viewMode, setViewMode] = useState<'snapshot' | 'full'>('snapshot');
  const [dataRecords, setDataRecords] = useState<DataRecordsPayload | null>(null);
  const [loadingRecords, setLoadingRecords] = useState(false);
  const [catalog, setCatalog] = useState<CatalogDataset[]>([]);

  // Phase 4: Hot-Standby Feed Failover, Preflight & Kill Switch state
  const [liveStatus, setLiveStatus] = useState<LiveStatusPayload | null>(null);
  const [feedActionLoading, setFeedActionLoading] = useState(false);
  const [feedFeedback, setFeedFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [preflight, setPreflight] = useState<PreflightPayload | null>(null);
  const [runningPreflight, setRunningPreflight] = useState(false);
  const [killReason, setKillReason] = useState('');
  const [killBusy, setKillBusy] = useState(false);
  const [verifyingAudit, setVerifyingAudit] = useState(false);

  const fetchOverview = useCallback(async (refresh = true): Promise<OverviewPayload | null> => {
    setRefreshing(true);
    setLoadError(null);
    try {
      const res = await fetch(`/api/overview?refresh=${refresh}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Server error (${res.status})`);
      }
      const data: OverviewPayload = await res.json();
      setOverview(data);
      setTradingSelection(data.selections.trading_broker);
      setDataSelection(data.selections.data_connection);
      return data;
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Unable to reach Python broker service.');
      return null;
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const instrSearchSeq = useRef(0);

  const searchInstruments = useCallback(async (q: string, instType = 'ALL') => {
    const seq = ++instrSearchSeq.current;
    try {
      const params = new URLSearchParams({ q, limit: '16' });
      if (instType && instType !== 'ALL') {
        params.set('instrument_type', instType);
      }
      const res = await fetch(`/api/instruments/search?${params.toString()}`);
      if (seq !== instrSearchSeq.current) return;
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const msg =
          res.status === 404
            ? 'Instrument lookup endpoint not found on running Python hub — restart the server to load Phase 3 routes.'
            : err.detail || `Instrument search failed (HTTP ${res.status}).`;
        setInstrFeedback({ type: 'err', text: msg });
        return;
      }
      const body = await res.json();
      if (seq !== instrSearchSeq.current) return;
      const rows = body.results || [];
      setInstrResults(rows);
      if (body.stats) setInstrStats(body.stats);
      if (q.trim() && rows.length === 0) {
        setInstrFeedback({
          type: 'err',
          text: `No matching ${instType === 'ALL' ? '' : `${instType} `}symbol for "${q.trim().toUpperCase()}" in local SQLite yet. Click Sync Upstox Master or Sync Zerodha Master.`,
        });
      } else {
        setInstrFeedback((prev) => (prev?.type === 'err' ? null : prev));
      }
    } catch {
      // ignore transient startup polling error
    }
  }, []);

  const fnoFetchSeq = useRef(0);

  const fetchFnoContracts = useCallback(
    async (
      opts?: {
        underlying?: string;
        exchange?: string;
        contractType?: string;
        expiry?: string;
        strike?: string;
      },
      syncDownloadTarget = false
    ) => {
      const seq = ++fnoFetchSeq.current;
      const ul = opts?.underlying ?? fnoUnderlying;
      const exch = opts?.exchange ?? fnoExchange;
      const ctype = opts?.contractType ?? fnoContractType;
      const exp = opts?.expiry ?? fnoExpiry;
      const stk = opts?.strike ?? fnoStrike;
      setLoadingFno(true);
      try {
        const params = new URLSearchParams({
          underlying: ul.trim().toUpperCase(),
          exchange: exch || 'NFO',
          contract_type: ctype || 'FO',
          limit: '40',
        });
        if (exp) params.set('expiry', exp);
        if (stk && !Number.isNaN(Number(stk))) params.set('strike', stk);
        const res = await fetch(`/api/instruments/fno?${params.toString()}`);
        if (seq !== fnoFetchSeq.current || !res.ok) return;
        const payload: FnoContractsPayload = await res.json();
        if (seq !== fnoFetchSeq.current) return;
        setFnoData(payload);

        if (syncDownloadTarget) {
          const contracts = payload.contracts || [];
          const preferredBroker = histProvider === 'auto' ? dataSelection : histProvider;
          const exactType = ctype && ['FUT', 'CE', 'PE'].includes(ctype.toUpperCase()) ? ctype.toUpperCase() : '';
          const match =
            contracts.find(
              (c) =>
                c.broker_id === preferredBroker &&
                (!exactType || c.instrument_type.toUpperCase() === exactType) &&
                (!exp || c.expiry === exp) &&
                (!stk || String(c.strike ?? '') === stk)
            ) ||
            contracts.find(
              (c) =>
                (!exactType || c.instrument_type.toUpperCase() === exactType) &&
                (!exp || c.expiry === exp) &&
                (!stk || String(c.strike ?? '') === stk)
            ) ||
            contracts.find((c) => c.broker_id === preferredBroker) ||
            contracts[0];

          if (match) {
            setHistSymbol(match.trading_symbol);
            setHistExchange(match.exchange || exch || 'NFO');
            setHistContractType(match.instrument_type || exactType);
            setHistInstrumentKey(match.instrument_key || '');
          } else {
            setHistExchange(exch || 'NFO');
            setHistContractType(exactType);
            setHistInstrumentKey('');
          }
        }
      } catch {
        // ignore transient error
      } finally {
        if (seq === fnoFetchSeq.current) setLoadingFno(false);
      }
    },
    [dataSelection, fnoContractType, fnoExchange, fnoExpiry, fnoStrike, fnoUnderlying, histProvider]
  );

  const fetchCatalog = useCallback(async () => {
    try {
      const res = await fetch('/api/data/catalog');
      if (!res.ok) return;
      const body = await res.json();
      setCatalog(body.datasets || []);
    } catch {
      // ignore
    }
  }, []);

  const fetchDataRecords = useCallback(
    async (
      sym: string,
      exch: string,
      tf: string,
      mode: 'snapshot' | 'full',
      fromD?: string,
      toD?: string
    ) => {
      if (!sym.trim()) return;
      setLoadingRecords(true);
      try {
        const params = new URLSearchParams({
          symbol: sym.trim().toUpperCase(),
          exchange: exch,
          timeframe: tf,
          view: mode,
        });
        if (fromD) params.set('from_date', fromD);
        if (toD) params.set('to_date', toD);
        const res = await fetch(`/api/data/records?${params.toString()}`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || 'Could not load stored records.');
        }
        const payload: DataRecordsPayload = await res.json();
        setDataRecords(payload);
      } catch (err) {
        setDataFeedback({
          type: 'err',
          text: err instanceof Error ? err.message : 'Could not load records.',
        });
      } finally {
        setLoadingRecords(false);
      }
    },
    []
  );

  const fetchLiveStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/live/status');
      if (!res.ok) return;
      const body: LiveStatusPayload = await res.json();
      setLiveStatus(body);
    } catch {
      // ignore transient startup polling error
    }
  }, []);

  useEffect(() => {
    void fetchOverview(true);
    void searchInstruments('', 'ALL');
    void fetchCatalog();
    void fetchLiveStatus();
  }, [fetchOverview, searchInstruments, fetchCatalog, fetchLiveStatus]);

  const handlePollFeeds = async () => {
    setFeedActionLoading(true);
    setFeedFeedback(null);
    try {
      const res = await fetch('/api/live/feeds/poll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Live quote poll failed.');
      setLiveStatus((prev) => ({
        ok: true,
        feeds: body,
        kill_switch: prev?.kill_switch ?? { armed: true, engaged: false, reason: '', engaged_at_ist: null, actor: 'system' },
        audit_verify: body.audit_verify ?? prev?.audit_verify,
        audit_events: body.audit_events ?? prev?.audit_events ?? [],
      }));
      setFeedFeedback({ type: body.ok ? 'ok' : 'err', text: body.message || 'Feed polled.' });
    } catch (err) {
      setFeedFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Feed poll failed.' });
    } finally {
      setFeedActionLoading(false);
    }
  };

  const handleTriggerFailover = async () => {
    setFeedActionLoading(true);
    setFeedFeedback(null);
    try {
      const res = await fetch('/api/live/feeds/failover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'Operator hot-standby failover drill from dashboard' }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Failover trigger failed.');
      setLiveStatus((prev) => ({
        ok: true,
        feeds: body,
        kill_switch: prev?.kill_switch ?? { armed: true, engaged: false, reason: '', engaged_at_ist: null, actor: 'system' },
        audit_verify: body.audit_verify ?? prev?.audit_verify,
        audit_events: body.audit_events ?? prev?.audit_events ?? [],
      }));
      setFeedFeedback({ type: 'ok', text: body.message || 'Failover triggered.' });
    } catch (err) {
      setFeedFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Failover failed.' });
    } finally {
      setFeedActionLoading(false);
    }
  };

  const handleRecoverPrimary = async () => {
    setFeedActionLoading(true);
    setFeedFeedback(null);
    try {
      const res = await fetch('/api/live/feeds/recover', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Primary recovery failed.');
      setLiveStatus((prev) => ({
        ok: true,
        feeds: body,
        kill_switch: prev?.kill_switch ?? { armed: true, engaged: false, reason: '', engaged_at_ist: null, actor: 'system' },
        audit_verify: body.audit_verify ?? prev?.audit_verify,
        audit_events: body.audit_events ?? prev?.audit_events ?? [],
      }));
      setFeedFeedback({ type: body.ok ? 'ok' : 'err', text: body.message || 'Primary restore attempted.' });
    } catch (err) {
      setFeedFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Recovery failed.' });
    } finally {
      setFeedActionLoading(false);
    }
  };

  const handleRunPreflight = async () => {
    setRunningPreflight(true);
    try {
      const res = await fetch('/api/live/preflight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbols: [histSymbol.trim().toUpperCase() || 'RELIANCE', 'NIFTY'] }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Preflight check failed.');
      setPreflight(body as PreflightPayload);
      setLiveStatus((prev) =>
        prev
          ? {
              ...prev,
              kill_switch: body.kill_switch ?? prev.kill_switch,
              audit_verify: body.audit_verify ?? prev.audit_verify,
              audit_events: body.audit_events ?? prev.audit_events,
            }
          : prev
      );
    } catch (err) {
      setFeedFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Preflight failed.' });
    } finally {
      setRunningPreflight(false);
    }
  };

  const handleToggleKillSwitch = async (engage: boolean) => {
    setKillBusy(true);
    try {
      const res = await fetch('/api/live/kill-switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          engaged: engage,
          reason:
            killReason.trim() ||
            (engage
              ? 'Emergency stop engaged by operator via AlgoDesk console'
              : 'Operator cleared kill switch after inspection'),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Kill switch update failed.');
      setLiveStatus(body as LiveStatusPayload);
      if (!engage) setKillReason('');
      await handleRunPreflight();
    } catch (err) {
      setFeedFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Kill switch update failed.' });
    } finally {
      setKillBusy(false);
    }
  };

  const handleVerifyAuditChain = async () => {
    setVerifyingAudit(true);
    try {
      const res = await fetch('/api/audit/verify?limit=20');
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Audit verification failed.');
      setLiveStatus((prev) =>
        prev
          ? {
              ...prev,
              audit_verify: body.verification,
              audit_events: body.events ?? prev.audit_events,
            }
          : prev
      );
    } finally {
      setVerifyingAudit(false);
    }
  };

  const handleSyncInstruments = async (brokerId: 'upstox' | 'zerodha') => {
    setSyncingMaster(brokerId);
    setInstrFeedback(null);
    try {
      const res = await fetch('/api/instruments/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ broker_id: brokerId }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || `Failed to sync ${brokerId} instruments.`);
      if (body.stats) setInstrStats(body.stats);
      setInstrFeedback({
        type: 'ok',
        text: `Indexed ${Number(body.indexed_count || 0).toLocaleString()} ${brokerId} instruments (Equity + F&O) into SQLite.`,
      });
      await searchInstruments(instrQuery, instrTypeFilter);
      if (fnoOpen) {
        await fetchFnoContracts();
      }
    } catch (err) {
      setInstrFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Instrument sync failed.',
      });
    } finally {
      setSyncingMaster(null);
    }
  };

  const handleSelectInstrument = (item: InstrumentItem) => {
    setHistSymbol(item.trading_symbol);
    setHistExchange(item.exchange);
    setHistContractType(item.instrument_type || '');
    setHistInstrumentKey(item.instrument_key || '');
    const uType = (item.instrument_type || '').toUpperCase();
    const isFnoItem =
      ['NFO', 'BFO', 'MCX', 'NSE_FO', 'BSE_FO', 'MCX_FO'].includes(item.exchange) ||
      ['FUT', 'CE', 'PE'].includes(uType) ||
      uType.startsWith('FUT') ||
      uType.startsWith('OPT');
    if (isFnoItem) {
      const nextEx = ['NFO', 'BFO', 'MCX'].includes(item.exchange) ? item.exchange : 'NFO';
      const nextUl = (item.asset_symbol || '').toUpperCase();
      const nextType = ['FUT', 'CE', 'PE'].includes(uType) ? uType : 'FO';
      const nextExp = item.expiry || '';
      const nextStrike = item.strike && item.strike > 0 ? String(item.strike) : '';
      setFnoOpen(true);
      setFnoExchange(nextEx);
      if (nextUl) setFnoUnderlying(nextUl);
      setFnoContractType(nextType);
      setFnoExpiry(nextExp);
      setFnoStrike(nextStrike);
      void fetchFnoContracts(
        {
          exchange: nextEx,
          underlying: nextUl,
          contractType: nextType,
          expiry: nextExp,
          strike: nextStrike,
        },
        false
      );
    }
    void fetchDataRecords(
      item.trading_symbol,
      item.exchange,
      histTimeframe,
      viewMode,
      histFrom,
      histTo
    );
  };

  const handleDownloadHistorical = async () => {
    if (!histSymbol.trim()) {
      setDataFeedback({ type: 'err', text: 'Enter a trading symbol or pick a contract before downloading.' });
      return;
    }
    setDownloadingHist(true);
    setDataFeedback(null);
    try {
      const res = await fetch('/api/data/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: histSymbol.trim().toUpperCase(),
          exchange: histExchange,
          timeframe: histTimeframe,
          from_date: histFrom,
          to_date: histTo,
          provider: histProvider === 'auto' ? null : histProvider,
          instrument_type: histContractType || null,
          instrument_key: histInstrumentKey || null,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg =
          res.status === 404
            ? 'Historical download endpoint not found on running Python hub — restart the server to load Phase 3 routes.'
            : body.detail || 'Historical download failed.';
        throw new Error(msg);
      }
      const payload = body as DownloadResultPayload;
      setDownloadAudit(payload);
      setDataFeedback({ type: 'ok', text: payload.message });
      await fetchCatalog();
      if (viewMode === 'snapshot' && payload.snapshot) {
        setDataRecords(payload.snapshot);
      } else {
        await fetchDataRecords(histSymbol, histExchange, histTimeframe, viewMode, histFrom, histTo);
      }
    } catch (err) {
      setDataFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Historical download failed.',
      });
    } finally {
      setDownloadingHist(false);
    }
  };

  const handleSwitchViewMode = async (mode: 'snapshot' | 'full') => {
    setViewMode(mode);
    await fetchDataRecords(histSymbol, histExchange, histTimeframe, mode, histFrom, histTo);
  };

  const handleProbeBroker = async (brokerId: string) => {
    setProbingIds((prev) => ({ ...prev, [brokerId]: true }));
    try {
      const res = await fetch(`/api/brokers/${brokerId}/probe`, { method: 'POST' });
      const data = await res.json();
      if (data.broker) {
        setOverview((prev) =>
          prev
            ? {
                ...prev,
                brokers: prev.brokers.map((b) => (b.broker_id === brokerId ? data.broker : b)),
              }
            : prev
        );
      }
    } finally {
      setProbingIds((prev) => ({ ...prev, [brokerId]: false }));
    }
  };

  const handleDisconnectBroker = async (brokerId: string) => {
    setProbingIds((prev) => ({ ...prev, [brokerId]: true }));
    try {
      const res = await fetch(`/api/brokers/${brokerId}/disconnect`, { method: 'POST' });
      const data = await res.json();
      if (data.broker) {
        setOverview((prev) =>
          prev
            ? {
                ...prev,
                brokers: prev.brokers.map((b) => (b.broker_id === brokerId ? data.broker : b)),
              }
            : prev
        );
      }
    } finally {
      setProbingIds((prev) => ({ ...prev, [brokerId]: false }));
    }
  };

  const handleSaveRouting = async () => {
    setSavingRouting(true);
    setRoutingFeedback(null);
    try {
      const res = await fetch('/api/selection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          trading_broker: tradingSelection,
          data_connection: dataSelection,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not save routing assignment.');
      setOverview((prev) => (prev ? { ...prev, selections: data.selections } : prev));
      setRoutingFeedback({
        type: 'ok',
        text: `Saved: Trading=${data.selections.trading_broker.toUpperCase()} · Data=${data.selections.data_connection.toUpperCase()}`,
      });
      void fetchLiveStatus();
    } catch (err) {
      setRoutingFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Save failed.',
      });
    } finally {
      setSavingRouting(false);
    }
  };

  const closeReconnectModal = useCallback(() => {
    if (captureActive && modalBrokerId) {
      void fetch(`/api/brokers/${modalBrokerId}/reconnect-cancel`, {
        method: 'POST',
        keepalive: true,
      }).catch(() => {});
    }
    setCaptureActive(false);
    setModalBrokerId(null);
  }, [captureActive, modalBrokerId]);

  const pollCaptureStatus = useCallback(async () => {
    if (!modalBrokerId || !captureActive) return;
    try {
      const res = await fetch(`/api/brokers/${modalBrokerId}/reconnect-status`);
      if (!res.ok) return;
      const st = await res.json();
      const label =
        overview?.brokers.find((b) => b.broker_id === modalBrokerId)?.display_name ||
        (modalBrokerId === 'zerodha' ? 'Zerodha Kite' : 'Upstox');
      if (st.status === 'listening' || st.status === 'exchanging') {
        setCaptureStatus({
          status: st.status,
          mode: st.capture_mode,
          redirectUri: st.redirect_uri,
          message: st.message || `Waiting for ${label} redirect...`,
        });
      } else if (st.status === 'connected') {
        setCaptureActive(false);
        setCaptureStatus({
          status: 'connected',
          mode: st.capture_mode,
          redirectUri: st.redirect_uri,
          message: st.message || `Connected automatically and verified with ${label}.`,
        });
        setModalFeedback({ type: 'ok', text: `Connected and verified against ${label} profile API.` });
        await fetchOverview(true);
        setTimeout(() => setModalBrokerId(null), 800);
      } else if (st.status === 'error' || st.status === 'timeout') {
        setCaptureActive(false);
        setCaptureStatus({
          status: st.status,
          mode: st.capture_mode,
          redirectUri: st.redirect_uri,
          message: st.message || 'Auto-capture did not complete.',
        });
        setManualOpen(true);
      }
    } catch {
      // Transient poll failure; ignore and retry on next interval
    }
  }, [captureActive, fetchOverview, modalBrokerId, overview]);

  useEffect(() => {
    if (!captureActive || !modalBrokerId) return;
    const timer = window.setInterval(() => {
      void pollCaptureStatus();
    }, 900);
    const onMessage = (event: MessageEvent) => {
      if (event?.data?.type === 'algodesk-oauth-captured') {
        void pollCaptureStatus();
      }
    };
    window.addEventListener('message', onMessage);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('message', onMessage);
    };
  }, [captureActive, modalBrokerId, pollCaptureStatus]);

  const openReconnectModal = async (brokerId: string) => {
    setCaptureActive(false);
    setCaptureStatus(null);
    setModalBrokerId(brokerId);
    setApiKey('');
    setApiSecret('');
    setOauthUrl('');
    setCallbackInput('');
    setModalFeedback(null);

    const fresh = await fetchOverview(false);
    const creds = fresh?.credentials?.[brokerId] ?? overview?.credentials?.[brokerId];
    const hasAll = Boolean(creds?.has_api_key && creds?.has_api_secret);
    setCredsExpanded(!hasAll);
    setManualOpen(false);
    setRedirectUri(creds?.redirect_uri || `${window.location.origin}/auth/${brokerId}/callback`);
  };

  const handleSaveCredentials = async () => {
    if (!modalBrokerId) return;
    const label =
      overview?.brokers.find((b) => b.broker_id === modalBrokerId)?.display_name ||
      (modalBrokerId === 'zerodha' ? 'Zerodha Kite' : 'Upstox');
    setModalFeedback(null);
    try {
      const res = await fetch(`/api/brokers/${modalBrokerId}/credentials`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
          redirect_uri: redirectUri.trim() || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Credential save failed.');
      await fetchOverview(false);
      setCredsExpanded(false);
      setModalFeedback({
        type: 'ok',
        text: `Credentials saved securely. Click Login with ${label} (Auto-Capture).`,
      });
    } catch (err) {
      setModalFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Failed to save credentials.',
      });
    }
  };

  const handleStartOAuth = async () => {
    if (!modalBrokerId) return;
    setModalFeedback(null);

    if (apiKey.trim() && apiSecret.trim()) {
      const saveRes = await fetch(`/api/brokers/${modalBrokerId}/credentials`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
          redirect_uri: redirectUri.trim() || null,
        }),
      });
      if (!saveRes.ok) {
        const errBody = await saveRes.json().catch(() => ({}));
        setModalFeedback({ type: 'err', text: errBody.detail || 'Could not save credentials.' });
        return;
      }
      await fetchOverview(false);
    }

    try {
      const res = await fetch(`/api/brokers/${modalBrokerId}/reconnect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ redirect_uri: redirectUri.trim() || null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not start OAuth login.');
      if (data.mode === 'needs_credentials') {
        setCredsExpanded(true);
        setModalFeedback({ type: 'err', text: data.message });
        return;
      }
      if (data.login_url) {
        setOauthUrl(data.login_url);
        const httpsHint = String(data.redirect_uri || '').startsWith('https://127.0.0.1')
          ? ' If your browser warns about the self-signed localhost certificate on 127.0.0.1, click Advanced -> Proceed so capture completes automatically.'
          : '';
        setCaptureStatus({
          status: data.capture_mode === 'manual_only' ? 'manual_only' : 'listening',
          mode: data.capture_mode,
          redirectUri: data.redirect_uri,
          message: (data.message || 'Listening for browser callback...') + httpsHint,
        });
        window.open(data.login_url, 'algodesk_oauth_login', 'width=640,height=760');
        if (data.capture_mode === 'manual_only') {
          setManualOpen(true);
        } else {
          setCaptureActive(true);
        }
      }
    } catch (err) {
      setModalFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'OAuth launch failed.',
      });
    }
  };

  const handleExchangeCode = async () => {
    if (!modalBrokerId) return;
    const label =
      overview?.brokers.find((b) => b.broker_id === modalBrokerId)?.display_name ||
      (modalBrokerId === 'zerodha' ? 'Zerodha Kite' : 'Upstox');
    if (!callbackInput.trim()) {
      setModalFeedback({ type: 'err', text: 'Paste the redirected callback URL or authorization token first.' });
      return;
    }
    setExchanging(true);
    setModalFeedback(null);
    try {
      const res = await fetch(`/api/brokers/${modalBrokerId}/callback-exchange`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code_or_url: callbackInput.trim(),
          redirect_uri: redirectUri.trim() || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Authorization token exchange failed.');
      setCaptureActive(false);
      setCaptureStatus({ status: 'connected', message: `Connected and verified with ${label}.` });
      setModalFeedback({ type: 'ok', text: `Connected and verified against ${label} profile API.` });
      await fetchOverview(true);
      setTimeout(() => setModalBrokerId(null), 650);
    } catch (err) {
      setModalFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Token exchange failed.',
      });
    } finally {
      setExchanging(false);
    }
  };

  const brokers = overview?.brokers ?? [];
  const activeCreds = modalBrokerId ? overview?.credentials?.[modalBrokerId] : undefined;
  const modalBrokerLabel =
    brokers.find((b) => b.broker_id === modalBrokerId)?.display_name ||
    (modalBrokerId === 'zerodha' ? 'Zerodha Kite' : 'Upstox');

  if (routePath === '/terminal') {
    return <LiveTerminal onNavigateHome={() => navigateTo('/')} />;
  }

  return (
    <div className="min-h-screen bg-[#0b1120] text-slate-100">
      {/* 3-Zone Top Bar */}
      <header className="border-b border-slate-800 bg-[#0b1120]">
        <div className="mx-auto flex h-16 max-w-[1160px] items-center justify-between gap-8 px-6">
          <a href="#routing" className="shrink-0 whitespace-nowrap text-lg font-bold tracking-tight text-slate-100">
            AlgoDesk
          </a>

          <nav className="hidden items-center gap-7 md:flex" aria-label="Primary">
            <a
              href="#routing"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Routing
            </a>
            <a
              href="#connections"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Connections
            </a>
            <a
              href="#historical-data"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Historical Data
            </a>
            <a
              href="#live-controls"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Live &amp; Safety
            </a>
            <a
              href="#analytics-console"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Analytics
            </a>
            <a
              href="/terminal"
              onClick={(e) => {
                e.preventDefault();
                navigateTo('/terminal');
              }}
              className="shrink-0 whitespace-nowrap text-sm font-semibold text-sky-400 transition-colors hover:text-sky-300 hover:underline hover:underline-offset-8"
            >
              Live Terminal
            </a>
          </nav>

          <button
            type="button"
            onClick={() => void fetchOverview(true)}
            disabled={refreshing}
            className="inline-flex min-h-[40px] shrink-0 items-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-[13px] font-semibold text-sky-950 transition-colors hover:bg-sky-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-400 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            <span>{refreshing ? 'Refreshing' : 'Refresh Status'}</span>
          </button>
        </div>
      </header>

      <main className="mx-auto flex max-w-[1160px] flex-col gap-10 px-6 py-10 pb-16">
        {/* Page Heading */}
        <div className="space-y-2">
          <h1 className="text-[clamp(1.5rem,3vw,2rem)] font-bold tracking-tight text-slate-100 text-balance">
            Broker &amp; Data Connectivity
          </h1>
          <p className="max-w-[68ch] text-[15px] text-slate-400">
            Verify live broker sessions, recover disconnected OAuth links, and assign independent providers for order execution and market data.
          </p>
          {overview?.runtime && (
            <p className="pt-1 font-mono text-xs text-slate-500">
              Python {overview.runtime.python_version} · {overview.runtime.system} {overview.runtime.machine} ·{' '}
              {overview.runtime.ist_time} · Instrument Master: Lazy
            </p>
          )}
        </div>

        {loadError && (
          <div className="flex items-center justify-between gap-4 rounded-xl border border-red-900/70 bg-red-950/40 px-5 py-4 text-sm text-red-200">
            <div className="flex items-center gap-2.5">
              <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
              <span>{loadError}</span>
            </div>
            <button
              type="button"
              onClick={() => void fetchOverview(true)}
              className="shrink-0 whitespace-nowrap rounded-lg border border-red-700/60 px-3 py-1.5 text-xs font-semibold text-red-100 hover:bg-red-900/40"
            >
              Retry
            </button>
          </div>
        )}

        {/* Section 1: Active Connection Routing */}
        <section id="routing" className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold tracking-tight text-slate-100">Active Connection Routing</h2>
            <p className="text-sm text-slate-400">
              Assign one verified broker for order execution and one provider for historical and live market data.
            </p>
          </div>

          <div className="rounded-xl border border-slate-800 bg-[#111827] p-6">
            <div className="grid grid-cols-1 items-end gap-5 md:grid-cols-[1fr_1fr_auto]">
              <div>
                <label htmlFor="trading-select" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                  Trading Connection (Order Execution)
                </label>
                <select
                  id="trading-select"
                  value={tradingSelection}
                  onChange={(e) => setTradingSelection(e.target.value)}
                  disabled={loading}
                  className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                >
                  {brokers
                    .filter((b) => b.capabilities.supports_trading)
                    .map((b) => (
                      <option key={b.broker_id} value={b.broker_id} disabled={b.stage === 'planned'}>
                        {b.stage === 'planned'
                          ? `${b.display_name} — Planned (Phase 2)`
                          : `${b.display_name} (${getStatusLabel(b.state, false)})`}
                      </option>
                    ))}
                </select>
                <span className="mt-1.5 block text-xs text-slate-500">
                  Only brokers with active execution support can be selected.
                </span>
              </div>

              <div>
                <label htmlFor="data-select" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                  Data Connection (Market Feed &amp; OHLCV)
                </label>
                <select
                  id="data-select"
                  value={dataSelection}
                  onChange={(e) => setDataSelection(e.target.value)}
                  disabled={loading}
                  className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                >
                  {brokers
                    .filter((b) => b.capabilities.supports_market_data)
                    .map((b) => (
                      <option key={b.broker_id} value={b.broker_id} disabled={b.stage === 'planned'}>
                        {b.stage === 'planned'
                          ? `${b.display_name} — Planned (Phase 2)`
                          : `${b.display_name} (${getStatusLabel(b.state, false)})`}
                      </option>
                    ))}
                </select>
                <span className="mt-1.5 block text-xs text-slate-500">
                  Supports broker feeds or Yahoo Finance historical fallback.
                </span>
              </div>

              <button
                type="button"
                onClick={() => void handleSaveRouting()}
                disabled={loading || savingRouting}
                className="inline-flex min-h-[42px] shrink-0 items-center justify-center whitespace-nowrap rounded-lg bg-sky-400 px-5 py-2 text-sm font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
              >
                {savingRouting ? 'Saving...' : 'Save Assignment'}
              </button>
            </div>

            {routingFeedback && (
              <div
                className={`mt-3.5 flex items-center gap-2 text-[13px] ${
                  routingFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                }`}
                role="status"
                aria-live="polite"
              >
                {routingFeedback.type === 'ok' ? <Check className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                <span>{routingFeedback.text}</span>
              </div>
            )}
          </div>
        </section>

        {/* Section 2: Broker & Provider Directory */}
        <section id="connections" className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold tracking-tight text-slate-100">Broker &amp; Provider Directory</h2>
            <p className="text-sm text-slate-400">
              Live connection status probed directly against broker endpoints. Disconnected sessions provide an immediate Reconnect action.
            </p>
          </div>

          <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
            {loading ? (
              <div className="divide-y divide-slate-800">
                {[0, 1, 2].map((idx) => (
                  <div key={idx} className="grid grid-cols-1 gap-4 px-6 py-5 md:grid-cols-[1.4fr_1.1fr_1.1fr_auto]">
                    <div className="h-10 animate-pulse rounded bg-slate-800/70" />
                    <div className="h-10 animate-pulse rounded bg-slate-800/60" />
                    <div className="h-10 animate-pulse rounded bg-slate-800/50" />
                    <div className="h-10 w-28 animate-pulse rounded bg-slate-800/70" />
                  </div>
                ))}
              </div>
            ) : (
              <div className="divide-y divide-slate-800">
                {brokers.map((b) => {
                  const isProbing = Boolean(probingIds[b.broker_id]);
                  const capLabels: string[] = [];
                  if (b.capabilities.supports_trading) capLabels.push('Trading');
                  if (b.capabilities.supports_market_data) capLabels.push('Market Data');
                  if (b.capabilities.supports_websocket) capLabels.push('WebSocket');

                  const assignments: string[] = [];
                  if (overview?.selections.trading_broker === b.broker_id) assignments.push('Assigned: Trading');
                  if (overview?.selections.data_connection === b.broker_id) assignments.push('Assigned: Data');

                  return (
                    <div
                      key={b.broker_id}
                      className="grid grid-cols-1 items-center gap-5 px-6 py-5 md:grid-cols-[1.4fr_1.1fr_1.1fr_auto]"
                    >
                      <div>
                        <div className="text-base font-semibold text-slate-100">{b.display_name}</div>
                        <div className="mt-0.5 text-[13px] text-slate-400">
                          {capLabels.join(' · ')}
                          {assignments.length > 0 ? ` · ${assignments.join(' · ')}` : ''}
                        </div>
                        <div className="mt-1 text-[13px] text-slate-500">{b.message}</div>
                      </div>

                      <div className="space-y-1">
                        <div className="inline-flex items-center gap-2.5 text-sm font-semibold text-slate-100">
                          <span
                            className={`h-2.5 w-2.5 shrink-0 rounded-full ${getLedColorClass(b.state, isProbing)}`}
                            aria-hidden="true"
                          />
                          <span>{getStatusLabel(b.state, isProbing)}</span>
                        </div>
                        <div className="font-mono text-xs text-slate-400">
                          {b.account_name || 'Session Inactive'}
                          {b.client_id ? ` · ${b.client_id}` : ''}
                        </div>
                      </div>

                      <div className="space-y-0.5 font-mono text-xs text-slate-400">
                        <div>Probe: {b.latency_ms !== null ? `${b.latency_ms} ms` : '—'}</div>
                        <div>Reset: {b.token_expires_at_ist || 'N/A'}</div>
                        <div>
                          Checked: {b.last_checked_at ? b.last_checked_at.slice(11, 19) : '—'} IST
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center justify-start gap-2.5 md:justify-end">
                        {b.stage === 'planned' ? (
                          <button
                            type="button"
                            disabled
                            className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-slate-900 px-4 py-2 text-[13px] font-semibold text-slate-500 opacity-60"
                          >
                            Coming in Phase 2
                          </button>
                        ) : (
                          <>
                            <button
                              type="button"
                              onClick={() => void handleProbeBroker(b.broker_id)}
                              disabled={isProbing}
                              className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-[13px] font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:bg-slate-800 disabled:opacity-50"
                            >
                              {isProbing ? 'Checking...' : 'Test'}
                            </button>

                            {!b.is_connected && b.can_reconnect && (
                              <button
                                type="button"
                                onClick={() => openReconnectModal(b.broker_id)}
                                className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-[13px] font-semibold text-sky-950 transition-colors hover:bg-sky-300"
                              >
                                Reconnect
                              </button>
                            )}

                            {b.is_connected && b.capabilities.requires_oauth && (
                              <button
                                type="button"
                                onClick={() => void handleDisconnectBroker(b.broker_id)}
                                disabled={isProbing}
                                className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg border border-red-800/70 bg-transparent px-4 py-2 text-[13px] font-semibold text-red-300 transition-colors hover:bg-red-950/50 disabled:opacity-50"
                              >
                                Disconnect
                              </button>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        {/* Section 3: Phase 3 — SQLite Instrument Index & Gap-Only Historical Data */}
        <section id="historical-data" className="space-y-4">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-slate-100">
                Historical Data &amp; SQLite Instrument Index
              </h2>
              <p className="text-sm text-slate-400">
                Supports Equity, Index, Futures &amp; Options (NFO/BFO/MCX). Downloads only missing trading days and supports Snapshot or Full Data inspection.
              </p>
            </div>
            {instrStats && (
              <div className="font-mono text-xs tabular-nums text-slate-400">
                Indexed Symbols: {instrStats.total_instruments.toLocaleString()}
                {typeof instrStats.fno_instruments === 'number'
                  ? ` · F&O Contracts: ${instrStats.fno_instruments.toLocaleString()}`
                  : ''}{' '}
                · DB: {instrStats.db_size_mb} MB
              </div>
            )}
          </div>

          <div className="space-y-6 rounded-xl border border-slate-800 bg-[#111827] p-6">
            {/* Instrument Lookup & Master Sync */}
            <div className="space-y-3 border-b border-slate-800 pb-6">
              <div className="grid grid-cols-1 items-end gap-3 md:grid-cols-[1fr_200px_auto_auto]">
                <div>
                  <label htmlFor="instr-search" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    SQLite Instrument Lookup (Under 5 MB RAM)
                  </label>
                  <div className="relative">
                    <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-slate-500" />
                    <input
                      id="instr-search"
                      type="text"
                      value={instrQuery}
                      onChange={(e) => {
                        const val = e.target.value;
                        setInstrQuery(val);
                        void searchInstruments(val, instrTypeFilter);
                      }}
                      placeholder="Search Equity or F&O (e.g. RELIANCE, NIFTY FUT, NIFTY 24500 CE)..."
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] py-2 pr-3 pl-9 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>
                </div>

                <div>
                  <label htmlFor="instr-type-filter" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    Segment / Contract
                  </label>
                    <select
                      id="instr-type-filter"
                      value={instrTypeFilter}
                      onChange={(e) => {
                        const nextType = e.target.value;
                        setInstrTypeFilter(nextType);
                        void searchInstruments(instrQuery, nextType);
                        if (['FO', 'FUT', 'OPT', 'CE', 'PE'].includes(nextType)) {
                          const mappedType = nextType === 'OPT' ? 'CE' : nextType;
                          const guessedUl =
                            fnoUnderlying.trim() ||
                            (instrQuery.trim().split(/\s+/)[0] || '').toUpperCase();
                          setFnoOpen(true);
                          setFnoContractType(mappedType);
                          if (guessedUl && !fnoUnderlying) setFnoUnderlying(guessedUl);
                          void fetchFnoContracts(
                            {
                              underlying: guessedUl,
                              contractType: mappedType,
                              expiry: '',
                              strike: '',
                            },
                            true
                          );
                        }
                      }}
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="ALL">All Instruments</option>
                    <option value="EQUITY">Equity &amp; Index</option>
                    <option value="FO">Futures &amp; Options (F&amp;O)</option>
                    <option value="FUT">Futures (FUT)</option>
                    <option value="OPT">Options (CE / PE)</option>
                    <option value="CE">Call Options (CE)</option>
                    <option value="PE">Put Options (PE)</option>
                  </select>
                </div>

                <button
                  type="button"
                  onClick={() => void handleSyncInstruments('upstox')}
                  disabled={syncingMaster !== null}
                  className="inline-flex min-h-[42px] shrink-0 items-center gap-2 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-[13px] font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:bg-slate-800 disabled:opacity-50"
                >
                  <Database className="h-3.5 w-3.5" />
                  <span>{syncingMaster === 'upstox' ? 'Syncing Upstox...' : 'Sync Upstox Master'}</span>
                </button>

                <button
                  type="button"
                  onClick={() => void handleSyncInstruments('zerodha')}
                  disabled={syncingMaster !== null}
                  className="inline-flex min-h-[42px] shrink-0 items-center gap-2 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-[13px] font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:bg-slate-800 disabled:opacity-50"
                >
                  <Database className="h-3.5 w-3.5" />
                  <span>{syncingMaster === 'zerodha' ? 'Syncing Zerodha...' : 'Sync Zerodha Master'}</span>
                </button>
              </div>

              {instrFeedback && (
                <div
                  className={`flex items-center gap-2 text-[13px] ${
                    instrFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                  }`}
                >
                  {instrFeedback.type === 'ok' ? <Check className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                  <span>{instrFeedback.text}</span>
                </div>
              )}

              {instrResults.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  {instrResults.slice(0, 10).map((item) => {
                    const metaBits = [item.exchange];
                    if (item.instrument_type && item.instrument_type !== 'EQUITY') {
                      metaBits.push(item.instrument_type);
                    }
                    if (item.expiry) metaBits.push(`Exp ${item.expiry}`);
                    if (item.strike && item.strike > 0) metaBits.push(`@${item.strike}`);
                    return (
                      <button
                        key={`${item.broker_id}-${item.instrument_key}`}
                        type="button"
                        onClick={() => handleSelectInstrument(item)}
                        className={`min-h-[32px] shrink-0 whitespace-nowrap rounded-md border px-2.5 py-1 font-mono text-xs tabular-nums transition-colors ${
                          histSymbol === item.trading_symbol && histExchange === item.exchange
                            ? 'border-sky-400 bg-sky-400/15 text-sky-300'
                            : 'border-slate-700 bg-[#0b1120] text-slate-300 hover:border-slate-500'
                        }`}
                      >
                        {item.trading_symbol} · {metaBits.join(' · ')} ({item.broker_id})
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Futures & Options (F&O) Contract Finder */}
            <div className="space-y-4 border-b border-slate-800 pb-6">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-slate-100">
                    Futures &amp; Options Contract Finder (NFO / BFO / MCX)
                  </div>
                  <p className="text-xs text-slate-400">
                    Pick an underlying, contract type (FUT / CE / PE), expiry, and strike from local SQLite before downloading OHLCV + OI.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    const next = !fnoOpen;
                    setFnoOpen(next);
                    if (next) {
                      const targetEx = ['NFO', 'BFO', 'MCX'].includes(histExchange) ? histExchange : fnoExchange;
                      setFnoExchange(targetEx);
                      void fetchFnoContracts({ exchange: targetEx });
                    }
                  }}
                  className="min-h-[36px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:border-sky-400 hover:bg-slate-800"
                >
                  {fnoOpen ? 'Hide F&O Finder' : 'Open F&O Contract Finder'}
                </button>
              </div>

              {fnoOpen && (
                <div className="space-y-4 pt-1">
                  <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-2 lg:grid-cols-[0.8fr_1.1fr_0.9fr_1fr_1fr_auto]">
                    <div>
                      <label htmlFor="fno-exchange" className="mb-1.5 block text-xs font-semibold text-slate-300">
                        F&amp;O Exchange
                      </label>
                      <select
                        id="fno-exchange"
                        value={fnoExchange}
                        onChange={(e) => {
                          const ex = e.target.value;
                          setFnoExchange(ex);
                          setHistExchange(ex);
                          setFnoExpiry('');
                          setFnoStrike('');
                          void fetchFnoContracts({ exchange: ex, expiry: '', strike: '' }, true);
                        }}
                        className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        <option value="NFO">NFO (NSE F&amp;O)</option>
                        <option value="BFO">BFO (BSE F&amp;O)</option>
                        <option value="MCX">MCX (Commodities)</option>
                      </select>
                    </div>

                    <div>
                      <label htmlFor="fno-underlying" className="mb-1.5 block text-xs font-semibold text-slate-300">
                        Underlying
                      </label>
                      <input
                        id="fno-underlying"
                        type="text"
                        list="fno-underlying-list"
                        value={fnoUnderlying}
                        onChange={(e) => {
                          const ul = e.target.value.toUpperCase();
                          setFnoUnderlying(ul);
                          setFnoExpiry('');
                          setFnoStrike('');
                          void fetchFnoContracts({ underlying: ul, expiry: '', strike: '' }, true);
                        }}
                        placeholder="NIFTY, BANKNIFTY, RELIANCE..."
                        className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                      />
                      <datalist id="fno-underlying-list">
                        {(fnoData?.underlyings ?? []).map((ul) => (
                          <option key={ul} value={ul} />
                        ))}
                      </datalist>
                    </div>

                    <div>
                      <label htmlFor="fno-type" className="mb-1.5 block text-xs font-semibold text-slate-300">
                        Contract Type
                      </label>
                      <select
                        id="fno-type"
                        value={fnoContractType}
                        onChange={(e) => {
                          const ct = e.target.value;
                          setFnoContractType(ct);
                          const nextStrike = ct === 'FUT' ? '' : fnoStrike;
                          if (ct === 'FUT') setFnoStrike('');
                          void fetchFnoContracts({ contractType: ct, strike: nextStrike }, true);
                        }}
                        className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        <option value="FUT">Futures (FUT)</option>
                        <option value="CE">Call Options (CE)</option>
                        <option value="PE">Put Options (PE)</option>
                        <option value="FO">All F&amp;O</option>
                      </select>
                    </div>

                    <div>
                      <label htmlFor="fno-expiry" className="mb-1.5 block text-xs font-semibold text-slate-300">
                        Expiry Date
                      </label>
                      <select
                        id="fno-expiry"
                        value={fnoExpiry}
                        onChange={(e) => {
                          const exp = e.target.value;
                          setFnoExpiry(exp);
                          setFnoStrike('');
                          void fetchFnoContracts({ expiry: exp, strike: '' }, true);
                        }}
                        className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                      >
                        <option value="">All Expiries ({fnoData?.expiries.length ?? 0})</option>
                        {(fnoData?.expiries ?? []).map((exp) => (
                          <option key={exp} value={exp}>
                            {exp}
                          </option>
                        ))}
                      </select>
                    </div>

                    <div>
                      <label htmlFor="fno-strike" className="mb-1.5 block text-xs font-semibold text-slate-300">
                        Strike Price
                      </label>
                      <select
                        id="fno-strike"
                        value={fnoStrike}
                        disabled={fnoContractType === 'FUT'}
                        onChange={(e) => {
                          const stk = e.target.value;
                          setFnoStrike(stk);
                          void fetchFnoContracts({ strike: stk }, true);
                        }}
                        className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none disabled:opacity-50"
                      >
                        <option value="">
                          {fnoContractType === 'FUT'
                            ? 'N/A (Futures)'
                            : `All Strikes (${fnoData?.strikes.length ?? 0})`}
                        </option>
                        {(fnoData?.strikes ?? []).map((stk) => (
                          <option key={stk} value={String(stk)}>
                            {stk}
                          </option>
                        ))}
                      </select>
                    </div>

                    <button
                      type="button"
                      onClick={() => void fetchFnoContracts(undefined, true)}
                      disabled={loadingFno}
                      className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-2 text-xs font-semibold text-slate-200 hover:border-slate-500 hover:bg-slate-800 disabled:opacity-50"
                    >
                      {loadingFno ? 'Loading...' : 'Refresh Contracts'}
                    </button>
                  </div>

                  {fnoData && fnoData.contracts.length > 0 ? (
                    <div className="space-y-2">
                      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
                        <span>
                          Matching F&amp;O Contracts ({fnoData.count}) — click one to bind exact contract symbol &amp; instrument key:
                        </span>
                        {fnoData.underlyings.length > 0 && !fnoUnderlying && (
                          <span className="font-mono text-slate-500">
                            Underlyings: {fnoData.underlyings.slice(0, 6).join(', ')}
                          </span>
                        )}
                      </div>
                      <div className="flex max-h-[168px] flex-wrap gap-1.5 overflow-y-auto pt-0.5">
                        {fnoData.contracts.map((item) => (
                          <button
                            key={`fno-${item.broker_id}-${item.instrument_key}`}
                            type="button"
                            onClick={() => handleSelectInstrument(item)}
                            className={`min-h-[32px] shrink-0 whitespace-nowrap rounded-md border px-2.5 py-1 font-mono text-xs tabular-nums transition-colors ${
                              histSymbol === item.trading_symbol && histInstrumentKey === item.instrument_key
                                ? 'border-sky-400 bg-sky-400/15 text-sky-300'
                                : 'border-slate-700 bg-[#0b1120] text-slate-300 hover:border-slate-500'
                            }`}
                          >
                            {item.trading_symbol}
                            {item.expiry ? ` · Exp ${item.expiry}` : ''}
                            {item.strike && item.strike > 0 ? ` · ${item.strike}` : ''}
                            {item.lot_size && item.lot_size > 1 ? ` · Lot ${item.lot_size}` : ''} · {item.broker_id}
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-800/80 pt-3 text-xs text-slate-400">
                      <span>
                        No {fnoExchange} {fnoContractType} contracts found in SQLite for the current filter. Sync Upstox or Zerodha instrument master to load live Futures &amp; Options contracts.
                      </span>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => void handleSyncInstruments('upstox')}
                          disabled={syncingMaster !== null}
                          className="shrink-0 whitespace-nowrap rounded-md border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs font-semibold text-slate-200 hover:border-sky-400 disabled:opacity-50"
                        >
                          Sync Upstox Master
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleSyncInstruments('zerodha')}
                          disabled={syncingMaster !== null}
                          className="shrink-0 whitespace-nowrap rounded-md border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs font-semibold text-slate-200 hover:border-sky-400 disabled:opacity-50"
                        >
                          Sync Zerodha Master
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Gap-Only Historical Downloader Form */}
            <div className="space-y-4">
              <div className="grid grid-cols-1 items-end gap-4 sm:grid-cols-2 lg:grid-cols-[1.15fr_0.85fr_0.85fr_0.95fr_0.95fr_1fr_auto]">
                <div>
                  <label htmlFor="hist-symbol" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    Symbol / Contract
                  </label>
                  <input
                    id="hist-symbol"
                    type="text"
                    value={histSymbol}
                    onChange={(e) => {
                      setHistSymbol(e.target.value.toUpperCase());
                      setHistInstrumentKey('');
                      setHistContractType('');
                    }}
                    placeholder="RELIANCE or NIFTY26NOVFUT"
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  />
                </div>

                <div>
                  <label htmlFor="hist-exchange" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    Exchange
                  </label>
                  <select
                    id="hist-exchange"
                    value={histExchange}
                    onChange={(e) => {
                      const ex = e.target.value;
                      setHistExchange(ex);
                      setHistInstrumentKey('');
                      if (['NFO', 'BFO', 'MCX'].includes(ex)) {
                        setFnoOpen(true);
                        setFnoExchange(ex);
                        void fetchFnoContracts({ exchange: ex, expiry: '', strike: '' }, true);
                      }
                    }}
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="NSE">NSE (Equity / Index)</option>
                    <option value="BSE">BSE (Equity)</option>
                    <option value="NFO">NFO (NSE F&amp;O)</option>
                    <option value="BFO">BFO (BSE F&amp;O)</option>
                    <option value="MCX">MCX (Commodity F&amp;O)</option>
                  </select>
                </div>

                <div>
                  <label htmlFor="hist-tf" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    Timeframe
                  </label>
                  <select
                    id="hist-tf"
                    value={histTimeframe}
                    onChange={(e) => setHistTimeframe(e.target.value)}
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="1day">1 Day (Canonical)</option>
                    <option value="1week">1 Week (Resampled from 1d)</option>
                    <option value="1month">1 Month (Resampled from 1d)</option>
                    <option value="1minute">1 Min (Canonical)</option>
                    <option value="5minute">5 Min (Resampled from 1m)</option>
                    <option value="15minute">15 Min (Resampled from 1m)</option>
                    <option value="30minute">30 Min (Resampled from 1m)</option>
                    <option value="60minute">60 Min (Resampled from 1m)</option>
                  </select>
                </div>

                <div>
                  <label htmlFor="hist-from" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    From Date
                  </label>
                  <input
                    id="hist-from"
                    type="date"
                    value={histFrom}
                    onChange={(e) => setHistFrom(e.target.value)}
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  />
                </div>

                <div>
                  <label htmlFor="hist-to" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    To Date
                  </label>
                  <input
                    id="hist-to"
                    type="date"
                    value={histTo}
                    onChange={(e) => setHistTo(e.target.value)}
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  />
                </div>

                <div>
                  <label htmlFor="hist-provider" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                    Source
                  </label>
                  <select
                    id="hist-provider"
                    value={histProvider}
                    onChange={(e) => setHistProvider(e.target.value)}
                    className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="auto">Assigned ({dataSelection})</option>
                    <option value="yfinance">Yahoo Finance</option>
                    <option value="upstox">Upstox</option>
                    <option value="zerodha">Zerodha Kite</option>
                  </select>
                </div>

                <button
                  type="button"
                  onClick={() => void handleDownloadHistorical()}
                  disabled={downloadingHist}
                  className="inline-flex min-h-[42px] shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-sm font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
                >
                  <Download className="h-4 w-4" />
                  <span>{downloadingHist ? 'Syncing...' : 'Download Missing Days'}</span>
                </button>
              </div>

              {(histInstrumentKey || ['NFO', 'BFO', 'MCX'].includes(histExchange)) && (
                <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs text-slate-400">
                  <span>
                    {histInstrumentKey
                      ? `Bound Contract Key: ${histInstrumentKey}${histContractType ? ` (${histContractType})` : ''}`
                      : `F&O Segment Selected: ${histExchange}`}
                  </span>
                  {(histProvider === 'yfinance' || (histProvider === 'auto' && dataSelection === 'yfinance')) &&
                    ['NFO', 'BFO', 'MCX'].includes(histExchange) && (
                      <span className="font-sans text-amber-400">
                        Yahoo Finance does not serve NSE/BSE F&amp;O — switch Source to Upstox or Zerodha Kite.
                      </span>
                    )}
                </div>
              )}

              {dataFeedback && (
                <div
                  className={`flex flex-wrap items-center justify-between gap-3 text-[13px] ${
                    dataFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                  }`}
                  role="status"
                  aria-live="polite"
                >
                  <span className="inline-flex items-center gap-2">
                    {dataFeedback.type === 'ok' ? <Check className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                    <span>{dataFeedback.text}</span>
                  </span>
                  {downloadAudit && (
                    <span className="font-mono text-xs tabular-nums text-slate-300">
                      API Calls Made: {downloadAudit.api_calls_made} · Existing Days Skipped:{' '}
                      {downloadAudit.existing_days_skipped} · Rows Inserted: {downloadAudit.rows_inserted}
                    </span>
                  )}
                </div>
              )}

              {/* Cached datasets quick switcher */}
              {catalog.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <span className="text-xs font-semibold text-slate-400">Cached Datasets:</span>
                  {catalog.map((ds) => (
                    <button
                      key={`${ds.symbol}-${ds.exchange}-${ds.timeframe}`}
                      type="button"
                      onClick={() => {
                        setHistSymbol(ds.symbol);
                        setHistExchange(ds.exchange);
                        setHistInstrumentKey('');
                        setHistContractType('');
                        setHistTimeframe(ds.timeframe);
                        setHistFrom(ds.start_date);
                        setHistTo(ds.end_date);
                        void fetchDataRecords(ds.symbol, ds.exchange, ds.timeframe, viewMode, ds.start_date, ds.end_date);
                      }}
                      className="min-h-[30px] shrink-0 whitespace-nowrap rounded-md border border-slate-700 bg-[#0b1120] px-2.5 py-1 font-mono text-xs tabular-nums text-slate-300 transition-colors hover:border-sky-400"
                    >
                      {ds.symbol} · {ds.exchange} · {ds.timeframe} ({ds.row_count} bars)
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Snapshot vs Full Data Viewer */}
            <div className="space-y-4 border-t border-slate-800 pt-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-1 rounded-lg bg-[#0b1120] p-1">
                  <button
                    type="button"
                    onClick={() => void handleSwitchViewMode('snapshot')}
                    className={`min-h-[34px] shrink-0 whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
                      viewMode === 'snapshot'
                        ? 'bg-slate-800 text-slate-100'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    Snapshot View
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleSwitchViewMode('full')}
                    className={`min-h-[34px] shrink-0 whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
                      viewMode === 'full'
                        ? 'bg-slate-800 text-slate-100'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    Full Data View
                  </button>
                </div>

                <div className="flex flex-wrap items-center gap-3">
                  {dataRecords && (
                    <span className="font-mono text-xs tabular-nums text-slate-400">
                      Showing {dataRecords.returned_rows} of {dataRecords.total_rows} stored rows
                      {dataRecords.is_truncated && (
                        <>
                          {' '}
                          (Snapshot sample —{' '}
                          <button
                            type="button"
                            onClick={() => void handleSwitchViewMode('full')}
                            className="font-sans font-semibold text-sky-400 underline underline-offset-2 hover:text-sky-300"
                          >
                            View all {dataRecords.total_rows} rows
                          </button>
                          )
                        </>
                      )}
                    </span>
                  )}
                  <a
                    href={`/api/data/export.csv?symbol=${encodeURIComponent(histSymbol)}&exchange=${encodeURIComponent(
                      histExchange
                    )}&timeframe=${encodeURIComponent(histTimeframe)}&from_date=${encodeURIComponent(
                      histFrom
                    )}&to_date=${encodeURIComponent(histTo)}`}
                    className="inline-flex min-h-[36px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:border-slate-500 hover:bg-slate-800"
                  >
                    <Download className="h-3.5 w-3.5" />
                    <span>Export CSV</span>
                  </a>
                </div>
              </div>

              {dataRecords && dataRecords.total_rows > 0 && (
                <div className="grid grid-cols-2 gap-4 border-y border-slate-800 py-3 font-mono text-xs tabular-nums sm:grid-cols-3 lg:grid-cols-6">
                  <div>
                    <div className="text-slate-500">Latest Close</div>
                    <div className="mt-0.5 text-sm font-semibold text-slate-100">
                      {dataRecords.summary.latest_close ?? '—'}
                    </div>
                  </div>
                  <div>
                    <div className="text-slate-500">Period Change</div>
                    <div
                      className={`mt-0.5 text-sm font-semibold ${
                        (dataRecords.summary.change_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                      }`}
                    >
                      {dataRecords.summary.change_pct !== null
                        ? `${dataRecords.summary.change_pct > 0 ? '+' : ''}${dataRecords.summary.change_pct}%`
                        : '—'}
                    </div>
                  </div>
                  <div>
                    <div className="text-slate-500">Range Low</div>
                    <div className="mt-0.5 text-sm font-semibold text-slate-100">
                      {dataRecords.summary.min_low ?? '—'}
                    </div>
                  </div>
                  <div>
                    <div className="text-slate-500">Range High</div>
                    <div className="mt-0.5 text-sm font-semibold text-slate-100">
                      {dataRecords.summary.max_high ?? '—'}
                    </div>
                  </div>
                  <div>
                    <div className="text-slate-500">Total Volume</div>
                    <div className="mt-0.5 text-sm font-semibold text-slate-100">
                      {dataRecords.summary.total_volume.toLocaleString()}
                    </div>
                  </div>
                  <div>
                    <div className="text-slate-500">Latest OI</div>
                    <div className="mt-0.5 text-sm font-semibold text-slate-100">
                      {(dataRecords.summary.latest_oi ?? 0).toLocaleString()}
                    </div>
                  </div>
                </div>
              )}

              {loadingRecords ? (
                <div className="space-y-2 py-4">
                  {[0, 1, 2, 3].map((idx) => (
                    <div key={idx} className="h-9 animate-pulse rounded bg-slate-800/60" />
                  ))}
                </div>
              ) : !dataRecords || dataRecords.rows.length === 0 ? (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-[#0b1120] px-4 py-4 text-sm text-slate-400">
                  <span>
                    No locally stored candles yet for {histSymbol} ({histExchange} · {histTimeframe}). Click Download Missing Days or Load Stored Data.
                  </span>
                  <button
                    type="button"
                    onClick={() => void fetchDataRecords(histSymbol, histExchange, histTimeframe, viewMode, histFrom, histTo)}
                    className="shrink-0 whitespace-nowrap rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-200 hover:bg-slate-800"
                  >
                    Load Stored Data
                  </button>
                </div>
              ) : (
                <div className="max-h-[420px] overflow-auto rounded-lg border border-slate-800 bg-[#0b1120]">
                  <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                    <thead className="sticky top-0 bg-[#111827] text-slate-400">
                      <tr className="border-b border-slate-800">
                        <th className="px-3.5 py-2.5 font-semibold">Timestamp (IST)</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">Open</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">High</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">Low</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">Close</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">Volume</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">OI</th>
                        <th className="px-3.5 py-2.5 text-right font-semibold">Source</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/80 text-slate-200">
                      {dataRecords.rows.map((row) => (
                        <tr key={row.ts_ist} className="hover:bg-slate-900/60">
                          <td className="px-3.5 py-2 whitespace-nowrap text-slate-300">{row.ts_ist}</td>
                          <td className="px-3.5 py-2 text-right">{row.open != null ? Number(row.open).toFixed(2) : '—'}</td>
                          <td className="px-3.5 py-2 text-right">{row.high != null ? Number(row.high).toFixed(2) : '—'}</td>
                          <td className="px-3.5 py-2 text-right">{row.low != null ? Number(row.low).toFixed(2) : '—'}</td>
                          <td className="px-3.5 py-2 text-right font-semibold text-slate-100">{row.close != null ? Number(row.close).toFixed(2) : '—'}</td>
                          <td className="px-3.5 py-2 text-right text-slate-300">{row.volume.toLocaleString()}</td>
                          <td className="px-3.5 py-2 text-right text-slate-300">{(row.oi ?? 0).toLocaleString()}</td>
                          <td className="px-3.5 py-2 text-right text-slate-400">{row.source}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </section>

        {/* Section 4: Phase 4 — Hot-Standby Feed Failover, Preflight & SEBI Kill Switch */}
        <section id="live-controls" className="space-y-5">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-slate-100">
                Live Feed Failover, Preflight &amp; Kill Switch
              </h2>
              <p className="text-sm text-slate-400">
                Automatic primary-to-standby market data failover, pre-trade broker margin &amp; symbol gate checks, and SEBI SHA-256 hash-chained safety ledger.
              </p>
            </div>
            {liveStatus?.feeds && (
              <div className="font-mono text-xs tabular-nums text-slate-400">
                Primary: {liveStatus.feeds.primary_feed.toUpperCase()} · Active Feed:{' '}
                {liveStatus.feeds.active_feed.toUpperCase()} · Failovers: {liveStatus.feeds.failover_count}
              </div>
            )}
          </div>

          {/* Hot-Standby Feed Priority Table & Live Quotes */}
          <div className="space-y-5 rounded-xl border border-slate-800 bg-[#111827] p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-100">
                <Activity className="h-4 w-4 text-sky-400" />
                <span>Hot-Standby Market Data Failover Chain</span>
              </div>
              <div className="flex flex-wrap items-center gap-2.5">
                <button
                  type="button"
                  onClick={() => navigateTo('/terminal')}
                  className="min-h-[38px] shrink-0 whitespace-nowrap rounded-lg border border-sky-500/60 bg-[#0b1120] px-4 py-2 text-xs font-semibold text-sky-400 transition-colors hover:bg-slate-800"
                >
                  Open Live Terminal
                </button>
                <button
                  type="button"
                  onClick={() => void handlePollFeeds()}
                  disabled={feedActionLoading}
                  className="min-h-[38px] shrink-0 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-xs font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
                >
                  {feedActionLoading ? 'Polling...' : 'Poll Live Quotes'}
                </button>
                <button
                  type="button"
                  onClick={() => void handleTriggerFailover()}
                  disabled={feedActionLoading}
                  className="min-h-[38px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-xs font-semibold text-slate-200 transition-colors hover:border-amber-500/70 hover:bg-slate-800 disabled:opacity-50"
                >
                  Simulate Failover
                </button>
                <button
                  type="button"
                  onClick={() => void handleRecoverPrimary()}
                  disabled={feedActionLoading}
                  className="min-h-[38px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-xs font-semibold text-slate-200 transition-colors hover:border-sky-400 hover:bg-slate-800 disabled:opacity-50"
                >
                  Restore Primary
                </button>
              </div>
            </div>

            {feedFeedback && (
              <div
                className={`flex items-center gap-2 text-[13px] ${
                  feedFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                }`}
                role="status"
                aria-live="polite"
              >
                {feedFeedback.type === 'ok' ? <Check className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                <span>{feedFeedback.text}</span>
              </div>
            )}

            <div className="overflow-x-auto border-t border-slate-800 pt-3">
              <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                <thead className="text-slate-400">
                  <tr className="border-b border-slate-800">
                    <th className="py-2.5 pr-4 font-semibold">Provider</th>
                    <th className="py-2.5 pr-4 font-semibold">Role</th>
                    <th className="py-2.5 pr-4 font-semibold">Transport</th>
                    <th className="py-2.5 pr-4 font-semibold">State</th>
                    <th className="py-2.5 pr-4 text-right font-semibold">Ticks / Errs</th>
                    <th className="py-2.5 pr-4 text-right font-semibold">Latency</th>
                    <th className="py-2.5 text-right font-semibold">Last Tick (IST)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80 text-slate-200">
                  {(liveStatus?.feeds?.slots ?? []).map((slot) => (
                    <tr key={slot.feed_id}>
                      <td className="py-2.5 pr-4 font-sans text-[13px] font-semibold text-slate-100">
                        {slot.display_name}
                      </td>
                      <td className="py-2.5 pr-4 text-slate-400">{slot.role}</td>
                      <td className="py-2.5 pr-4 text-slate-400">{slot.transport}</td>
                      <td
                        className={`py-2.5 pr-4 font-semibold ${
                          slot.status === 'CONNECTED'
                            ? 'text-emerald-400'
                            : slot.status === 'FAILOVER' || slot.status === 'DISCONNECTED'
                              ? 'text-amber-400'
                              : 'text-slate-400'
                        }`}
                      >
                        {slot.status}
                      </td>
                      <td className="py-2.5 pr-4 text-right">
                        {slot.ticks_received} / {slot.error_count}
                      </td>
                      <td className="py-2.5 pr-4 text-right text-slate-300">
                        {slot.latency_ms !== null ? `${slot.latency_ms} ms` : '—'}
                      </td>
                      <td className="py-2.5 text-right text-slate-400">
                        {slot.last_tick_ist ? slot.last_tick_ist.slice(11, 19) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {liveStatus?.feeds?.quotes && liveStatus.feeds.quotes.length > 0 && (
              <div className="grid grid-cols-2 gap-4 border-t border-slate-800 pt-4 font-mono text-xs tabular-nums sm:grid-cols-5">
                {liveStatus.feeds.quotes.map((q, idx) => (
                  <div key={`${q.exchange || 'NSE'}-${q.symbol}-${idx}`} title={q.note || undefined}>
                    <div className="text-slate-400">
                      {q.symbol} · {q.source}
                    </div>
                    <div className="mt-0.5 text-sm font-semibold text-slate-100">
                      {q.ltp !== null && q.ltp !== undefined && Number.isFinite(Number(q.ltp))
                        ? `₹${Number(q.ltp).toFixed(2)}`
                        : '—'}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Preflight Gate & Emergency Kill Switch */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* Preflight Checks */}
            <div className="flex flex-col justify-between space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h3 className="text-base font-semibold text-slate-100">Preflight Safety &amp; Margin Gate</h3>
                    <p className="text-xs text-slate-400">
                      {preflight
                        ? `${preflight.passed_count}/${preflight.total_count} passed · ${
                            preflight.overall_ready ? 'Ready for execution' : 'Execution blocked until ERROR checks pass'
                          }`
                        : 'Verify auth, live equity margin, SQLite symbol mapping, market hours, and kill switch.'}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => void handleRunPreflight()}
                    disabled={runningPreflight}
                    className="min-h-[38px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-2 text-xs font-semibold text-slate-200 transition-colors hover:border-sky-400 hover:bg-slate-800 disabled:opacity-50"
                  >
                    {runningPreflight ? 'Checking...' : 'Run Preflight Checks'}
                  </button>
                </div>

                {preflight ? (
                  <div className="divide-y divide-slate-800 border-t border-slate-800">
                    {preflight.checks.map((chk) => (
                      <div key={chk.id} className="flex items-start justify-between gap-4 py-2.5 text-xs">
                        <div>
                          <div className="font-semibold text-slate-200">{chk.name}</div>
                          <div className="mt-0.5 text-slate-400">{chk.message}</div>
                        </div>
                        <span
                          className={`shrink-0 font-mono font-semibold ${
                            chk.status === 'PASS'
                              ? 'text-emerald-400'
                              : chk.status === 'WARN'
                                ? 'text-amber-400'
                                : 'text-red-400'
                          }`}
                        >
                          {chk.status}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="border-t border-slate-800 pt-3 text-xs text-slate-500">
                    Click Run Preflight Checks to probe broker margin, instrument mapping, and kill-switch state.
                  </div>
                )}
              </div>
            </div>

            {/* Emergency Kill Switch */}
            <div className="flex flex-col justify-between space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-6">
              <div className="space-y-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <h3 className="text-base font-semibold text-slate-100">Emergency Kill Switch</h3>
                    <p className="text-xs text-slate-400">
                      Persists immediately to disk, cancels active open orders on the trading broker, and blocks all new strategy signals.
                    </p>
                  </div>
                  <div
                    className={`inline-flex shrink-0 items-center gap-1.5 font-mono text-xs font-semibold ${
                      liveStatus?.kill_switch?.engaged ? 'text-red-400' : 'text-emerald-400'
                    }`}
                  >
                    {liveStatus?.kill_switch?.engaged ? (
                      <>
                        <ShieldAlert className="h-4 w-4" />
                        <span>ENGAGED</span>
                      </>
                    ) : (
                      <>
                        <ShieldCheck className="h-4 w-4" />
                        <span>ARMED / CLEAR</span>
                      </>
                    )}
                  </div>
                </div>

                {liveStatus?.kill_switch?.engaged && (
                  <div className="border-y border-red-900/60 py-2.5 text-xs text-red-300">
                    Stopped at {liveStatus.kill_switch.engaged_at_ist || 'now'} ·{' '}
                    {liveStatus.kill_switch.reason || 'Operator halt'}
                  </div>
                )}

                <div>
                  <label htmlFor="kill-reason-input" className="mb-1.5 block text-xs font-semibold text-slate-300">
                    Operator Audit Reason
                  </label>
                  <input
                    id="kill-reason-input"
                    type="text"
                    value={killReason}
                    onChange={(e) => setKillReason(e.target.value)}
                    placeholder="Reason for emergency halt or reset (logged to SHA-256 chain)..."
                    className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                  />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-3 pt-2">
                {liveStatus?.kill_switch?.engaged ? (
                  <button
                    type="button"
                    onClick={() => void handleToggleKillSwitch(false)}
                    disabled={killBusy}
                    className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg bg-emerald-500 px-4 py-2 text-xs font-semibold text-emerald-950 transition-colors hover:bg-emerald-400 disabled:opacity-50"
                  >
                    {killBusy ? 'Resetting...' : 'Reset Kill Switch'}
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void handleToggleKillSwitch(true)}
                    disabled={killBusy}
                    className="min-h-[40px] shrink-0 whitespace-nowrap rounded-lg bg-red-600 px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-red-500 disabled:opacity-50"
                  >
                    {killBusy ? 'Engaging...' : 'Engage Emergency Kill Switch'}
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* SEBI Hash-Chained Audit Ledger */}
          <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="text-base font-semibold text-slate-100">SEBI Hash-Chained Audit Trail</h3>
                <p className="font-mono text-xs tabular-nums text-slate-400">
                  {liveStatus?.audit_verify
                    ? `${liveStatus.audit_verify.valid ? 'VERIFIED' : 'TAMPER ALERT'} · ${liveStatus.audit_verify.total_events} events · Head: ${liveStatus.audit_verify.head_hash.slice(0, 14)}…`
                    : 'Append-only SHA-256 ledger'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void handleVerifyAuditChain()}
                disabled={verifyingAudit}
                className="min-h-[36px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:border-sky-400 hover:bg-slate-800 disabled:opacity-50"
              >
                {verifyingAudit ? 'Verifying...' : 'Verify SHA-256 Chain'}
              </button>
            </div>

            {!liveStatus?.audit_events || liveStatus.audit_events.length === 0 ? (
              <div className="border-t border-slate-800 pt-3 text-xs text-slate-500">
                No audit events recorded yet. Trigger a preflight check, feed failover, or kill-switch test to append a SHA-256 linked record.
              </div>
            ) : (
              <div className="max-h-[260px] overflow-auto border-t border-slate-800">
                <table className="w-full border-collapse text-left font-mono text-xs tabular-nums">
                  <thead className="sticky top-0 bg-[#111827] text-slate-400">
                    <tr className="border-b border-slate-800">
                      <th className="py-2 pr-4 font-semibold">Timestamp (IST)</th>
                      <th className="py-2 pr-4 font-semibold">Event</th>
                      <th className="py-2 pr-4 font-semibold">Actor</th>
                      <th className="py-2 pr-4 font-semibold">Detail</th>
                      <th className="py-2 text-right font-semibold">SHA-256 Chain Hash</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/80 text-slate-300">
                    {liveStatus.audit_events.map((ev) => (
                      <tr key={ev.id}>
                        <td className="py-2 pr-4 whitespace-nowrap text-slate-400">{ev.ts_ist.slice(0, 19)}</td>
                        <td className="py-2 pr-4 font-semibold text-slate-100">{ev.event_type}</td>
                        <td className="py-2 pr-4 text-slate-400">{ev.actor}</td>
                        <td className="py-2 pr-4 font-sans text-slate-300">
                          {String(ev.payload.reason || ev.payload.trading_broker || ev.payload.trading_symbol || '—')}
                        </td>
                        <td className="py-2 text-right text-sky-400">{ev.chain_hash.slice(0, 16)}…</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>

        {/* Section 5: Phase 5 — Screener, Backtester & Portfolio Risk Console */}
        <AnalyticsConsole
          activeDataConnection={dataSelection}
          activeTradingBroker={tradingSelection}
          onRiskStateChange={() => void fetchLiveStatus()}
        />

        {/* Section 6: Modular Expansion Roadmap */}
        <section id="roadmap" className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold tracking-tight text-slate-100">Modular Expansion Roadmap</h2>
            <p className="text-sm text-slate-400">
              Each adapter satisfies the unified capability and health contract, enabling Zerodha and trading modules to plug in incrementally.
            </p>
          </div>

          <div className="divide-y divide-slate-800 border-t border-slate-800">
            {ROADMAP_PHASES.map((item) => (
              <div key={item.phase} className="grid grid-cols-1 gap-3 py-4 md:grid-cols-[110px_1fr] md:gap-5">
                <div className="font-mono text-[13px] font-semibold text-sky-400">{item.phase}</div>
                <div>
                  <div className="font-semibold text-slate-100">{item.title}</div>
                  <div className="mt-1 text-sm text-slate-400">{item.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </section>
      </main>

      {/* OAuth Reconnect & Credential Modal */}
      {modalBrokerId && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-5"
          role="dialog"
          aria-modal="true"
          aria-labelledby="reconnect-modal-title"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeReconnectModal();
          }}
        >
          <div className="max-h-[92vh] w-full max-w-[560px] overflow-y-auto rounded-xl border border-slate-700 bg-[#111827] p-6">
            <div className="mb-5 flex items-center justify-between gap-4">
              <h3 id="reconnect-modal-title" className="text-lg font-semibold text-slate-100">
                Reconnect {modalBrokerLabel} Session
              </h3>
              <button
                type="button"
                onClick={closeReconnectModal}
                className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800"
              >
                <X className="h-3.5 w-3.5" />
                <span>Close</span>
              </button>
            </div>

            <div className="space-y-4">
              {/* Auto-detected .env / Keychain Configuration */}
              <div className="space-y-2 rounded-lg border border-slate-800 bg-[#0b1120] p-3.5">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-[13px] font-semibold text-slate-200">Detected Local Configuration</span>
                  <button
                    type="button"
                    onClick={() => setCredsExpanded((prev) => !prev)}
                    className="rounded-md border border-slate-700 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-800"
                  >
                    {credsExpanded ? 'Hide Inputs' : 'Edit Credentials'}
                  </button>
                </div>
                <div className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="font-semibold text-slate-400">API Key</span>
                  <span className="font-mono text-slate-200">
                    {activeCreds?.has_api_key
                      ? `${activeCreds.api_key_preview} (${formatCredentialSource(activeCreds.key_source)})`
                      : 'Missing'}
                  </span>
                </div>
                <div className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="font-semibold text-slate-400">API Secret</span>
                  <span className="font-mono text-slate-200">
                    {activeCreds?.has_api_secret
                      ? `Configured (${formatCredentialSource(activeCreds.secret_source)})`
                      : 'Missing'}
                  </span>
                </div>
                <div className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="font-semibold text-slate-400">Redirect URI</span>
                  <span className="font-mono break-all text-right text-slate-200">
                    {activeCreds?.redirect_uri
                      ? `${activeCreds.redirect_uri} (${formatCredentialSource(activeCreds.redirect_source)})`
                      : `${redirectUri} (Default)`}
                  </span>
                </div>
              </div>

              {credsExpanded && (
                <div className="space-y-3.5 rounded-lg border border-slate-800 bg-[#0b1120]/60 p-4">
                  <div>
                    <label htmlFor="modal-api-key" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      {modalBrokerLabel} API Key
                    </label>
                    <input
                      id="modal-api-key"
                      type="text"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={
                        activeCreds?.has_api_key
                          ? `Detected (${activeCreds.api_key_preview}) — enter only to override`
                          : `Enter your ${modalBrokerLabel} API Key`
                      }
                      autoComplete="off"
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>

                  <div>
                    <label htmlFor="modal-api-secret" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      {modalBrokerLabel} API Secret
                    </label>
                    <input
                      id="modal-api-secret"
                      type="password"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      placeholder={
                        activeCreds?.has_api_secret
                          ? `Detected in ${formatCredentialSource(activeCreds.secret_source)} — enter only to override`
                          : `Enter your ${modalBrokerLabel} API Secret`
                      }
                      autoComplete="off"
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>

                  <div>
                    <label htmlFor="modal-redirect-uri" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      OAuth Redirect URI
                    </label>
                    <input
                      id="modal-redirect-uri"
                      type="text"
                      value={redirectUri}
                      onChange={(e) => setRedirectUri(e.target.value)}
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>

                  <button
                    type="button"
                    onClick={() => void handleSaveCredentials()}
                    className="min-h-[38px] shrink-0 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-[13px] font-semibold text-slate-200 hover:bg-slate-800"
                  >
                    Save Credentials
                  </button>
                </div>
              )}

              <button
                type="button"
                onClick={() => void handleStartOAuth()}
                className="inline-flex min-h-[42px] w-full items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-sm font-semibold text-sky-950 hover:bg-sky-300"
              >
                <ExternalLink className="h-4 w-4" />
                <span>Login with {modalBrokerLabel} (Auto-Capture)</span>
              </button>

              {modalBrokerId === 'zerodha' && (
                <p className="text-xs leading-relaxed text-slate-400">
                  If Kite shows <span className="font-mono text-slate-300">&quot;The user is not enabled for the app&quot;</span>, sign in with the exact Zerodha Client ID linked to this API key (or use an Incognito window) and confirm the app status/subscription is Active on <span className="font-mono text-slate-300">developers.kite.trade</span>.
                </p>
              )}

              {captureStatus && (
                <div className="space-y-1.5 rounded-lg border border-slate-700 bg-[#0b1120] p-3.5">
                  <div className="flex items-center gap-2.5 text-[13px] font-semibold text-slate-100">
                    <span
                      className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                        captureStatus.status === 'connected'
                          ? 'bg-emerald-600 shadow-[0_0_10px_rgba(22,163,74,0.7)]'
                          : captureStatus.status === 'error' || captureStatus.status === 'timeout'
                            ? 'bg-red-600 shadow-[0_0_10px_rgba(220,38,38,0.65)]'
                            : 'bg-amber-600 shadow-[0_0_10px_rgba(217,119,6,0.7)]'
                      }`}
                      aria-hidden="true"
                    />
                    <span>
                      {captureStatus.status === 'connected'
                        ? 'Connected automatically'
                        : captureStatus.status === 'exchanging'
                          ? 'Token captured — verifying session...'
                          : captureStatus.status === 'timeout'
                            ? 'Auto-capture timed out'
                            : captureStatus.status === 'error'
                              ? 'Auto-capture failed'
                              : `Listening for ${modalBrokerLabel} browser redirect...`}
                    </span>
                  </div>
                  <p className="text-xs text-slate-400">{captureStatus.message}</p>
                  {oauthUrl && (
                    <a
                      href={oauthUrl}
                      target="algodesk_oauth_login"
                      rel="noreferrer"
                      className="inline-block font-mono text-[11px] text-sky-400 underline underline-offset-4"
                    >
                      Open {modalBrokerLabel} login link (if popup was blocked)
                    </a>
                  )}
                </div>
              )}

              <hr className="border-slate-800" />

              <details
                open={manualOpen}
                onToggle={(e) => setManualOpen((e.currentTarget as HTMLDetailsElement).open)}
              >
                <summary className="cursor-pointer select-none text-[13px] font-semibold text-slate-400 hover:text-slate-200">
                  Manual Fallback: Paste Redirect URL or Authorization Token
                </summary>
                <div className="mt-3 space-y-3">
                  <div>
                    <label htmlFor="modal-callback-input" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      Redirected Callback URL or Token
                    </label>
                    <input
                      id="modal-callback-input"
                      type="text"
                      value={callbackInput}
                      onChange={(e) => setCallbackInput(e.target.value)}
                      placeholder={
                        modalBrokerId === 'zerodha'
                          ? 'http://127.0.0.1:8000/auth/zerodha/callback?request_token=... or bare token'
                          : 'https://127.0.0.1:5000/?code=... or bare code'
                      }
                      autoComplete="off"
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                    <span className="mt-1.5 block text-xs text-slate-500">
                      Use this only if browser auto-capture is blocked by a browser warning or proxy.
                    </span>
                  </div>

                  <button
                    type="button"
                    onClick={() => void handleExchangeCode()}
                    disabled={exchanging}
                    className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-4 py-2 text-[13px] font-semibold text-slate-200 hover:bg-slate-800 disabled:opacity-50"
                  >
                    {exchanging ? 'Exchanging Token...' : 'Exchange Token & Connect'}
                  </button>
                </div>
              </details>

              {modalFeedback && (
                <div
                  className={`flex items-start gap-2 text-[13px] ${
                    modalFeedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                  }`}
                  role="status"
                  aria-live="polite"
                >
                  {modalFeedback.type === 'ok' ? (
                    <Check className="mt-0.5 h-4 w-4 shrink-0" />
                  ) : (
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  )}
                  <span>{modalFeedback.text}</span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
