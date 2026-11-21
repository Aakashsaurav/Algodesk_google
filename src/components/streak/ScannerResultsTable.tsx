import { useMemo, useState } from 'react';
import { BarChart2, Download, Filter, Radio, Search } from 'lucide-react';
import type { QuickOrderTarget } from './QuickOrderModal';
import type { ScannerColumnSpec, ScannerResultRow } from './types';

interface ScannerResultsTableProps {
  rows: ScannerResultRow[];
  columns?: ScannerColumnSpec[];
  loading?: boolean;
  error?: string;
  scannedAt?: string;
  asOfDate?: string;
  isLive?: boolean;
  liveTickCount?: number;
  liveStatusText?: string;
  totalScanned?: number;
  emptyTitle?: string;
  emptySubtitle?: string;
  onQuickOrder: (target: QuickOrderTarget) => void;
  onOpenChart: (symbol: string) => void;
  footerActions?: React.ReactNode;
}

function formatCellValue(value: unknown, col: ScannerColumnSpec): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number' && Number.isFinite(value)) {
    const dec = col.decimals ?? col.digits ?? 2;
    const fmt = col.fmt || col.format || 'number';
    if (fmt === 'pct' || fmt === 'percent') {
      return `${value >= 0 ? '+' : ''}${value.toFixed(dec)}%`;
    }
    if (fmt === 'int' || fmt === 'volume') {
      return Math.round(value).toLocaleString('en-IN');
    }
    return value.toLocaleString('en-IN', {
      minimumFractionDigits: dec,
      maximumFractionDigits: dec,
    });
  }
  return String(value);
}

function formatVolume(vol?: number): string {
  if (vol === undefined || vol === null || !Number.isFinite(vol)) return '—';
  if (vol >= 10_000_000) return `${(vol / 10_000_000).toFixed(2)}Cr`;
  if (vol >= 100_000) return `${(vol / 100_000).toFixed(2)}L`;
  if (vol >= 1_000) return `${(vol / 1_000).toFixed(1)}K`;
  return Math.round(vol).toLocaleString('en-IN');
}

