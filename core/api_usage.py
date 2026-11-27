"""
core/api_usage.py
=================
Thread-safe, SQLite-persisted outbound third-party API call counter.

Tracks every outbound HTTP/HTTPS API call made by AlgoDesk to third-party
services (brokers, market-data providers, exchange archives, Telegram, etc.):
- **1-Minute window (`calls_1m`)**: Rolling 60-second window (auto-expires after 60s).
- **1-Hour window (`calls_1h`)**: Rolling 3,600-second window (auto-expires after 1h).
- **1-Day window (`calls_1d`)**: Calendar day in IST (`Asia/Kolkata`, `YYYY-MM-DD`),
  strictly resetting to `0` at `00:00:00 IST` on each new day.

Transport Interception Strategy:
- Installs a single low-level hook on `http.client.HTTPConnection.putrequest`
  which universally captures `urllib.request`, `http.client`, `requests`,
  `urllib3`, `kiteconnect`, `upstox_client`, `yfinance`, NSE archive downloads,
  and Telegram Bot API calls WITHOUT altering `urllib.request.urlopen` identity
  (preserving all unit-test mocks).
- Optionally hooks `httpx` and `curl_cffi` if installed, guarded by a thread-local
  re-entrancy flag so no request is ever double-counted.
- Explicitly excludes localhost / loopback addresses (`127.0.0.1`, `localhost`,
  `::1`, `0.0.0.0`, `*.local`) so internal proxy/health traffic is never counted.
- Never stores tokens or query secrets (redacts Telegram `/bot<TOKEN>/` paths).
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config import IST_TZ, config

logger = logging.getLogger(__name__)

WINDOW_1M_SEC = 60.0
WINDOW_1H_SEC = 3600.0


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    category: str  # "Broker" | "Data Provider" | "Exchange / Data" | "Notifications" | "Third-Party"
    host_suffixes: Tuple[str, ...]
    always_visible: bool = False


KNOWN_PROVIDERS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="zerodha",
        name="Zerodha Kite",
        category="Broker",
        host_suffixes=("kite.trade", "zerodha.com"),
        always_visible=True,
    ),
    ProviderSpec(
        id="upstox",
        name="Upstox",
        category="Broker",
        host_suffixes=("upstox.com",),
        always_visible=True,
    ),
    ProviderSpec(
        id="yfinance",
        name="Yahoo Finance",
        category="Data Provider",
        host_suffixes=("yahoo.com", "yimg.com"),
        always_visible=True,
    ),
    ProviderSpec(
        id="nse_india",
        name="NSE India",
        category="Exchange / Data",
        host_suffixes=("nseindia.com",),
        always_visible=True,
    ),
    ProviderSpec(
        id="telegram",
        name="Telegram",
        category="Notifications",
        host_suffixes=("telegram.org",),
        always_visible=True,
    ),
    ProviderSpec(
        id="bse_india",
        name="BSE India",
        category="Exchange / Data",
        host_suffixes=("bseindia.com",),
        always_visible=False,
    ),
    ProviderSpec(
        id="dhan",
        name="Dhan",
        category="Broker",
        host_suffixes=("dhan.co",),
        always_visible=False,
    ),
    ProviderSpec(
        id="angelone",
        name="Angel One",
        category="Broker",
        host_suffixes=("angelbroking.com", "angelone.in"),
        always_visible=False,
    ),
    ProviderSpec(
        id="fyers",
        name="Fyers",
        category="Broker",
        host_suffixes=("fyers.in",),
        always_visible=False,
    ),
    ProviderSpec(
        id="groww",
        name="Groww",
        category="Broker",
        host_suffixes=("groww.in",),
        always_visible=False,
    ),
)

_PROVIDER_BY_ID: Dict[str, ProviderSpec] = {p.id: p for p in KNOWN_PROVIDERS}
_PROVIDER_ORDER: Dict[str, int] = {p.id: idx for idx, p in enumerate(KNOWN_PROVIDERS)}
_BOT_TOKEN_RE = re.compile(r"/bot[^/\s?]+", re.IGNORECASE)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def _normalize_host(raw_host: str) -> str:
    h = str(raw_host or "").strip().lower()
    if not h:
        return ""
    if "://" in h:
        parsed = urlparse(h)
        h = (parsed.hostname or "").strip().lower()
    if h.startswith("[") and "]" in h:
        h = h[1 : h.index("]")]
    elif ":" in h and h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def is_local_host(raw_host: str) -> bool:
    """Return True for loopback / private local addresses that are NOT third-party APIs."""
    host = _normalize_host(raw_host)
    if not host:
        return True
    if host in _LOCAL_HOSTS or host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or ip.is_unspecified)
    except ValueError:
        return False


def sanitize_path(raw_url_or_path: str) -> str:
    """Strip query strings and redact sensitive path tokens (e.g., Telegram bot tokens)."""
    text = str(raw_url_or_path or "").strip()
    if not text:
        return "/"
    if "://" in text:
        parsed = urlparse(text)
        path = parsed.path or "/"
    else:
        path = text.split("?", 1)[0] or "/"
    path = _BOT_TOKEN_RE.sub("/bot***", path)
    return path[:120]


def classify_third_party_host(raw_host: str) -> Optional[Tuple[str, str, str]]:
    """
    Classify an outbound host into `(provider_id, display_name, category)`.
    Returns `None` for local/loopback hosts.
    """
    host = _normalize_host(raw_host)
    if is_local_host(host):
        return None
    for spec in KNOWN_PROVIDERS:
        for suffix in spec.host_suffixes:
            if host == suffix or host.endswith(f".{suffix}"):
                return spec.id, spec.name, spec.category

    # Unknown external third-party host -> derive clean registrable domain label
    labels = [part for part in host.split(".") if part]
    if len(labels) >= 2:
        # Handle two-part public suffixes like .co.in, .co.uk, .com.au
        if len(labels) >= 3 and labels[-1] in {"in", "uk", "au", "jp", "sg"} and labels[-2] in {"co", "org", "gov", "ac", "net"}:
            domain = ".".join(labels[-3:])
        else:
            domain = ".".join(labels[-2:])
    else:
        domain = host
    clean_id = re.sub(r"[^a-z0-9]+", "_", domain).strip("_") or "external"
    return clean_id, domain, "Third-Party"


def _ist_day_for_epoch(ts_epoch: float) -> str:
    return datetime.fromtimestamp(ts_epoch, tz=IST_TZ).strftime("%Y-%m-%d")


def _seconds_until_ist_midnight(ts_epoch: float) -> int:
    now_dt = datetime.fromtimestamp(ts_epoch, tz=IST_TZ)
    next_midnight = (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((next_midnight - now_dt).total_seconds()))


class OutboundApiUsageTracker:
    """
    Thread-safe, SQLite-backed counter for all outbound third-party API calls.

    Windows:
    - `calls_1m`: rolling last 60 seconds (`ts > now - 60`)
    - `calls_1h`: rolling last 3600 seconds (`ts > now - 3600`)
    - `calls_1d`: current IST calendar day (`day_ist == today_ist`), resetting at 00:00 IST
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._explicit_db_path = db_path
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        # In-memory fast structures keyed by provider_id
        self._meta: Dict[str, Dict[str, str]] = {
            p.id: {"id": p.id, "name": p.name, "category": p.category}
            for p in KNOWN_PROVIDERS
            if p.always_visible
        }
        self._ts_1m: Dict[str, Deque[float]] = {}
        self._ts_1h: Dict[str, Deque[float]] = {}
        self._day_counts: Dict[str, int] = {}
        self._last_call: Dict[str, Dict[str, Any]] = {}
        self._current_day_ist: str = _ist_day_for_epoch(self._now())
        self._last_prune_ts: float = 0.0
        self._loaded_from_db: bool = False

    def _now(self) -> float:
        return float(self._clock())

    @property
    def db_path(self) -> Path:
        return self._explicit_db_path or config.API_USAGE_DB_PATH

    def _ensure_db_locked(self) -> Optional[sqlite3.Connection]:
        if self._conn is not None:
            return self._conn
        try:
            path = self.db_path
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_epoch REAL NOT NULL,
                    day_ist TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    host TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbound_api_ts ON outbound_api_calls (ts_epoch)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbound_api_day ON outbound_api_calls (day_ist, provider_id)"
            )
            conn.commit()
            self._conn = conn
            return conn
        except Exception as exc:
            logger.debug("OutboundApiUsageTracker SQLite init skipped: %s", exc)
            return None

    def _hydrate_from_db_locked(self) -> None:
        if self._loaded_from_db:
            return
        self._loaded_from_db = True
        conn = self._ensure_db_locked()
        if conn is None:
            return
        now = self._now()
        today = _ist_day_for_epoch(now)
        self._current_day_ist = today
        cutoff_1h = now - WINDOW_1H_SEC
        cutoff_1m = now - WINDOW_1M_SEC
        try:
            rows = conn.execute(
                """
                SELECT ts_epoch, day_ist, provider_id, provider_name, category, host, method, path
                FROM outbound_api_calls
                WHERE day_ist = ? OR ts_epoch > ?
                ORDER BY ts_epoch ASC
                """,
                (today, cutoff_1h),
            ).fetchall()
            for ts_ep, day_ist, pid, pname, cat, host, method, path in rows:
                ts_f = float(ts_ep)
                if pid not in self._meta:
                    self._meta[pid] = {"id": pid, "name": pname, "category": cat}
                if ts_f > cutoff_1h:
                    self._ts_1h.setdefault(pid, deque()).append(ts_f)
                if ts_f > cutoff_1m:
                    self._ts_1m.setdefault(pid, deque()).append(ts_f)
                if day_ist == today:
                    self._day_counts[pid] = self._day_counts.get(pid, 0) + 1
                self._last_call[pid] = {
                    "ts_epoch": ts_f,
                    "ts_ist": datetime.fromtimestamp(ts_f, tz=IST_TZ).isoformat(timespec="seconds"),
                    "host": host,
                    "method": method,
                    "path": path,
                }
            # Prune expired rows from older days beyond the 1h rolling window
            conn.execute(
                "DELETE FROM outbound_api_calls WHERE day_ist < ? AND ts_epoch <= ?",
                (today, cutoff_1h),
            )
            conn.commit()
            self._last_prune_ts = now
        except Exception as exc:
            logger.debug("OutboundApiUsageTracker hydrate error: %s", exc)

    def _roll_windows_locked(self, now: float) -> None:
        self._hydrate_from_db_locked()
        today = _ist_day_for_epoch(now)

        # Day counter strictly resets on each new IST calendar day
        if today != self._current_day_ist:
            self._current_day_ist = today
            self._day_counts.clear()

        cutoff_1m = now - WINDOW_1M_SEC
        for q in self._ts_1m.values():
            while q and q[0] <= cutoff_1m:
                q.popleft()

        cutoff_1h = now - WINDOW_1H_SEC
        for q in self._ts_1h.values():
            while q and q[0] <= cutoff_1h:
                q.popleft()

        # Periodically prune stale SQLite rows (at most once every 5 minutes)
        if now - self._last_prune_ts >= 300.0:
            self._last_prune_ts = now
            conn = self._ensure_db_locked()
            if conn is not None:
                try:
                    conn.execute(
                        "DELETE FROM outbound_api_calls WHERE day_ist < ? AND ts_epoch <= ?",
                        (today, cutoff_1h),
                    )
                    conn.commit()
                except Exception:
                    pass

    def record_call(
        self,
        *,
        host: str,
        method: str = "GET",
        path: str = "/",
        provider_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        category: Optional[str] = None,
        ts_epoch: Optional[float] = None,
    ) -> bool:
        """
        Record a single outbound third-party API call.
        Returns True if recorded, False if ignored (e.g., localhost).
        """
        norm_host = _normalize_host(host)
        if provider_id:
            pid = provider_id.strip().lower()
            spec = _PROVIDER_BY_ID.get(pid)
            pname = provider_name or (spec.name if spec else pid.title())
            pcat = category or (spec.category if spec else "Third-Party")
        else:
            classified = classify_third_party_host(norm_host)
            if classified is None:
                return False
            pid, pname, pcat = classified

        now = float(ts_epoch) if ts_epoch is not None else self._now()
        today = _ist_day_for_epoch(now)
        clean_method = (method or "GET").strip().upper()[:12] or "GET"
        clean_path = sanitize_path(path)

        with self._lock:
            self._roll_windows_locked(now)
            if pid not in self._meta:
                self._meta[pid] = {"id": pid, "name": pname, "category": pcat}

            if now > self._now() - WINDOW_1M_SEC or ts_epoch is None:
                self._ts_1m.setdefault(pid, deque()).append(now)
            if now > self._now() - WINDOW_1H_SEC or ts_epoch is None:
                self._ts_1h.setdefault(pid, deque()).append(now)
            if today == self._current_day_ist:
                self._day_counts[pid] = self._day_counts.get(pid, 0) + 1

            self._last_call[pid] = {
                "ts_epoch": now,
                "ts_ist": datetime.fromtimestamp(now, tz=IST_TZ).isoformat(timespec="seconds"),
                "host": norm_host,
                "method": clean_method,
                "path": clean_path,
            }

            conn = self._ensure_db_locked()
            if conn is not None:
                try:
                    conn.execute(
                        """
                        INSERT INTO outbound_api_calls
                            (ts_epoch, day_ist, provider_id, provider_name, category, host, method, path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (now, today, pid, pname, pcat, norm_host, clean_method, clean_path),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.debug("OutboundApiUsageTracker insert failed: %s", exc)
        return True

    def get_snapshot(self) -> Dict[str, Any]:
        """Return live per-third-party and aggregate 1m / 1h / 1d counters and reset timers."""
        with self._lock:
            now = self._now()
            self._roll_windows_locked(now)
            today = self._current_day_ist
            day_reset_sec = _seconds_until_ist_midnight(now)

            provider_ids = set(self._meta.keys())
            provider_ids.update(self._ts_1m.keys())
            provider_ids.update(self._ts_1h.keys())
            provider_ids.update(self._day_counts.keys())

            rows: List[Dict[str, Any]] = []
            tot_1m = 0
            tot_1h = 0
            tot_1d = 0
            earliest_1m: Optional[float] = None
            earliest_1h: Optional[float] = None

            for pid in provider_ids:
                meta = self._meta.get(pid) or {"id": pid, "name": pid.title(), "category": "Third-Party"}
                q1m = self._ts_1m.get(pid)
                q1h = self._ts_1h.get(pid)
                c1m = len(q1m) if q1m else 0
                c1h = len(q1h) if q1h else 0
                c1d = int(self._day_counts.get(pid, 0))

                # Hide non-default providers that have zero across all windows
                spec = _PROVIDER_BY_ID.get(pid)
                if not (spec and spec.always_visible) and c1m == 0 and c1h == 0 and c1d == 0:
                    continue

                tot_1m += c1m
                tot_1h += c1h
                tot_1d += c1d

                p_reset_1m: Optional[int] = None
                if q1m and len(q1m) > 0:
                    rem = max(0, int(round((q1m[0] + WINDOW_1M_SEC) - now)))
                    p_reset_1m = rem
                    if earliest_1m is None or q1m[0] < earliest_1m:
                        earliest_1m = q1m[0]

                p_reset_1h: Optional[int] = None
                if q1h and len(q1h) > 0:
                    rem_h = max(0, int(round((q1h[0] + WINDOW_1H_SEC) - now)))
                    p_reset_1h = rem_h
                    if earliest_1h is None or q1h[0] < earliest_1h:
                        earliest_1h = q1h[0]

                last_info = self._last_call.get(pid)
                rows.append(
                    {
                        "id": pid,
                        "name": meta["name"],
                        "category": meta["category"],
                        "calls_1m": c1m,
                        "calls_1h": c1h,
                        "calls_1d": c1d,
                        "reset_1m_in_sec": p_reset_1m,
                        "reset_1h_in_sec": p_reset_1h,
                        "last_call_ts_ist": last_info["ts_ist"] if last_info else None,
                        "last_method": last_info["method"] if last_info else None,
                        "last_host": last_info["host"] if last_info else None,
                        "last_path": last_info["path"] if last_info else None,
                    }
                )

            rows.sort(
                key=lambda r: (
                    0 if r["calls_1d"] > 0 or r["calls_1h"] > 0 or r["calls_1m"] > 0 else 1,
                    -r["calls_1m"],
                    -r["calls_1h"],
                    -r["calls_1d"],
                    _PROVIDER_ORDER.get(r["id"], 999),
                    r["name"].lower(),
                )
            )

            next_1m_expiry = max(0, int(round((earliest_1m + WINDOW_1M_SEC) - now))) if earliest_1m is not None else None
            next_1h_expiry = max(0, int(round((earliest_1h + WINDOW_1H_SEC) - now))) if earliest_1h is not None else None

            return {
                "ok": True,
                "current_day_ist": today,
                "server_ts_ist": datetime.fromtimestamp(now, tz=IST_TZ).isoformat(timespec="seconds"),
                "windows": {
                    "minute_sec": int(WINDOW_1M_SEC),
                    "hour_sec": int(WINDOW_1H_SEC),
                    "day_timezone": "Asia/Kolkata (IST)",
                    "day_reset_in_sec": day_reset_sec,
                    "next_1m_expiry_in_sec": next_1m_expiry,
                    "next_1h_expiry_in_sec": next_1h_expiry,
                },
                "totals": {
                    "calls_1m": tot_1m,
                    "calls_1h": tot_1h,
                    "calls_1d": tot_1d,
                    "active_providers": sum(
                        1 for r in rows if r["calls_1m"] > 0 or r["calls_1h"] > 0 or r["calls_1d"] > 0
                    ),
                },
                "providers": rows,
                "hooks_installed": _HOOKS_INSTALLED,
            }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


# ── Global Outbound HTTP Transport Hook (Idempotent & Test-Safe) ──────────

api_usage_tracker = OutboundApiUsageTracker()

_HOOK_LOCK = threading.Lock()
_HOOKS_INSTALLED = False
_HOOK_TL = threading.local()
_ORIG_PUTREQUEST = http.client.HTTPConnection.putrequest


def _in_hook() -> bool:
    return bool(getattr(_HOOK_TL, "depth", 0) > 0)


def install_outbound_api_hooks(tracker: Optional[OutboundApiUsageTracker] = None) -> bool:
    """
    Install idempotent low-level hooks capturing all outbound HTTP(S) calls to
    third-party hosts without touching `urllib.request.urlopen` identity.
    """
    global _HOOKS_INSTALLED
    target = tracker or api_usage_tracker

    with _HOOK_LOCK:
        if _HOOKS_INSTALLED and tracker is None:
            return True

        def _hooked_putrequest(
            self: http.client.HTTPConnection,
            method: str,
            url: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if not _in_hook():
                _HOOK_TL.depth = getattr(_HOOK_TL, "depth", 0) + 1
                try:
                    host = getattr(self, "host", "") or ""
                    if not host and isinstance(url, str) and "://" in url:
                        host = urlparse(url).hostname or ""
                    if host:
                        target.record_call(host=str(host), method=str(method or "GET"), path=str(url or "/"))
                except Exception:
                    pass
                finally:
                    _HOOK_TL.depth = max(0, getattr(_HOOK_TL, "depth", 1) - 1)
            return _ORIG_PUTREQUEST(self, method, url, *args, **kwargs)

        http.client.HTTPConnection.putrequest = _hooked_putrequest  # type: ignore[method-assign]

        # Optional hook for httpx if installed (does not use http.client)
        try:
            import httpx  # type: ignore

            if not getattr(httpx.Client.send, "_algodesk_api_hooked", False):
                orig_send = httpx.Client.send

                def _hooked_httpx_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
                    if not _in_hook():
                        _HOOK_TL.depth = getattr(_HOOK_TL, "depth", 0) + 1
                        try:
                            url_str = str(getattr(request, "url", "") or "")
                            parsed = urlparse(url_str)
                            if parsed.hostname:
                                target.record_call(
                                    host=parsed.hostname,
                                    method=str(getattr(request, "method", "GET") or "GET"),
                                    path=parsed.path or "/",
                                )
                        except Exception:
                            pass
                        finally:
                            _HOOK_TL.depth = max(0, getattr(_HOOK_TL, "depth", 1) - 1)
                    return orig_send(self, request, *args, **kwargs)

                _hooked_httpx_send._algodesk_api_hooked = True  # type: ignore[attr-defined]
                httpx.Client.send = _hooked_httpx_send  # type: ignore[method-assign]
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("Optional httpx hook skipped: %s", exc)

        # Optional hook for curl_cffi (used by newer yfinance releases if installed)
        try:
            from curl_cffi import requests as curl_requests  # type: ignore

            if not getattr(curl_requests.Session.request, "_algodesk_api_hooked", False):
                orig_curl_req = curl_requests.Session.request

                def _hooked_curl_request(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
                    if not _in_hook():
                        _HOOK_TL.depth = getattr(_HOOK_TL, "depth", 0) + 1
                        try:
                            parsed = urlparse(str(url or ""))
                            if parsed.hostname:
                                target.record_call(
                                    host=parsed.hostname,
                                    method=str(method or "GET"),
                                    path=parsed.path or "/",
                                )
                        except Exception:
                            pass
                        finally:
                            _HOOK_TL.depth = max(0, getattr(_HOOK_TL, "depth", 1) - 1)
                    return orig_curl_req(self, method, url, *args, **kwargs)

                _hooked_curl_request._algodesk_api_hooked = True  # type: ignore[attr-defined]
                curl_requests.Session.request = _hooked_curl_request  # type: ignore[method-assign]
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("Optional curl_cffi hook skipped: %s", exc)

        _HOOKS_INSTALLED = True
        return True


__all__ = [
    "KNOWN_PROVIDERS",
    "OutboundApiUsageTracker",
    "ProviderSpec",
    "WINDOW_1H_SEC",
    "WINDOW_1M_SEC",
    "api_usage_tracker",
    "classify_third_party_host",
    "install_outbound_api_hooks",
    "is_local_host",
    "sanitize_path",
]
