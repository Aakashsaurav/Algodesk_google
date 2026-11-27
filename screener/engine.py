"""
screener/engine.py
==================
Orchestrates point-in-time ("As-Of Date") zero-lookahead multi-symbol market scans
across named universes (NIFTY 50, NIFTY F&O, NIFTY 500, etc.) using drop-in
`.py` screens and IndicatorToolkit.
"""

from __future__ import annotations

import math
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import IST_TZ, config
from core.historical import (
    HistoricalDataManager,
    _parse_iso_date,
    historical_data_manager,
    normalize_timeframe,
    parse_timeframe,
    resample_ohlcv_rows,
    resolve_base_timeframe,
)
from core.jobs import JobCancelledError
from indicators.toolkit import IndicatorToolkit, available_backends
from screener.base import ColumnSpec
from screener.nse_universe_sync import nse_universe_sync
from screener.registry import ScreenRegistry, screen_registry
from screener.sr_engine import filter_complete_weekly_candles, short_tf_label, slice_candles_as_of
from screener.universes import UniverseResolver, universe_resolver


_OPTION_SYMBOL_RE = re.compile(r"(?:\d+(?:\.\d+)?(?:CE|PE)$|\bOPT(?:STK|IDX)?\b)", re.IGNORECASE)
_FUTURES_SYMBOL_RE = re.compile(r"(?:FUT$|\d{2}[A-Z]{3}FUT$)", re.IGNORECASE)

OPTIONS_EXCLUDED_NOTICE = (
    "Option-premium contracts (CE/PE/OPT) are excluded from Support & Resistance screening; "
    "evaluate structural zones on the underlying cash equity or futures contract instead."
)


def is_option_contract(symbol: str, instrument_type: Optional[str] = None) -> bool:
    """Detect NSE/BSE option contracts (CE/PE/OPT) without misclassifying cash equities."""
    itype = str(instrument_type or "").strip().upper()
    if itype in {"CE", "PE", "OPT", "OPTSTK", "OPTIDX"} or itype.startswith("OPT"):
        return True
    clean = str(symbol or "").strip().upper()
    if not clean:
        return False
    return bool(_OPTION_SYMBOL_RE.search(clean))


def is_futures_contract(symbol: str, instrument_type: Optional[str] = None) -> bool:
    """Detect NSE/BSE futures contracts (FUT/FUTSTK/FUTIDX)."""
    itype = str(instrument_type or "").strip().upper()
    if itype.startswith("FUT"):
        return True
    clean = str(symbol or "").strip().upper()
    return bool(clean and _FUTURES_SYMBOL_RE.search(clean))


def normalize_segments(segments: Optional[List[str]]) -> Tuple[List[str], bool]:
    """
    Normalize requested market segments into canonical ['EQ'], ['FUT'], or ['EQ', 'FUT'].
    Returns `(normalized_segments, options_requested)`.
    """
    if not segments:
        return ["EQ"], False
    out: List[str] = []
    options_requested = False
    for raw in segments:
        tok = str(raw or "").strip().upper()
        if tok in {"OPT", "OPTIONS", "CE", "PE", "OPTSTK", "OPTIDX"}:
            options_requested = True
            continue
        if tok in {"EQ", "EQUITY", "CASH", "SPOT"}:
            if "EQ" not in out:
                out.append("EQ")
        elif tok in {"FUT", "FUTURES", "FUTSTK", "FUTIDX", "FNO_FUT"}:
            if "FUT" not in out:
                out.append("FUT")
        elif tok in {"EQ+FUT", "EQ_FUT", "ALL"}:
            for item in ("EQ", "FUT"):
                if item not in out:
                    out.append(item)
    return (out or ["EQ"]), options_requested


