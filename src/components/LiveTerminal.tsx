import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch, createAuthenticatedWebSocket, withAuthTokenQuery } from '../utils/authClient';
import {
  Activity,
  AlertCircle,
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  Check,
  Database,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Wifi,
  WifiOff,
} from 'lucide-react';

export interface FeedSlotHealth {
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

export interface LiveSubscriptionItem {
  symbol: string;
  exchange: string;
  instrument_type: string;
  instrument_key: string;
}

export interface LiveTickPoint {
  ts_ist: string;
  ltp: number;
}

export interface MarketDepthLevel {
  price: number;
  quantity: number;
  orders?: number;
}

export interface MarketDepthSnapshot {
  symbol?: string;
  exchange?: string;
  source?: string;
  levels?: number;
  requested_levels?: number;
  max_broker_levels?: number;
  bids: MarketDepthLevel[];
  asks: MarketDepthLevel[];
  total_buy_qty?: number;
  total_sell_qty?: number;
  tbq?: number;
  tsq?: number;
  spread?: number | null;
  spread_bps?: number | null;
  imbalance_ratio?: number | null;
  ts_ist?: string | null;
}

export interface LiveQuoteRow {
  symbol: string;
  exchange?: string;
  instrument_type?: string;
  instrument_key?: string;
  ltp: number | null;
  prev_close?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  change?: number | null;
  change_pct: number | null;
  volume?: number;
  oi?: number;
  bid?: number | null;
  ask?: number | null;
  depth?: MarketDepthSnapshot | null;
  depth_levels?: number;
  execution_grade?: boolean;
  feed_quality?: string;
  source: string;
  transport?: string;
  direction?: 'up' | 'down' | 'flat';
  tick_seq?: number;
  note?: string;
  ts_ist: string | null;
  history?: LiveTickPoint[];
}

export interface LiveTickEvent {
  seq: number;
  ts_ist: string;
  symbol: string;
  exchange: string;
  ltp: number;
  change_pct: number | null;
  volume: number;
  oi: number;
  direction: 'up' | 'down' | 'flat';
  source: string;
}

export interface FeedManagerPayload {
  primary_feed: string;
  active_feed: string;
  in_failover: boolean;
  forced_failover: boolean;
  failover_count: number;
  last_failover_reason: string;
  tick_seq?: number;
  watchlist: string[];
  subscriptions?: LiveSubscriptionItem[];
  slots: FeedSlotHealth[];
  quotes: LiveQuoteRow[];
  tick_events?: LiveTickEvent[];
}

interface LookupHit {
  broker_id: string;
  exchange: string;
  segment: string;
  trading_symbol: string;
  asset_symbol?: string;
  name: string;
  instrument_type: string;
  instrument_key: string;
  expiry?: string | null;
  strike?: number | null;
  lot_size?: number;
}

interface InstrumentStats {
  total_instruments: number;
  fno_instruments?: number;
  db_size_mb: number;
}

interface FnoContractsPayload {
  ok: boolean;
  exchange: string;
  contract_type: string;
  underlying: string;
  underlyings: string[];
  expiries: string[];
  strikes: number[];
  contracts: LookupHit[];
  count: number;
}

interface LiveTerminalProps {
  onNavigateHome: () => void;
}

const PRESET_ASSETS: Array<{ symbol: string; exchange: string; instrument_type: string }> = [
  { symbol: 'NIFTY', exchange: 'NSE', instrument_type: 'INDEX' },
  { symbol: 'BANKNIFTY', exchange: 'NSE', instrument_type: 'INDEX' },
  { symbol: 'SENSEX', exchange: 'BSE', instrument_type: 'INDEX' },
  { symbol: 'RELIANCE', exchange: 'NSE', instrument_type: 'EQUITY' },
  { symbol: 'TCS', exchange: 'NSE', instrument_type: 'EQUITY' },
  { symbol: 'INFY', exchange: 'NSE', instrument_type: 'EQUITY' },
  { symbol: 'HDFCBANK', exchange: 'NSE', instrument_type: 'EQUITY' },
];

function fmtPrice(val: number | null | undefined): string {
  if (val === null || val === undefined || Number.isNaN(val)) return '—';
  return val.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtInt(val: number | null | undefined): string {
  if (val === null || val === undefined || Number.isNaN(val)) return '0';
  return Math.round(val).toLocaleString('en-IN');
}

function fmtShortTime(ts: string | null | undefined): string {
  if (!ts) return '—';
  const match = ts.match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : ts.slice(0, 19);
}

function assetKey(symbol: string, exchange = 'NSE'): string {
  return `${(exchange || 'NSE').toUpperCase()}:${symbol.toUpperCase()}`;
}

export function LiveTerminal({ onNavigateHome }: LiveTerminalProps) {
  const [feedState, setFeedState] = useState<FeedManagerPayload | null>(null);
  const [wsStatus, setWsStatus] = useState<'connecting' | 'ws_open' | 'sse_open' | 'paused' | 'reconnecting'>('connecting');
  const [paused, setPaused] = useState(false);
  const [polling, setPolling] = useState(false);

  // Subscribe form + SQLite lookup & F&O Finder state (mirrors Historical Download section)
  const [symbolInput, setSymbolInput] = useState('');
  const [exchangeInput, setExchangeInput] = useState('NSE');
  const [selectedInstrumentType, setSelectedInstrumentType] = useState('');
  const [selectedInstrumentKey, setSelectedInstrumentKey] = useState('');
  const [instrQuery, setInstrQuery] = useState('');
  const [instrTypeFilter, setInstrTypeFilter] = useState('ALL');
  const [instrResults, setInstrResults] = useState<LookupHit[]>([]);
  const [instrStats, setInstrStats] = useState<InstrumentStats | null>(null);
  const [syncingMaster, setSyncingMaster] = useState<'upstox' | 'zerodha' | null>(null);
  const [instrFeedback, setInstrFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const [fnoOpen, setFnoOpen] = useState(false);
  const [fnoExchange, setFnoExchange] = useState('NFO');
  const [fnoUnderlying, setFnoUnderlying] = useState('NIFTY');
  const [fnoContractType, setFnoContractType] = useState('FUT');
  const [fnoExpiry, setFnoExpiry] = useState('');
  const [fnoStrike, setFnoStrike] = useState('');
  const [fnoData, setFnoData] = useState<FnoContractsPayload | null>(null);
  const [loadingFno, setLoadingFno] = useState(false);

  const [subscribing, setSubscribing] = useState(false);
  const [feedback, setFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // Selected quote in inspector + L5/L30 market depth state
  const [selectedKey, setSelectedKey] = useState<string>('');
  const [depthLevels, setDepthLevels] = useState<5 | 30>(5);
  const [fetchedDepth, setFetchedDepth] = useState<MarketDepthSnapshot | null>(null);
  const [depthLoading, setDepthLoading] = useState(false);
  const [switchingDepth, setSwitchingDepth] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const sseRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const instrSearchSeqRef = useRef(0);
  const fnoFetchSeqRef = useRef(0);
  const unmountedRef = useRef(false);

  const applyFeedPayload = useCallback((payload: unknown) => {
    if (!payload || typeof payload !== 'object') return;
    const obj = payload as Record<string, unknown>;
    const normalized: FeedManagerPayload =
      obj.feeds && typeof obj.feeds === 'object'
        ? (obj.feeds as FeedManagerPayload)
        : (obj as unknown as FeedManagerPayload);

    if (!Array.isArray(normalized.quotes) && !Array.isArray(normalized.slots)) {
      return;
    }

    setFeedState(normalized);
    if (typeof obj.message === 'string' && obj.type === 'ack') {
      setFeedback({ type: obj.ok === false ? 'err' : 'ok', text: obj.message });
    }
  }, []);

  const fetchInitialSnapshot = useCallback(async () => {
    try {
      const res = await apiFetch('/api/live/status');
      const data = await res.json();
      if (res.ok && data.feeds) {
        applyFeedPayload(data.feeds);
      }
    } catch {
      // Transport connection will retry shortly
    }
  }, [applyFeedPayload]);

  const closeTransports = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.onopen = null;
      wsRef.current.onmessage = null;
      wsRef.current.onerror = null;
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    if (sseRef.current) {
      sseRef.current.close();
      sseRef.current = null;
    }
  }, []);

  const connectStream = useCallback(() => {
    if (unmountedRef.current || paused) return;
    closeTransports();
    setWsStatus('connecting');

    const startSseFallback = () => {
      if (unmountedRef.current || paused) return;
      try {
        const es = new EventSource(withAuthTokenQuery('/api/live/stream'));
        sseRef.current = es;
        es.onopen = () => {
          if (!unmountedRef.current) setWsStatus('sse_open');
        };
        es.onmessage = (evt) => {
          try {
            const parsed = JSON.parse(evt.data);
            applyFeedPayload(parsed);
          } catch {
            // Ignore ping/malformed frames
          }
        };
        es.onerror = () => {
          es.close();
          sseRef.current = null;
          if (!unmountedRef.current && !paused) {
            setWsStatus('reconnecting');
            reconnectTimerRef.current = window.setTimeout(connectStream, 3000);
          }
        };
      } catch {
        setWsStatus('reconnecting');
        reconnectTimerRef.current = window.setTimeout(connectStream, 3000);
      }
    };

    try {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = createAuthenticatedWebSocket(`${proto}//${window.location.host}/ws/live`);
      wsRef.current = ws;
      let openedOnce = false;

      ws.onopen = () => {
        openedOnce = true;
        if (!unmountedRef.current) {
          setWsStatus('ws_open');
        }
      };

      ws.onmessage = (evt) => {
        try {
          const parsed = JSON.parse(evt.data);
          if (parsed.type === 'error') {
            setFeedback({ type: 'err', text: parsed.detail || 'WebSocket command failed.' });
            return;
          }
          applyFeedPayload(parsed);
        } catch {
          // Ignore malformed message
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (unmountedRef.current || paused) return;
        if (!openedOnce) {
          startSseFallback();
        } else {
          setWsStatus('reconnecting');
          reconnectTimerRef.current = window.setTimeout(connectStream, 2000);
        }
      };

      ws.onerror = () => {
        if (!openedOnce) {
          ws.close();
        }
      };
    } catch {
      startSseFallback();
    }
  }, [applyFeedPayload, closeTransports, paused]);

  useEffect(() => {
    unmountedRef.current = false;
    void fetchInitialSnapshot();
    if (!paused) {
      connectStream();
    } else {
      closeTransports();
      setWsStatus('paused');
    }
    return () => {
      unmountedRef.current = true;
      closeTransports();
    };
  }, [paused, connectStream, closeTransports, fetchInitialSnapshot]);

  const searchInstruments = useCallback(async (q: string, instType = 'ALL') => {
    const seq = ++instrSearchSeqRef.current;
    try {
      const params = new URLSearchParams({ q, limit: '16' });
      if (instType && instType !== 'ALL') {
        params.set('instrument_type', instType);
      }
      const res = await apiFetch(`/api/instruments/search?${params.toString()}`);
      if (seq !== instrSearchSeqRef.current) return;
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setInstrFeedback({
          type: 'err',
          text: err.detail || `Instrument search failed (HTTP ${res.status}).`,
        });
        return;
      }
      const body = await res.json();
      if (seq !== instrSearchSeqRef.current) return;
      const rows: LookupHit[] = body.results ?? body.items ?? [];
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
      // Ignore transient startup error
    }
  }, []);

  const fetchFnoContracts = useCallback(
    async (
      opts?: {
        underlying?: string;
        exchange?: string;
        contractType?: string;
        expiry?: string;
        strike?: string;
      },
      syncSubscribeTarget = false
    ) => {
      const seq = ++fnoFetchSeqRef.current;
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
        const res = await apiFetch(`/api/instruments/fno?${params.toString()}`);
        if (seq !== fnoFetchSeqRef.current || !res.ok) return;
        const payload: FnoContractsPayload = await res.json();
        if (seq !== fnoFetchSeqRef.current) return;
        setFnoData(payload);
        if (syncSubscribeTarget) {
          const contracts = payload.contracts || [];
          const preferredBroker = feedState?.primary_feed || 'upstox';
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
            setSymbolInput(match.trading_symbol);
            setExchangeInput(match.exchange || exch || 'NFO');
            setSelectedInstrumentType(match.instrument_type || exactType);
            setSelectedInstrumentKey(match.instrument_key || '');
          } else {
            setExchangeInput(exch || 'NFO');
            setSelectedInstrumentType(exactType);
            setSelectedInstrumentKey('');
          }
        }
      } catch {
        // ignore transient error
      } finally {
        if (seq === fnoFetchSeqRef.current) setLoadingFno(false);
      }
    },
    [feedState?.primary_feed, fnoContractType, fnoExchange, fnoExpiry, fnoStrike, fnoUnderlying]
  );

