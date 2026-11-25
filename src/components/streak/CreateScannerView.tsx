import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch, withAuthTokenQuery } from '../../utils/authClient';
import {
  Bell,
  Calendar,
  Compass,
  FolderOpen,
  Play,
  Radio,
  RefreshCw,
  Save,
  Square,
  Trash2,
  X,
} from 'lucide-react';
import type { QuickOrderTarget } from './QuickOrderModal';
import { ScannerResultsTable } from './ScannerResultsTable';
import { StreakConditionBuilder, type BuilderEngineMode } from './StreakConditionBuilder';
import type {
  ScannerColumnSpec,
  ScannerDraftPreset,
  ScannerResultRow,
  StreakMetaPayload,
  VisualConditionGroup,
  VisualRuleDef,
} from './types';

interface CreateScannerViewProps {
  meta: StreakMetaPayload;
  draft: ScannerDraftPreset | null;
  onRefreshMeta: () => void;
  onNavigateDiscover: () => void;
  onNavigateAlerts: () => void;
  onQuickOrder: (target: QuickOrderTarget) => void;
  onOpenChart: (symbol: string) => void;
}

const DEFAULT_GROUP: VisualConditionGroup = {
  logic: 'AND',
  conditions: [
    {
      left: { type: 'close', offset: 0 },
      operator: '>',
      right: { type: 'ema', period: 20, offset: 0 },
      enabled: true,
      timeframe: '1day',
    },
  ],
};

const TIMEFRAME_OPTIONS = [
  { id: '1minute', label: '1 Minute' },
  { id: '5minute', label: '5 Minutes' },
  { id: '15minute', label: '15 Minutes' },
  { id: '60minute', label: '1 Hour' },
  { id: '1day', label: '1 Day' },
];

