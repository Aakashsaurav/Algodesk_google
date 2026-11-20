import { useState } from 'react';
import { Search, RefreshCw } from 'lucide-react';

interface TechnicalsResult {
  ok: boolean;
  symbol: string;
  exchange: string;
  timeframe: string;
  provider: string;
  close: number;
  change_pct: number;
  bars_analyzed: number;
  as_of_date: string;
  summary: {
    rating: string;
    score: number;
    buy: number;
    neutral: number;
    sell: number;
  };
  moving_averages: {
    summary: { rating: string; buy: number; neutral: number; sell: number };
    rows: Array<{ name: string; value: number | null; signal: string }>;
  };
  oscillators: {
    summary: { rating: string; buy: number; neutral: number; sell: number };
    rows: Array<{ name: string; value: number | null; signal: string; note?: string }>;
  };
  pivots: {
    classic: Record<string, number>;
    camarilla: Record<string, number>;
  };
  sr_zones: Array<{
    type: string;
    classification: string;
    lower: number;
    upper: number;
    representative_price: number;
    strength: number;
    touches: number;
  }>;
}

const TF_OPTIONS = [
  { id: '5minute', label: '5m' },
  { id: '15minute', label: '15m' },
  { id: '60minute', label: '1h' },
  { id: '1day', label: '1D' },
  { id: '1week', label: '1W' },
];

