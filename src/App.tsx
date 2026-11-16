import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, ExternalLink, X, Check, AlertCircle } from 'lucide-react';

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

interface OverviewPayload {
  brokers: BrokerSnapshot[];
  selections: {
    trading_broker: string;
    data_connection: string;
    updated_at: string;
  };
  credentials: {
    upstox?: {
      has_api_key: boolean;
      has_api_secret: boolean;
      api_key_preview: string;
      redirect_uri: string;
      key_source?: 'env' | 'keychain' | 'none';
      secret_source?: 'env' | 'keychain' | 'none';
      redirect_source?: 'env' | 'keychain' | 'none';
    };
  };
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
  if (src === 'keychain') return 'macOS Keychain';
  return 'Default';
}

const ROADMAP_PHASES = [
  {
    phase: 'Phase 01',
    title: 'Hardened Upstox Hub & Security Fixes',
    detail:
      'Fixed Upstox logout endpoint, IST 03:30 AM daily reset boundary validation, macOS Keychain secret storage, and decoupled M2-friendly broker health probing.',
  },
  {
    phase: 'Phase 02',
    title: 'Zerodha Kite Connect Adapter',
    detail:
      'Implement Kite OAuth SHA-256 checksum login, KiteTicker WebSocket feed, and promote Zerodha from Planned to Active inside BrokerRegistry without UI rewrites.',
  },
  {
    phase: 'Phase 03',
    title: 'SQLite Instrument Index & Core Consolidation',
    detail:
      'Replace eager 100k-entry JSON parsing with an indexed SQLite instrument lookup under 5 MB RSS and unify core/live execution managers.',
  },
  {
    phase: 'Phase 04',
    title: 'Hot-Standby Feed Failover & Kill Switch',
    detail:
      'Surface WebSocket tick telemetry, automatic primary-to-fallback feed switching, preflight margin checks, and SEBI hash-chained kill switch controls.',
  },
  {
    phase: 'Phase 05',
    title: 'Screener, Backtester & Risk Console',
    detail:
      'Connect existing screener, vectorized backtester, and risk limits to the selected Data Connection as async FastAPI background tasks.',
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
  const [overview, setOverview] = useState<OverviewPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

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

  useEffect(() => {
    void fetchOverview(true);
  }, [fetchOverview]);

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
      if (st.status === 'listening' || st.status === 'exchanging') {
        setCaptureStatus({
          status: st.status,
          mode: st.capture_mode,
          redirectUri: st.redirect_uri,
          message: st.message || 'Waiting for Upstox redirect...',
        });
      } else if (st.status === 'connected') {
        setCaptureActive(false);
        setCaptureStatus({
          status: 'connected',
          mode: st.capture_mode,
          redirectUri: st.redirect_uri,
          message: st.message || 'Connected automatically and verified with Upstox.',
        });
        setModalFeedback({ type: 'ok', text: 'Connected and verified against Upstox profile API.' });
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
  }, [captureActive, fetchOverview, modalBrokerId]);

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
    const creds = fresh?.credentials?.upstox ?? overview?.credentials?.upstox;
    const hasAll = Boolean(creds?.has_api_key && creds?.has_api_secret);
    setCredsExpanded(!hasAll);
    setManualOpen(false);
    setRedirectUri(creds?.redirect_uri || `${window.location.origin}/auth/${brokerId}/callback`);
  };

  const handleSaveCredentials = async () => {
    if (!modalBrokerId) return;
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
        text: 'Credentials saved securely. Click Login with Upstox (Auto-Capture).',
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
    if (!callbackInput.trim()) {
      setModalFeedback({ type: 'err', text: 'Paste the redirected callback URL or authorization code first.' });
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
      if (!res.ok) throw new Error(data.detail || 'Authorization code exchange failed.');
      setCaptureActive(false);
      setCaptureStatus({ status: 'connected', message: 'Connected and verified with Upstox.' });
      setModalFeedback({ type: 'ok', text: 'Connected and verified against Upstox profile API.' });
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
  const upstoxCreds = overview?.credentials?.upstox;

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
              href="#roadmap"
              className="shrink-0 whitespace-nowrap text-sm font-medium text-slate-400 transition-colors hover:text-slate-100 hover:underline hover:underline-offset-8"
            >
              Roadmap
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

        {/* Section 3: Modular Expansion Roadmap */}
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
                Reconnect {modalBrokerId.charAt(0).toUpperCase() + modalBrokerId.slice(1)} Session
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
                    {upstoxCreds?.has_api_key
                      ? `${upstoxCreds.api_key_preview} (${formatCredentialSource(upstoxCreds.key_source)})`
                      : 'Missing'}
                  </span>
                </div>
                <div className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="font-semibold text-slate-400">API Secret</span>
                  <span className="font-mono text-slate-200">
                    {upstoxCreds?.has_api_secret
                      ? `Configured (${formatCredentialSource(upstoxCreds.secret_source)})`
                      : 'Missing'}
                  </span>
                </div>
                <div className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="font-semibold text-slate-400">Redirect URI</span>
                  <span className="font-mono break-all text-right text-slate-200">
                    {upstoxCreds?.redirect_uri
                      ? `${upstoxCreds.redirect_uri} (${formatCredentialSource(upstoxCreds.redirect_source)})`
                      : `${redirectUri} (Default)`}
                  </span>
                </div>
              </div>

              {credsExpanded && (
                <div className="space-y-3.5 rounded-lg border border-slate-800 bg-[#0b1120]/60 p-4">
                  <div>
                    <label htmlFor="modal-api-key" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      Upstox API Key
                    </label>
                    <input
                      id="modal-api-key"
                      type="text"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={
                        upstoxCreds?.has_api_key
                          ? `Detected (${upstoxCreds.api_key_preview}) — enter only to override`
                          : 'Enter your Upstox API Key'
                      }
                      autoComplete="off"
                      className="min-h-[42px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 py-2 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
                    />
                  </div>

                  <div>
                    <label htmlFor="modal-api-secret" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      Upstox API Secret
                    </label>
                    <input
                      id="modal-api-secret"
                      type="password"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      placeholder={
                        upstoxCreds?.has_api_secret
                          ? `Detected in ${formatCredentialSource(upstoxCreds.secret_source)} — enter only to override`
                          : 'Enter your Upstox API Secret'
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
                <span>Login with Upstox (Auto-Capture)</span>
              </button>

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
                          ? 'Code captured — verifying session...'
                          : captureStatus.status === 'timeout'
                            ? 'Auto-capture timed out'
                            : captureStatus.status === 'error'
                              ? 'Auto-capture failed'
                              : 'Listening for Upstox browser redirect...'}
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
                      Open Upstox login link (if popup was blocked)
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
                  Manual Fallback: Paste Redirect URL or Authorization Code
                </summary>
                <div className="mt-3 space-y-3">
                  <div>
                    <label htmlFor="modal-callback-input" className="mb-1.5 block text-[13px] font-semibold text-slate-200">
                      Redirected Callback URL or Code
                    </label>
                    <input
                      id="modal-callback-input"
                      type="text"
                      value={callbackInput}
                      onChange={(e) => setCallbackInput(e.target.value)}
                      placeholder="https://127.0.0.1:5000/?code=... or bare code"
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
                    {exchanging ? 'Exchanging Code...' : 'Exchange Code & Connect'}
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