export function CreateScannerView({
  meta,
  draft,
  onRefreshMeta,
  onNavigateDiscover,
  onNavigateAlerts,
  onQuickOrder,
  onOpenChart,
}: CreateScannerViewProps) {
  const [ruleId, setRuleId] = useState<string | undefined>(undefined);
  const [name, setName] = useState('Untitled Scanner');
  const [description, setDescription] = useState('');
  const [mode, setMode] = useState<BuilderEngineMode>('basic');
  const [scanOnKey, setScanOnKey] = useState('universe:DEFAULT_WATCHLIST');
  const [customSymbolsText, setCustomSymbolsText] = useState('');
  const [chartType, setChartType] = useState<'candlestick' | 'heikin_ashi'>('candlestick');
  const [timeframe, setTimeframe] = useState('1day');

  // Addition #2: Live/Latest vs Historical As-of Date
  const [scanDateMode, setScanDateMode] = useState<'latest' | 'as_of'>('latest');
  const [asOfDate, setAsOfDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [indicatorBackend, setIndicatorBackend] = useState('auto');

  // Visual conditions state
  const [group, setGroup] = useState<VisualConditionGroup>(DEFAULT_GROUP);

  // Addition #1: Python (.py) screen state
  const [selectedPythonId, setSelectedPythonId] = useState(meta.screens?.[0]?.id || 'support_resistance');
  const [pythonParams, setPythonParams] = useState<Record<string, unknown>>({});
  const [mtfTimeframes, setMtfTimeframes] = useState<string[]>(['5m', '15m', '1h', '1d', '1w']);

  // Results & Execution
  const [rows, setRows] = useState<ScannerResultRow[]>([]);
  const [columns, setColumns] = useState<ScannerColumnSpec[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exportingPy, setExportingPy] = useState(false);
  const [syncingNse, setSyncingNse] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [scannedAt, setScannedAt] = useState('');
  const [totalScanned, setTotalScanned] = useState<number | undefined>(undefined);

  // Live WS state
  const [liveSessionId, setLiveSessionId] = useState<string | null>(null);
  const [liveTickCount, setLiveTickCount] = useState(0);
  const [liveStatusText, setLiveStatusText] = useState('');
  const wsRef = useRef<WebSocket | null>(null);

  // My Scanners modal
  const [myScannersOpen, setMyScannersOpen] = useState(false);

  // Load draft preset when navigated from Discover Scanners / Copy / My Scanners
  useEffect(() => {
    if (!draft) return;
    setMode(draft.mode || 'basic');
    setName(draft.name || draft.rule?.name || 'Untitled Scanner');
    setDescription(draft.description || draft.rule?.description || '');
    setTimeframe(draft.timeframe || draft.rule?.timeframe || '1day');
    setChartType(draft.chartType || draft.rule?.chart_type || 'candlestick');
    if (draft.basketId) {
      setScanOnKey(`basket:${draft.basketId}`);
    } else if (draft.universe || draft.rule?.universe) {
      setScanOnKey(`universe:${draft.universe || draft.rule?.universe}`);
    }
    if (draft.rule) {
      setRuleId(draft.isCopy ? undefined : draft.rule.id);
      setGroup(
        draft.rule.entry?.conditions?.length
          ? JSON.parse(JSON.stringify(draft.rule.entry))
          : DEFAULT_GROUP
      );
    } else {
      setRuleId(undefined);
    }
    if (draft.pythonScreenId) {
      setSelectedPythonId(draft.pythonScreenId);
    }
  }, [draft]);

  // Populate default params when Python screen changes
  useEffect(() => {
    const spec = (meta.screens || []).find((s) => s.id === selectedPythonId);
    if (!spec?.params) return;
    const defaults: Record<string, unknown> = {};
    for (const p of spec.params) {
      defaults[p.name] = p.default;
    }
    setPythonParams(defaults);
  }, [selectedPythonId, meta.screens]);

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

  const buildScanTarget = () => {
    if (scanOnKey === 'custom') {
      const syms = customSymbolsText
        .split(/[\s,]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      return { universe: 'custom', symbols: syms, basket_id: undefined };
    }
    if (scanOnKey.startsWith('basket:')) {
      return { universe: 'DEFAULT_WATCHLIST', symbols: undefined, basket_id: scanOnKey.slice(7) };
    }
    return { universe: scanOnKey.replace(/^universe:/, '') || 'DEFAULT_WATCHLIST', symbols: undefined, basket_id: undefined };
  };

  const buildVisualRuleDef = (): VisualRuleDef => ({
    id: ruleId,
    kind: 'screen',
    mode: 'visual',
    builder_mode: mode === 'advance' ? 'advance' : 'basic',
    name: name.trim() || 'Untitled Scanner',
    description: description.trim() || 'Custom visual scanner',
    universe: scanOnKey.startsWith('universe:') ? scanOnKey.slice(9) : 'DEFAULT_WATCHLIST',
    basket_id: scanOnKey.startsWith('basket:') ? scanOnKey.slice(7) : undefined,
    timeframe,
    chart_type: chartType,
    entry: group,
  });

  const handleRunScan = async () => {
    await stopLiveWs();
    setLoading(true);
    setError('');
    setNotice('');
    try {
      const target = buildScanTarget();
      if (scanOnKey === 'custom' && (!target.symbols || target.symbols.length === 0)) {
        throw new Error('Enter at least one custom symbol (e.g. RELIANCE, TCS, INFY).');
      }
      const payload: Record<string, unknown> = {
        screen_id: mode === 'python' ? selectedPythonId : ruleId ? `visual:${ruleId}` : 'visual:inline',
        universe: target.universe,
        basket_id: target.basket_id,
        symbols: target.symbols,
        exchange: 'NSE',
        timeframe,
        backend: indicatorBackend,
        as_of_date: scanDateMode === 'as_of' ? asOfDate : undefined,
      };
      if (mode === 'python') {
        payload.params = pythonParams;
        const spec = (meta.screens || []).find((s) => s.id === selectedPythonId);
        if (spec?.supports_multi_timeframe || selectedPythonId === 'support_resistance') {
          payload.timeframes = mtfTimeframes;
        }
      } else {
        payload.rule_def = buildVisualRuleDef();
      }
      const res = await apiFetch('/api/analytics/screener', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Scanner run failed.');
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
  };

  const handleStartLiveWs = async () => {
    if (scanDateMode === 'as_of') {
      setScanDateMode('latest');
    }
    await stopLiveWs();
    setError('');
    setLiveTickCount(0);
    setLiveStatusText('Starting live WebSocket scanner...');
    try {
      const target = buildScanTarget();
      const res = await apiFetch('/api/analytics/live-screener/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          screen_id: mode === 'python' ? selectedPythonId : ruleId ? `visual:${ruleId}` : 'visual:inline',
          rule_def: mode !== 'python' ? buildVisualRuleDef() : undefined,
          params: mode === 'python' ? pythonParams : undefined,
          universe: target.universe,
          basket_id: target.basket_id,
          symbols: target.symbols,
          exchange: 'NSE',
          timeframe,
          backend: indicatorBackend,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Live WebSocket failed to start.');
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
            setLiveStatusText(`Live WS active · ${msg.feed_mode || msg.source || 'streaming'}`);
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
          }
        } catch {
          // ignore
        }
      };
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Live WS failed.');
      setLiveStatusText('');
    }
  };

  const handleSaveScanner = async (): Promise<VisualRuleDef | null> => {
    if (mode === 'python') {
      setNotice('Python (.py) plugins are saved directly inside the Python code editor above.');
      return null;
    }
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const ruleDef = buildVisualRuleDef();
      const res = await apiFetch('/api/streak/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(ruleDef),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not save scanner.');
      const saved = body.rule as VisualRuleDef;
      setRuleId(saved.id);
      setNotice(`Saved "${saved.name}" to My Scanners.`);
      onRefreshMeta();
      return saved;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.');
      return null;
    } finally {
      setSaving(false);
    }
  };

  const handleExportToPython = async () => {
    setExportingPy(true);
    setError('');
    try {
      const saved = await handleSaveScanner();
      if (!saved?.id) return;
      const res = await apiFetch('/api/streak/rules/export-python', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rule_def: saved, save_to_disk: true }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Export to .py failed.');
      setNotice(`Exported Python screen: ${body.saved_path || body.filename || body.file || body.module_id || 'saved'}`);
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed.');
    } finally {
      setExportingPy(false);
    }
  };

  const handleSetAlert = async () => {
    setError('');
    setNotice('');
    try {
      const target = buildScanTarget();
      const res = await apiFetch('/api/streak/scanners/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim() || 'Untitled Scanner',
          description: description.trim() || 'Scanner alert subscription',
          mode,
          screen_id: mode === 'python' ? selectedPythonId : ruleId ? `visual:${ruleId}` : 'visual:inline',
          rule_def: mode !== 'python' ? buildVisualRuleDef() : null,
          params: mode === 'python' ? pythonParams : {},
          universe: target.universe,
          basket_id: target.basket_id || null,
          symbols: target.symbols || [],
          exchange: 'NSE',
          timeframe,
          chart_type: chartType,
          frequency_sec: 300,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Could not create scanner alert.');
      setNotice('Scanner alert created! Opening Scanner Alerts...');
      window.setTimeout(() => onNavigateAlerts(), 600);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Alert setup failed.');
    }
  };

  const handleSyncNse = async () => {
    setSyncingNse(true);
    try {
      await apiFetch('/api/analytics/universes/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      onRefreshMeta();
      setNotice('Synced NSE Index constituents.');
    } finally {
      setSyncingNse(false);
    }
  };

  const backendOptions = useMemo(() => {
    const raw = meta.indicator_backends;
    const keys = Array.isArray(raw) ? raw : raw && typeof raw === 'object' ? Object.keys(raw) : ['builtin'];
    return ['auto', ...keys].filter((v, i, arr) => arr.indexOf(v) === i);
  }, [meta.indicator_backends]);

  const mySavedScanners = useMemo(
    () => (meta.visual_rules || []).filter((r) => r.kind === 'screen'),
    [meta.visual_rules]
  );

  return (
    <div className="space-y-4">
      {/* Top Bar matching Streak Create_scanner_advance_8da0abb1.png */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Scanner name"
            className="min-h-[40px] w-full max-w-md rounded-lg border border-transparent bg-transparent px-2 text-xl font-bold tracking-tight text-slate-100 hover:border-slate-700 focus:border-sky-400 focus:bg-[#111827] focus:outline-none"
          />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onNavigateDiscover}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3.5 text-xs font-semibold text-slate-200 hover:border-sky-400"
          >
            <Compass className="h-3.5 w-3.5 text-sky-400" />
            <span>Discover</span>
          </button>
          <button
            type="button"
            onClick={() => setMyScannersOpen(true)}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-3.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
          >
            <FolderOpen className="h-3.5 w-3.5" />
            <span>My Scanners</span>
          </button>
        </div>
      </div>

      {/* Main Split-Pane: Left Conditions Builder (5 cols) + Right Scanner Results (7 cols) */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        {/* Left Builder Card */}
        <div className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#111827] xl:col-span-5">
          <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3.5">
            <h3 className="text-sm font-semibold text-slate-100">Scanner Conditions</h3>
            <button
              type="button"
              onClick={() => void handleSyncNse()}
              disabled={syncingNse}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-400 hover:text-sky-300"
            >
              <RefreshCw className={`h-3 w-3 ${syncingNse ? 'animate-spin' : ''}`} />
              <span>Sync NSE CSV</span>
            </button>
          </div>

          <div className="flex-1 space-y-5 p-5">
            {/* Row 1: Scan On + Chart Settings */}
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 block text-xs font-semibold text-slate-300">Scan On</label>
                <select
                  value={scanOnKey}
                  onChange={(e) => setScanOnKey(e.target.value)}
                  className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
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
                  <option value="custom">Custom Symbols...</option>
                </select>
                {scanOnKey === 'custom' && (
                  <input
                    type="text"
                    value={customSymbolsText}
                    onChange={(e) => setCustomSymbolsText(e.target.value)}
                    placeholder="RELIANCE, TCS, HDFCBANK, INFY"
                    className="mt-2 min-h-[36px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
                  />
                )}
              </div>

              <div>
                <label className="mb-1.5 block text-xs font-semibold text-slate-300">Chart Settings</label>
                <div className="grid grid-cols-2 gap-2">
                  <select
                    value={chartType}
                    onChange={(e) => setChartType(e.target.value as 'candlestick' | 'heikin_ashi')}
                    aria-label="Chart Type"
                    className="min-h-[38px] rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    <option value="candlestick">Candlestick</option>
                    <option value="heikin_ashi">Heikin-Ashi</option>
                  </select>
                  <select
                    value={timeframe}
                    onChange={(e) => setTimeframe(e.target.value)}
                    aria-label="Candle Timeframe"
                    className="min-h-[38px] rounded-lg border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
                  >
                    {TIMEFRAME_OPTIONS.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>

            {/* Row 2: Addition #2 — Scan Date Mode (Live/Latest vs As-of Date) & Indicator Engine */}
            <div className="grid grid-cols-1 gap-3 rounded-lg border border-slate-800 bg-[#0b1120]/70 p-3 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-slate-300">
                  <Calendar className="h-3.5 w-3.5 text-sky-400" />
                  <span>Scan Evaluation Date</span>
                </label>
                <div className="flex items-center gap-1.5">
                  <div className="inline-flex rounded-md border border-slate-800 bg-[#111827] p-0.5">
                    <button
                      type="button"
                      onClick={() => setScanDateMode('latest')}
                      className={`min-h-[28px] rounded px-2.5 text-[11px] font-semibold ${
                        scanDateMode === 'latest'
                          ? 'bg-sky-500 text-slate-950'
                          : 'text-slate-400 hover:text-slate-100'
                      }`}
                    >
                      Live / Latest
                    </button>
                    <button
                      type="button"
                      onClick={() => setScanDateMode('as_of')}
                      className={`min-h-[28px] rounded px-2.5 text-[11px] font-semibold ${
                        scanDateMode === 'as_of'
                          ? 'bg-amber-400 text-slate-950'
                          : 'text-slate-400 hover:text-slate-100'
                      }`}
                    >
                      As-of Date
                    </button>
                  </div>
                  {scanDateMode === 'as_of' && (
                    <input
                      type="date"
                      value={asOfDate}
                      onChange={(e) => setAsOfDate(e.target.value)}
                      aria-label="As-of date"
                      className="min-h-[30px] flex-1 rounded-md border border-amber-600/70 bg-[#111827] px-2 font-mono text-xs text-amber-200 focus:outline-none"
                    />
                  )}
                </div>
              </div>

              <div>
                <label className="mb-1.5 block text-[11px] font-semibold text-slate-300">
                  Indicator Engine
                </label>
                <select
                  value={indicatorBackend}
                  onChange={(e) => setIndicatorBackend(e.target.value)}
                  className="min-h-[32px] w-full rounded-md border border-slate-700 bg-[#111827] px-2.5 text-xs text-slate-200 focus:border-sky-400 focus:outline-none"
                >
                  {backendOptions.map((b) => (
                    <option key={b} value={b}>
                      {b === 'auto' ? 'Auto (TA-Lib -> pandas-ta -> O(N))' : b}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Row 3: Addition #1 — Basic / Advance / Python (.py) Condition Builder */}
            <div className="border-t border-slate-800 pt-4">
              <StreakConditionBuilder
                kind="screen"
                mode={mode}
                onModeChange={setMode}
                group={group}
                onGroupChange={setGroup}
                defaultTimeframe={timeframe}
                operands={meta.visual_operands}
                operators={meta.visual_operators}
                pythonPlugins={meta.screens || []}
                selectedPythonId={selectedPythonId}
                onSelectPythonId={setSelectedPythonId}
                pythonParams={pythonParams}
                onPythonParamsChange={setPythonParams}
                mtfTimeframes={mtfTimeframes}
                onMtfTimeframesChange={setMtfTimeframes}
                onRefreshPlugins={onRefreshMeta}
                onExportVisualToPython={handleExportToPython}
                exportingPython={exportingPy}
              />
            </div>

            {notice && (
              <div className="rounded-lg border border-emerald-800/70 bg-emerald-950/40 px-3.5 py-2 text-xs text-emerald-200">
                {notice}
              </div>
            )}
          </div>

          {/* Bottom Action Bar matching Streak Create Scanner */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800 bg-[#0b1120]/80 px-5 py-3.5">
            <button
              type="button"
              onClick={() => void handleSetAlert()}
              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-slate-700 bg-[#111827] px-3 text-xs font-semibold text-slate-200 hover:border-sky-400"
            >
              <Bell className="h-3.5 w-3.5 text-sky-400" />
              <span>Set Alerts</span>
            </button>

            <div className="flex flex-wrap items-center gap-2">
              {mode !== 'python' && (
                <button
                  type="button"
                  onClick={() => void handleSaveScanner()}
                  disabled={saving}
                  className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-sky-700/70 bg-sky-950/30 px-4 text-xs font-semibold text-sky-300 hover:border-sky-400 disabled:opacity-50"
                >
                  <Save className="h-3.5 w-3.5" />
                  <span>{saving ? 'Saving...' : 'Save'}</span>
                </button>
              )}

              {liveSessionId ? (
                <button
                  type="button"
                  onClick={() => void stopLiveWs()}
                  className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-red-600 px-4 text-xs font-semibold text-white hover:bg-red-500"
                >
                  <Square className="h-3.5 w-3.5" />
                  <span>Stop Live</span>
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void handleStartLiveWs()}
                  title="Stream real-time ticks over WebSocket"
                  className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-emerald-700/70 bg-emerald-950/30 px-3.5 text-xs font-semibold text-emerald-300 hover:border-emerald-400"
                >
                  <Radio className="h-3.5 w-3.5" />
                  <span>Live WS</span>
                </button>
              )}

              <button
                type="button"
                onClick={() => void handleRunScan()}
                disabled={loading}
                className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-sky-500 px-5 text-xs font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                <span>{loading ? 'Scanning...' : 'Run Scan'}</span>
              </button>
            </div>
          </div>
        </div>

        {/* Right Results Table Card */}
        <div className="xl:col-span-7">
          <ScannerResultsTable
            rows={rows}
            columns={columns}
            loading={loading}
            error={error}
            scannedAt={scannedAt}
            asOfDate={scanDateMode === 'as_of' ? asOfDate : undefined}
            isLive={Boolean(liveSessionId)}
            liveTickCount={liveTickCount}
            liveStatusText={liveStatusText}
            totalScanned={totalScanned}
            onQuickOrder={onQuickOrder}
            onOpenChart={onOpenChart}
          />
        </div>
      </div>

      {/* My Scanners Modal */}
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
                      {r.timeframe || '1day'} · {r.builder_mode || 'basic'} · {r.universe || 'nifty50'}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setRuleId(r.id);
                        setName(r.name);
                        setDescription(r.description || '');
                        setMode(r.builder_mode === 'advance' ? 'advance' : 'basic');
                        setTimeframe(r.timeframe || '1day');
                        setChartType(r.chart_type || 'candlestick');
                        setScanOnKey(`universe:${r.universe || 'nifty50'}`);
                        setGroup(r.entry ? JSON.parse(JSON.stringify(r.entry)) : DEFAULT_GROUP);
                        setMyScannersOpen(false);
                      }}
                      className="rounded-lg border border-sky-700/70 bg-sky-950/30 px-3 py-1.5 text-xs font-semibold text-sky-300 hover:border-sky-400"
                    >
                      Load
                    </button>
                    {r.id && !r.seeded && (
                      <button
                        type="button"
                        onClick={async () => {
                          await apiFetch(`/api/streak/rules/${encodeURIComponent(r.id!)}`, { method: 'DELETE' });
                          onRefreshMeta();
                        }}
                        className="rounded-lg border border-slate-700 p-1.5 text-slate-400 hover:border-red-700 hover:text-red-400"
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