def compute_auto_lookback_days(required_bars: int, timeframe: str) -> int:
    """
    Convert required indicator bar count into calendar days before As-Of Date,
    accounting for weekends, NSE holidays (~250 trading days/yr), and NSE
    intraday session length (375 minutes/day = 09:15–15:30 IST).
    """
    count, unit = parse_timeframe(timeframe)
    bars = max(15, int(required_bars or 60))

    if unit == "minute":
        bars_per_day = max(1, 375 // count)
        trading_days = math.ceil(bars / float(bars_per_day)) + 2
        return max(7, min(730, math.ceil(trading_days * 1.6) + 5))
    if unit == "week":
        return max(90, min(4500, bars * 7 * count + 28))
    if unit == "month":
        return max(180, min(5475, bars * 32 * count + 45))
    # Daily / N-day bars (~250 trading days/365 calendar days ≈ 1.46x + holiday buffer)
    return max(45, min(4500, math.ceil(bars * count * 1.55) + 15))


def _infer_columns_from_metrics(sample_metrics: Dict[str, Any]) -> List[ColumnSpec]:
    """Auto-generate clean table columns for custom `.py` screens that don't define `get_columns()`."""
    cols: List[ColumnSpec] = []
    for k, v in sample_metrics.items():
        if k in {"symbol", "exchange", "bars", "last_date", "close", "change_pct", "signal", "tags", "score", "matched", "api_calls"}:
            continue
        label = k.replace("_", " ").upper() if len(k) <= 6 else k.replace("_", " ").title()
        if isinstance(v, bool) or isinstance(v, str):
            fmt = "badge" if k.lower() in {"supertrend", "trend", "bias", "regime", "state"} else "text"
            cols.append(ColumnSpec(key=k, label=label, fmt=fmt, decimals=0, align="left"))
        elif k.endswith("_pct") or "pct" in k.lower():
            fmt = "signed_pct" if ("dist" in k.lower() or "chg" in k.lower() or "ret" in k.lower()) else "pct"
            cols.append(ColumnSpec(key=k, label=label, fmt=fmt, decimals=2, align="right"))
        elif "ratio" in k.lower() and "vol" in k.lower():
            cols.append(ColumnSpec(key=k, label=label, fmt="ratio", decimals=2, align="right"))
        else:
            cols.append(ColumnSpec(key=k, label=label, fmt="number", decimals=2, align="right"))
    return cols


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

    def _resolve_fno_eligible_set(self) -> set[str]:
        """Resolve NSE F&O eligible underlying symbols from local CSV/SQLite without network calls."""
        fno_syms: set[str] = set()
        try:
            csv_syms, _ = nse_universe_sync.read_universe_csv("NIFTY_FNO")
            for s in csv_syms:
                fno_syms.add(str(s).strip().upper())
        except Exception:
            pass
        return fno_syms

    def _expand_segment_targets(
        self,
        *,
        clean_symbols: List[str],
        exchanges: List[str],
        segments: List[str],
        as_of_date: str,
    ) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, str]]]:
        """
        Expand symbols into segment-tagged scan targets (`EQ` and/or active `FUT` contracts)
        across requested exchanges (`NSE`, `BSE`), strictly excluding option contracts and
        never silently stitching across futures expiries.
        """
        targets: List[Dict[str, Any]] = []
        excluded_options = 0
        missing_contracts: List[Dict[str, str]] = []
        seen_keys: set[Tuple[str, str, str]] = set()

        include_eq = "EQ" in segments
        include_fut = "FUT" in segments

        try:
            from core.instruments import instrument_index
        except Exception:
            instrument_index = None  # type: ignore[assignment]

        for raw_sym in clean_symbols:
            sym_u = str(raw_sym or "").strip().upper()
            if not sym_u:
                continue
            if is_option_contract(sym_u):
                excluded_options += 1
                continue

            sym_is_fut = is_futures_contract(sym_u)
            for exch in exchanges:
                ex_u = str(exch or "NSE").strip().upper()
                if sym_is_fut:
                    if include_fut:
                        fut_exch = "BFO" if ex_u in {"BSE", "BFO"} else "NFO"
                        key = (sym_u, fut_exch, "FUT")
                        if key not in seen_keys:
                            seen_keys.add(key)
                            expiry_val = None
                            if instrument_index is not None:
                                try:
                                    c_obj = instrument_index.get_contract(sym_u, exchange=fut_exch)
                                    if c_obj is not None:
                                        expiry_val = c_obj.expiry
                                except Exception:
                                    pass
                            targets.append(
                                {
                                    "symbol": sym_u,
                                    "underlying": sym_u,
                                    "exchange": fut_exch,
                                    "instrument_category": "FUT",
                                    "contract_expiry": expiry_val,
                                }
                            )
                    continue

                if include_eq:
                    eq_exch = "BSE" if ex_u in {"BSE", "BFO"} else "NSE"
                    key = (sym_u, eq_exch, "EQ")
                    if key not in seen_keys:
                        seen_keys.add(key)
                        targets.append(
                            {
                                "symbol": sym_u,
                                "underlying": sym_u,
                                "exchange": eq_exch,
                                "instrument_category": "EQ",
                                "contract_expiry": None,
                            }
                        )

                if include_fut:
                    fut_exch = "BFO" if ex_u in {"BSE", "BFO"} else "NFO"
                    found_fut = False
                    if instrument_index is not None:
                        try:
                            fno_out = instrument_index.list_fno_contracts(
                                underlying=sym_u,
                                exchange=fut_exch,
                                contract_type="FUT",
                                include_expired=False,
                                limit=6,
                            )
                            contracts = [
                                c for c in (fno_out.get("contracts") or [])
                                if str(c.get("asset_symbol") or "").strip().upper() == sym_u
                                and not is_option_contract(
                                    str(c.get("trading_symbol") or ""),
                                    str(c.get("instrument_type") or ""),
                                )
                            ]
                            # Sort by nearest expiry first; keep active contracts distinct (no stitching)
                            contracts.sort(key=lambda c: str(c.get("expiry") or "9999-12-31"))
                            for c in contracts[:2]:
                                c_sym = str(c.get("trading_symbol") or "").strip().upper()
                                if not c_sym:
                                    continue
                                key = (c_sym, fut_exch, "FUT")
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    targets.append(
                                        {
                                            "symbol": c_sym,
                                            "underlying": sym_u,
                                            "exchange": fut_exch,
                                            "instrument_category": "FUT",
                                            "contract_expiry": c.get("expiry"),
                                            "instrument_key": c.get("instrument_key"),
                                        }
                                    )
                                    found_fut = True
                        except Exception:
                            found_fut = False

                    if not found_fut:
                        missing_contracts.append(
                            {
                                "symbol": sym_u,
                                "exchange": fut_exch,
                                "timeframe": "ALL",
                                "canonical_timeframe": "ALL",
                                "reason": (
                                    f"No active {fut_exch} futures contract found in local instrument master for {sym_u} "
                                    f"as of {as_of_date} (sync instrument master to screen FUT contracts)."
                                ),
                            }
                        )

        return targets, excluded_options, missing_contracts

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
        # Read ALL locally cached bars <= to_date (zero extra API calls) so recursive
        # indicators like Slow EMA (200/233), Wilder RSI, and ADX achieve maximum convergence.
        records = self._hist.get_records(
            symbol=dl_meta["symbol"],
            exchange=dl_meta["exchange"],
            timeframe=dl_meta["timeframe"],
            from_date=None,
            to_date=to_date,
            view="full",
        )
        return records.get("rows", []), dl_meta

    def _scan_multi_timeframe(
        self,
        *,
        screen: Any,
        resolved_params: Dict[str, Any],
        clean_symbols: List[str],
        uni_bundle: Dict[str, Any],
        eff_exch: str,
        target_tfs: List[str],
        eff_end_d: date,
        today_ist: date,
        eff_provider: str,
        lookback_days: Optional[int],
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]],
        progress_cb: Optional[Callable[[int, int, str], None]],
        cancel_event: Optional[threading.Event],
        segments: Optional[List[str]] = None,
        exchanges: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Multi-timeframe orchestration layer reusing canonical base series:
        - Fetches `1minute` base data ONCE per symbol for 5m / 15m / 1h.
        - Fetches `1day` base data ONCE per symbol for 1d / 1w.
        - Derives all higher timeframes in-memory via `resample_ohlcv_rows`
          and strictly excludes incomplete weekly candles for point-in-time accuracy.
        - Supports EQ and active NSE/BSE FUT contracts while explicitly excluding
          option-premium contracts.
        - Reports missing `(symbol, timeframe, reason)` explicitly without
          fabricating candles or silently substituting daily data for intraday.
        """
        to_date = eff_end_d.isoformat()
        eff_segments, options_requested = normalize_segments(segments)
        eff_exchanges = [
            str(e).strip().upper()
            for e in (exchanges if (exchanges and len(exchanges) > 0) else [eff_exch])
            if str(e).strip()
        ] or [eff_exch]

        targets, excluded_options, missing_contracts = self._expand_segment_targets(
            clean_symbols=clean_symbols,
            exchanges=eff_exchanges,
            segments=eff_segments,
            as_of_date=to_date,
        )

        intraday_tfs = [t for t in target_tfs if resolve_base_timeframe(t) == "1minute"]
        daily_tfs = [t for t in target_tfs if resolve_base_timeframe(t) == "1day"]

        def _req_bars_for(tf_name: str) -> int:
            if hasattr(screen, "required_bars_for_timeframe"):
                return int(screen.required_bars_for_timeframe(tf_name, resolved_params))
            return int(screen.required_bars(resolved_params))

        intra_days = 0
        if intraday_tfs:
            intra_days = max(compute_auto_lookback_days(_req_bars_for(t), t) for t in intraday_tfs)
            if lookback_days is not None and int(lookback_days) > 0:
                intra_days = max(intra_days, min(int(lookback_days), 180))
        daily_days = 0
        if daily_tfs:
            daily_days = max(compute_auto_lookback_days(_req_bars_for(t), t) for t in daily_tfs)
            if lookback_days is not None and int(lookback_days) > 0:
                daily_days = max(daily_days, min(int(lookback_days), 3650))

        intra_from_date = (eff_end_d - timedelta(days=intra_days)).isoformat() if intra_days > 0 else to_date
        daily_from_date = (eff_end_d - timedelta(days=daily_days)).isoformat() if daily_days > 0 else to_date
        overall_from_date = min(
            [d for d in (intra_from_date if intraday_tfs else None, daily_from_date if daily_tfs else None) if d]
            or [to_date]
        )

        fno_set = self._resolve_fno_eligible_set()
        declared_cols = screen.get_columns(resolved_params)

        all_rows: List[Dict[str, Any]] = []
        missing_data: List[Dict[str, str]] = list(missing_contracts)
        errors: List[Dict[str, str]] = []
        evaluated_symbols = 0
        total_api_calls = 0
        total_cached_skipped = 0

        total = len(targets)
        for idx, target in enumerate(targets):
            sym = target["symbol"]
            target_exch = target["exchange"]
            inst_cat = target["instrument_category"]
            contract_expiry = target.get("contract_expiry")
            underlying = target.get("underlying") or sym

            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelledError(f"Screener cancelled at {idx}/{total} ({sym}).")
            if progress_cb:
                progress_cb(
                    idx,
                    total,
                    f"Scanning {sym} [{inst_cat}] ({idx + 1}/{total}) across {len(target_tfs)} TFs as of {to_date}...",
                )

            sym_api_calls = 0
            sym_norm = sym.strip().upper()
            candles_by_tf: Dict[str, List[Dict[str, Any]]] = {}
            sym_missing: List[Dict[str, str]] = []

            # 1) Intraday base fetch: download & read `1minute` ONCE per symbol
            if intraday_tfs:
                raw_1m: List[Dict[str, Any]] = []
                intra_err = ""
                try:
                    dl_1m = self._hist.download(
                        symbol=sym,
                        exchange=target_exch,
                        timeframe="1minute",
                        from_date=intra_from_date,
                        to_date=to_date,
                        provider=eff_provider,
                        fetcher=fetcher,
                    )
                    sym_norm = dl_1m["symbol"]
                    sym_api_calls += int(dl_1m.get("api_calls_made", 0))
                    total_api_calls += int(dl_1m.get("api_calls_made", 0))
                    total_cached_skipped += int(dl_1m.get("existing_days_skipped", 0))
                    rec_1m = self._hist.get_records(
                        symbol=dl_1m["symbol"],
                        exchange=dl_1m["exchange"],
                        timeframe="1minute",
                        from_date=None,
                        to_date=to_date,
                        view="full",
                    )
                    raw_1m = slice_candles_as_of(rec_1m.get("rows", []), to_date)
                except JobCancelledError:
                    raise
                except Exception as exc:
                    intra_err = str(exc)

                if raw_1m:
                    for itf in intraday_tfs:
                        resampled = raw_1m if itf == "1minute" else resample_ohlcv_rows(raw_1m, itf, exchange=target_exch)
                        candles_by_tf[itf] = resampled
                else:
                    reason = intra_err or f"No 1minute intraday data available from {eff_provider.upper()} up to {to_date}"
                    for itf in intraday_tfs:
                        sym_missing.append(
                            {
                                "symbol": sym_norm,
                                "exchange": target_exch,
                                "timeframe": short_tf_label(itf),
                                "canonical_timeframe": itf,
                                "reason": reason,
                            }
                        )

            # 2) Daily/Weekly base fetch: download & read `1day` ONCE per symbol
            if daily_tfs:
                raw_1d: List[Dict[str, Any]] = []
                daily_err = ""
                try:
                    dl_1d = self._hist.download(
                        symbol=sym,
                        exchange=target_exch,
                        timeframe="1day",
                        from_date=daily_from_date,
                        to_date=to_date,
                        provider=eff_provider,
                        fetcher=fetcher,
                    )
                    sym_norm = dl_1d["symbol"]
                    sym_api_calls += int(dl_1d.get("api_calls_made", 0))
                    total_api_calls += int(dl_1d.get("api_calls_made", 0))
                    total_cached_skipped += int(dl_1d.get("existing_days_skipped", 0))
                    rec_1d = self._hist.get_records(
                        symbol=dl_1d["symbol"],
                        exchange=dl_1d["exchange"],
                        timeframe="1day",
                        from_date=None,
                        to_date=to_date,
                        view="full",
                    )
                    raw_1d = slice_candles_as_of(rec_1d.get("rows", []), to_date)
                except JobCancelledError:
                    raise
                except Exception as exc:
                    daily_err = str(exc)

                if raw_1d:
                    for dtf in daily_tfs:
                        if dtf == "1day":
                            candles_by_tf[dtf] = raw_1d
                        elif dtf == "1week":
                            candles_by_tf[dtf] = filter_complete_weekly_candles(raw_1d, as_of_date=to_date, exchange=target_exch)
                        else:
                            candles_by_tf[dtf] = resample_ohlcv_rows(raw_1d, dtf, exchange=target_exch)
                else:
                    reason = daily_err or f"No 1day data available from {eff_provider.upper()} up to {to_date}"
                    for dtf in daily_tfs:
                        sym_missing.append(
                            {
                                "symbol": sym_norm,
                                "exchange": target_exch,
                                "timeframe": short_tf_label(dtf),
                                "canonical_timeframe": dtf,
                                "reason": reason,
                            }
                        )

            missing_data.extend(sym_missing)

            if not candles_by_tf:
                errors.append(
                    {
                        "symbol": sym_norm,
                        "error": sym_missing[0]["reason"] if sym_missing else f"No historical candles available up to {to_date}.",
                    }
                )
                continue

            try:
                eval_kwargs: Dict[str, Any] = {
                    "symbol": sym_norm,
                    "exchange": target_exch,
                    "candles_by_tf": candles_by_tf,
                    "params": resolved_params,
                    "as_of_date": to_date,
                    "fno_eligible": (underlying in fno_set or inst_cat == "FUT"),
                    "api_calls": sym_api_calls,
                    "instrument_category": inst_cat,
                    "contract_expiry": contract_expiry,
                    "data_source": eff_provider,
                    "adjustment_basis": "split_adjusted_ohlcv",
                }
                eval_out = screen.evaluate_multi_timeframe(**eval_kwargs)
                if eval_out.get("evaluated_timeframes"):
                    evaluated_symbols += 1
                if eval_out.get("missing_data"):
                    missing_data.extend(eval_out["missing_data"])
                if eval_out.get("rows"):
                    all_rows.extend(eval_out["rows"])
            except JobCancelledError:
                raise
            except Exception as exc:
                errors.append({"symbol": sym_norm, "error": str(exc)})

        if progress_cb:
            progress_cb(
                total,
                total,
                f"Multi-TF scan completed ({evaluated_symbols}/{total} instruments, {len(all_rows)} S&R events).",
            )

        # Priority Ranking: Tier 1 (Major Breakout/Breakdown) -> Tier 2 (Major Retest) ->
        # Tier 3 (Major Bounce/Rejection) -> Tier 4 (Near Major) -> Tier 5 (Minor Events) -> Tier 6 (Other)
        all_rows.sort(
            key=lambda r: (
                -int(r.get("rank_tier", 6)),
                float(r.get("strength", 0.0)),
                -abs(float(r.get("distance_pct", 0.0))),
            ),
            reverse=True,
        )

        major_count = sum(1 for r in all_rows if r.get("classification") == "Major")
        minor_count = sum(1 for r in all_rows if r.get("classification") == "Minor")
        short_tfs = [short_tf_label(t) for t in target_tfs]
        storage_meta = self._hist.storage_info()

        return {
            "ok": True,
            "screen_id": screen.id,
            "preset": screen.id,
            "preset_label": screen.name,
            "screen_name": screen.name,
            "multi_timeframe": True,
            "multi_row": True,
            "params": resolved_params,
            "columns": [c.to_dict() for c in declared_cols],
            "universe": uni_bundle["universe"],
            "universe_label": uni_bundle["label"],
            "as_of_date": to_date,
            "from_date": overall_from_date,
            "is_historical_as_of": eff_end_d < today_ist,
            "provider": eff_provider,
            "exchange": eff_exch,
            "exchanges": eff_exchanges,
            "segments": eff_segments,
            "options_excluded_count": excluded_options,
            "options_excluded_notice": (
                OPTIONS_EXCLUDED_NOTICE if (excluded_options > 0 or options_requested or "FUT" in eff_segments) else None
            ),
            "timeframe": ",".join(short_tfs),
            "timeframes": short_tfs,
            "canonical_timeframes": target_tfs,
            "base_timeframe": "1minute+1day" if (intraday_tfs and daily_tfs) else ("1minute" if intraday_tfs else "1day"),
            "resampled": True,
            "required_bars": int(screen.required_bars(resolved_params)),
            "lookback_days": max(daily_days, intra_days),
            "lookback_mode": "manual_min_auto" if lookback_days else "auto",
            "storage_backend": storage_meta["active_mode"],
            "ta_backends": available_backends(),
            "scanned_count": len(targets),
            "evaluated_count": evaluated_symbols,
            "matched_count": len(all_rows),
            "events_found": len(all_rows),
            "major_count": major_count,
            "minor_count": minor_count,
            "missing_data_count": len(missing_data),
            "missing_data": missing_data,
            "price_adjustment_note": (
                "Split-adjusted historical OHLCV preserved without dividend total-return distortion; "
                "incomplete weekly candles excluded as of as_of_date."
            ),
            "api_calls_made": total_api_calls,
            "existing_days_skipped": total_cached_skipped,
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
            "results": all_rows,
            "all_evaluated_count": evaluated_symbols,
            "errors": errors,
        }

    def scan(
        self,
        *,
        symbols: Optional[List[str]] = None,
        universe: str = "CUSTOM",
        exchange: str = "NSE",
        exchanges: Optional[List[str]] = None,
        segments: Optional[List[str]] = None,
        timeframe: str = "1day",
        timeframes: Optional[List[str]] = None,
        lookback_days: Optional[int] = None,
        as_of_date: Optional[str] = None,
        screen_id: Optional[str] = None,
        preset: str = "all_metrics",
        rule_def: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        backend: str = "auto",
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        target_screen_id = screen_id or preset or "all_metrics"
        if isinstance(rule_def, dict) and (rule_def.get("entry") or rule_def.get("conditions")):
            from rules.engine import compile_visual_screen

            screen = compile_visual_screen(rule_def)
        else:
            try:
                screen = self._registry.get(target_screen_id)
            except Exception:
                from rules.engine import compile_visual_screen, visual_rule_store

                stored_rule = visual_rule_store.get(target_screen_id)
                if stored_rule is None:
                    raise
                screen = compile_visual_screen(stored_rule)
        resolved_params = screen.resolve_params(params)

        eff_exch = (exchange or "NSE").strip().upper()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()

        today_ist = datetime.now(tz=IST_TZ).date()
        eff_end_d: date = _parse_iso_date(as_of_date, default=today_ist)
        if eff_end_d > today_ist:
            eff_end_d = today_ist

        # Backward-compatible Multi-Timeframe orchestration path
        if bool(getattr(screen, "supports_multi_timeframe", False)) and hasattr(screen, "evaluate_multi_timeframe"):
            raw_tfs = timeframes if (timeframes and len(timeframes) > 0) else (
                list(getattr(screen, "default_timeframes", []) or []) or [timeframe]
            )
            norm_tfs: List[str] = []
            seen_tfs: set[str] = set()
            for t in raw_tfs:
                nt = normalize_timeframe(str(t))
                if nt not in seen_tfs:
                    seen_tfs.add(nt)
                    norm_tfs.append(nt)

            uni_bundle = self._universes.resolve(
                universe=universe,
                custom_symbols=symbols,
                exchange=eff_exch,
                timeframe=norm_tfs[0] if norm_tfs else "1day",
            )
            return self._scan_multi_timeframe(
                screen=screen,
                resolved_params=resolved_params,
                clean_symbols=uni_bundle["symbols"],
                uni_bundle=uni_bundle,
                eff_exch=eff_exch,
                target_tfs=norm_tfs,
                eff_end_d=eff_end_d,
                today_ist=today_ist,
                eff_provider=eff_provider,
                lookback_days=lookback_days,
                fetcher=fetcher,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
                segments=segments,
                exchanges=exchanges,
            )

        # Standard Single-Timeframe path (100% preserved for all existing screens)
        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)

        req_bars = int(screen.required_bars(resolved_params))
        auto_days = compute_auto_lookback_days(req_bars, tf)
        if lookback_days is not None and int(lookback_days) > 0:
            days = max(auto_days, min(int(lookback_days), 3650))
            lookback_mode = "manual_min_auto"
        else:
            days = auto_days
            lookback_mode = "auto"

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

        declared_cols = screen.get_columns(resolved_params)
        inferred_cols: Optional[List[ColumnSpec]] = None

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
                if not declared_cols and inferred_cols is None and match.metrics:
                    inferred_cols = _infer_columns_from_metrics(match.metrics)

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
                    "ltp": round(last_close, 2),
                    "volume": int(last.get("volume") or 0),
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

        active_columns = declared_cols or inferred_cols or []
        storage_meta = self._hist.storage_info()
        return {
            "ok": True,
            "screen_id": screen.id,
            "preset": screen.id,
            "preset_label": screen.name,
            "screen_name": screen.name,
            "params": resolved_params,
            "columns": [c.to_dict() for c in active_columns],
            "universe": uni_bundle["universe"],
            "universe_label": uni_bundle["label"],
            "as_of_date": to_date,
            "from_date": from_date,
            "is_historical_as_of": eff_end_d < today_ist,
            "provider": eff_provider,
            "exchange": eff_exch,
            "timeframe": tf,
            "base_timeframe": base_tf,
            "resampled": tf != base_tf,
            "required_bars": req_bars,
            "lookback_days": days,
            "lookback_mode": lookback_mode,
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

__all__ = ["ScreenerEngine", "compute_auto_lookback_days", "screener_engine"]
