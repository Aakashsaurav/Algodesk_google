/**
 * src/utils/authClient.ts
 * =======================
 * In-memory Bearer session token manager and fetch interceptor.
 * Security rule: session tokens are NEVER stored in localStorage or sessionStorage.
 */

export interface SecurityStatus {
  ok: boolean;
  auth_enforced: boolean;
  has_env_token: boolean;
  has_passcode: boolean;
  needs_claim: boolean;
  active_sessions: number;
  live_trading_enabled: boolean;
}

type AuthRequiredListener = (detail?: string) => void;
type TokenChangeListener = (hasToken: boolean) => void;

let _sessionToken: string | null = null;
let _installed = false;
const _authRequiredListeners = new Set<AuthRequiredListener>();
const _tokenChangeListeners = new Set<TokenChangeListener>();

export function getSessionToken(): string | null {
  return _sessionToken;
}

export function hasSessionToken(): boolean {
  return Boolean(_sessionToken);
}

export function setSessionToken(token: string | null): void {
  _sessionToken = token ? token.trim() : null;
  const active = Boolean(_sessionToken);
  _tokenChangeListeners.forEach((fn) => {
    try {
      fn(active);
    } catch {
      // ignore listener errors
    }
  });
}

export function clearSessionToken(): void {
  setSessionToken(null);
}

export function subscribeAuthRequired(fn: AuthRequiredListener): () => void {
  _authRequiredListeners.add(fn);
  return () => {
    _authRequiredListeners.delete(fn);
  };
}

export function subscribeTokenChange(fn: TokenChangeListener): () => void {
  _tokenChangeListeners.add(fn);
  return () => {
    _tokenChangeListeners.delete(fn);
  };
}

function notifyAuthRequired(detail?: string): void {
  _authRequiredListeners.forEach((fn) => {
    try {
      fn(detail);
    } catch {
      // ignore
    }
  });
}

function isSameOriginApiUrl(input: RequestInfo | URL): string | null {
  try {
    const raw =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    if (raw.startsWith('/api/')) return raw;
    const parsed = new URL(raw, window.location.origin);
    if (parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/')) {
      return parsed.pathname;
    }
  } catch {
    // ignore malformed URLs
  }
  return null;
}

let _nativeFetch: typeof window.fetch | null = null;

function resolveNativeFetch(): typeof window.fetch {
  if (_nativeFetch) return _nativeFetch;
  const bound =
    typeof window !== 'undefined' && typeof window.fetch === 'function'
      ? window.fetch.bind(window)
      : globalThis.fetch.bind(globalThis);
  _nativeFetch = bound;
  return bound;
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const nativeFetch = resolveNativeFetch();
  const apiPath = isSameOriginApiUrl(input);
  let finalInit = init;

  if (apiPath && _sessionToken) {
    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined)
    );
    if (!headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${_sessionToken}`);
    }
    finalInit = { ...(init || {}), headers };
  }

  const res = await nativeFetch(input, finalInit);

  if (
    apiPath &&
    res.status === 401 &&
    !apiPath.startsWith('/api/security/login') &&
    !apiPath.startsWith('/api/security/claim')
  ) {
    let detail = 'Admin authorization required for this action.';
    try {
      const clone = res.clone();
      const body = await clone.json();
      if (body && typeof body.detail === 'string') {
        detail = body.detail;
      }
    } catch {
      // keep default message
    }
    notifyAuthRequired(detail);
  }

  return res;
}

export function installAuthFetchInterceptor(): void {
  if (_installed || typeof window === 'undefined' || typeof window.fetch !== 'function') {
    return;
  }
  _installed = true;
  resolveNativeFetch();

  const descriptor = Object.getOwnPropertyDescriptor(window, 'fetch');
  try {
    if (!descriptor || descriptor.writable || typeof descriptor.set === 'function') {
      window.fetch = apiFetch;
      return;
    }
  } catch {
    // window.fetch may be getter-only or inherited readonly (e.g. Safari/iframe sandbox)
  }

  if (!descriptor || descriptor.configurable) {
    try {
      Object.defineProperty(window, 'fetch', {
        value: apiFetch,
        writable: true,
        configurable: true,
        enumerable: descriptor?.enumerable ?? true,
      });
    } catch {
      // Strictly non-configurable host sandbox; avoid crashing boot.
    }
  }
}

export async function fetchSecurityStatus(): Promise<SecurityStatus | null> {
  try {
    const res = await apiFetch('/api/security/status');
    if (!res.ok) return null;
    return (await res.json()) as SecurityStatus;
  } catch {
    return null;
  }
}

export async function claimAdminPasscode(passcode: string): Promise<{
  ok: boolean;
  token?: string;
  status?: SecurityStatus;
  detail?: string;
}> {
  const res = await apiFetch('/api/security/claim', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ passcode }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    return { ok: false, detail: body.detail || 'Could not set admin passcode.' };
  }
  if (typeof body.token === 'string' && body.token) {
    setSessionToken(body.token);
  }
  return { ok: true, token: body.token, status: body.status };
}

export async function loginAdmin(passcodeOrToken: string): Promise<{
  ok: boolean;
  token?: string;
  status?: SecurityStatus;
  detail?: string;
}> {
  const res = await apiFetch('/api/security/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ passcode: passcodeOrToken }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    return { ok: false, detail: body.detail || 'Invalid passcode or API token.' };
  }
  if (typeof body.token === 'string' && body.token) {
    setSessionToken(body.token);
  }
  return { ok: true, token: body.token, status: body.status };
}
