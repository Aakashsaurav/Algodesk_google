import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ChevronDown, RefreshCw } from 'lucide-react';
import { apiFetch } from '../../utils/authClient';

export interface OutboundProviderUsage {
  id: string;
  name: string;
  category: string;
  calls_1m: number;
  calls_1h: number;
  calls_1d: number;
  reset_1m_in_sec: number | null;
  reset_1h_in_sec: number | null;
  last_call_ts_ist: string | null;
  last_method: string | null;
  last_host: string | null;
  last_path: string | null;
}

export interface OutboundApiUsageSnapshot {
  ok: boolean;
  current_day_ist: string;
  server_ts_ist: string;
  windows: {
    minute_sec: number;
    hour_sec: number;
    day_timezone: string;
    day_reset_in_sec: number;
    next_1m_expiry_in_sec: number | null;
    next_1h_expiry_in_sec: number | null;
  };
  totals: {
    calls_1m: number;
    calls_1h: number;
    calls_1d: number;
    active_providers: number;
  };
  providers: OutboundProviderUsage[];
  hooks_installed: boolean;
}

function formatCountdown(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || !Number.isFinite(totalSeconds)) {
    return '—';
  }
  const clamped = Math.max(0, Math.floor(totalSeconds));
  const hrs = Math.floor(clamped / 3600);
  const mins = Math.floor((clamped % 3600) / 60);
  const secs = clamped % 60;
  if (hrs > 0) {
    return `${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function formatShortTime(tsIst: string | null | undefined): string {
  if (!tsIst) return '—';
  const match = tsIst.match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} IST` : tsIst;
}

export function useOutboundApiUsage(enabled: boolean, pollMs = 5000) {
  const [data, setData] = useState<OutboundApiUsageSnapshot | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string>('');
  const [tickOffsetSec, setTickOffsetSec] = useState<number>(0);

  const refresh = useCallback(
    async (showSpinner = false) => {
      if (!enabled) return;
      if (showSpinner) setLoading(true);
      try {
        const res = await apiFetch('/api/telemetry/api-usage');
        if (res.status === 401) {
          setError('');
          return;
        }
        const body = await res.json().catch(() => ({}));
        if (!res.ok || !body?.ok) {
          throw new Error(body?.detail || `HTTP ${res.status}`);
        }
        setData(body as OutboundApiUsageSnapshot);
        setTickOffsetSec(0);
        setError('');
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load API call usage.');
      } finally {
        if (showSpinner) setLoading(false);
      }
    },
    [enabled]
  );

  useEffect(() => {
    if (!enabled) return;
    void refresh(true);
    const pollTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void refresh(false);
      }
    }, pollMs);
    const secTimer = window.setInterval(() => {
      setTickOffsetSec((prev) => prev + 1);
    }, 1000);
    return () => {
      window.clearInterval(pollTimer);
      window.clearInterval(secTimer);
    };
  }, [enabled, pollMs, refresh]);

  const dayResetRemaining =
    data?.windows?.day_reset_in_sec !== undefined
      ? Math.max(0, data.windows.day_reset_in_sec - tickOffsetSec)
      : null;

  return {
    data,
    loading,
    error,
    dayResetRemaining,
    refresh,
  };
}

interface ApiUsageHeaderPopoverProps {
  data: OutboundApiUsageSnapshot | null;
  loading: boolean;
  error: string;
  dayResetRemaining: number | null;
  onRefresh: () => void;
  onOpenFullView?: () => void;
}

