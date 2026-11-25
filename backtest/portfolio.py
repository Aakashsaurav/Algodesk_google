"""
backtest/portfolio.py
=====================
Dual-Mode (Individual & Portfolio) Multi-Symbol Backtester with Real 2026 Indian
Brokerage & Statutory Charges (`charges.calculator`).

Supports BOTH:
- Visual No-Code Strategies (`rules.engine`, including Advance mathematical formulas)
  AND custom Python `.py` Strategies (`backtest.registry`) via `BacktestStrategy`.
- Next-bar open execution: signals at bar `i` close fill at bar `i + 1` open;
  strict `exit_bar_idx > entry_bar_idx` invariant ensures BUY and SELL NEVER
  happen on the same bar.
- Both LONG (`side="BUY"`) and SHORT (`side="SELL"`), intraday time windows
  (`start_time`, `end_time`), and `MIS` end-of-day square-off.
- Complete-period OHLCV + trading symbol + all strategy indicator values CSV
  export cached per `run_id` (`export_ohlcv_csv`).
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backtest.engine import (
    BacktestEngine,
    backtest_engine,
    collect_strategy_indicators,
    compute_sharpe_metrics,
    format_bar_timestamp,
    within_time_window,
)
from backtest.registry import StrategyRegistry, strategy_registry
from baskets.store import basket_store
from charges.calculator import ChargeBreakdown, ChargesConfig, charges_engine
from config import IST_TZ, config
from core.historical import (
    HistoricalDataManager,
    historical_data_manager,
    normalize_timeframe,
    resolve_base_timeframe,
)
from core.jobs import JobCancelledError
from indicators.toolkit import IndicatorToolkit, available_backends, compute_roc_series
from screener.universes import universe_resolver


VALID_ALLOCATION_MODES = ("shared_pool", "equal_split", "risk_based")
VALID_SIGNAL_RANKINGS = ("momentum", "score", "alphabetical")
_MAX_CACHED_EXPORTS = 15


def _compute_metrics_from_curve_and_trades(
    *,
    initial_capital: float,
    final_equity: float,
    raw_curve: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    timeframe: str,
    exchange: str = "NSE",
) -> Dict[str, Any]:
    cap = max(1.0, float(initial_capital))
    net_pnl = final_equity - cap
    total_return_pct = (net_pnl / cap) * 100.0

    cagr_pct = total_return_pct
    if len(raw_curve) >= 2:
        try:
            start_d = date.fromisoformat(str(raw_curve[0]["date"])[:10])
            end_d = date.fromisoformat(str(raw_curve[-1]["date"])[:10])
            span_days = max(1, (end_d - start_d).days)
            years = max(span_days / 365.25, 1.0 / 252.0)
            if years >= 0.08:
                cagr_pct = ((max(final_equity, 1.0) / cap) ** (1.0 / years) - 1.0) * 100.0
        except Exception:
            cagr_pct = total_return_pct

    peak = cap
    max_dd = 0.0
    for pt in raw_curve:
        eq = float(pt["equity"])
        if eq > peak:
            peak = eq
        dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    sharpe_meta = compute_sharpe_metrics(raw_curve, cap, timeframe, exchange=exchange)

    wins = [t for t in trades if float(t.get("pnl", 0.0)) > 0]
    losses = [t for t in trades if float(t.get("pnl", 0.0)) < 0]
    win_rate = (len(wins) / float(len(trades))) * 100.0 if trades else 0.0
    gross_win = sum(float(t["pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["pnl"]) for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

    return {
        "final_equity": round(final_equity, 2),
        "net_pnl": round(net_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": sharpe_meta["sharpe_ratio"],
        "daily_sharpe_ratio": sharpe_meta["daily_sharpe_ratio"],
        "bar_sharpe_ratio": sharpe_meta["bar_sharpe_ratio"],
        "sharpe_method": sharpe_meta["sharpe_method"],
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
    }


def _downsample_curve(raw_curve: List[Dict[str, Any]], max_points: int = 180) -> List[Dict[str, Any]]:
    if len(raw_curve) <= max_points:
        return raw_curve
    step = max(1, len(raw_curve) // max_points)
    sampled = [raw_curve[j] for j in range(0, len(raw_curve), step)]
    if sampled[-1]["date"] != raw_curve[-1]["date"]:
        sampled.append(raw_curve[-1])
    return sampled


class PortfolioBacktestEngine:
    """Unified Individual + Portfolio Backtester with 2026 Indian trading charges & OHLCV+Indicator export."""

    def __init__(
        self,
        hist_manager: Optional[HistoricalDataManager] = None,
        registry: Optional[StrategyRegistry] = None,
        single_engine: Optional[BacktestEngine] = None,
        runs_file: Optional[Path] = None,
    ) -> None:
        self._hist = hist_manager or historical_data_manager
        self._registry = registry or strategy_registry
        self._single = single_engine or backtest_engine
        self._runs_file = runs_file or (config.DATA_DIR / "backtest_runs.json")
        self._exports_dir = self._runs_file.parent / "backtest_exports"
        self._export_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _resolve_symbols(
        self,
        *,
        symbols: Optional[List[str]] = None,
        basket_id: Optional[str] = None,
        universe: Optional[str] = None,
        exchange: str = "NSE",
        timeframe: str = "1day",
    ) -> Tuple[List[str], str]:
        if basket_id:
            basket = basket_store.get(basket_id)
            if basket and basket.get("symbols"):
                return list(basket["symbols"]), f"Basket: {basket['name']}"
        if symbols and len(symbols) > 0:
            cleaned: List[str] = []
            seen: set[str] = set()
            for raw in symbols:
                s = str(raw or "").strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    cleaned.append(s)
            if cleaned:
                return cleaned, "Custom Symbols"
        if universe and universe.strip().upper() != "CUSTOM":
            bundle = universe_resolver.resolve(
                universe=universe,
                custom_symbols=None,
                exchange=exchange,
                timeframe=timeframe,
            )
            return list(bundle.get("symbols") or []), str(bundle.get("label") or universe)
        return ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"], "Default Watchlist"

    def _resolve_strategy(self, strategy: str, rule_def: Optional[Dict[str, Any]] = None):
        return self._single._resolve_strategy(strategy, rule_def=rule_def)

    def _store_export_bundle(self, run_id: str, bundle: Dict[str, Any]) -> None:
        """
        Stream full-period OHLCV + indicator rows directly to CSV files on disk
        under `backtest_exports/{run_id}/` so 8 GB RAM hosts never retain large
        per-run row arrays in memory.
        """
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "", run_id)
        if not safe_id:
            return
        by_sym: Dict[str, List[Dict[str, Any]]] = bundle.get("by_symbol") or {}
        ind_cols: List[str] = list(bundle.get("indicator_columns") or [])
        strategy_name = str(bundle.get("strategy") or "strategy")
        headers = [
            "trading_symbol",
            "exchange",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            *ind_cols,
            "entry_signal",
            "exit_signal",
            "order_action",
            "order_price",
            "exit_reason",
        ]

        with self._lock:
            try:
                run_dir = self._exports_dir / safe_id
                run_dir.mkdir(parents=True, exist_ok=True)
                symbol_files: Dict[str, str] = {}
                for sym_k in sorted(by_sym.keys()):
                    safe_sym = re.sub(r"[^A-Za-z0-9._-]+", "_", sym_k.upper()) or "SYM"
                    csv_path = run_dir / f"{safe_sym}.csv"
                    with csv_path.open("w", encoding="utf-8", newline="") as fh:
                        writer = csv.writer(fh)
                        writer.writerow(headers)
                        for r in by_sym[sym_k]:
                            writer.writerow([r.get("symbol", "") if h == "trading_symbol" else r.get(h, "") for h in headers])
                    symbol_files[sym_k.upper()] = csv_path.name

                meta = {
                    "run_id": safe_id,
                    "strategy": strategy_name,
                    "indicator_columns": ind_cols,
                    "headers": headers,
                    "symbol_files": symbol_files,
                }
                (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
                self._export_cache[safe_id] = meta
                while len(self._export_cache) > _MAX_CACHED_EXPORTS:
                    oldest = next(iter(self._export_cache))
                    self._export_cache.pop(oldest, None)

                # Prune old export directories/files beyond _MAX_CACHED_EXPORTS
                entries = sorted(self._exports_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                for old_entry in entries[_MAX_CACHED_EXPORTS:]:
                    try:
                        if old_entry.is_dir():
                            for child in old_entry.iterdir():
                                child.unlink(missing_ok=True)
                            old_entry.rmdir()
                        else:
                            old_entry.unlink(missing_ok=True)
                    except OSError:
                        pass
            except Exception:
                pass

    def _load_export_meta(self, run_id: str) -> Optional[Dict[str, Any]]:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "", run_id or "")
        if not safe_id:
            return None
        with self._lock:
            cached = self._export_cache.get(safe_id)
            if cached is not None:
                return cached
            meta_file = self._exports_dir / safe_id / "meta.json"
            if meta_file.exists():
                try:
                    data = json.loads(meta_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._export_cache[safe_id] = data
                        return data
                except Exception:
                    return None
            # Legacy single-file JSON fallback
            legacy_file = self._exports_dir / f"{safe_id}.json"
            if legacy_file.exists():
                try:
                    data = json.loads(legacy_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    return None
        return None

    def export_ohlcv_csv(self, run_id: str, symbol: Optional[str] = None) -> Tuple[str, str]:
        """
        Render complete-period OHLCV + trading symbol + all strategy indicator
        values + signals/order actions from disk-streamed CSV storage.
        """
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "", run_id or "")
        meta = self._load_export_meta(safe_id)
        if not meta:
            raise KeyError(
                f"OHLCV + Indicator export data for run '{run_id}' is not available. Re-run the backtest to generate it."
            )

        strat_slug = re.sub(r"[^a-z0-9]+", "_", str(meta.get("strategy") or "strategy").lower()).strip("_") or "strategy"
        clean_sym = (symbol or "").strip().upper()

        # Disk-streamed CSV layout (low RAM)
        if "symbol_files" in meta:
            run_dir = self._exports_dir / safe_id
            sym_files: Dict[str, str] = meta.get("symbol_files") or {}
            if clean_sym and clean_sym not in {"ALL", "COMBINED"}:
                if clean_sym not in sym_files:
                    raise KeyError(f"Symbol '{clean_sym}' not found in backtest run '{run_id}'.")
                csv_path = run_dir / sym_files[clean_sym]
                return f"{strat_slug}_{clean_sym}_ohlcv_indicators.csv", csv_path.read_text(encoding="utf-8")

            out_lines: List[str] = []
            for idx_s, sym_k in enumerate(sorted(sym_files.keys())):
                csv_path = run_dir / sym_files[sym_k]
                if not csv_path.exists():
                    continue
                content = csv_path.read_text(encoding="utf-8")
                if idx_s == 0:
                    out_lines.append(content.rstrip("\r\n"))
                else:
                    parts = content.splitlines()
                    if len(parts) > 1:
                        out_lines.append("\n".join(parts[1:]))
            return f"{strat_slug}_all_symbols_ohlcv_indicators.csv", ("\n".join(out_lines) + "\n") if out_lines else ""

        # Legacy JSON fallback
        by_sym: Dict[str, List[Dict[str, Any]]] = meta.get("by_symbol") or {}
        ind_cols: List[str] = list(meta.get("indicator_columns") or [])
        if clean_sym and clean_sym not in {"ALL", "COMBINED"}:
            if clean_sym not in by_sym:
                raise KeyError(f"Symbol '{clean_sym}' not found in backtest run '{run_id}'.")
            rows = by_sym[clean_sym]
            filename = f"{strat_slug}_{clean_sym}_ohlcv_indicators.csv"
        else:
            rows = []
            for sym_k in sorted(by_sym.keys()):
                rows.extend(by_sym[sym_k])
            filename = f"{strat_slug}_all_symbols_ohlcv_indicators.csv"

        headers = [
            "trading_symbol",
            "exchange",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            *ind_cols,
            "entry_signal",
            "exit_signal",
            "order_action",
            "order_price",
            "exit_reason",
        ]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r.get("symbol", "") if h == "trading_symbol" else r.get(h, "") for h in headers])
        return filename, buf.getvalue()

    @staticmethod
    def _classify_indicator_series(
        col_name: str,
        values: List[Optional[float]],
        closes: List[float],
        all_cols_lower: set[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Classify a strategy indicator column into:
        - `None` (skip trivial identity OHLCV copies or redundant helper flags)
        - `{"pane": "overlay" | "oscillator", "kind": "line" | "histogram", "levels": [...]}`
        """
        raw = (col_name or "").strip()
        if not raw:
            return None
        norm = re.sub(r"\s+", "", raw).lower()
        # Strip leading scalar multiplier like "1.02*sma(20)" -> "sma(20)"
        base_norm = re.sub(r"^[0-9.]+\*", "", norm)

        # Skip pure unshifted OHLCV identity copies already shown by candlesticks & volume
        if norm in {
            "open", "high", "low", "close", "volume",
            "open(0)", "high(0)", "low(0)", "close(0)", "volume(0)",
            "open[0]", "high[0]", "low[0]", "close[0]", "volume[0]",
        }:
            return None

        # Skip redundant +1/-1 Supertrend direction flag when the price-scale Supertrend line is present
        if (base_norm.startswith("supertrend_dir") or base_norm.startswith("supertrenddir")) and any(
            c.startswith("supertrend(") for c in all_cols_lower
        ):
            return None

        non_null = [v for v in values if v is not None and math.isfinite(v)]
        if not non_null:
            return None

        if "macd_hist" in base_norm or base_norm.startswith("macdhist"):
            return {"pane": "oscillator", "kind": "histogram", "levels": [0.0]}

        osc_prefixes = (
            "rsi", "macd", "adx", "plus_di", "minus_di", "stoch", "atr",
            "roc", "cci", "mfi", "williams", "willr", "obv", "cmf", "aroon",
            "volsma", "vol_sma", "volume_sma", "supertrend_dir", "supertrenddir",
            "momentum", "mom(", "ao(", "uo(", "tsi(", "trix", "dpo", "bop", "cmo", "kst",
        )
        if any(base_norm.startswith(p) for p in osc_prefixes):
            levels: List[float] = []
            if base_norm.startswith("rsi"):
                levels = [30.0, 70.0]
            elif base_norm.startswith("stoch"):
                levels = [20.0, 80.0]
            elif base_norm.startswith("adx"):
                levels = [25.0]
            elif base_norm.startswith(("macd", "roc", "cci", "mom(", "momentum", "cmf", "ao(")):
                levels = [0.0]
            return {"pane": "oscillator", "kind": "line", "levels": levels}

        overlay_prefixes = (
            "sma", "ema", "wma", "dema", "tema", "hma", "vwma", "rma", "kama", "alma",
            "vwap", "supertrend", "bb_", "bollinger", "keltner", "kc_", "donchian", "dc_",
            "rollinghigh", "rollinglow", "ichimoku", "tenkan", "kijun", "senkou", "chikou",
            "psar", "parabolic", "pivot", "max(", "min(", "high(", "low(", "open(", "close(",
        )
        if any(base_norm.startswith(p) for p in overlay_prefixes):
            return {"pane": "overlay", "kind": "line", "levels": []}

        # Numeric scale heuristic for custom Python plugin indicator columns
        valid_closes = [c for c in closes if c is not None and math.isfinite(c) and c > 0]
        if valid_closes:
            min_c = min(valid_closes)
            max_c = max(valid_closes)
            unique_small = {round(v, 4) for v in non_null[:100]}
            if unique_small.issubset({-1.0, 0.0, 1.0}):
                return {"pane": "oscillator", "kind": "line", "levels": [0.0]}
            in_price_band = sum(1 for v in non_null if (min_c * 0.4) <= v <= (max_c * 2.2))
            if in_price_band / float(len(non_null)) >= 0.7:
                return {"pane": "overlay", "kind": "line", "levels": []}

        return {"pane": "oscillator", "kind": "line", "levels": []}

    def get_chart_series(
        self,
        run_id: str,
        symbol: Optional[str] = None,
        max_bars: int = 3000,
    ) -> Dict[str, Any]:
        """
        Return the exact OHLCV bars and strategy-used indicator series for a
        backtest run & symbol so the backtest candlestick chart renders the
        strategy's real indicators instead of fixed defaults.
        """
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "", run_id or "")
        meta = self._load_export_meta(safe_id)
        if not meta:
            raise KeyError(
                f"Chart indicator data for backtest run '{run_id}' is not available."
            )

        ind_cols: List[str] = list(meta.get("indicator_columns") or [])
        clean_sym = (symbol or "").strip().upper()
        raw_rows: List[Dict[str, Any]] = []

        if "symbol_files" in meta:
            run_dir = self._exports_dir / safe_id
            sym_files: Dict[str, str] = meta.get("symbol_files") or {}
            if not sym_files:
                raise KeyError(f"No symbols stored for backtest run '{run_id}'.")
            if not clean_sym or clean_sym in {"ALL", "COMBINED"}:
                clean_sym = sorted(sym_files.keys())[0]
            if clean_sym not in sym_files:
                raise KeyError(f"Symbol '{clean_sym}' not found in backtest run '{run_id}'.")
            csv_path = run_dir / sym_files[clean_sym]
            if not csv_path.exists():
                raise KeyError(f"Export file for '{clean_sym}' is missing.")
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for rec in reader:
                    raw_rows.append(rec)
        else:
            by_sym: Dict[str, List[Dict[str, Any]]] = meta.get("by_symbol") or {}
            if not by_sym:
                raise KeyError(f"No symbols stored for backtest run '{run_id}'.")
            if not clean_sym or clean_sym in {"ALL", "COMBINED"}:
                clean_sym = sorted(by_sym.keys())[0]
            if clean_sym not in by_sym:
                raise KeyError(f"Symbol '{clean_sym}' not found in backtest run '{run_id}'.")
            raw_rows = list(by_sym[clean_sym])

        if max_bars > 0 and len(raw_rows) > max_bars:
            raw_rows = raw_rows[-max_bars:]

        def _parse_num(val: Any) -> Optional[float]:
            if val is None or val == "":
                return None
            try:
                num = float(val)
                return num if math.isfinite(num) else None
            except (TypeError, ValueError):
                return None

        bars: List[Dict[str, Any]] = []
        closes: List[float] = []
        raw_series: Dict[str, List[Optional[float]]] = {c: [] for c in ind_cols}

        for rec in raw_rows:
            ts = str(rec.get("timestamp") or "").strip()
            o_val = _parse_num(rec.get("open"))
            h_val = _parse_num(rec.get("high"))
            l_val = _parse_num(rec.get("low"))
            c_val = _parse_num(rec.get("close"))
            v_val = _parse_num(rec.get("volume")) or 0.0
            if o_val is None or h_val is None or l_val is None or c_val is None:
                continue
            bars.append(
                {
                    "ts_ist": ts,
                    "trade_date": ts[:10],
                    "open": o_val,
                    "high": h_val,
                    "low": l_val,
                    "close": c_val,
                    "volume": int(v_val),
                }
            )
            closes.append(c_val)
            for col in ind_cols:
                raw_series[col].append(_parse_num(rec.get(col)))

        all_cols_lower = {re.sub(r"\s+", "", c).lower() for c in ind_cols}
        indicators: List[Dict[str, Any]] = []
        for col in ind_cols:
            vals = raw_series[col]
            meta_cls = self._classify_indicator_series(col, vals, closes, all_cols_lower)
            if meta_cls is None:
                continue
            indicators.append(
                {
                    "id": col,
                    "label": col,
                    "pane": meta_cls["pane"],
                    "kind": meta_cls["kind"],
                    "levels": meta_cls["levels"],
                    "values": vals,
                }
            )

        return {
            "ok": True,
            "run_id": safe_id,
            "symbol": clean_sym,
            "strategy": str(meta.get("strategy") or "strategy"),
            "bars": bars,
            "indicators": indicators,
            "indicator_columns": [ind["id"] for ind in indicators],
        }

    def _save_run_snapshot(self, result: Dict[str, Any]) -> None:
        snapshot = {
            "run_id": result["run_id"],
            "mode": result["mode"],
            "allocation_mode": result.get("allocation_mode"),
            "strategy": result["strategy"],
            "strategy_label": result["strategy_label"],
            "strategy_mode": result.get("strategy_mode", "python"),
            "symbols": result["symbols"],
            "symbol_source": result.get("symbol_source", ""),
            "timeframe": result["timeframe"],
            "from_date": result["from_date"],
            "to_date": result["to_date"],
            "initial_capital": result["initial_capital"],
            "final_equity": result["final_equity"],
            "gross_pnl": result["gross_pnl"],
            "total_charges": result["total_charges"],
            "net_pnl": result["net_pnl"],
            "total_return_pct": result["total_return_pct"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "win_rate_pct": result["win_rate_pct"],
            "total_trades": result["total_trades"],
            "charges_profile": result["charges_profile"],
            "segment": result["segment"],
            "deploy_spec": result["deploy_spec"],
            "generated_at_ist": result["generated_at_ist"],
        }
        with self._lock:
            existing: List[Dict[str, Any]] = []
            try:
                if self._runs_file.exists():
                    raw = json.loads(self._runs_file.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        existing = [r for r in raw if isinstance(r, dict)]
            except Exception:
                existing = []
            existing.insert(0, snapshot)
            existing = existing[:30]
            try:
                self._runs_file.parent.mkdir(parents=True, exist_ok=True)
                self._runs_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            except Exception:
                pass

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                if self._runs_file.exists():
                    raw = json.loads(self._runs_file.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        return raw[: max(1, int(limit))]
            except Exception:
                pass
        return []

    def run(
        self,
        *,
        mode: str = "portfolio",
        symbols: Optional[List[str]] = None,
        basket_id: Optional[str] = None,
        universe: Optional[str] = None,
        exchange: str = "NSE",
        strategy: str = "ema_crossover",
        rule_def: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        holding_type: Optional[str] = None,
        strategy_cycle: Optional[int] = None,
        side: Optional[str] = None,
        initial_capital: float = 500000.0,
        allocation_mode: str = "shared_pool",
        max_open_positions: int = 5,
        risk_per_trade_pct: float = 1.5,
        signal_ranking: str = "momentum",
        stop_loss_pct: float = 3.0,
        take_profit_pct: float = 7.0,
        trailing_sl_pct: float = 0.0,
        charges_profile: str = "zerodha",
        segment: str = "equity_intraday",
        charges_overrides: Optional[Dict[str, Any]] = None,
        slippage_bps: float = 5.0,
        provider: Optional[str] = None,
        backend: str = "auto",
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        eff_mode = (mode or "portfolio").strip().lower()
        if eff_mode not in {"individual", "portfolio"}:
            eff_mode = "portfolio"

        eff_alloc = (allocation_mode or "shared_pool").strip().lower()
        if eff_alloc not in VALID_ALLOCATION_MODES:
            eff_alloc = "shared_pool"

        eff_rank = (signal_ranking or "momentum").strip().lower()
        if eff_rank not in VALID_SIGNAL_RANKINGS:
            eff_rank = "momentum"

        clean_exch = (exchange or "NSE").strip().upper()
        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)
        today_ist = datetime.now(tz=IST_TZ).date()
        end_str = (to_date or "").strip() or today_ist.isoformat()
        start_str = (from_date or "").strip() or (today_ist - timedelta(days=365)).isoformat()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()

        eff_side = (side or (rule_def.get("side") if isinstance(rule_def, dict) else None) or "BUY").strip().upper()
        eff_holding = (holding_type or ("MIS" if segment == "equity_intraday" else "CNC")).strip().upper()
        eff_segment = segment
        if holding_type:
            if eff_holding == "CNC" and segment == "equity_intraday":
                eff_segment = "equity_delivery"
            elif eff_holding == "MIS" and segment == "equity_delivery":
                eff_segment = "equity_intraday"

        resolved_syms, symbol_source = self._resolve_symbols(
            symbols=symbols,
            basket_id=basket_id,
            universe=universe,
            exchange=clean_exch,
            timeframe=tf,
        )
        resolved_syms = resolved_syms[:50]
        if not resolved_syms:
            raise ValueError("At least one symbol is required to run a backtest.")

        cap = max(5000.0, float(initial_capital or 500000.0))
        max_pos = max(1, min(len(resolved_syms), int(max_open_positions or 5)))
        risk_pct = max(0.1, min(25.0, float(risk_per_trade_pct or 1.5)))
        sl_pct = max(0.0, float(stop_loss_pct or 0.0))
        tp_pct = max(0.0, float(take_profit_pct or 0.0))
        tsl_pct = max(0.0, float(trailing_sl_pct or 0.0))

        ov = dict(charges_overrides or {})
        if "slippage_bps" not in ov and slippage_bps is not None:
            ov["slippage_bps"] = float(slippage_bps)
        charges_cfg: ChargesConfig = charges_engine.resolve_config(
            profile=charges_profile or "zerodha",
            segment=eff_segment or "equity_intraday",
            overrides=ov,
        )

        probe_strat, strat_mode = self._resolve_strategy(strategy, rule_def=rule_def)
        resolved_params = probe_strat.resolve_params(params)
        run_id = f"bt-{uuid.uuid4().hex[:10]}"

        if eff_mode == "individual":
            return self._run_individual(
                run_id=run_id,
                symbols=resolved_syms,
                symbol_source=symbol_source,
                exchange=clean_exch,
                strategy=strategy,
                strategy_id=probe_strat.id,
                strategy_label=probe_strat.name,
                strategy_mode=strat_mode,
                rule_def=rule_def,
                params=resolved_params,
                timeframe=tf,
                base_tf=base_tf,
                from_date=start_str,
                to_date=end_str,
                start_time=start_time,
                end_time=end_time,
                holding_type=eff_holding,
                side=eff_side,
                initial_capital=cap,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                tsl_pct=tsl_pct,
                charges_profile=charges_cfg.profile,
                segment=charges_cfg.segment,
                charges_overrides=ov,
                slippage_bps=charges_cfg.slippage_bps,
                provider=eff_provider,
                backend=backend,
                fetcher=fetcher,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )

        return self._run_portfolio(
            run_id=run_id,
            symbols=resolved_syms,
            symbol_source=symbol_source,
            exchange=clean_exch,
            strategy=strategy,
            strategy_id=probe_strat.id,
            strategy_label=probe_strat.name,
            strategy_mode=strat_mode,
            rule_def=rule_def,
            params=resolved_params,
            timeframe=tf,
            base_tf=base_tf,
            from_date=start_str,
            to_date=end_str,
            start_time=start_time,
            end_time=end_time,
            holding_type=eff_holding,
            side=eff_side,
            initial_capital=cap,
            allocation_mode=eff_alloc,
            max_open_positions=max_pos,
            risk_per_trade_pct=risk_pct,
            signal_ranking=eff_rank,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            tsl_pct=tsl_pct,
            charges_cfg=charges_cfg,
            charges_overrides=ov,
            provider=eff_provider,
            backend=backend,
            fetcher=fetcher,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )

    def _run_individual(
        self,
        *,
        run_id: str,
        symbols: List[str],
        symbol_source: str,
        exchange: str,
        strategy: str,
        strategy_id: str,
        strategy_label: str,
        strategy_mode: str,
        rule_def: Optional[Dict[str, Any]],
        params: Dict[str, Any],
        timeframe: str,
        base_tf: str,
        from_date: str,
        to_date: str,
        start_time: Optional[str],
        end_time: Optional[str],
        holding_type: str,
        side: str,
        initial_capital: float,
        sl_pct: float,
        tp_pct: float,
        tsl_pct: float,
        charges_profile: str,
        segment: str,
        charges_overrides: Dict[str, Any],
        slippage_bps: float,
        provider: str,
        backend: str,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]],
        progress_cb: Optional[Callable[[int, int, str], None]],
        cancel_event: Optional[threading.Event],
    ) -> Dict[str, Any]:
        per_symbol: List[Dict[str, Any]] = []
        all_trades: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        agg_charges = ChargeBreakdown()
        total_api_calls = 0
        total_skipped = 0
        total_gross_pnl = 0.0
        total_net_pnl = 0.0

        curve_by_date: Dict[str, List[float]] = {}
        export_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        export_ind_cols: List[str] = []

        for idx, sym in enumerate(symbols):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelledError("Backtest cancelled by operator.")
            if progress_cb:
                progress_cb(idx, len(symbols), f"Individual backtest: {sym} ({idx + 1}/{len(symbols)})...")
            try:
                res = self._single.run(
                    symbol=sym,
                    exchange=exchange,
                    strategy=strategy,
                    rule_def=rule_def,
                    params=params,
                    timeframe=timeframe,
                    from_date=from_date,
                    to_date=to_date,
                    start_time=start_time,
                    end_time=end_time,
                    holding_type=holding_type,
                    side=side,
                    initial_capital=initial_capital,
                    slippage_bps=slippage_bps,
                    fee_bps=0.0,
                    stop_loss_pct=sl_pct,
                    take_profit_pct=tp_pct,
                    trailing_sl_pct=tsl_pct,
                    charges_profile=charges_profile,
                    segment=segment,
                    charges_overrides=charges_overrides,
                    provider=provider,
                    backend=backend,
                    collect_export=True,
                    fetcher=fetcher,
                    cancel_event=cancel_event,
                )
                exp = res.pop("_export", None)
                if exp:
                    export_by_symbol[res["symbol"]] = exp.get("rows") or []
                    for col_name in exp.get("indicator_columns") or []:
                        if col_name not in export_ind_cols:
                            export_ind_cols.append(col_name)

                total_api_calls += int(res.get("api_calls_made", 0))
                total_skipped += int(res.get("existing_days_skipped", 0))
                total_gross_pnl += float(res.get("gross_pnl", 0.0))
                total_net_pnl += float(res.get("net_pnl", 0.0))
                cb = res.get("charges_breakdown") or {}
                agg_charges.add(
                    ChargeBreakdown(
                        turnover=float(cb.get("turnover", 0.0)),
                        brokerage=float(cb.get("brokerage", 0.0)),
                        stt=float(cb.get("stt", 0.0)),
                        exchange_txn=float(cb.get("exchange_txn", 0.0)),
                        sebi_fee=float(cb.get("sebi_fee", 0.0)),
                        gst=float(cb.get("gst", 0.0)),
                        stamp_duty=float(cb.get("stamp_duty", 0.0)),
                        dp_charges=float(cb.get("dp_charges", 0.0)),
                        slippage_cost=float(cb.get("slippage_cost", 0.0)),
                    )
                )
                per_symbol.append(
                    {
                        "symbol": res["symbol"],
                        "bars": res["bars_evaluated"],
                        "trades": res["total_trades"],
                        "win_rate_pct": res["win_rate_pct"],
                        "gross_pnl": res["gross_pnl"],
                        "charges": res["total_charges"],
                        "net_pnl": res["net_pnl"],
                        "return_pct": res["total_return_pct"],
                        "cagr_pct": res["cagr_pct"],
                        "max_drawdown_pct": res["max_drawdown_pct"],
                        "sharpe_ratio": res["sharpe_ratio"],
                        "profit_factor": res["profit_factor"],
                    }
                )
                all_trades.extend(res.get("trades") or [])
                for pt in res.get("equity_curve") or []:
                    curve_by_date.setdefault(str(pt["date"]), []).append(float(pt["equity"]))
            except JobCancelledError:
                raise
            except Exception as exc:
                errors.append({"symbol": sym, "error": str(exc)})

        if not per_symbol:
            first_err = errors[0]["error"] if errors else "No symbols produced backtest bars."
            raise ValueError(f"Individual backtest failed for all symbols: {first_err}")

        self._store_export_bundle(
            run_id,
            {
                "run_id": run_id,
                "strategy": strategy_id,
                "indicator_columns": export_ind_cols,
                "by_symbol": export_by_symbol,
            },
        )

        total_cap = initial_capital * len(per_symbol)
        final_equity = total_cap + total_net_pnl
        raw_curve: List[Dict[str, Any]] = []
        peak = total_cap
        for dt_key in sorted(curve_by_date.keys()):
            vals = curve_by_date[dt_key]
            eq = (sum(vals) / float(len(vals))) * len(per_symbol)
            if eq > peak:
                peak = eq
            dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 0.0
            raw_curve.append({"date": dt_key, "equity": round(eq, 2), "drawdown_pct": round(dd, 2)})

        all_trades.sort(key=lambda t: (str(t.get("exit_date", "")), str(t.get("symbol", ""))))
        per_symbol.sort(key=lambda r: float(r["net_pnl"]), reverse=True)
        metrics = _compute_metrics_from_curve_and_trades(
            initial_capital=total_cap,
            final_equity=final_equity,
            raw_curve=raw_curve,
            trades=all_trades,
            timeframe=timeframe,
            exchange=exchange,
        )

        if progress_cb:
            progress_cb(len(symbols), len(symbols), f"Individual backtest finished ({len(per_symbol)} symbols).")

        result = {
            "ok": True,
            "run_id": run_id,
            "mode": "individual",
            "allocation_mode": "per_symbol_independent",
            "side": "SHORT" if side == "SELL" else "LONG",
            "holding_type": holding_type,
            "start_time": start_time,
            "end_time": end_time,
            "symbol_source": symbol_source,
            "symbols": [r["symbol"] for r in per_symbol],
            "exchange": exchange,
            "strategy": strategy_id,
            "strategy_label": strategy_label,
            "strategy_mode": strategy_mode,
            "params": params,
            "timeframe": timeframe,
            "base_timeframe": base_tf,
            "resampled": timeframe != base_tf,
            "provider": provider,
            "ta_backends": available_backends(),
            "from_date": from_date,
            "to_date": to_date,
            "api_calls_made": total_api_calls,
            "existing_days_skipped": total_skipped,
            "initial_capital": round(total_cap, 2),
            "per_symbol_capital": round(initial_capital, 2),
            "gross_pnl": round(total_gross_pnl, 2),
            "total_charges": round(agg_charges.total_charges, 2),
            "charges_profile": charges_profile,
            "segment": segment,
            "charges_breakdown": agg_charges.to_dict(),
            "execution_assumptions": {
                "fill_model": "next_bar_open",
                "no_same_bar_exit": True,
                "gap_through_stops": True,
                "slippage_bps": float(slippage_bps or 0.0),
                "sharpe_method": metrics.get("sharpe_method", "daily_resampled_sqrt252"),
            },
            "indicator_columns": export_ind_cols,
            "has_ohlcv_export": True,
            **metrics,
            "equity_curve": _downsample_curve(raw_curve),
            "per_symbol": per_symbol,
            "trades": all_trades[-400:],
            "errors": errors,
            "deploy_spec": {
                "backtest_run_id": run_id,
                "mode": "individual",
                "allocation_mode": "equal_split",
                "strategy_id": strategy_id,
                "strategy_name": strategy_label,
                "strategy_mode": strategy_mode,
                "rule_def": rule_def,
                "params": params,
                "symbols": [r["symbol"] for r in per_symbol],
                "exchange": exchange,
                "timeframe": timeframe,
                "initial_capital": round(total_cap, 2),
                "max_open_positions": len(per_symbol),
                "stop_loss_pct": sl_pct,
                "take_profit_pct": tp_pct,
                "trailing_sl_pct": tsl_pct,
                "charges_profile": charges_profile,
                "segment": segment,
                "charges_overrides": charges_overrides,
            },
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }
        self._save_run_snapshot(result)
        return result

    def _run_portfolio(
        self,
        *,
        run_id: str,
        symbols: List[str],
        symbol_source: str,
        exchange: str,
        strategy: str,
        strategy_id: str,
        strategy_label: str,
        strategy_mode: str,
        rule_def: Optional[Dict[str, Any]],
        params: Dict[str, Any],
        timeframe: str,
        base_tf: str,
        from_date: str,
        to_date: str,
        start_time: Optional[str],
        end_time: Optional[str],
        holding_type: str,
        side: str,
        initial_capital: float,
        allocation_mode: str,
        max_open_positions: int,
        risk_per_trade_pct: float,
        signal_ranking: str,
        sl_pct: float,
        tp_pct: float,
        tsl_pct: float,
        charges_cfg: ChargesConfig,
        charges_overrides: Dict[str, Any],
        provider: str,
        backend: str,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]],
        progress_cb: Optional[Callable[[int, int, str], None]],
        cancel_event: Optional[threading.Event],
    ) -> Dict[str, Any]:
        is_short = side == "SELL"
        is_intraday_tf = timeframe not in {"1day", "1week", "1month"}
        mis_eod_squareoff = is_intraday_tf and holding_type == "MIS"

        # Phase 1: Load OHLCV and pre-compute per-symbol signals & indicator series ONCE
        sym_series: Dict[str, Dict[str, Any]] = {}
        errors: List[Dict[str, str]] = []
        total_api_calls = 0
        total_skipped = 0
        all_timestamps: set[str] = set()
        export_ind_cols: List[str] = []

        for idx, sym in enumerate(symbols):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelledError("Portfolio backtest cancelled by operator.")
            if progress_cb:
                progress_cb(idx, len(symbols) * 2, f"Preparing {sym} ({idx + 1}/{len(symbols)})...")
            try:
                rows, dl_meta = self._single._load_candles(
                    symbol=sym,
                    exchange=exchange,
                    timeframe=timeframe,
                    from_date=from_date,
                    to_date=to_date,
                    provider=provider,
                    fetcher=fetcher,
                )
                total_api_calls += int(dl_meta.get("api_calls_made", 0))
                total_skipped += int(dl_meta.get("existing_days_skipped", 0))
                if len(rows) < 15:
                    errors.append({"symbol": sym, "error": f"Insufficient bars ({len(rows)} < 15)."})
                    continue

                strat_inst, _ = self._resolve_strategy(strategy, rule_def=rule_def)
                tk = IndicatorToolkit(rows, backend=backend)
                strat_inst.prepare(rows, params, tk)
                ind_map = collect_strategy_indicators(strat_inst, tk, len(rows))
                for col_name in ind_map.keys():
                    if col_name not in export_ind_cols:
                        export_ind_cols.append(col_name)

                # Compute ranking momentum directly without polluting tk.recorded_series
                roc_10 = compute_roc_series(tk.closes, 10)

                bar_by_ts: Dict[str, Tuple[int, Dict[str, Any], bool, bool, float]] = {}
                export_rows: List[Dict[str, Any]] = []
                for i, bar in enumerate(rows):
                    ts_key = str(bar.get("ts_ist") or bar.get("trade_date") or "")
                    enter_sig, exit_sig = strat_inst.signal_at(i, bar)
                    mom_val = float(roc_10[i]) if (i < len(roc_10) and roc_10[i] is not None) else 0.0
                    bar_by_ts[ts_key] = (i, bar, bool(enter_sig), bool(exit_sig), mom_val)
                    all_timestamps.add(ts_key)

                    rec: Dict[str, Any] = {
                        "symbol": dl_meta["symbol"],
                        "exchange": dl_meta["exchange"],
                        "timestamp": format_bar_timestamp(bar, timeframe),
                        "open": round(float(bar["open"]), 4),
                        "high": round(float(bar["high"]), 4),
                        "low": round(float(bar["low"]), 4),
                        "close": round(float(bar["close"]), 4),
                        "volume": int(float(bar.get("volume") or 0)),
                    }
                    for ind_k, ind_vals in ind_map.items():
                        iv = ind_vals[i]
                        rec[ind_k] = round(float(iv), 4) if iv is not None else ""
                    rec["entry_signal"] = 1 if enter_sig else 0
                    rec["exit_signal"] = 1 if exit_sig else 0
                    rec["order_action"] = ""
                    rec["order_price"] = ""
                    rec["exit_reason"] = ""
                    export_rows.append(rec)

                sym_series[dl_meta["symbol"]] = {
                    "bars_count": len(rows),
                    "bar_by_ts": bar_by_ts,
                    "export_rows": export_rows,
                    "last_close": float(rows[0]["close"]),
                }
            except JobCancelledError:
                raise
            except Exception as exc:
                errors.append({"symbol": sym, "error": str(exc)})

        if not sym_series:
            first_err = errors[0]["error"] if errors else "No symbols loaded for portfolio backtest."
            raise ValueError(f"Portfolio backtest failed: {first_err}")

        ordered_ts = sorted(all_timestamps)
        active_syms = list(sym_series.keys())
        eff_max_slots = max(1, min(len(active_syms), max_open_positions))

        # Phase 2: Chronological Portfolio Event Loop with NEXT-BAR OPEN execution & strict no-same-bar guard
        cash = float(initial_capital)
        positions: Dict[str, Dict[str, Any]] = {}
        pending_entries: Dict[str, float] = {}  # sym -> ranking momentum from prior bar close
        pending_exits: set[str] = set()
        trades: List[Dict[str, Any]] = []
        raw_curve: List[Dict[str, Any]] = []
        agg_charges = ChargeBreakdown()
        sym_stats: Dict[str, Dict[str, Any]] = {
            s: {"symbol": s, "bars": sym_series[s]["bars_count"], "trades": 0, "wins": 0, "gross_pnl": 0.0, "charges": 0.0, "net_pnl": 0.0}
            for s in active_syms
        }

        peak_equity = cash
        total_gross_pnl = 0.0

        def _mark_to_market_equity() -> float:
            eq = cash
            for psym, pos in positions.items():
                cur_c = sym_series[psym]["last_close"]
                if is_short:
                    eq += (pos["qty"] * pos["entry_price"]) + ((pos["entry_price"] - cur_c) * pos["qty"])
                else:
                    eq += pos["qty"] * cur_c
            return eq

        def _close_position(psym: str, exit_px: float, exit_ts: str, exit_step_idx: int, sym_bar_idx: Optional[int], reason: str) -> None:
            nonlocal cash, total_gross_pnl
            pos = positions.pop(psym)
            qty = int(pos["qty"])
            entry_px = float(pos["entry_price"])
            gross_pnl = (entry_px - exit_px) * qty if is_short else (exit_px - entry_px) * qty
            total_gross_pnl += gross_pnl

            exit_side = "BUY" if is_short else "SELL"
            exit_leg = charges_engine.calculate_leg(
                side=exit_side, price=exit_px, quantity=qty, cfg=charges_cfg, include_slippage=True
            )
            trade_chg = ChargeBreakdown()
            trade_chg.add(pos["entry_leg"])
            trade_chg.add(exit_leg)
            agg_charges.add(trade_chg)

            net_pnl = gross_pnl - trade_chg.total_charges
            if is_short:
                cash += (qty * entry_px) + gross_pnl - exit_leg.total_charges
            else:
                cash += (qty * exit_px) - exit_leg.total_charges
            ret_pct = (net_pnl / max(1.0, qty * entry_px)) * 100.0

            trades.append(
                {
                    "symbol": psym,
                    "side": "SHORT" if is_short else "LONG",
                    "entry_date": pos["entry_date"],
                    "exit_date": exit_ts,
                    "entry_bar_idx": pos["entry_bar_idx"],
                    "exit_bar_idx": exit_step_idx,
                    "qty": qty,
                    "entry_price": round(entry_px, 2),
                    "exit_price": round(exit_px, 2),
                    "gross_pnl": round(gross_pnl, 2),
                    "charges": round(trade_chg.total_charges, 2),
                    "pnl": round(net_pnl, 2),
                    "return_pct": round(ret_pct, 2),
                    "exit_reason": reason,
                }
            )
            if sym_bar_idx is not None and 0 <= sym_bar_idx < len(sym_series[psym]["export_rows"]):
                er = sym_series[psym]["export_rows"][sym_bar_idx]
                er["order_action"] = exit_side
                er["order_price"] = round(exit_px, 4)
                er["exit_reason"] = reason

            st = sym_stats[psym]
            st["trades"] += 1
            if net_pnl > 0:
                st["wins"] += 1
            st["gross_pnl"] += gross_pnl
            st["charges"] += trade_chg.total_charges
            st["net_pnl"] += net_pnl

        total_steps = len(ordered_ts)
        for step_idx, ts_key in enumerate(ordered_ts):
            if cancel_event is not None and (step_idx % 50 == 0) and cancel_event.is_set():
                raise JobCancelledError("Portfolio backtest cancelled by operator.")
            is_last_bar = step_idx == (total_steps - 1)
            cur_day = ts_key[:10]
            next_day = ordered_ts[step_idx + 1][:10] if step_idx + 1 < total_steps else ""
            is_session_last_bar = mis_eod_squareoff and (is_last_bar or next_day != cur_day)

            # Determine formatted bar timestamp for this step
            step_ts_label = ts_key[:10] if not is_intraday_tf else ts_key[:16].replace("T", " ")

            # 1) Execute PENDING EXITS from previous bar at THIS bar's OPEN (frees cash & slots first)
            for sym in list(pending_exits):
                rec = sym_series[sym]["bar_by_ts"].get(ts_key)
                if rec is None:
                    continue
                sym_i, bar, _, _, _ = rec
                b_open = float(bar["open"])
                bar_ts = format_bar_timestamp(bar, timeframe)
                if sym in positions and step_idx > positions[sym]["entry_bar_idx"]:
                    _close_position(sym, b_open, bar_ts, step_idx, sym_i, "STRATEGY_EXIT")
                pending_exits.discard(sym)

            # 2) Execute PENDING ENTRIES from previous bar at THIS bar's OPEN (ranked by signal_ranking)
            entered_this_step: set[str] = set()
            if pending_entries and not is_last_bar and not is_session_last_bar and len(positions) < eff_max_slots:
                ranked_candidates: List[Tuple[str, float, float, int, str]] = []
                for sym, mom_val in list(pending_entries.items()):
                    if sym in positions:
                        pending_entries.pop(sym, None)
                        continue
                    rec = sym_series[sym]["bar_by_ts"].get(ts_key)
                    if rec is None:
                        continue
                    sym_i, bar, _, _, _ = rec
                    if not within_time_window(bar, start_time, end_time, timeframe):
                        pending_entries.pop(sym, None)
                        continue
                    b_open = float(bar["open"])
                    bar_ts = format_bar_timestamp(bar, timeframe)
                    ranked_candidates.append((sym, b_open, mom_val, sym_i, bar_ts))
                    pending_entries.pop(sym, None)

                if signal_ranking == "alphabetical":
                    ranked_candidates.sort(key=lambda c: c[0])
                elif signal_ranking == "score":
                    ranked_candidates.sort(key=lambda c: (abs(c[2]), c[2], c[0]), reverse=True)
                else:
                    ranked_candidates.sort(key=lambda c: (c[2], c[0]), reverse=True)

                entry_side = "SELL" if is_short else "BUY"
                for sym, entry_px, _, sym_i, bar_ts in ranked_candidates:
                    open_slots = eff_max_slots - len(positions)
                    if open_slots <= 0 or cash <= 0 or entry_px <= 0:
                        break
                    cur_equity = _mark_to_market_equity()
                    est_rate = (charges_cfg.slippage_bps / 10000.0) + charges_cfg.stt_buy_pct + charges_cfg.stamp_duty_buy_pct + 0.001
                    if allocation_mode == "equal_split":
                        target_budget = min(cash, initial_capital / float(eff_max_slots))
                        qty = int(max(0.0, target_budget - charges_cfg.brokerage_flat_per_order * 1.2) // (entry_px * (1.0 + est_rate)))
                    elif allocation_mode == "risk_based":
                        eff_sl = sl_pct if sl_pct > 0 else 2.0
                        risk_amt = cur_equity * (risk_per_trade_pct / 100.0)
                        risk_per_share = max(0.05, entry_px * (eff_sl / 100.0))
                        qty_by_risk = int(risk_amt // risk_per_share)
                        max_cash_budget = min(cash, cur_equity * 0.50)
                        qty_by_cash = int(max(0.0, max_cash_budget - charges_cfg.brokerage_flat_per_order * 1.2) // (entry_px * (1.0 + est_rate)))
                        qty = min(qty_by_risk, qty_by_cash)
                    else:
                        target_budget = cash / float(open_slots)
                        qty = int(max(0.0, target_budget - charges_cfg.brokerage_flat_per_order * 1.2) // (entry_px * (1.0 + est_rate)))

                    if qty < 1:
                        continue
                    entry_leg = charges_engine.calculate_leg(
                        side=entry_side, price=entry_px, quantity=qty, cfg=charges_cfg, include_slippage=True
                    )
                    total_outlay = (qty * entry_px) + entry_leg.total_charges
                    if total_outlay > cash and qty > 1:
                        qty = max(1, int((cash - entry_leg.total_charges) // entry_px))
                        entry_leg = charges_engine.calculate_leg(
                            side=entry_side, price=entry_px, quantity=qty, cfg=charges_cfg, include_slippage=True
                        )
                        total_outlay = (qty * entry_px) + entry_leg.total_charges
                    if qty >= 1 and total_outlay <= cash:
                        cash -= total_outlay
                        positions[sym] = {
                            "qty": qty,
                            "entry_price": entry_px,
                            "entry_date": bar_ts,
                            "entry_bar_idx": step_idx,
                            "entry_leg": entry_leg,
                            "extreme": entry_px,
                        }
                        entered_this_step.add(sym)
                        er = sym_series[sym]["export_rows"][sym_i]
                        er["order_action"] = entry_side
                        er["order_price"] = round(entry_px, 4)
            else:
                pending_entries.clear()

            # 3) Intrabar SL/TSL/TP/MIS-EOD checks (ONLY on bars strictly AFTER entry bar) + schedule NEXT-BAR signals
            for sym in active_syms:
                rec = sym_series[sym]["bar_by_ts"].get(ts_key)
                if rec is None:
                    continue
                sym_i, bar, enter_sig, exit_sig, mom_val = rec
                b_open = float(bar["open"])
                b_close = float(bar["close"])
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                bar_ts = format_bar_timestamp(bar, timeframe)
                sym_series[sym]["last_close"] = b_close

                if sym in positions and sym not in entered_this_step and step_idx > positions[sym]["entry_bar_idx"]:
                    pos = positions[sym]
                    reason = ""
                    exit_px = b_close
                    if not is_short:
                        if b_high > pos["extreme"]:
                            pos["extreme"] = b_high
                        stop_lvl = pos["entry_price"] * (1.0 - sl_pct / 100.0)
                        tsl_lvl = pos["extreme"] * (1.0 - tsl_pct / 100.0)
                        tp_lvl = pos["entry_price"] * (1.0 + tp_pct / 100.0)
                        if sl_pct > 0 and b_low <= stop_lvl:
                            exit_px = min(b_open, stop_lvl) if b_open < stop_lvl else stop_lvl
                            reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                        elif tsl_pct > 0 and pos["extreme"] > pos["entry_price"] and b_low <= tsl_lvl:
                            exit_px = min(b_open, tsl_lvl) if b_open < tsl_lvl else tsl_lvl
                            reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                        elif tp_pct > 0 and b_high >= tp_lvl:
                            exit_px = max(b_open, tp_lvl) if b_open > tp_lvl else tp_lvl
                            reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                        elif is_session_last_bar:
                            exit_px = b_close
                            reason = "MIS_EOD_SQUAREOFF"
                    else:
                        if b_low < pos["extreme"]:
                            pos["extreme"] = b_low
                        stop_lvl = pos["entry_price"] * (1.0 + sl_pct / 100.0)
                        tsl_lvl = pos["extreme"] * (1.0 + tsl_pct / 100.0)
                        tp_lvl = pos["entry_price"] * (1.0 - tp_pct / 100.0)
                        if sl_pct > 0 and b_high >= stop_lvl:
                            exit_px = max(b_open, stop_lvl) if b_open > stop_lvl else stop_lvl
                            reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                        elif tsl_pct > 0 and pos["extreme"] < pos["entry_price"] and b_high >= tsl_lvl:
                            exit_px = max(b_open, tsl_lvl) if b_open > tsl_lvl else tsl_lvl
                            reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                        elif tp_pct > 0 and b_low <= tp_lvl:
                            exit_px = min(b_open, tp_lvl) if b_open < tp_lvl else tp_lvl
                            reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                        elif is_session_last_bar:
                            exit_px = b_close
                            reason = "MIS_EOD_SQUAREOFF"

                    if reason:
                        _close_position(sym, exit_px, bar_ts, step_idx, sym_i, reason)
                        continue

                # Schedule NEXT-BAR signals evaluated at bar close
                if not is_last_bar:
                    if sym in positions and exit_sig:
                        pending_exits.add(sym)
                    elif sym not in positions and enter_sig and not is_session_last_bar:
                        if within_time_window(bar, start_time, end_time, timeframe):
                            pending_entries[sym] = mom_val

            # 4) Mark-to-market portfolio curve point
            eq = _mark_to_market_equity()
            if eq > peak_equity:
                peak_equity = eq
            dd = ((peak_equity - eq) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            raw_curve.append(
                {
                    "date": step_ts_label,
                    "equity": round(eq, 2),
                    "drawdown_pct": round(dd, 2),
                    "open_positions": len(positions),
                }
            )

        # Force-close open positions at end of window
        last_step_idx = max(0, total_steps - 1)
        last_ts_label = (ordered_ts[-1][:10] if not is_intraday_tf else ordered_ts[-1][:16].replace("T", " ")) if ordered_ts else to_date
        for psym in list(positions.keys()):
            last_sym_i = sym_series[psym]["bars_count"] - 1
            _close_position(psym, sym_series[psym]["last_close"], last_ts_label, last_step_idx, last_sym_i, "END_OF_WINDOW")

        # Persist complete-period OHLCV + Indicator rows directly to disk CSVs
        self._store_export_bundle(
            run_id,
            {
                "run_id": run_id,
                "strategy": strategy_id,
                "indicator_columns": export_ind_cols,
                "by_symbol": {s: sym_series[s]["export_rows"] for s in active_syms},
            },
        )

        final_equity = cash
        metrics = _compute_metrics_from_curve_and_trades(
            initial_capital=initial_capital,
            final_equity=final_equity,
            raw_curve=raw_curve,
            trades=trades,
            timeframe=timeframe,
            exchange=exchange,
        )

        per_symbol_rows: List[Dict[str, Any]] = []
        for s, st in sym_stats.items():
            tr = int(st["trades"])
            wr = (st["wins"] / float(tr)) * 100.0 if tr > 0 else 0.0
            per_symbol_rows.append(
                {
                    "symbol": s,
                    "bars": st["bars"],
                    "trades": tr,
                    "win_rate_pct": round(wr, 1),
                    "gross_pnl": round(st["gross_pnl"], 2),
                    "charges": round(st["charges"], 2),
                    "net_pnl": round(st["net_pnl"], 2),
                    "contribution_pct": round((st["net_pnl"] / initial_capital) * 100.0, 2),
                }
            )
        per_symbol_rows.sort(key=lambda r: float(r["net_pnl"]), reverse=True)

        if progress_cb:
            progress_cb(
                len(symbols) * 2,
                len(symbols) * 2,
                f"Portfolio backtest complete ({len(trades)} trades, Net PnL ₹{metrics['net_pnl']:,.2f}).",
            )

        result = {
            "ok": True,
            "run_id": run_id,
            "mode": "portfolio",
            "allocation_mode": allocation_mode,
            "side": "SHORT" if is_short else "LONG",
            "holding_type": holding_type,
            "start_time": start_time,
            "end_time": end_time,
            "max_open_positions": eff_max_slots,
            "risk_per_trade_pct": risk_per_trade_pct,
            "signal_ranking": signal_ranking,
            "symbol_source": symbol_source,
            "symbols": active_syms,
            "exchange": exchange,
            "strategy": strategy_id,
            "strategy_label": strategy_label,
            "strategy_mode": strategy_mode,
            "params": params,
            "timeframe": timeframe,
            "base_timeframe": base_tf,
            "resampled": timeframe != base_tf,
            "provider": provider,
            "ta_backends": available_backends(),
            "from_date": ordered_ts[0][:10] if ordered_ts else from_date,
            "to_date": ordered_ts[-1][:10] if ordered_ts else to_date,
            "api_calls_made": total_api_calls,
            "existing_days_skipped": total_skipped,
            "initial_capital": round(initial_capital, 2),
            "gross_pnl": round(total_gross_pnl, 2),
            "total_charges": round(agg_charges.total_charges, 2),
            "charges_profile": charges_cfg.profile,
            "segment": charges_cfg.segment,
            "charges_breakdown": agg_charges.to_dict(),
            "execution_assumptions": {
                "fill_model": "next_bar_open",
                "no_same_bar_exit": True,
                "gap_through_stops": True,
                "slippage_bps": float(charges_cfg.slippage_bps or 0.0),
                "sharpe_method": metrics.get("sharpe_method", "daily_resampled_sqrt252"),
            },
            "indicator_columns": export_ind_cols,
            "has_ohlcv_export": True,
            **metrics,
            "equity_curve": _downsample_curve(raw_curve),
            "per_symbol": per_symbol_rows,
            "trades": trades[-400:],
            "errors": errors,
            "deploy_spec": {
                "backtest_run_id": run_id,
                "mode": "portfolio",
                "allocation_mode": allocation_mode,
                "strategy_id": strategy_id,
                "strategy_name": strategy_label,
                "strategy_mode": strategy_mode,
                "rule_def": rule_def,
                "params": params,
                "symbols": active_syms,
                "exchange": exchange,
                "timeframe": timeframe,
                "initial_capital": round(initial_capital, 2),
                "max_open_positions": eff_max_slots,
                "risk_per_trade_pct": risk_per_trade_pct,
                "stop_loss_pct": sl_pct,
                "take_profit_pct": tp_pct,
                "trailing_sl_pct": tsl_pct,
                "charges_profile": charges_cfg.profile,
                "segment": charges_cfg.segment,
                "charges_overrides": charges_overrides,
            },
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }
        self._save_run_snapshot(result)
        return result


portfolio_backtest_engine = PortfolioBacktestEngine()

__all__ = [
    "PortfolioBacktestEngine",
    "VALID_ALLOCATION_MODES",
    "VALID_SIGNAL_RANKINGS",
    "portfolio_backtest_engine",
]
