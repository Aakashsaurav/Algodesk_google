import { useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import { Plus, Trash2, ArrowUpRight, Search, FolderKanban } from 'lucide-react';
import type { BasketItem, DiscoverItem, StreakMetaPayload } from './types';

interface DiscoverProps {
  items: DiscoverItem[];
  onOpenStrategy: (item: DiscoverItem) => void;
  onOpenOptions: (item: DiscoverItem) => void;
  onOpenScanner: (item: DiscoverItem) => void;
}

export function DiscoverView({ items, onOpenStrategy, onOpenOptions, onOpenScanner }: DiscoverProps) {
  const [filterCat, setFilterCat] = useState<'all' | 'strategy' | 'screen' | 'options'>('all');
  const [filterMode, setFilterMode] = useState<'all' | 'visual' | 'python' | 'options'>('all');
  const [query, setQuery] = useState('');

  const filtered = items.filter((it) => {
    if (filterCat !== 'all' && it.category !== filterCat) return false;
    if (filterMode !== 'all' && it.mode !== filterMode) return false;
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      return (
        it.name.toLowerCase().includes(q) ||
        it.tagline.toLowerCase().includes(q) ||
        it.tags.some((t) => t.toLowerCase().includes(q))
      );
    }
    return true;
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold tracking-tight text-slate-100">Discover Strategies, Scanners &amp; Options</h1>
        <p className="text-sm text-slate-400">
          Curated ready-to-clone visual block templates, high-precision Python (.py) plugins, and multi-leg NSE options setups.
        </p>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-[#111827] p-4">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {(['all', 'strategy', 'screen', 'options'] as const).map((cat) => (
            <button
              key={cat}
              type="button"
              onClick={() => setFilterCat(cat)}
              className={`rounded-md px-3 py-1.5 font-semibold capitalize ${
                filterCat === cat ? 'bg-sky-500/20 text-sky-300' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {cat === 'all' ? 'All Categories' : cat === 'screen' ? 'Scanners' : cat === 'strategy' ? 'Strategies' : 'Options'}
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <select value={filterMode} onChange={(e) => setFilterMode(e.target.value as typeof filterMode)} className="h-9 rounded-md border border-slate-700 bg-[#0b1120] px-2.5 text-xs text-slate-200">
            <option value="all">All Engines</option>
            <option value="visual">Visual Blocks</option>
            <option value="python">Python (.py)</option>
            <option value="options">Options Payoff</option>
          </select>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-slate-500" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search templates..."
              className="h-9 w-52 rounded-md border border-slate-700 bg-[#0b1120] pl-8 pr-3 text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
            />
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {filtered.map((item) => (
          <div key={item.id} className="flex flex-col justify-between rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-4">
            <div className="space-y-2">
              <div className="flex items-center justify-between text-[11px] font-mono text-slate-400">
                <span>{item.category.toUpperCase()} · {item.mode.toUpperCase()}</span>
                <span>{item.timeframe}</span>
              </div>
              <h3 className="text-sm font-bold text-slate-100">{item.name}</h3>
              <p className="text-xs leading-relaxed text-slate-400">{item.tagline}</p>
              <p className="font-mono text-[11px] text-slate-500">{item.tags.join(' · ')}</p>
            </div>
            <div className="flex items-center justify-between border-t border-slate-800 pt-3">
              <span className="font-mono text-[11px] text-slate-500">{item.segment.replace('equity_', '')}</span>
              <button
                type="button"
                onClick={() => {
                  if (item.category === 'options') onOpenOptions(item);
                  else if (item.category === 'screen') onOpenScanner(item);
                  else onOpenStrategy(item);
                }}
                className="inline-flex items-center gap-1 rounded-md border border-sky-500/40 bg-sky-500/15 px-3 py-1.5 text-xs font-semibold text-sky-300 hover:bg-sky-500/25"
              >
                {item.category === 'options' ? 'Open Payoff' : item.category === 'screen' ? 'Open Scanner' : 'Clone & Backtest'}
                <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

interface BasketsProps {
  meta: StreakMetaPayload | null;
  onRefreshMeta: () => void;
  onBacktestBasket: (basket: BasketItem) => void;
}

export function BasketsView({ meta, onRefreshMeta, onBacktestBasket }: BasketsProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [symbolsText, setSymbolsText] = useState('RELIANCE, TCS, INFY, HDFCBANK');
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);

  const handleSave = async () => {
    const syms = symbolsText.split(/[,\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (!name.trim() || syms.length === 0) {
      setFeedback({ ok: false, text: 'Enter a basket name and at least one symbol.' });
      return;
    }
    setSaving(true);
    setFeedback(null);
    try {
      const res = await apiFetch('/api/streak/baskets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), description: description.trim(), exchange: 'NSE', symbols: syms }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not save basket.');
      setName('');
      setDescription('');
      setFeedback({ ok: true, text: `Saved basket "${body.basket.name}" (${body.basket.symbols.length} symbols).` });
      onRefreshMeta();
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : 'Save failed.' });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    await apiFetch(`/api/streak/baskets/${id}`, { method: 'DELETE' });
    onRefreshMeta();
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold tracking-tight text-slate-100">Watchlists &amp; Symbol Baskets</h1>
        <p className="text-sm text-slate-400">
          Create custom NSE/BSE symbol baskets for 1-click Portfolio Backtesting, Live Scanners, and Strategy Deployments — alongside your live-synced NSE CSV universes.
        </p>
      </div>

      <div className="rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-3">
        <h3 className="text-sm font-semibold text-slate-200 flex items-center gap-2">
          <FolderKanban className="h-4 w-4 text-sky-400" />
          Create Custom Basket
        </h3>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Basket Name (e.g. PSU Banks Swing)"
            className="h-9 rounded-md border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-100"
          />
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Description (optional)"
            className="h-9 rounded-md border border-slate-700 bg-[#0b1120] px-3 text-xs text-slate-100"
          />
          <input
            type="text"
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value.toUpperCase())}
            placeholder="SBIN, BANKBARODA, PNB, CANBK"
            className="h-9 rounded-md border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100"
          />
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={saving}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-sky-400 px-4 text-xs font-semibold text-sky-950 hover:bg-sky-300 disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" />
            {saving ? 'Saving...' : 'Save Basket'}
          </button>
          {feedback && <span className={`text-xs ${feedback.ok ? 'text-emerald-400' : 'text-red-400'}`}>{feedback.text}</span>}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {(meta?.baskets || []).map((b) => (
          <div key={b.id} className="flex flex-col justify-between rounded-xl border border-slate-800 bg-[#111827] p-5 space-y-3">
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-bold text-slate-100">{b.name}</h3>
                <button type="button" onClick={() => void handleDelete(b.id)} className="rounded p-1 text-slate-500 hover:text-red-400" title="Delete basket">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
              {b.description && <p className="text-xs text-slate-400">{b.description}</p>}
              <p className="font-mono text-[11px] leading-relaxed text-slate-300">{b.symbols.join(', ')}</p>
            </div>
            <div className="flex items-center justify-between border-t border-slate-800 pt-3">
              <span className="font-mono text-[11px] text-slate-500">{b.exchange} · {b.symbols.length} symbols</span>
              <button
                type="button"
                onClick={() => onBacktestBasket(b)}
                className="rounded-md border border-sky-500/40 bg-sky-500/15 px-3 py-1 text-xs font-semibold text-sky-300 hover:bg-sky-500/25"
              >
                Backtest Basket
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
