import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch, withAuthTokenQuery } from '../../utils/authClient';
import {
  Bell,
  Code2,
  Copy,
  Eye,
  FolderOpen,
  Play,
  Plus,
  Radio,
  Search,
  Square,
  Trash2,
  X,
} from 'lucide-react';
import type { QuickOrderTarget } from './QuickOrderModal';
import { ScannerResultsTable } from './ScannerResultsTable';
import type {
  ScannerColumnSpec,
  ScannerDraftPreset,
  ScannerResultRow,
  ScreenSpecMeta,
  StreakMetaPayload,
  VisualCondition,
  VisualConditionGroup,
  VisualRuleDef,
} from './types';

interface DiscoverScannersViewProps {
  meta: StreakMetaPayload;
  onRefreshMeta: () => void;
  onOpenInCreateScanner: (draft: ScannerDraftPreset) => void;
  onNavigateAlerts: () => void;
  onQuickOrder: (target: QuickOrderTarget) => void;
  onOpenChart: (symbol: string) => void;
}

interface UnifiedScannerItem {
  key: string;
  id: string;
  name: string;
  description: string;
  engine: 'basic' | 'advance' | 'python';
  timeframe: string;
  timeframeLabel: string;
  universe: string;
  rule?: VisualRuleDef;
  pySpec?: ScreenSpecMeta;
}

const TF_TABS = [
  { id: 'all', label: 'All' },
  { id: '1minute', label: '1min' },
  { id: '5minute', label: '5min' },
  { id: '15minute', label: '15min' },
  { id: '60minute', label: '1hour' },
  { id: '1day', label: '1day' },
  { id: 'python', label: 'Python (.py)' },
];

function formatTfLabel(tf?: string): string {
  switch (tf) {
    case '1minute':
      return '1 Min';
    case '5minute':
      return '5 Min';
    case '15minute':
      return '15 Min';
    case '60minute':
      return '1 Hour';
    default:
      return '1 Day';
  }
}

function summarizeGroup(group?: VisualConditionGroup): string[] {
  if (!group || !group.conditions?.length) return ['No conditions defined.'];
  const lines: string[] = [];
  group.conditions.forEach((node, idx) => {
    const prefix = idx > 0 ? `${group.logic} ` : '';
    if ('conditions' in node && Array.isArray(node.conditions)) {
      lines.push(`${prefix}( ${summarizeGroup(node).join(' ')} )`);
    } else {
      const c = node as VisualCondition;
      const fmtOp = (o: VisualCondition['left']) =>
        o.type === 'number'
          ? String(o.value ?? 0)
          : `${o.type.toUpperCase()}${o.period ? `(${o.period})` : ''}[${o.offset ?? 0}]`;
      const opTxt =
        c.operator === 'up_by_pct'
          ? `up by ${c.pct ?? 1}% from`
          : c.operator === 'down_by_pct'
            ? `down by ${c.pct ?? 1}% from`
            : c.operator;
      lines.push(`${prefix}${fmtOp(c.left)} ${opTxt} ${fmtOp(c.right)}`);
    }
  });
  return lines;
}