export function TechnicalsView() {
  const [symbol, setSymbol] = useState('RELIANCE');
  const [exchange, setExchange] = useState('NSE');
  const [timeframe, setTimeframe] = useState('1day');
  const [data, setData] = useState<TechnicalsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const runAnalysis = async (tf = timeframe, sym = symbol) => {
    const clean = sym.trim().toUpperCase();
    if (!clean) return;
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/streak/technicals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: clean, exchange, timeframe: tf }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Technicals analysis failed.');
      setData(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Technicals analysis failed.');
    } finally {
      setLoading(false);
    }
  };

  const signalColor = (sig: string) => {
    const u = sig.toUpperCase();
    if (u.includes('BUY')) return 'text-emerald-400';
    if (u.includes('SELL')) return 'text-red-400';
    return 'text-slate-400';
  };

  // Semicircle SVG Gauge (-1 to +1 score -> -90deg to +90deg needle)
  const renderGauge = (score: number, rating: string) => {
    const clamped = Math.max(-1, Math.min(1, score));
    const angleDeg = clamped * 80;
    return (
      <div className="flex flex-col items-center">
        <svg viewBox="0 0 220 125" className="h-[125px] w-[220px]">
          <path d="M 20 110 A 90 90 0 0 1 65 32" fill="none" stroke="#ef4444" strokeWidth="10" strokeLinecap="round" />
          <path d="M 70 29 A 90 90 0 0 1 150 29" fill="none" stroke="#64748b" strokeWidth="10" />
          <path d="M 155 32 A 90 90 0 0 1 200 110" fill="none" stroke="#10b981" strokeWidth="10" strokeLinecap="round" />
          <g transform={`translate(110, 110) rotate(${angleDeg})`}>
            <line x1="0" y1="0" x2="0" y2="-74" stroke="#f8fafc" strokeWidth="2.5" strokeLinecap="round" />
            <circle cx="0" cy="0" r="5" fill="#38bdf8" />
          </g>
        </svg>
        <p className={`-mt-2 text-base font-bold tracking-tight ${signalColor(rating)}`}>{rating}</p>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold tracking-tight text-slate-100">Technicals &amp; Multi-Timeframe Gauges</h1>
        <p className="text-sm text-slate-400">
          Moving averages, oscillators, Classic &amp; Camarilla pivots, and ATR-clustered Major/Minor Support &amp; Resistance zones.
        </p>
      </div>

      {/* Controls */}
      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-slate-800 bg-[#111827] p-4">
        <label className="space-y-1">
          <span className="block text-xs font-medium text-slate-400">Symbol</span>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-500" />
            <input
              type="text"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === 'Enter' && void runAnalysis()}
              placeholder="RELIANCE"
              className="h-9 w-44 rounded-md border border-slate-700 bg-[#0b1120] pl-8 pr-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
            />
          </div>
        </label>
        <label className="space-y-1">
          <span className="block text-xs font-medium text-slate-400">Exchange</span>
          <select value={exchange} onChange={(e) => setExchange(e.target.value)} className="h-9 rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-100">
            <option value="NSE">NSE</option>
            <option value="BSE">BSE</option>
          </select>
        </label>
        <div className="space-y-1">
          <span className="block text-xs font-medium text-slate-400">Timeframe</span>
          <div className="inline-flex rounded-md border border-slate-700 bg-[#0b1120] p-0.5">
            {TF_OPTIONS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => {
                  setTimeframe(t.id);
                  if (data) void runAnalysis(t.id, symbol);
                }}
                className={`rounded px-3 py-1.5 font-mono text-xs font-semibold transition-colors ${
                  timeframe === t.id ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
        <button
          type="button"
          onClick={() => void runAnalysis()}
          disabled={loading}
          className="inline-flex h-9 items-center gap-2 rounded-lg bg-sky-400 px-4 text-xs font-semibold text-sky-950 hover:bg-sky-300 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          {loading ? 'Analyzing...' : 'Analyze Technicals'}
        </button>
      </div>

      {error && <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-4 py-3 text-xs text-red-300">{error}</div>}

      {!data && !loading && (
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-8 text-center text-sm text-slate-400">
          Enter any NSE/BSE symbol above and click <strong className="text-slate-200">Analyze Technicals</strong> to compute real-time gauges, pivots, and S&amp;R zones from AlgoDesk&rsquo;s historical data engine.
        </div>
      )}

      {data && (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 flex flex-col items-center justify-center">
              <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">Overall Technical Summary</p>
              {renderGauge(data.summary.score, data.summary.rating)}
              <p className="mt-2 font-mono text-xs text-slate-400 tabular-nums">
                Buy {data.summary.buy} · Neutral {data.summary.neutral} · Sell {data.summary.sell}
              </p>
            </div>

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-3">
              <div className="flex items-baseline justify-between">
                <div>
                  <h2 className="text-lg font-bold text-slate-100">{data.symbol}</h2>
                  <p className="font-mono text-xs text-slate-500">{data.exchange} · {data.timeframe} · {data.bars_analyzed} bars · {data.provider.toUpperCase()}</p>
                </div>
                <div className="text-right font-mono tabular-nums">
                  <div className="text-lg font-bold text-slate-100">₹{data.close.toLocaleString('en-IN')}</div>
                  <div className={`text-xs font-semibold ${data.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {data.change_pct >= 0 ? '+' : ''}{data.change_pct}%
                  </div>
                </div>
              </div>
              <div className="border-t border-slate-800 pt-3 grid grid-cols-2 gap-3 text-xs">
                <div>
                  <div className="text-slate-500">Moving Averages</div>
                  <div className={`font-semibold ${signalColor(data.moving_averages.summary.rating)}`}>{data.moving_averages.summary.rating}</div>
                  <div className="font-mono text-[11px] text-slate-500">B {data.moving_averages.summary.buy} · N {data.moving_averages.summary.neutral} · S {data.moving_averages.summary.sell}</div>
                </div>
                <div>
                  <div className="text-slate-500">Oscillators</div>
                  <div className={`font-semibold ${signalColor(data.oscillators.summary.rating)}`}>{data.oscillators.summary.rating}</div>
                  <div className="font-mono text-[11px] text-slate-500">B {data.oscillators.summary.buy} · N {data.oscillators.summary.neutral} · S {data.oscillators.summary.sell}</div>
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-2">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Classic vs Camarilla Pivots</h3>
              <div className="grid grid-cols-3 gap-y-1 font-mono text-xs tabular-nums">
                <span className="text-slate-500">Level</span>
                <span className="text-right text-slate-500">Classic</span>
                <span className="text-right text-slate-500">Camarilla</span>
                {['R3', 'R2', 'R1', 'P', 'S1', 'S2', 'S3'].map((k) => (
                  <div key={k} className="contents">
                    <span className={k.startsWith('R') ? 'text-red-400' : k.startsWith('S') ? 'text-emerald-400' : 'text-sky-400'}>{k}</span>
                    <span className="text-right text-slate-200">{data.pivots.classic[k]?.toLocaleString('en-IN') ?? '—'}</span>
                    <span className="text-right text-slate-200">{data.pivots.camarilla[k]?.toLocaleString('en-IN') ?? '—'}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <h3 className="mb-3 text-sm font-semibold text-slate-200">Moving Averages</h3>
              <table className="w-full text-xs">
                <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                  <tr><th className="py-1.5 text-left">Indicator</th><th className="py-1.5 text-right">Value</th><th className="py-1.5 text-right">Signal</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                  {data.moving_averages.rows.map((r) => (
                    <tr key={r.name}>
                      <td className="py-2 font-sans text-slate-200">{r.name}</td>
                      <td className="py-2 text-right text-slate-300">{r.value != null ? r.value.toLocaleString('en-IN') : '—'}</td>
                      <td className={`py-2 text-right font-sans font-semibold ${signalColor(r.signal)}`}>{r.signal}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <h3 className="mb-3 text-sm font-semibold text-slate-200">Oscillators &amp; Trend</h3>
              <table className="w-full text-xs">
                <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                  <tr><th className="py-1.5 text-left">Indicator</th><th className="py-1.5 text-right">Value</th><th className="py-1.5 text-right">Signal</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                  {data.oscillators.rows.map((r) => (
                    <tr key={r.name}>
                      <td className="py-2 font-sans text-slate-200">{r.name}</td>
                      <td className="py-2 text-right text-slate-300">{r.value != null ? r.value.toLocaleString('en-IN') : '—'}</td>
                      <td className={`py-2 text-right font-sans font-semibold ${signalColor(r.signal)}`}>{r.signal}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {data.sr_zones.length > 0 && (
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <h3 className="mb-3 text-sm font-semibold text-slate-200">Detected Support &amp; Resistance Zones</h3>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                    <tr>
                      <th className="py-1.5 text-left">Type</th>
                      <th className="py-1.5 text-left">Tier</th>
                      <th className="py-1.5 text-right">Zone Band</th>
                      <th className="py-1.5 text-right">Median</th>
                      <th className="py-1.5 text-right">Strength</th>
                      <th className="py-1.5 text-right">Touches</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                    {data.sr_zones.map((z, i) => (
                      <tr key={i}>
                        <td className={`py-2 font-sans font-semibold ${z.type === 'Support' ? 'text-emerald-400' : 'text-red-400'}`}>{z.type}</td>
                        <td className="py-2 font-sans text-slate-300">{z.classification}</td>
                        <td className="py-2 text-right text-slate-300">₹{z.lower} – ₹{z.upper}</td>
                        <td className="py-2 text-right text-slate-100">₹{z.representative_price}</td>
                        <td className="py-2 text-right text-sky-300">{z.strength}/100</td>
                        <td className="py-2 text-right text-slate-400">{z.touches}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
