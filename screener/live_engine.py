"""
screener/live_engine.py
=======================
Real-Time Live Screener Engine.

Lifecycle when started by the operator:
1. Warmup Phase (First Time — Historical Data):
   - Resolves selected assets via `UniverseResolver` (reading NSE CSVs from
     `data/universes/` or custom symbols).
   - Downloads missing canonical (`1minute` / `1day`) candles gap-only via
     `HistoricalDataManager` and loads ALL local historical candles up to today
     (resampled to the user's chosen timeframe) so recursive indicators (Slow
     EMA, Wilder RSI, ATR, ADX, Supertrend) are accurately warmed up once.
   - Evaluates the selected `.py` screen plugin on each symbol's historical
     series and broadcasts warmup progress + baseline rows over `/ws/live-screener`.

2. Live Inspection Phase (Real-Time WebSocket Stream):
   - Continuously inspects selected assets using live quotes from the active
     market feed (`HotStandbyFeedManager`, plus listening to incoming main WS
     feed ticks).
   - Merges each live tick into the symbol's in-memory forming candle bucket
     (updating today's daily bar or rolling IST 09:15-anchored N-minute intraday
     candles cleanly without re-fetching historical data).
   - Re-evaluates the screen plugin on every price update, detects live signal
     transitions (`ENTERED` / `EXITED`), and pushes structured WebSocket frames
     to all connected `/ws/live-screener` clients.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from config import IST_TZ, config
from core.feed_manager import HotStandbyFeedManager, feed_manager
from core.historical import (
    HistoricalDataManager,
    historical_data_manager,
    normalize_timeframe,
    parse_timeframe,
    resolve_base_timeframe,
)
from indicators.toolkit import IndicatorToolkit, available_backends
from screener.base import BaseScreen, ColumnSpec
from screener.engine import _infer_columns_from_metrics, compute_auto_lookback_days
from screener.registry import ScreenRegistry, screen_registry
from screener.universes import UniverseResolver, universe_resolver

logger = logging.getLogger(__name__)


def _ist_bucket_for_tick(ts_ist: Optional[str], timeframe: str) -> Tuple[str, str]:
    """
    Compute the canonical `(bucket_ts_ist, trade_date)` for a live tick under
    `timeframe` (`1day`, `Nday`, or `Nminute`), matching `core.historical`
    IST 09:15 session alignment.
    """
    now_dt = datetime.now(tz=IST_TZ)
    dt = now_dt
    if ts_ist:
        try:
            parsed = datetime.fromisoformat(str(ts_ist).replace("Z", "+00:00"))
            dt = parsed.astimezone(IST_TZ) if parsed.tzinfo else parsed.replace(tzinfo=IST_TZ)
        except ValueError:
            dt = now_dt

    count, unit = parse_timeframe(timeframe)
    tdate = dt.date().isoformat()
    if unit == "minute":
        # Anchor to 09:15 IST NSE session open (matching core.historical._resample_minute_bars)
        session_open_min = 9 * 60 + 15
        cur_min = dt.hour * 60 + dt.minute
        if cur_min >= session_open_min:
            bucket_min = session_open_min + ((cur_min - session_open_min) // count) * count
        else:
            bucket_min = (cur_min // count) * count
        bh, bm = divmod(min(bucket_min, 23 * 60 + 59), 60)
        bdt = dt.replace(hour=bh, minute=bm, second=0, microsecond=0)
        return bdt.isoformat(timespec="seconds"), tdate

    # Daily / N-day candle bucket
    return f"{tdate}T15:30:00+05:30", tdate


def merge_live_tick_into_candles(
    base_bars: List[Dict[str, Any]],
    forming_bar: Optional[Dict[str, Any]],
    tick: Dict[str, Any],
    timeframe: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Merge a live market tick `{ltp, open, high, low, volume, oi, ts_ist}` into
    `(base_bars, forming_bar)` without mutating historical candles prior to the
    live forming bucket.

    Returns `(updated_base_bars, updated_forming_bar, combined_candles_for_eval)`.
    """
    raw_ltp = tick.get("ltp")
    if raw_ltp is None:
        combined = list(base_bars) + ([forming_bar] if forming_bar else [])
        return base_bars, forming_bar, combined

    ltp = float(raw_ltp)
    if ltp <= 0:
        combined = list(base_bars) + ([forming_bar] if forming_bar else [])
        return base_bars, forming_bar, combined

    count, unit = parse_timeframe(timeframe)
    bucket_ts, tdate = _ist_bucket_for_tick(tick.get("ts_ist"), timeframe)

    tick_open = float(tick["open"]) if tick.get("open") not in (None, 0) else ltp
    tick_high = max(ltp, float(tick["high"]) if tick.get("high") not in (None, 0) else ltp)
    tick_low = min(ltp, float(tick["low"]) if tick.get("low") not in (None, 0) else ltp)
    tick_vol = int(tick.get("volume") or 0)
    tick_oi = int(tick.get("oi") or 0)

    if unit == "minute":
        # Intraday forming candle management
        last_base_ts = str(base_bars[-1].get("ts_ist") or "") if base_bars else ""
        if forming_bar is not None:
            fb_ts = str(forming_bar.get("ts_ist") or "")
            if fb_ts == bucket_ts:
                new_fb = {
                    **forming_bar,
                    "high": max(float(forming_bar["high"]), ltp),
                    "low": min(float(forming_bar["low"]), ltp),
                    "close": ltp,
                    "volume": max(int(forming_bar.get("volume") or 0), tick_vol),
                    "oi": tick_oi if tick_oi > 0 else int(forming_bar.get("oi") or 0),
                }
                return base_bars, new_fb, list(base_bars) + [new_fb]
            if bucket_ts > fb_ts:
                # Previous live intraday candle closed -> commit to base_bars!
                base_bars = list(base_bars) + [forming_bar]
                new_fb = {
                    "ts_ist": bucket_ts,
                    "trade_date": tdate,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": tick_vol,
                    "oi": tick_oi,
                }
                return base_bars, new_fb, list(base_bars) + [new_fb]
            # Older/out-of-order tick: keep current forming bar
            return base_bars, forming_bar, list(base_bars) + [forming_bar]

        # No forming bar yet: check if bucket matches the last historical bar
        if base_bars and last_base_ts == bucket_ts:
            prev = base_bars[-1]
            trimmed_base = list(base_bars[:-1])
            new_fb = {
                **prev,
                "high": max(float(prev["high"]), ltp),
                "low": min(float(prev["low"]), ltp),
                "close": ltp,
                "volume": max(int(prev.get("volume") or 0), tick_vol),
                "oi": tick_oi if tick_oi > 0 else int(prev.get("oi") or 0),
            }
            return trimmed_base, new_fb, trimmed_base + [new_fb]

        new_fb = {
            "ts_ist": bucket_ts,
            "trade_date": tdate,
            "open": ltp,
            "high": ltp,
            "low": ltp,
            "close": ltp,
            "volume": tick_vol,
            "oi": tick_oi,
        }
        return base_bars, new_fb, list(base_bars) + [new_fb]

    # Daily / N-day forming candle management
    if base_bars:
        last_td = str(base_bars[-1].get("trade_date") or str(base_bars[-1].get("ts_ist", ""))[:10])[:10]
        if last_td == tdate:
            prev = base_bars[-1]
            trimmed_base = list(base_bars[:-1])
            new_fb = {
                **prev,
                "open": float(prev["open"]) if float(prev.get("open") or 0) > 0 else tick_open,
                "high": max(float(prev["high"]), tick_high, ltp),
                "low": min(float(prev["low"]), tick_low, ltp),
                "close": ltp,
                "volume": max(int(prev.get("volume") or 0), tick_vol),
                "oi": tick_oi if tick_oi > 0 else int(prev.get("oi") or 0),
            }
            return trimmed_base, new_fb, trimmed_base + [new_fb]

    if forming_bar is not None and str(forming_bar.get("trade_date") or "") == tdate:
        new_fb = {
            **forming_bar,
            "open": float(forming_bar["open"]) if float(forming_bar.get("open") or 0) > 0 else tick_open,
            "high": max(float(forming_bar["high"]), tick_high, ltp),
            "low": min(float(forming_bar["low"]), tick_low, ltp),
            "close": ltp,
            "volume": max(int(forming_bar.get("volume") or 0), tick_vol),
            "oi": tick_oi if tick_oi > 0 else int(forming_bar.get("oi") or 0),
        }
        return base_bars, new_fb, list(base_bars) + [new_fb]

    if forming_bar is not None and str(forming_bar.get("trade_date") or "") < tdate:
        base_bars = list(base_bars) + [forming_bar]

    new_fb = {
        "ts_ist": bucket_ts,
        "trade_date": tdate,
        "open": tick_open,
        "high": tick_high,
        "low": tick_low,
        "close": ltp,
        "volume": tick_vol,
        "oi": tick_oi,
    }
    return base_bars, new_fb, list(base_bars) + [new_fb]


