import { useCallback, useEffect, useRef, useState } from 'react';
import { Pause, Play, Square, LogOut, Trash2, CheckCircle2, XCircle, ShieldAlert, RefreshCw } from 'lucide-react';
import type { ActionableAlert, DeploymentRecord, DeploymentsPayload } from './types';

interface Props {
  onGoToStrategies: () => void;
}

const fmtInr = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function DeploymentsConsole({ onGoToStrategies }: Props) {
  const [data, setData] = useState<DeploymentsPayload | null>(null);
  const [tab, setTab] = useState<'all' | 'paper' | 'live' | 'alerts'>('all');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const fetchDeployments = useCallback(async () => {
    try {
      const res = await fetch('/api/streak/deployments');
      if (res.ok) {
        setData((await res.json()) as DeploymentsPayload);
      }
    } catch {
      // ignore transient network glitches
    }
  }, []);

  useEffect(() => {
    void fetchDeployments();
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/deployments`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && Array.isArray(msg.deployments)) {
          setData(msg as DeploymentsPayload);
        }
      } catch {
        // ignore
      }
    };
    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [fetchDeployments]);

  const handleControl = async (id: string, action: 'pause' | 'resume' | 'square_off' | 'stop' | 'delete') => {
    setBusyId(id);
    setFeedback(null);
    try {
      const res =
        action === 'delete'
          ? await fetch(`/api/streak/deployments/${id}`, { method: 'DELETE' })
          : await fetch(`/api/streak/deployments/${id}/action`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action }),
            });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || `Action '${action}' failed.`);
      setFeedback({ ok: true, text: `Deployment ${id}: ${action.replace('_', ' ')} completed.` });
      await fetchDeployments();
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Action failed.' });
    } finally {
      setBusyId(null);
    }
  };

  const handleAlertAction = async (alertId: string, action: 'execute' | 'dismiss') => {
    setBusyId(alertId);
    setFeedback(null);
    try {
      const res = await fetch(`/api/streak/deployments/alerts/${alertId}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Alert action failed.');
      setFeedback({
        ok: Boolean(body.ok !== false),
        text:
          body.status === 'EXECUTED'
            ? `Order executed (${body.broker_order_id || alertId}).`
            : body.status === 'BLOCKED'
            ? `Order blocked by safety gate: ${(body.reasons || []).join('; ')}`
            : `Alert ${alertId}: ${body.status}.`,
      });
      await fetchDeployments();
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Alert action failed.' });
    } finally {
      setBusyId(null);
    }
  };

  const deployments = data?.deployments || [];
  const filtered = deployments.filter((d) => {
    if (tab === 'paper') return d.exec_mode === 'paper';
    if (tab === 'live') return d.exec_mode !== 'paper';
    return true;
  });
  const pendingAlerts = data?.pending_alerts || [];
  const allAlerts = data?.alerts || [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-100">Deployed Strategies (Paper &amp; Live)</h1>
          <p className="text-sm text-slate-400">
            Real-time MTM tracking over shared Broker WebSocket, 1-click actionable order alerts, and Kill-Switch / Risk-Limit gated live execution.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void fetchDeployments()}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-slate-700 px-3 text-xs font-semibold text-slate-200 hover:bg-slate-800"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
          <button
            type="button"
            onClick={onGoToStrategies}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-sky-400 px-4 text-xs font-semibold text-sky-950 hover:bg-sky-300"
          >
            Deploy New Strategy
          </button>
        </div>
      </div>

      {data?.kill_switch?.engaged && (
        <div className="flex items-center gap-3 rounded-xl border border-red-800/80 bg-red-950/40 px-4 py-3 text-xs text-red-200">
          <ShieldAlert className="h-4 w-4 shrink-0 text-red-400" />
          <span>
            <strong>Emergency Kill Switch ENGAGED</strong> — All Live Auto-Execute and 1-Click Live Order dispatches are blocked until reset in Connections &amp; Risk.
          </span>
        </div>
      )}

      {/* KPI Summary */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Running / Total</p>
          <p className="mt-1 font-mono text-lg font-bold text-slate-100 tabular-nums">
            {data?.summary.running_count ?? 0} / {data?.summary.total_deployments ?? 0}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Paper Net P&amp;L</p>
          <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${(data?.summary.paper_net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {fmtInr(data?.summary.paper_net_pnl ?? 0)}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Live Net P&amp;L</p>
          <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${(data?.summary.live_net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {fmtInr(data?.summary.live_net_pnl ?? 0)}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Charges Accrued</p>
          <p className="mt-1 font-mono text-lg font-semibold text-amber-300 tabular-nums">{fmtInr(data?.summary.total_charges ?? 0)}</p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Pending 1-Click Alerts</p>
          <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${pendingAlerts.length > 0 ? 'text-sky-400' : 'text-slate-400'}`}>
            {pendingAlerts.length}
          </p>
        </div>
      </div>

      {/* Pending 1-Click Actionable Alerts Banner */}
      {pendingAlerts.length > 0 && (
        <div className="rounded-xl border border-sky-500/40 bg-[#111827] p-4 space-y-3">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-sky-300">
            Action Required — Pending 1-Click Live Order Alerts ({pendingAlerts.length})
          </h3>
          <div className="space-y-2">
            {pendingAlerts.map((a: ActionableAlert) => (
              <div key={a.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-[#0b1120] px-4 py-3">
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2 text-xs font-semibold">
                    <span className={a.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{a.side}</span>
                    <span className="font-mono text-slate-100">{a.qty} × {a.symbol}</span>
                    <span className="font-mono text-slate-300">@ ₹{a.signal_price}</span>
                    <span className="text-slate-500">· {a.product} · {a.deployment_name}</span>
                  </div>
                  <p className="text-[11px] text-slate-500">{a.reason} · {a.created_at_ist}</p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={busyId === a.id}
                    onClick={() => void handleAlertAction(a.id, 'execute')}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md bg-emerald-500/20 px-3 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-50"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Execute Order
                  </button>
                  <button
                    type="button"
                    disabled={busyId === a.id}
                    onClick={() => void handleAlertAction(a.id, 'dismiss')}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-700 px-3 text-xs font-semibold text-slate-400 hover:bg-slate-800 hover:text-slate-200 disabled:opacity-50"
                  >
                    <XCircle className="h-3.5 w-3.5" />
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {feedback && (
        <p className={`text-xs ${feedback.ok ? 'text-emerald-400' : 'text-red-400'}`}>{feedback.text}</p>
      )}

      {/* Filter Tabs */}
      <div className="flex items-center gap-2 border-b border-slate-800 pb-2 text-xs">
        {([
          ['all', `All Deployments (${deployments.length})`],
          ['paper', `Paper Trade (${data?.summary.paper_count ?? 0})`],
          ['live', `Live Trade (${data?.summary.live_count ?? 0})`],
          ['alerts', `Alert Log (${allAlerts.length})`],
        ] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={`rounded-md px-3 py-1.5 font-semibold ${
              tab === id ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'alerts' ? (
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4 overflow-x-auto">
          {allAlerts.length === 0 ? (
            <p className="py-6 text-center text-xs text-slate-500">No actionable alerts recorded yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                <tr>
                  <th className="py-2 text-left">Time</th>
                  <th className="py-2 text-left">Deployment</th>
                  <th className="py-2 text-left">Order</th>
                  <th className="py-2 text-right">Signal ₹</th>
                  <th className="py-2 text-left pl-3">Status</th>
                  <th className="py-2 text-left">Note</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                {allAlerts.map((a) => (
                  <tr key={a.id}>
                    <td className="py-2 text-slate-400">{a.created_at_ist.slice(11, 19)}</td>
                    <td className="py-2 font-sans text-slate-200">{a.deployment_name}</td>
                    <td className="py-2">
                      <span className={a.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{a.side}</span> {a.qty} {a.symbol}
                    </td>
                    <td className="py-2 text-right text-slate-200">{a.signal_price}</td>
                    <td className="py-2 pl-3 font-sans font-semibold text-slate-300">{a.status}</td>
                    <td className="py-2 font-sans text-slate-400">{a.note || a.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-10 text-center space-y-3">
          <p className="text-sm text-slate-300">No strategies deployed in this view yet.</p>
          <p className="text-xs text-slate-500">
            Run a backtest in <strong>Strategies &amp; Backtest</strong> and click <strong>Deploy Strategy</strong> to promote it into Paper Trade or Live Trade.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {filtered.map((d: DeploymentRecord) => (
            <div key={d.id} className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-bold text-slate-100">{d.name}</h3>
                    <span className="font-mono text-[11px] text-slate-400">
                      · {d.exec_mode.replace('_', ' ').toUpperCase()} · {d.status} · {d.strategy_mode} · {d.timeframe} · {d.charges_profile.toUpperCase()}
                    </span>
                  </div>
                  <p className="font-mono text-xs text-slate-500">
                    {d.symbols.join(', ')} · SL {d.stop_loss_pct}% · TP {d.take_profit_pct}%{d.trailing_sl_pct > 0 ? ` · TSL ${d.trailing_sl_pct}%` : ''}
                  </p>
                  {d.last_note && <p className="text-xs text-slate-400">{d.last_note}</p>}
                </div>

                <div className="flex flex-wrap items-center gap-1.5">
                  {d.status === 'RUNNING' ? (
                    <button type="button" disabled={busyId === d.id} onClick={() => void handleControl(d.id, 'pause')} className="inline-flex h-8 items-center gap-1 rounded border border-slate-700 px-2.5 text-xs text-slate-200 hover:bg-slate-800">
                      <Pause className="h-3 w-3" /> Pause
                    </button>
                  ) : d.status === 'PAUSED' ? (
                    <button type="button" disabled={busyId === d.id} onClick={() => void handleControl(d.id, 'resume')} className="inline-flex h-8 items-center gap-1 rounded border border-sky-500/40 bg-sky-500/10 px-2.5 text-xs text-sky-300 hover:bg-sky-500/20">
                      <Play className="h-3 w-3" /> Resume
                    </button>
                  ) : null}
                  {d.open_positions_count > 0 && (
                    <button type="button" disabled={busyId === d.id} onClick={() => void handleControl(d.id, 'square_off')} className="inline-flex h-8 items-center gap-1 rounded border border-amber-500/40 bg-amber-500/10 px-2.5 text-xs text-amber-300 hover:bg-amber-500/20">
                      <LogOut className="h-3 w-3" /> Square-Off
                    </button>
                  )}
                  {d.status !== 'STOPPED' && (
                    <button type="button" disabled={busyId === d.id} onClick={() => void handleControl(d.id, 'stop')} className="inline-flex h-8 items-center gap-1 rounded border border-red-500/40 bg-red-500/10 px-2.5 text-xs text-red-300 hover:bg-red-500/20">
                      <Square className="h-3 w-3" /> Stop
                    </button>
                  )}
                  <button type="button" disabled={busyId === d.id} onClick={() => void handleControl(d.id, 'delete')} className="inline-flex h-8 items-center rounded border border-slate-800 px-2 text-slate-500 hover:text-red-400" title="Remove deployment">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 border-t border-slate-800 pt-3 sm:grid-cols-6 font-mono text-xs tabular-nums">
                <div><span className="block font-sans text-[11px] text-slate-500">Capital</span><span className="text-slate-200">{fmtInr(d.initial_capital)}</span></div>
                <div><span className="block font-sans text-[11px] text-slate-500">Equity</span><span className="text-slate-100 font-semibold">{fmtInr(d.equity)}</span></div>
                <div><span className="block font-sans text-[11px] text-slate-500">Realized</span><span className={d.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtInr(d.realized_pnl)}</span></div>
                <div><span className="block font-sans text-[11px] text-slate-500">Unrealized MTM</span><span className={d.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtInr(d.unrealized_pnl)}</span></div>
                <div><span className="block font-sans text-[11px] text-slate-500">Charges</span><span className="text-amber-300">{fmtInr(d.total_charges)}</span></div>
                <div><span className="block font-sans text-[11px] text-slate-500">Net P&amp;L</span><span className={`font-bold ${d.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmtInr(d.net_pnl)} ({d.return_pct}%)</span></div>
              </div>

              {d.positions.length > 0 && (
                <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                  <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Open Positions ({d.positions.length})</p>
                  <div className="grid grid-cols-1 gap-1.5 font-mono text-xs tabular-nums sm:grid-cols-2">
                    {d.positions.map((p) => {
                      const mtm = (p.last_price - p.entry_price) * p.qty;
                      return (
                        <div key={p.symbol} className="flex items-center justify-between rounded bg-[#111827] px-3 py-1.5">
                          <span className="font-sans font-semibold text-slate-200">{p.symbol} × {p.qty}</span>
                          <span className="text-slate-400">Avg ₹{p.entry_price} · LTP ₹{p.last_price}</span>
                          <span className={mtm >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtInr(mtm)}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
