import { ArrowUpRight, Blocks, Code2, Radar, Sliders, Rocket, Gauge } from 'lucide-react';
import type { DiscoverItem, PlatformNavId, StreakMetaPayload } from './types';

interface Props {
  meta: StreakMetaPayload | null;
  brokerSummary: { trading: string; data: string; connectedCount: number; totalCount: number };
  onNavigate: (tab: PlatformNavId) => void;
  onOpenDiscoverItem: (item: DiscoverItem) => void;
}

const fmtInr = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function PlatformDashboard({ meta, brokerSummary, onNavigate, onOpenDiscoverItem }: Props) {
  const depSum = meta?.deployments_summary;
  const recent = meta?.recent_backtests || [];
  const featured = (meta?.discover || []).slice(0, 6);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-100">Trading Command Dashboard</h1>
          <p className="text-sm text-slate-400">
            Visual blocks &amp; custom Python strategies, Individual &amp; Portfolio backtesting with 2026 Indian charges, Paper &amp; Live deployment on shared Broker WebSocket.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onNavigate('strategies')}
            className="inline-flex h-9 items-center gap-2 rounded-lg bg-sky-400 px-4 text-xs font-semibold text-sky-950 hover:bg-sky-300"
          >
            <Blocks className="h-3.5 w-3.5" />
            Create Strategy
          </button>
          <button
            type="button"
            onClick={() => onNavigate('deployed')}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-slate-700 px-3.5 text-xs font-semibold text-slate-200 hover:bg-slate-800"
          >
            <Rocket className="h-3.5 w-3.5 text-emerald-400" />
            Deployed ({depSum?.running_count ?? 0})
          </button>
        </div>
      </div>

      {/* Executive KPI Strip */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Paper Trade MTM</p>
          <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${(depSum?.paper_net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {fmtInr(depSum?.paper_net_pnl ?? 0)}
          </p>
          <p className="font-mono text-[11px] text-slate-500">{depSum?.paper_count ?? 0} paper strategies</p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Live Trade MTM</p>
          <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${(depSum?.live_net_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {fmtInr(depSum?.live_net_pnl ?? 0)}
          </p>
          <p className="font-mono text-[11px] text-slate-500">{depSum?.live_count ?? 0} live strategies</p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">1-Click Order Alerts</p>
          <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${(depSum?.pending_alerts_count ?? 0) > 0 ? 'text-sky-400' : 'text-slate-200'}`}>
            {depSum?.pending_alerts_count ?? 0} Pending
          </p>
          <button type="button" onClick={() => onNavigate('deployed')} className="mt-0.5 text-[11px] font-medium text-sky-400 hover:underline">
            Review queue →
          </button>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Strategy Library</p>
          <p className="mt-1 font-mono text-lg font-bold text-slate-100 tabular-nums">
            {(meta?.visual_rules.length ?? 0) + (meta?.strategy_specs.length ?? 0)}
          </p>
          <p className="font-mono text-[11px] text-slate-500">
            {meta?.visual_rules.length ?? 0} Visual · {meta?.strategy_specs.length ?? 0} Python
          </p>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
          <p className="text-[11px] uppercase tracking-wider text-slate-500">Broker Routing</p>
          <p className="mt-1 font-mono text-sm font-bold text-slate-100">
            {brokerSummary.trading.toUpperCase()} / {brokerSummary.data.toUpperCase()}
          </p>
          <p className="font-mono text-[11px] text-slate-500">
            {brokerSummary.connectedCount}/{brokerSummary.totalCount} active ·{' '}
            <button type="button" onClick={() => onNavigate('connections')} className="text-sky-400 hover:underline">
              Manage
            </button>
          </p>
        </div>
      </div>

      {/* Quick Launchers */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          { id: 'strategies' as PlatformNavId, icon: Blocks, title: 'Visual & Python Strategies', desc: 'Individual & Portfolio backtests with real Zerodha/Upstox charges' },
          { id: 'scanners' as PlatformNavId, icon: Radar, title: 'Live WebSocket Scanners', desc: 'Historical warmup + real-time tick scans & MTF S&R' },
          { id: 'options' as PlatformNavId, icon: Sliders, title: 'Options Builder & Payoff', desc: 'Iron Condor, Straddles, Black-Scholes Greeks & 2026 charges' },
          { id: 'technicals' as PlatformNavId, icon: Gauge, title: 'Multi-TF Technicals', desc: 'Buy/Sell gauges, Classic/Camarilla pivots & S&R zones' },
        ].map((card) => (
          <button
            key={card.id}
            type="button"
            onClick={() => onNavigate(card.id)}
            className="group rounded-xl border border-slate-800 bg-[#111827] p-4 text-left transition-colors hover:border-sky-500/40"
          >
            <div className="flex items-center justify-between">
              <card.icon className="h-4 w-4 text-sky-400" />
              <ArrowUpRight className="h-3.5 w-3.5 text-slate-600 group-hover:text-sky-400" />
            </div>
            <h3 className="mt-3 text-sm font-semibold text-slate-100">{card.title}</h3>
            <p className="mt-1 text-xs text-slate-400">{card.desc}</p>
          </button>
        ))}
      </div>

      {/* Recent Backtests + Discover Highlights */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">Recent Backtest Runs</h2>
            <button type="button" onClick={() => onNavigate('strategies')} className="text-xs font-medium text-sky-400 hover:underline">
              Open Backtester →
            </button>
          </div>
          {recent.length === 0 ? (
            <p className="py-6 text-xs text-slate-500">
              No backtests run yet. Launch <strong>Strategies &amp; Backtest</strong> to run your first Individual or Portfolio simulation.
            </p>
          ) : (
            <div className="divide-y divide-slate-800/70">
              {recent.slice(0, 5).map((r) => (
                <div key={r.run_id} className="flex items-center justify-between py-2.5 text-xs">
                  <div>
                    <div className="font-semibold text-slate-200">{r.strategy_label}</div>
                    <div className="font-mono text-[11px] text-slate-500">
                      {r.mode} · {r.timeframe} · {r.symbols.length} sym · {r.charges_profile.toUpperCase()}
                    </div>
                  </div>
                  <div className="text-right font-mono tabular-nums">
                    <div className={`font-semibold ${r.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {r.net_pnl >= 0 ? '+' : ''}{fmtInr(r.net_pnl)}
                    </div>
                    <div className="text-[11px] text-slate-500">Win {r.win_rate_pct}% · Chg {fmtInr(r.total_charges)}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">Discover Templates</h2>
            <button type="button" onClick={() => onNavigate('discover')} className="text-xs font-medium text-sky-400 hover:underline">
              Browse all →
            </button>
          </div>
          <div className="divide-y divide-slate-800/70">
            {featured.map((item) => (
              <div key={item.id} className="flex items-center justify-between gap-3 py-2.5">
                <div>
                  <div className="flex items-center gap-2 text-xs font-semibold text-slate-200">
                    {item.mode === 'python' ? <Code2 className="h-3.5 w-3.5 text-sky-400" /> : <Blocks className="h-3.5 w-3.5 text-emerald-400" />}
                    <span>{item.name}</span>
                  </div>
                  <p className="font-mono text-[11px] text-slate-500">{item.category} · {item.mode} · {item.timeframe}</p>
                </div>
                <button
                  type="button"
                  onClick={() => onOpenDiscoverItem(item)}
                  className="shrink-0 rounded border border-slate-700 px-2.5 py-1 text-[11px] font-semibold text-slate-200 hover:border-sky-400 hover:text-sky-300"
                >
                  Open
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
