import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  OctagonAlert,
  RefreshCw,
  Save,
  ShieldCheck,
  ShieldOff,
} from 'lucide-react';

interface RiskLimitsState {
  capital: number;
  max_order_value: number;
  max_total_exposure: number;
  max_daily_loss: number;
  max_open_positions: number;
  max_symbol_exposure_pct: number;
  max_orders_per_minute: number;
}

interface RiskApiPayload {
  ok?: boolean;
  limits?: Partial<RiskLimitsState>;
  policy?: Partial<RiskLimitsState>;
  metrics?: {
    total_exposure?: number;
    open_positions?: number;
    realized_pnl?: number;
    daily_loss?: number;
    exposure_pct?: number;
  };
  breaches?: string[];
  kill_switch?: { engaged: boolean; reason?: string; updated_at_ist?: string };
}

interface PreflightCheck {
  gate?: string;
  name?: string;
  passed?: boolean;
  ok?: boolean;
  detail?: string;
  message?: string;
}

const DEFAULT_LIMITS: RiskLimitsState = {
  capital: 500000,
  max_order_value: 100000,
  max_total_exposure: 400000,
  max_daily_loss: 15000,
  max_open_positions: 8,
  max_symbol_exposure_pct: 25,
  max_orders_per_minute: 20,
};

