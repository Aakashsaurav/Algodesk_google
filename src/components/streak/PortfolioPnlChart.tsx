import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  Calendar,
  ChevronLeft,
  ChevronRight,
  Maximize2,
  RotateCcw,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';

export interface PortfolioCurvePoint {
  date: string;
  pnl: number;
  equity?: number;
  drawdownPct?: number;
  openPositions?: number;
  symbol?: string;
  tradePnl?: number;
}

interface PortfolioPnlChartProps {
  title: string;
  initialCapital: number;
  fromDate?: string;
  toDate?: string;
  includeCharges: boolean;
  isCombined: boolean;
  equityCurve?: Array<{
    date: string;
    equity: number;
    drawdown_pct: number;
    open_positions?: number;
  }>;
  tradeSeries: Array<{
    label: string;
    pnl: number;
    symbol?: string;
    tradePnl?: number;
  }>;
  totalPnl: number;
}

const MIN_POINTS = 5;

function formatShortDate(raw: string, spansMultiYear: boolean): string {
  const s = (raw || '').trim();
  if (!s || s === 'Start') return s;
  const datePart = s.slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(datePart)) {
    const [y, m, d] = datePart.split('-');
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const mon = months[Math.max(0, Math.min(11, Number(m) - 1))] || m;
    return spansMultiYear ? `${d} ${mon} '${y.slice(2)}` : `${d} ${mon}`;
  }
  return s.slice(0, 10);
}