  const handleSelectInstrument = (item: LookupHit) => {
    setSymbolInput(item.trading_symbol);
    setExchangeInput(item.exchange || 'NSE');
    setSelectedInstrumentType(item.instrument_type || '');
    setSelectedInstrumentKey(item.instrument_key || '');
    setFeedback(null);
    if (['NFO', 'BFO', 'MCX', 'NSE_FO', 'BSE_FO', 'MCX_FO'].includes(item.exchange)) {
      setFnoOpen(true);
      setFnoExchange(item.exchange);
      if (item.asset_symbol) setFnoUnderlying(item.asset_symbol);
      if (['FUT', 'CE', 'PE'].includes(item.instrument_type)) {
        setFnoContractType(item.instrument_type);
      }
      setFnoExpiry(item.expiry || '');
      setFnoStrike(item.strike && item.strike > 0 ? String(item.strike) : '');
    }
  };

  const handleSyncInstruments = async (brokerId: 'upstox' | 'zerodha') => {
    setSyncingMaster(brokerId);
    setInstrFeedback(null);
    try {
      const res = await apiFetch('/api/instruments/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ broker_id: brokerId }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Failed to sync ${brokerId} instruments.`);
      if (body.stats) setInstrStats(body.stats);
      setInstrFeedback({
        type: 'ok',
        text: `Synced ${Number(body.indexed_count || 0).toLocaleString()} ${brokerId.toUpperCase()} instruments into local SQLite.`,
      });
      await searchInstruments(instrQuery, instrTypeFilter);
      if (fnoOpen) await fetchFnoContracts();
    } catch (err) {
      setInstrFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Instrument sync failed.',
      });
    } finally {
      setSyncingMaster(null);
    }
  };

  useEffect(() => {
    void searchInstruments('', 'ALL');
  }, [searchInstruments]);

  const quotes = useMemo(() => feedState?.quotes ?? [], [feedState]);
  const subscribedSet = useMemo(() => {
    const s = new Set<string>();
    for (const q of quotes) {
      s.add(assetKey(q.symbol, q.exchange || 'NSE'));
    }
    return s;
  }, [quotes]);

  // Keep a valid selected asset automatically
  useEffect(() => {
    if (quotes.length === 0) {
      setSelectedKey('');
      return;
    }
    const exists = quotes.some((q) => assetKey(q.symbol, q.exchange || 'NSE') === selectedKey);
    if (!exists) {
      setSelectedKey(assetKey(quotes[0].symbol, quotes[0].exchange || 'NSE'));
    }
  }, [quotes, selectedKey]);

  const selectedQuote = useMemo(() => {
    if (!quotes.length) return null;
    return quotes.find((q) => assetKey(q.symbol, q.exchange || 'NSE') === selectedKey) ?? quotes[0];
  }, [quotes, selectedKey]);

  const activeSlot = useMemo(
    () => feedState?.slots?.find((s) => s.active) ?? feedState?.slots?.[0] ?? null,
    [feedState]
  );

  const subscribeAsset = async (
    symRaw: string,
    exchRaw: string,
    itypeRaw = '',
    ikeyRaw = ''
  ) => {
    const cleanSym = symRaw.trim().toUpperCase();
    const cleanExch = (exchRaw || 'NSE').trim().toUpperCase();
    if (!cleanSym) {
      setFeedback({ type: 'err', text: 'Select an instrument from SQLite Lookup / F&O Finder or enter a valid symbol first.' });
      return;
    }
    setSubscribing(true);
    setFeedback(null);

    try {
      const res = await apiFetch('/api/live/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: cleanSym,
          exchange: cleanExch,
          instrument_type: itypeRaw,
          instrument_key: ikeyRaw,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not subscribe asset.');
      const subSym = data.subscribed_asset?.symbol || cleanSym;
      const subExch = data.subscribed_asset?.exchange || cleanExch;
      setSelectedKey(assetKey(subSym, subExch));
      applyFeedPayload(data);
      setFeedback({ type: 'ok', text: data.message || `Subscribed to ${subExch}:${subSym}.` });
      setSymbolInput('');
      setSelectedInstrumentType('');
      setSelectedInstrumentKey('');
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Subscription failed.',
      });
    } finally {
      setSubscribing(false);
    }
  };

  const unsubscribeAsset = async (symbol: string, exchange: string) => {
    setFeedback(null);
    try {
      const res = await apiFetch('/api/live/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, exchange }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not unsubscribe asset.');
      applyFeedPayload(data);
      setFeedback({ type: 'ok', text: data.message || `Unsubscribed ${exchange}:${symbol}.` });
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Unsubscribe failed.',
      });
    }
  };

  const handleManualPoll = async () => {
    setPolling(true);
    try {
      const res = await apiFetch('/api/live/feeds/poll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Tick poll failed.');
      applyFeedPayload(data);
      setFeedback({ type: 'ok', text: data.message || 'Polled live market snapshot.' });
    } catch (err) {
      setFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Live poll failed.' });
    } finally {
      setPolling(false);
    }
  };

  const handleFailoverToggle = async () => {
    const endpoint = feedState?.in_failover ? '/api/live/feeds/recover' : '/api/live/feeds/failover';
    try {
      const res = await apiFetch(endpoint, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Feed switch failed.');
      applyFeedPayload(data);
      setFeedback({ type: 'ok', text: data.message || 'Feed route updated.' });
    } catch (err) {
      setFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Feed switch failed.' });
    }
  };

  const fetchSelectedDepth = useCallback(
    async (symbol: string, exchange: string, levels: 5 | 30) => {
      setDepthLoading(true);
      try {
        const qs = new URLSearchParams({
          symbol,
          exchange,
          levels: String(levels),
        });
        const res = await apiFetch(`/api/live/depth?${qs.toString()}`);
        if (res.ok) {
          const data = await res.json();
          const d = (data.depth ?? data) as MarketDepthSnapshot;
          if (d && (Array.isArray(d.bids) || Array.isArray(d.asks))) {
            setFetchedDepth(d);
          } else {
            setFetchedDepth(null);
          }
        }
      } catch {
        // fallback cleanly to live quote depth if available
      } finally {
        setDepthLoading(false);
      }
    },
    [],
  );

  const handleDepthLevelSwitch = async (nextLevels: 5 | 30) => {
    setDepthLevels(nextLevels);
    setSwitchingDepth(true);
    try {
      const res = await apiFetch('/api/live/depth/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ levels: nextLevels }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        setFeedback({
          type: 'ok',
          text:
            body.message ||
            `Market depth stream mode switched to L${nextLevels} (${nextLevels === 30 ? 'Upstox full_d30 / max supported' : 'standard L5'}).`,
        });
      }
      if (selectedQuote) {
        await fetchSelectedDepth(selectedQuote.symbol, selectedQuote.exchange || 'NSE', nextLevels);
      }
    } catch (err) {
      setFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Failed to switch depth mode.' });
    } finally {
      setSwitchingDepth(false);
    }
  };

  useEffect(() => {
    if (!selectedQuote) {
      setFetchedDepth(null);
      return;
    }
    void fetchSelectedDepth(selectedQuote.symbol, selectedQuote.exchange || 'NSE', depthLevels);
  }, [selectedQuote?.symbol, selectedQuote?.exchange, depthLevels, fetchSelectedDepth]);

  const activeDepth: MarketDepthSnapshot | null = useMemo(() => {
    const liveD = selectedQuote?.depth;
    if (liveD && ((liveD.bids?.length ?? 0) > 0 || (liveD.asks?.length ?? 0) > 0)) {
      if (
        fetchedDepth &&
        (fetchedDepth.bids?.length ?? 0) > (liveD.bids?.length ?? 0) &&
        depthLevels === 30
      ) {
        return fetchedDepth;
      }
      return liveD;
    }
    return fetchedDepth;
  }, [selectedQuote?.depth, fetchedDepth, depthLevels]);

  // Sparkline geometry calculation from real WebSocket history
  const sparkline = useMemo(() => {
    const pts = (selectedQuote?.history ?? []).filter((h) => typeof h.ltp === 'number');
    if (pts.length === 0) return null;
    const vals = pts.map((p) => p.ltp);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || Math.max(min * 0.002, 1);
    const w = 360;
    const h = 88;
    const coords = pts.map((p, idx) => {
      const x = pts.length === 1 ? w / 2 : (idx / (pts.length - 1)) * w;
      const y = h - 8 - ((p.ltp - min) / span) * (h - 16);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const isPositive = (selectedQuote?.change_pct ?? 0) >= 0;
    return {
      points: coords.join(' '),
      lastX: pts.length === 1 ? w / 2 : w,
      lastY: Number(coords[coords.length - 1].split(',')[1]),
      min,
      max,
      stroke: isPositive ? '#16a34a' : '#dc2626',
    };
  }, [selectedQuote]);

  const statusMeta = useMemo(() => {
    if (wsStatus === 'ws_open') return { label: 'WebSocket Connected', dot: 'bg-emerald-600 shadow-[0_0_8px_rgba(22,163,74,0.75)]' };
    if (wsStatus === 'sse_open') return { label: 'EventStream Active', dot: 'bg-emerald-600' };
    if (wsStatus === 'paused') return { label: 'Stream Paused', dot: 'bg-slate-500' };
    if (wsStatus === 'reconnecting') return { label: 'Reconnecting...', dot: 'bg-amber-500' };
    return { label: 'Connecting Socket...', dot: 'bg-amber-500' };
  }, [wsStatus]);

  return (
    <div className="min-h-screen bg-[#0b1120] text-slate-100">
      {/* 3-Zone Top Bar */}
      <header className="border-b border-slate-800 bg-[#0b1120]">
        <div className="mx-auto flex h-16 max-w-[1280px] items-center justify-between gap-8 px-6">
          <div className="flex items-center gap-4">
            <button
              type="button"
              onClick={onNavigateHome}
              className="inline-flex items-center gap-1.5 whitespace-nowrap text-lg font-bold tracking-tight text-slate-100 hover:text-sky-400"
            >
              AlgoDesk
            </button>
            <span className="hidden text-xs font-medium text-slate-500 sm:inline">/ Live Terminal</span>
          </div>

          <nav className="hidden items-center gap-7 md:flex" aria-label="Terminal Navigation">
            <button
              type="button"
              onClick={onNavigateHome}
              className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span>Market Hub</span>
            </button>
            <a
              href="#terminal-subscribe"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Subscribe
            </a>
            <a
              href="#market-watch"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Market Watch
            </a>
            <a
              href="#tick-inspector"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Inspector
            </a>
            <a
              href="#tick-tape"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Tick Tape
            </a>
          </nav>

          <button
            type="button"
            onClick={() => setPaused((prev) => !prev)}
            className="inline-flex min-h-[40px] shrink-0 items-center gap-2 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-[13px] font-semibold text-sky-950 transition-colors hover:bg-sky-300"
          >
            {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
            <span>{paused ? 'Resume Stream' : 'Pause Stream'}</span>
          </button>
        </div>
      </header>

      <main className="mx-auto flex max-w-[1280px] flex-col gap-6 px-6 py-8 pb-14">
        {/* Terminal Header & Stream Telemetry Strip */}
        <section className="rounded-xl border border-slate-800 bg-[#111827] px-5 py-4">
          <div className="flex flex-col justify-between gap-4 border-b border-slate-800 pb-4 lg:flex-row lg:items-center">
            <div className="space-y-1">
              <div className="flex items-center gap-2.5">
                <span className={`h-2 w-2 shrink-0 rounded-full ${statusMeta.dot}`} aria-hidden="true" />
                <h1 className="text-xl font-bold tracking-tight text-slate-100">Live WebSocket Market Terminal</h1>
              </div>
              <p className="text-[13px] text-slate-400">
                Real-time quote streaming over <span className="font-mono text-slate-300">/ws/live</span> across subscribed Equities, Indices, and F&amp;O contracts.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2.5">
              <button
                type="button"
                onClick={() => void handleManualPoll()}
                disabled={polling}
                className="inline-flex min-h-[38px] items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-1.5 text-[13px] font-semibold text-slate-200 hover:bg-slate-800 disabled:opacity-50"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${polling ? 'animate-spin' : ''}`} />
                <span>{polling ? 'Polling...' : 'Poll Snapshot'}</span>
              </button>
              <button
                type="button"
                onClick={() => void handleFailoverToggle()}
                className="inline-flex min-h-[38px] items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-700 bg-[#0b1120] px-3.5 py-1.5 text-[13px] font-semibold text-slate-200 hover:bg-slate-800"
              >
                <Activity className="h-3.5 w-3.5 text-sky-400" />
                <span>{feedState?.in_failover ? 'Restore Primary Feed' : 'Switch Standby Feed'}</span>
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 pt-4 md:grid-cols-5">
            <div>
              <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">Socket Link</span>
              <span className="mt-1 inline-flex items-center gap-1.5 font-mono text-[13px] font-medium text-slate-100">
                {wsStatus === 'paused' || wsStatus === 'reconnecting' ? (
                  <WifiOff className="h-3.5 w-3.5 text-amber-400" />
                ) : (
                  <Wifi className="h-3.5 w-3.5 text-emerald-400" />
                )}
                <span>{statusMeta.label}</span>
              </span>
            </div>
            <div>
              <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">Active Provider</span>
              <span className="mt-1 block font-mono text-[13px] font-medium text-slate-100">
                {(feedState?.active_feed || 'upstox').toUpperCase()}
                {feedState?.in_failover ? ' (Standby Failover)' : ' (Primary)'}
              </span>
            </div>
            <div>
              <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">Feed Latency</span>
              <span className="mt-1 block font-mono text-[13px] tabular-nums text-slate-100">
                {activeSlot?.latency_ms != null ? `${activeSlot.latency_ms} ms` : '—'}
              </span>
            </div>
            <div>
              <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">WS Frame Seq</span>
              <span className="mt-1 block font-mono text-[13px] tabular-nums text-slate-100">
                #{feedState?.tick_seq ?? 0} · {fmtShortTime(activeSlot?.last_tick_ist)} IST
              </span>
            </div>
            <div>
              <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">Watchlist Assets</span>
              <span className="mt-1 block font-mono text-[13px] tabular-nums text-slate-100">
                {quotes.length} subscribed
              </span>
            </div>
          </div>
        </section>

        {/* Subscribe / Unsubscribe Asset via SQLite Instrument Index (mirrors Download section) */}
        <section id="terminal-subscribe" className="space-y-6 rounded-xl border border-slate-800 bg-[#111827] p-6">
          <div className="flex flex-wrap items-end justify-between gap-4 border-b border-slate-800 pb-4">
            <div>
              <h2 className="text-base font-semibold tracking-tight text-slate-100">
                Subscribe Asset to Live WebSocket Stream
              </h2>
              <p className="text-xs text-slate-400">
                Supports Equity, Index, Futures &amp; Options (NFO/BFO/MCX). Look up or bind a verified contract from SQLite before subscribing.
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

          {/* 1. SQLite Instrument Lookup & Master Sync */}
          <div className="space-y-3 border-b border-slate-800 pb-6">
            <div className="grid grid-cols-1 items-end gap-3 md:grid-cols-[1fr_200px_auto_auto]">
              <div>
                <label htmlFor="live-instr-search" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                  SQLite Instrument Lookup (Under 5 MB RAM)
                </label>
                <div className="relative">
                  <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <input
                    id="live-instr-search"
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
                <label htmlFor="live-instr-type-filter" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                  Segment / Contract
                </label>
                <select
                  id="live-instr-type-filter"
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
                        symbolInput === item.trading_symbol && exchangeInput === item.exchange
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

          {/* 2. Futures & Options (F&O) Contract Finder */}
          <div className="space-y-4 border-b border-slate-800 pb-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-slate-100">
                  Futures &amp; Options Contract Finder (NFO / BFO / MCX)
                </div>
                <p className="text-xs text-slate-400">
                  Pick an underlying, contract type (FUT / CE / PE), expiry, and strike from local SQLite before subscribing to the live stream.
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  const next = !fnoOpen;
                  setFnoOpen(next);
                  if (next) {
                    const targetEx = ['NFO', 'BFO', 'MCX'].includes(exchangeInput) ? exchangeInput : fnoExchange;
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
                    <label htmlFor="live-fno-exchange" className="mb-1.5 block text-xs font-semibold text-slate-300">
                      F&amp;O Exchange
                    </label>
                    <select
                      id="live-fno-exchange"
                      value={fnoExchange}
                      onChange={(e) => {
                        const ex = e.target.value;
                        setFnoExchange(ex);
                        setExchangeInput(ex);
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
                    <label htmlFor="live-fno-underlying" className="mb-1.5 block text-xs font-semibold text-slate-300">
                      Underlying
                    </label>
                    <input
                      id="live-fno-underlying"
                      type="text"
                      list="live-fno-underlying-list"
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
                    <datalist id="live-fno-underlying-list">
                      {(fnoData?.underlyings ?? []).map((ul) => (
                        <option key={ul} value={ul} />
                      ))}
                    </datalist>
                  </div>

                  <div>
                    <label htmlFor="live-fno-type" className="mb-1.5 block text-xs font-semibold text-slate-300">
                      Contract Type
                    </label>
                    <select
                      id="live-fno-type"
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
                    <label htmlFor="live-fno-expiry" className="mb-1.5 block text-xs font-semibold text-slate-300">
                      Expiry Date
                    </label>
                    <select
                      id="live-fno-expiry"
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
                    <label htmlFor="live-fno-strike" className="mb-1.5 block text-xs font-semibold text-slate-300">
                      Strike Price
                    </label>
                    <select
                      id="live-fno-strike"
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
                          key={`live-fno-${item.broker_id}-${item.instrument_key}`}
                          type="button"
                          onClick={() => handleSelectInstrument(item)}
                          className={`min-h-[32px] shrink-0 whitespace-nowrap rounded-md border px-2.5 py-1 font-mono text-xs tabular-nums transition-colors ${
                            symbolInput === item.trading_symbol && selectedInstrumentKey === item.instrument_key
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

          {/* 3. Bound Target Subscribe Form & Feedback */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void subscribeAsset(symbolInput, exchangeInput, selectedInstrumentType, selectedInstrumentKey);
            }}
            className="space-y-3"
          >
            <div className="grid grid-cols-1 items-end gap-4 sm:grid-cols-2 lg:grid-cols-[1.4fr_0.9fr_auto]">
              <div>
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <label htmlFor="ws-symbol-input" className="block text-[13px] font-semibold text-slate-200">
                    Symbol / Contract
                  </label>
                  {selectedInstrumentKey && (
                    <span className="font-mono text-[11px] text-sky-400">{selectedInstrumentKey}</span>
                  )}
                </div>
                <input
                  id="ws-symbol-input"
                  type="text"
                  value={symbolInput}
                  onChange={(e) => {
                    setSymbolInput(e.target.value.toUpperCase());
                    setSelectedInstrumentType('');
                    setSelectedInstrumentKey('');
                  }}
                  placeholder="RELIANCE or NIFTY26NOVFUT"
                  autoComplete="off"
                  className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 font-mono text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-400 focus:outline-none"
                />
              </div>

              <div>
                <label htmlFor="ws-exchange-select" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                  Exchange
                </label>
                <select
                  id="ws-exchange-select"
                  value={exchangeInput}
                  onChange={(e) => {
                    const ex = e.target.value;
                    setExchangeInput(ex);
                    setSelectedInstrumentKey('');
                    if (['NFO', 'BFO', 'MCX'].includes(ex)) {
                      setFnoOpen(true);
                      setFnoExchange(ex);
                      void fetchFnoContracts({ exchange: ex, expiry: '', strike: '' }, true);
                    }
                  }}
                  className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                >
                  <option value="NSE">NSE (Equity / Index)</option>
                  <option value="BSE">BSE (Equity / Index)</option>
                  <option value="NFO">NFO (NSE F&amp;O)</option>
                  <option value="BFO">BFO (BSE F&amp;O)</option>
                  <option value="MCX">MCX (Commodity F&amp;O)</option>
                  <option value="US">US (Global Equity)</option>
                </select>
              </div>

              <button
                type="submit"
                disabled={subscribing}
                className="inline-flex min-h-[42px] shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-sky-400 px-5 py-2 text-[13px] font-semibold text-sky-950 transition-colors hover:bg-sky-300 disabled:opacity-50"
              >
                <Plus className="h-4 w-4" />
                <span>{subscribing ? 'Validating & Subscribing...' : 'Subscribe Asset'}</span>
              </button>
            </div>

            {feedback && (
              <div
                className={`flex items-center gap-2 text-[13px] ${
                  feedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'
                }`}
                role="status"
                aria-live="polite"
              >
                {feedback.type === 'ok' ? <Check className="h-4 w-4 shrink-0" /> : <AlertCircle className="h-4 w-4 shrink-0" />}
                <span>{feedback.text}</span>
              </div>
            )}
          </form>

          {/* Quick Asset Presets */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-slate-800/80 pt-3 text-xs">
            <span className="font-medium text-slate-500">Quick Toggle:</span>
            {PRESET_ASSETS.map((preset) => {
              const isSubscribed = subscribedSet.has(assetKey(preset.symbol, preset.exchange));
              return (
                <button
                  key={`${preset.exchange}:${preset.symbol}`}
                  type="button"
                  onClick={() =>
                    isSubscribed
                      ? void unsubscribeAsset(preset.symbol, preset.exchange)
                      : void subscribeAsset(preset.symbol, preset.exchange, preset.instrument_type)
                  }
                  className={`inline-flex items-center gap-1 font-mono transition-colors ${
                    isSubscribed
                      ? 'text-emerald-400 hover:text-red-400'
                      : 'text-slate-400 hover:text-sky-400'
                  }`}
                  title={isSubscribed ? `Click to unsubscribe ${preset.symbol}` : `Click to subscribe ${preset.symbol}`}
                >
                  <span>{isSubscribed ? '✓' : '+'}</span>
                  <span>{preset.symbol}</span>
                </button>
              );
            })}
          </div>
        </section>

        {/* Main 2-Column Trading Workspace */}
        <section className="grid grid-cols-1 items-stretch gap-6 lg:grid-cols-[1.65fr_1fr]">
          {/* Left: Market Watch Quote Table */}
          <div id="market-watch" className="flex flex-col rounded-xl border border-slate-800 bg-[#111827] overflow-hidden">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3.5">
              <div>
                <h2 className="text-sm font-semibold text-slate-100">Market Watch · Live WebSocket Quotes</h2>
                <p className="text-xs text-slate-400">
                  Click any row to inspect OHLCV, Bid/Ask depth, and real-time tick trajectory.
                </p>
              </div>
              <span className="font-mono text-xs text-slate-400">
                {quotes.length} {quotes.length === 1 ? 'asset' : 'assets'}
              </span>
            </div>

            {quotes.length === 0 ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 py-14 text-center">
                <p className="text-sm font-medium text-slate-300">No assets subscribed to the WebSocket feed.</p>
                <p className="text-xs text-slate-500">Use the Subscribe toolbar above or Quick Toggle presets to start streaming quotes.</p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      <th className="px-4 py-2.5">Asset</th>
                      <th className="px-3 py-2.5 text-right">LTP</th>
                      <th className="px-3 py-2.5 text-right">Chg / %</th>
                      <th className="px-3 py-2.5 text-right">Bid / Ask</th>
                      <th className="px-3 py-2.5 text-right">High / Low</th>
                      <th className="px-3 py-2.5 text-right">Vol / OI</th>
                      <th className="px-4 py-2.5 text-right">Subscription</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/80">
                    {quotes.map((q) => {
                      const exch = q.exchange || 'NSE';
                      const key = assetKey(q.symbol, exch);
                      const isSelected = selectedQuote && assetKey(selectedQuote.symbol, selectedQuote.exchange || 'NSE') === key;
                      const chgPct = q.change_pct ?? 0;
                      const isUp = q.direction === 'up' || (q.direction !== 'down' && chgPct > 0);
                      const isDown = q.direction === 'down' || (q.direction !== 'up' && chgPct < 0);

                      return (
                        <tr
                          key={key}
                          onClick={() => setSelectedKey(key)}
                          className={`cursor-pointer transition-colors ${
                            isSelected ? 'bg-slate-800/70' : 'hover:bg-slate-800/35'
                          }`}
                        >
                          <td className="px-4 py-3">
                            <div className="font-mono text-[13px] font-semibold text-slate-100">{q.symbol}</div>
                            <div className="text-[11px] text-slate-400">
                              {exch} · {q.instrument_type || 'EQUITY'} · {(q.source || '').toUpperCase()}
                            </div>
                            {q.note && <div className="mt-0.5 text-[11px] text-amber-400">{q.note}</div>}
                          </td>

                          <td className="px-3 py-3 text-right font-mono tabular-nums">
                            <div
                              className={`inline-flex items-center justify-end gap-1 font-semibold ${
                                q.ltp === null
                                  ? 'text-slate-500'
                                  : q.direction === 'up'
                                    ? 'text-emerald-400'
                                    : q.direction === 'down'
                                      ? 'text-red-400'
                                      : 'text-slate-100'
                              }`}
                            >
                              {q.direction === 'up' && <ArrowUpRight className="h-3.5 w-3.5" />}
                              {q.direction === 'down' && <ArrowDownRight className="h-3.5 w-3.5" />}
                              <span>{fmtPrice(q.ltp)}</span>
                            </div>
                            <div className="text-[11px] text-slate-500">{fmtShortTime(q.ts_ist)}</div>
                          </td>

                          <td className="px-3 py-3 text-right font-mono tabular-nums">
                            {q.change_pct === null || q.change_pct === undefined ? (
                              <span className="text-slate-500">—</span>
                            ) : (
                              <>
                                <div className={isUp ? 'text-emerald-400' : isDown ? 'text-red-400' : 'text-slate-300'}>
                                  {chgPct > 0 ? '+' : ''}
                                  {chgPct.toFixed(2)}%
                                </div>
                                <div className="text-[11px] text-slate-400">
                                  {q.change !== null && q.change !== undefined
                                    ? `${q.change > 0 ? '+' : ''}${q.change.toFixed(2)}`
                                    : '—'}
                                </div>
                              </>
                            )}
                          </td>

                          <td className="px-3 py-3 text-right font-mono text-xs tabular-nums text-slate-300">
                            <div>{fmtPrice(q.bid)}</div>
                            <div className="text-slate-500">{fmtPrice(q.ask)}</div>
                          </td>

                          <td className="px-3 py-3 text-right font-mono text-xs tabular-nums text-slate-300">
                            <div>H {fmtPrice(q.high)}</div>
                            <div className="text-slate-500">L {fmtPrice(q.low)}</div>
                          </td>

                          <td className="px-3 py-3 text-right font-mono text-xs tabular-nums text-slate-300">
                            <div>{fmtInt(q.volume)}</div>
                            {q.oi ? <div className="text-slate-400">OI {fmtInt(q.oi)}</div> : <div className="text-slate-600">—</div>}
                          </td>

                          <td className="px-4 py-3 text-right">
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                void unsubscribeAsset(q.symbol, exch);
                              }}
                              className="inline-flex min-h-[30px] items-center gap-1 whitespace-nowrap rounded-md border border-slate-700 bg-[#0b1120] px-2.5 py-1 text-xs font-medium text-slate-300 hover:border-red-700/70 hover:text-red-400"
                            >
                              <Trash2 className="h-3 w-3" />
                              <span>Unsubscribe</span>
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Right Column: Selected Inspector + WebSocket Tick Tape */}
          <div className="flex flex-col gap-6">
            {/* Selected Asset Inspector */}
            <div id="tick-inspector" className="rounded-xl border border-slate-800 bg-[#111827] p-5">
              <div className="flex items-start justify-between gap-4 border-b border-slate-800 pb-4">
                <div>
                  <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                    Selected Quote Inspector
                  </span>
                  <h3 className="mt-1 font-mono text-lg font-bold text-slate-100">
                    {selectedQuote ? `${selectedQuote.exchange || 'NSE'}:${selectedQuote.symbol}` : 'No Asset Selected'}
                  </h3>
                  {selectedQuote && (
                    <span className="text-xs text-slate-400">
                      {selectedQuote.instrument_type || 'EQUITY'} · Feed {(selectedQuote.source || '').toUpperCase()} · {fmtShortTime(selectedQuote.ts_ist)} IST
                    </span>
                  )}
                </div>

                {selectedQuote && (
                  <div className="text-right font-mono tabular-nums">
                    <div className="text-xl font-bold text-slate-100">{fmtPrice(selectedQuote.ltp)}</div>
                    <div
                      className={`text-xs font-medium ${
                        (selectedQuote.change_pct ?? 0) > 0
                          ? 'text-emerald-400'
                          : (selectedQuote.change_pct ?? 0) < 0
                            ? 'text-red-400'
                            : 'text-slate-400'
                      }`}
                    >
                      {selectedQuote.change_pct !== null && selectedQuote.change_pct !== undefined
                        ? `${selectedQuote.change_pct > 0 ? '+' : ''}${selectedQuote.change_pct.toFixed(2)}%`
                        : '—'}
                    </div>
                  </div>
                )}
              </div>

              {selectedQuote ? (
                <>
                  {selectedQuote.note && (
                    <p className="mt-3 text-xs text-amber-400">{selectedQuote.note}</p>
                  )}

                  <div className="mt-4 grid grid-cols-4 gap-3 border-b border-slate-800 pb-4 font-mono text-xs tabular-nums">
                    <div>
                      <span className="block text-[11px] text-slate-500">Open</span>
                      <span className="mt-0.5 block text-slate-200">{fmtPrice(selectedQuote.open)}</span>
                    </div>
                    <div>
                      <span className="block text-[11px] text-slate-500">High</span>
                      <span className="mt-0.5 block text-slate-200">{fmtPrice(selectedQuote.high)}</span>
                    </div>
                    <div>
                      <span className="block text-[11px] text-slate-500">Low</span>
                      <span className="mt-0.5 block text-slate-200">{fmtPrice(selectedQuote.low)}</span>
                    </div>
                    <div>
                      <span className="block text-[11px] text-slate-500">Prev Close</span>
                      <span className="mt-0.5 block text-slate-200">{fmtPrice(selectedQuote.prev_close)}</span>
                    </div>
                  </div>

                  <div className="mt-3 grid grid-cols-3 gap-3 font-mono text-xs tabular-nums">
                    <div>
                      <span className="block text-[11px] text-slate-500">Volume</span>
                      <span className="mt-0.5 block text-slate-200">{fmtInt(selectedQuote.volume)}</span>
                    </div>
                    <div>
                      <span className="block text-[11px] text-slate-500">Open Interest</span>
                      <span className="mt-0.5 block text-slate-200">{fmtInt(selectedQuote.oi)}</span>
                    </div>
                    <div>
                      <span className="block text-[11px] text-slate-500">Best Bid / Ask</span>
                      <span className="mt-0.5 block text-slate-200">
                        {fmtPrice(selectedQuote.bid)} / {fmtPrice(selectedQuote.ask)}
                      </span>
                    </div>
                  </div>

                  {/* Live Session Tick Sparkline */}
                  <div className="mt-4 rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                    <div className="mb-2 flex items-center justify-between text-[11px] text-slate-400">
                      <span>Session Tick Trajectory ({selectedQuote.history?.length ?? 0} samples)</span>
                      {sparkline && (
                        <span className="font-mono tabular-nums">
                          {fmtPrice(sparkline.min)} – {fmtPrice(sparkline.max)}
                        </span>
                      )}
                    </div>
                    {sparkline ? (
                      <svg viewBox="0 0 360 88" className="h-20 w-full overflow-visible" aria-label="Live tick sparkline">
                        <polyline
                          fill="none"
                          stroke={sparkline.stroke}
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          points={sparkline.points}
                        />
                        <circle cx={sparkline.lastX} cy={sparkline.lastY} r="3.5" fill={sparkline.stroke} />
                      </svg>
                    ) : (
                      <div className="flex h-20 items-center justify-center text-xs text-slate-500">
                        Waiting for live ticks...
                      </div>
                    )}
                  </div>

                  {/* Level 5 / Level 30 Market Depth Order Book Ladder */}
                  <div className="mt-4 rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                    <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">
                          Market Depth (L{depthLevels})
                        </span>
                        {selectedQuote.execution_grade === false && (
                          <span className="rounded border border-amber-700/70 bg-amber-950/40 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300">
                            Delayed Feed
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5">
                        <div className="inline-flex rounded-md border border-slate-700 bg-[#111827] p-0.5 text-[11px] font-semibold">
                          <button
                            type="button"
                            disabled={switchingDepth}
                            onClick={() => void handleDepthLevelSwitch(5)}
                            className={`rounded px-2 py-0.5 transition-colors ${
                              depthLevels === 5
                                ? 'bg-sky-500 text-slate-950'
                                : 'text-slate-400 hover:text-slate-200'
                            }`}
                          >
                            L5
                          </button>
                          <button
                            type="button"
                            disabled={switchingDepth}
                            onClick={() => void handleDepthLevelSwitch(30)}
                            className={`rounded px-2 py-0.5 transition-colors ${
                              depthLevels === 30
                                ? 'bg-sky-500 text-slate-950'
                                : 'text-slate-400 hover:text-slate-200'
                            }`}
                            title="Upstox v3 full_d30 (30 levels) or broker maximum"
                          >
                            L30
                          </button>
                        </div>
                        <button
                          type="button"
                          disabled={depthLoading}
                          onClick={() =>
                            void fetchSelectedDepth(
                              selectedQuote.symbol,
                              selectedQuote.exchange || 'NSE',
                              depthLevels,
                            )
                          }
                          className="rounded border border-slate-700 p-1 text-slate-400 hover:border-sky-400 hover:text-slate-200 disabled:opacity-50"
                          title="Refresh order book depth"
                        >
                          <RefreshCw className={`h-3 w-3 ${depthLoading ? 'animate-spin' : ''}`} />
                        </button>
                      </div>
                    </div>

                    {activeDepth && ((activeDepth.bids?.length ?? 0) > 0 || (activeDepth.asks?.length ?? 0) > 0) ? (
                      (() => {
                        const bids = (activeDepth.bids || []).slice(0, depthLevels);
                        const asks = (activeDepth.asks || []).slice(0, depthLevels);
                        const rows = Math.max(bids.length, asks.length);
                        const maxQty = Math.max(
                          1,
                          ...bids.map((b) => Number(b.quantity || 0)),
                          ...asks.map((a) => Number(a.quantity || 0)),
                        );
                        const tbq = Number(
                          activeDepth.total_buy_qty ??
                            activeDepth.tbq ??
                            bids.reduce((acc, b) => acc + Number(b.quantity || 0), 0),
                        );
                        const tsq = Number(
                          activeDepth.total_sell_qty ??
                            activeDepth.tsq ??
                            asks.reduce((acc, a) => acc + Number(a.quantity || 0), 0),
                        );
                        const buyPct = tbq + tsq > 0 ? Math.round((tbq / (tbq + tsq)) * 100) : 50;
                        const bestBid = bids[0]?.price;
                        const bestAsk = asks[0]?.price;
                        const spreadVal =
                          activeDepth.spread ??
                          (bestBid && bestAsk && bestAsk >= bestBid ? bestAsk - bestBid : null);
                        const spreadBps =
                          activeDepth.spread_bps ??
                          (spreadVal !== null && bestBid && bestAsk
                            ? (spreadVal / ((bestBid + bestAsk) / 2)) * 10000
                            : null);

                        return (
                          <div className="space-y-2 font-mono text-[11px] tabular-nums">
                            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-slate-400">
                              <span>
                                Levels: <strong className="text-slate-200">{rows}</strong>
                                {activeDepth.source ? ` · ${activeDepth.source.toUpperCase()}` : ''}
                              </span>
                              <span>
                                Spread:{' '}
                                <strong className="text-slate-200">
                                  {spreadVal !== null ? `₹${spreadVal.toFixed(2)}` : '—'}
                                  {spreadBps !== null ? ` (${spreadBps.toFixed(1)} bps)` : ''}
                                </strong>
                              </span>
                            </div>

                            {/* TBQ vs TSQ Imbalance Bar */}
                            <div>
                              <div className="mb-1 flex items-center justify-between text-[10px]">
                                <span className="text-emerald-400">TBQ {fmtInt(tbq)} ({buyPct}%)</span>
                                <span className="text-red-400">TSQ {fmtInt(tsq)} ({100 - buyPct}%)</span>
                              </div>
                              <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                                <div className="bg-emerald-500/80" style={{ width: `${buyPct}%` }} />
                                <div className="bg-red-500/80" style={{ width: `${100 - buyPct}%` }} />
                              </div>
                            </div>

                            {/* Bid / Ask Ladder */}
                            <div className={depthLevels === 30 ? 'max-h-[280px] overflow-y-auto pr-1' : ''}>
                              <div className="grid grid-cols-6 border-b border-slate-800 pb-1 text-[10px] font-semibold uppercase text-slate-500">
                                <span className="text-left">Ord</span>
                                <span className="text-right">Bid Qty</span>
                                <span className="pr-2 text-right">Bid ₹</span>
                                <span className="pl-2 text-left">Ask ₹</span>
                                <span className="text-right">Ask Qty</span>
                                <span className="text-right">Ord</span>
                              </div>
                              <div className="divide-y divide-slate-800/50">
                                {Array.from({ length: rows }).map((_, idx) => {
                                  const b = bids[idx];
                                  const a = asks[idx];
                                  const bPct = b ? Math.min(100, Math.round((Number(b.quantity || 0) / maxQty) * 100)) : 0;
                                  const aPct = a ? Math.min(100, Math.round((Number(a.quantity || 0) / maxQty) * 100)) : 0;
                                  return (
                                    <div key={idx} className="grid grid-cols-6 items-center py-1 text-[11px]">
                                      <span className="text-left text-slate-500">{b?.orders ?? '—'}</span>
                                      <span className="relative text-right text-slate-300">
                                        <span
                                          className="pointer-events-none absolute inset-y-0 right-0 rounded-sm bg-emerald-500/15"
                                          style={{ width: `${bPct}%` }}
                                        />
                                        <span className="relative">{b ? fmtInt(b.quantity) : '—'}</span>
                                      </span>
                                      <span className="pr-2 text-right font-semibold text-emerald-400">
                                        {b ? fmtPrice(b.price) : '—'}
                                      </span>
                                      <span className="pl-2 text-left font-semibold text-red-400">
                                        {a ? fmtPrice(a.price) : '—'}
                                      </span>
                                      <span className="relative text-right text-slate-300">
                                        <span
                                          className="pointer-events-none absolute inset-y-0 left-0 rounded-sm bg-red-500/15"
                                          style={{ width: `${aPct}%` }}
                                        />
                                        <span className="relative">{a ? fmtInt(a.quantity) : '—'}</span>
                                      </span>
                                      <span className="text-right text-slate-500">{a?.orders ?? '—'}</span>
                                    </div>
                                  );
                                })}
                              </div>
                            </div>
                          </div>
                        );
                      })()
                    ) : (
                      <div className="py-4 text-center text-xs text-slate-500">
                        {depthLoading
                          ? 'Loading order book depth...'
                          : 'Order book depth (L5 / L30) requires an authenticated broker feed (Upstox L5/L30 or Zerodha L5).'}
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <p className="py-6 text-xs text-slate-500">Subscribe an asset to view live price depth and sparkline telemetry.</p>
              )}
            </div>

            {/* Live WebSocket Tick Tape */}
            <div id="tick-tape" className="flex flex-1 flex-col rounded-xl border border-slate-800 bg-[#111827] overflow-hidden">
              <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
                <h3 className="text-sm font-semibold text-slate-100">Incoming WebSocket Tick Tape</h3>
                <span className="font-mono text-[11px] text-slate-500">Latest 30 ticks</span>
              </div>
              <div className="max-h-[290px] flex-1 overflow-y-auto divide-y divide-slate-800/70 font-mono text-xs tabular-nums">
                {(feedState?.tick_events ?? []).length === 0 ? (
                  <div className="px-5 py-8 text-center text-slate-500">No WebSocket ticks received yet.</div>
                ) : (
                  (feedState?.tick_events ?? []).map((ev, i) => (
                    <div
                      key={`${ev.seq}-${ev.symbol}-${i}`}
                      className="flex items-center justify-between gap-3 px-4 py-2 hover:bg-slate-800/30"
                    >
                      <div className="flex items-center gap-2.5">
                        <span className="text-slate-500">{fmtShortTime(ev.ts_ist)}</span>
                        <span className="font-semibold text-slate-200">{ev.symbol}</span>
                        <span className="text-[11px] text-slate-500">{ev.exchange}</span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span
                          className={
                            ev.direction === 'up'
                              ? 'text-emerald-400'
                              : ev.direction === 'down'
                                ? 'text-red-400'
                                : 'text-slate-200'
                          }
                        >
                          {fmtPrice(ev.ltp)}
                        </span>
                        <span className="w-14 text-right text-[11px] text-slate-400">
                          {ev.change_pct !== null && ev.change_pct !== undefined
                            ? `${ev.change_pct > 0 ? '+' : ''}${ev.change_pct.toFixed(2)}%`
                            : '—'}
                        </span>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
