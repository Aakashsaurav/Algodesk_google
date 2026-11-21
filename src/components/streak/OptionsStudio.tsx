import { useCallback, useEffect, useState } from 'react';
import { Plus, Trash2, RefreshCw } from 'lucide-react';
import type { ChargeBreakdown } from './types';

interface OptionLeg {
  side: 'BUY' | 'SELL';
  option_type: 'CE' | 'PE';
  strike: number;
  lots: number;
  lot_size: number;
  premium: number;
  iv_pct: number;
}

interface OptionsAnalysis {
  ok: boolean;
  spot: number;
  dte_days: number;
  net_premium: number;
  net_premium_label: string;
  max_profit: number;
  max_profit_unlimited: boolean;
  max_loss: number;
  max_loss_unlimited: boolean;
  risk_reward: number | null;
  breakevens: number[];
  estimated_margin_inr: number;
  charges: ChargeBreakdown;
  net_greeks: { delta: number; gamma: number; theta: number; vega: number };
  legs: Array<OptionLeg & { qty: number; bs_price: number; delta: number; gamma: number; theta: number; vega: number }>;
  payoff_curve: Array<{ spot: number; expiry_pnl: number; t0_pnl: number }>;
}

interface Props {
  templates:
    | Array<{ id: string; name: string; outlook?: string; description?: string }>
    | Record<string, { id: string; name: string; outlook?: string; description?: string }>;
  initialTemplate?: { id: string; spot?: number; step?: number; lotSize?: number } | null;
}

