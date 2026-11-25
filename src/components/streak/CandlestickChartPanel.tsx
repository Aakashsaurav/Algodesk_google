import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import { RefreshCw, TrendingUp } from 'lucide-react';
import type { BacktestTradeRow } from './types';
import type { QuickOrderTarget } from './QuickOrderModal';

interface OhlcvBar {
  ts_ist: string;
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface CandlestickChartPanelProps {
  symbols: string[];
  activeSymbol: string;
  onSelectSymbol?: (sym: string) => void;
  exchange?: string;
  timeframe: string;
  onTimeframeChange?: (tf: string) => void;
  trades?: BacktestTradeRow[];
  onQuickOrder?: (target: QuickOrderTarget) => void;
  height?: number;
  compactHeader?: boolean;
}

const CHART_TFS = [
  { id: '1minute', label: '1 Minute' },
  { id: '5minute', label: '5 Minutes' },
  { id: '15minute', label: '15 Minutes' },
  { id: '60minute', label: '1 Hour' },
  { id: '1day', label: '1 Day' },
];

function computeEma(bars: OhlcvBar[], period: number): Array<number | null> {
  if (bars.length < period) return bars.map(() => null);
  const k = 2 / (period + 1);
  const out: Array<number | null> = new Array(bars.length).fill(null);
  let sum = 0;
  for (let i = 0; i < period; i += 1) sum += bars[i].close;
  let ema = sum / period;
  out[period - 1] = ema;
  for (let i = period; i < bars.length; i += 1) {
    ema = bars[i].close * k + ema * (1 - k);
    out[i] = ema;
  }
  return out;
}

export function CandlestickChartPanel({
  symbols,
  activeSymbol,
  onSelectSymbol,
  exchange = 'NSE',
  timeframe,
  onTimeframeChange,
  trades = [],
  onQuickOrder,
  height = 380,
  compactHeader = false,
}: CandlestickChartPanelProps) {
  const [bars, setBars] = useState<OhlcvBar[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [showEma20, setShowEma20] = useState(true);
  const [showEma50, setShowEma50] = useState(false);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const fetchBars = useCallback(
    async (sym: string, tf: string, forceDownload = false) => {
      const clean = sym.trim().toUpperCase();
      if (!clean) return;
      setLoading(true);
      setError('');
      try {
        if (!forceDownload) {
          const recRes = await apiFetch(
            `/api/data/records?symbol=${encodeURIComponent(clean)}&exchange=${encodeURIComponent(exchange)}&timeframe=${encodeURIComponent(tf)}&view=full`
          );
          if (recRes.ok) {
            const recBody = await recRes.json();
            if (Array.isArray(recBody.rows) && recBody.rows.length >= 10) {
              setBars(recBody.rows.slice(-90));
              setLoading(false);
              return;
            }
          }
        }
        // Gap-only download if not yet cached locally
        const toIso = new Date().toISOString().slice(0, 10);
        const lookbackMs = tf.includes('minute') ? 35 * 86400_000 : 180 * 86400_000;
        const fromIso = new Date(Date.now() - lookbackMs).toISOString().slice(0, 10);
        const dlRes = await apiFetch('/api/data/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            symbol: clean,
            exchange,
            timeframe: tf,
            from_date: fromIso,
            to_date: toIso,
          }),
        });
        const dlBody = await dlRes.json().catch(() => ({}));
        if (!dlRes.ok) throw new Error(dlBody.detail || `Could not load ${clean} candles.`);
        const recRes2 = await apiFetch(
          `/api/data/records?symbol=${encodeURIComponent(clean)}&exchange=${encodeURIComponent(exchange)}&timeframe=${encodeURIComponent(tf)}&view=full`
        );
        const recBody2 = await recRes2.json().catch(() => ({}));
        setBars(Array.isArray(recBody2.rows) ? recBody2.rows.slice(-90) : []);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load chart candles.');
        setBars([]);
      } finally {
        setLoading(false);
      }
    },
    [exchange]
  );

  useEffect(() => {
    if (activeSymbol) {
      void fetchBars(activeSymbol, timeframe, false);
    }
  }, [activeSymbol, timeframe, fetchBars]);

  const ema20 = useMemo(() => (showEma20 ? computeEma(bars, 20) : []), [bars, showEma20]);
  const ema50 = useMemo(() => (showEma50 ? computeEma(bars, 50) : []), [bars, showEma50]);

  const activeBar = hoverIdx !== null && bars[hoverIdx] ? bars[hoverIdx] : bars[bars.length - 1] || null;
  const prevBar =
    hoverIdx !== null && hoverIdx > 0
      ? bars[hoverIdx - 1]
      : bars.length >= 2
        ? bars[bars.length - 2]
        : activeBar;
  const chg = activeBar && prevBar ? activeBar.close - prevBar.close : 0;
  const chgPct = activeBar && prevBar && prevBar.close > 0 ? (chg / prevBar.close) * 100 : 0;

  // Map trades to bar indices for EN / TP / SL markers
  const markersByBar = useMemo(() => {
    const map = new Map<number, Array<{ kind: 'EN' | 'TP' | 'SL' | 'EX'; price: number }>>();
    if (!trades.length || !bars.length) return map;
    const symTrades = trades.filter(
      (t) => !t.symbol || t.symbol.toUpperCase() === activeSymbol.toUpperCase()
    );
    const findIdx = (dt: string) => {
      const target = dt.slice(0, 10);
      return bars.findIndex((b) => (b.trade_date || b.ts_ist.slice(0, 10)) >= target);
    };
    for (const tr of symTrades) {
      const eIdx = findIdx(tr.entry_date);
      if (eIdx >= 0) {
        const arr = map.get(eIdx) || [];
        arr.push({ kind: 'EN', price: tr.entry_price });
        map.set(eIdx, arr);
      }
      const xIdx = findIdx(tr.exit_date);
      if (xIdx >= 0) {
        const reason = (tr.exit_reason || '').toUpperCase();
        const k = reason.includes('TARGET') || reason.includes('TP') ? 'TP' : reason.includes('STOP') || reason.includes('SL') ? 'SL' : 'EX';
        const arr = map.get(xIdx) || [];
        arr.push({ kind: k, price: tr.exit_price });
        map.set(xIdx, arr);
      }
    }
    return map;
  }, [trades, bars, activeSymbol]);

  // SVG Layout Math
  const svgW = 760;
  const svgH = height;
  const padL = 14;
  const padR = 64;
  const padT = 16;
  const padB = 26;
  const volH = 56;
  const priceH = svgH - padT - padB - volH - 10;

  const { minP, maxP, maxVol } = useMemo(() => {
    if (!bars.length) return { minP: 0, maxP: 100, maxVol: 1 };
    let lo = Infinity;
    let hi = -Infinity;
    let mv = 1;
    for (const b of bars) {
      if (b.low < lo) lo = b.low;
      if (b.high > hi) hi = b.high;
      if (b.volume > mv) mv = b.volume;
    }
    const pad = (hi - lo) * 0.08 || 1;
    return { minP: lo - pad, maxP: hi + pad, maxVol: mv };
  }, [bars]);

  const plotW = svgW - padL - padR;
  const stepX = bars.length > 0 ? plotW / Math.max(bars.length, 1) : 10;
  const candleW = Math.max(2, Math.min(10, stepX * 0.65));

  const yForPrice = (p: number) =>
    padT + ((maxP - p) / Math.max(0.0001, maxP - minP)) * priceH;

  const buildLinePath = (series: Array<number | null>) => {
    const pts: string[] = [];
    series.forEach((v, i) => {
      if (v === null) return;
      const x = padL + i * stepX + stepX / 2;
      const y = yForPrice(v);
      pts.push(`${pts.length === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`);
    });
    return pts.join(' ');
  };

  const priceTicks = useMemo(() => {
    const out: number[] = [];
    const steps = 5;
    for (let i = 0; i <= steps; i += 1) {
      out.push(minP + ((maxP - minP) * i) / steps);
    }
    return out;
  }, [minP, maxP]);

  return (
    <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
      {/* Instrument tabs bar */}
      {symbols.length > 0 && !compactHeader && (
        <div className="flex items-center gap-1.5 overflow-x-auto border-b border-slate-800 bg-[#0b1120] px-3 py-2">
          {symbols.slice(0, 12).map((sym) => {
            const isSel = sym.toUpperCase() === activeSymbol.toUpperCase();
            return (
              <button
                key={sym}
                type="button"
                onClick={() => onSelectSymbol?.(sym)}
                className={`min-h-[30px] shrink-0 whitespace-nowrap rounded-md px-3 font-mono text-xs font-semibold transition-colors ${
                  isSel
                    ? 'bg-sky-500 text-slate-950'
                    : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'
                }`}
              >
                {sym}
              </button>
            );
          })}
        </div>
      )}

      {/* Chart toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-sm font-bold text-slate-100">{activeSymbol || 'SYMBOL'}</span>
            <span className="text-[11px] text-slate-500">{exchange}</span>
          </div>
          {activeBar && (
            <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] tabular-nums">
              <span className="text-slate-400">O <strong className="text-slate-200">{activeBar.open.toFixed(2)}</strong></span>
              <span className="text-slate-400">H <strong className="text-emerald-400">{activeBar.high.toFixed(2)}</strong></span>
              <span className="text-slate-400">L <strong className="text-red-400">{activeBar.low.toFixed(2)}</strong></span>
              <span className="text-slate-400">C <strong className="text-slate-100">{activeBar.close.toFixed(2)}</strong></span>
              <span className={chg >= 0 ? 'font-semibold text-emerald-400' : 'font-semibold text-red-400'}>
                {chg >= 0 ? '+' : ''}{chg.toFixed(2)} ({chgPct >= 0 ? '+' : ''}{chgPct.toFixed(2)}%)
              </span>
              <span className="text-slate-500">Vol {(activeBar.volume || 0).toLocaleString('en-IN')}</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2">
          {onQuickOrder && activeSymbol && (
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => onQuickOrder({ symbol: activeSymbol, exchange, side: 'BUY', ltp: activeBar?.close })}
                className="min-h-[28px] rounded bg-sky-500 px-2.5 text-[11px] font-bold text-slate-950 hover:bg-sky-400"
              >
                BUY
              </button>
              <button
                type="button"
                onClick={() => onQuickOrder({ symbol: activeSymbol, exchange, side: 'SELL', ltp: activeBar?.close })}
                className="min-h-[28px] rounded bg-red-500 px-2.5 text-[11px] font-bold text-white hover:bg-red-400"
              >
                SELL
              </button>
            </div>
          )}

          {onTimeframeChange && (
            <select
              value={timeframe}
              onChange={(e) => onTimeframeChange(e.target.value)}
              className="min-h-[28px] rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-200 focus:border-sky-400 focus:outline-none"
            >
              {CHART_TFS.map((t) => (
                <option key={t.id} value={t.id}>{t.label}</option>
              ))}
            </select>
          )}

          <button
            type="button"
            onClick={() => setShowEma20((v) => !v)}
            className={`min-h-[28px] rounded-md border px-2 font-mono text-[11px] font-semibold ${
              showEma20 ? 'border-sky-500/70 bg-sky-500/15 text-sky-300' : 'border-slate-700 text-slate-400'
            }`}
          >
            EMA20
          </button>
          <button
            type="button"
            onClick={() => setShowEma50((v) => !v)}
            className={`min-h-[28px] rounded-md border px-2 font-mono text-[11px] font-semibold ${
              showEma50 ? 'border-amber-500/70 bg-amber-500/15 text-amber-300' : 'border-slate-700 text-slate-400'
            }`}
          >
            EMA50
          </button>
          <button
            type="button"
            onClick={() => void fetchBars(activeSymbol, timeframe, true)}
            disabled={loading}
            title="Refresh candles"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-slate-700 text-slate-400 hover:border-sky-400 hover:text-slate-100"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Candlestick Canvas */}
      <div className="relative bg-[#0b1120]" style={{ minHeight: height }}>
        {loading && bars.length === 0 ? (
          <div className="flex h-full min-h-[320px] items-center justify-center text-xs text-slate-400">
            Loading {activeSymbol} ({timeframe}) candlesticks...
          </div>
        ) : error && bars.length === 0 ? (
          <div className="flex h-full min-h-[320px] flex-col items-center justify-center gap-2 p-6 text-center text-xs text-slate-400">
            <TrendingUp className="h-6 w-6 text-slate-600" />
            <p>{error}</p>
            <button
              type="button"
              onClick={() => void fetchBars(activeSymbol, timeframe, true)}
              className="rounded-md border border-slate-700 px-3 py-1.5 font-semibold text-sky-300 hover:border-sky-400"
            >
              Fetch Candles
            </button>
          </div>
        ) : bars.length === 0 ? (
          <div className="flex h-full min-h-[320px] items-center justify-center text-xs text-slate-500">
            Select an instrument to render candlestick chart.
          </div>
        ) : (
          <svg
            viewBox={`0 0 ${svgW} ${svgH}`}
            className="block w-full select-none"
            onMouseLeave={() => setHoverIdx(null)}
            onMouseMove={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              const relX = ((e.clientX - rect.left) / rect.width) * svgW;
              const idx = Math.floor((relX - padL) / stepX);
              if (idx >= 0 && idx < bars.length) setHoverIdx(idx);
            }}
          >
            {/* Horizontal grid & right Y-axis */}
            {priceTicks.map((pt, i) => {
              const y = yForPrice(pt);
              return (
                <g key={i}>
                  <line x1={padL} y1={y} x2={svgW - padR} y2={y} stroke="#1e293b" strokeWidth="1" />
                  <text
                    x={svgW - padR + 6}
                    y={y + 3}
                    fill="#64748b"
                    fontSize="10"
                    fontFamily="IBM Plex Mono, monospace"
                  >
                    {pt.toFixed(1)}
                  </text>
                </g>
              );
            })}

            {/* Volume bars */}
            {bars.map((b, i) => {
              const x = padL + i * stepX + (stepX - candleW) / 2;
              const vh = (b.volume / Math.max(1, maxVol)) * volH;
              const y = svgH - padB - vh;
              const up = b.close >= b.open;
              return (
                <rect
                  key={`v-${i}`}
                  x={x}
                  y={y}
                  width={candleW}
                  height={Math.max(1, vh)}
                  fill={up ? 'rgba(16, 185, 129, 0.25)' : 'rgba(239, 68, 68, 0.25)'}
                />
              );
            })}

            {/* Candlesticks */}
            {bars.map((b, i) => {
              const cx = padL + i * stepX + stepX / 2;
              const x = cx - candleW / 2;
              const yHigh = yForPrice(b.high);
              const yLow = yForPrice(b.low);
              const yOpen = yForPrice(b.open);
              const yClose = yForPrice(b.close);
              const bodyTop = Math.min(yOpen, yClose);
              const bodyH = Math.max(1.5, Math.abs(yClose - yOpen));
              const up = b.close >= b.open;
              const color = up ? '#10b981' : '#ef4444';
              const barMarkers = markersByBar.get(i);
              return (
                <g key={`c-${i}`}>
                  <line x1={cx} y1={yHigh} x2={cx} y2={yLow} stroke={color} strokeWidth="1.1" />
                  <rect x={x} y={bodyTop} width={candleW} height={bodyH} fill={color} rx="0.5" />
                  {barMarkers?.map((m, mi) => {
                    const my = m.kind === 'EN' ? yLow + 12 + mi * 12 : yHigh - 8 - mi * 12;
                    const fill = m.kind === 'EN' ? '#38bdf8' : m.kind === 'TP' ? '#10b981' : '#ef4444';
                    return (
                      <text
                        key={`m-${mi}`}
                        x={cx}
                        y={Math.max(12, Math.min(padT + priceH - 4, my))}
                        textAnchor="middle"
                        fill={fill}
                        fontSize="9"
                        fontWeight="700"
                        fontFamily="IBM Plex Mono, monospace"
                      >
                        {m.kind}
                      </text>
                    );
                  })}
                </g>
              );
            })}

            {/* EMA Overlays */}
            {showEma20 && ema20.length > 0 && (
              <path d={buildLinePath(ema20)} fill="none" stroke="#38bdf8" strokeWidth="1.4" opacity="0.9" />
            )}
            {showEma50 && ema50.length > 0 && (
              <path d={buildLinePath(ema50)} fill="none" stroke="#f59e0b" strokeWidth="1.4" opacity="0.85" />
            )}

            {/* Latest close horizontal dashed guide */}
            {bars.length > 0 && (
              <g>
                <line
                  x1={padL}
                  y1={yForPrice(bars[bars.length - 1].close)}
                  x2={svgW - padR}
                  y2={yForPrice(bars[bars.length - 1].close)}
                  stroke="#38bdf8"
                  strokeDasharray="3 3"
                  strokeWidth="1"
                  opacity="0.6"
                />
                <rect
                  x={svgW - padR + 2}
                  y={yForPrice(bars[bars.length - 1].close) - 9}
                  width={58}
                  height={18}
                  rx="3"
                  fill="#0284c7"
                />
                <text
                  x={svgW - padR + 6}
                  y={yForPrice(bars[bars.length - 1].close) + 3}
                  fill="#f8fafc"
                  fontSize="10"
                  fontWeight="600"
                  fontFamily="IBM Plex Mono, monospace"
                >
                  {bars[bars.length - 1].close.toFixed(1)}
                </text>
              </g>
            )}

            {/* Hover crosshair */}
            {hoverIdx !== null && bars[hoverIdx] && (
              <g>
                <line
                  x1={padL + hoverIdx * stepX + stepX / 2}
                  y1={padT}
                  x2={padL + hoverIdx * stepX + stepX / 2}
                  y2={svgH - padB}
                  stroke="#64748b"
                  strokeDasharray="3 3"
                  strokeWidth="1"
                />
                <rect
                  x={Math.min(svgW - padR - 110, Math.max(padL, padL + hoverIdx * stepX - 50))}
                  y={svgH - padB + 4}
                  width={106}
                  height={18}
                  rx="3"
                  fill="#1e293b"
                />
                <text
                  x={Math.min(svgW - padR - 57, Math.max(padL + 53, padL + hoverIdx * stepX + 3))}
                  y={svgH - padB + 16}
                  textAnchor="middle"
                  fill="#e2e8f0"
                  fontSize="9.5"
                  fontFamily="IBM Plex Mono, monospace"
                >
                  {(bars[hoverIdx].ts_ist || bars[hoverIdx].trade_date).slice(0, 16).replace('T', ' ')}
                </text>
              </g>
            )}

            {/* Bottom X-axis labels */}
            {bars.map((b, i) => {
              const stride = Math.max(1, Math.floor(bars.length / 6));
              if (i % stride !== 0 && i !== bars.length - 1) return null;
              const x = padL + i * stepX + stepX / 2;
              return (
                <text
                  key={`x-${i}`}
                  x={x}
                  y={svgH - 8}
                  textAnchor="middle"
                  fill="#64748b"
                  fontSize="9.5"
                  fontFamily="IBM Plex Mono, monospace"
                >
                  {(b.trade_date || b.ts_ist.slice(0, 10)).slice(5)}
                </text>
              );
            })}
          </svg>
        )}
      </div>
    </div>
  );
}
