"""
screener/engine.py
==================
Orchestrates point-in-time ("As-Of Date") zero-lookahead multi-symbol market scans
across named universes (NIFTY 50, NIFTY F&O, NIFTY 500, etc.) using drop-in
`.py` screens and IndicatorToolkit.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from config import IST_TZ, config
from core.historical import HistoricalDataManager, _parse_iso_date, historical_data_manager
from core.jobs import JobCancelledError
from indicators.toolkit import IndicatorToolkit, available_backends
from screener.registry import ScreenRegistry, screen_registry
from screener.universes import UniverseResolver, universe_resolver


class ScreenerEngine:
    """Point-in-time multi-universe market screener with hot-reloaded `.py` screens."""

    def __init__(
        self,
        hist_manager: Optional[HistoricalDataManager] = None,
        registry: Optional[ScreenRegistry] = None,
        universes: Optional[UniverseResolver] = None,
    ) -> None:
        self._hist = hist_manager or historical_data_manager
        self._registry = registry or screen_registry
        self._universes = universes or universe_resolver

    def _load_candles(
        self,
        *,
        symbol: str,
        exchange: str,
        timeframe: str,
        from_date: str,
        to_date: str,
        provider: str,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ):
        dl_meta = self._hist.download(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            provider=provider,
            fetcher=fetcher,
        )
        records = self._hist.get_records(
            symbol=dl_meta["symbol"],
            exchange=dl_meta["exchange"],
            timeframe=dl_meta["timeframe"],
            from_date=from_date,
            to_date=to_date,
            view="full",
        )
        return records.get("rows", []), dl_meta

    def scan(
        self,
        *,
        symbols: Optional[List[str]] = None,
        universe: str = "CUSTOM",
        exchange: str = "NSE",
        timeframe: str = "1day",
        lookback_days: int = 120,
        as_of_date: Optional[str] = None,
        screen_id: Optional[str] = None,
        preset: str = "all_metrics",
        params: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        backend: str = "auto",
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        target_screen_id = screen_id or preset or "all_metrics"
        screen = self._registry.get(target_screen_id)
        resolved_params = screen.resolve_params(params)

        tf = (timeframe or "1day").strip().lower()
        eff_exch = (exchange or "NSE").strip().upper()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()
        days = max(30, min(int(lookback_days or 120), 1095))

        today_ist = datetime.now(tz=IST_TZ).date()
        eff_end_d: date = _parse_iso_date(as_of_date, default=today_ist)
        if eff_end_d > today_ist:
            eff_end_d = today_ist
        eff_start_d = eff_end_d - timedelta(days=days)
        from_date = eff_start_d.isoformat()
        to_date = eff_end_d.isoformat()

        uni_bundle = self._universes.resolve(
            universe=universe,
            custom_symbols=symbols,
            exchange=eff_exch,
            timeframe=tf,
        )
        clean_symbols: List[str] = uni_bundle["symbols"]

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        total_api_calls = 0
        total_cached_skipped = 0

        total = len(clean_symbols)
        for idx, sym in enumerate(clean_symbols):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelledError(f"Screener cancelled at {idx}/{total} ({sym}).")
            if progress_cb:
                progress_cb(idx, total, f"Scanning {sym} ({idx + 1}/{total}) as of {to_date} via {eff_provider.upper()}...")
            try:
                rows, dl_meta = self._load_candles(
                    symbol=sym,
                    exchange=eff_exch,
                    timeframe=tf,
                    from_date=from_date,
                    to_date=to_date,
                    provider=eff_provider,
                    fetcher=fetcher,
                )
                total_api_calls += int(dl_meta.get("api_calls_made", 0))
                total_cached_skipped += int(dl_meta.get("existing_days_skipped", 0))

                # Strictly enforce zero-lookahead As-Of Date boundary on bars
                rows = [r for r in rows if str(r.get("trade_date") or str(r.get("ts_ist", ""))[:10])[:10] <= to_date]

                min_req = max(5, int(getattr(screen, "min_bars", 15) or 15))
                if len(rows) < min_req:
                    errors.append({"symbol": sym, "error": f"Insufficient bars ({len(rows)} < {min_req}) up to {to_date}."})
                    continue

                tk = IndicatorToolkit(rows, backend=backend)
                match = screen.evaluate(rows, resolved_params, tk)

                last = rows[-1]
                prev = rows[-2] if len(rows) >= 2 else last
                last_close = float(last["close"])
                prev_close = float(prev["close"])
                day_chg_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

                row_payload: Dict[str, Any] = {
                    "symbol": dl_meta["symbol"],
                    "exchange": dl_meta["exchange"],
                    "bars": len(rows),
                    "last_date": str(last.get("trade_date") or str(last.get("ts_ist", ""))[:10]),
                    "close": round(last_close, 2),
                    "change_pct": round(day_chg_pct, 2),
                    "signal": match.signal,
                    "tags": list(match.tags),
                    "score": int(match.score),
                    "matched": bool(match.matched),
                    "api_calls": int(dl_meta.get("api_calls_made", 0)),
                }
                row_payload.update(match.metrics or {})
                results.append(row_payload)
            except JobCancelledError:
                raise
            except Exception as exc:
                errors.append({"symbol": sym, "error": str(exc)})

        if progress_cb:
            progress_cb(total, total, f"Screener completed ({len(results)} symbols evaluated as of {to_date}).")

        matched_rows = [r for r in results if r["matched"]]
        matched_rows.sort(key=lambda r: (r["score"], abs(r["change_pct"])), reverse=True)

        storage_meta = self._hist.storage_info()
        return {
            "ok": True,
            "screen_id": screen.id,
            "preset": screen.id,
            "preset_label": screen.name,
            "screen_name": screen.name,
            "params": resolved_params,
            "universe": uni_bundle["universe"],
            "universe_label": uni_bundle["label"],
            "as_of_date": to_date,
            "from_date": from_date,
            "is_historical_as_of": eff_end_d < today_ist,
            "provider": eff_provider,
            "exchange": eff_exch,
            "timeframe": tf,
            "lookback_days": days,
            "storage_backend": storage_meta["active_mode"],
            "ta_backends": available_backends(),
            "scanned_count": len(clean_symbols),
            "evaluated_count": len(results),
            "matched_count": len(matched_rows),
            "api_calls_made": total_api_calls,
            "existing_days_skipped": total_cached_skipped,
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
            "results": matched_rows,
            "all_evaluated_count": len(results),
            "errors": errors,
        }


screener_engine = ScreenerEngine()

__all__ = ["ScreenerEngine", "screener_engine"]