class LiveScreenerEngine:
    """
    Server-authoritative real-time live screener backed by historical warmup +
    live WebSocket quote inspection.
    """

    def __init__(
        self,
        hist_manager: Optional[HistoricalDataManager] = None,
        registry: Optional[ScreenRegistry] = None,
        universes: Optional[UniverseResolver] = None,
        feed_mgr: Optional[HotStandbyFeedManager] = None,
    ) -> None:
        self._hist = hist_manager or historical_data_manager
        self._registry = registry or screen_registry
        self._universes = universes or universe_resolver
        self._feed = feed_mgr or feed_manager

        self._lock = threading.RLock()
        self._listeners: Set[queue.Queue] = set()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # Active session state
        self._session_id: str = ""
        self._running: bool = False
        self._phase: str = "idle"  # idle | warming_up | streaming | stopped | error
        self._error_message: str = ""

        self._screen: Optional[BaseScreen] = None
        self._screen_id: str = "all_metrics"
        self._screen_name: str = ""
        self._resolved_params: Dict[str, Any] = {}
        self._universe_id: str = "CUSTOM"
        self._universe_label: str = ""
        self._exchange: str = "NSE"
        self._timeframe: str = "1day"
        self._base_timeframe: str = "1day"
        self._provider: str = "upstox"
        self._backend: str = "auto"
        self._required_bars: int = 0
        self._lookback_days: int = 0
        self._poll_interval_sec: float = 2.0
        self._columns: List[Dict[str, Any]] = []
        self._started_at_ist: Optional[str] = None

        # Warmup progress & per-symbol state
        self._symbols: List[str] = []
        self._warmup_completed: int = 0
        self._warmup_total: int = 0
        self._warmup_message: str = "Idle"
        self._warmup_errors: List[Dict[str, str]] = []

        # Per-symbol runtime cache: {sym: {"base_bars": [...], "forming_bar": dict|None, "row": dict, "last_ltp": float|None, "matched": bool}}
        self._sym_state: Dict[str, Dict[str, Any]] = {}
        self._alerts: List[Dict[str, Any]] = []

        # Live feed telemetry
        self._active_feed: str = ""
        self._transport: str = ""
        self._tick_seq: int = 0
        self._ticks_processed: int = 0
        self._latency_ms: Optional[float] = None
        self._last_tick_ist: Optional[str] = None

    # ── WebSocket Pub/Sub Listeners ──────────────────────────────────────

    def register_listener(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=32)
        with self._lock:
            self._listeners.add(q)
        return q

    def unregister_listener(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)

    def _broadcast(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        msg = {"type": event_type, **payload}
        for q in listeners:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                q.put_nowait(msg)
            except Exception:
                pass

    # ── Core Evaluation Helper ───────────────────────────────────────────

    def _evaluate_symbol(
        self,
        sym: str,
        candles: List[Dict[str, Any]],
        *,
        live_quote: Optional[Dict[str, Any]] = None,
        prev_ltp: Optional[float] = None,
        prev_matched: Optional[bool] = None,
        active_feed: str = "",
    ) -> Optional[Dict[str, Any]]:
        screen = self._screen
        if screen is None or not candles:
            return None
        min_req = max(5, int(getattr(screen, "min_bars", 15) or 15))
        if len(candles) < min_req:
            return None

        tk = IndicatorToolkit(candles, backend=self._backend)
        match = screen.evaluate(candles, self._resolved_params, tk)

        last_close = float(candles[-1]["close"])
        prev_close_bar = float(candles[-2]["close"]) if len(candles) >= 2 else last_close
        prev_close = (
            float(live_quote["prev_close"])
            if (live_quote and live_quote.get("prev_close") not in (None, 0))
            else prev_close_bar
        )
        day_chg_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

        tick_dir = "flat"
        if live_quote and live_quote.get("ltp") is not None and prev_ltp is not None:
            new_p = float(live_quote["ltp"])
            if new_p > prev_ltp:
                tick_dir = "up"
            elif new_p < prev_ltp:
                tick_dir = "down"

        # Auto-infer columns if a custom .py screen doesn't declare get_columns()
        if not self._columns and match.metrics:
            inferred = _infer_columns_from_metrics(match.metrics)
            self._columns = [c.to_dict() for c in inferred]

        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        row: Dict[str, Any] = {
            "symbol": sym,
            "exchange": self._exchange,
            "bars": len(candles),
            "last_date": str(candles[-1].get("trade_date") or str(candles[-1].get("ts_ist", ""))[:10]),
            "last_ts_ist": str(candles[-1].get("ts_ist") or ""),
            "close": round(last_close, 2),
            "ltp": round(float(live_quote["ltp"]), 2) if (live_quote and live_quote.get("ltp") is not None) else round(last_close, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(day_chg_pct, 2),
            "volume": int((live_quote or {}).get("volume") or candles[-1].get("volume") or 0),
            "tick_direction": tick_dir,
            "live_source": (live_quote or {}).get("source") or active_feed or "historical_warmup",
            "live_updated_ist": (live_quote or {}).get("ts_ist") or now_ist,
            "is_live": bool(live_quote and live_quote.get("ltp") is not None),
            "signal": match.signal,
            "tags": list(match.tags),
            "score": int(match.score),
            "matched": bool(match.matched),
            **match.metrics,
        }

        # Detect live signal transitions once streaming
        if prev_matched is not None and bool(match.matched) != bool(prev_matched):
            transition = "ENTERED" if match.matched else "EXITED"
            alert = {
                "id": f"al-{uuid.uuid4().hex[:8]}",
                "ts_ist": now_ist,
                "symbol": sym,
                "transition": transition,
                "signal": match.signal,
                "ltp": row["ltp"],
                "change_pct": row["change_pct"],
                "score": row["score"],
                "tags": list(match.tags),
            }
            with self._lock:
                self._alerts.insert(0, alert)
                if len(self._alerts) > 50:
                    del self._alerts[50:]

        return row

    def apply_live_quotes(
        self,
        quotes: Dict[str, Dict[str, Any]],
        *,
        active_feed: str = "",
        transport: str = "",
        latency_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Apply a batch of live quotes `{SYMBOL: quote_dict}` to the currently
        warmed-up symbols, recompute indicators on merged historical+live bars,
        and broadcast a `live_screen:tick` delta update.
        """
        if not quotes:
            return {"updated": 0, "rows": []}

        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        updated_rows: List[Dict[str, Any]] = []

        with self._lock:
            if not self._running or self._screen is None:
                return {"updated": 0, "rows": []}
            symbols_snapshot = list(self._sym_state.keys())
            tf = self._timeframe
            if active_feed:
                self._active_feed = active_feed
            if transport:
                self._transport = transport
            if latency_ms is not None:
                self._latency_ms = latency_ms

        for sym in symbols_snapshot:
            q = quotes.get(sym)
            if not q or q.get("ltp") is None:
                continue
            with self._lock:
                st = self._sym_state.get(sym)
                if not st:
                    continue
                base_bars = st["base_bars"]
                forming_bar = st["forming_bar"]
                prev_ltp = st.get("last_ltp")
                prev_matched = st.get("matched")
                prev_vol = st.get("last_vol")

            new_ltp = float(q["ltp"])
            new_vol = int(q.get("volume") or 0)
            # Always evaluate on first live tick for this symbol; afterwards skip only if completely unchanged
            if prev_ltp is not None and new_ltp == prev_ltp and new_vol == prev_vol and st.get("has_live_tick"):
                continue

            new_base, new_forming, combined = merge_live_tick_into_candles(base_bars, forming_bar, q, tf)
            row = self._evaluate_symbol(
                sym,
                combined,
                live_quote=q,
                prev_ltp=prev_ltp,
                prev_matched=prev_matched,
                active_feed=active_feed or self._active_feed,
            )
            if row is None:
                continue

            with self._lock:
                if sym in self._sym_state:
                    self._sym_state[sym]["base_bars"] = new_base
                    self._sym_state[sym]["forming_bar"] = new_forming
                    self._sym_state[sym]["row"] = row
                    self._sym_state[sym]["last_ltp"] = new_ltp
                    self._sym_state[sym]["last_vol"] = new_vol
                    self._sym_state[sym]["matched"] = bool(row["matched"])
                    self._sym_state[sym]["has_live_tick"] = True
            updated_rows.append(row)

        with self._lock:
            self._tick_seq += 1
            self._ticks_processed += len(updated_rows)
            self._last_tick_ist = now_ist
            snapshot = self._build_sorted_results_locked()
            payload = {
                "session_id": self._session_id,
                "phase": self._phase,
                "running": self._running,
                "tick_seq": self._tick_seq,
                "ticks_processed": self._ticks_processed,
                "updated_count": len(updated_rows),
                "active_feed": self._active_feed,
                "transport": self._transport,
                "latency_ms": self._latency_ms,
                "last_tick_ist": self._last_tick_ist,
                "columns": list(self._columns),
                "matched_count": sum(1 for r in snapshot if r.get("matched")),
                "evaluated_count": len(snapshot),
                "results": snapshot,
                "alerts": list(self._alerts[:20]),
            }

        self._broadcast("live_screen:tick", payload)
        return {"updated": len(updated_rows), "rows": updated_rows}

    # ── Start / Stop Lifecycle ───────────────────────────────────────────

    def start(
        self,
        *,
        symbols: Optional[List[str]] = None,
        universe: str = "CUSTOM",
        exchange: str = "NSE",
        timeframe: str = "1day",
        screen_id: Optional[str] = None,
        preset: str = "all_metrics",
        params: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        backend: str = "auto",
        poll_interval_sec: float = 2.0,
        hist_fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        live_fetcher: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """Start (or restart) the Live Screener: historical warmup once -> continuous WS inspection."""
        # Stop any prior session cleanly before re-initializing
        self.stop(silent=True)

        target_screen_id = screen_id or preset or "all_metrics"
        try:
            screen = self._registry.get(target_screen_id)
        except Exception:
            from rules.engine import compile_visual_screen, visual_rule_store

            rule_def = visual_rule_store.get(target_screen_id)
            if rule_def is None:
                raise
            screen = compile_visual_screen(rule_def)
        resolved_params = screen.resolve_params(params)

        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)
        eff_exch = (exchange or "NSE").strip().upper()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()
        req_bars = int(screen.required_bars(resolved_params))
        auto_days = compute_auto_lookback_days(req_bars, tf)

        uni_bundle = self._universes.resolve(
            universe=universe,
            custom_symbols=symbols,
            exchange=eff_exch,
            timeframe=tf,
        )
        clean_symbols: List[str] = uni_bundle["symbols"]
        if not clean_symbols:
            raise ValueError("No symbols resolved for Live Screen. Choose a synced NSE universe or enter custom symbols.")

        declared_cols = [c.to_dict() for c in screen.get_columns(resolved_params)]
        session_id = f"lsc-{uuid.uuid4().hex[:8]}"
        interval = max(1.0, min(30.0, float(poll_interval_sec or 2.0)))

        with self._lock:
            self._stop_event = threading.Event()
            self._session_id = session_id
            self._running = True
            self._phase = "warming_up"
            self._error_message = ""
            self._screen = screen
            self._screen_id = screen.id
            self._screen_name = screen.name
            self._resolved_params = resolved_params
            self._universe_id = uni_bundle["universe"]
            self._universe_label = uni_bundle["label"]
            self._exchange = eff_exch
            self._timeframe = tf
            self._base_timeframe = base_tf
            self._provider = eff_provider
            self._backend = backend or "auto"
            self._required_bars = req_bars
            self._lookback_days = auto_days
            self._poll_interval_sec = interval
            self._columns = declared_cols
            self._started_at_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")

            self._symbols = list(clean_symbols)
            self._warmup_completed = 0
            self._warmup_total = len(clean_symbols)
            self._warmup_message = f"Starting historical warmup for {len(clean_symbols)} symbols ({auto_days}d / {req_bars} bars)..."
            self._warmup_errors = []
            self._sym_state = {}
            self._alerts = []
            self._active_feed = eff_provider
            self._tick_seq = 0
            self._ticks_processed = 0
            self._latency_ms = None
            self._last_tick_ist = None

            stop_ev = self._stop_event
            self._worker_thread = threading.Thread(
                target=self._run_session_loop,
                args=(session_id, stop_ev, hist_fetcher, live_fetcher),
                name=f"algodesk-live-screen-{session_id}",
                daemon=True,
            )
            self._worker_thread.start()

        st = self.status()
        self._broadcast("live_screen:state", st)
        return st

    def stop(self, *, silent: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            worker = self._worker_thread
            was_running = self._running
            self._running = False
            if was_running:
                self._phase = "stopped"
                self._warmup_message = "Live screen stopped by operator."
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=3.0)
        try:
            self._feed.release_live_screen_ws()
        except Exception:
            pass
        st = self.status()
        if not silent:
            self._broadcast("live_screen:stopped", st)
        return st

    def _run_session_loop(
        self,
        session_id: str,
        stop_ev: threading.Event,
        hist_fetcher: Optional[Callable[..., List[Dict[str, Any]]]],
        live_fetcher: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]],
    ) -> None:
        today_ist = datetime.now(tz=IST_TZ).date()
        to_date = today_ist.isoformat()
        from_date = (today_ist - timedelta(days=self._lookback_days)).isoformat()

        # ── Phase 1: Historical Warmup (once per symbol) ─────────────────
        for idx, sym in enumerate(list(self._symbols)):
            if stop_ev.is_set():
                return
            try:
                dl_meta = self._hist.download(
                    symbol=sym,
                    exchange=self._exchange,
                    timeframe=self._timeframe,
                    from_date=from_date,
                    to_date=to_date,
                    provider=self._provider,
                    fetcher=hist_fetcher,
                )
                records = self._hist.get_records(
                    symbol=dl_meta["symbol"],
                    exchange=dl_meta["exchange"],
                    timeframe=dl_meta["timeframe"],
                    from_date=None,
                    to_date=to_date,
                    view="full",
                )
                bars = records.get("rows", [])
                row = self._evaluate_symbol(sym, bars, live_quote=None, prev_ltp=None, prev_matched=None)
                if row is not None:
                    with self._lock:
                        self._sym_state[sym] = {
                            "base_bars": list(bars),
                            "forming_bar": None,
                            "row": row,
                            "last_ltp": float(bars[-1]["close"]),
                            "last_vol": int(bars[-1].get("volume") or 0),
                            "matched": bool(row["matched"]),
                            "has_live_tick": False,
                        }
                else:
                    with self._lock:
                        self._warmup_errors.append(
                            {
                                "symbol": sym,
                                "error": f"Insufficient historical bars ({len(bars)}) for indicator warmup.",
                            }
                        )
            except Exception as exc:
                with self._lock:
                    self._warmup_errors.append({"symbol": sym, "error": str(exc)})

            with self._lock:
                self._warmup_completed = idx + 1
                self._warmup_message = f"Warming up indicators: {sym} ({idx + 1}/{self._warmup_total})"
                warmup_payload = {
                    "session_id": session_id,
                    "phase": "warming_up",
                    "running": True,
                    "warmup": {
                        "completed": self._warmup_completed,
                        "total": self._warmup_total,
                        "pct": int(round((self._warmup_completed / max(1, self._warmup_total)) * 100)),
                        "symbol": sym,
                        "message": self._warmup_message,
                        "error_count": len(self._warmup_errors),
                    },
                    "columns": list(self._columns),
                    "results": self._build_sorted_results_locked(),
                }
            # Broadcast warmup progress smoothly (every symbol for small lists, every 3 for large universes)
            if self._warmup_total <= 30 or (idx + 1) % 3 == 0 or (idx + 1) == self._warmup_total:
                self._broadcast("live_screen:warmup", warmup_payload)

        if stop_ev.is_set():
            return

        with self._lock:
            self._phase = "streaming"
            self._warmup_message = (
                f"Historical warmup complete ({len(self._sym_state)}/{self._warmup_total} symbols ready). "
                f"Inspecting live ticks via {self._provider.upper()}."
            )
            ready_state = self.status()
        self._broadcast("live_screen:state", ready_state)

        # ── Phase 2: Live WebSocket Tick Inspection Loop ─────────────────
        # Also listen to incoming main Live Feed WS ticks for zero-latency piggybacking
        main_feed_q: Optional[queue.Queue] = None
        if live_fetcher is None:
            try:
                main_feed_q = self._feed.register_listener()
            except Exception:
                main_feed_q = None

        try:
            while not stop_ev.is_set():
                # Drain any ticks already pushed on the main Live Feed WS
                if main_feed_q is not None:
                    pushed_quotes: Dict[str, Dict[str, Any]] = {}
                    pushed_feed = ""
                    while True:
                        try:
                            ev = main_feed_q.get_nowait()
                        except queue.Empty:
                            break
                        if isinstance(ev, dict) and isinstance(ev.get("quotes"), list):
                            pushed_feed = str(ev.get("active_feed") or pushed_feed)
                            for q in ev["quotes"]:
                                if isinstance(q, dict) and q.get("symbol") and q.get("ltp") is not None:
                                    pushed_quotes[str(q["symbol"]).upper()] = q
                    if pushed_quotes:
                        self.apply_live_quotes(pushed_quotes, active_feed=pushed_feed)

                # Inspect all warmed-up live screen symbols via active feed chain
                with self._lock:
                    active_syms = list(self._sym_state.keys())
                if active_syms:
                    try:
                        if live_fetcher is not None:
                            raw_q = live_fetcher(active_syms) or {}
                            self.apply_live_quotes(raw_q, active_feed=self._provider, transport="custom", latency_ms=1.0)
                        else:
                            res = self._feed.fetch_live_quotes_for(
                                active_syms,
                                exchange=self._exchange,
                                provider=self._provider,
                            )
                            if res.get("ok") and res.get("quotes"):
                                self.apply_live_quotes(
                                    res["quotes"],
                                    active_feed=str(res.get("active_feed") or self._provider),
                                    transport=str(res.get("transport") or ""),
                                    latency_ms=res.get("latency_ms"),
                                )
                            elif res.get("error"):
                                with self._lock:
                                    self._error_message = str(res["error"])
                    except Exception as exc:
                        logger.debug("Live screen tick fetch error: %s", exc)

                stop_ev.wait(self._poll_interval_sec)
        finally:
            try:
                self._feed.release_live_screen_ws()
            except Exception:
                pass
            if main_feed_q is not None:
                try:
                    self._feed.unregister_listener(main_feed_q)
                except Exception:
                    pass

    def _build_sorted_results_locked(self) -> List[Dict[str, Any]]:
        rows = [st["row"] for st in self._sym_state.values() if st.get("row")]
        # Stable sort: matched first, then score desc, then alphabetical symbol A->Z
        # so rows don't constantly jump around on minor 0.01% LTP ticks while scrolling.
        rows.sort(key=lambda r: (0 if r.get("matched") else 1, -int(r.get("score") or 0), str(r.get("symbol") or "")))
        return rows

    def status(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._build_sorted_results_locked()
            return {
                "ok": True,
                "session_id": self._session_id,
                "running": self._running,
                "phase": self._phase,
                "error": self._error_message,
                "started_at_ist": self._started_at_ist,
                "screen_id": self._screen_id,
                "screen_name": self._screen_name,
                "params": dict(self._resolved_params),
                "universe": self._universe_id,
                "universe_label": self._universe_label,
                "exchange": self._exchange,
                "timeframe": self._timeframe,
                "base_timeframe": self._base_timeframe,
                "provider": self._provider,
                "backend": self._backend,
                "required_bars": self._required_bars,
                "lookback_days": self._lookback_days,
                "poll_interval_sec": self._poll_interval_sec,
                "columns": list(self._columns),
                "warmup": {
                    "completed": self._warmup_completed,
                    "total": self._warmup_total,
                    "pct": int(round((self._warmup_completed / max(1, self._warmup_total)) * 100)) if self._warmup_total else 0,
                    "message": self._warmup_message,
                    "error_count": len(self._warmup_errors),
                    "errors": list(self._warmup_errors[:25]),
                },
                "live": {
                    "active_feed": self._active_feed,
                    "transport": self._transport,
                    "tick_seq": self._tick_seq,
                    "ticks_processed": self._ticks_processed,
                    "latency_ms": self._latency_ms,
                    "last_tick_ist": self._last_tick_ist,
                    "symbols_ready": len(self._sym_state),
                    "symbols_live": sum(1 for st in self._sym_state.values() if st.get("has_live_tick")),
                },
                "matched_count": sum(1 for r in rows if r.get("matched")),
                "evaluated_count": len(rows),
                "results": rows,
                "alerts": list(self._alerts[:20]),
                "available_backends": available_backends(),
            }


live_screener_engine = LiveScreenerEngine()

__all__ = ["LiveScreenerEngine", "live_screener_engine", "merge_live_tick_into_candles"]