const fmtInr = (v: number | null | undefined) =>
  `₹${Number(v ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function OptionsStudio({ templates, initialTemplate }: Props) {
  const templateList = Array.isArray(templates)
    ? templates
    : templates && typeof templates === 'object'
      ? Object.values(templates)
      : [];
  const [spot, setSpot] = useState(initialTemplate?.spot ?? 24200);
  const [step, setStep] = useState(initialTemplate?.step ?? 100);
  const [lotSize, setLotSize] = useState(initialTemplate?.lotSize ?? 75);
  const [dteDays, setDteDays] = useState(7);
  const [ivPct, setIvPct] = useState(15);
  const [chargesProfile, setChargesProfile] = useState<'zerodha' | 'upstox' | 'zero'>('zerodha');
  const [activeTemplate, setActiveTemplate] = useState(initialTemplate?.id ?? 'iron_condor');
  const [legs, setLegs] = useState<OptionLeg[]>([]);
  const [analysis, setAnalysis] = useState<OptionsAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadTemplate = useCallback(
    async (tplId: string, customSpot = spot, customStep = step, customLot = lotSize, customDte = dteDays, customIv = ivPct, prof = chargesProfile) => {
      setLoading(true);
      setError('');
      try {
        const res = await fetch('/api/streak/options/template', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            template_id: tplId,
            spot: customSpot,
            step: customStep,
            lot_size: customLot,
            dte_days: customDte,
            iv_pct: customIv,
            charges_profile: prof,
          }),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail || 'Could not build options template.');
        setActiveTemplate(tplId);
        setLegs(body.legs || []);
        setAnalysis(body.analysis || null);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Options template failed.');
      } finally {
        setLoading(false);
      }
    },
    [spot, step, lotSize, dteDays, ivPct, chargesProfile]
  );

  const analyzeLegs = useCallback(
    async (nextLegs: OptionLeg[]) => {
      if (nextLegs.length === 0) return;
      setLoading(true);
      setError('');
      try {
        const res = await fetch('/api/streak/options/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            spot,
            legs: nextLegs,
            dte_days: dteDays,
            charges_profile: chargesProfile,
          }),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail || 'Options payoff calculation failed.');
        setAnalysis(body);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Options calculation failed.');
      } finally {
        setLoading(false);
      }
    },
    [spot, dteDays, chargesProfile]
  );

  useEffect(() => {
    const tid = initialTemplate?.id ?? 'iron_condor';
    const sp = initialTemplate?.spot ?? 24200;
    const st = initialTemplate?.step ?? 100;
    const ls = initialTemplate?.lotSize ?? 75;
    setSpot(sp);
    setStep(st);
    setLotSize(ls);
    void loadTemplate(tid, sp, st, ls, 7, 15, 'zerodha');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTemplate?.id, initialTemplate?.spot]);

  const updateLeg = (idx: number, patch: Partial<OptionLeg>) => {
    const next = legs.map((l, i) => (i === idx ? { ...l, ...patch } : l));
    setLegs(next);
  };

  const addLeg = () => {
    setLegs([
      ...legs,
      { side: 'BUY', option_type: 'CE', strike: Math.round(spot / step) * step, lots: 1, lot_size: lotSize, premium: 120, iv_pct: ivPct },
    ]);
  };

  const removeLeg = (idx: number) => {
    setLegs(legs.filter((_, i) => i !== idx));
  };

  // Render clean SVG Payoff Chart (Expiry solid + T+0 dashed + Breakeven markers)
  const renderPayoffSvg = () => {
    const pts = analysis?.payoff_curve || [];
    if (pts.length < 2) return null;
    const W = 760;
    const H = 260;
    const padL = 64;
    const padR = 20;
    const padT = 18;
    const padB = 34;

    const minX = pts[0].spot;
    const maxX = pts[pts.length - 1].spot;
    const allY = pts.flatMap((p) => [p.expiry_pnl, p.t0_pnl, 0]);
    const minY = Math.min(...allY);
    const maxY = Math.max(...allY);
    const spanY = Math.max(100, maxY - minY);

    const sx = (x: number) => padL + ((x - minX) / Math.max(1, maxX - minX)) * (W - padL - padR);
    const sy = (y: number) => padT + ((maxY - y) / spanY) * (H - padT - padB);
    const zeroY = sy(0);
    const spotX = sx(analysis!.spot);

    const expPath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${sx(p.spot).toFixed(1)},${sy(p.expiry_pnl).toFixed(1)}`).join(' ');
    const t0Path = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${sx(p.spot).toFixed(1)},${sy(p.t0_pnl).toFixed(1)}`).join(' ');

    return (
      <svg viewBox={`0 0 ${W} ${H}`} className="h-[260px] w-full select-none">
        <rect x={padL} y={padT} width={W - padL - padR} height={H - padT - padB} fill="#090e1a" rx={6} />
        {/* Zero line */}
        <line x1={padL} y1={zeroY} x2={W - padR} y2={zeroY} stroke="#475569" strokeDasharray="4 4" strokeWidth={1} />
        {/* Spot vertical line */}
        <line x1={spotX} y1={padT} x2={spotX} y2={H - padB} stroke="#38bdf8" strokeDasharray="3 3" strokeWidth={1} />
        <text x={spotX + 4} y={padT + 12} fill="#38bdf8" fontSize="10" fontFamily="IBM Plex Mono">
          Spot {analysis!.spot.toLocaleString('en-IN')}
        </text>
        {/* T+0 dashed curve */}
        <path d={t0Path} fill="none" stroke="#94a3b8" strokeWidth={1.6} strokeDasharray="4 3" />
        {/* Expiry payoff curve */}
        <path d={expPath} fill="none" stroke="#34d399" strokeWidth={2.2} />
        {/* Breakevens */}
        {analysis!.breakevens.map((be, idx) => (
          <g key={idx}>
            <circle cx={sx(be)} cy={zeroY} r={4} fill="#f59e0b" />
            <text x={sx(be)} y={zeroY - 8} textAnchor="middle" fill="#fbbf24" fontSize="10" fontFamily="IBM Plex Mono">
              BE {be.toLocaleString('en-IN')}
            </text>
          </g>
        ))}
        {/* Axis labels */}
        <text x={padL - 8} y={padT + 10} textAnchor="end" fill="#94a3b8" fontSize="10" fontFamily="IBM Plex Mono">
          {Math.round(maxY).toLocaleString('en-IN')}
        </text>
        <text x={padL - 8} y={zeroY + 3} textAnchor="end" fill="#cbd5e1" fontSize="10" fontFamily="IBM Plex Mono">
          0
        </text>
        <text x={padL - 8} y={H - padB} textAnchor="end" fill="#94a3b8" fontSize="10" fontFamily="IBM Plex Mono">
          {Math.round(minY).toLocaleString('en-IN')}
        </text>
        <text x={padL} y={H - 10} fill="#64748b" fontSize="10" fontFamily="IBM Plex Mono">
          {Math.round(minX).toLocaleString('en-IN')}
        </text>
        <text x={W - padR} y={H - 10} textAnchor="end" fill="#64748b" fontSize="10" fontFamily="IBM Plex Mono">
          {Math.round(maxX).toLocaleString('en-IN')}
        </text>
      </svg>
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-100">Options Strategy Builder &amp; Payoff Charts</h1>
          <p className="text-sm text-slate-400">
            Zero-dependency Black-Scholes Greeks, Expiry &amp; T+0 payoff simulation, breakevens, margin estimate, and April 2026 NSE Options charges.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void analyzeLegs(legs)}
          disabled={loading || legs.length === 0}
          className="inline-flex h-9 items-center gap-2 rounded-lg bg-sky-400 px-4 text-xs font-semibold text-sky-950 hover:bg-sky-300 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          Recalculate Payoff
        </button>
      </div>

      {/* Templates Strip */}
      <div className="rounded-xl border border-slate-800 bg-[#111827] p-4 space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          {templateList.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => void loadTemplate(t.id)}
              className={`rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                activeTemplate === t.id
                  ? 'border-sky-400 bg-sky-500/15 text-sky-300'
                  : 'border-slate-800 bg-[#0b1120] text-slate-300 hover:border-slate-600'
              }`}
              title={t.description}
            >
              {t.name}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-6">
          <label className="space-y-1">
            <span className="block text-[11px] font-medium text-slate-400">Underlying Spot</span>
            <input type="number" value={spot} onChange={(e) => setSpot(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
          </label>
          <label className="space-y-1">
            <span className="block text-[11px] font-medium text-slate-400">Strike Step</span>
            <input type="number" value={step} onChange={(e) => setStep(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
          </label>
          <label className="space-y-1">
            <span className="block text-[11px] font-medium text-slate-400">Lot Size</span>
            <input type="number" value={lotSize} onChange={(e) => setLotSize(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
          </label>
          <label className="space-y-1">
            <span className="block text-[11px] font-medium text-slate-400">Days to Expiry</span>
            <input type="number" step="0.5" min={0.5} value={dteDays} onChange={(e) => setDteDays(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
          </label>
          <label className="space-y-1">
            <span className="block text-[11px] font-medium text-slate-400">Default IV %</span>
            <input type="number" step="0.5" min={2} value={ivPct} onChange={(e) => setIvPct(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
          </label>
          <label className="space-y-1">
            <span className="block text-[11px] font-medium text-slate-400">Charges Preset</span>
            <select value={chargesProfile} onChange={(e) => setChargesProfile(e.target.value as 'zerodha' | 'upstox' | 'zero')} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
              <option value="zerodha">Zerodha (2026)</option>
              <option value="upstox">Upstox (2026)</option>
              <option value="zero">Zero Cost</option>
            </select>
          </label>
        </div>
      </div>

      {error && <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-4 py-3 text-xs text-red-300">{error}</div>}

      {/* KPI Strip */}
      {analysis && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">Max Profit</p>
            <p className="mt-1 font-mono text-lg font-semibold text-emerald-400 tabular-nums">
              {analysis.max_profit_unlimited ? 'Unlimited' : fmtInr(analysis.max_profit)}
            </p>
          </div>
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">Max Loss</p>
            <p className="mt-1 font-mono text-lg font-semibold text-red-400 tabular-nums">
              {analysis.max_loss_unlimited ? 'Unlimited' : fmtInr(analysis.max_loss)}
            </p>
          </div>
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">{analysis.net_premium_label}</p>
            <p className="mt-1 font-mono text-lg font-semibold text-slate-100 tabular-nums">{fmtInr(Math.abs(analysis.net_premium))}</p>
          </div>
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">Breakevens</p>
            <p className="mt-1 font-mono text-sm font-semibold text-amber-300 tabular-nums">
              {analysis.breakevens.length ? analysis.breakevens.map((b) => b.toLocaleString('en-IN')).join(' / ') : '—'}
            </p>
          </div>
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">Est. Margin</p>
            <p className="mt-1 font-mono text-lg font-semibold text-slate-100 tabular-nums">{fmtInr(analysis.estimated_margin_inr)}</p>
          </div>
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500">2026 Charges</p>
            <p className="mt-1 font-mono text-lg font-semibold text-slate-200 tabular-nums">{fmtInr(analysis.charges.total_charges)}</p>
          </div>
        </div>
      )}

      {/* Payoff Diagram + Net Greeks */}
      {analysis && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_280px]">
          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
            <div className="mb-2 flex items-center justify-between text-xs text-slate-400">
              <span className="font-semibold text-slate-200">Payoff Curve (Solid: Expiry · Dashed: T+0)</span>
              <span className="font-mono">DTE: {analysis.dte_days}d · RR: {analysis.risk_reward ?? '—'}</span>
            </div>
            {renderPayoffSvg()}
          </div>

          <div className="rounded-xl border border-slate-800 bg-[#111827] p-4 space-y-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Portfolio Greeks (Net)</h3>
            <div className="grid grid-cols-2 gap-3 font-mono text-xs tabular-nums">
              <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                <div className="text-slate-500">Delta</div>
                <div className="mt-1 text-base font-semibold text-slate-100">{analysis.net_greeks.delta}</div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                <div className="text-slate-500">Gamma</div>
                <div className="mt-1 text-base font-semibold text-slate-100">{analysis.net_greeks.gamma}</div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                <div className="text-slate-500">Theta / Day</div>
                <div className={`mt-1 text-base font-semibold ${analysis.net_greeks.theta >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {analysis.net_greeks.theta}
                </div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                <div className="text-slate-500">Vega / 1% IV</div>
                <div className="mt-1 text-base font-semibold text-slate-100">{analysis.net_greeks.vega}</div>
              </div>
            </div>
            <div className="border-t border-slate-800 pt-3 text-xs text-slate-400 space-y-1 font-mono tabular-nums">
              <div className="flex justify-between"><span>Brokerage</span><span>{fmtInr(analysis.charges.brokerage)}</span></div>
              <div className="flex justify-between"><span>STT (0.15% sell)</span><span>{fmtInr(analysis.charges.stt)}</span></div>
              <div className="flex justify-between"><span>Exchange + SEBI</span><span>{fmtInr(analysis.charges.exchange_txn + analysis.charges.sebi_fee)}</span></div>
              <div className="flex justify-between"><span>GST (18%)</span><span>{fmtInr(analysis.charges.gst)}</span></div>
              <div className="flex justify-between"><span>Stamp Duty</span><span>{fmtInr(analysis.charges.stamp_duty)}</span></div>
            </div>
          </div>
        </div>
      )}

      {/* Multi-Leg Editor Table */}
      <div className="rounded-xl border border-slate-800 bg-[#111827] p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-200">Option Legs ({legs.length})</h3>
          <button
            type="button"
            onClick={addLeg}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-200 hover:border-sky-500/50"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Leg
          </button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="py-2 pr-2">Side</th>
                <th className="py-2 pr-2">Type</th>
                <th className="py-2 pr-2">Strike</th>
                <th className="py-2 pr-2">Lots</th>
                <th className="py-2 pr-2">Lot Size</th>
                <th className="py-2 pr-2">Premium (₹)</th>
                <th className="py-2 pr-2">IV %</th>
                <th className="py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70">
              {legs.map((leg, idx) => (
                <tr key={idx}>
                  <td className="py-2 pr-2">
                    <select
                      value={leg.side}
                      onChange={(e) => updateLeg(idx, { side: e.target.value as 'BUY' | 'SELL' })}
                      className={`h-8 rounded border border-slate-700 bg-[#0b1120] px-2 font-semibold ${
                        leg.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'
                      }`}
                    >
                      <option value="BUY">BUY</option>
                      <option value="SELL">SELL</option>
                    </select>
                  </td>
                  <td className="py-2 pr-2">
                    <select
                      value={leg.option_type}
                      onChange={(e) => updateLeg(idx, { option_type: e.target.value as 'CE' | 'PE' })}
                      className="h-8 rounded border border-slate-700 bg-[#0b1120] px-2 text-slate-100"
                    >
                      <option value="CE">CE</option>
                      <option value="PE">PE</option>
                    </select>
                  </td>
                  <td className="py-2 pr-2">
                    <input type="number" step={step} value={leg.strike} onChange={(e) => updateLeg(idx, { strike: Number(e.target.value) })} className="h-8 w-24 rounded border border-slate-700 bg-[#0b1120] px-2 font-mono text-slate-100 tabular-nums" />
                  </td>
                  <td className="py-2 pr-2">
                    <input type="number" min={1} value={leg.lots} onChange={(e) => updateLeg(idx, { lots: Math.max(1, Number(e.target.value)) })} className="h-8 w-16 rounded border border-slate-700 bg-[#0b1120] px-2 font-mono text-slate-100 tabular-nums" />
                  </td>
                  <td className="py-2 pr-2">
                    <input type="number" min={1} value={leg.lot_size} onChange={(e) => updateLeg(idx, { lot_size: Math.max(1, Number(e.target.value)) })} className="h-8 w-20 rounded border border-slate-700 bg-[#0b1120] px-2 font-mono text-slate-100 tabular-nums" />
                  </td>
                  <td className="py-2 pr-2">
                    <input type="number" step="0.5" min={0.05} value={leg.premium} onChange={(e) => updateLeg(idx, { premium: Number(e.target.value) })} className="h-8 w-24 rounded border border-slate-700 bg-[#0b1120] px-2 font-mono text-slate-100 tabular-nums" />
                  </td>
                  <td className="py-2 pr-2">
                    <input type="number" step="0.5" min={1} value={leg.iv_pct} onChange={(e) => updateLeg(idx, { iv_pct: Number(e.target.value) })} className="h-8 w-20 rounded border border-slate-700 bg-[#0b1120] px-2 font-mono text-slate-100 tabular-nums" />
                  </td>
                  <td className="py-2 text-right">
                    <button type="button" onClick={() => removeLeg(idx)} className="rounded p-1.5 text-slate-500 hover:bg-red-950/40 hover:text-red-400">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
