import { useState } from 'react';
import { Plus, Trash2, Save, Code2, Check, Copy } from 'lucide-react';
import type { VisualCondition, VisualOperandSpec, VisualRuleDef } from './types';

interface Props {
  rule: VisualRuleDef;
  onChange: (next: VisualRuleDef) => void;
  operands: Array<{ id: string; label: string; kind: string; default_period: number | null }>;
  operators: Array<{ id: string; label: string }>;
  onSaved?: (saved: VisualRuleDef) => void;
}

function defaultOperand(type = 'close', defaultPeriod: number | null = null): VisualOperandSpec {
  if (type === 'number') return { type: 'number', value: 50, offset: 0 };
  if (defaultPeriod) return { type, period: defaultPeriod, multiplier: 1, offset: 0 };
  return { type, multiplier: 1, offset: 0 };
}

export function VisualRuleBuilder({ rule, onChange, operands, operators, onSaved }: Props) {
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportedCode, setExportedCode] = useState<string | null>(null);
  const [savedPath, setSavedPath] = useState<string>('');
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);

  const operandById = new Map(operands.map((o) => [o.id, o]));

  const updateCondition = (
    section: 'entry' | 'exit',
    idx: number,
    patch: Partial<VisualCondition>
  ) => {
    const group = rule[section] || { logic: 'AND' as const, conditions: [] };
    const nextConds = group.conditions.map((c, i) => (i === idx ? { ...c, ...patch } : c));
    onChange({ ...rule, [section]: { ...group, conditions: nextConds } });
  };

  const addCondition = (section: 'entry' | 'exit') => {
    const group = rule[section] || { logic: 'AND' as const, conditions: [] };
    const nextCond: VisualCondition =
      section === 'entry'
        ? {
            left: { type: 'ema', period: 9, multiplier: 1, offset: 0 },
            operator: 'crosses_above',
            right: { type: 'ema', period: 21, multiplier: 1, offset: 0 },
          }
        : {
            left: { type: 'ema', period: 9, multiplier: 1, offset: 0 },
            operator: 'crosses_below',
            right: { type: 'ema', period: 21, multiplier: 1, offset: 0 },
          };
    onChange({
      ...rule,
      [section]: { ...group, conditions: [...group.conditions, nextCond] },
    });
  };

  const removeCondition = (section: 'entry' | 'exit', idx: number) => {
    const group = rule[section] || { logic: 'AND' as const, conditions: [] };
    onChange({
      ...rule,
      [section]: { ...group, conditions: group.conditions.filter((_, i) => i !== idx) },
    });
  };

  const renderOperandEditor = (
    op: VisualOperandSpec,
    onUpdate: (next: VisualOperandSpec) => void
  ) => {
    const meta = operandById.get(op.type);
    const needsPeriod = meta ? meta.default_period !== null : ['ema', 'sma', 'rsi', 'atr', 'adx', 'vol_sma'].includes(op.type);
    const isNumber = op.type === 'number';
    return (
      <div className="flex flex-wrap items-center gap-1.5">
        <select
          value={op.type}
          onChange={(e) => {
            const nextType = e.target.value;
            const m = operandById.get(nextType);
            onUpdate(defaultOperand(nextType, m?.default_period ?? null));
          }}
          className="h-9 rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
        >
          {operands.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
        {isNumber ? (
          <input
            type="number"
            step="any"
            value={op.value ?? 0}
            onChange={(e) => onUpdate({ ...op, value: Number(e.target.value) })}
            className="h-9 w-24 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
            aria-label="Constant value"
          />
        ) : (
          <>
            {needsPeriod && (
              <label className="flex items-center gap-1 text-[11px] text-slate-400">
                <span>P</span>
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={op.period ?? meta?.default_period ?? 14}
                  onChange={(e) => onUpdate({ ...op, period: Math.max(1, Number(e.target.value)) })}
                  className="h-9 w-16 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
                />
              </label>
            )}
            <label className="flex items-center gap-1 text-[11px] text-slate-400">
              <span>×</span>
              <input
                type="number"
                step="0.1"
                value={op.multiplier ?? 1}
                onChange={(e) => onUpdate({ ...op, multiplier: Number(e.target.value) || 1 })}
                className="h-9 w-16 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
                title="Multiplier (e.g. 1.5 for 1.5x Volume SMA)"
              />
            </label>
            <label className="flex items-center gap-1 text-[11px] text-slate-400">
              <span>Off</span>
              <input
                type="number"
                min={0}
                max={20}
                value={op.offset ?? 0}
                onChange={(e) => onUpdate({ ...op, offset: Math.max(0, Number(e.target.value)) })}
                className="h-9 w-14 rounded-md border border-slate-700 bg-[#0b1120] px-2 font-mono text-xs text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
                title="Candle offset (0 = current bar, 1 = previous bar)"
              />
            </label>
          </>
        )}
      </div>
    );
  };

  const renderGroup = (section: 'entry' | 'exit', title: string) => {
    const group = rule[section] || { logic: 'AND' as const, conditions: [] };
    return (
      <div className="rounded-lg border border-slate-800 bg-[#0d1424] p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-300">{title}</h4>
            <div className="inline-flex rounded-md border border-slate-700 bg-[#0b1120] p-0.5 text-xs">
              {(['AND', 'OR'] as const).map((lg) => (
                <button
                  key={lg}
                  type="button"
                  onClick={() => onChange({ ...rule, [section]: { ...group, logic: lg } })}
                  className={`rounded px-2.5 py-1 font-semibold transition-colors ${
                    group.logic === lg ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {lg}
                </button>
              ))}
            </div>
          </div>
          <button
            type="button"
            onClick={() => addCondition(section)}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-800/70 px-2.5 py-1.5 text-xs font-medium text-slate-200 hover:border-sky-500/50 hover:text-sky-300"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Condition
          </button>
        </div>

        {group.conditions.length === 0 ? (
          <p className="text-xs text-slate-500">
            No {section} conditions configured yet. Click &ldquo;Add Condition&rdquo; to define a rule block.
          </p>
        ) : (
          <div className="space-y-2">
            {group.conditions.map((cond, idx) => (
              <div
                key={idx}
                className="flex flex-wrap items-center gap-2 rounded-md border border-slate-800 bg-[#111827] p-2.5"
              >
                <span className="w-6 font-mono text-[11px] text-slate-500">{idx + 1}.</span>
                {renderOperandEditor(cond.left, (nextLeft) => updateCondition(section, idx, { left: nextLeft }))}
                <select
                  value={cond.operator}
                  onChange={(e) => updateCondition(section, idx, { operator: e.target.value })}
                  className="h-9 rounded-md border border-sky-500/40 bg-[#0b1120] px-2.5 text-xs font-semibold text-sky-300 focus:border-sky-400 focus:outline-none"
                >
                  {operators.map((op) => (
                    <option key={op.id} value={op.id}>
                      {op.label}
                    </option>
                  ))}
                </select>
                {renderOperandEditor(cond.right, (nextRight) => updateCondition(section, idx, { right: nextRight }))}
                <button
                  type="button"
                  onClick={() => removeCondition(section, idx)}
                  className="ml-auto rounded-md p-1.5 text-slate-500 hover:bg-red-950/50 hover:text-red-400"
                  title="Remove condition"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };

  const handleSaveRule = async () => {
    setSaving(true);
    setFeedback(null);
    try {
      const res = await fetch('/api/streak/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(rule),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not save visual rule.');
      onChange(body.rule);
      onSaved?.(body.rule);
      setFeedback({ ok: true, text: `Saved visual rule "${body.rule.name}" (${body.rule.id}).` });
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Save failed.' });
    } finally {
      setSaving(false);
    }
  };

  const handleExportPython = async (saveToDisk: boolean) => {
    setExporting(true);
    setFeedback(null);
    try {
      const res = await fetch('/api/streak/rules/export-python', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rule_def: rule, save_to_disk: saveToDisk }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Python export failed.');
      setExportedCode(body.code || '');
      setSavedPath(body.saved_path || '');
      setFeedback({
        ok: true,
        text: saveToDisk && body.saved_path
          ? `Exported and hot-reloaded Python plugin at ${body.saved_path}.`
          : 'Generated editable Python plugin code below.',
      });
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Export failed.' });
    } finally {
      setExporting(false);
    }
  };

  const copyCode = async () => {
    if (!exportedCode) return;
    try {
      await navigator.clipboard.writeText(exportedCode);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // ignore clipboard denial in sandboxed iframes
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-semibold text-slate-300">Rule Name</label>
          <input
            type="text"
            value={rule.name}
            onChange={(e) => onChange({ ...rule, name: e.target.value })}
            placeholder="e.g. EMA 9/21 Momentum Breakout"
            className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-3 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-semibold text-slate-300">Description</label>
          <input
            type="text"
            value={rule.description || ''}
            onChange={(e) => onChange({ ...rule, description: e.target.value })}
            placeholder="Short logic note for your strategy library"
            className="h-9 w-full rounded-md border border-slate-700 bg-[#0b1120] px-3 text-sm text-slate-100 focus:border-sky-400 focus:outline-none"
          />
        </div>
      </div>

      {renderGroup('entry', rule.kind === 'screen' ? 'Scanner Match Conditions' : 'Entry Conditions')}
      {rule.kind === 'strategy' && renderGroup('exit', 'Exit Conditions (Optional — SL/TP/TSL also apply)')}

      <div className="flex flex-wrap items-center gap-2.5 pt-1">
        <button
          type="button"
          onClick={() => void handleSaveRule()}
          disabled={saving}
          className="inline-flex h-9 items-center gap-1.5 rounded-md border border-sky-500/40 bg-sky-500/15 px-3.5 text-xs font-semibold text-sky-300 hover:bg-sky-500/25 disabled:opacity-50"
        >
          <Save className="h-3.5 w-3.5" />
          {saving ? 'Saving...' : 'Save Visual Rule'}
        </button>
        <button
          type="button"
          onClick={() => void handleExportPython(false)}
          disabled={exporting}
          className="inline-flex h-9 items-center gap-1.5 rounded-md border border-slate-700 bg-slate-800/80 px-3.5 text-xs font-semibold text-slate-200 hover:border-slate-500 disabled:opacity-50"
        >
          <Code2 className="h-3.5 w-3.5" />
          Preview Python (.py)
        </button>
        <button
          type="button"
          onClick={() => void handleExportPython(true)}
          disabled={exporting}
          className="inline-flex h-9 items-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/15 px-3.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
        >
          <Code2 className="h-3.5 w-3.5" />
          Export &amp; Hot-Load .py Plugin
        </button>
      </div>

      {feedback && (
        <p className={`text-xs ${feedback.ok ? 'text-emerald-400' : 'text-red-400'}`}>{feedback.text}</p>
      )}

      {exportedCode && (
        <div className="rounded-lg border border-slate-800 bg-[#090e1a] p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-xs text-slate-400">
              {savedPath ? `Saved plugin: ${savedPath}` : 'Generated AlgoDesk Python Plugin'}
            </span>
            <button
              type="button"
              onClick={() => void copyCode()}
              className="inline-flex items-center gap-1 rounded border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-800"
            >
              {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="max-h-64 overflow-auto font-mono text-[11px] leading-relaxed text-slate-300">
            {exportedCode}
          </pre>
        </div>
      )}
    </div>
  );
}
