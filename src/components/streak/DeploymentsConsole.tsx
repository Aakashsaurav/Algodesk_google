import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch, createAuthenticatedWebSocket } from '../../utils/authClient';
import {
  Bell,
  CheckCircle2,
  Compass,
  ListChecks,
  LogOut,
  Pause,
  Play,
  RefreshCw,
  ShieldAlert,
  Square,
  Trash2,
  XCircle,
} from 'lucide-react';
import type { ActionableAlert, DeploymentRecord, DeploymentsPayload, OmsOrderRecord } from './types';

interface Props {
  onGoToStrategies: () => void;
  onGoToRisk?: () => void;
}

const fmtInr = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function DeploymentsConsole({ onGoToStrategies, onGoToRisk }: Props) {
  const [data, setData] = useState<DeploymentsPayload | null>(null);
  const [omsOrders, setOmsOrders] = useState<OmsOrderRecord[]>([]);
  const [tab, setTab] = useState<'live_alert' | 'paper' | 'live_auto' | 'all' | 'alerts' | 'oms'>('all');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reconciling, setReconciling] = useState(false);
  const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const fetchOmsOrders = useCallback(async () => {
    try {
      const res = await apiFetch('/api/streak/oms/orders?limit=100');
      if (res.ok) {
        const body = await res.json();
        setOmsOrders(Array.isArray(body.orders) ? (body.orders as OmsOrderRecord[]) : []);
      }
    } catch {
      // ignore
    }
  }, []);

  const fetchDeployments = useCallback(async () => {
    try {
      const res = await apiFetch('/api/streak/deployments');
      if (res.ok) {
        setData((await res.json()) as DeploymentsPayload);
      }
    } catch {
      // ignore transient network glitches
    }
    void fetchOmsOrders();
  }, [fetchOmsOrders]);

  const handleReconcileBroker = async () => {
    setReconciling(true);
    setFeedback(null);
    try {
      const res = await apiFetch('/api/streak/deployments/reconcile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Broker reconciliation failed.');
      const synced = Number(body.reconciled_count ?? body.reconciled_orders ?? body.updated_orders ?? 0);
      setFeedback({
        ok: true,
        text: `Broker orderbook synchronized (${synced} order update${synced === 1 ? '' : 's'} applied).`,
      });
      await fetchDeployments();
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Reconciliation failed.' });
    } finally {
      setReconciling(false);
    }
  };

  useEffect(() => {
    void fetchDeployments();
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = createAuthenticatedWebSocket(`${proto}//${window.location.host}/ws/deployments`);
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
          ? await apiFetch(`/api/streak/deployments/${encodeURIComponent(id)}`, { method: 'DELETE' })
          : await apiFetch(`/api/streak/deployments/${encodeURIComponent(id)}/action`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action }),
            });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Action '${action}' failed.`);
      setFeedback({ ok: true, text: `Deployment ${action.replace('_', ' ')} completed.` });
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
      const res = await apiFetch(`/api/streak/deployments/alerts/${encodeURIComponent(alertId)}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      const body = await res.json().catch(() => ({}));
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

  const handleResolveManualOrder = async (
    clientOrderId: string,
    status: 'FILLED' | 'REJECTED' | 'CANCELLED',
    defaultQty: number,
    defaultPrice: number,
  ) => {
    setBusyId(clientOrderId);
    setFeedback(null);
    try {
      const payload: Record<string, unknown> = {
        client_order_id: clientOrderId,
        status,
        note: `Resolved via operator console as ${status}`,
      };
      if (status === 'FILLED') {
        payload.filled_qty = defaultQty;
        if (defaultPrice > 0) payload.avg_price = defaultPrice;
      }
      const res = await apiFetch('/api/streak/oms/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Manual order resolution failed.');
      setFeedback({ ok: true, text: `OMS order ${clientOrderId} resolved as ${status}.` });
      await fetchDeployments();
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Manual order resolution failed.' });
    } finally {
      setBusyId(null);
    }
  };

  const deployments = data?.deployments || [];
  const filtered = deployments.filter((d) => {
    if (tab === 'paper') return d.exec_mode === 'paper';
    if (tab === 'live_alert') return d.exec_mode === 'live_alert';
    if (tab === 'live_auto') return d.exec_mode === 'live_auto';
    return true;
  });
  const pendingAlerts = data?.pending_alerts || [];
  const allAlerts = data?.alerts || [];

  const nbtCount = deployments.filter((d) => d.exec_mode === 'live_alert').length;
  const paperCount = deployments.filter((d) => d.exec_mode === 'paper').length;
  const autoCount = deployments.filter((d) => d.exec_mode === 'live_auto').length;

  return (
    <div className="space-y-5">
      {/* Header matching Streak Live Strategy.png */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-100">Live Strategies</h1>
          <p className="mt-0.5 text-xs text-slate-400">
            Real-time MTM tracking across Notification Based Trading (1-click), Paper Trading, and Gated Auto-Execute deployments.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={reconciling}
            onClick={() => void handleReconcileBroker()}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-emerald-700/70 bg-emerald-950/30 px-3 text-xs font-semibold text-emerald-300 hover:border-emerald-400 disabled:opacity-50"
          >
            <ListChecks className={`h-3.5 w-3.5 ${reconciling ? 'animate-pulse' : ''}`} />
            <span>{reconciling ? 'Syncing Broker...' : 'Sync Broker Now'}</span>
          </button>
          <button
            type="button"
            onClick={() => void fetchDeployments()}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            <span>Refresh</span>
          </button>
          <button
            type="button"
            onClick={onGoToStrategies}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
          >
            <Compass className="h-3.5 w-3.5" />
            <span>Go to Discover Strategies</span>
          </button>
        </div>
      </div>

      {data?.kill_switch?.engaged && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-800/80 bg-red-950/40 px-4 py-3 text-xs text-red-200">
          <div className="flex items-center gap-2.5">
            <ShieldAlert className="h-4 w-4 shrink-0 text-red-400" />
            <span>
              <strong>Emergency Kill Switch ENGAGED</strong> — All Live Auto-Execute and 1-Click Live Order dispatches are halted.
            </span>
          </div>
          {onGoToRisk && (
            <button
              type="button"
              onClick={onGoToRisk}
              className="rounded-md border border-red-700 px-3 py-1 font-semibold text-red-200 hover:bg-red-900/50"
            >
              Open Risk Limits
            </button>
          )}
        </div>
      )}

      {/* KPI Strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Running / Total</p>
          <p className="mt-1 font-mono text-lg font-bold text-slate-100 tabular-nums">
            {data?.summary.running_count ?? 0} / {data?.summary.total_deployments ?? 0}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Paper Net P&amp;L</p>
          <p
            className={`mt-1 font-mono text-lg font-bold tabular-nums ${
              (data?.summary.paper_net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
            }`}
          >
            {fmtInr(data?.summary.paper_net_pnl ?? 0)}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Live Net P&amp;L</p>
          <p
            className={`mt-1 font-mono text-lg font-bold tabular-nums ${
              (data?.summary.live_net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
            }`}
          >
            {fmtInr(data?.summary.live_net_pnl ?? 0)}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Charges Accrued</p>
          <p className="mt-1 font-mono text-lg font-semibold text-amber-300 tabular-nums">
            {fmtInr(data?.summary.total_charges ?? 0)}
          </p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Pending 1-Click Alerts</p>
          <p
            className={`mt-1 font-mono text-lg font-bold tabular-nums ${
              pendingAlerts.length > 0 ? 'text-sky-400' : 'text-slate-400'
            }`}
          >
            {pendingAlerts.length}
          </p>
        </div>
      </div>

      {/* Pending 1-Click Actionable Alerts Queue */}
      {pendingAlerts.length > 0 && (
        <div className="space-y-3 rounded-xl border border-sky-500/40 bg-[#111827] p-4">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-sky-300">
            <Bell className="h-3.5 w-3.5" />
            <span>Action Required — Pending 1-Click Order Alerts ({pendingAlerts.length})</span>
          </div>
          <div className="space-y-2">
            {pendingAlerts.map((a: ActionableAlert) => (
              <div
                key={a.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-[#0b1120] px-4 py-3"
              >
                <div className="space-y-0.5">
                  <div className="flex flex-wrap items-center gap-2 text-xs font-semibold">
                    <span className={a.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{a.side}</span>
                    <span className="font-mono text-slate-100">
                      {a.qty} × {a.symbol}
                    </span>
                    <span className="font-mono text-slate-300">@ ₹{a.signal_price}</span>
                    <span className="text-slate-500">
                      · {a.product} · {a.deployment_name}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-500">
                    {a.reason} · {a.created_at_ist}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={busyId === a.id}
                    onClick={() => void handleAlertAction(a.id, 'execute')}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md bg-emerald-500 px-3 text-xs font-semibold text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    <span>Execute Order</span>
                  </button>
                  <button
                    type="button"
                    disabled={busyId === a.id}
                    onClick={() => void handleAlertAction(a.id, 'dismiss')}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-700 px-3 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                  >
                    <XCircle className="h-3.5 w-3.5" />
                    <span>Dismiss</span>
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {feedback && (
        <div
          className={`rounded-lg border px-4 py-2 text-xs ${
            feedback.ok
              ? 'border-emerald-800/70 bg-emerald-950/30 text-emerald-300'
              : 'border-red-900/70 bg-red-950/30 text-red-300'
          }`}
        >
          {feedback.text}
        </div>
      )}

      {/* Streak Live Strategy Tabs */}
      <div className="flex flex-wrap items-center gap-1.5 border-b border-slate-800 pb-2 text-xs">
        {(
          [
            ['all', `All (${deployments.length})`],
            ['live_alert', `Notification Based Trading (${nbtCount})`],
            ['paper', `Paper Trading (${paperCount})`],
            ['live_auto', `Gated Auto-Execute (${autoCount})`],
            ['alerts', `Alert Log (${allAlerts.length})`],
            ['oms', `OMS Order Lifecycle (${omsOrders.length})`],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={`min-h-[32px] rounded-md px-3 font-semibold transition-colors ${
              tab === id ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'oms' ? (
        <div className="space-y-3">
          {omsOrders.some((o) => {
            const st = (o.status || '').toUpperCase();
            return st === 'MANUAL_INTERVENTION' || st === 'UNCERTAIN';
          }) && (
            <div className="flex items-center gap-2.5 rounded-xl border border-amber-700/70 bg-amber-950/35 px-4 py-3 text-xs text-amber-200">
              <ShieldAlert className="h-4 w-4 shrink-0 text-amber-400" />
              <span>
                <strong>Operator Verification Required</strong> — One or more orders are in{' '}
                <span className="font-mono font-semibold">UNCERTAIN</span> or{' '}
                <span className="font-mono font-semibold">MANUAL_INTERVENTION</span> state after an ambiguous broker
                response. Verify status directly in your broker terminal before resolving below.
              </span>
            </div>
          )}
          <div className="overflow-x-auto rounded-xl border border-slate-800 bg-[#111827] p-4">
            {omsOrders.length === 0 ? (
              <p className="py-8 text-center text-xs text-slate-500">
                No OMS orders recorded yet. Paper, quick, and live orders appear here with full broker fill reconciliation.
              </p>
            ) : (
              <table className="w-full text-xs">
                <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                  <tr>
                    <th className="py-2 text-left">Client / Broker ID</th>
                    <th className="py-2 text-left">Instrument</th>
                    <th className="py-2 text-left">Side / Product</th>
                    <th className="py-2 text-right">Filled / Qty</th>
                    <th className="py-2 text-right">Avg ₹</th>
                    <th className="py-2 pl-3 text-left">Lifecycle State</th>
                    <th className="py-2 text-left">Note / Operator Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                  {omsOrders.map((o) => {
                    const st = (o.status || '').toUpperCase();
                    const statusBadge =
                      st === 'FILLED'
                        ? { label: 'FILLED (Confirmed)', cls: 'border-emerald-700/70 bg-emerald-950/40 text-emerald-300' }
                        : st === 'PARTIALLY_FILLED'
                          ? { label: 'PARTIAL FILL', cls: 'border-amber-700/70 bg-amber-950/40 text-amber-300' }
                          : st === 'SUBMITTED' || st === 'PENDING' || st === 'OPEN'
                            ? {
                                label: st === 'SUBMITTED' ? 'SUBMITTED (Unfilled)' : `${st} (Working)`,
                                cls: 'border-sky-700/70 bg-sky-950/40 text-sky-300',
                              }
                            : st === 'UNCERTAIN' || st === 'MANUAL_INTERVENTION'
                              ? {
                                  label: st === 'MANUAL_INTERVENTION' ? 'MANUAL INTERVENTION' : 'UNCERTAIN (Verify)',
                                  cls: 'border-amber-600/80 bg-amber-950/60 text-amber-200',
                                }
                              : st === 'REJECTED' || st === 'CANCELLED' || st === 'EXPIRED'
                                ? { label: st, cls: 'border-red-800/70 bg-red-950/40 text-red-300' }
                                : { label: st || 'UNKNOWN', cls: 'border-slate-700 bg-slate-900 text-slate-300' };
                    const totalQty = Number(o.quantity ?? o.qty ?? 0);
                    const filledQty = Number(o.filled_quantity ?? o.filled_qty ?? 0);
                    const avgPx = Number(o.average_fill_price ?? o.avg_price ?? o.limit_price ?? 0);
                    const modeLabel = o.exec_mode || o.mode || '';
                    const needsManualResolve = st === 'MANUAL_INTERVENTION' || st === 'UNCERTAIN';
                    return (
                      <tr key={o.client_order_id} className="hover:bg-slate-800/30">
                        <td className="py-2 text-[11px] text-slate-300">
                          <div>{o.client_order_id}</div>
                          <div className="text-[10px] text-slate-500">
                            {o.broker_order_id || '—'} · {modeLabel}
                          </div>
                        </td>
                        <td className="py-2 text-slate-100">
                          {o.symbol} <span className="text-slate-500">({o.exchange})</span>
                        </td>
                        <td className="py-2">
                          <span className={o.side === 'BUY' || o.side === 'COVER' ? 'text-emerald-400' : 'text-red-400'}>
                            {o.side}
                          </span>{' '}
                          <span className="text-slate-400">{o.product}</span>
                        </td>
                        <td className="py-2 text-right text-slate-200">
                          {filledQty} / {totalQty}
                        </td>
                        <td className="py-2 text-right text-slate-200">
                          {avgPx > 0 ? `₹${avgPx.toFixed(2)}` : '—'}
                        </td>
                        <td className="py-2 pl-3 font-sans">
                          <span
                            className={`inline-block rounded border px-2 py-0.5 text-[11px] font-semibold ${statusBadge.cls}`}
                          >
                            {statusBadge.label}
                          </span>
                        </td>
                        <td className="py-2 font-sans text-slate-400">
                          <div>{o.rejection_reason || o.updated_at_ist.slice(11, 19)}</div>
                          {needsManualResolve && (
                            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                              <button
                                type="button"
                                disabled={busyId === o.client_order_id}
                                onClick={() =>
                                  void handleResolveManualOrder(o.client_order_id, 'FILLED', totalQty, avgPx)
                                }
                                className="rounded border border-emerald-700/70 bg-emerald-950/40 px-2 py-0.5 text-[10px] font-semibold text-emerald-300 hover:bg-emerald-900/50 disabled:opacity-50"
                              >
                                Confirm Filled
                              </button>
                              <button
                                type="button"
                                disabled={busyId === o.client_order_id}
                                onClick={() =>
                                  void handleResolveManualOrder(o.client_order_id, 'CANCELLED', 0, 0)
                                }
                                className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                              >
                                Mark Cancelled
                              </button>
                              <button
                                type="button"
                                disabled={busyId === o.client_order_id}
                                onClick={() =>
                                  void handleResolveManualOrder(o.client_order_id, 'REJECTED', 0, 0)
                                }
                                className="rounded border border-red-800/70 bg-red-950/40 px-2 py-0.5 text-[10px] font-semibold text-red-300 hover:bg-red-900/50 disabled:opacity-50"
                              >
                                Mark Rejected
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : tab === 'alerts' ? (
        <div className="overflow-x-auto rounded-xl border border-slate-800 bg-[#111827] p-4">
          {allAlerts.length === 0 ? (
            <p className="py-8 text-center text-xs text-slate-500">No actionable alerts recorded yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                <tr>
                  <th className="py-2 text-left">Time</th>
                  <th className="py-2 text-left">Deployment</th>
                  <th className="py-2 text-left">Order</th>
                  <th className="py-2 text-right">Signal ₹</th>
                  <th className="py-2 pl-3 text-left">Status</th>
                  <th className="py-2 text-left">Note</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                {allAlerts.map((a) => (
                  <tr key={a.id}>
                    <td className="py-2 text-slate-400">{a.created_at_ist.slice(11, 19)}</td>
                    <td className="py-2 font-sans text-slate-200">{a.deployment_name}</td>
                    <td className="py-2">
                      <span className={a.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}>{a.side}</span> {a.qty}{' '}
                      {a.symbol}
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
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead className="border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-3">Strategy / Instrument</th>
                  <th className="px-3 py-3 text-right">Avg. Price</th>
                  <th className="px-3 py-3 text-right">LTP</th>
                  <th className="px-3 py-3 text-right">TP</th>
                  <th className="px-3 py-3 text-right">SL</th>
                  <th className="px-3 py-3 text-right">P&amp;L</th>
                  <th className="px-3 py-3 text-right">Net Change</th>
                  <th className="px-3 py-3">Status / Mode</th>
                  <th className="px-4 py-3 text-right">Controls / Log</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {filtered.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="px-6 py-16 text-center">
                      <div className="mx-auto max-w-sm space-y-2.5">
                        <p className="text-sm font-semibold text-slate-200">No Strategies Deployed</p>
                        <p className="text-xs text-slate-400">
                          Backtest a strategy in Discover Strategies or Create Strategy, then click Notification Based Trading to take it live.
                        </p>
                        <div className="pt-2">
                          <button
                            type="button"
                            onClick={onGoToStrategies}
                            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
                          >
                            Go to Discover Strategies
                          </button>
                        </div>
                      </div>
                    </td>
                  </tr>
                ) : (
                  filtered.map((d: DeploymentRecord) => {
                    const primaryPos = d.positions?.[0];
                    const isLogOpen = expandedLogId === d.id;
                    return (
                      <tr key={d.id} className="align-top hover:bg-slate-800/35">
                        <td className="px-4 py-3">
                          <div className="font-semibold text-slate-100">{d.name}</div>
                          <div className="mt-0.5 font-mono text-[11px] text-slate-400">
                            {d.symbols.slice(0, 4).join(', ')}
                            {d.symbols.length > 4 ? ` +${d.symbols.length - 4}` : ''} · {d.timeframe} ·{' '}
                            {d.strategy_mode}
                          </div>
                          {isLogOpen && (
                            <div className="mt-2.5 space-y-1 rounded-lg border border-slate-800 bg-[#0b1120] p-2.5 font-mono text-[11px] text-slate-300">
                              <div>
                                Capital: {fmtInr(d.initial_capital)} · Equity: {fmtInr(d.equity)} · Charges:{' '}
                                {fmtInr(d.total_charges)}
                              </div>
                              {d.last_note && <div className="text-slate-400">Note: {d.last_note}</div>}
                              {d.positions.length > 0 && (
                                <div className="pt-1">
                                  {d.positions.map((p) => (
                                    <div key={p.symbol} className="text-sky-300">
                                      OPEN {p.symbol} × {p.qty} @ ₹{p.entry_price} (LTP ₹{p.last_price})
                                    </div>
                                  ))}
                                </div>
                              )}
                              {d.trades.slice(-3).map((t) => (
                                <div key={t.id} className="text-slate-400">
                                  {t.executed_at_ist.slice(11, 19)} {t.side} {t.symbol} × {t.qty} · {t.reason} ·{' '}
                                  {fmtInr(t.net_pnl)}
                                </div>
                              ))}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-3 text-right font-mono text-slate-300 tabular-nums">
                          {primaryPos ? `₹${primaryPos.entry_price.toFixed(2)}` : '—'}
                        </td>
                        <td className="px-3 py-3 text-right font-mono text-slate-100 tabular-nums">
                          {primaryPos ? `₹${primaryPos.last_price.toFixed(2)}` : '—'}
                        </td>
                        <td className="px-3 py-3 text-right font-mono text-emerald-400 tabular-nums">
                          {d.take_profit_pct}%
                        </td>
                        <td className="px-3 py-3 text-right font-mono text-red-400 tabular-nums">
                          {d.stop_loss_pct}%
                        </td>
                        <td
                          className={`px-3 py-3 text-right font-mono font-semibold tabular-nums ${
                            d.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                          }`}
                        >
                          {d.net_pnl >= 0 ? '+' : ''}
                          {fmtInr(d.net_pnl)}
                        </td>
                        <td
                          className={`px-3 py-3 text-right font-mono font-semibold tabular-nums ${
                            d.return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'
                          }`}
                        >
                          {d.return_pct >= 0 ? '+' : ''}
                          {d.return_pct.toFixed(2)}%
                        </td>
                        <td className="px-3 py-3 font-mono text-[11px]">
                          <div
                            className={
                              d.status === 'RUNNING'
                                ? 'font-semibold text-emerald-400'
                                : d.status === 'PAUSED'
                                  ? 'font-semibold text-amber-400'
                                  : 'text-slate-400'
                            }
                          >
                            {d.status}
                          </div>
                          <div className="text-[10px] text-slate-500">
                            {d.exec_mode === 'live_alert'
                              ? '1-Click Alerts'
                              : d.exec_mode === 'live_auto'
                                ? 'Gated Auto'
                                : 'Paper'}{' '}
                            · {d.evaluation_mode === 'intrabar' ? 'Intrabar' : 'Candle Close'}
                          </div>
                          <div className="text-[10px] text-slate-600">
                            {d.exchange || 'NSE'} · {d.product || 'MIS'}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="inline-flex items-center gap-1">
                            {d.status === 'RUNNING' ? (
                              <button
                                type="button"
                                disabled={busyId === d.id}
                                onClick={() => void handleControl(d.id, 'pause')}
                                title="Pause"
                                className="rounded border border-slate-700 p-1.5 text-slate-300 hover:border-amber-400"
                              >
                                <Pause className="h-3.5 w-3.5" />
                              </button>
                            ) : d.status === 'PAUSED' ? (
                              <button
                                type="button"
                                disabled={busyId === d.id}
                                onClick={() => void handleControl(d.id, 'resume')}
                                title="Resume"
                                className="rounded border border-sky-700/70 p-1.5 text-sky-300 hover:border-sky-400"
                              >
                                <Play className="h-3.5 w-3.5" />
                              </button>
                            ) : null}
                            {d.open_positions_count > 0 && (
                              <button
                                type="button"
                                disabled={busyId === d.id}
                                onClick={() => void handleControl(d.id, 'square_off')}
                                title="Square-off positions"
                                className="rounded border border-amber-700/70 p-1.5 text-amber-300 hover:border-amber-400"
                              >
                                <LogOut className="h-3.5 w-3.5" />
                              </button>
                            )}
                            {d.status !== 'STOPPED' && (
                              <button
                                type="button"
                                disabled={busyId === d.id}
                                onClick={() => void handleControl(d.id, 'stop')}
                                title="Stop deployment"
                                className="rounded border border-red-800/70 p-1.5 text-red-400 hover:border-red-500"
                              >
                                <Square className="h-3.5 w-3.5" />
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => setExpandedLogId(isLogOpen ? null : d.id)}
                              className="rounded border border-slate-700 px-2 py-1 text-[11px] font-medium text-sky-300 hover:border-sky-400"
                            >
                              {isLogOpen ? 'Hide' : 'Log'}
                            </button>
                            <button
                              type="button"
                              disabled={busyId === d.id}
                              onClick={() => void handleControl(d.id, 'delete')}
                              title="Remove deployment"
                              className="rounded border border-slate-800 p-1.5 text-slate-500 hover:text-red-400"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