export function DiscoverScannersView({
  meta,
  onRefreshMeta,
  onOpenInCreateScanner,
  onNavigateAlerts,
  onQuickOrder,
  onOpenChart,
}: DiscoverScannersViewProps) {
  const [search, setSearch] = useState('');
  const [activeTab, setActiveTab] = useState('all');
  const [scanOnKey, setScanOnKey] = useState<string>('universe:DEFAULT_WATCHLIST');
  const [selectedKey, setSelectedKey] = useState<string>('');

  // Scan execution state
  const [rows, setRows] = useState<ScannerResultRow[]>([]);
  const [columns, setColumns] = useState<ScannerColumnSpec[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [scannedAt, setScannedAt] = useState('');
  const [totalScanned, setTotalScanned] = useState<number | undefined>(undefined);

  // Live WS state
  const [liveSessionId, setLiveSessionId] = useState<string | null>(null);
  const [liveTickCount, setLiveTickCount] = useState(0);
  const [liveStatusText, setLiveStatusText] = useState('');
  const wsRef = useRef<WebSocket | null>(null);

  // Modals
  const [conditionsModalOpen, setConditionsModalOpen] = useState(false);
  const [myScannersOpen, setMyScannersOpen] = useState(false);
  const [alertModalOpen, setAlertModalOpen] = useState(false);
  const [alertFreqSec, setAlertFreqSec] = useState(300);
  const [alertBusy, setAlertBusy] = useState(false);
  const [alertFeedback, setAlertFeedback] = useState('');

  // Merge visual rules (screens) + python screens
  const allScanners = useMemo<UnifiedScannerItem[]>(() => {
    const visuals: UnifiedScannerItem[] = (meta.visual_rules || [])
      .filter((r) => r.kind === 'screen')
      .map((r) => ({
        key: `visual:${r.id || r.name}`,
        id: r.id || r.name,
        name: r.name,
        description: r.description || 'Visual multi-condition scanner',
        engine: r.builder_mode === 'advance' ? 'advance' : 'basic',
        timeframe: r.timeframe || '1day',
        timeframeLabel: formatTfLabel(r.timeframe),
        universe: r.universe || 'nifty50',
        rule: r,
      }));
    const pythons: UnifiedScannerItem[] = (meta.screens || []).map((s) => ({
      key: `python:${s.id}`,
      id: s.id,
      name: s.name,
      description: s.description || 'Custom Python (.py) scanner plugin',
      engine: 'python',
      timeframe: '1day',
      timeframeLabel: s.supports_multi_timeframe ? 'Multi-TF' : '1 Day',
      universe: 'nifty50',
      pySpec: s,
    }));
    return [...visuals, ...pythons];
  }, [meta.visual_rules, meta.screens]);

  const filteredScanners = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allScanners.filter((item) => {
      if (activeTab === 'python' && item.engine !== 'python') return false;
      if (activeTab !== 'all' && activeTab !== 'python' && item.timeframe !== activeTab) return false;
      if (q && !item.name.toLowerCase().includes(q) && !item.description.toLowerCase().includes(q)) {
        return false;
      }
      return true;
    });
  }, [allScanners, activeTab, search]);

  const selected = useMemo(() => {
    return (
      allScanners.find((s) => s.key === selectedKey) ||
      filteredScanners[0] ||
      allScanners[0] ||
      null
    );
  }, [allScanners, filteredScanners, selectedKey]);

  const mySavedScanners = useMemo(
    () => (meta.visual_rules || []).filter((r) => r.kind === 'screen'),
    [meta.visual_rules]
  );

  const stopLiveWs = useCallback(async () => {
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {
        // ignore
      }
      wsRef.current = null;
    }
    if (liveSessionId) {
      setLiveSessionId(null);
      setLiveStatusText('');
      await apiFetch('/api/analytics/live-screener/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }).catch(() => {});
    }
  }, [liveSessionId]);

  useEffect(() => {
    return () => {
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  const parseScanTarget = useCallback(() => {
    if (scanOnKey.startsWith('basket:')) {
      return { universe: 'DEFAULT_WATCHLIST', basket_id: scanOnKey.slice(7) };
    }
    return { universe: scanOnKey.replace(/^universe:/, '') || 'DEFAULT_WATCHLIST', basket_id: undefined };
  }, [scanOnKey]);

  const runSelectedScan = useCallback(
    async (item: UnifiedScannerItem | null) => {
      if (!item) return;
      await stopLiveWs();
      setLoading(true);
      setError('');
      try {
        const target = parseScanTarget();
        const payload: Record<string, unknown> = {
          screen_id: item.engine === 'python' ? item.id : `visual:${item.id}`,
          universe: target.universe,
          basket_id: target.basket_id,
          exchange: 'NSE',
          timeframe: item.timeframe || '1day',
          backend: 'auto',
        };
        if (item.engine !== 'python' && item.rule) {
          payload.rule_def = item.rule;
        }
        const res = await apiFetch('/api/analytics/screener', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || 'Scanner execution failed.');
        const resRows = Array.isArray(body.results)
          ? body.results
          : Array.isArray(body.rows)
            ? body.rows
            : [];
        setRows(resRows);
        setColumns(Array.isArray(body.columns) ? body.columns : []);
        setScannedAt(body.generated_at_ist || body.as_of_date || new Date().toISOString());
        setTotalScanned(body.total_scanned ?? body.symbols_scanned ?? body.scanned_count);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Scan failed.');
        setRows([]);
      } finally {
        setLoading(false);
      }
    },
    [parseScanTarget, stopLiveWs]
  );

  useEffect(() => {
    if (selected && !selectedKey) {
      setSelectedKey(selected.key);
    }
  }, [selected, selectedKey]);

  // Auto-run scan when selected scanner or Scan On universe changes
  useEffect(() => {
    if (selected) {
      void runSelectedScan(selected);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.key, scanOnKey]);

  const startLiveWs = async () => {
    if (!selected) return;
    await stopLiveWs();
    setError('');
    setLiveTickCount(0);
    setLiveStatusText('Connecting to WebSocket market feed...');
    try {
      const target = parseScanTarget();
      const res = await apiFetch('/api/analytics/live-screener/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          screen_id: selected.engine === 'python' ? selected.id : `visual:${selected.id}`,
          rule_def: selected.engine !== 'python' ? selected.rule : undefined,
          universe: target.universe,
          basket_id: target.basket_id,
          exchange: 'NSE',
          timeframe: selected.timeframe || '1day',
          backend: 'auto',
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not start live WebSocket stream.');
      const sid = String(body.session_id || 'active');
      setLiveSessionId(sid);
      const initRows = Array.isArray(body.results) ? body.results : Array.isArray(body.rows) ? body.rows : null;
      if (initRows) setRows(initRows);
      if (Array.isArray(body.columns)) setColumns(body.columns);

      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(withAuthTokenQuery(`${proto}//${window.location.host}/ws/live-screener`));
      wsRef.current = ws;
      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          const snapRows = Array.isArray(msg.results) ? msg.results : Array.isArray(msg.rows) ? msg.rows : null;
          if (
            (msg.type === 'snapshot' || msg.type === 'update' || msg.type === 'live_screen:state' || msg.type === 'live_screen:snapshot') &&
            snapRows
          ) {
            if (msg.type === 'update' || msg.type === 'live_screen:state') setLiveTickCount((c) => c + 1);
            setRows(snapRows);
            setScannedAt(msg.ts_ist || msg.updated_at_ist || msg.last_tick_at || new Date().toISOString());
            setLiveStatusText(`Live WS connected · ${msg.feed_mode || msg.source || 'streaming'}`);
          } else if ((msg.type === 'tick' || msg.type === 'live_screen:tick') && msg.row) {
            setLiveTickCount((c) => c + 1);
            const updated = msg.row as ScannerResultRow;
            setRows((prev) => {
              const idx = prev.findIndex((r) => r.symbol === updated.symbol);
              if (updated.matched === false) {
                return idx >= 0 ? prev.filter((_, i) => i !== idx) : prev;
              }
              if (idx >= 0) {
                const next = [...prev];
                next[idx] = { ...next[idx], ...updated };
                return next;
              }
              return [updated, ...prev];
            });
          } else if (msg.type === 'status' && msg.message) {
            setLiveStatusText(String(msg.message));
          }
        } catch {
          // ignore parse errors
        }
      };
      ws.onerror = () => setLiveStatusText('Live WebSocket encountered a connection issue.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Live WS start failed.');
      setLiveStatusText('');
    }
  };

  const handleCopyToBuilder = (item: UnifiedScannerItem, isCopy = true) => {
    onOpenInCreateScanner({
      mode: item.engine,
      rule: item.rule ? JSON.parse(JSON.stringify(item.rule)) : undefined,
      pythonScreenId: item.engine === 'python' ? item.id : undefined,
      name: isCopy ? `${item.name} (Copy)` : item.name,
      description: item.description,
      universe: parseScanTarget().universe,
      basketId: parseScanTarget().basket_id,
      timeframe: item.timeframe,
      chartType: item.rule?.chart_type || 'candlestick',
      isCopy,
    });
  };

  const handleSaveAlert = async () => {
    if (!selected) return;
    setAlertBusy(true);
    setAlertFeedback('');
    try {
      const target = parseScanTarget();
      const res = await apiFetch('/api/streak/scanners/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: selected.name,
          description: selected.description,
          mode: selected.engine,
          screen_id: selected.engine === 'python' ? selected.id : `visual:${selected.id}`,
          rule_def: selected.engine !== 'python' ? selected.rule : null,
          universe: target.universe,
          basket_id: target.basket_id || null,
          exchange: 'NSE',
          timeframe: selected.timeframe || '1day',
          chart_type: selected.rule?.chart_type || 'candlestick',
          frequency_sec: alertFreqSec,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Failed to subscribe to alerts.');
      setAlertFeedback('Alert created & initial scan completed.');
      window.setTimeout(() => {
        setAlertModalOpen(false);
        setAlertFeedback('');
        onNavigateAlerts();
      }, 650);
    } catch (err) {
      setAlertFeedback(err instanceof Error ? err.message : 'Failed to create alert.');
    } finally {
      setAlertBusy(false);
    }
  };

  const handleDeleteSaved = async (ruleId: string) => {
    await apiFetch(`/api/streak/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' }).catch(() => {});
    onRefreshMeta();
  };

  return (
    <div className="space-y-4">
      {/* Top Action Header matching Streak Live_Scanner_993d056a.png */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-100">Discover Scanners</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            Pre-built Streak-style visual scanners &amp; custom Python (.py) screens with live WebSocket updates.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() =>
              onOpenInCreateScanner({
                mode: 'basic',
                name: 'Untitled Scanner',
                universe: 'nifty50',
                timeframe: '1day',
              })
            }
            className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
          >
            <Plus className="h-4 w-4" />
            <span>Create Scanner</span>
          </button>
          <button
            type="button"
            onClick={() => setMyScannersOpen(true)}
            className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-4 text-xs font-semibold text-sky-300 hover:border-sky-400"
          >
            <FolderOpen className="h-4 w-4" />
            <span>My Scanners ({mySavedScanners.length})</span>
          </button>
        </div>
      </div>

      {/* Main Split-Pane Layout matching Streak Discover Scanners */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Left List Pane (4 cols) */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827] lg:col-span-4">
          <div className="space-y-3 border-b border-slate-800 p-3.5">
            {/* Search & Scan On Universe selector */}
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search scanners"
                  aria-label="Search scanners"
                  className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] pl-8 pr-2.5 text-xs text-slate-100 placeholder-slate-500 focus:border-sky-400 focus:outline-none"
                />
              </div>

              <select
                value={scanOnKey}
                onChange={(e) => setScanOnKey(e.target.value)}
                aria-label="Scan On Universe or Basket"
                className="min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <optgroup label="NSE Universes">
                  {(meta.universes || []).map((u) => (
                    <option key={u.id} value={`universe:${u.id}`}>
                      {u.label} ({u.count})
                    </option>
                  ))}
                </optgroup>
                {(meta.baskets || []).length > 0 && (
                  <optgroup label="My Baskets">
                    {meta.baskets.map((b) => (
                      <option key={b.id} value={`basket:${b.id}`}>
                        Basket: {b.name} ({b.symbols.length})
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>

            {/* Timeframe Filter Tabs */}
            <div className="flex items-center gap-1 overflow-x-auto pb-0.5">
              {TF_TABS.map((tab) => {
                const active = activeTab === tab.id;
                return (
                  <button
                    key={tab.id}
                    type="button"
                    onClick={() => setActiveTab(tab.id)}
                    className={`min-h-[28px] shrink-0 whitespace-nowrap rounded-md px-2.5 text-xs font-semibold transition-colors ${
                      active
                        ? 'bg-sky-500/20 text-sky-300'
                        : 'text-slate-400 hover:bg-slate-800/80 hover:text-slate-200'
                    }`}
                  >
                    {tab.label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Scrollable Scanners List */}
          <div className="max-h-[640px] flex-1 divide-y divide-slate-800/70 overflow-y-auto">
            {filteredScanners.length === 0 ? (
              <div className="p-6 text-center text-xs text-slate-500">
                No scanners match this filter.
              </div>
            ) : (
              filteredScanners.map((item) => {
                const isSel = selected?.key === item.key;
                return (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setSelectedKey(item.key)}
                    className={`flex w-full items-start justify-between gap-3 px-4 py-3.5 text-left transition-colors ${
                      isSel
                        ? 'bg-sky-950/40'
                        : 'hover:bg-slate-800/40'
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className={`truncate text-sm font-semibold ${isSel ? 'text-sky-300' : 'text-slate-100'}`}>
                          {item.name}
                        </span>
                        {item.engine === 'python' && (
                          <Code2 className="h-3.5 w-3.5 shrink-0 text-amber-400" />
                        )}
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs text-slate-400">{item.description}</p>
                    </div>
                    <div className="shrink-0 text-right font-mono text-[11px] text-slate-400">
                      <div>{item.timeframeLabel}</div>
                      <div className="mt-0.5 text-[10px] uppercase text-slate-500">
                        {item.engine === 'python' ? '.py' : item.engine}
                      </div>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right Detail + Results Pane (8 cols) */}
        <div className="flex flex-col gap-3 lg:col-span-8">
          {selected && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-[#111827] px-4 py-3.5">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-base font-bold text-slate-100">{selected.name}</h3>
                  <span className="font-mono text-xs text-slate-400">
                    · {selected.timeframeLabel} ·{' '}
                    {selected.engine === 'python' ? 'Python (.py) Engine' : `${selected.engine === 'advance' ? 'Advance' : 'Basic'} Visual`}
                  </span>
                </div>
                <p className="mt-0.5 text-xs text-slate-400">{selected.description}</p>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setConditionsModalOpen(true)}
                  className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
                >
                  <Eye className="h-3.5 w-3.5 text-sky-400" />
                  <span>View Conditions</span>
                </button>
                <button
                  type="button"
                  onClick={() => void runSelectedScan(selected)}
                  disabled={loading}
                  className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
                >
                  <Play className="h-3.5 w-3.5" />
                  <span>{loading ? 'Scanning...' : 'Run Scan'}</span>
                </button>
                {liveSessionId ? (
                  <button
                    type="button"
                    onClick={() => void stopLiveWs()}
                    className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg bg-red-600 px-3 text-xs font-semibold text-white hover:bg-red-500"
                  >
                    <Square className="h-3 w-3" />
                    <span>Stop Live WS</span>
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void startLiveWs()}
                    className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-emerald-700/70 bg-emerald-950/30 px-3 text-xs font-semibold text-emerald-300 hover:border-emerald-400"
                  >
                    <Radio className="h-3.5 w-3.5" />
                    <span>Live WS</span>
                  </button>
                )}
              </div>
            </div>
          )}

          <ScannerResultsTable
            rows={rows}
            columns={columns}
            loading={loading}
            error={error}
            scannedAt={scannedAt}
            isLive={Boolean(liveSessionId)}
            liveTickCount={liveTickCount}
            liveStatusText={liveStatusText}
            totalScanned={totalScanned}
            onQuickOrder={onQuickOrder}
            onOpenChart={onOpenChart}
            footerActions={
              selected ? (
                <>
                  <button
                    type="button"
                    onClick={() => handleCopyToBuilder(selected, true)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-sky-600/70 bg-[#0b1120] px-4 text-xs font-semibold text-sky-300 hover:border-sky-400 hover:bg-sky-950/40"
                  >
                    <Copy className="h-3.5 w-3.5" />
                    <span>Copy</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setAlertModalOpen(true)}
                    className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400"
                  >
                    <Bell className="h-3.5 w-3.5" />
                    <span>Set Alerts</span>
                  </button>
                </>
              ) : undefined
            }
          />
        </div>
      </div>

      {/* View Conditions Modal */}
      {conditionsModalOpen && selected && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="w-full max-w-lg rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3.5">
              <h3 className="text-sm font-semibold text-slate-100">Conditions — {selected.name}</h3>
              <button
                type="button"
                onClick={() => setConditionsModalOpen(false)}
                className="text-slate-400 hover:text-slate-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-3 p-5 text-xs">
              {selected.engine === 'python' ? (
                <div className="space-y-2">
                  <p className="text-slate-300">
                    Executed via Python plugin <span className="font-mono text-amber-300">{selected.pySpec?.source_file || `${selected.id}.py`}</span>
                  </p>
                  <p className="text-slate-400">{selected.description}</p>
                </div>
              ) : (
                <div className="space-y-1.5">
                  {summarizeGroup(selected.rule?.entry).map((line, i) => (
                    <div
                      key={i}
                      className="rounded-lg border border-slate-800 bg-[#0b1120] px-3 py-2 font-mono text-xs text-slate-200"
                    >
                      {line}
                    </div>
                  ))}
                </div>
              )}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => {
                    setConditionsModalOpen(false);
                    handleCopyToBuilder(selected, false);
                  }}
                  className="rounded-lg bg-sky-500 px-4 py-2 text-xs font-semibold text-slate-950 hover:bg-sky-400"
                >
                  Open in Create Scanner
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Set Alerts Modal */}
      {alertModalOpen && selected && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="w-full max-w-md rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3.5">
              <h3 className="text-sm font-semibold text-slate-100">Set Scanner Alerts — {selected.name}</h3>
              <button
                type="button"
                onClick={() => setAlertModalOpen(false)}
                className="text-slate-400 hover:text-slate-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-4 p-5 text-xs">
              <p className="text-slate-400">
                AlgoDesk will monitor <strong className="text-slate-200">{scanOnKey.replace(/^(universe|basket):/, '')}</strong> and record ENTERED / EXITED symbol transitions on the Scanner Alerts desk.
              </p>
              <div>
                <label className="mb-1.5 block font-semibold text-slate-300">Alert Check Frequency</label>
                <select
                  value={alertFreqSec}
                  onChange={(e) => setAlertFreqSec(Number(e.target.value))}
                  className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-100"
                >
                  <option value={60}>Every 1 Minute</option>
                  <option value={300}>Every 5 Minutes</option>
                  <option value={900}>Every 15 Minutes</option>
                  <option value={3600}>Every 1 Hour</option>
                </select>
              </div>
              {alertFeedback && <p className="font-medium text-emerald-400">{alertFeedback}</p>}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setAlertModalOpen(false)}
                  className="min-h-[36px] rounded-lg border border-slate-700 px-4 text-xs font-semibold text-slate-300"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void handleSaveAlert()}
                  disabled={alertBusy}
                  className="min-h-[36px] rounded-lg bg-sky-500 px-4 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
                >
                  {alertBusy ? 'Creating...' : 'Take Live & Set Alert'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* My Scanners Drawer/Modal */}
      {myScannersOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
              <h3 className="text-sm font-semibold text-slate-100">
                My Scanners ({mySavedScanners.length})
              </h3>
              <button
                type="button"
                onClick={() => setMyScannersOpen(false)}
                className="text-slate-400 hover:text-slate-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 divide-y divide-slate-800 overflow-y-auto">
              {mySavedScanners.map((r) => (
                <div key={r.id || r.name} className="flex items-center justify-between gap-3 px-5 py-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-slate-100">{r.name}</div>
                    <div className="mt-0.5 text-xs text-slate-400">
                      {formatTfLabel(r.timeframe)} · {r.builder_mode || 'basic'} · {r.universe || 'nifty50'}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setMyScannersOpen(false);
                        onOpenInCreateScanner({
                          mode: r.builder_mode === 'advance' ? 'advance' : 'basic',
                          rule: JSON.parse(JSON.stringify(r)),
                          name: r.name,
                          description: r.description,
                          universe: r.universe,
                          timeframe: r.timeframe,
                          chartType: r.chart_type || 'candlestick',
                        });
                      }}
                      className="rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 py-1.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
                    >
                      Edit
                    </button>
                    {r.id && !r.seeded && (
                      <button
                        type="button"
                        onClick={() => void handleDeleteSaved(r.id!)}
                        className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:border-red-700 hover:text-red-400"
                        title="Delete scanner"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
