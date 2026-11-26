"""
backtest/engine.py
==================
Single-pass O(N) backtester loading hot-reloaded `.py` strategies from
`StrategyRegistry`, powered by Hybrid Parquet/SQLite OHLCV storage and IndicatorToolkit.

Execution realism:
- Signals evaluated at the close of bar `i` execute on the NEXT bar `i + 1` at
  `bar[i + 1]["open"]` (zero look-ahead bias).
- Strict `exit_bar_idx > entry_bar_idx` invariant: BUY and SELL never occur on
  the same bar.
- Supports both LONG (`side="BUY"`) and SHORT (`side="SELL"`), intraday session
  time windows (`start_time` / `end_time`), and `MIS` end-of-day square-off.
- Optionally collects complete-period OHLCV + symbol + strategy indicator values
  for CSV export.
"""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from backtest.registry import StrategyRegistry, strategy_registry
from config import IST_TZ, config
from core.calendar import session_minutes
from core.historical import (
    HistoricalDataManager,
    historical_data_manager,
    normalize_timeframe,
    parse_timeframe,
    resolve_base_timeframe,
)
from core.jobs import JobCancelledError
from indicators.toolkit import IndicatorToolkit, available_backends


def compute_sharpe_metrics(
    raw_curve: List[Dict[str, Any]],
    initial_capital: float,
    timeframe: str,
    exchange: str = "NSE",
) -> Dict[str, Any]:
    """
    Compute statistically sound Sharpe ratios across all timeframes:
    - `daily_sharpe_ratio`: Headline Sharpe computed from end-of-day resampled
      equity returns annualized by `sqrt(252)` (comparable across 1m, 5m, 15m, 1d).
    - `bar_sharpe_ratio`: Bar-frequency Sharpe annualized by the exact number of
      bars per trading year for `(timeframe, exchange)`.
    """
    count, unit = parse_timeframe(timeframe)
    if unit == "minute":
        sess_mins = float(session_minutes(exchange))
        bars_per_year = 252.0 * max(1.0, sess_mins / float(max(1, count)))
    elif unit == "day":
        bars_per_year = 252.0 / float(max(1, count))
    elif unit == "week":
        bars_per_year = 52.0
    elif unit == "month":
        bars_per_year = 12.0
    else:
        bars_per_year = 252.0

    # Bar-level returns
    bar_rets: List[float] = []
    prev_eq = max(1.0, float(initial_capital))
    eod_equity: Dict[str, float] = {}
    for pt in raw_curve:
        eq = float(pt.get("equity", prev_eq))
        if prev_eq > 0:
            bar_rets.append((eq - prev_eq) / prev_eq)
        prev_eq = eq
        day_key = str(pt.get("date") or "")[:10]
        if day_key:
            eod_equity[day_key] = eq

    bar_sharpe = 0.0
    if len(bar_rets) >= 2:
        mean_b = sum(bar_rets) / float(len(bar_rets))
        var_b = sum((r - mean_b) ** 2 for r in bar_rets) / float(len(bar_rets) - 1)
        std_b = math.sqrt(max(0.0, var_b))
        if std_b > 1e-12:
            bar_sharpe = (mean_b / std_b) * math.sqrt(bars_per_year)

    # Daily-resampled returns (end-of-day equity per calendar trading date)
    daily_rets: List[float] = []
    prev_d_eq = max(1.0, float(initial_capital))
    for d_key in sorted(eod_equity.keys()):
        d_eq = eod_equity[d_key]
        if prev_d_eq > 0:
            daily_rets.append((d_eq - prev_d_eq) / prev_d_eq)
        prev_d_eq = d_eq

    daily_sharpe = 0.0
    if len(daily_rets) >= 2:
        mean_d = sum(daily_rets) / float(len(daily_rets))
        var_d = sum((r - mean_d) ** 2 for r in daily_rets) / float(len(daily_rets) - 1)
        std_d = math.sqrt(max(0.0, var_d))
        if std_d > 1e-12:
            daily_sharpe = (mean_d / std_d) * math.sqrt(252.0)

    headline = daily_sharpe if len(daily_rets) >= 2 else bar_sharpe
    return {
        "sharpe_ratio": round(headline, 2),
        "daily_sharpe_ratio": round(daily_sharpe, 2),
        "bar_sharpe_ratio": round(bar_sharpe, 2),
        "sharpe_method": "daily_resampled_sqrt252" if len(daily_rets) >= 2 else "exact_session_bars",
        "sharpe_bars_per_year": round(bars_per_year, 2),
        "trading_days_in_curve": len(eod_equity),
    }


