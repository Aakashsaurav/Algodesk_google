import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import {
  ChevronLeft,
  ChevronRight,
  Maximize2,
  RefreshCw,
  RotateCcw,
  TrendingUp,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
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
  fromDate?: string;
  toDate?: string;
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

const MAX_LOADED_BARS = 3000;
const MIN_VISIBLE_BARS = 10;
const DEFAULT_VISIBLE_BARS = 100;

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

function normalizeTs(value: string): string {
  return (value || '').trim().replace('T', ' ').slice(0, 16);
}

export function CandlestickChartPanel({
  symbols,
  activeSymbol,
  onSelectSymbol,
  exchange = 'NSE',
  timeframe,
  onTimeframeChange,
  trades = [],
  fromDate,
  toDate,
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
  const [hoverPriceY, setHoverPriceY] = useState<number | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [viewRange, setViewRange] = useState<{ start: number; end: number }>({ start: 0, end: 0 });

  const clipPathId = `candle-plot-${useId().replace(/:/g, '')}`;
  const chartWrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const viewRangeRef = useRef(viewRange);
  const barsLenRef = useRef(0);
  const wheelPanAccumRef = useRef(0);
  const dragRef = useRef<{
    pointerId: number;
    startClientX: number;
    startViewStart: number;
    startViewEnd: number;
    rectWidth: number;
  } | null>(null);

  viewRangeRef.current = viewRange;
  barsLenRef.current = bars.length;

  // SVG Layout Constants
  const svgW = 760;
  const svgH = height;
  const padL = 14;
  const padR = 66;
  const padT = 16;
  const padB = 26;
  const volH = 56;
  const priceH = svgH - padT - padB - volH - 10;
  const plotW = svgW - padL - padR;

  const computeDefaultRange = useCallback(
    (total: number) => {
      if (total <= 0) return { start: 0, end: 0 };
      if (total <= 45) return { start: 0, end: total };
      // Start with a comfortable ~70-85 bar window so pan/drag and zoom in/out both work immediately
      const span = Math.min(total - Math.min(12, Math.floor(total * 0.2)), DEFAULT_VISIBLE_BARS);
      const effSpan = Math.max(MIN_VISIBLE_BARS, Math.min(total, span));
      return { start: Math.max(0, total - effSpan), end: total };
    },
    []
  );

  const fetchBars = useCallback(
    async (sym: string, tf: string, forceDownload = false) => {
      const clean = sym.trim().toUpperCase();
      if (!clean) return;
      setLoading(true);
      setError('');
      try {
        // Load extra warmup/context bars before fromDate so EMA20/EMA50 and panning both have full history
        let paddedFrom = fromDate;
        if (fromDate && /^\d{4}-\d{2}-\d{2}$/.test(fromDate)) {
          const d = new Date(`${fromDate}T00:00:00Z`);
          if (!Number.isNaN(d.getTime())) {
            const padDays = tf.includes('minute') ? 10 : 90;
            d.setUTCDate(d.getUTCDate() - padDays);
            paddedFrom = d.toISOString().slice(0, 10);
          }
        }
        const params = new URLSearchParams({
          symbol: clean,
          exchange,
          timeframe: tf,
          view: 'full',
          limit: String(MAX_LOADED_BARS),
        });
        if (paddedFrom) params.set('from_date', paddedFrom);
        if (toDate) params.set('to_date', toDate);

        if (!forceDownload) {
          const recRes = await apiFetch(`/api/data/records?${params.toString()}`);
          if (recRes.ok) {
            const recBody = await recRes.json();
            if (Array.isArray(recBody.rows) && recBody.rows.length >= 5) {
              const loaded: OhlcvBar[] =
                recBody.rows.length > MAX_LOADED_BARS
                  ? recBody.rows.slice(-MAX_LOADED_BARS)
                  : recBody.rows;
              setBars(loaded);
              setViewRange(computeDefaultRange(loaded.length));
              setHoverIdx(null);
              setLoading(false);
              return;
            }
          }
        }

        // Gap-only download if not yet cached locally
        const toIso = toDate || new Date().toISOString().slice(0, 10);
        const lookbackMs = tf.includes('minute') ? 35 * 86400_000 : 180 * 86400_000;
        const fromIso = fromDate || new Date(Date.now() - lookbackMs).toISOString().slice(0, 10);
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

        const recRes2 = await apiFetch(`/api/data/records?${params.toString()}`);
        const recBody2 = await recRes2.json().catch(() => ({}));
        const loaded2: OhlcvBar[] = Array.isArray(recBody2.rows)
          ? recBody2.rows.length > MAX_LOADED_BARS
            ? recBody2.rows.slice(-MAX_LOADED_BARS)
            : recBody2.rows
          : [];
        setBars(loaded2);
        setViewRange(computeDefaultRange(loaded2.length));
        setHoverIdx(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load chart candles.');
        setBars([]);
        setViewRange({ start: 0, end: 0 });
      } finally {
        setLoading(false);
      }
    },
    [exchange, fromDate, toDate, computeDefaultRange]
  );

  useEffect(() => {
    if (activeSymbol) {
      void fetchBars(activeSymbol, timeframe, false);
    }
  }, [activeSymbol, timeframe, fetchBars]);

  // Clamp viewport whenever bar count changes
  const clampedStart = Math.max(0, Math.min(viewRange.start, Math.max(0, bars.length - 1)));
  const clampedEnd = Math.max(clampedStart + Math.min(bars.length, 1), Math.min(viewRange.end, bars.length));
  const visibleBars = useMemo(
    () => (bars.length > 0 ? bars.slice(clampedStart, clampedEnd) : []),
    [bars, clampedStart, clampedEnd]
  );

  const zoomAt = useCallback((scaleFactor: number, anchorRatio = 0.5) => {
    const total = barsLenRef.current;
    if (total <= 1) return;
    const minSpan = Math.min(total, MIN_VISIBLE_BARS);
    setViewRange((prev) => {
      const curStart = Math.max(0, Math.min(prev.start, total - 1));
      const curEnd = Math.max(curStart + 1, Math.min(prev.end, total));
      const curSpan = curEnd - curStart;
      let nextSpan = Math.round(curSpan * scaleFactor);
      if (scaleFactor < 1 && nextSpan >= curSpan) nextSpan = curSpan - 2;
      if (scaleFactor > 1 && nextSpan <= curSpan) nextSpan = curSpan + 2;
      nextSpan = Math.max(minSpan, Math.min(total, nextSpan));
      if (nextSpan === curSpan) return prev;
      const clampedAnchor = Math.max(0, Math.min(1, anchorRatio));
      const anchorBar = curStart + clampedAnchor * curSpan;
      let nextStart = Math.round(anchorBar - clampedAnchor * nextSpan);
      nextStart = Math.max(0, Math.min(total - nextSpan, nextStart));
      return { start: nextStart, end: nextStart + nextSpan };
    });
  }, []);

  const panByBars = useCallback((deltaBars: number) => {
    const total = barsLenRef.current;
    if (total <= 1 || deltaBars === 0) return;
    setViewRange((prev) => {
      const curStart = Math.max(0, Math.min(prev.start, total - 1));
      const curEnd = Math.max(curStart + 1, Math.min(prev.end, total));
      const span = curEnd - curStart;
      const nextStart = Math.max(0, Math.min(total - span, curStart + deltaBars));
      if (nextStart === curStart) return prev;
      return { start: nextStart, end: nextStart + span };
    });
  }, []);

  const fitAll = useCallback(() => {
    const total = barsLenRef.current;
    if (total <= 0) return;
    setViewRange({ start: 0, end: total });
  }, []);

  const resetView = useCallback(() => {
    const total = barsLenRef.current;
    setViewRange(computeDefaultRange(total));
  }, [computeDefaultRange]);

  // Attach non-passive wheel listener so mouse wheel / trackpad zoom doesn't scroll the page
  useEffect(() => {
    const el = chartWrapRef.current;
    if (!el) return;
    const handleWheel = (e: WheelEvent) => {
      if (barsLenRef.current <= 1) return;
      e.preventDefault();

      const svgEl = svgRef.current;
      const rect = svgEl ? svgEl.getBoundingClientRect() : el.getBoundingClientRect();
      const relSvgX = rect.width > 0 ? ((e.clientX - rect.left) / rect.width) * svgW : svgW / 2;
      const anchorRatio = Math.max(0, Math.min(1, (relSvgX - padL) / plotW));

      // Horizontal trackpad scroll -> pan
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey && !e.metaKey) {
        const { start, end } = viewRangeRef.current;
        const span = Math.max(1, end - start);
        const barsPerPx = span / Math.max(120, rect.width * (plotW / svgW));
        wheelPanAccumRef.current += e.deltaX * barsPerPx;
        const whole = wheelPanAccumRef.current > 0
          ? Math.floor(wheelPanAccumRef.current)
          : Math.ceil(wheelPanAccumRef.current);
        if (whole !== 0) {
          wheelPanAccumRef.current -= whole;
          panByBars(whole);
        }
        return;
      }

      // Vertical scroll or trackpad pinch -> zoom anchored at cursor
      const rawDelta = e.deltaY;
      if (rawDelta === 0) return;
      const intensity = Math.min(1, Math.max(0.25, Math.abs(rawDelta) / 100));
      const factor = rawDelta < 0 ? 1 - 0.18 * intensity : 1 + 0.22 * intensity;
      zoomAt(factor, anchorRatio);
    };

    el.addEventListener('wheel', handleWheel, { passive: false });
    return () => el.removeEventListener('wheel', handleWheel);
  }, [panByBars, plotW, svgW, zoomAt]);

  // Compute EMAs on FULL history so values never shift while zooming/panning
  const ema20Full = useMemo(() => (showEma20 ? computeEma(bars, 20) : []), [bars, showEma20]);
  const ema50Full = useMemo(() => (showEma50 ? computeEma(bars, 50) : []), [bars, showEma50]);
  const ema20Visible = useMemo(
    () => (showEma20 ? ema20Full.slice(clampedStart, clampedEnd) : []),
    [ema20Full, showEma20, clampedStart, clampedEnd]
  );
  const ema50Visible = useMemo(
    () => (showEma50 ? ema50Full.slice(clampedStart, clampedEnd) : []),
    [ema50Full, showEma50, clampedStart, clampedEnd]
  );

  const activeFullIdx =
    hoverIdx !== null && visibleBars[hoverIdx]
      ? clampedStart + hoverIdx
      : bars.length > 0
        ? clampedEnd - 1
        : -1;
  const activeBar = activeFullIdx >= 0 ? bars[activeFullIdx] : null;
  const prevBar =
    activeFullIdx > 0 ? bars[activeFullIdx - 1] : activeBar;
  const chg = activeBar && prevBar ? activeBar.close - prevBar.close : 0;
  const chgPct = activeBar && prevBar && prevBar.close > 0 ? (chg / prevBar.close) * 100 : 0;

  // Map trades to full bar indices for EN / TP / SL / EX markers
  const markersByBar = useMemo(() => {
    const map = new Map<number, Array<{ kind: 'EN' | 'TP' | 'SL' | 'EX'; price: number }>>();
    if (!trades.length || !bars.length) return map;
    const symTrades = trades.filter(
      (t) => !t.symbol || t.symbol.toUpperCase() === activeSymbol.toUpperCase()
    );
    const isIntraday = timeframe.includes('minute');
    const findIdx = (dt: string) => {
      if (!dt) return -1;
      const norm = normalizeTs(dt);
      if (isIntraday && norm.length > 10) {
        const idx = bars.findIndex((b) => normalizeTs(b.ts_ist || b.trade_date) >= norm);
        if (idx >= 0) return idx;
      }
      const targetDate = dt.slice(0, 10);
      return bars.findIndex((b) => (b.trade_date || b.ts_ist.slice(0, 10)) >= targetDate);
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
        const k =
          reason.includes('TARGET') || reason.includes('TP')
            ? 'TP'
            : reason.includes('STOP') || reason.includes('SL')
              ? 'SL'
              : 'EX';
        const arr = map.get(xIdx) || [];
        arr.push({ kind: k, price: tr.exit_price });
        map.set(xIdx, arr);
      }
    }
    return map;
  }, [trades, bars, activeSymbol, timeframe]);

  // Dynamic Y-axis auto-scale based on visible bars
  const { minP, maxP, maxVol } = useMemo(() => {
    if (!visibleBars.length) return { minP: 0, maxP: 100, maxVol: 1 };
    let lo = Infinity;
    let hi = -Infinity;
    let mv = 1;
    for (const b of visibleBars) {
      if (b.low < lo) lo = b.low;
      if (b.high > hi) hi = b.high;
      if (b.volume > mv) mv = b.volume;
    }
    const pad = (hi - lo) * 0.1 || 1;
    return { minP: lo - pad, maxP: hi + pad, maxVol: mv };
  }, [visibleBars]);

  const stepX = visibleBars.length > 0 ? plotW / Math.max(visibleBars.length, 1) : 10;
  const candleW = Math.max(1.5, Math.min(18, stepX * 0.68));

  const yForPrice = (p: number) =>
    padT + ((maxP - p) / Math.max(0.0001, maxP - minP)) * priceH;

  const priceForY = (y: number) =>
    maxP - ((y - padT) / Math.max(1, priceH)) * (maxP - minP);

  const buildLinePath = (series: Array<number | null>) => {
    const pts: string[] = [];
    let drawing = false;
    series.forEach((v, i) => {
      if (v === null) {
        drawing = false;
        return;
      }
      const x = padL + i * stepX + stepX / 2;
      const y = yForPrice(v);
      pts.push(`${drawing ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`);
      drawing = true;
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

  const spansMultiYear = useMemo(() => {
    if (visibleBars.length < 2) return false;
    const firstY = (visibleBars[0].trade_date || visibleBars[0].ts_ist).slice(0, 4);
    const lastY = (
      visibleBars[visibleBars.length - 1].trade_date ||
      visibleBars[visibleBars.length - 1].ts_ist
    ).slice(0, 4);
    return firstY !== lastY;
  }, [visibleBars]);

  const formatXLabel = (b: OhlcvBar) => {
    const isIntraday = timeframe.includes('minute');
    if (isIntraday && visibleBars.length <= 80 && b.ts_ist) {
      return b.ts_ist.replace('T', ' ').slice(5, 16);
    }
    const d = b.trade_date || b.ts_ist.slice(0, 10);
    return spansMultiYear ? d.slice(2) : d.slice(5);
  };

  const visibleCount = visibleBars.length;
  const minSpan = Math.min(bars.length, MIN_VISIBLE_BARS);
  const canPanLeft = clampedStart > 0;
  const canPanRight = clampedEnd < bars.length;
  const canZoomIn = visibleCount > minSpan;
  const canZoomOut = visibleCount < bars.length;
  const latestBar = bars.length > 0 ? bars[bars.length - 1] : null;
  const latestInViewPriceRange =
    latestBar !== null && latestBar.close >= minP && latestBar.close <= maxP;

  const panStep = Math.max(3, Math.round(visibleCount * 0.25));

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

        <div className="flex flex-wrap items-center gap-1.5">
          {onQuickOrder && activeSymbol && (
            <div className="mr-1 flex items-center gap-1">
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

          {/* Pan & Zoom controls */}
          {bars.length > 0 && (
            <div className="flex items-center rounded-md border border-slate-700 bg-[#0b1120] p-0.5">
              <button
                type="button"
                onClick={() => panByBars(-panStep)}
                disabled={!canPanLeft}
                title="Pan left (older bars)"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => panByBars(panStep)}
                disabled={!canPanRight}
                title="Pan right (newer bars)"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
              <span className="mx-0.5 h-3.5 w-px bg-slate-700" />
              <button
                type="button"
                onClick={() => zoomAt(0.75, 0.5)}
                disabled={!canZoomIn}
                title="Zoom in (or scroll up)"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ZoomIn className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => zoomAt(1.33, 0.5)}
                disabled={!canZoomOut}
                title="Zoom out (or scroll down)"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ZoomOut className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={fitAll}
                disabled={!canZoomOut}
                title="Fit all loaded bars"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <Maximize2 className="h-3 w-3" />
              </button>
              <button
                type="button"
                onClick={resetView}
                title="Reset chart zoom (or double-click chart)"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white"
              >
                <RotateCcw className="h-3 w-3" />
              </button>
            </div>
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
      <div
        ref={chartWrapRef}
        className="relative bg-[#0b1120]"
        style={{ minHeight: height }}
      >
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
          <>
            <svg
              ref={svgRef}
              viewBox={`0 0 ${svgW} ${svgH}`}
              className={`block w-full select-none touch-none ${
                isDragging ? 'cursor-grabbing' : 'cursor-grab'
              }`}
              onDoubleClick={resetView}
              onPointerDown={(e) => {
                if (e.button !== 0 && e.pointerType === 'mouse') return;
                const rect = e.currentTarget.getBoundingClientRect();
                e.currentTarget.setPointerCapture(e.pointerId);
                dragRef.current = {
                  pointerId: e.pointerId,
                  startClientX: e.clientX,
                  startViewStart: clampedStart,
                  startViewEnd: clampedEnd,
                  rectWidth: Math.max(1, rect.width),
                };
                setIsDragging(true);
                setHoverIdx(null);
                setHoverPriceY(null);
              }}
              onPointerMove={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                if (dragRef.current) {
                  const d = dragRef.current;
                  const dxPx = e.clientX - d.startClientX;
                  const dxSvg = (dxPx / d.rectWidth) * svgW;
                  const span = Math.max(1, d.startViewEnd - d.startViewStart);
                  const barStepSvg = plotW / span;
                  const barDelta = Math.round(-dxSvg / Math.max(0.5, barStepSvg));
                  const nextStart = Math.max(
                    0,
                    Math.min(bars.length - span, d.startViewStart + barDelta)
                  );
                  setViewRange((prev) =>
                    prev.start === nextStart && prev.end === nextStart + span
                      ? prev
                      : { start: nextStart, end: nextStart + span }
                  );
                  return;
                }
                const relX = ((e.clientX - rect.left) / Math.max(1, rect.width)) * svgW;
                const relY = ((e.clientY - rect.top) / Math.max(1, rect.height)) * svgH;
                const idx = Math.floor((relX - padL) / stepX);
                if (idx >= 0 && idx < visibleBars.length) {
                  setHoverIdx(idx);
                } else {
                  setHoverIdx(null);
                }
                if (relY >= padT && relY <= padT + priceH) {
                  setHoverPriceY(relY);
                } else {
                  setHoverPriceY(null);
                }
              }}
              onPointerUp={(e) => {
                if (dragRef.current && dragRef.current.pointerId === e.pointerId) {
                  try {
                    e.currentTarget.releasePointerCapture(e.pointerId);
                  } catch {
                    // ignore if capture already released
                  }
                  dragRef.current = null;
                  setIsDragging(false);
                }
              }}
              onPointerCancel={() => {
                dragRef.current = null;
                setIsDragging(false);
              }}
              onPointerLeave={() => {
                if (!dragRef.current) {
                  setHoverIdx(null);
                  setHoverPriceY(null);
                }
              }}
            >
              <defs>
                <clipPath id={clipPathId}>
                  <rect x={padL} y={padT} width={plotW} height={priceH} />
                </clipPath>
              </defs>

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
              {visibleBars.map((b, i) => {
                const x = padL + i * stepX + (stepX - candleW) / 2;
                const vh = (b.volume / Math.max(1, maxVol)) * volH;
                const y = svgH - padB - vh;
                const up = b.close >= b.open;
                return (
                  <rect
                    key={`v-${clampedStart + i}`}
                    x={x}
                    y={y}
                    width={candleW}
                    height={Math.max(1, vh)}
                    fill={up ? 'rgba(16, 185, 129, 0.25)' : 'rgba(239, 68, 68, 0.25)'}
                  />
                );
              })}

              {/* Candlesticks & Trade Markers */}
              <g clipPath={`url(#${clipPathId})`}>
                {visibleBars.map((b, i) => {
                  const fullIdx = clampedStart + i;
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
                  const barMarkers = markersByBar.get(fullIdx);
                  return (
                    <g key={`c-${fullIdx}`}>
                      <line x1={cx} y1={yHigh} x2={cx} y2={yLow} stroke={color} strokeWidth="1.1" />
                      <rect x={x} y={bodyTop} width={candleW} height={bodyH} fill={color} rx="0.5" />
                      {barMarkers?.map((m, mi) => {
                        const my = m.kind === 'EN' ? yLow + 12 + mi * 12 : yHigh - 8 - mi * 12;
                        const fill =
                          m.kind === 'EN' ? '#38bdf8' : m.kind === 'TP' ? '#10b981' : '#ef4444';
                        return (
                          <text
                            key={`m-${mi}`}
                            x={cx}
                            y={Math.max(padT + 10, Math.min(padT + priceH - 4, my))}
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
                {showEma20 && ema20Visible.length > 0 && (
                  <path
                    d={buildLinePath(ema20Visible)}
                    fill="none"
                    stroke="#38bdf8"
                    strokeWidth="1.4"
                    opacity="0.9"
                  />
                )}
                {showEma50 && ema50Visible.length > 0 && (
                  <path
                    d={buildLinePath(ema50Visible)}
                    fill="none"
                    stroke="#f59e0b"
                    strokeWidth="1.4"
                    opacity="0.85"
                  />
                )}
              </g>

              {/* Latest close horizontal dashed guide (only if in visible price range) */}
              {latestBar && latestInViewPriceRange && (
                <g>
                  <line
                    x1={padL}
                    y1={yForPrice(latestBar.close)}
                    x2={svgW - padR}
                    y2={yForPrice(latestBar.close)}
                    stroke="#38bdf8"
                    strokeDasharray="3 3"
                    strokeWidth="1"
                    opacity="0.6"
                  />
                  <rect
                    x={svgW - padR + 2}
                    y={yForPrice(latestBar.close) - 9}
                    width={60}
                    height={18}
                    rx="3"
                    fill="#0284c7"
                  />
                  <text
                    x={svgW - padR + 6}
                    y={yForPrice(latestBar.close) + 3}
                    fill="#f8fafc"
                    fontSize="10"
                    fontWeight="600"
                    fontFamily="IBM Plex Mono, monospace"
                  >
                    {latestBar.close.toFixed(1)}
                  </text>
                </g>
              )}

              {/* Hover crosshair (vertical + horizontal price guide) */}
              {!isDragging && hoverIdx !== null && visibleBars[hoverIdx] && (
                <g pointerEvents="none">
                  <line
                    x1={padL + hoverIdx * stepX + stepX / 2}
                    y1={padT}
                    x2={padL + hoverIdx * stepX + stepX / 2}
                    y2={svgH - padB}
                    stroke="#64748b"
                    strokeDasharray="3 3"
                    strokeWidth="1"
                  />
                  {hoverPriceY !== null && (
                    <>
                      <line
                        x1={padL}
                        y1={hoverPriceY}
                        x2={svgW - padR}
                        y2={hoverPriceY}
                        stroke="#64748b"
                        strokeDasharray="3 3"
                        strokeWidth="1"
                      />
                      <rect
                        x={svgW - padR + 2}
                        y={hoverPriceY - 9}
                        width={60}
                        height={18}
                        rx="3"
                        fill="#1e293b"
                        stroke="#475569"
                        strokeWidth="0.7"
                      />
                      <text
                        x={svgW - padR + 6}
                        y={hoverPriceY + 3}
                        fill="#e2e8f0"
                        fontSize="9.5"
                        fontFamily="IBM Plex Mono, monospace"
                      >
                        {priceForY(hoverPriceY).toFixed(1)}
                      </text>
                    </>
                  )}
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
                    {(visibleBars[hoverIdx].ts_ist || visibleBars[hoverIdx].trade_date)
                      .slice(0, 16)
                      .replace('T', ' ')}
                  </text>
                </g>
              )}

              {/* Bottom X-axis labels */}
              {visibleBars.map((b, i) => {
                const stride = Math.max(1, Math.floor(visibleBars.length / 6));
                if (i % stride !== 0 && i !== visibleBars.length - 1) return null;
                const x = padL + i * stepX + stepX / 2;
                return (
                  <text
                    key={`x-${clampedStart + i}`}
                    x={x}
                    y={svgH - 8}
                    textAnchor="middle"
                    fill="#64748b"
                    fontSize="9.5"
                    fontFamily="IBM Plex Mono, monospace"
                  >
                    {formatXLabel(b)}
                  </text>
                );
              })}
            </svg>

            {/* Interactive Timeline Scrubber / Pan Bar */}
            {bars.length > MIN_VISIBLE_BARS && (
              <div className="border-t border-slate-800/80 bg-[#090f1d] px-3 py-2">
                <div
                  role="slider"
                  aria-label="Chart timeline navigator"
                  aria-valuemin={1}
                  aria-valuemax={bars.length}
                  aria-valuenow={clampedStart + 1}
                  tabIndex={0}
                  onPointerDown={(e) => {
                    const track = e.currentTarget;
                    const rect = track.getBoundingClientRect();
                    const span = Math.max(1, clampedEnd - clampedStart);
                    const total = bars.length;
                    const jumpToClientX = (clientX: number) => {
                      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
                      const centerIdx = Math.round(frac * total);
                      const nextStart = Math.max(0, Math.min(total - span, centerIdx - Math.floor(span / 2)));
                      setViewRange({ start: nextStart, end: nextStart + span });
                    };
                    jumpToClientX(e.clientX);
                    const onMove = (ev: PointerEvent) => jumpToClientX(ev.clientX);
                    const onUp = () => {
                      window.removeEventListener('pointermove', onMove);
                      window.removeEventListener('pointerup', onUp);
                    };
                    window.addEventListener('pointermove', onMove);
                    window.addEventListener('pointerup', onUp);
                  }}
                  className="relative h-4 w-full cursor-pointer select-none rounded bg-slate-900/90 ring-1 ring-inset ring-slate-800"
                >
                  <div
                    className="absolute top-0.5 bottom-0.5 rounded bg-sky-500/35 ring-1 ring-sky-400/70 transition-[left,width] duration-75"
                    style={{
                      left: `${(clampedStart / Math.max(1, bars.length)) * 100}%`,
                      width: `${Math.max(4, ((clampedEnd - clampedStart) / Math.max(1, bars.length)) * 100)}%`,
                    }}
                  />
                </div>
              </div>
            )}

            {/* Compact interactive status footer */}
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800/80 bg-[#0b1120] px-3 py-1.5 font-mono text-[10px] text-slate-500">
              <span>
                Bars {clampedStart + 1}–{clampedEnd} of {bars.length}
                {visibleBars.length > 0 && (
                  <>
                    {' · '}
                    {(visibleBars[0].trade_date || visibleBars[0].ts_ist.slice(0, 10))} →{' '}
                    {(visibleBars[visibleBars.length - 1].trade_date ||
                      visibleBars[visibleBars.length - 1].ts_ist.slice(0, 10))}
                  </>
                )}
              </span>
              <span className="font-sans">
                Scroll to zoom · Drag chart or timeline bar to pan · Double-click to reset
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
