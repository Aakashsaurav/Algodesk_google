import { useEffect, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import { Play, Rocket, Layers, Code2, Blocks, X, Check } from 'lucide-react';
import { VisualRuleBuilder } from './VisualRuleBuilder';
import type {
  BacktestFullResult,
  BacktestRunSummary,
  DeploySpec,
  StreakMetaPayload,
  VisualRuleDef,
} from './types';

interface Props {
  meta: StreakMetaPayload | null;
  initialPreset?: {
    mode: 'visual' | 'python';
    strategyId: string;
    symbols?: string[];
    timeframe?: string;
    segment?: string;
  } | null;
  onRefreshMeta: () => void;
  onDeployed: () => void;
}

const DEFAULT_VISUAL_RULE: VisualRuleDef = {
  id: '',
  kind: 'strategy',
  mode: 'visual',
  name: 'EMA 9/21 Crossover + RSI Filter',
  description: 'Visual strategy: Enter on EMA(9) crossing above EMA(21) with RSI(14) > 52.',
  timeframe: '1day',
  segment: 'equity_intraday',
  stop_loss_pct: 2.5,
  take_profit_pct: 6.0,
  trailing_sl_pct: 0.0,
  entry: {
    logic: 'AND',
    conditions: [
      { left: { type: 'ema', period: 9, multiplier: 1, offset: 0 }, operator: 'crosses_above', right: { type: 'ema', period: 21, multiplier: 1, offset: 0 } },
      { left: { type: 'rsi', period: 14, multiplier: 1, offset: 0 }, operator: '>', right: { type: 'number', value: 52 } },
    ],
  },
  exit: {
    logic: 'OR',
    conditions: [
      { left: { type: 'ema', period: 9, multiplier: 1, offset: 0 }, operator: 'crosses_below', right: { type: 'ema', period: 21, multiplier: 1, offset: 0 } },
    ],
  },
};

const fmtInr = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function StrategyBacktestStudio({ meta, initialPreset, onRefreshMeta, onDeployed }: Props) {
  const [authorMode, setAuthorMode] = useState<'visual' | 'python'>(initialPreset?.mode ?? 'visual');
  const [selectedPythonId, setSelectedPythonId] = useState(initialPreset?.mode === 'python' ? initialPreset.strategyId : 'ema_crossover');
  const [pythonParams, setPythonParams] = useState<Record<string, number | string | boolean>>({});
  const [visualRule, setVisualRule] = useState<VisualRuleDef>(DEFAULT_VISUAL_RULE);

  // Backtest configuration
  const [btMode, setBtMode] = useState<'portfolio' | 'individual'>('portfolio');
  const [symbolSourceType, setSymbolSourceType] = useState<'custom' | 'basket' | 'universe'>('custom');
  const [customSymbolsText, setCustomSymbolsText] = useState('RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK');
  const [selectedBasketId, setSelectedBasketId] = useState('');
  const [selectedUniverse, setSelectedUniverse] = useState('NIFTY50');
  const [timeframe, setTimeframe] = useState('1day');
  const [fromDate, setFromDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().slice(0, 10);
  });
  const [toDate, setToDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [initialCapital, setInitialCapital] = useState(500000);
  const [allocationMode, setAllocationMode] = useState<'shared_pool' | 'equal_split' | 'risk_based'>('shared_pool');
  const [maxOpenPositions, setMaxOpenPositions] = useState(5);
  const [riskPerTradePct, setRiskPerTradePct] = useState(1.5);
  const [signalRanking, setSignalRanking] = useState<'momentum' | 'score' | 'alphabetical'>('momentum');
  const [slPct, setSlPct] = useState(3.0);
  const [tpPct, setTpPct] = useState(7.0);
  const [tslPct, setTslPct] = useState(0.0);

  // Real Indian Trading Charges
  const [chargesProfile, setChargesProfile] = useState<'zerodha' | 'upstox' | 'zero' | 'custom'>('zerodha');
  const [segment, setSegment] = useState<'equity_intraday' | 'equity_delivery' | 'equity_futures' | 'equity_options'>('equity_intraday');
  const [slippageBps, setSlippageBps] = useState(5);
  const [customBrokerageFlat, setCustomBrokerageFlat] = useState(20);
  const [customSttSellPct, setCustomSttSellPct] = useState(0.025);

  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<BacktestFullResult | null>(null);
  const [resultTab, setResultTab] = useState<'overview' | 'symbols' | 'trades' | 'charges'>('overview');

  // Deploy modal state
  const [deploySpec, setDeploySpec] = useState<DeploySpec | null>(null);
  const [deployName, setDeployName] = useState('');
  const [deployExecMode, setDeployExecMode] = useState<'paper' | 'live_alert' | 'live_auto'>('paper');
  const [deploying, setDeploying] = useState(false);
  const [deployMsg, setDeployMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!meta) return;
    if (!selectedBasketId && meta.baskets.length > 0) {
      setSelectedBasketId(meta.baskets[0].id);
    }
  }, [meta, selectedBasketId]);

  useEffect(() => {
    if (!initialPreset || !meta) return;
    setAuthorMode(initialPreset.mode);
    if (initialPreset.timeframe) setTimeframe(initialPreset.timeframe);
    if (initialPreset.segment) setSegment(initialPreset.segment as typeof segment);
    if (initialPreset.symbols?.length) {
      setSymbolSourceType('custom');
      setCustomSymbolsText(initialPreset.symbols.join(', '));
    }
    if (initialPreset.mode === 'python') {
      setSelectedPythonId(initialPreset.strategyId);
    } else {
      const found = meta.visual_rules.find((r) => r.id === initialPreset.strategyId);
      if (found) setVisualRule(found);
    }
  }, [initialPreset, meta]);

  const activePythonSpec = meta?.strategy_specs.find((s) => s.id === selectedPythonId);
  const visualStrategies = (meta?.visual_rules || []).filter((r) => r.kind === 'strategy');

  const handleRunBacktest = async () => {
    setRunning(true);
    setError('');
    try {
      const parsedSyms = customSymbolsText
        .split(/[,\s]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);

      const overrides: Record<string, number> = { slippage_bps: slippageBps };
      if (chargesProfile === 'custom') {
        overrides.brokerage_flat_per_order = customBrokerageFlat;
        overrides.stt_sell_pct = customSttSellPct / 100.0;
      }

      const payload: Record<string, unknown> = {
        mode: btMode,
        exchange: 'NSE',
        strategy: authorMode === 'python' ? selectedPythonId : (visualRule.id || 'visual_inline'),
        rule_def: authorMode === 'visual' ? visualRule : undefined,
        params: authorMode === 'python' ? pythonParams : undefined,
        timeframe,
        from_date: fromDate,
        to_date: toDate,
        initial_capital: initialCapital,
        allocation_mode: allocationMode,
        max_open_positions: maxOpenPositions,
        risk_per_trade_pct: riskPerTradePct,
        signal_ranking: signalRanking,
        stop_loss_pct: slPct,
        take_profit_pct: tpPct,
        trailing_sl_pct: tslPct,
        charges_profile: chargesProfile === 'custom' ? 'zerodha' : chargesProfile,
        segment,
        charges_overrides: overrides,
        slippage_bps: slippageBps,
      };
      if (symbolSourceType === 'basket') {
        payload.basket_id = selectedBasketId;
      } else if (symbolSourceType === 'universe') {
        payload.universe = selectedUniverse;
      } else {
        payload.symbols = parsedSyms;
      }

      const res = await apiFetch('/api/streak/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Backtest failed.');
      setResult(body as BacktestFullResult);
      setResultTab('overview');
      onRefreshMeta();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backtest failed.');
    } finally {
      setRunning(false);
    }
  };

  const openDeployModal = (spec: DeploySpec) => {
    setDeploySpec(spec);
    setDeployName(`${spec.strategy_name || spec.strategy_id} · ${spec.timeframe}`);
    setDeployExecMode('paper');
    setDeployMsg(null);
  };

  const handleConfirmDeploy = async () => {
    if (!deploySpec) return;
    setDeploying(true);
    setDeployMsg(null);
    try {
      const res = await apiFetch('/api/streak/deployments/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...deploySpec,
          name: deployName.trim() || undefined,
          exec_mode: deployExecMode,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Deployment failed.');
      setDeployMsg({ ok: true, text: `Deployed "${body.deployment?.name}" in ${deployExecMode.toUpperCase()} mode.` });
      onRefreshMeta();
      setTimeout(() => {
        setDeploySpec(null);
        onDeployed();
      }, 700);
    } catch (err) {
      setDeployMsg({ ok: false, text: err instanceof Error ? err.message : 'Deployment failed.' });
    } finally {
      setDeploying(false);
    }
  };

  const renderEquityCurve = () => {
    const pts = result?.equity_curve || [];
    if (pts.length < 2) return null;
    const W = 820;
    const H = 220;
    const padL = 68;
    const padR = 16;
    const padT = 16;
    const padB = 28;
    const vals = pts.map((p) => p.equity);
    const minY = Math.min(...vals, result!.initial_capital);
    const maxY = Math.max(...vals, result!.initial_capital);
    const spanY = Math.max(100, maxY - minY);

    const sx = (idx: number) => padL + (idx / Math.max(1, pts.length - 1)) * (W - padL - padR);
    const sy = (val: number) => padT + ((maxY - val) / spanY) * (H - padT - padB);
    const baseLineY = sy(result!.initial_capital);
    const linePath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${sx(i).toFixed(1)},${sy(p.equity).toFixed(1)}`).join(' ');
    const isUp = result!.net_pnl >= 0;

    return (
      <svg viewBox={`0 0 ${W} ${H}`} className="h-[220px] w-full">
        <rect x={padL} y={padT} width={W - padL - padR} height={H - padT - padB} fill="#090e1a" rx={6} />
        <line x1={padL} y1={baseLineY} x2={W - padR} y2={baseLineY} stroke="#475569" strokeDasharray="4 4" strokeWidth={1} />
        <path d={linePath} fill="none" stroke={isUp ? '#34d399' : '#f87171'} strokeWidth={2} />
        <text x={padL - 8} y={padT + 10} textAnchor="end" fill="#94a3b8" fontSize="10" fontFamily="IBM Plex Mono">
          {Math.round(maxY).toLocaleString('en-IN')}
        </text>
        <text x={padL - 8} y={baseLineY + 3} textAnchor="end" fill="#cbd5e1" fontSize="10" fontFamily="IBM Plex Mono">
          Base
        </text>
        <text x={padL - 8} y={H - padB} textAnchor="end" fill="#94a3b8" fontSize="10" fontFamily="IBM Plex Mono">
          {Math.round(minY).toLocaleString('en-IN')}
        </text>
        <text x={padL} y={H - 8} fill="#64748b" fontSize="10" fontFamily="IBM Plex Mono">
          {pts[0].date}
        </text>
        <text x={W - padR} y={H - 8} textAnchor="end" fill="#64748b" fontSize="10" fontFamily="IBM Plex Mono">
          {pts[pts.length - 1].date}
        </text>
      </svg>
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-100">Strategies &amp; Dual-Mode Backtester</h1>
          <p className="text-sm text-slate-400">
            Build strategies visually with condition blocks or run custom Python (.py) plugins — backtest in Individual or Portfolio mode with authentic 2026 Indian brokerage &amp; statutory charges, then promote straight to Paper or Live Trade.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void handleRunBacktest()}
            disabled={running}
            className="inline-flex h-10 items-center gap-2 rounded-lg bg-sky-400 px-5 text-xs font-semibold text-sky-950 hover:bg-sky-300 disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />
            {running ? 'Running Backtest...' : `Run ${btMode === 'portfolio' ? 'Portfolio' : 'Individual'} Backtest`}
          </button>
          {result?.deploy_spec && (
            <button
              type="button"
              onClick={() => openDeployModal(result.deploy_spec)}
              className="inline-flex h-10 items-center gap-2 rounded-lg border border-emerald-500/40 bg-emerald-500/15 px-4 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25"
            >
              <Rocket className="h-3.5 w-3.5" />
              Deploy Strategy
            </button>
          )}
        </div>
      </div>

      {/* Step 1: Strategy Authoring Mode (Visual Blocks OR Python Script) */}
      <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="inline-flex rounded-lg border border-slate-800 bg-[#0b1120] p-1">
            <button
              type="button"
              onClick={() => setAuthorMode('visual')}
              className={`inline-flex items-center gap-2 rounded-md px-3.5 py-1.5 text-xs font-semibold transition-colors ${
                authorMode === 'visual' ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Blocks className="h-3.5 w-3.5" />
              Visual Block Builder (Streak Style)
            </button>
            <button
              type="button"
              onClick={() => setAuthorMode('python')}
              className={`inline-flex items-center gap-2 rounded-md px-3.5 py-1.5 text-xs font-semibold transition-colors ${
                authorMode === 'python' ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Code2 className="h-3.5 w-3.5" />
              Python Script Plugin (.py)
            </button>
          </div>

          {authorMode === 'visual' && visualStrategies.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">Load saved:</span>
              <select
                value={visualRule.id || ''}
                onChange={(e) => {
                  const found = visualStrategies.find((r) => r.id === e.target.value);
                  if (found) setVisualRule(found);
                }}
                className="h-8 rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-200"
              >
                <option value="">New Visual Strategy</option>
                {visualStrategies.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        {authorMode === 'visual' ? (
          <VisualRuleBuilder
            rule={visualRule}
            onChange={setVisualRule}
            operands={meta?.visual_operands || []}
            operators={meta?.visual_operators || []}
            onSaved={() => onRefreshMeta()}
          />
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <label className="space-y-1">
                <span className="block text-xs font-semibold text-slate-300">Python Strategy Plugin (`strategies/*.py`)</span>
                <select
                  value={selectedPythonId}
                  onChange={(e) => {
                    setSelectedPythonId(e.target.value);
                    setPythonParams({});
                  }}
                  className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-100"
                >
                  {(meta?.strategy_specs || []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.id})
                    </option>
                  ))}
                </select>
                {activePythonSpec && (
                  <p className="text-xs text-slate-500">{activePythonSpec.description}</p>
                )}
              </label>
              {activePythonSpec && activePythonSpec.params.length > 0 && (
                <div className="grid grid-cols-2 gap-2.5">
                  {activePythonSpec.params.map((p) => (
                    <label key={p.name} className="space-y-1">
                      <span className="block text-[11px] font-medium text-slate-400">{p.label}</span>
                      <input
                        type="number"
                        step={p.step ?? 1}
                        value={Number(pythonParams[p.name] ?? p.default)}
                        onChange={(e) => setPythonParams((prev) => ({ ...prev, [p.name]: Number(e.target.value) }))}
                        className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums"
                      />
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Step 2: Backtest Scope, Portfolio Allocation & Real Indian Charges */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-200 flex items-center gap-2">
              <Layers className="h-4 w-4 text-sky-400" />
              Backtest Mode &amp; Capital Allocation
            </h3>
            <div className="inline-flex rounded-md border border-slate-700 bg-[#0b1120] p-0.5 text-xs">
              {(['portfolio', 'individual'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setBtMode(m)}
                  className={`rounded px-3 py-1 font-semibold capitalize ${
                    btMode === m ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {m} Mode
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Symbol Source</span>
              <select value={symbolSourceType} onChange={(e) => setSymbolSourceType(e.target.value as typeof symbolSourceType)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
                <option value="custom">Custom Symbols</option>
                <option value="basket">Saved Basket</option>
                <option value="universe">NSE CSV Universe</option>
              </select>
            </label>

            <div className="sm:col-span-2 space-y-1">
              <span className="block text-[11px] text-slate-400">
                {symbolSourceType === 'basket' ? 'Select Basket' : symbolSourceType === 'universe' ? 'Select NSE Universe' : 'Symbols (comma-separated)'}
              </span>
              {symbolSourceType === 'basket' ? (
                <select value={selectedBasketId} onChange={(e) => setSelectedBasketId(e.target.value)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-100">
                  {(meta?.baskets || []).map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name} ({b.symbols.length} symbols)
                    </option>
                  ))}
                </select>
              ) : symbolSourceType === 'universe' ? (
                <select value={selectedUniverse} onChange={(e) => setSelectedUniverse(e.target.value)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-100">
                  {(meta?.universes || []).filter((u) => u.id !== 'CUSTOM').map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.label} ({u.count})
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={customSymbolsText}
                  onChange={(e) => setCustomSymbolsText(e.target.value.toUpperCase())}
                  className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100"
                />
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Timeframe</span>
              <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
                <option value="5minute">5 min</option>
                <option value="15minute">15 min</option>
                <option value="60minute">1 hour</option>
                <option value="1day">1 day</option>
                <option value="1week">1 week</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">From Date</span>
              <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100" />
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">To Date</span>
              <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100" />
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">{btMode === 'portfolio' ? 'Portfolio Capital (₹)' : 'Capital / Symbol (₹)'}</span>
              <input type="number" step={10000} value={initialCapital} onChange={(e) => setInitialCapital(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
            </label>
          </div>

          {btMode === 'portfolio' && (
            <div className="grid grid-cols-1 gap-3 border-t border-slate-800 pt-3 sm:grid-cols-3">
              <label className="space-y-1">
                <span className="block text-[11px] text-slate-400">Allocation Mode</span>
                <select value={allocationMode} onChange={(e) => setAllocationMode(e.target.value as typeof allocationMode)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
                  <option value="shared_pool">Shared Pool + Ranking</option>
                  <option value="equal_split">Equal Capital Split</option>
                  <option value="risk_based">Risk-Based (% / SL)</option>
                </select>
              </label>
              <label className="space-y-1">
                <span className="block text-[11px] text-slate-400">Max Open Positions</span>
                <input type="number" min={1} max={50} value={maxOpenPositions} onChange={(e) => setMaxOpenPositions(Math.max(1, Number(e.target.value)))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
              </label>
              {allocationMode === 'risk_based' ? (
                <label className="space-y-1">
                  <span className="block text-[11px] text-slate-400">Risk / Trade (%)</span>
                  <input type="number" step="0.25" min={0.25} max={10} value={riskPerTradePct} onChange={(e) => setRiskPerTradePct(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
                </label>
              ) : (
                <label className="space-y-1">
                  <span className="block text-[11px] text-slate-400">Signal Ranking</span>
                  <select value={signalRanking} onChange={(e) => setSignalRanking(e.target.value as typeof signalRanking)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
                    <option value="momentum">Momentum (ROC 10)</option>
                    <option value="score">Signal Strength</option>
                    <option value="alphabetical">Alphabetical</option>
                  </select>
                </label>
              )}
            </div>
          )}
        </div>

        {/* Risk Exits + Real Indian Trading Charges */}
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-4">
          <h3 className="text-sm font-semibold text-slate-200">Risk Exits &amp; Real Indian Trading Charges (2026)</h3>
          <div className="grid grid-cols-3 gap-3">
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Stop Loss (%)</span>
              <input type="number" step="0.5" min={0} value={slPct} onChange={(e) => setSlPct(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Take Profit (%)</span>
              <input type="number" step="0.5" min={0} value={tpPct} onChange={(e) => setTpPct(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Trailing SL (%)</span>
              <input type="number" step="0.5" min={0} value={tslPct} onChange={(e) => setTslPct(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
            </label>
          </div>

          <div className="grid grid-cols-1 gap-3 border-t border-slate-800 pt-3 sm:grid-cols-3">
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Broker Charges Preset</span>
              <select value={chargesProfile} onChange={(e) => setChargesProfile(e.target.value as typeof chargesProfile)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
                <option value="zerodha">Zerodha (2026)</option>
                <option value="upstox">Upstox (2026)</option>
                <option value="zero">Zero Cost (Gross)</option>
                <option value="custom">Custom Overrides</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Trading Segment</span>
              <select value={segment} onChange={(e) => setSegment(e.target.value as typeof segment)} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2 text-xs text-slate-100">
                <option value="equity_intraday">Equity Intraday (MIS)</option>
                <option value="equity_delivery">Equity Delivery (CNC)</option>
                <option value="equity_futures">Equity Futures (NFO)</option>
                <option value="equity_options">Equity Options (NFO)</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="block text-[11px] text-slate-400">Slippage (bps / leg)</span>
              <input type="number" step="1" min={0} value={slippageBps} onChange={(e) => setSlippageBps(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
            </label>
          </div>

          {chargesProfile === 'custom' && (
            <div className="grid grid-cols-2 gap-3 pt-1">
              <label className="space-y-1">
                <span className="block text-[11px] text-slate-400">Brokerage Cap / Order (₹)</span>
                <input type="number" min={0} value={customBrokerageFlat} onChange={(e) => setCustomBrokerageFlat(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
              </label>
              <label className="space-y-1">
                <span className="block text-[11px] text-slate-400">STT Sell Rate (%)</span>
                <input type="number" step="0.005" min={0} value={customSttSellPct} onChange={(e) => setCustomSttSellPct(Number(e.target.value))} className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-2.5 font-mono text-xs text-slate-100 tabular-nums" />
              </label>
            </div>
          )}

          <p className="text-[11px] text-slate-500">
            Includes Brokerage, April 2026 STT, NSE Exchange Txn, SEBI ₹10/Cr, 18% GST (strictly on Brokerage+Exchange+SEBI), Buy-side Stamp Duty, Delivery DP charges, and Slippage.
          </p>
        </div>
      </div>

      {error && <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-4 py-3 text-xs text-red-300">{error}</div>}

      {/* Step 3: Backtest Results Workspace */}
      {result && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <p className="text-[11px] uppercase tracking-wider text-slate-500">Net P&amp;L</p>
              <p className={`mt-1 font-mono text-lg font-bold tabular-nums ${result.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {result.net_pnl >= 0 ? '+' : ''}{fmtInr(result.net_pnl)}
              </p>
              <p className="font-mono text-[11px] text-slate-500 tabular-nums">
                {result.total_return_pct >= 0 ? '+' : ''}
                {result.total_return_pct}% · CAGR{' '}
                {result.cagr_pct !== null && result.cagr_pct !== undefined ? `${result.cagr_pct}%` : 'N/A (<90d)'}
              </p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <p className="text-[11px] uppercase tracking-wider text-slate-500">Gross P&amp;L</p>
              <p className="mt-1 font-mono text-lg font-semibold text-slate-100 tabular-nums">{fmtInr(result.gross_pnl)}</p>
              <p className="font-mono text-[11px] text-slate-500">Before charges</p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <p className="text-[11px] uppercase tracking-wider text-slate-500">Real Charges</p>
              <p className="mt-1 font-mono text-lg font-semibold text-amber-300 tabular-nums">{fmtInr(result.total_charges)}</p>
              <p className="font-mono text-[11px] text-slate-500">{result.charges_profile.toUpperCase()} · {result.segment.replace('equity_', '')}</p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <p className="text-[11px] uppercase tracking-wider text-slate-500">Max Drawdown</p>
              <p className="mt-1 font-mono text-lg font-semibold text-red-400 tabular-nums">-{result.max_drawdown_pct}%</p>
              <p className="font-mono text-[11px] text-slate-500">Sharpe {result.sharpe_ratio}</p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4">
              <p className="text-[11px] uppercase tracking-wider text-slate-500">Win Rate</p>
              <p className="mt-1 font-mono text-lg font-semibold text-slate-100 tabular-nums">{result.win_rate_pct}%</p>
              <p className="font-mono text-[11px] text-slate-500">{result.total_trades} trades · PF {result.profit_factor}</p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-[#111827] p-4 flex flex-col justify-between">
              <p className="text-[11px] uppercase tracking-wider text-slate-500">Promote Artifact</p>
              <button
                type="button"
                onClick={() => openDeployModal(result.deploy_spec)}
                className="mt-2 inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-md bg-emerald-500/20 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/30"
              >
                <Rocket className="h-3.5 w-3.5" />
                Paper / Live
              </button>
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
              <div className="flex items-center gap-2 text-xs">
                {(['overview', 'symbols', 'trades', 'charges'] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setResultTab(t)}
                    className={`rounded-md px-3 py-1.5 font-semibold capitalize ${
                      resultTab === t ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {t === 'symbols' ? `Per-Symbol (${result.per_symbol.length})` : t === 'trades' ? `Trades (${result.trades.length})` : t === 'charges' ? 'Charges Breakdown' : 'Equity Curve'}
                  </button>
                ))}
              </div>
              <span className="font-mono text-xs text-slate-500">
                {result.mode.toUpperCase()} · {result.allocation_mode} · {result.from_date} → {result.to_date}
              </span>
            </div>

            {resultTab === 'overview' && renderEquityCurve()}

            {resultTab === 'symbols' && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                    <tr>
                      <th className="py-2 text-left">Symbol</th>
                      <th className="py-2 text-right">Trades</th>
                      <th className="py-2 text-right">Win %</th>
                      <th className="py-2 text-right">Gross P&amp;L</th>
                      <th className="py-2 text-right">Charges</th>
                      <th className="py-2 text-right">Net P&amp;L</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                    {result.per_symbol.map((r) => (
                      <tr key={r.symbol}>
                        <td className="py-2 font-sans font-semibold text-slate-200">{r.symbol}</td>
                        <td className="py-2 text-right text-slate-300">{r.trades}</td>
                        <td className="py-2 text-right text-slate-300">{r.win_rate_pct}%</td>
                        <td className="py-2 text-right text-slate-300">{fmtInr(r.gross_pnl)}</td>
                        <td className="py-2 text-right text-amber-300">{fmtInr(r.charges)}</td>
                        <td className={`py-2 text-right font-semibold ${r.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmtInr(r.net_pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {resultTab === 'trades' && (
              <div className="max-h-96 overflow-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 border-b border-slate-800 bg-[#111827] text-[11px] uppercase text-slate-500">
                    <tr>
                      <th className="py-2 text-left">Symbol</th>
                      <th className="py-2 text-left">Entry</th>
                      <th className="py-2 text-left">Exit</th>
                      <th className="py-2 text-right">Qty</th>
                      <th className="py-2 text-right">Entry ₹</th>
                      <th className="py-2 text-right">Exit ₹</th>
                      <th className="py-2 text-right">Charges</th>
                      <th className="py-2 text-right">Net P&amp;L</th>
                      <th className="py-2 text-left pl-3">Reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                    {result.trades.map((t, i) => (
                      <tr key={i}>
                        <td className="py-1.5 font-sans font-semibold text-slate-200">{t.symbol || result.symbols[0]}</td>
                        <td className="py-1.5 text-slate-400">{t.entry_date}</td>
                        <td className="py-1.5 text-slate-400">{t.exit_date}</td>
                        <td className="py-1.5 text-right text-slate-300">{t.qty}</td>
                        <td className="py-1.5 text-right text-slate-300">{t.entry_price}</td>
                        <td className="py-1.5 text-right text-slate-300">{t.exit_price}</td>
                        <td className="py-1.5 text-right text-amber-300">{t.charges}</td>
                        <td className={`py-1.5 text-right font-semibold ${t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmtInr(t.pnl)}</td>
                        <td className="py-1.5 pl-3 font-sans text-[11px] text-slate-400">{t.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {resultTab === 'charges' && (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 font-mono text-xs tabular-nums">
                {[
                  ['Total Turnover', result.charges_breakdown.turnover],
                  ['Brokerage', result.charges_breakdown.brokerage],
                  ['STT (2026)', result.charges_breakdown.stt],
                  ['Exchange Txn', result.charges_breakdown.exchange_txn],
                  ['SEBI Fee', result.charges_breakdown.sebi_fee],
                  ['GST (18%)', result.charges_breakdown.gst],
                  ['Stamp Duty', result.charges_breakdown.stamp_duty],
                  ['DP Charges', result.charges_breakdown.dp_charges],
                  ['Slippage', result.charges_breakdown.slippage_cost],
                  ['Total Deductions', result.charges_breakdown.total_charges],
                ].map(([label, val]) => (
                  <div key={String(label)} className="rounded-lg border border-slate-800 bg-[#0b1120] p-3">
                    <div className="font-sans text-[11px] text-slate-500">{label}</div>
                    <div className="mt-1 text-sm font-semibold text-slate-100">{fmtInr(Number(val))}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Recent Immutable Backtest Runs */}
      {(meta?.recent_backtests?.length ?? 0) > 0 && (
        <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">Recent Backtest Snapshots (1-Click Promote to Paper / Live)</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="border-b border-slate-800 text-[11px] uppercase text-slate-500">
                <tr>
                  <th className="py-2 text-left">Strategy</th>
                  <th className="py-2 text-left">Mode</th>
                  <th className="py-2 text-left">Symbols</th>
                  <th className="py-2 text-right">Net P&amp;L</th>
                  <th className="py-2 text-right">Charges</th>
                  <th className="py-2 text-right">Win %</th>
                  <th className="py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono tabular-nums">
                {(meta?.recent_backtests || []).slice(0, 8).map((r: BacktestRunSummary) => (
                  <tr key={r.run_id}>
                    <td className="py-2 font-sans font-semibold text-slate-200">
                      {r.strategy_label} <span className="text-[11px] font-normal text-slate-500">· {r.strategy_mode}</span>
                    </td>
                    <td className="py-2 font-sans text-slate-400">{r.mode} · {r.timeframe}</td>
                    <td className="py-2 font-sans text-slate-400">{r.symbols.slice(0, 3).join(', ')}{r.symbols.length > 3 ? ` +${r.symbols.length - 3}` : ''}</td>
                    <td className={`py-2 text-right font-semibold ${r.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmtInr(r.net_pnl)}</td>
                    <td className="py-2 text-right text-amber-300">{fmtInr(r.total_charges)}</td>
                    <td className="py-2 text-right text-slate-300">{r.win_rate_pct}%</td>
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        onClick={() => openDeployModal(r.deploy_spec)}
                        className="rounded border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 font-sans text-[11px] font-semibold text-emerald-300 hover:bg-emerald-500/20"
                      >
                        Deploy
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Deploy Modal */}
      {deploySpec && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4">
          <div className="w-full max-w-lg rounded-xl border border-slate-700 bg-[#111827] p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-base font-bold text-slate-100">Deploy Strategy to Paper / Live</h3>
              <button type="button" onClick={() => setDeploySpec(null)} className="rounded p-1 text-slate-400 hover:text-slate-200">
                <X className="h-4 w-4" />
              </button>
            </div>
            <p className="text-xs text-slate-400">
              Promoting exact backtested strategy <strong className="text-slate-200">{deploySpec.strategy_name || deploySpec.strategy_id}</strong> ({deploySpec.symbols.length} symbols · {deploySpec.timeframe} · {deploySpec.charges_profile.toUpperCase()} charges).
            </p>
            <label className="block space-y-1">
              <span className="text-xs font-semibold text-slate-300">Deployment Name</span>
              <input
                type="text"
                value={deployName}
                onChange={(e) => setDeployName(e.target.value)}
                className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-3 text-sm text-slate-100"
              />
            </label>
            <div className="space-y-2">
              <span className="block text-xs font-semibold text-slate-300">Execution Workflow</span>
              {([
                ['paper', 'Paper Trade (Virtual Fills + Real 2026 Charges)', 'Simulates live LTP fills with full brokerage/STT/GST deduction and live MTM P&L.'],
                ['live_alert', 'Live Trade — 1-Click Actionable Alerts (Streak Style)', 'Queues real-time order alerts on signal; you click Execute or Dismiss before any order hits your broker.'],
                ['live_auto', 'Live Trade — Gated Auto-Execute', 'Routes orders automatically to active Trading Broker ONLY if Kill Switch is OFF and Risk Limits pass.'],
              ] as const).map(([id, label, desc]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setDeployExecMode(id)}
                  className={`w-full rounded-lg border p-3 text-left transition-colors ${
                    deployExecMode === id ? 'border-sky-400 bg-sky-500/10' : 'border-slate-800 bg-[#0b1120] hover:border-slate-600'
                  }`}
                >
                  <div className="flex items-center justify-between text-xs font-semibold text-slate-100">
                    <span>{label}</span>
                    {deployExecMode === id && <Check className="h-3.5 w-3.5 text-sky-400" />}
                  </div>
                  <p className="mt-1 text-[11px] text-slate-400">{desc}</p>
                </button>
              ))}
            </div>

            {deployMsg && (
              <p className={`text-xs ${deployMsg.ok ? 'text-emerald-400' : 'text-red-400'}`}>{deployMsg.text}</p>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button type="button" onClick={() => setDeploySpec(null)} className="h-9 rounded-lg border border-slate-700 px-4 text-xs font-semibold text-slate-300 hover:bg-slate-800">
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleConfirmDeploy()}
                disabled={deploying}
                className="inline-flex h-9 items-center gap-2 rounded-lg bg-sky-400 px-4 text-xs font-semibold text-sky-950 hover:bg-sky-300 disabled:opacity-50"
              >
                <Rocket className="h-3.5 w-3.5" />
                {deploying ? 'Deploying...' : 'Confirm & Deploy'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
