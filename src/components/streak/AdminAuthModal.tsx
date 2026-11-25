import { useState } from 'react';
import { Lock, ShieldCheck, ShieldAlert, Unlock, X } from 'lucide-react';
import {
  claimAdminPasscode,
  clearSessionToken,
  hasSessionToken,
  loginAdmin,
  type SecurityStatus,
} from '../../utils/authClient';

interface AdminAuthModalProps {
  open: boolean;
  securityStatus: SecurityStatus | null;
  promptReason?: string;
  onClose: () => void;
  onAuthenticated: (nextStatus: SecurityStatus | null) => void;
}

export function AdminAuthModal({
  open,
  securityStatus,
  promptReason,
  onClose,
  onAuthenticated,
}: AdminAuthModalProps) {
  const [passcode, setPasscode] = useState('');
  const [confirmPasscode, setConfirmPasscode] = useState('');
  const [bootstrapCode, setBootstrapCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  if (!open) return null;

  const needsClaim = Boolean(securityStatus?.needs_claim);
  const bootstrapRequired = Boolean(needsClaim && securityStatus?.bootstrap_required);
  const unlocked = hasSessionToken();
  const liveEnabled = Boolean(securityStatus?.live_trading_enabled);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccess('');

    if (needsClaim) {
      if (bootstrapRequired && !bootstrapCode.trim()) {
        setError('Enter the one-time server bootstrap code from startup logs or data/.bootstrap_code.');
        return;
      }
      if (passcode.trim().length < 6) {
        setError('Passcode must be at least 6 characters.');
        return;
      }
      if (passcode !== confirmPasscode) {
        setError('Passcodes do not match.');
        return;
      }
      setBusy(true);
      try {
        const res = await claimAdminPasscode(passcode, bootstrapCode);
        if (!res.ok) {
          setError(res.detail || 'Could not claim admin passcode.');
          return;
        }
        setPasscode('');
        setConfirmPasscode('');
        setBootstrapCode('');
        setSuccess('Admin passcode saved on server and session unlocked.');
        onAuthenticated(res.status || null);
        window.setTimeout(() => {
          setSuccess('');
          onClose();
        }, 650);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (!passcode.trim()) {
      setError('Enter your admin passcode or API token.');
      return;
    }

    setBusy(true);
    try {
      const res = await loginAdmin(passcode);
      if (!res.ok) {
        setError(res.detail || 'Invalid credentials.');
        return;
      }
      setPasscode('');
      setSuccess('Session unlocked in memory.');
      onAuthenticated(res.status || null);
      window.setTimeout(() => {
        setSuccess('');
        onClose();
      }, 500);
    } finally {
      setBusy(false);
    }
  };

  const handleSignOut = () => {
    clearSessionToken();
    setSuccess('Session token cleared from browser memory.');
    onAuthenticated(securityStatus);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-slate-700 bg-[#111827] shadow-2xl">
        <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
          <div className="flex items-center gap-2">
            {unlocked ? (
              <Unlock className="h-4 w-4 text-emerald-400" />
            ) : (
              <Lock className="h-4 w-4 text-sky-400" />
            )}
            <h3 className="text-sm font-semibold text-slate-100">
              {needsClaim ? 'Set Operator Admin Passcode' : 'Operator Session & Safety Lock'}
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-100"
            aria-label="Close security modal"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4 p-5 text-xs">
          {/* Live Trading Master Switch Banner */}
          <div
            className={`flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 ${
              liveEnabled
                ? 'border-emerald-800/70 bg-emerald-950/30 text-emerald-200'
                : 'border-amber-800/70 bg-amber-950/30 text-amber-200'
            }`}
          >
            {liveEnabled ? (
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
            ) : (
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
            )}
            <div>
              <div className="font-semibold">
                {liveEnabled
                  ? 'Live Broker Trading ENABLED on Server'
                  : 'Live Broker Trading LOCKED (Safe Default)'}
              </div>
              <p className="mt-0.5 text-[11px] opacity-85">
                {liveEnabled
                  ? 'Real-money orders are allowed after passing Kill Switch, Risk Limits, and Preflight gates.'
                  : 'Paper trading & 1-click alerts work normally. Set ALGODESK_ENABLE_LIVE_TRADING=true in server .env to unlock real orders.'}
              </p>
            </div>
          </div>

          {promptReason && (
            <div className="rounded-lg border border-sky-800/70 bg-sky-950/40 px-3 py-2 text-sky-200">
              {promptReason}
            </div>
          )}

          {needsClaim ? (
            <p className="text-slate-400">
              Claim this AlgoDesk server by setting a local operator passcode (minimum 6 characters).
              The passcode is stored server-side as a salted hash, and your session token stays strictly in browser memory.
            </p>
          ) : (
            <p className="text-slate-400">
              {securityStatus?.has_env_token
                ? 'Enter your configured ALGODESK_API_TOKEN (or operator passcode) to unlock protected trading, risk, and plugin controls.'
                : 'Enter your operator passcode to unlock protected trading, risk, and plugin controls.'}
            </p>
          )}

          {bootstrapRequired && (
            <div>
              <label className="mb-1.5 block font-semibold text-slate-300">
                Server Bootstrap Code
              </label>
              <input
                type="text"
                value={bootstrapCode}
                onChange={(e) => setBootstrapCode(e.target.value)}
                placeholder="From server console or data/.bootstrap_code"
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
          )}

          <div>
            <label className="mb-1.5 block font-semibold text-slate-300">
              {needsClaim ? 'New Operator Passcode' : securityStatus?.has_env_token ? 'API Token or Passcode' : 'Operator Passcode'}
            </label>
            <input
              type="password"
              value={passcode}
              onChange={(e) => setPasscode(e.target.value)}
              autoFocus={!bootstrapRequired}
              placeholder={needsClaim ? 'At least 6 characters' : 'Enter passcode or token'}
              className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
            />
          </div>

          {needsClaim && (
            <div>
              <label className="mb-1.5 block font-semibold text-slate-300">Confirm Passcode</label>
              <input
                type="password"
                value={confirmPasscode}
                onChange={(e) => setConfirmPasscode(e.target.value)}
                placeholder="Re-enter passcode"
                className="min-h-[38px] w-full rounded-lg border border-slate-700 bg-[#0b1120] px-3 font-mono text-xs text-slate-100 focus:border-sky-400 focus:outline-none"
              />
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-900 bg-red-950/40 px-3 py-2 text-red-200">
              {error}
            </div>
          )}
          {success && (
            <div className="rounded-lg border border-emerald-800 bg-emerald-950/40 px-3 py-2 text-emerald-200">
              {success}
            </div>
          )}

          <div className="flex items-center justify-between gap-2 pt-1">
            {unlocked ? (
              <button
                type="button"
                onClick={handleSignOut}
                className="min-h-[36px] rounded-lg border border-slate-700 px-3 font-semibold text-slate-300 hover:border-red-500 hover:text-red-300"
              >
                Lock Session
              </button>
            ) : (
              <span className="text-[11px] text-slate-500">Token held in memory only</span>
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                className="min-h-[36px] rounded-lg border border-slate-700 px-4 font-semibold text-slate-300 hover:text-slate-100"
              >
                Close
              </button>
              <button
                type="submit"
                disabled={busy}
                className="min-h-[36px] rounded-lg bg-sky-500 px-4 font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
              >
                {busy ? 'Verifying...' : needsClaim ? 'Claim & Unlock' : 'Unlock'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
