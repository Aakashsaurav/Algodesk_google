import { useEffect, useState } from 'react';
import { apiFetch } from '../../utils/authClient';
import { AlertCircle, CheckCircle2, ShieldCheck, X } from 'lucide-react';

export interface QuickOrderTarget {
  symbol: string;
  exchange?: string;
  side: 'BUY' | 'SELL';
  ltp?: number;
}

interface QuickOrderModalProps {
  target: QuickOrderTarget | null;
  tradingBroker: string;
  onClose: () => void;
}

export function QuickOrderModal({ target, tradingBroker, onClose }: QuickOrderModalProps) {
  const [side, setSide] = useState<'BUY' | 'SELL'>(target?.side || 'BUY');
  const [qty, setQty] = useState('1');
  const [orderType, setOrderType] = useState<'MARKET' | 'LIMIT'>('MARKET');
  const [limitPrice, setLimitPrice] = useState(target?.ltp ? String(target.ltp) : '');
  const [product, setProduct] = useState<'MIS' | 'CNC' | 'NRML'>('MIS');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    if (!target) return;
    setSide(target.side);
    setQty('1');
    setOrderType('MARKET');
    setLimitPrice(target.ltp && target.ltp > 0 ? target.ltp.toFixed(2) : '');
    setProduct('MIS');
    setResult(null);
  }, [target]);

  if (!target) return null;

  const estPrice = orderType === 'LIMIT' ? Number(limitPrice) || 0 : target.ltp || 0;
  const estValue = Math.max(0, (Number(qty) || 0) * estPrice);
  const isBuy = side === 'BUY';

  const handleSubmit = async () => {
    setSubmitting(true);
    setResult(null);
    try {
      const res = await apiFetch('/api/streak/orders/quick', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: target.symbol,
          exchange: target.exchange || 'NSE',
          side,
          quantity: Math.max(1, Number.parseInt(qty, 10) || 1),
          order_type: orderType,
          price: orderType === 'LIMIT' ? Number(limitPrice) || 0 : 0,
          product,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || 'Order rejected by safety gates or broker.');
      }
      setResult({ type: 'ok', text: body.message || 'Order placed.' });
    } catch (err) {
      setResult({
        type: 'err',
        text: err instanceof Error ? err.message : 'Quick order failed.',
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="quick-order-title"
    >
      <div className="w-full max-w-md rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
        {/* Header */}
        <div
          className={`flex items-center justify-between border-b px-5 py-4 ${
            isBuy ? 'border-sky-800/60 bg-sky-950/30' : 'border-red-900/60 bg-red-950/30'
          }`}
        >
          <div>
            <div className="flex items-center gap-2">
              <h3 id="quick-order-title" className="text-base font-semibold text-slate-100">
                {side} {target.symbol}
              </h3>
              <span className="font-mono text-xs text-slate-400">{target.exchange || 'NSE'}</span>
            </div>
            <p className="mt-0.5 font-mono text-xs text-slate-400 tabular-nums">
              LTP: {target.ltp && target.ltp > 0 ? `₹${target.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : 'Live Market'} · Broker: {tradingBroker.toUpperCase()}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close order modal"
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body */}
        <div className="space-y-4 p-5">
          {/* Side Switch */}
          <div className="grid grid-cols-2 gap-1.5 rounded-lg bg-[#0b1120] p-1">
            <button
              type="button"
              onClick={() => setSide('BUY')}
              className={`min-h-[36px] rounded-md text-xs font-semibold transition-colors ${
                isBuy ? 'bg-sky-500 text-slate-950' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              BUY
            </button>
            <button
              type="button"
              onClick={() => setSide('SELL')}
              className={`min-h-[36px] rounded-md text-xs font-semibold transition-colors ${
                !isBuy ? 'bg-red-500 text-white' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              SELL
            </button>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="qo-qty" className="mb-1.5 block text-xs font-medium text-slate-300">
                Quantity
              </label>
              <input
                id="qo-qty"
                type="number"
                min={1}
                step={1}
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-sm text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none"
              />
            </div>

            <div>
              <label htmlFor="qo-product" className="mb-1.5 block text-xs font-medium text-slate-300">
                Product
              </label>
              <select
                id="qo-product"
                value={product}
                onChange={(e) => setProduct(e.target.value as 'MIS' | 'CNC' | 'NRML')}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 text-xs font-medium text-slate-100 focus:border-sky-400 focus:outline-none"
              >
                <option value="MIS">MIS (Intraday)</option>
                <option value="CNC">CNC (Delivery)</option>
                <option value="NRML">NRML (F&amp;O)</option>
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-300">Order Type</label>
              <div className="grid grid-cols-2 gap-1 rounded-lg bg-[#0b1120] p-1">
                {(['MARKET', 'LIMIT'] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setOrderType(t)}
                    className={`min-h-[32px] rounded text-xs font-semibold transition-colors ${
                      orderType === t ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label htmlFor="qo-price" className="mb-1.5 block text-xs font-medium text-slate-300">
                Limit Price (₹)
              </label>
              <input
                id="qo-price"
                type="number"
                step="0.05"
                disabled={orderType === 'MARKET'}
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value)}
                placeholder={orderType === 'MARKET' ? 'Market Price' : '0.00'}
                className="min-h-[40px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-sm text-slate-100 tabular-nums focus:border-sky-400 focus:outline-none disabled:opacity-45"
              />
            </div>
          </div>

          <div className="flex items-center justify-between rounded-lg border border-slate-800 bg-[#0b1120] px-3.5 py-2.5 text-xs">
            <span className="flex items-center gap-1.5 text-slate-400">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
              6-Gate Preflight + Risk Limits enforced
            </span>
            <span className="font-mono font-semibold text-slate-200 tabular-nums">
              {estValue > 0 ? `Est. ₹${estValue.toLocaleString('en-IN', { maximumFractionDigits: 2 })}` : 'Market'}
            </span>
          </div>

          {result && (
            <div
              className={`flex items-start gap-2 rounded-lg border px-3.5 py-2.5 text-xs ${
                result.type === 'ok'
                  ? 'border-emerald-800/70 bg-emerald-950/40 text-emerald-200'
                  : 'border-red-900/70 bg-red-950/40 text-red-200'
              }`}
            >
              {result.type === 'ok' ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
              ) : (
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
              )}
              <span>{result.text}</span>
            </div>
          )}

          <div className="flex items-center justify-end gap-2.5 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="min-h-[40px] rounded-lg border border-slate-700 px-4 text-xs font-semibold text-slate-300 hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={submitting}
              className={`min-h-[40px] rounded-lg px-5 text-xs font-semibold transition-colors disabled:opacity-50 ${
                isBuy
                  ? 'bg-sky-500 text-slate-950 hover:bg-sky-400'
                  : 'bg-red-500 text-white hover:bg-red-400'
              }`}
            >
              {submitting ? 'Validating & Sending...' : `Place ${side} Order`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
