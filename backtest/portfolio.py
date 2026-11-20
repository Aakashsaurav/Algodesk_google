"""
backtest/portfolio.py
=====================
Dual-Mode (Individual & Portfolio) Multi-Symbol Backtester with Real 2026 Indian
Brokerage & Statutory Charges (`charges.calculator`).

Supports BOTH:
- Visual No-Code Strategies (`rules.engine`) AND custom Python `.py` Strategies
  (`backtest.registry`) via the exact same `BacktestStrategy` interface.
- `mode="individual"`: simulates every symbol independently with its own capital
  envelope, producing per-symbol metrics + aggregated overview.
- `mode="portfolio"`: simulates all symbols chronologically across aligned
  timestamps sharing one portfolio cash pool with ALL THREE confirmed capital
  allocation modes:
    1. `shared_pool`: shared cash pool + `max_open_positions` + signal ranking
       (`momentum` | `score` | `alphabetical`).
    2. `equal_split`: fixed equal capital slice per slot (`initial_capital / max_slots`).
    3. `risk_based`: stop-loss-distance position sizing
       (`qty = floor((equity * risk_per_trade_pct / 100) / (entry_price * stop_loss_pct / 100))`).

Persists compact immutable backtest run snapshots (`data/backtest_runs.json`) so
the exact backtested artifact can be promoted 1-click into Paper Trade or Live Trade.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backtest.engine import BacktestEngine, backtest_engine
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
from indicators.toolkit import IndicatorToolkit, available_backends
from screener.universes import universe_resolver


VALID_ALLOCATION_MODES = ("shared_pool", "equal_split", "risk_based")
VALID_SIGNAL_RANKINGS = ("momentum", "score", "alphabetical")


def _compute_metrics_from_curve_and_trades(
    *,
    initial_capital: float,
    final_equity: float,
    raw_curve: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    timeframe: str,
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
    rets: List[float] = []
    prev_eq = cap
    for pt in raw_curve:
        eq = float(pt["equity"])
        if eq > peak:
            peak = eq
        dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if prev_eq > 0:
            rets.append((eq - prev_eq) / prev_eq)
        prev_eq = eq

    sharpe = 0.0
    if len(rets) >= 2:
        mean_r = sum(rets) / float(len(rets))
        var_r = sum((r - mean_r) ** 2 for r in rets) / float(len(rets) - 1)
        std_r = math.sqrt(max(0.0, var_r))
        if std_r > 0:
            ann_factor = 252.0 if timeframe == "1day" else (252.0 * 12.0)
            sharpe = (mean_r / std_r) * math.sqrt(ann_factor)

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
        "sharpe_ratio": round(sharpe, 2),
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
    """Unified Individual + Portfolio Backtester with 2026 Indian trading charges."""

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

        resolved_syms, symbol_source = self._resolve_symbols(
            symbols=symbols,
            basket_id=basket_id,
            universe=universe,
            exchange=clean_exch,
            timeframe=tf,
        )
        # Cap runaway single-request portfolio sizes cleanly while allowing large NIFTY50 runs
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
            segment=segment or "equity_intraday",
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

        # Build an averaged/combined equity curve indexed by date across symbols
        curve_by_date: Dict[str, List[float]] = {}

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
                    fetcher=fetcher,
                    cancel_event=cancel_event,
                )
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

        total_cap = initial_capital * len(per_symbol)
        final_equity = total_cap + total_net_pnl
        raw_curve: List[Dict[str, Any]] = []
        peak = total_cap
        for dt_key in sorted(curve_by_date.keys()):
            vals = curve_by_date[dt_key]
            # Scale average per-symbol equity to total deployed individual capital
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
        )

        if progress_cb:
            progress_cb(len(symbols), len(symbols), f"Individual backtest finished ({len(per_symbol)} symbols).")

        result = {
            "ok": True,
            "run_id": run_id,
            "mode": "individual",
            "allocation_mode": "per_symbol_independent",
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
        # Phase 1: Load OHLCV and pre-compute per-symbol signals ONCE (O(N) per symbol)
        sym_series: Dict[str, Dict[str, Any]] = {}
        errors: List[Dict[str, str]] = []
        total_api_calls = 0
        total_skipped = 0
        all_timestamps: set[str] = set()

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
                roc_10 = tk.roc(10)

                bar_by_ts: Dict[str, Tuple[int, Dict[str, Any], bool, bool, float]] = {}
                for i, bar in enumerate(rows):
                    ts_key = str(bar.get("ts_ist") or bar.get("trade_date") or "")
                    enter_sig, exit_sig = strat_inst.signal_at(i, bar)
                    mom_val = float(roc_10[i]) if (i < len(roc_10) and roc_10[i] is not None) else 0.0
                    bar_by_ts[ts_key] = (i, bar, bool(enter_sig), bool(exit_sig), mom_val)
                    all_timestamps.add(ts_key)

                sym_series[dl_meta["symbol"]] = {
                    "bars_count": len(rows),
                    "bar_by_ts": bar_by_ts,
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

        # Phase 2: Chronological Portfolio Event Loop (shared cash pool + allocation modes)
        cash = float(initial_capital)
        positions: Dict[str, Dict[str, Any]] = {}
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
                eq += pos["qty"] * sym_series[psym]["last_close"]
            return eq

        def _close_position(psym: str, exit_px: float, exit_date: str, reason: str) -> None:
            nonlocal cash, total_gross_pnl
            pos = positions.pop(psym)
            qty = int(pos["qty"])
            entry_px = float(pos["entry_price"])
            gross_pnl = (exit_px - entry_px) * qty
            total_gross_pnl += gross_pnl

            sell_leg = charges_engine.calculate_leg(
                side="SELL", price=exit_px, quantity=qty, cfg=charges_cfg, include_slippage=True
            )
            trade_chg = ChargeBreakdown()
            trade_chg.add(pos["entry_leg"])
            trade_chg.add(sell_leg)
            agg_charges.add(trade_chg)

            net_pnl = gross_pnl - trade_chg.total_charges
            cash += (qty * exit_px) - sell_leg.total_charges
            ret_pct = (net_pnl / max(1.0, qty * entry_px)) * 100.0

            trades.append(
                {
                    "symbol": psym,
                    "entry_date": pos["entry_date"],
                    "exit_date": exit_date,
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
            bar_date = ts_key[:10]

            # 1) Update last_close and process EXITS first (frees cash & slots cleanly)
            entry_candidates: List[Tuple[str, float, float]] = []
            for sym in active_syms:
                rec = sym_series[sym]["bar_by_ts"].get(ts_key)
                if rec is None:
                    continue
                _, bar, enter_sig, exit_sig, mom_val = rec
                b_close = float(bar["close"])
                b_high = float(bar["high"])
                b_low = float(bar["low"])
                sym_series[sym]["last_close"] = b_close

                if sym in positions:
                    pos = positions[sym]
                    if b_high > pos["highest"]:
                        pos["highest"] = b_high
                    reason = ""
                    exit_px = b_close
                    if sl_pct > 0 and b_low <= pos["entry_price"] * (1.0 - sl_pct / 100.0):
                        exit_px = pos["entry_price"] * (1.0 - sl_pct / 100.0)
                        reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                    elif tsl_pct > 0 and pos["highest"] > pos["entry_price"] and b_low <= pos["highest"] * (1.0 - tsl_pct / 100.0):
                        exit_px = pos["highest"] * (1.0 - tsl_pct / 100.0)
                        reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                    elif tp_pct > 0 and b_high >= pos["entry_price"] * (1.0 + tp_pct / 100.0):
                        exit_px = pos["entry_price"] * (1.0 + tp_pct / 100.0)
                        reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                    elif exit_sig:
                        reason = "STRATEGY_EXIT"
                    if reason:
                        _close_position(sym, exit_px, bar_date, reason)
                elif enter_sig and not is_last_bar:
                    entry_candidates.append((sym, b_close, mom_val))

            # 2) Rank & allocate ENTRIES across available portfolio slots
            if entry_candidates and len(positions) < eff_max_slots:
                if signal_ranking == "alphabetical":
                    entry_candidates.sort(key=lambda c: c[0])
                elif signal_ranking == "score":
                    entry_candidates.sort(key=lambda c: (abs(c[2]), c[2], c[0]), reverse=True)
                else:
                    # Default: momentum (highest 10-bar ROC first)
                    entry_candidates.sort(key=lambda c: (c[2], c[0]), reverse=True)

                for sym, entry_px, _ in entry_candidates:
                    open_slots = eff_max_slots - len(positions)
                    if open_slots <= 0 or cash <= 0 or entry_px <= 0:
                        break

                    cur_equity = _mark_to_market_equity()
                    if allocation_mode == "equal_split":
                        target_budget = min(cash, initial_capital / float(eff_max_slots))
                        est_rate = (charges_cfg.slippage_bps / 10000.0) + charges_cfg.stt_buy_pct + charges_cfg.stamp_duty_buy_pct + 0.001
                        qty = int(max(0.0, target_budget - charges_cfg.brokerage_flat_per_order * 1.2) // (entry_px * (1.0 + est_rate)))
                    elif allocation_mode == "risk_based":
                        eff_sl = sl_pct if sl_pct > 0 else 2.0
                        risk_amt = cur_equity * (risk_per_trade_pct / 100.0)
                        risk_per_share = max(0.05, entry_px * (eff_sl / 100.0))
                        qty_by_risk = int(risk_amt // risk_per_share)
                        # Cap single position by remaining cash & 50% of equity
                        max_cash_budget = min(cash, cur_equity * 0.50)
                        est_rate = (charges_cfg.slippage_bps / 10000.0) + charges_cfg.stt_buy_pct + charges_cfg.stamp_duty_buy_pct + 0.001
                        qty_by_cash = int(max(0.0, max_cash_budget - charges_cfg.brokerage_flat_per_order * 1.2) // (entry_px * (1.0 + est_rate)))
                        qty = min(qty_by_risk, qty_by_cash)
                    else:
                        # shared_pool: divide available cash across remaining free slots
                        target_budget = cash / float(open_slots)
                        est_rate = (charges_cfg.slippage_bps / 10000.0) + charges_cfg.stt_buy_pct + charges_cfg.stamp_duty_buy_pct + 0.001
                        qty = int(max(0.0, target_budget - charges_cfg.brokerage_flat_per_order * 1.2) // (entry_px * (1.0 + est_rate)))

                    if qty < 1:
                        continue
                    buy_leg = charges_engine.calculate_leg(
                        side="BUY", price=entry_px, quantity=qty, cfg=charges_cfg, include_slippage=True
                    )
                    total_outlay = (qty * entry_px) + buy_leg.total_charges
                    if total_outlay > cash and qty > 1:
                        qty = max(1, int((cash - buy_leg.total_charges) // entry_px))
                        buy_leg = charges_engine.calculate_leg(
                            side="BUY", price=entry_px, quantity=qty, cfg=charges_cfg, include_slippage=True
                        )
                        total_outlay = (qty * entry_px) + buy_leg.total_charges
                    if qty >= 1 and total_outlay <= cash:
                        cash -= total_outlay
                        positions[sym] = {
                            "qty": qty,
                            "entry_price": entry_px,
                            "entry_date": bar_date,
                            "entry_leg": buy_leg,
                            "highest": entry_px,
                        }

            # 3) Mark-to-market portfolio curve point
            eq = _mark_to_market_equity()
            if eq > peak_equity:
                peak_equity = eq
            dd = ((peak_equity - eq) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            raw_curve.append(
                {
                    "date": bar_date,
                    "equity": round(eq, 2),
                    "drawdown_pct": round(dd, 2),
                    "open_positions": len(positions),
                }
            )

        # Force-close open positions at end of window
        last_date = ordered_ts[-1][:10] if ordered_ts else to_date
        for psym in list(positions.keys()):
            _close_position(psym, sym_series[psym]["last_close"], last_date, "END_OF_WINDOW")

        final_equity = cash
        metrics = _compute_metrics_from_curve_and_trades(
            initial_capital=initial_capital,
            final_equity=final_equity,
            raw_curve=raw_curve,
            trades=trades,
            timeframe=timeframe,
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