def format_bar_timestamp(bar: Dict[str, Any], timeframe: str) -> str:
    """Format candle timestamp preserving HH:MM for intraday timeframes."""
    tf = (timeframe or "1day").strip().lower()
    ts_raw = str(bar.get("ts_ist") or "").strip()
    td_raw = str(bar.get("trade_date") or "").strip()
    if tf in {"1day", "1week", "1month"}:
        return (td_raw or ts_raw)[:10]
    if "T" in ts_raw:
        return ts_raw[:16].replace("T", " ")
    if len(ts_raw) >= 16:
        return ts_raw[:16]
    return (td_raw or ts_raw)[:10]


def extract_hhmm(bar: Dict[str, Any]) -> Optional[str]:
    ts_raw = str(bar.get("ts_ist") or "").strip()
    if "T" in ts_raw and len(ts_raw) >= 16:
        return ts_raw[11:16]
    if " " in ts_raw and len(ts_raw) >= 16:
        return ts_raw[11:16]
    return None


def within_time_window(bar: Dict[str, Any], start_time: Optional[str], end_time: Optional[str], timeframe: str) -> bool:
    if (timeframe or "1day").strip().lower() in {"1day", "1week", "1month"}:
        return True
    if not start_time and not end_time:
        return True
    hhmm = extract_hhmm(bar)
    if not hhmm:
        return True
    if start_time and hhmm < start_time:
        return False
    if end_time and hhmm > end_time:
        return False
    return True


def collect_strategy_indicators(
    strat_inst: Any,
    tk: IndicatorToolkit,
    n_bars: int,
) -> Dict[str, List[Optional[float]]]:
    """Merge strategy-declared indicator series with IndicatorToolkit recorded series."""
    merged: Dict[str, List[Optional[float]]] = {}
    if hasattr(strat_inst, "get_indicator_series"):
        try:
            custom = strat_inst.get_indicator_series() or {}
            for k, v in custom.items():
                if isinstance(v, list) and len(v) == n_bars:
                    merged[str(k)] = v
        except Exception:
            pass
    for k, v in tk.get_recorded_series().items():
        if k not in merged and isinstance(v, list) and len(v) == n_bars:
            merged[str(k)] = v
    return merged