export function PortfolioPnlChart({
  title,
  initialCapital,
  fromDate,
  toDate,
  includeCharges,
  isCombined,
  equityCurve = [],
  tradeSeries,
  totalPnl,
}: PortfolioPnlChartProps) {
  const hasDailyCurve = isCombined && includeCharges && equityCurve.length >= 2;
  const [seriesMode, setSeriesMode] = useState<'daily' | 'trades'>(hasDailyCurve ? 'daily' : 'trades');

  // Switch cleanly if equityCurve availability changes (e.g. toggling Gross/Net or selecting a single symbol)
  useEffect(() => {
    if (!hasDailyCurve && seriesMode === 'daily') {
      setSeriesMode('trades');
    } else if (hasDailyCurve && isCombined) {
      setSeriesMode('daily');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasDailyCurve, isCombined]);

  const points: PortfolioCurvePoint[] = useMemo(() => {
    if (seriesMode === 'daily' && hasDailyCurve) {
      return equityCurve.map((pt) => ({
        date: pt.date,
        equity: pt.equity,
        pnl: pt.equity - initialCapital,
        drawdownPct: pt.drawdown_pct,
        openPositions: pt.open_positions,
      }));
    }
    return tradeSeries.map((pt, idx) => ({
      date: idx === 0 && pt.label === 'Start' ? fromDate || 'Start' : pt.label,
      pnl: pt.pnl,
      equity: initialCapital + pt.pnl,
      symbol: pt.symbol,
      tradePnl: pt.tradePnl,
    }));
  }, [seriesMode, hasDailyCurve, equityCurve, initialCapital, tradeSeries, fromDate]);

  const [viewRange, setViewRange] = useState<{ start: number; end: number }>({
    start: 0,
    end: points.length,
  });
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [isDragging, setIsDragging] = useState(false);

  const clipAboveId = `pnl-above-${useId().replace(/:/g, '')}`;
  const clipBelowId = `pnl-below-${useId().replace(/:/g, '')}`;
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const ptsLenRef = useRef(points.length);
  const viewRangeRef = useRef(viewRange);
  const dragRef = useRef<{
    pointerId: number;
    startClientX: number;
    startViewStart: number;
    startViewEnd: number;
    rectWidth: number;
  } | null>(null);

  ptsLenRef.current = points.length;
  viewRangeRef.current = viewRange;

  // Reset viewport when dataset switches
  useEffect(() => {
    setViewRange({ start: 0, end: points.length });
    setHoverIdx(null);
  }, [points.length, seriesMode, isCombined]);

  const clampedStart = Math.max(0, Math.min(viewRange.start, Math.max(0, points.length - 1)));
  const clampedEnd = Math.max(
    clampedStart + Math.min(points.length, 1),
    Math.min(viewRange.end, points.length)
  );
  const visiblePts = useMemo(
    () => (points.length > 0 ? points.slice(clampedStart, clampedEnd) : []),
    [points, clampedStart, clampedEnd]
  );

  const W = 760;
  const H = 232;
  const padL = 16;
  const padR = 68;
  const padT = 18;
  const padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const zoomAt = useCallback((scaleFactor: number, anchorRatio = 0.5) => {
    const total = ptsLenRef.current;
    if (total <= MIN_POINTS) return;
    const minSpan = Math.min(total, MIN_POINTS);
    setViewRange((prev) => {
      const curStart = Math.max(0, Math.min(prev.start, total - 1));
      const curEnd = Math.max(curStart + 1, Math.min(prev.end, total));
      const curSpan = curEnd - curStart;
      let nextSpan = Math.round(curSpan * scaleFactor);
      if (scaleFactor < 1 && nextSpan >= curSpan) nextSpan = curSpan - 2;
      if (scaleFactor > 1 && nextSpan <= curSpan) nextSpan = curSpan + 2;
      nextSpan = Math.max(minSpan, Math.min(total, nextSpan));
      if (nextSpan === curSpan) return prev;
      const anchor = Math.max(0, Math.min(1, anchorRatio));
      const anchorIdx = curStart + anchor * curSpan;
      let nextStart = Math.round(anchorIdx - anchor * nextSpan);
      nextStart = Math.max(0, Math.min(total - nextSpan, nextStart));
      return { start: nextStart, end: nextStart + nextSpan };
    });
  }, []);

  const panBy = useCallback((delta: number) => {
    const total = ptsLenRef.current;
    if (total <= 1 || delta === 0) return;
    setViewRange((prev) => {
      const curStart = Math.max(0, Math.min(prev.start, total - 1));
      const curEnd = Math.max(curStart + 1, Math.min(prev.end, total));
      const span = curEnd - curStart;
      const nextStart = Math.max(0, Math.min(total - span, curStart + delta));
      if (nextStart === curStart) return prev;
      return { start: nextStart, end: nextStart + span };
    });
  }, []);

  const resetView = useCallback(() => {
    setViewRange({ start: 0, end: ptsLenRef.current });
  }, []);

  // Non-passive wheel zoom on portfolio chart
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (ptsLenRef.current <= MIN_POINTS) return;
      e.preventDefault();
      const rect = (svgRef.current || el).getBoundingClientRect();
      const relX = rect.width > 0 ? ((e.clientX - rect.left) / rect.width) * W : W / 2;
      const anchorRatio = Math.max(0, Math.min(1, (relX - padL) / plotW));
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey && !e.metaKey) {
        const span = Math.max(1, viewRangeRef.current.end - viewRangeRef.current.start);
        const step = Math.max(1, Math.round((Math.abs(e.deltaX) / 40) * (span * 0.08)));
        panBy(e.deltaX > 0 ? step : -step);
        return;
      }
      if (e.deltaY === 0) return;
      const factor = e.deltaY < 0 ? 0.82 : 1.22;
      zoomAt(factor, anchorRatio);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [panBy, plotW, zoomAt]);

  const geom = useMemo(() => {
    const vals = visiblePts.map((p) => p.pnl);
    const rawMin = vals.length ? Math.min(0, ...vals) : -10;
    const rawMax = vals.length ? Math.max(0, ...vals) : 10;
    const span = Math.max(10, rawMax - rawMin);
    const minV = rawMin - span * 0.1;
    const maxV = rawMax + span * 0.1;
    const yFor = (v: number) => padT + ((maxV - v) / Math.max(1, maxV - minV)) * plotH;
    const xFor = (i: number) =>
      padL + (visiblePts.length > 1 ? (i / (visiblePts.length - 1)) * plotW : plotW / 2);
    const zeroY = yFor(0);
    const linePath = visiblePts
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${xFor(i).toFixed(1)},${yFor(p.pnl).toFixed(1)}`)
      .join(' ');
    const areaPath =
      visiblePts.length > 1
        ? `${linePath} L${xFor(visiblePts.length - 1).toFixed(1)},${zeroY.toFixed(1)} L${xFor(0).toFixed(1)},${zeroY.toFixed(1)} Z`
        : '';
    return { minV, maxV, yFor, xFor, zeroY, linePath, areaPath };
  }, [visiblePts, plotH, plotW]);

  const spansMultiYear = useMemo(() => {
    if (visiblePts.length < 2) return false;
    const y0 = (visiblePts[0].date || '').slice(0, 4);
    const y1 = (visiblePts[visiblePts.length - 1].date || '').slice(0, 4);
    return Boolean(y0 && y1 && y0 !== y1);
  }, [visiblePts]);

  const xTickIndices = useMemo(() => {
    const n = visiblePts.length;
    if (n <= 0) return [];
    if (n <= 6) return visiblePts.map((_, i) => i);
    const count = 6;
    const out: number[] = [];
    for (let k = 0; k < count; k += 1) {
      const idx = Math.round((k * (n - 1)) / (count - 1));
      if (out[out.length - 1] !== idx) out.push(idx);
    }
    return out;
  }, [visiblePts]);

  const hoveredPoint = hoverIdx !== null && visiblePts[hoverIdx] ? visiblePts[hoverIdx] : null;
  const visibleCount = visiblePts.length;
  const canPanLeft = clampedStart > 0;
  const canPanRight = clampedEnd < points.length;
  const canZoomIn = visibleCount > Math.min(points.length, MIN_POINTS);
  const canZoomOut = visibleCount < points.length;
  const panStep = Math.max(2, Math.round(visibleCount * 0.25));

  const startLabel = visiblePts[0]?.date || fromDate || '';
  const endLabel = visiblePts[visiblePts.length - 1]?.date || toDate || '';

  return (
    <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2.5">
          <h4 className="text-xs font-semibold text-slate-200">{title}</h4>
          {(startLabel || endLabel) && (
            <span className="inline-flex items-center gap-1 rounded-md border border-slate-800 bg-[#0b1120] px-2 py-0.5 font-mono text-[10px] text-slate-400">
              <Calendar className="h-3 w-3 text-sky-400" />
              {startLabel.slice(0, 10)} → {endLabel.slice(0, 10)}
            </span>
          )}
          {hasDailyCurve && (
            <div className="inline-flex rounded-md border border-slate-800 bg-[#0b1120] p-0.5 text-[10px]">
              <button
                type="button"
                onClick={() => setSeriesMode('daily')}
                className={`rounded px-2 py-0.5 font-semibold transition-colors ${
                  seriesMode === 'daily'
                    ? 'bg-sky-500/20 text-sky-300'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                By Date (Daily)
              </button>
              <button
                type="button"
                onClick={() => setSeriesMode('trades')}
                className={`rounded px-2 py-0.5 font-semibold transition-colors ${
                  seriesMode === 'trades'
                    ? 'bg-sky-500/20 text-sky-300'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                By Trade
              </button>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Interactive Hover Readout OR Total P&L */}
          {hoveredPoint ? (
            <div className="flex flex-wrap items-center gap-2.5 rounded-md border border-slate-700 bg-[#0b1120] px-2.5 py-1 font-mono text-[11px] tabular-nums">
              <span className="font-semibold text-sky-300">
                {hoveredPoint.date.slice(0, 16).replace('T', ' ')}
              </span>
              <span className={hoveredPoint.pnl >= 0 ? 'font-bold text-emerald-400' : 'font-bold text-red-400'}>
                {hoveredPoint.pnl >= 0 ? '+' : ''}₹
                {hoveredPoint.pnl.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
              </span>
              {typeof hoveredPoint.equity === 'number' && (
                <span className="text-slate-400">
                  Eq ₹{hoveredPoint.equity.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                </span>
              )}
              {typeof hoveredPoint.drawdownPct === 'number' && hoveredPoint.drawdownPct > 0 && (
                <span className="text-amber-300">DD -{hoveredPoint.drawdownPct.toFixed(2)}%</span>
              )}
              {typeof hoveredPoint.openPositions === 'number' && (
                <span className="text-slate-500">Pos {hoveredPoint.openPositions}</span>
              )}
              {hoveredPoint.symbol && (
                <span className="text-slate-300">{hoveredPoint.symbol}</span>
              )}
            </div>
          ) : (
            <span
              className={`font-mono text-xs font-bold tabular-nums ${
                totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'
              }`}
            >
              {totalPnl >= 0 ? '+' : ''}₹
              {totalPnl.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
            </span>
          )}

          {points.length > MIN_POINTS && (
            <div className="flex items-center rounded-md border border-slate-700 bg-[#0b1120] p-0.5">
              <button
                type="button"
                onClick={() => panBy(-panStep)}
                disabled={!canPanLeft}
                title="Pan earlier"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => panBy(panStep)}
                disabled={!canPanRight}
                title="Pan later"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
              <span className="mx-0.5 h-3.5 w-px bg-slate-700" />
              <button
                type="button"
                onClick={() => zoomAt(0.7, 0.5)}
                disabled={!canZoomIn}
                title="Zoom in"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ZoomIn className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => zoomAt(1.4, 0.5)}
                disabled={!canZoomOut}
                title="Zoom out"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                <ZoomOut className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={resetView}
                disabled={!canZoomOut}
                title="Fit full range"
                className="flex h-6 w-6 items-center justify-center rounded text-slate-300 hover:bg-slate-800 hover:text-white disabled:opacity-30"
              >
                {canZoomOut ? <Maximize2 className="h-3 w-3" /> : <RotateCcw className="h-3 w-3" />}
              </button>
            </div>
          )}
        </div>
      </div>

      <div ref={wrapRef} className="overflow-hidden rounded-lg border border-slate-800 bg-[#0b1120]">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className={`block w-full select-none touch-none ${
            isDragging ? 'cursor-grabbing' : points.length > MIN_POINTS ? 'cursor-crosshair' : ''
          }`}
          onDoubleClick={resetView}
          onPointerDown={(e) => {
            if (e.button !== 0 && e.pointerType === 'mouse') return;
            if (visibleCount >= points.length) return;
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
          }}
          onPointerMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            if (dragRef.current) {
              const d = dragRef.current;
              const dxSvg = ((e.clientX - d.startClientX) / d.rectWidth) * W;
              const span = Math.max(1, d.startViewEnd - d.startViewStart);
              const stepSvg = plotW / Math.max(1, span - 1);
              const delta = Math.round(-dxSvg / Math.max(0.5, stepSvg));
              const nextStart = Math.max(0, Math.min(points.length - span, d.startViewStart + delta));
              setViewRange((prev) =>
                prev.start === nextStart && prev.end === nextStart + span
                  ? prev
                  : { start: nextStart, end: nextStart + span }
              );
              return;
            }
            if (visiblePts.length === 0) return;
            const relX = ((e.clientX - rect.left) / Math.max(1, rect.width)) * W;
            const ratio = Math.max(0, Math.min(1, (relX - padL) / Math.max(1, plotW)));
            const idx = Math.round(ratio * Math.max(0, visiblePts.length - 1));
            if (idx >= 0 && idx < visiblePts.length) {
              setHoverIdx(idx);
            }
          }}
          onPointerUp={(e) => {
            if (dragRef.current && dragRef.current.pointerId === e.pointerId) {
              try {
                e.currentTarget.releasePointerCapture(e.pointerId);
              } catch {
                // ignore
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
            if (!dragRef.current) setHoverIdx(null);
          }}
        >
          <defs>
            <clipPath id={clipAboveId}>
              <rect x={padL} y={0} width={plotW} height={Math.max(0, geom.zeroY)} />
            </clipPath>
            <clipPath id={clipBelowId}>
              <rect x={padL} y={geom.zeroY} width={plotW} height={Math.max(0, H - geom.zeroY)} />
            </clipPath>
          </defs>

          {/* Subtle horizontal grid */}
          <line x1={padL} y1={padT} x2={W - padR} y2={padT} stroke="#1e293b" strokeWidth="1" />
          <line x1={padL} y1={padT + plotH} x2={W - padR} y2={padT + plotH} stroke="#1e293b" strokeWidth="1" />

          {/* Zero baseline */}
          <line
            x1={padL}
            y1={geom.zeroY}
            x2={W - padR}
            y2={geom.zeroY}
            stroke="#334155"
            strokeDasharray="3 3"
            strokeWidth="1"
          />

          {/* Right Y-axis values */}
          <text x={W - padR + 6} y={geom.zeroY + 3} fill="#64748b" fontSize="10" fontFamily="IBM Plex Mono, monospace">
            0
          </text>
          <text x={W - padR + 6} y={padT + 8} fill="#10b981" fontSize="10" fontFamily="IBM Plex Mono, monospace">
            {geom.maxV >= 0 ? '+' : ''}{geom.maxV.toFixed(0)}
          </text>
          <text x={W - padR + 6} y={padT + plotH} fill="#ef4444" fontSize="10" fontFamily="IBM Plex Mono, monospace">
            {geom.minV.toFixed(0)}
          </text>

          {/* Green / Red zero-line area + stroke */}
          {geom.areaPath && (
            <>
              <path d={geom.areaPath} fill="rgba(16, 185, 129, 0.22)" clipPath={`url(#${clipAboveId})`} />
              <path d={geom.areaPath} fill="rgba(239, 68, 68, 0.22)" clipPath={`url(#${clipBelowId})`} />
              <path d={geom.linePath} fill="none" stroke="#10b981" strokeWidth="1.8" clipPath={`url(#${clipAboveId})`} />
              <path d={geom.linePath} fill="none" stroke="#ef4444" strokeWidth="1.8" clipPath={`url(#${clipBelowId})`} />
            </>
          )}

          {/* X-axis date ticks */}
          {xTickIndices.map((idx, pos) => {
            const pt = visiblePts[idx];
            if (!pt) return null;
            const x = geom.xFor(idx);
            const anchor = pos === 0 ? 'start' : pos === xTickIndices.length - 1 ? 'end' : 'middle';
            return (
              <g key={`xt-${clampedStart + idx}`}>
                <line x1={x} y1={padT + plotH} x2={x} y2={padT + plotH + 4} stroke="#334155" strokeWidth="1" />
                <text
                  x={x}
                  y={H - 8}
                  textAnchor={anchor}
                  fill="#64748b"
                  fontSize="9.5"
                  fontFamily="IBM Plex Mono, monospace"
                >
                  {formatShortDate(pt.date, spansMultiYear)}
                </text>
              </g>
            );
          })}

          {/* Interactive hover crosshair + point marker + X-axis date badge */}
          {!isDragging && hoverIdx !== null && visiblePts[hoverIdx] && (
            <g pointerEvents="none">
              <line
                x1={geom.xFor(hoverIdx)}
                y1={padT}
                x2={geom.xFor(hoverIdx)}
                y2={padT + plotH}
                stroke="#64748b"
                strokeDasharray="3 3"
                strokeWidth="1"
              />
              <circle
                cx={geom.xFor(hoverIdx)}
                cy={geom.yFor(visiblePts[hoverIdx].pnl)}
                r="4"
                fill={visiblePts[hoverIdx].pnl >= 0 ? '#10b981' : '#ef4444'}
                stroke="#0b1120"
                strokeWidth="1.5"
              />
              <rect
                x={Math.min(W - padR - 98, Math.max(padL, geom.xFor(hoverIdx) - 48))}
                y={padT + plotH + 5}
                width={96}
                height={18}
                rx="3"
                fill="#1e293b"
                stroke="#475569"
                strokeWidth="0.7"
              />
              <text
                x={Math.min(W - padR - 50, Math.max(padL + 48, geom.xFor(hoverIdx)))}
                y={padT + plotH + 17}
                textAnchor="middle"
                fill="#f1f5f9"
                fontSize="9.5"
                fontFamily="IBM Plex Mono, monospace"
              >
                {visiblePts[hoverIdx].date.slice(0, 10)}
              </text>
            </g>
          )}
        </svg>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800/80 px-3 py-1.5 font-mono text-[10px] text-slate-500">
          <span>
            {seriesMode === 'daily' ? 'Daily portfolio dates' : 'Trade exit dates'}:{' '}
            {visibleCount} / {points.length} points
          </span>
          <span className="font-sans">
            Hover to inspect date &amp; P&amp;L · Scroll to zoom · Drag to pan
          </span>
        </div>
      </div>
    </div>
  );
}
