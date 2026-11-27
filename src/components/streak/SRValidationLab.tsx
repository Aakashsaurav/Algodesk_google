import { useState } from 'react';
import { BarChart3, Download, Play, RefreshCw } from 'lucide-react';
import { apiFetch } from '../../utils/authClient';
import type { SRValidationBucket, SRValidationReport } from './types';

interface SRValidationLabProps {
  defaultSymbols?: string[];
  defaultUniverse?: string;
  defaultExchange?: string;
  defaultTimeframe?: string;
  defaultFromDate?: string;
  defaultToDate?: string;
  defaultParams?: Record<string, unknown>;
  compact?: boolean;
}

type BreakdownTab = 'event' | 'tier' | 'regime' | 'horizon' | 'baselines';

const TF_OPTIONS = [
  { id: '5minute', label: '5m' },
  { id: '15minute', label: '15m' },
  { id: '60minute', label: '1h' },
  { id: '1day', label: '1D' },
];

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v.toFixed(digits)}%`;
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

function fmtSigned(v: number | null | undefined, suffix = '', digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}${suffix}`;
}

export function SRValidationLab({
  defaultSymbols,
  defaultUniverse = 'CUSTOM',
  defaultExchange = 'NSE',
  defaultTimeframe = '1day',
  defaultFromDate,
  defaultToDate,
  defaultParams,
  compact = false,
}: SRValidationLabProps) {
  const todayIso = new Date().toISOString().slice(0, 10);
  const defaultStartIso = new Date(Date.now() - 540 * 86400_000).toISOString().slice(0, 10);

  const [symbolsText, setSymbolsText] = useState(
    defaultSymbols && defaultSymbols.length > 0
      ? defaultSymbols.join(', ')
      : 'RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK'
  );
  const [exchange, setExchange] = useState(defaultExchange || 'NSE');
  const [timeframe, setTimeframe] = useState(defaultTimeframe || '1day');
  const [fromDate, setFromDate] = useState(defaultFromDate || defaultStartIso);
  const [toDate, setToDate] = useState(defaultToDate || todayIso);
  const [primaryHorizon, setPrimaryHorizon] = useState<number>(10);
  const [htfConfluence, setHtfConfluence] = useState<boolean>(false);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [report, setReport] = useState<SRValidationReport | null>(null);
  const [activeTab, setActiveTab] = useState<BreakdownTab>('baselines');

  const handleRunValidation = async () => {
    setLoading(true);
    setError('');
    try {
      const parsedSymbols = symbolsText
        .split(/[\s,]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      const res = await apiFetch('/api/analytics/sr/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbols: parsedSymbols.length > 0 ? parsedSymbols : undefined,
          universe: parsedSymbols.length > 0 ? 'CUSTOM' : defaultUniverse,
          exchange,
          timeframe,
          from_date: fromDate,
          to_date: toDate,
          horizons: [5, 10, 20],
          primary_horizon: primaryHorizon,
          htf_confluence: htfConfluence,
          params: defaultParams || {},
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || 'Statistical S&R validation failed.');
      }
      setReport(body as SRValidationReport);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Validation request failed.');
    } finally {
      setLoading(false);
    }
  };

  const handleDownloadJson = () => {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `sr_validation_${report.timeframe}_${report.to_date || todayIso}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleDownloadCsv = () => {
    if (!report) return;
    const headers = [
      'Section',
      'Group',
      'Events',
      'Success %',
      'Avg MFE (ATR)',
      'Avg MAE (ATR)',
      'Edge Ratio (MFE/MAE)',
      'Avg Reaction Bars',
      'Avg Return %',
    ];
    const rows: string[] = [headers.join(',')];
    const pushRows = (section: string, list: Array<{ label: string; b: SRValidationBucket }>) => {
      for (const { label, b } of list) {
        rows.push(
          [
            section,
            `"${label.replace(/"/g, '""')}"`,
            b.count,
            b.success_rate_pct ?? '',
            b.avg_mfe_atr ?? '',
            b.avg_mae_atr ?? '',
            b.edge_ratio ?? '',
            b.avg_reaction_bars ?? '',
            b.avg_return_pct ?? '',
          ].join(',')
        );
      }
    };
    pushRows('ByEvent', (report.by_event || []).map((r) => ({ label: r.event, b: r })));
    pushRows('ByStrengthTier', (report.by_strength_tier || []).map((r) => ({ label: r.tier, b: r })));
    pushRows('ByRegime', (report.by_regime || []).map((r) => ({ label: r.regime, b: r })));
    pushRows('ByHorizon', (report.by_horizon || []).map((r) => ({ label: `${r.horizon_bars} bars`, b: r })));
    pushRows('Baseline', (report.baselines || []).map((r) => ({ label: r.label, b: r })));
    const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `sr_validation_${report.timeframe}_${report.to_date || todayIso}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const renderBucketTable = (entries: Array<{ label: string; bucket: SRValidationBucket }>, groupLabel: string) => (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full border-collapse text-left text-xs">
        <thead className="border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-3 py-2.5">{groupLabel}</th>
            <th className="px-3 py-2.5 text-right">Events</th>
            <th className="px-3 py-2.5 text-right">Success %</th>
            <th className="px-3 py-2.5 text-right">Avg MFE (ATR)</th>
            <th className="px-3 py-2.5 text-right">Avg MAE (ATR)</th>
            <th className="px-3 py-2.5 text-right">Edge (MFE/MAE)</th>
            <th className="px-3 py-2.5 text-right">Reaction Bars</th>
            <th className="px-3 py-2.5 text-right">Avg Ret %</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
          {entries.length === 0 ? (
            <tr>
              <td colSpan={8} className="px-4 py-6 text-center font-sans text-slate-500">
                No qualifying S&amp;R events in this breakdown for the selected date range.
              </td>
            </tr>
          ) : (
            entries.map(({ label, bucket: b }) => (
              <tr key={label} className="hover:bg-slate-800/30">
                <td className="px-3 py-2 font-sans font-medium text-slate-200">{label}</td>
                <td className="px-3 py-2 text-right text-slate-300">{b.count}</td>
                <td
                  className={`px-3 py-2 text-right font-semibold ${
                    (b.success_rate_pct ?? 0) >= 55
                      ? 'text-emerald-400'
                      : (b.success_rate_pct ?? 0) >= 45
                        ? 'text-amber-300'
                        : 'text-red-400'
                  }`}
                >
                  {fmtPct(b.success_rate_pct)}
                </td>
                <td className="px-3 py-2 text-right text-emerald-400">{fmtNum(b.avg_mfe_atr)}</td>
                <td className="px-3 py-2 text-right text-red-400">{fmtNum(b.avg_mae_atr)}</td>
                <td className="px-3 py-2 text-right font-semibold text-sky-300">{fmtNum(b.edge_ratio)}x</td>
                <td className="px-3 py-2 text-right text-slate-300">{fmtNum(b.avg_reaction_bars, 1)}</td>
                <td
                  className={`px-3 py-2 text-right ${
                    (b.avg_return_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                  }`}
                >
                  {fmtSigned(b.avg_return_pct, '%')}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );

  const summary = report?.summary;

  return (
    <div className="space-y-4 rounded-xl border border-slate-800 bg-[#111827] p-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2.5">
          <BarChart3 className="h-4 w-4 text-sky-400" />
          <div>
            <h4 className="text-sm font-semibold text-slate-100">
              Support &amp; Resistance Statistical Validation Lab
            </h4>
            <p className="text-[11px] text-slate-400">
              Causal zero-lookahead evaluation · Bounce / Rejection / Breakout / Retest · MFE &amp; MAE (ATR) · 4 Baselines
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {report && (
            <>
              <button
                type="button"
                onClick={handleDownloadCsv}
                className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-semibold text-slate-300 hover:border-sky-400 hover:text-sky-300"
              >
                <Download className="h-3.5 w-3.5" />
                <span>CSV</span>
              </button>
              <button
                type="button"
                onClick={handleDownloadJson}
                className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-semibold text-slate-300 hover:border-sky-400 hover:text-sky-300"
              >
                <Download className="h-3.5 w-3.5" />
                <span>JSON</span>
              </button>
            </>
          )}
          <button
            type="button"
            onClick={() => void handleRunValidation()}
            disabled={loading}
            className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg bg-sky-500 px-3.5 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
          >
            {loading ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            <span>{loading ? 'Validating...' : report ? 'Re-Run Validation' : 'Run S&R Validation'}</span>
          </button>
        </div>
      </div>

      {/* Parameter Strip */}
      <div className={`grid grid-cols-1 gap-3 ${compact ? 'md:grid-cols-6' : 'md:grid-cols-12'}`}>
        <div className={compact ? 'md:col-span-2' : 'md:col-span-4'}>
          <label className="mb-1 block text-[11px] font-medium text-slate-400">Symbols (comma-separated)</label>
          <input
            type="text"
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
            placeholder="RELIANCE, TCS, INFY, HDFCBANK"
            className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
          />
        </div>
        <div className={compact ? 'md:col-span-1' : 'md:col-span-1'}>
          <label className="mb-1 block text-[11px] font-medium text-slate-400">Exchange</label>
          <select
            value={exchange}
            onChange={(e) => setExchange(e.target.value)}
            className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100"
          >
            <option value="NSE">NSE</option>
            <option value="BSE">BSE</option>
          </select>
        </div>
        <div className={compact ? 'md:col-span-1' : 'md:col-span-1'}>
          <label className="mb-1 block text-[11px] font-medium text-slate-400">Timeframe</label>
          <select
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
            className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100"
          >
            {TF_OPTIONS.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className={compact ? 'md:col-span-1' : 'md:col-span-2'}>
          <label className="mb-1 block text-[11px] font-medium text-slate-400">From Date</label>
          <input
            type="date"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
            className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-200"
          />
        </div>
        <div className={compact ? 'md:col-span-1' : 'md:col-span-2'}>
          <label className="mb-1 block text-[11px] font-medium text-slate-400">To Date</label>
          <input
            type="date"
            value={toDate}
            onChange={(e) => setToDate(e.target.value)}
            className="min-h-[34px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-200"
          />
        </div>
        <div className={compact ? 'md:col-span-6 flex flex-wrap items-center gap-4' : 'md:col-span-2 flex flex-col justify-end gap-1.5'}>
          <div className="flex items-center gap-2">
            <label className="text-[11px] font-medium text-slate-400">Primary Horizon</label>
            <select
              value={primaryHorizon}
              onChange={(e) => setPrimaryHorizon(Number(e.target.value))}
              className="min-h-[30px] rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100"
            >
              <option value={5}>5 bars</option>
              <option value={10}>10 bars</option>
              <option value={20}>20 bars</option>
            </select>
          </div>
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-300">
            <input
              type="checkbox"
              checked={htfConfluence}
              onChange={(e) => setHtfConfluence(e.target.checked)}
              className="h-3.5 w-3.5 accent-sky-400"
            />
            <span>HTF Confluence (Causal)</span>
          </label>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-3.5 py-2.5 text-xs text-red-200">
          {error}
        </div>
      )}

      {!report && !loading && !error && (
        <div className="rounded-lg border border-dashed border-slate-800 bg-[#0b1120]/60 px-4 py-7 text-center text-xs text-slate-400">
          Click <strong className="text-slate-200">Run S&amp;R Validation</strong> to measure causal bounce, rejection,
          breakout, retest, MFE/MAE, strength-tier separation, market-regime edge, and lift against 4 baselines.
        </div>
      )}

      {loading && (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-slate-800 bg-[#0b1120]/60 py-10 text-xs text-slate-400">
          <RefreshCw className="h-4 w-4 animate-spin text-sky-400" />
          <span>Evaluating causal S&amp;R events and 4 baseline detectors across {timeframe} bars...</span>
        </div>
      )}

      {report && !loading && summary && (
        <div className="space-y-4">
          {/* Metadata Footnote (Zero-pill tabular line) */}
          <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-slate-400">
            <span>
              Evaluated {report.symbol_count} symbols ({(report.symbols_evaluated || []).slice(0, 6).join(', ')}
              {(report.symbols_evaluated || []).length > 6 ? ` +${report.symbols_evaluated.length - 6}` : ''}) ·{' '}
              {(report.total_bars_analyzed ?? 0).toLocaleString('en-IN')} bars · Primary Horizon:{' '}
              {report.primary_horizon_bars} bars · {report.htf_confluence ? 'HTF Confluence ON' : 'Chart TF Only'}
            </span>
            {report.missing_symbols && report.missing_symbols.length > 0 && (
              <span className="text-amber-300">
                Skipped {report.missing_symbols.length} symbol(s) with insufficient history
              </span>
            )}
          </div>

          {/* Headline KPI Strip */}
          <div className="grid grid-cols-2 gap-3 rounded-lg border border-slate-800 bg-[#0b1120] p-4 sm:grid-cols-5 lg:grid-cols-10">
            <div>
              <div className="text-[11px] text-slate-400">Events</div>
              <div className="mt-1 font-mono text-sm font-bold text-slate-100 tabular-nums">
                {summary.total_events}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Overall Hit</div>
              <div className="mt-1 font-mono text-sm font-bold text-sky-300 tabular-nums">
                {fmtPct(summary.overall_success_rate_pct)}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Bounce %</div>
              <div className="mt-1 font-mono text-sm font-semibold text-emerald-400 tabular-nums">
                {fmtPct(summary.bounce_success_rate_pct)}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Rejection %</div>
              <div className="mt-1 font-mono text-sm font-semibold text-emerald-400 tabular-nums">
                {fmtPct(summary.rejection_success_rate_pct)}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Breakout %</div>
              <div className="mt-1 font-mono text-sm font-semibold text-sky-300 tabular-nums">
                {fmtPct(summary.breakout_success_rate_pct)}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Retest Hold</div>
              <div className="mt-1 font-mono text-sm font-semibold text-emerald-400 tabular-nums">
                {fmtPct(summary.retest_success_rate_pct)}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">False BO %</div>
              <div className="mt-1 font-mono text-sm font-semibold text-amber-300 tabular-nums">
                {fmtPct(summary.false_breakout_rate_pct)}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Avg MFE</div>
              <div className="mt-1 font-mono text-sm font-semibold text-emerald-400 tabular-nums">
                {fmtNum(summary.avg_mfe_atr)} ATR
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Avg MAE</div>
              <div className="mt-1 font-mono text-sm font-semibold text-red-400 tabular-nums">
                {fmtNum(summary.avg_mae_atr)} ATR
              </div>
            </div>
            <div>
              <div className="text-[11px] text-slate-400">Edge / React</div>
              <div className="mt-1 font-mono text-sm font-bold text-slate-100 tabular-nums">
                {fmtNum(summary.edge_ratio)}x · {fmtNum(summary.avg_reaction_bars, 1)}b
              </div>
            </div>
          </div>

          {/* Breakdown Tabs */}
          <div className="flex flex-wrap items-center gap-1 border-b border-slate-800 pb-2">
            {(
              [
                { id: 'baselines', label: 'Baseline Comparison & Lift' },
                { id: 'event', label: 'By Event' },
                { id: 'tier', label: 'By Strength Tier' },
                { id: 'regime', label: 'By Market Regime' },
                { id: 'horizon', label: 'By Horizon' },
              ] as const
            ).map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={`min-h-[30px] rounded-md px-3 text-xs font-semibold transition-colors ${
                  activeTab === tab.id
                    ? 'bg-sky-500/20 text-sky-300'
                    : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Active Breakdown View */}
          {activeTab === 'baselines' && (
            <div className="overflow-x-auto rounded-lg border border-slate-800">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="border-b border-slate-800 bg-[#0b1120] text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-3 py-2.5">Detector Model</th>
                    <th className="px-3 py-2.5 text-right">Events</th>
                    <th className="px-3 py-2.5 text-right">Success %</th>
                    <th className="px-3 py-2.5 text-right">Hit Lift vs Base</th>
                    <th className="px-3 py-2.5 text-right">Avg MFE (ATR)</th>
                    <th className="px-3 py-2.5 text-right">Avg MAE (ATR)</th>
                    <th className="px-3 py-2.5 text-right">Edge Ratio</th>
                    <th className="px-3 py-2.5 text-right">Edge Lift</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
                  {(report.baselines || []).map((row) => {
                    const isPrimary = row.id === 'full_sr_engine';
                    return (
                      <tr
                        key={row.id}
                        className={isPrimary ? 'bg-sky-950/25 font-semibold' : 'hover:bg-slate-800/30'}
                      >
                        <td className="px-3 py-2.5 font-sans text-slate-100">
                          {row.label}
                          {isPrimary && (
                            <span className="ml-2 font-mono text-[10px] font-semibold text-sky-400">
                              PRIMARY
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-right text-slate-300">{row.count}</td>
                        <td className="px-3 py-2.5 text-right text-slate-100">{fmtPct(row.success_rate_pct)}</td>
                        <td
                          className={`px-3 py-2.5 text-right ${
                            isPrimary
                              ? 'text-slate-500'
                              : (row.lift_vs_baseline_pp ?? 0) >= 0
                                ? 'text-emerald-400'
                                : 'text-red-400'
                          }`}
                        >
                          {isPrimary ? '—' : fmtSigned(row.lift_vs_baseline_pp, ' pp', 1)}
                        </td>
                        <td className="px-3 py-2.5 text-right text-emerald-400">{fmtNum(row.avg_mfe_atr)}</td>
                        <td className="px-3 py-2.5 text-right text-red-400">{fmtNum(row.avg_mae_atr)}</td>
                        <td className="px-3 py-2.5 text-right text-sky-300">{fmtNum(row.edge_ratio)}x</td>
                        <td
                          className={`px-3 py-2.5 text-right ${
                            isPrimary
                              ? 'text-slate-500'
                              : (row.edge_ratio_lift ?? 0) >= 0
                                ? 'text-emerald-400'
                                : 'text-red-400'
                          }`}
                        >
                          {isPrimary ? '—' : fmtSigned(row.edge_ratio_lift, 'x', 2)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {activeTab === 'event' &&
            renderBucketTable(
              (report.by_event || []).map((r) => ({ label: r.event, bucket: r })),
              'S&R Event Type'
            )}

          {activeTab === 'tier' &&
            renderBucketTable(
              (report.by_strength_tier || []).map((r) => ({ label: r.tier, bucket: r })),
              'Zone Strength Tier'
            )}

          {activeTab === 'regime' &&
            renderBucketTable(
              (report.by_regime || []).map((r) => ({ label: r.regime, bucket: r })),
              'Market Regime'
            )}

          {activeTab === 'horizon' &&
            renderBucketTable(
              (report.by_horizon || []).map((r) => ({ label: `${r.horizon_bars} bars`, bucket: r })),
              'Forward Horizon'
            )}
        </div>
      )}
    </div>
  );
}