class BacktestEngine:
    """Single-pass O(N) backtester with next-bar open fills, slippage, fees, SL/TP, and equity curve."""

    def __init__(
        self,
        hist_manager: Optional[HistoricalDataManager] = None,
        registry: Optional[StrategyRegistry] = None,
    ) -> None:
        self._hist = hist_manager or historical_data_manager
        self._registry = registry or strategy_registry

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
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
    ):
        dl_meta = self._hist.download(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            provider=provider,
            instrument_type=instrument_type,
            instrument_key=instrument_key,
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

    def _resolve_strategy(
        self,
        strategy: Any,
        rule_def: Optional[Dict[str, Any]] = None,
    ):
        from backtest.base import BacktestStrategy

        if isinstance(strategy, BacktestStrategy):
            return strategy, "python"
        if rule_def is not None:
            from rules.engine import compile_visual_strategy

            return compile_visual_strategy(rule_def), "visual"
        try:
            return self._registry.create(strategy), "python"
        except Exception:
            from rules.engine import compile_visual_strategy, visual_rule_store

            saved = visual_rule_store.get(strategy)
            if saved is None:
                raise
            return compile_visual_strategy(saved), "visual"

    def run(
        self,
        *,
        symbol: str,
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
        side: Optional[str] = None,
        initial_capital: float = 500000.0,
        slippage_bps: float = 5.0,
        spread_bps: float = 0.0,
        max_volume_participation_pct: float = 0.0,
        fee_bps: float = 5.0,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 12.0,
        trailing_sl_pct: float = 0.0,
        charges_profile: Optional[str] = None,
        segment: str = "equity_intraday",
        charges_overrides: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        backend: str = "auto",
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
        collect_export: bool = False,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        clean_sym = (symbol or "").strip().upper()
        if not clean_sym:
            raise ValueError("symbol is required for backtesting.")
        clean_exch = (exchange or "NSE").strip().upper()
        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)
        is_intraday_tf = tf not in {"1day", "1week", "1month"}

        strat_inst, strat_mode = self._resolve_strategy(strategy, rule_def=rule_def)
        resolved_params = strat_inst.resolve_params(params)

        eff_side = (side or (rule_def.get("side") if isinstance(rule_def, dict) else None) or "BUY").strip().upper()
        is_short = eff_side in {"SELL", "SHORT"}
        eff_holding = (holding_type or ("MIS" if segment == "equity_intraday" else "CNC")).strip().upper()
        mis_eod_squareoff = is_intraday_tf and eff_holding == "MIS"

        cap = max(1000.0, float(initial_capital or 500000.0))
        eff_spread_bps = max(0.0, float(spread_bps or 0.0))
        half_spread_rate = (eff_spread_bps / 2.0) / 10000.0
        vol_cap_pct = max(0.0, min(100.0, float(max_volume_participation_pct or 0.0)))
        cost_rate = max(0.0, (float(slippage_bps or 0.0) + float(fee_bps or 0.0)) / 10000.0)
        sl_pct = max(0.0, float(stop_loss_pct or 0.0))
        tp_pct = max(0.0, float(take_profit_pct or 0.0))
        tsl_pct = max(0.0, float(trailing_sl_pct or 0.0))

        use_real_charges = bool(charges_profile)
        charges_cfg = None
        total_charges_acc = None
        if use_real_charges:
            from charges.calculator import ChargeBreakdown, charges_engine

            ov = dict(charges_overrides or {})
            if "slippage_bps" not in ov and slippage_bps is not None:
                ov["slippage_bps"] = float(slippage_bps)
            charges_cfg = charges_engine.resolve_config(
                profile=str(charges_profile),
                segment=segment or "equity_intraday",
                overrides=ov,
            )
            total_charges_acc = ChargeBreakdown()

        today_ist = datetime.now(tz=IST_TZ).date()
        end_str = (to_date or "").strip() or today_ist.isoformat()
        start_str = (from_date or "").strip() or (today_ist - timedelta(days=365)).isoformat()
        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()

        if progress_cb:
            progress_cb(10, 100, f"Loading OHLCV for {clean_sym} via {eff_provider.upper()}...")

        rows, dl_meta = self._load_candles(
            symbol=clean_sym,
            exchange=clean_exch,
            timeframe=tf,
            from_date=start_str,
            to_date=end_str,
            provider=eff_provider,
            fetcher=fetcher,
            instrument_type=instrument_type,
            instrument_key=instrument_key,
        )

        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelledError("Backtest cancelled by operator.")

        if len(rows) < 15:
            raise ValueError(
                f"Need at least 15 bars to run backtest on {dl_meta['symbol']} (found {len(rows)}). "
                "Expand the date range or verify provider connection."
            )

        if progress_cb:
            progress_cb(45, 100, f"Preparing {strat_inst.name} indicators on {len(rows)} bars...")

        tk = IndicatorToolkit(rows, backend=backend)
        strat_inst.prepare(rows, resolved_params, tk)
        indicator_series = collect_strategy_indicators(strat_inst, tk, len(rows)) if collect_export else {}

        if progress_cb:
            progress_cb(70, 100, f"Simulating {strat_inst.name} (next-bar open fills)...")

        cash = cap
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_bar_idx = -1
        entry_cost = 0.0
        entry_leg_charges = None
        extreme_since_entry = 0.0  # highest for LONG, lowest for SHORT
        pending_entry = False
        pending_exit = False
        trades: List[Dict[str, Any]] = []
        raw_curve: List[Dict[str, Any]] = []

        # Per-bar audit trail for OHLCV + Indicators CSV export
        bar_entry_sigs = [0] * len(rows)
        bar_exit_sigs = [0] * len(rows)
        bar_order_action = [""] * len(rows)
        bar_order_price: List[Optional[float]] = [None] * len(rows)
        bar_exit_reason = [""] * len(rows)

        peak_equity = cap
        max_drawdown_pct = 0.0
        daily_returns: List[float] = []
        prev_equity = cap
        gross_pnl_total = 0.0

        def _mark_equity(close_px: float) -> float:
            if shares <= 0:
                return cash
            if is_short:
                return cash + (shares * entry_price) + ((entry_price - close_px) * shares)
            return cash + (shares * close_px)

        def _open_trade(fill_px_raw: float, bar_ts: str, bar_i: int, bar_vol: int = 0) -> bool:
            nonlocal cash, shares, entry_price, entry_date, entry_bar_idx, entry_cost, entry_leg_charges, extreme_since_entry
            if fill_px_raw <= 0 or cash <= 0:
                return False
            # Apply half-spread adversely (higher buy fill for LONG, lower sell fill for SHORT)
            fill_px = fill_px_raw * (1.0 - half_spread_rate) if is_short else fill_px_raw * (1.0 + half_spread_rate)
            entry_side = "SELL" if is_short else "BUY"
            max_vol_qty = (
                max(1, int(bar_vol * (vol_cap_pct / 100.0)))
                if (vol_cap_pct > 0 and bar_vol > 0)
                else 0
            )
            # Resolve F&O contract lot size & tick size if registered in local instrument master
            _lot_size = 1
            _contract_expiry: Optional[str] = None
            try:
                from core.instruments import instrument_index

                _c = instrument_index.get_contract(
                    dl_meta["symbol"],
                    exchange=clean_exch,
                    instrument_type=instrument_type,
                )
                if _c is not None:
                    _lot_size = _c.effective_lot_size
                    _contract_expiry = _c.expiry
            except Exception:
                pass
            if _lot_size > 1:
                if max_vol_qty > 0:
                    max_vol_qty = (max_vol_qty // _lot_size) * _lot_size

            if use_real_charges and charges_cfg is not None:
                from charges.calculator import charges_engine

                est_rate = (charges_cfg.slippage_bps / 10000.0) + charges_cfg.stt_buy_pct + charges_cfg.stamp_duty_buy_pct + 0.001
                qty = int(max(0.0, cash - charges_cfg.brokerage_flat_per_order * 1.2) // (fill_px * (1.0 + est_rate)))
                if max_vol_qty > 0:
                    qty = min(qty, max_vol_qty)
                if _lot_size > 1:
                    qty = (qty // _lot_size) * _lot_size
                if qty < _lot_size:
                    return False
                leg = charges_engine.calculate_leg(
                    side=entry_side, price=fill_px, quantity=qty, cfg=charges_cfg, include_slippage=True
                )
                total_outlay = (qty * fill_px) + leg.total_charges
                if total_outlay > cash and qty > _lot_size:
                    qty = int((cash - leg.total_charges) // fill_px)
                    if max_vol_qty > 0:
                        qty = min(qty, max_vol_qty)
                    if _lot_size > 1:
                        qty = (qty // _lot_size) * _lot_size
                    if qty < _lot_size:
                        return False
                    leg = charges_engine.calculate_leg(
                        side=entry_side, price=fill_px, quantity=qty, cfg=charges_cfg, include_slippage=True
                    )
                    total_outlay = (qty * fill_px) + leg.total_charges
                if qty < _lot_size or total_outlay > cash:
                    return False
                shares = qty
                entry_price = fill_px
                entry_date = bar_ts
                entry_bar_idx = bar_i
                entry_cost = leg.total_charges
                entry_leg_charges = leg
                extreme_since_entry = fill_px
                cash -= total_outlay
            else:
                buy_eff = fill_px * (1.0 + cost_rate)
                qty = int(cash // buy_eff)
                if max_vol_qty > 0:
                    qty = min(qty, max_vol_qty)
                if _lot_size > 1:
                    qty = (qty // _lot_size) * _lot_size
                if qty < _lot_size:
                    return False
                shares = qty
                entry_price = fill_px
                entry_date = bar_ts
                entry_bar_idx = bar_i
                gross = shares * fill_px
                entry_cost = gross * cost_rate
                extreme_since_entry = fill_px
                cash -= gross + entry_cost
            bar_order_action[bar_i] = entry_side
            bar_order_price[bar_i] = round(fill_px, 4)
            return True

        def _close_trade(exit_px_raw: float, exit_date_str: str, exit_bar_i: int, reason: str) -> None:
            nonlocal cash, shares, entry_price, entry_date, entry_bar_idx, entry_cost, entry_leg_charges, extreme_since_entry, gross_pnl_total
            # Apply half-spread adversely on exit (higher buy-cover for SHORT, lower sell for LONG)
            exit_px_eff = exit_px_raw * (1.0 + half_spread_rate) if is_short else exit_px_raw * (1.0 - half_spread_rate)
            gross_trade_pnl = (
                (entry_price - exit_px_eff) * shares if is_short else (exit_px_eff - entry_price) * shares
            )
            gross_pnl_total += gross_trade_pnl
            exit_side = "BUY" if is_short else "SELL"
            exit_px_raw = exit_px_eff
            if use_real_charges and charges_cfg is not None:
                from charges.calculator import ChargeBreakdown, charges_engine

                exit_leg = charges_engine.calculate_leg(
                    side=exit_side, price=exit_px_raw, quantity=shares, cfg=charges_cfg, include_slippage=True
                )
                trade_chg = ChargeBreakdown()
                if entry_leg_charges is not None:
                    trade_chg.add(entry_leg_charges)
                trade_chg.add(exit_leg)
                if total_charges_acc is not None:
                    total_charges_acc.add(trade_chg)
                net_trade_pnl = gross_trade_pnl - trade_chg.total_charges
                if is_short:
                    proceeds = (shares * entry_price) + gross_trade_pnl - exit_leg.total_charges
                else:
                    proceeds = (shares * exit_px_raw) - exit_leg.total_charges
                cash += proceeds
                ret_pct = (net_trade_pnl / max(1.0, shares * entry_price)) * 100.0
                trades.append(
                    {
                        "symbol": dl_meta["symbol"],
                        "side": "SHORT" if is_short else "LONG",
                        "entry_date": entry_date,
                        "exit_date": exit_date_str,
                        "entry_bar_idx": entry_bar_idx,
                        "exit_bar_idx": exit_bar_i,
                        "qty": shares,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_px_raw, 2),
                        "gross_pnl": round(gross_trade_pnl, 2),
                        "charges": round(trade_chg.total_charges, 2),
                        "pnl": round(net_trade_pnl, 2),
                        "return_pct": round(ret_pct, 2),
                        "exit_reason": reason,
                    }
                )
            else:
                exit_cost = (shares * exit_px_raw) * cost_rate
                pnl = gross_trade_pnl - entry_cost - exit_cost
                ret_pct = (pnl / max(1.0, shares * entry_price)) * 100.0
                cash += (shares * entry_price) + gross_trade_pnl - exit_cost
                trades.append(
                    {
                        "symbol": dl_meta["symbol"],
                        "side": "SHORT" if is_short else "LONG",
                        "entry_date": entry_date,
                        "exit_date": exit_date_str,
                        "entry_bar_idx": entry_bar_idx,
                        "exit_bar_idx": exit_bar_i,
                        "qty": shares,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_px_raw, 2),
                        "gross_pnl": round(gross_trade_pnl, 2),
                        "charges": round(entry_cost + exit_cost, 2),
                        "pnl": round(pnl, 2),
                        "return_pct": round(ret_pct, 2),
                        "exit_reason": reason,
                    }
                )
            bar_order_action[exit_bar_i] = exit_side
            bar_order_price[exit_bar_i] = round(exit_px_raw, 4)
            bar_exit_reason[exit_bar_i] = reason
            shares = 0
            entry_price = 0.0
            entry_date = ""
            entry_bar_idx = -1
            entry_cost = 0.0
            entry_leg_charges = None
            extreme_since_entry = 0.0

        n_rows = len(rows)
        for i, bar in enumerate(rows):
            if cancel_event is not None and (i % 50 == 0) and cancel_event.is_set():
                raise JobCancelledError("Backtest cancelled by operator.")
            bar_open = float(bar["open"])
            bar_close = float(bar["close"])
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])
            bar_ts = format_bar_timestamp(bar, tf)
            cur_day = str(bar.get("trade_date") or bar_ts)[:10]
            next_day = str(rows[i + 1].get("trade_date") or format_bar_timestamp(rows[i + 1], tf))[:10] if i + 1 < n_rows else ""
            is_session_last_bar = mis_eod_squareoff and (i == n_rows - 1 or next_day != cur_day)
            in_window = within_time_window(bar, start_time, end_time, tf)

            # 1) Execute pending STRATEGY_EXIT from previous bar at THIS bar's open (strictly i > entry_bar_idx)
            if shares > 0 and pending_exit and i > entry_bar_idx:
                _close_trade(bar_open, bar_ts, i, "STRATEGY_EXIT")
                pending_exit = False
            else:
                pending_exit = False

            # 2) Execute pending ENTRY from previous bar at THIS bar's open (never on last bar or EOD MIS bar)
            entered_this_bar = False
            if shares == 0 and pending_entry and i < n_rows - 1 and in_window and not is_session_last_bar:
                bar_vol = int(float(bar.get("volume") or 0))
                entered_this_bar = _open_trade(bar_open, bar_ts, i, bar_vol=bar_vol)
            pending_entry = False

            # 3) Intrabar SL / TSL / TP / MIS EOD square-off (ONLY on bars strictly AFTER entry bar!)
            # Includes realistic gap-through-stop fills on BOTH long and short positions.
            if shares > 0 and not entered_this_bar and i > entry_bar_idx:
                exit_reason = ""
                exit_px = bar_close
                if not is_short:
                    if bar_high > extreme_since_entry:
                        extreme_since_entry = bar_high
                    stop_lvl = entry_price * (1.0 - sl_pct / 100.0)
                    tsl_lvl = extreme_since_entry * (1.0 - tsl_pct / 100.0)
                    tp_lvl = entry_price * (1.0 + tp_pct / 100.0)
                    if sl_pct > 0 and bar_low <= stop_lvl:
                        exit_px = min(bar_open, stop_lvl) if bar_open < stop_lvl else stop_lvl
                        exit_reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                    elif tsl_pct > 0 and extreme_since_entry > entry_price and bar_low <= tsl_lvl:
                        exit_px = min(bar_open, tsl_lvl) if bar_open < tsl_lvl else tsl_lvl
                        exit_reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                    elif tp_pct > 0 and bar_high >= tp_lvl:
                        exit_px = max(bar_open, tp_lvl) if bar_open > tp_lvl else tp_lvl
                        exit_reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                    elif is_session_last_bar:
                        exit_px = bar_close
                        exit_reason = "MIS_EOD_SQUAREOFF"
                else:
                    if bar_low < extreme_since_entry:
                        extreme_since_entry = bar_low
                    stop_lvl = entry_price * (1.0 + sl_pct / 100.0)
                    tsl_lvl = extreme_since_entry * (1.0 + tsl_pct / 100.0)
                    tp_lvl = entry_price * (1.0 - tp_pct / 100.0)
                    if sl_pct > 0 and bar_high >= stop_lvl:
                        exit_px = max(bar_open, stop_lvl) if bar_open > stop_lvl else stop_lvl
                        exit_reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                    elif tsl_pct > 0 and extreme_since_entry < entry_price and bar_high >= tsl_lvl:
                        exit_px = max(bar_open, tsl_lvl) if bar_open > tsl_lvl else tsl_lvl
                        exit_reason = f"TRAILING_SL (-{tsl_pct:.1f}%)"
                    elif tp_pct > 0 and bar_low <= tp_lvl:
                        exit_px = min(bar_open, tp_lvl) if bar_open < tp_lvl else tp_lvl
                        exit_reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"
                    elif is_session_last_bar:
                        exit_px = bar_close
                        exit_reason = "MIS_EOD_SQUAREOFF"

                if exit_reason:
                    _close_trade(exit_px, bar_ts, i, exit_reason)

            # 4) Evaluate strategy signals at bar `i` close -> schedule for NEXT bar `i + 1` open!
            enter_sig, exit_sig = strat_inst.signal_at(i, bar)
            bar_entry_sigs[i] = 1 if enter_sig else 0
            bar_exit_sigs[i] = 1 if exit_sig else 0

            if i < n_rows - 1:
                if shares > 0 and exit_sig:
                    pending_exit = True
                elif shares == 0 and enter_sig and in_window and not is_session_last_bar:
                    pending_entry = True

            equity = _mark_equity(bar_close)
            if equity > peak_equity:
                peak_equity = equity
            dd_pct = ((peak_equity - equity) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct
            if prev_equity > 0:
                daily_returns.append((equity - prev_equity) / prev_equity)
            prev_equity = equity
            raw_curve.append({"date": bar_ts, "equity": round(equity, 2), "drawdown_pct": round(dd_pct, 2)})

        if shares > 0:
            last_idx = n_rows - 1
            last_close = float(rows[last_idx]["close"])
            last_ts = format_bar_timestamp(rows[last_idx], tf)
            _close_trade(last_close, last_ts, last_idx, "END_OF_WINDOW")

        final_equity = cash
        net_pnl = final_equity - cap
        total_return_pct = (net_pnl / cap) * 100.0

        start_d = date.fromisoformat(str(rows[0]["trade_date"])[:10])
        end_d = date.fromisoformat(str(rows[-1]["trade_date"])[:10])
        span_days = max(1, (end_d - start_d).days)
        years = max(span_days / 365.25, 1.0 / 252.0)
        if span_days >= 90:
            cagr_pct: Optional[float] = round(((max(final_equity, 1.0) / cap) ** (1.0 / years) - 1.0) * 100.0, 2)
            cagr_methodology = "compound_annualized_365_25"
            sample_warning: Optional[str] = (
                f"Low trade sample ({len(trades)} trades < 10); metrics may lack statistical significance."
                if len(trades) < 10
                else None
            )
        else:
            cagr_pct = None
            cagr_methodology = "suppressed_short_window_lt_90d"
            sample_warning = (
                f"Backtest duration is {span_days} days (< 90 days); CAGR is suppressed to avoid misleading annualization."
            )

        sharpe_metrics = compute_sharpe_metrics(raw_curve, cap, tf, exchange=clean_exch)
        sharpe = float(sharpe_metrics["sharpe_ratio"])

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        win_rate = (len(wins) / float(len(trades))) * 100.0 if trades else 0.0
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

        curve = raw_curve
        if len(raw_curve) > 160:
            step = max(1, len(raw_curve) // 160)
            sampled = [raw_curve[j] for j in range(0, len(raw_curve), step)]
            if sampled[-1]["date"] != raw_curve[-1]["date"]:
                sampled.append(raw_curve[-1])
            curve = sampled

        if progress_cb:
            progress_cb(100, 100, f"Backtest finished ({len(trades)} trades, Net PnL ₹{net_pnl:,.2f}).")

        storage_meta = self._hist.storage_info()
        charges_breakdown = (
            total_charges_acc.to_dict()
            if total_charges_acc is not None
            else {
                "turnover": round(sum((t["entry_price"] + t["exit_price"]) * t["qty"] for t in trades), 2),
                "brokerage": 0.0,
                "stt": 0.0,
                "exchange_txn": 0.0,
                "sebi_fee": 0.0,
                "gst": 0.0,
                "stamp_duty": 0.0,
                "dp_charges": 0.0,
                "slippage_cost": round(gross_pnl_total - net_pnl, 2),
                "statutory_and_brokerage": 0.0,
                "total_charges": round(gross_pnl_total - net_pnl, 2),
            }
        )

        export_payload = None
        if collect_export:
            ind_names = list(indicator_series.keys())
            export_rows: List[Dict[str, Any]] = []
            for i, bar in enumerate(rows):
                rec: Dict[str, Any] = {
                    "symbol": dl_meta["symbol"],
                    "exchange": dl_meta["exchange"],
                    "timestamp": format_bar_timestamp(bar, tf),
                    "open": round(float(bar["open"]), 4),
                    "high": round(float(bar["high"]), 4),
                    "low": round(float(bar["low"]), 4),
                    "close": round(float(bar["close"]), 4),
                    "volume": int(float(bar.get("volume") or 0)),
                }
                for ind_k in ind_names:
                    iv = indicator_series[ind_k][i]
                    rec[ind_k] = round(float(iv), 4) if iv is not None else ""
                rec["entry_signal"] = bar_entry_sigs[i]
                rec["exit_signal"] = bar_exit_sigs[i]
                rec["order_action"] = bar_order_action[i]
                rec["order_price"] = bar_order_price[i] if bar_order_price[i] is not None else ""
                rec["exit_reason"] = bar_exit_reason[i]
                export_rows.append(rec)
            export_payload = {
                "symbol": dl_meta["symbol"],
                "indicator_columns": ind_names,
                "rows": export_rows,
            }

        out: Dict[str, Any] = {
            "ok": True,
            "symbol": dl_meta["symbol"],
            "exchange": dl_meta["exchange"],
            "strategy": strat_inst.id,
            "strategy_label": strat_inst.name,
            "strategy_mode": strat_mode,
            "side": "SHORT" if is_short else "LONG",
            "holding_type": eff_holding,
            "params": resolved_params,
            "timeframe": tf,
            "base_timeframe": base_tf,
            "resampled": tf != base_tf,
            "provider": eff_provider,
            "storage_backend": storage_meta["active_mode"],
            "ta_backends": available_backends(),
            "from_date": str(rows[0]["trade_date"])[:10],
            "to_date": str(rows[-1]["trade_date"])[:10],
            "bars_evaluated": len(rows),
            "api_calls_made": int(dl_meta.get("api_calls_made", 0)),
            "existing_days_skipped": int(dl_meta.get("existing_days_skipped", 0)),
            "initial_capital": round(cap, 2),
            "final_equity": round(final_equity, 2),
            "gross_pnl": round(gross_pnl_total, 2),
            "total_charges": round(float(charges_breakdown["total_charges"]), 2),
            "charges_profile": charges_profile or "flat_bps",
            "segment": segment,
            "charges_breakdown": charges_breakdown,
            "net_pnl": round(net_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "cagr_pct": cagr_pct,
            "annualized_return_pct": cagr_pct,
            "duration_days": span_days,
            "cagr_methodology": cagr_methodology,
            "sample_warning": sample_warning,
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "daily_sharpe_ratio": sharpe_metrics["daily_sharpe_ratio"],
            "bar_sharpe_ratio": sharpe_metrics["bar_sharpe_ratio"],
            "sharpe_method": sharpe_metrics["sharpe_method"],
            "execution_assumptions": {
                "fill_model": "next_bar_open",
                "no_same_bar_exit": True,
                "gap_through_stops": True,
                "slippage_bps": float(slippage_bps or 0.0),
                "spread_bps": eff_spread_bps,
                "max_volume_participation_pct": vol_cap_pct,
                "sharpe_method": sharpe_metrics["sharpe_method"],
                "sharpe_bars_per_year": sharpe_metrics["sharpe_bars_per_year"],
            },
            "total_trades": len(trades),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "equity_curve": curve,
            "trades": trades,
            "deploy_spec": {
                "strategy_id": strat_inst.id,
                "strategy_name": strat_inst.name,
                "strategy_mode": strat_mode,
                "rule_def": rule_def,
                "params": resolved_params,
                "symbols": [dl_meta["symbol"]],
                "exchange": dl_meta["exchange"],
                "timeframe": tf,
                "initial_capital": round(cap, 2),
                "stop_loss_pct": sl_pct,
                "take_profit_pct": tp_pct,
                "trailing_sl_pct": tsl_pct,
                "charges_profile": charges_profile or "zerodha",
                "segment": segment,
                "charges_overrides": charges_overrides or {},
            },
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }
        if export_payload is not None:
            out["_export"] = export_payload
        return out


backtest_engine = BacktestEngine()

__all__ = [
    "BacktestEngine",
    "backtest_engine",
    "collect_strategy_indicators",
    "compute_sharpe_metrics",
    "extract_hhmm",
    "format_bar_timestamp",
    "within_time_window",
]