export function ApiUsageHeaderPopover({
  data,
  loading,
  error,
  dayResetRemaining,
  onRefresh,
  onOpenFullView,
}: ApiUsageHeaderPopoverProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const handleOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handleOutside);
    document.addEventListener('keydown', handleEsc);
    return () => {
      document.removeEventListener('mousedown', handleOutside);
      document.removeEventListener('keydown', handleEsc);
    };
  }, [open]);

  const totals = data?.totals ?? { calls_1m: 0, calls_1h: 0, calls_1d: 0, active_providers: 0 };
  const providers = data?.providers ?? [];

  return (
    <div ref={containerRef} className="relative shrink-0">
      <button
        type="button"
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (next) onRefresh();
        }}
        aria-expanded={open}
        aria-label="Outbound third-party API call counter"
        title="Outbound third-party API calls (Last 1m · Last 1h · Today IST)"
        className={`inline-flex h-8 shrink-0 items-center gap-2 whitespace-nowrap rounded-lg border px-2.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 ${
          open
            ? 'border-sky-500/70 bg-slate-900 text-slate-100'
            : 'border-slate-700/90 bg-[#111827] text-slate-300 hover:border-slate-500 hover:text-slate-100'
        }`}
      >
        <Activity className="h-3.5 w-3.5 shrink-0 text-sky-400" />
        <span className="font-medium text-slate-400">API</span>
        <span className="font-mono tabular-nums text-slate-100">
          <span className="text-slate-500">1m</span>{' '}
          <strong className={totals.calls_1m > 0 ? 'text-sky-300' : 'text-slate-200'}>{totals.calls_1m}</strong>
          <span className="mx-1.5 text-slate-700">·</span>
          <span className="text-slate-500">1h</span>{' '}
          <strong className="text-slate-200">{totals.calls_1h}</strong>
          <span className="mx-1.5 text-slate-700">·</span>
          <span className="text-slate-500">1d</span>{' '}
          <strong className="text-slate-200">{totals.calls_1d}</strong>
        </span>
        <ChevronDown className={`h-3 w-3 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-10 z-50 w-[min(92vw,540px)] rounded-xl border border-slate-700 bg-[#0f172a] shadow-2xl">
          <div className="flex items-center justify-between gap-3 border-b border-slate-800 px-4 py-3">
            <div>
              <div className="text-xs font-semibold text-slate-100">Outbound Third-Party API Calls</div>
              <div className="mt-0.5 font-mono text-[11px] text-slate-400 tabular-nums">
                1m &amp; 1h rolling windows · Day ({data?.current_day_ist || 'IST'}) resets in{' '}
                {formatCountdown(dayResetRemaining)}
              </div>
            </div>
            <button
              type="button"
              onClick={onRefresh}
              disabled={loading}
              aria-label="Refresh API call counts"
              className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-[11px] font-medium text-slate-300 hover:border-sky-400 hover:text-sky-300 disabled:opacity-50"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
              <span>Refresh</span>
            </button>
          </div>

          {error && (
            <div className="border-b border-red-900/60 bg-red-950/40 px-4 py-2 text-xs text-red-200">{error}</div>
          )}

          <div className="max-h-[340px] overflow-y-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead className="sticky top-0 border-b border-slate-800 bg-[#0b1120] text-[11px] font-medium text-slate-400">
                <tr>
                  <th className="px-4 py-2">Third Party</th>
                  <th className="px-2.5 py-2 text-right">1m</th>
                  <th className="px-2.5 py-2 text-right">1h</th>
                  <th className="px-2.5 py-2 text-right">Today</th>
                  <th className="px-4 py-2 text-right">Last Call</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
                {providers.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-6 text-center font-sans text-xs text-slate-400">
                      {loading ? 'Loading outbound API counters...' : 'No third-party API calls recorded yet.'}
                    </td>
                  </tr>
                ) : (
                  providers.map((p) => (
                    <tr key={p.id} className="hover:bg-slate-800/35">
                      <td className="px-4 py-2.5 font-sans">
                        <div className="font-medium text-slate-100">{p.name}</div>
                        <div className="text-[11px] text-slate-500">
                          {p.category}
                          {p.last_method && p.last_path ? ` · ${p.last_method} ${p.last_path}` : ''}
                        </div>
                      </td>
                      <td
                        className={`px-2.5 py-2.5 text-right font-semibold ${
                          p.calls_1m > 0 ? 'text-sky-300' : 'text-slate-500'
                        }`}
                      >
                        {p.calls_1m}
                      </td>
                      <td
                        className={`px-2.5 py-2.5 text-right font-semibold ${
                          p.calls_1h > 0 ? 'text-slate-200' : 'text-slate-500'
                        }`}
                      >
                        {p.calls_1h}
                      </td>
                      <td
                        className={`px-2.5 py-2.5 text-right font-semibold ${
                          p.calls_1d > 0 ? 'text-emerald-300' : 'text-slate-500'
                        }`}
                      >
                        {p.calls_1d}
                      </td>
                      <td className="px-4 py-2.5 text-right text-[11px] text-slate-400">
                        {formatShortTime(p.last_call_ts_ist)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
              {providers.length > 0 && (
                <tfoot className="border-t border-slate-700/80 bg-[#0b1120] font-mono text-xs font-semibold tabular-nums text-slate-100">
                  <tr>
                    <td className="px-4 py-2.5 font-sans text-slate-300">All Third Parties</td>
                    <td className="px-2.5 py-2.5 text-right text-sky-300">{totals.calls_1m}</td>
                    <td className="px-2.5 py-2.5 text-right text-slate-100">{totals.calls_1h}</td>
                    <td className="px-2.5 py-2.5 text-right text-emerald-300">{totals.calls_1d}</td>
                    <td className="px-4 py-2.5 text-right font-sans text-[11px] font-normal text-slate-500">
                      00:00 IST reset
                    </td>
                  </tr>
                </tfoot>
              )}
            </table>
          </div>

          {onOpenFullView && (
            <div className="flex items-center justify-between border-t border-slate-800 px-4 py-2.5 text-[11px] text-slate-400">
              <span>Auto-refreshes every 5s</span>
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  onOpenFullView();
                }}
                className="font-semibold text-sky-400 hover:text-sky-300"
              >
                Open Full Counter View
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface ApiUsageSectionProps {
  data: OutboundApiUsageSnapshot | null;
  loading: boolean;
  error: string;
  dayResetRemaining: number | null;
  onRefresh: () => void;
}

export function ApiUsageSection({
  data,
  loading,
  error,
  dayResetRemaining,
  onRefresh,
}: ApiUsageSectionProps) {
  const totals = data?.totals ?? { calls_1m: 0, calls_1h: 0, calls_1d: 0, active_providers: 0 };
  const providers = data?.providers ?? [];

  return (
    <section id="outbound-api-usage" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-100">
            Outbound Third-Party API Counter
          </h2>
          <p className="text-sm text-slate-400">
            Live request counts across brokers, market-data providers, exchanges, and Telegram. Minute and hour counters
            reset automatically after each rolling window lapses; the daily counter resets at 00:00 IST each new day.
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="inline-flex min-h-[36px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-700 bg-[#111827] px-3.5 text-xs font-semibold text-slate-200 hover:border-sky-400 hover:text-sky-300 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          <span>{loading ? 'Refreshing' : 'Refresh Counts'}</span>
        </button>
      </div>

      <div className="rounded-xl border border-slate-800 bg-[#111827]">
        {/* Headline KPI Strip */}
        <div className="grid grid-cols-2 gap-4 border-b border-slate-800 px-6 py-4 sm:grid-cols-4">
          <div>
            <div className="text-xs text-slate-400">Last 1 Minute</div>
            <div className="mt-1 font-mono text-xl font-semibold text-sky-300 tabular-nums">
              {totals.calls_1m.toLocaleString('en-IN')}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-slate-500 tabular-nums">
              {data?.windows?.next_1m_expiry_in_sec !== null && data?.windows?.next_1m_expiry_in_sec !== undefined
                ? `Next expiry in ${formatCountdown(data.windows.next_1m_expiry_in_sec)}`
                : 'Rolling 60s window'}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-400">Last 1 Hour</div>
            <div className="mt-1 font-mono text-xl font-semibold text-slate-100 tabular-nums">
              {totals.calls_1h.toLocaleString('en-IN')}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-slate-500 tabular-nums">
              {data?.windows?.next_1h_expiry_in_sec !== null && data?.windows?.next_1h_expiry_in_sec !== undefined
                ? `Next expiry in ${formatCountdown(data.windows.next_1h_expiry_in_sec)}`
                : 'Rolling 60m window'}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-400">Today ({data?.current_day_ist || 'IST'})</div>
            <div className="mt-1 font-mono text-xl font-semibold text-emerald-300 tabular-nums">
              {totals.calls_1d.toLocaleString('en-IN')}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-slate-500 tabular-nums">
              {totals.active_providers} active third-party service{totals.active_providers === 1 ? '' : 's'}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-400">Daily Reset (00:00 IST)</div>
            <div className="mt-1 font-mono text-xl font-semibold text-slate-200 tabular-nums">
              {formatCountdown(dayResetRemaining)}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-slate-500 tabular-nums">
              Resets on each new IST day
            </div>
          </div>
        </div>

        {error && (
          <div className="border-b border-red-900/60 bg-red-950/40 px-6 py-3 text-xs text-red-200">{error}</div>
        )}

        {/* Per-Third-Party Table */}
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead className="border-b border-slate-800 bg-[#0b1120] text-[11px] font-medium text-slate-400">
              <tr>
                <th className="px-6 py-3">Third Party</th>
                <th className="px-4 py-3">Category</th>
                <th className="px-4 py-3 text-right">Last 1 Min</th>
                <th className="px-4 py-3 text-right">Last 1 Hour</th>
                <th className="px-4 py-3 text-right">Today (IST)</th>
                <th className="px-6 py-3 text-right">Last Request</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
              {!data && loading ? (
                Array.from({ length: 5 }).map((_, idx) => (
                  <tr key={idx}>
                    <td colSpan={6} className="px-6 py-3.5">
                      <div className="h-4 w-full animate-pulse rounded bg-slate-800/60" />
                    </td>
                  </tr>
                ))
              ) : providers.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-6 py-8 text-center font-sans text-xs text-slate-400">
                    No outbound third-party API calls recorded yet. Counts will appear automatically as soon as any
                    broker, market-data, exchange, or Telegram request runs.
                  </td>
                </tr>
              ) : (
                providers.map((p) => (
                  <tr key={p.id} className="hover:bg-slate-800/30">
                    <td className="px-6 py-3 font-sans font-medium text-slate-100">
                      {p.name}
                      {p.last_host && (
                        <span className="ml-2 font-mono text-[11px] font-normal text-slate-500">{p.last_host}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-sans text-slate-400">{p.category}</td>
                    <td
                      className={`px-4 py-3 text-right font-semibold ${
                        p.calls_1m > 0 ? 'text-sky-300' : 'text-slate-500'
                      }`}
                    >
                      {p.calls_1m.toLocaleString('en-IN')}
                    </td>
                    <td
                      className={`px-4 py-3 text-right font-semibold ${
                        p.calls_1h > 0 ? 'text-slate-100' : 'text-slate-500'
                      }`}
                    >
                      {p.calls_1h.toLocaleString('en-IN')}
                    </td>
                    <td
                      className={`px-4 py-3 text-right font-semibold ${
                        p.calls_1d > 0 ? 'text-emerald-300' : 'text-slate-500'
                      }`}
                    >
                      {p.calls_1d.toLocaleString('en-IN')}
                    </td>
                    <td className="px-6 py-3 text-right text-[11px] text-slate-400">
                      {p.last_call_ts_ist ? (
                        <span>
                          {p.last_method || 'GET'} {p.last_path || '/'} · {formatShortTime(p.last_call_ts_ist)}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
