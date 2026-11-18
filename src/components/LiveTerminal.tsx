import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
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

  // Subscribe form + SQLite lookup state
  const [symbolInput, setSymbolInput] = useState('');
  const [exchangeInput, setExchangeInput] = useState('NSE');
  const [selectedInstrumentType, setSelectedInstrumentType] = useState('');
  const [selectedInstrumentKey, setSelectedInstrumentKey] = useState('');
  const [lookupHits, setLookupHits] = useState<LookupHit[]>([]);
  const [lookupOpen, setLookupOpen] = useState(false);
  const [subscribing, setSubscribing] = useState(false);
  const [feedback, setFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // Selected quote in inspector
  const [selectedKey, setSelectedKey] = useState<string>('');

  const wsRef = useRef<WebSocket | null>(null);
  const sseRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const lookupDebounceRef = useRef<number | null>(null);
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
      const res = await fetch('/api/live/status');
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
        const es = new EventSource('/api/live/stream');
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
      const wsUrl = `${proto}//${window.location.host}/ws/live`;
      const ws = new WebSocket(wsUrl);
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

  // SQLite Instrument Lookup autocomplete
  useEffect(() => {
    if (lookupDebounceRef.current !== null) {
      window.clearTimeout(lookupDebounceRef.current);
    }
    const q = symbolInput.trim();
    if (q.length < 2) {
      setLookupHits([]);
      return;
    }
    lookupDebounceRef.current = window.setTimeout(async () => {
      try {
        const res = await fetch(`/api/instruments/search?q=${encodeURIComponent(q)}&limit=10`);
        const data = await res.json();
        if (res.ok && Array.isArray(data.items)) {
          setLookupHits(data.items);
        }
      } catch {
        setLookupHits([]);
      }
    }, 180);

    return () => {
      if (lookupDebounceRef.current !== null) {
        window.clearTimeout(lookupDebounceRef.current);
      }
    };
  }, [symbolInput]);

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
      setFeedback({ type: 'err', text: 'Enter a symbol or select a contract from lookup first.' });
      return;
    }
    setSubscribing(true);
    setFeedback(null);
    setLookupOpen(false);
    setSelectedKey(assetKey(cleanSym, cleanExch));

    try {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            action: 'subscribe',
            symbol: cleanSym,
            exchange: cleanExch,
            instrument_type: itypeRaw,
            instrument_key: ikeyRaw,
          })
        );
      }
      const res = await fetch('/api/live/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: cleanSym,
          exchange: cleanExch,
          instrument_type: itypeRaw,
          instrument_key: ikeyRaw,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Could not subscribe asset.');
      applyFeedPayload(data);
      setFeedback({ type: 'ok', text: data.message || `Subscribed to ${cleanExch}:${cleanSym}.` });
      setSymbolInput('');
      setSelectedInstrumentType('');
      setSelectedInstrumentKey('');
      setLookupHits([]);
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
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'unsubscribe', symbol, exchange }));
      }
      const res = await fetch('/api/live/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, exchange }),
      });
      const data = await res.json();
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
      const res = await fetch('/api/live/poll', { method: 'POST' });
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
    const endpoint = feedState?.in_failover ? '/api/live/recover-primary' : '/api/live/failover';
    try {
      const res = await fetch(endpoint, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Feed switch failed.');
      applyFeedPayload(data);
      setFeedback({ type: 'ok', text: data.message || 'Feed route updated.' });
    } catch (err) {
      setFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Feed switch failed.' });
    }
  };

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

        {/* Subscribe / Unsubscribe Asset Toolbar */}
        <section id="terminal-subscribe" className="rounded-xl border border-slate-800 bg-[#111827] px-5 py-4">
          <div className="mb-3 flex flex-col justify-between gap-2 sm:flex-row sm:items-center">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">Subscribe Any Asset to Live WebSocket Stream</h2>
              <p className="text-xs text-slate-400">
                Type an Equity (e.g. RELIANCE, AAPL), Index (NIFTY, SENSEX), or search F&amp;O contracts from your synced SQLite master.
              </p>
            </div>
            {feedback && (
              <p
                className={`text-xs font-medium ${feedback.type === 'ok' ? 'text-emerald-400' : 'text-red-400'}`}
                role="status"
                aria-live="polite"
              >
                {feedback.text}
              </p>
            )}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              void subscribeAsset(symbolInput, exchangeInput, selectedInstrumentType, selectedInstrumentKey);
            }}
            className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_140px_auto]"
          >
            <div className="relative">
              <label htmlFor="ws-symbol-input" className="sr-only">
                Asset Symbol or F&amp;O Contract
              </label>
              <div className="relative flex items-center">
                <Search className="pointer-events-none absolute left-3 h-4 w-4 text-slate-500" />
                <input
                  id="ws-symbol-input"
                  type="text"
                  value={symbolInput}
                  onFocus={() => setLookupOpen(true)}
                  onChange={(e) => {
                    setSymbolInput(e.target.value);
                    setSelectedInstrumentType('');
                    setSelectedInstrumentKey('');
                    setLookupOpen(true);
                  }}
                  placeholder="Type symbol or F&O contract (e.g. TATAMOTORS, NIFTY26NOVFUT, MSFT)"
                  autoComplete="off"
                  className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] pl-9 pr-3 text-[13px] text-slate-100 placeholder:text-slate-500 focus:border-sky-400 focus:outline-none"
                />
              </div>

              {lookupOpen && lookupHits.length > 0 && (
                <div className="absolute z-30 mt-1.5 max-h-64 w-full overflow-y-auto rounded-lg border border-slate-700 bg-[#0b1120] shadow-xl">
                  {lookupHits.map((hit) => (
                    <button
                      key={`${hit.broker_id}-${hit.exchange}-${hit.trading_symbol}-${hit.instrument_key}`}
                      type="button"
                      onClick={() => {
                        setSymbolInput(hit.trading_symbol);
                        setExchangeInput(hit.exchange || 'NSE');
                        setSelectedInstrumentType(hit.instrument_type || '');
                        setSelectedInstrumentKey(hit.instrument_key || '');
                        setLookupOpen(false);
                        void subscribeAsset(
                          hit.trading_symbol,
                          hit.exchange || 'NSE',
                          hit.instrument_type || '',
                          hit.instrument_key || ''
                        );
                      }}
                      className="flex w-full items-center justify-between border-b border-slate-800/80 px-3.5 py-2.5 text-left text-xs hover:bg-slate-800/70 last:border-b-0"
                    >
                      <div>
                        <span className="font-mono font-semibold text-slate-100">{hit.trading_symbol}</span>
                        <span className="ml-2 text-slate-400">{hit.name || hit.asset_symbol || ''}</span>
                      </div>
                      <span className="font-mono text-[11px] text-slate-400">
                        {hit.exchange} · {hit.instrument_type}
                        {hit.expiry ? ` · ${hit.expiry}` : ''}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div>
              <label htmlFor="ws-exchange-select" className="sr-only">
                Exchange
              </label>
              <select
                id="ws-exchange-select"
                value={exchangeInput}
                onChange={(e) => setExchangeInput(e.target.value)}
                className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-[13px] text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="NSE">NSE (Eq/Idx)</option>
                <option value="BSE">BSE (Eq/Idx)</option>
                <option value="NFO">NFO (NSE F&amp;O)</option>
                <option value="BFO">BFO (BSE F&amp;O)</option>
                <option value="MCX">MCX (Commodity)</option>
                <option value="US">US (Global Eq)</option>
              </select>
            </div>

            <button
              type="submit"
              disabled={subscribing}
              className="inline-flex min-h-[42px] items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-sky-400 px-4 py-2 text-[13px] font-semibold text-sky-950 hover:bg-sky-300 disabled:opacity-50"
            >
              <Plus className="h-4 w-4" />
              <span>{subscribing ? 'Subscribing...' : 'Subscribe Asset'}</span>
            </button>
          </form>

          {/* Quick Asset Presets */}
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-slate-800/80 pt-3 text-xs">
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