export function RiskLimitsView() {
  const [limits, setLimits] = useState<RiskLimitsState>(DEFAULT_LIMITS);
  const [metrics, setMetrics] = useState<RiskApiPayload['metrics']>({});
  const [breaches, setBreaches] = useState<string[]>([]);
  const [killSwitch, setKillSwitch] = useState<{ engaged: boolean; reason?: string }>({ engaged: false });
  const [killReason, setKillReason] = useState('Manual operator halt');

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [killBusy, setKillBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // 6-Gate Preflight Dry-Run state
  const [pfSymbol, setPfSymbol] = useState('RELIANCE');
  const [pfSide, setPfSide] = useState<'BUY' | 'SELL'>('BUY');
  const [pfQty, setPfQty] = useState('10');
  const [pfPrice, setPfPrice] = useState('2500');
  const [pfBusy, setPfBusy] = useState(false);
  const [pfResult, setPfResult] = useState<{ allowed?: boolean; checks?: PreflightCheck[]; summary?: string } | null>(
    null
  );

  const loadRisk = useCallback(async () => {
    setLoading(true);
    try {
      const [riskRes, ksRes] = await Promise.all([
        fetch('/api/analytics/risk'),
        fetch('/api/live/status'),
      ]);
      const riskBody = (await riskRes.json().catch(() => ({}))) as RiskApiPayload;
      const ksBody = await ksRes.json().catch(() => ({}));
      const src = riskBody.limits || riskBody.policy || {};
      setLimits((prev) => ({
        capital: Number(src.capital ?? prev.capital),
        max_order_value: Number(src.max_order_value ?? prev.max_order_value),
        max_total_exposure: Number(src.max_total_exposure ?? prev.max_total_exposure),
        max_daily_loss: Number(src.max_daily_loss ?? prev.max_daily_loss),
        max_open_positions: Number(src.max_open_positions ?? prev.max_open_positions),
        max_symbol_exposure_pct: Number(src.max_symbol_exposure_pct ?? prev.max_symbol_exposure_pct),
        max_orders_per_minute: Number(src.max_orders_per_minute ?? prev.max_orders_per_minute),
      }));
      setMetrics(riskBody.metrics || {});
      setBreaches(Array.isArray(riskBody.breaches) ? riskBody.breaches : []);
      const ks = ksBody.kill_switch || (typeof ksBody.engaged === 'boolean' ? ksBody : null);
      if (ks) {
        setKillSwitch({ engaged: Boolean(ks.engaged), reason: ks.reason });
      }
    } catch {
      // keep defaults
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRisk();
  }, [loadRisk]);

  const handleSaveLimits = async () => {
    setSaving(true);
    setFeedback(null);
    try {
      const res = await fetch('/api/analytics/risk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(limits),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not update risk limits.');
      setFeedback({ type: 'ok', text: 'Risk policy saved and enforced across quick orders and live deployments.' });
      await loadRisk();
    } catch (err) {
      setFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Save failed.' });
    } finally {
      setSaving(false);
    }
  };

  const handleToggleKillSwitch = async (engage: boolean) => {
    setKillBusy(true);
    setFeedback(null);
    try {
      const res = await fetch('/api/live/kill-switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engaged: engage, reason: engage ? killReason : 'Operator resumed trading' }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Kill switch toggle failed.');
      setKillSwitch({ engaged: engage, reason: engage ? killReason : undefined });
      setFeedback({
        type: 'ok',
        text: engage ? 'Emergency Kill Switch ENGAGED — all live order routing halted.' : 'Kill Switch released.',
      });
    } catch (err) {
      setFeedback({ type: 'err', text: err instanceof Error ? err.message : 'Toggle failed.' });
    } finally {
      setKillBusy(false);
    }
  };

  const handleRunPreflight = async () => {
    setPfBusy(true);
    setPfResult(null);
    try {
      const res = await fetch('/api/live/preflight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: pfSymbol.trim().toUpperCase() || 'RELIANCE',
          exchange: 'NSE',
          side: pfSide,
          quantity: Math.max(1, Number.parseInt(pfQty, 10) || 1),
          price: Math.max(0, Number(pfPrice) || 0),
          product: 'MIS',
        }),
      });
      const body = await res.json().catch(() => ({}));
      setPfResult({
        allowed: Boolean(body.allowed ?? body.passed ?? res.ok),
        checks: Array.isArray(body.checks) ? body.checks : Array.isArray(body.gates) ? body.gates : [],
        summary: body.message || body.detail || (body.allowed ? 'All preflight safety gates passed.' : 'Blocked by preflight gate.'),
      });
    } catch (err) {
      setPfResult({
        allowed: false,
        checks: [],
        summary: err instanceof Error ? err.message : 'Preflight check failed.',
      });
    } finally {
      setPfBusy(false);
    }
  };

  const exposureUsed = Number(metrics?.total_exposure ?? 0);
  const exposurePct = limits.max_total_exposure > 0 ? Math.min(100, (exposureUsed / limits.max_total_exposure) * 100) : 0;
  const dailyLoss = Math.abs(Math.min(0, Number(metrics?.realized_pnl ?? 0)));
  const lossPct = limits.max_daily_loss > 0 ? Math.min(100, (dailyLoss / limits.max_daily_loss) * 100) : 0;

  return (
    <div className="space-y-5">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-100">Risk Limits &amp; Execution Guardrails</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            Centralized capital caps, daily drawdown limits, emergency Kill Switch, and 6-gate order preflight verification.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadRisk()}
          className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3.5 text-xs font-semibold text-slate-200 hover:border-sky-400"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          <span>Refresh Policy</span>
        </button>
      </div>

      {/* Active Breaches Banner */}
      {(breaches.length > 0 || killSwitch.engaged) && (
        <div className="rounded-xl border border-red-800/80 bg-red-950/40 p-4 text-xs text-red-200">
          <div className="flex items-center gap-2 font-semibold text-red-300">
            <AlertTriangle className="h-4 w-4" />
            <span>{killSwitch.engaged ? 'Emergency Kill Switch Engaged' : 'Active Risk Breaches Detected'}</span>
          </div>
          {killSwitch.engaged && killSwitch.reason && <p className="mt-1 text-red-200/90">{killSwitch.reason}</p>}
          {breaches.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5">
              {breaches.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* KPI & Utilization Strip */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <div className="text-xs font-medium text-slate-400">Account Risk Capital</div>
          <div className="mt-1.5 font-mono text-xl font-bold text-slate-100 tabular-nums">
            ₹{limits.capital.toLocaleString('en-IN')}
          </div>
          <div className="mt-1 font-mono text-[11px] text-slate-500">
            Max Single Order: ₹{limits.max_order_value.toLocaleString('en-IN')}
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <div className="flex items-center justify-between text-xs font-medium text-slate-400">
            <span>Total Exposure</span>
            <span className="font-mono text-slate-300">{exposurePct.toFixed(1)}%</span>
          </div>
          <div className="mt-1.5 font-mono text-xl font-bold text-slate-100 tabular-nums">
            ₹{exposureUsed.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
          </div>
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
            <div className="h-full bg-sky-500" style={{ width: `${exposurePct}%` }} />
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <div className="flex items-center justify-between text-xs font-medium text-slate-400">
            <span>Daily Loss Utilization</span>
            <span className="font-mono text-slate-300">{lossPct.toFixed(1)}%</span>
          </div>
          <div className="mt-1.5 font-mono text-xl font-bold text-slate-100 tabular-nums">
            ₹{dailyLoss.toLocaleString('en-IN', { maximumFractionDigits: 0 })} / ₹{limits.max_daily_loss.toLocaleString('en-IN')}
          </div>
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
            <div
              className={`h-full ${lossPct > 75 ? 'bg-red-500' : 'bg-emerald-500'}`}
              style={{ width: `${lossPct}%` }}
            />
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <div className="text-xs font-medium text-slate-400">Kill Switch Status</div>
          <div
            className={`mt-1.5 flex items-center gap-2 font-mono text-lg font-bold ${
              killSwitch.engaged ? 'text-red-400' : 'text-emerald-400'
            }`}
          >
            {killSwitch.engaged ? <ShieldOff className="h-5 w-5" /> : <ShieldCheck className="h-5 w-5" />}
            <span>{killSwitch.engaged ? 'HALTED' : 'ARMED & READY'}</span>
          </div>
          <div className="mt-1 font-mono text-[11px] text-slate-500">
            Max Open Positions: {metrics?.open_positions ?? 0} / {limits.max_open_positions}
          </div>
        </div>
      </div>

      {/* Main 2-Column Controls: Left Risk Policy Editor | Right Kill Switch + 6-Gate Preflight */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        {/* Editable Risk Policy Form */}
        <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#111827] xl:col-span-7">
          <div className="border-b border-slate-800 px-5 py-3.5">
            <h3 className="text-sm font-semibold text-slate-100">Capital &amp; Exposure Risk Policy</h3>
          </div>
          <div className="space-y-4 p-5">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {[
                { key: 'capital' as const, label: 'Allocated Capital (₹)', step: 25000 },
                { key: 'max_order_value' as const, label: 'Max Single Order Value (₹)', step: 10000 },
                { key: 'max_total_exposure' as const, label: 'Max Portfolio Exposure (₹)', step: 25000 },
                { key: 'max_daily_loss' as const, label: 'Max Daily Loss Stop (₹)', step: 2500 },
                { key: 'max_open_positions' as const, label: 'Max Open Positions', step: 1 },
                { key: 'max_symbol_exposure_pct' as const, label: 'Max Per-Symbol Exposure (%)', step: 1 },
                { key: 'max_orders_per_minute' as const, label: 'Order Rate Limit (Orders / Min)', step: 1 },
              ].map((field) => (
                <div key={field.key}>
                  <label className="mb-1.5 block text-xs font-medium text-slate-300">{field.label}</label>
                  <input
                    type="number"
                    min={0}
                    step={field.step}
                    value={limits[field.key]}
                    onChange={(e) =>
                      setLimits((prev) => ({
                        ...prev,
                        [field.key]: Math.max(0, Number(e.target.value) || 0),
                      }))
                    }
                    className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-sm text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
                  />
                </div>
              ))}
            </div>

            {feedback && (
              <div
                className={`flex items-center gap-2 rounded-lg border px-3.5 py-2.5 text-xs ${
                  feedback.type === 'ok'
                    ? 'border-emerald-800/70 bg-emerald-950/40 text-emerald-200'
                    : 'border-red-900/70 bg-red-950/40 text-red-200'
                }`}
              >
                <CheckCircle2 className="h-4 w-4 shrink-0" />
                <span>{feedback.text}</span>
              </div>
            )}

            <div className="flex justify-end pt-1">
              <button
                type="button"
                onClick={() => void handleSaveLimits()}
                disabled={saving}
                className="inline-flex min-h-[40px] items-center gap-1.5 rounded-lg bg-sky-500 px-5 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
              >
                <Save className="h-4 w-4" />
                <span>{saving ? 'Saving Policy...' : 'Save Risk Limits'}</span>
              </button>
            </div>
          </div>
        </div>

        {/* Right Column: Emergency Kill Switch + 6-Gate Preflight Dry-Run */}
        <div className="space-y-4 xl:col-span-5">
          {/* Kill Switch Card */}
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
            <div className="flex items-center gap-2">
              <OctagonAlert className="h-4 w-4 text-red-400" />
              <h3 className="text-sm font-semibold text-slate-100">Emergency Kill Switch</h3>
            </div>
            <p className="mt-1.5 text-xs text-slate-400">
              Immediately halts all new quick orders, 1-click alert executions, and gated auto-execute strategies on the server.
            </p>
            <div className="mt-3 space-y-3">
              <input
                type="text"
                value={killReason}
                onChange={(e) => setKillReason(e.target.value)}
                placeholder="Reason for halting trading"
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
              <div className="flex gap-2">
                {killSwitch.engaged ? (
                  <button
                    type="button"
                    onClick={() => void handleToggleKillSwitch(false)}
                    disabled={killBusy}
                    className="min-h-[38px] flex-1 rounded-lg bg-emerald-500 px-4 text-xs font-semibold text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
                  >
                    Release Kill Switch &amp; Resume
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void handleToggleKillSwitch(true)}
                    disabled={killBusy}
                    className="min-h-[38px] flex-1 rounded-lg bg-red-600 px-4 text-xs font-semibold text-white hover:bg-red-500 disabled:opacity-50"
                  >
                    Engage Emergency Kill Switch
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* 6-Gate Preflight Verifier */}
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-5">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-sky-400" />
              <h3 className="text-sm font-semibold text-slate-100">6-Gate Order Preflight Verifier</h3>
            </div>
            <p className="mt-1 text-xs text-slate-400">
              Dry-run an order through the exact server-side safety gates before live execution.
            </p>
            <div className="mt-3 grid grid-cols-2 gap-2.5 text-xs">
              <input
                type="text"
                value={pfSymbol}
                onChange={(e) => setPfSymbol(e.target.value)}
                placeholder="Symbol"
                className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-slate-100"
              />
              <select
                value={pfSide}
                onChange={(e) => setPfSide(e.target.value as 'BUY' | 'SELL')}
                className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-slate-100"
              >
                <option value="BUY">BUY</option>
                <option value="SELL">SELL</option>
              </select>
              <input
                type="number"
                value={pfQty}
                onChange={(e) => setPfQty(e.target.value)}
                placeholder="Qty"
                className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-slate-100"
              />
              <input
                type="number"
                value={pfPrice}
                onChange={(e) => setPfPrice(e.target.value)}
                placeholder="Est Price"
                className="min-h-[36px] rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-slate-100"
              />
            </div>
            <button
              type="button"
              onClick={() => void handleRunPreflight()}
              disabled={pfBusy}
              className="mt-3 min-h-[36px] w-full rounded-lg border border-sky-700/70 bg-sky-950/30 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
            >
              {pfBusy ? 'Running 6 Gates...' : 'Run Preflight Dry-Run'}
            </button>

            {pfResult && (
              <div className="mt-3 space-y-1.5 rounded-lg border border-slate-800 bg-[#0b1120] p-3 text-xs">
                <div className={`font-semibold ${pfResult.allowed ? 'text-emerald-400' : 'text-red-400'}`}>
                  {pfResult.summary}
                </div>
                {pfResult.checks && pfResult.checks.length > 0 && (
                  <div className="space-y-1 pt-1">
                    {pfResult.checks.map((c, i) => {
                      const ok = Boolean(c.passed ?? c.ok);
                      return (
                        <div key={i} className="flex items-center justify-between gap-2 font-mono text-[11px]">
                          <span className="text-slate-300">{c.gate || c.name || `Gate ${i + 1}`}</span>
                          <span className={ok ? 'text-emerald-400' : 'text-red-400'}>
                            {ok ? 'PASS' : 'BLOCK'} {c.detail || c.message ? `· ${c.detail || c.message}` : ''}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