export function ScannerResultsTable({
  rows,
  columns = [],
  loading = false,
  error = '',
  scannedAt,
  asOfDate,
  isLive = false,
  liveTickCount = 0,
  liveStatusText,
  totalScanned,
  emptyTitle = 'No Results Found',
  emptySubtitle = 'Run on a different basket (Scan on) or adjust your conditions to see matching symbols.',
  onQuickOrder,
  onOpenChart,
  footerActions,
}: ScannerResultsTableProps) {
  const [search, setSearch] = useState('');
  const [matchedOnly, setMatchedOnly] = useState(false);

  // Filter out standard columns so we don't duplicate Symbol/LTP/Change/Volume
  const extraCols = useMemo(() => {
    const skip = new Set(['symbol', 'exchange', 'close', 'ltp', 'change_pct', 'volume', 'matched']);
    return columns.filter((c) => !skip.has(c.key.toLowerCase())).slice(0, 4);
  }, [columns]);

  const filteredRows = useMemo(() => {
    const q = search.trim().toUpperCase();
    return rows.filter((r) => {
      if (matchedOnly && r.matched === false) return false;
      if (q && !r.symbol.toUpperCase().includes(q)) return false;
      return true;
    });
  }, [rows, search, matchedOnly]);

  const matchedCount = useMemo(
    () => rows.filter((r) => r.matched !== false).length,
    [rows]
  );

  const handleExportCsv = () => {
    if (!filteredRows.length) return;
    const headers = ['Symbol', 'Exchange', 'LTP', 'Change %', 'Volume', ...extraCols.map((c) => c.label)];
    const csvLines = [headers.join(',')];
    for (const r of filteredRows) {
      const ltp = Number(r.ltp ?? r.close ?? 0);
      const vals = [
        r.symbol,
        r.exchange || 'NSE',
        ltp.toFixed(2),
        Number(r.change_pct || 0).toFixed(2),
        String(r.volume ?? ''),
        ...extraCols.map((c) => `"${String(r[c.key] ?? '').replace(/"/g, '""')}"`),
      ];
      csvLines.push(vals.join(','));
    }
    const blob = new Blob([csvLines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `scanner_results_${asOfDate || 'latest'}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827]">
      {/* Header matching Streak Scanner Results card */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <h3 className="text-sm font-semibold text-slate-100">
            Scanner Results <span className="font-mono text-sky-400">({filteredRows.length})</span>
          </h3>
          {isLive && (
            <span className="inline-flex items-center gap-1.5 rounded bg-emerald-950/60 px-2 py-0.5 font-mono text-[11px] font-semibold text-emerald-300">
              <Radio className="h-3 w-3 text-emerald-400" />
              LIVE WS · {liveTickCount} ticks
            </span>
          )}
          {!isLive && asOfDate && (
            <span className="font-mono text-[11px] font-medium text-amber-300">
              As of {asOfDate}
            </span>
          )}
          {scannedAt && (
            <span className="font-mono text-[11px] text-slate-500">
              {totalScanned ? `${matchedCount}/${totalScanned} matched · ` : ''}
              {scannedAt.slice(0, 19).replace('T', ' ')}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {rows.some((r) => r.matched === false) && (
            <button
              type="button"
              onClick={() => setMatchedOnly((v) => !v)}
              className={`inline-flex min-h-[32px] items-center gap-1 rounded-lg border px-2.5 text-xs font-medium transition-colors ${
                matchedOnly
                  ? 'border-sky-500 bg-sky-500/15 text-sky-300'
                  : 'border-slate-700 bg-[#0b1120] text-slate-400 hover:text-slate-200'
              }`}
            >
              <Filter className="h-3.5 w-3.5" />
              <span>Matched</span>
            </button>
          )}

          <button
            type="button"
            onClick={handleExportCsv}
            disabled={filteredRows.length === 0}
            title="Download CSV"
            aria-label="Download CSV"
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-slate-700 bg-[#0b1120] text-sky-400 hover:border-sky-400 disabled:opacity-40"
          >
            <Download className="h-4 w-4" />
          </button>

          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search"
              aria-label="Search scanner results"
              className="min-h-[32px] w-40 rounded-lg border border-slate-700 bg-[#0b1120] pl-8 pr-2.5 text-xs text-slate-100 placeholder-slate-500 focus:border-sky-400 focus:outline-none sm:w-48"
            />
          </div>
        </div>
      </div>

      {liveStatusText && isLive && (
        <div className="border-b border-slate-800/80 bg-[#0b1120]/70 px-4 py-1.5 font-mono text-[11px] text-slate-400">
          {liveStatusText}
        </div>
      )}

      {error && (
        <div className="border-b border-red-900/60 bg-red-950/40 px-4 py-2.5 text-xs text-red-200">
          {error}
        </div>
      )}

      {/* Table Body with overflow-anchor:none so live ticks don't jump scroll */}
      <div className="min-h-[360px] flex-1 overflow-auto [overflow-anchor:none]">
        {loading && filteredRows.length === 0 ? (
          <div className="flex h-72 flex-col items-center justify-center gap-2 text-xs text-slate-400">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-slate-700 border-t-sky-400" />
            <span>Evaluating scanner conditions across instruments...</span>
          </div>
        ) : filteredRows.length === 0 ? (
          <div className="flex h-80 flex-col items-center justify-center px-6 text-center">
            <div className="mb-3 flex h-14 w-14 items-center justify-center rounded-xl border border-slate-800 bg-[#0b1120] text-slate-500">
              <Search className="h-6 w-6" />
            </div>
            <p className="text-sm font-semibold text-slate-200">{emptyTitle}</p>
            <p className="mt-1 max-w-sm text-xs text-slate-400">{emptySubtitle}</p>
          </div>
        ) : (
          <table className="w-full border-collapse text-left">
            <thead className="sticky top-0 z-10 border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-2.5">Symbol</th>
                <th className="px-3 py-2.5 text-right">LTP</th>
                <th className="px-3 py-2.5 text-right">Change %</th>
                <th className="px-3 py-2.5 text-right">Volume</th>
                {extraCols.map((col) => (
                  <th key={col.key} className="px-3 py-2.5 text-right">
                    {col.label}
                  </th>
                ))}
                <th className="px-4 py-2.5 text-right">Buy / Sell</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70 text-xs">
              {filteredRows.map((row) => {
                const ltp = Number(row.ltp ?? row.close ?? 0);
                const chg = Number(row.change_pct ?? 0);
                const isUp = chg >= 0;
                const tickBg =
                  row.tick_direction === 'up'
                    ? 'bg-emerald-950/20'
                    : row.tick_direction === 'down'
                      ? 'bg-red-950/20'
                      : '';
                return (
                  <tr
                    key={`${row.exchange || 'NSE'}:${row.symbol}`}
                    className={`transition-colors hover:bg-slate-800/50 ${tickBg}`}
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-semibold text-slate-100">
                          {row.symbol}
                        </span>
                        <span className="text-[10px] font-medium text-slate-500">
                          {row.exchange || 'NSE'}
                        </span>
                      </div>
                      {row.signal && row.signal !== 'NEUTRAL' && (
                        <div className="mt-0.5 font-mono text-[10px] text-slate-400">
                          {row.signal}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono font-semibold text-slate-100 tabular-nums">
                      {ltp > 0 ? ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}
                    </td>
                    <td
                      className={`px-3 py-2.5 text-right font-mono font-semibold tabular-nums ${
                        isUp ? 'text-emerald-400' : 'text-red-400'
                      }`}
                    >
                      {isUp ? '+' : ''}
                      {chg.toFixed(2)}%
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-300 tabular-nums">
                      {formatVolume(row.volume)}
                    </td>
                    {extraCols.map((col) => (
                      <td
                        key={col.key}
                        className="px-3 py-2.5 text-right font-mono text-slate-300 tabular-nums"
                      >
                        {formatCellValue(row[col.key], col)}
                      </td>
                    ))}
                    <td className="px-4 py-2.5 text-right">
                      <div className="inline-flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() =>
                            onQuickOrder({
                              symbol: row.symbol,
                              exchange: row.exchange || 'NSE',
                              side: 'BUY',
                              ltp,
                            })
                          }
                          title={`Buy ${row.symbol}`}
                          className="flex h-7 w-7 items-center justify-center rounded border border-sky-700/80 bg-sky-950/40 font-mono text-[11px] font-bold text-sky-300 hover:bg-sky-500 hover:text-slate-950"
                        >
                          B
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            onQuickOrder({
                              symbol: row.symbol,
                              exchange: row.exchange || 'NSE',
                              side: 'SELL',
                              ltp,
                            })
                          }
                          title={`Sell ${row.symbol}`}
                          className="flex h-7 w-7 items-center justify-center rounded border border-red-800/80 bg-red-950/40 font-mono text-[11px] font-bold text-red-300 hover:bg-red-500 hover:text-white"
                        >
                          S
                        </button>
                        <button
                          type="button"
                          onClick={() => onOpenChart(row.symbol)}
                          title={`Open ${row.symbol} Technicals`}
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

      {/* Optional Streak Bottom Action Bar (Copy / Set Alerts etc.) */}
      {footerActions && (
        <div className="flex flex-wrap items-center justify-end gap-2.5 border-t border-slate-800 bg-[#0b1120]/70 px-4 py-3">
          {footerActions}
        </div>
      )}
    </div>
  );
}
