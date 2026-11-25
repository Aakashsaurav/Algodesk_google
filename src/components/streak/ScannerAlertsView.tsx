import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import {
  BarChart2,
  Bell,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react';
import type { QuickOrderTarget } from './QuickOrderModal';
import type {
  ScannerAlertEvent,
  ScannerAlertSubscription,
  ScannerDraftPreset,
} from './types';

interface ScannerAlertsViewProps {
  onOpenCreateScanner: (draft?: ScannerDraftPreset) => void;
  onQuickOrder: (target: QuickOrderTarget) => void;
  onOpenChart: (symbol: string) => void;
}

export function ScannerAlertsView({
  onOpenCreateScanner,
  onQuickOrder,
  onOpenChart,
}: ScannerAlertsViewProps) {
  const [subscriptions, setSubscriptions] = useState<ScannerAlertSubscription[]>([]);
  const [history, setHistory] = useState<ScannerAlertEvent[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<'ALL' | 'ACTIVE' | 'PAUSED'>('ALL');
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState('');

  const loadAlerts = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await apiFetch('/api/streak/scanners/alerts');
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not load scanner alerts.');
      setSubscriptions(Array.isArray(body.subscriptions) ? body.subscriptions : []);
      const evList = Array.isArray(body.history)
        ? body.history
        : Array.isArray(body.events)
          ? body.events
          : [];
      setHistory(evList);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Load failed.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAlerts();
  }, [loadAlerts]);

  const filteredSubs = useMemo(() => {
    const q = search.trim().toLowerCase();
    return subscriptions.filter((sub) => {
      if (statusFilter !== 'ALL' && sub.status !== statusFilter) return false;
      if (q && !sub.name.toLowerCase().includes(q) && !sub.universe.toLowerCase().includes(q)) {
        return false;
      }
      return true;
    });
  }, [subscriptions, search, statusFilter]);

  const selectedSub = useMemo(() => {
    return (
      subscriptions.find((s) => s.id === selectedId) ||
      filteredSubs[0] ||
      null
    );
  }, [subscriptions, filteredSubs, selectedId]);

  const visibleHistory = useMemo(() => {
    if (!selectedSub) return history;
    return history.filter((h) => h.subscription_id === selectedSub.id);
  }, [history, selectedSub]);

  const runAction = async (subId: string, action: 'pause' | 'resume' | 'scan_now' | 'clear_history') => {
    setBusyId(subId);
    setError('');
    try {
      const res = await apiFetch(`/api/streak/scanners/alerts/${encodeURIComponent(subId)}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Action failed.');
      await loadAlerts();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async (subId: string) => {
    setBusyId(subId);
    try {
      await apiFetch(`/api/streak/scanners/alerts/${encodeURIComponent(subId)}`, { method: 'DELETE' });
      if (selectedId === subId) setSelectedId('');
      await loadAlerts();
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-100">Scanner Alerts</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            Track live symbol entries and exits across your visual and Python (.py) scanner subscriptions.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void loadAlerts()}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
          <button
            type="button"
            onClick={() => onOpenCreateScanner({ mode: 'basic', name: 'Untitled Scanner' })}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
          >
            <Plus className="h-4 w-4" />
            <span>Create Scanner</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-4 py-2.5 text-xs text-red-200">
          {error}
        </div>
      )}

      {/* Streak Scanner_Alert_51373f5a.png Split-Pane */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Left List Pane (4 cols) */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827] lg:col-span-4">
          <div className="space-y-2.5 border-b border-slate-800 p-3.5">
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search for Scanners"
                  className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] pl-8 pr-2.5 text-xs text-slate-100 placeholder-slate-500 focus:border-sky-400 focus:outline-none"
                />
              </div>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value as 'ALL' | 'ACTIVE' | 'PAUSED')}
                aria-label="Filter alert status"
                className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-200"
              >
                <option value="ALL">All</option>
                <option value="ACTIVE">Active</option>
                <option value="PAUSED">Paused</option>
              </select>
            </div>
          </div>

          <div className="min-h-[420px] flex-1 divide-y divide-slate-800/70 overflow-y-auto">
            {filteredSubs.length === 0 ? (
              <div className="flex h-80 flex-col items-center justify-center px-6 text-center">
                <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl border border-slate-800 bg-[#0b1120] text-slate-500">
                  <Bell className="h-5 w-5" />
                </div>
                <p className="text-sm font-semibold text-slate-200">No Scanner Live!</p>
                <p className="mt-1 text-xs text-slate-400">
                  Subscribe to alerts from Discover Scanners or Create Scanner to monitor live entries.
                </p>
                <button
                  type="button"
                  onClick={() => onOpenCreateScanner({ mode: 'basic', name: 'Untitled Scanner' })}
                  className="mt-4 rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-slate-950 hover:bg-sky-400"
                >
                  Create Scanner
                </button>
              </div>
            ) : (
              filteredSubs.map((sub) => {
                const isSel = selectedSub?.id === sub.id;
                return (
                  <div
                    key={sub.id}
                    onClick={() => setSelectedId(sub.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') setSelectedId(sub.id);
                    }}
                    className={`cursor-pointer px-4 py-3.5 transition-colors ${
                      isSel ? 'bg-sky-950/40' : 'hover:bg-slate-800/40'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-slate-100">{sub.name}</div>
                        <div className="mt-0.5 font-mono text-[11px] text-slate-400">
                          {sub.universe} · {sub.timeframe} · every {Math.round(sub.frequency_sec / 60)}m
                        </div>
                      </div>
                      <span
                        className={`shrink-0 font-mono text-[11px] font-semibold ${
                          sub.status === 'ACTIVE' ? 'text-emerald-400' : 'text-amber-400'
                        }`}
                      >
                        {sub.status}
                      </span>
                    </div>

                    <div className="mt-2.5 flex items-center justify-between text-[11px] text-slate-400">
                      <span className="font-mono">
                        Matched: <strong className="text-slate-200">{sub.last_matched_count}</strong> · Alerts:{' '}
                        <strong className="text-sky-300">{sub.alerts_triggered}</strong>
                      </span>
                      <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                        <button
                          type="button"
                          onClick={() => void runAction(sub.id, 'scan_now')}
                          disabled={busyId === sub.id}
                          title="Check now"
                          className="rounded border border-slate-700 p-1 text-sky-300 hover:border-sky-400"
                        >
                          <RefreshCw className={`h-3 w-3 ${busyId === sub.id ? 'animate-spin' : ''}`} />
                        </button>
                        <button
                          type="button"
                          onClick={() => void runAction(sub.id, sub.status === 'ACTIVE' ? 'pause' : 'resume')}
                          disabled={busyId === sub.id}
                          title={sub.status === 'ACTIVE' ? 'Pause alert' : 'Resume alert'}
                          className="rounded border border-slate-700 p-1 text-slate-300 hover:border-amber-400"
                        >
                          {sub.status === 'ACTIVE' ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleDelete(sub.id)}
                          disabled={busyId === sub.id}
                          title="Delete alert"
                          className="rounded border border-slate-700 p-1 text-slate-400 hover:border-red-700 hover:text-red-400"
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Right Alert History Pane (8 cols) */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827] lg:col-span-8">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-5 py-3.5">
            <div>
              <h3 className="text-sm font-semibold text-slate-100">
                {selectedSub ? `Alert History — ${selectedSub.name}` : 'Alert History'}
              </h3>
              {selectedSub?.last_checked_ist && (
                <p className="mt-0.5 font-mono text-[11px] text-slate-500">
                  Last checked: {selectedSub.last_checked_ist.slice(0, 19).replace('T', ' ')}
                </p>
              )}
            </div>
            {selectedSub && (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void runAction(selectedSub.id, 'scan_now')}
                  disabled={busyId === selectedSub.id}
                  className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg bg-sky-500 px-3 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${busyId === selectedSub.id ? 'animate-spin' : ''}`} />
                  <span>Check Now</span>
                </button>
                <button
                  type="button"
                  onClick={() => void runAction(selectedSub.id, 'clear_history')}
                  className="min-h-[32px] rounded-lg border border-slate-700 px-3 text-xs font-medium text-slate-300 hover:border-slate-500"
                >
                  Clear Log
                </button>
              </div>
            )}
          </div>

          <div className="min-h-[420px] flex-1 overflow-auto">
            {visibleHistory.length === 0 ? (
              <div className="flex h-80 flex-col items-center justify-center px-6 text-center">
                <p className="text-sm font-semibold text-slate-300">No Alert Events Yet</p>
                <p className="mt-1 max-w-sm text-xs text-slate-500">
                  When symbols enter or exit the scanner criteria, timestamped alerts appear here with one-click Buy, Sell, and Chart actions.
                </p>
              </div>
            ) : (
              <table className="w-full border-collapse text-left">
                <thead className="sticky top-0 border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-4 py-2.5">Time (IST)</th>
                    <th className="px-3 py-2.5">Symbol</th>
                    <th className="px-3 py-2.5">Event</th>
                    <th className="px-3 py-2.5 text-right">LTP</th>
                    <th className="px-3 py-2.5 text-right">Change %</th>
                    <th className="px-4 py-2.5 text-right">Buy / Sell</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70 text-xs">
                  {visibleHistory.map((ev) => {
                    const isEntered = ev.transition === 'ENTERED';
                    const isUp = ev.change_pct >= 0;
                    return (
                      <tr key={ev.id} className="hover:bg-slate-800/40">
                        <td className="px-4 py-2.5 font-mono text-[11px] text-slate-400">
                          {ev.ts_ist.slice(0, 19).replace('T', ' ')}
                        </td>
                        <td className="px-3 py-2.5 font-mono font-semibold text-slate-100">
                          {ev.symbol}
                          <span className="ml-1.5 text-[10px] font-normal text-slate-500">
                            {ev.exchange}
                          </span>
                        </td>
                        <td className="px-3 py-2.5">
                          <span
                            className={`font-mono text-[11px] font-bold ${
                              isEntered ? 'text-emerald-400' : 'text-amber-400'
                            }`}
                          >
                            {ev.transition}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-right font-mono text-slate-200 tabular-nums">
                          {ev.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </td>
                        <td
                          className={`px-3 py-2.5 text-right font-mono font-semibold tabular-nums ${
                            isUp ? 'text-emerald-400' : 'text-red-400'
                          }`}
                        >
                          {isUp ? '+' : ''}
                          {ev.change_pct.toFixed(2)}%
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <div className="inline-flex items-center gap-1.5">
                            <button
                              type="button"
                              onClick={() =>
                                onQuickOrder({
                                  symbol: ev.symbol,
                                  exchange: ev.exchange || 'NSE',
                                  side: 'BUY',
                                  ltp: ev.ltp,
                                })
                              }
                              className="flex h-7 w-7 items-center justify-center rounded border border-sky-700/80 bg-sky-950/40 font-mono text-[11px] font-bold text-sky-300 hover:bg-sky-500 hover:text-slate-950"
                            >
                              B
                            </button>
                            <button
                              type="button"
                              onClick={() =>
                                onQuickOrder({
                                  symbol: ev.symbol,
                                  exchange: ev.exchange || 'NSE',
                                  side: 'SELL',
                                  ltp: ev.ltp,
                                })
                              }
                              className="flex h-7 w-7 items-center justify-center rounded border border-red-800/80 bg-red-950/40 font-mono text-[11px] font-bold text-red-300 hover:bg-red-500 hover:text-white"
                            >
                              S
                            </button>
                            <button
                              type="button"
                              onClick={() => onOpenChart(ev.symbol)}
                              className="flex h-7 w-7 items-center justify-center rounded border border-slate-700 bg-[#0b1120] text-slate-300 hover:border-sky-400 hover:text-sky-300"
                            >
                              <BarChart2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
