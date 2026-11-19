"""
backtest/engine.py
==================
Single-pass O(N) backtester loading hot-reloaded `.py` strategies from
`StrategyRegistry`, powered by Hybrid Parquet/SQLite OHLCV storage and IndicatorToolkit.
"""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from backtest.registry import StrategyRegistry, strategy_registry
from config import IST_TZ, config
from core.historical import (
    HistoricalDataManager,
    historical_data_manager,
    normalize_timeframe,
    resolve_base_timeframe,
)
from core.jobs import JobCancelledError
from indicators.toolkit import IndicatorToolkit, available_backends


class BacktestEngine:
    """Single-pass O(N) backtester with slippage, fees, SL/TP, and equity curve."""

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

    def run(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        strategy: str = "ema_crossover",
        params: Optional[Dict[str, Any]] = None,
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        initial_capital: float = 500000.0,
        slippage_bps: float = 5.0,
        fee_bps: float = 5.0,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 12.0,
        provider: Optional[str] = None,
        backend: str = "auto",
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
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

        strat_inst = self._registry.create(strategy)
        resolved_params = strat_inst.resolve_params(params)

        cap = max(1000.0, float(initial_capital or 500000.0))
        cost_rate = max(0.0, (float(slippage_bps or 0.0) + float(fee_bps or 0.0)) / 10000.0)
        sl_pct = max(0.0, float(stop_loss_pct or 0.0))
        tp_pct = max(0.0, float(take_profit_pct or 0.0))

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

        if progress_cb:
            progress_cb(70, 100, f"Simulating {strat_inst.name}...")

        cash = cap
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_cost = 0.0
        trades: List[Dict[str, Any]] = []
        raw_curve: List[Dict[str, Any]] = []

        peak_equity = cap
        max_drawdown_pct = 0.0
        daily_returns: List[float] = []
        prev_equity = cap

        for i, bar in enumerate(rows):
            if cancel_event is not None and (i % 50 == 0) and cancel_event.is_set():
                raise JobCancelledError("Backtest cancelled by operator.")
            bar_close = float(bar["close"])
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])
            bar_date = str(bar.get("trade_date") or str(bar.get("ts_ist", ""))[:10])

            exit_reason = ""
            exit_px = bar_close
            if shares > 0:
                if sl_pct > 0 and bar_low <= entry_price * (1.0 - sl_pct / 100.0):
                    exit_px = entry_price * (1.0 - sl_pct / 100.0)
                    exit_reason = f"STOP_LOSS (-{sl_pct:.1f}%)"
                elif tp_pct > 0 and bar_high >= entry_price * (1.0 + tp_pct / 100.0):
                    exit_px = entry_price * (1.0 + tp_pct / 100.0)
                    exit_reason = f"TAKE_PROFIT (+{tp_pct:.1f}%)"

            enter_sig, exit_sig = strat_inst.signal_at(i, bar)

            if shares > 0 and (exit_reason or exit_sig):
                sell_px = exit_px * (1.0 - cost_rate)
                proceeds = shares * sell_px
                pnl = proceeds - (shares * entry_price) - entry_cost
                ret_pct = ((sell_px - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
                cash += proceeds
                trades.append(
                    {
                        "entry_date": entry_date,
                        "exit_date": bar_date,
                        "qty": shares,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_px, 2),
                        "pnl": round(pnl, 2),
                        "return_pct": round(ret_pct, 2),
                        "exit_reason": exit_reason or "STRATEGY_EXIT",
                    }
                )
                shares = 0
                entry_price = 0.0
                entry_date = ""
                entry_cost = 0.0
            elif shares == 0 and enter_sig and i < len(rows) - 1:
                buy_eff = bar_close * (1.0 + cost_rate)
                qty = int(cash // buy_eff)
                if qty >= 1:
                    shares = qty
                    entry_price = bar_close
                    entry_date = bar_date
                    gross = shares * bar_close
                    entry_cost = gross * cost_rate
                    cash -= gross + entry_cost

            equity = cash + shares * bar_close
            if equity > peak_equity:
                peak_equity = equity
            dd_pct = ((peak_equity - equity) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct
            if prev_equity > 0:
                daily_returns.append((equity - prev_equity) / prev_equity)
            prev_equity = equity
            raw_curve.append({"date": bar_date, "equity": round(equity, 2), "drawdown_pct": round(dd_pct, 2)})

        if shares > 0:
            last_close = float(rows[-1]["close"])
            last_date = str(rows[-1].get("trade_date") or str(rows[-1].get("ts_ist", ""))[:10])
            sell_px = last_close * (1.0 - cost_rate)
            proceeds = shares * sell_px
            pnl = proceeds - (shares * entry_price) - entry_cost
            ret_pct = ((sell_px - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
            cash += proceeds
            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": last_date,
                    "qty": shares,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(last_close, 2),
                    "pnl": round(pnl, 2),
                    "return_pct": round(ret_pct, 2),
                    "exit_reason": "END_OF_WINDOW",
                }
            )
            shares = 0

        final_equity = cash
        net_pnl = final_equity - cap
        total_return_pct = (net_pnl / cap) * 100.0

        start_d = date.fromisoformat(str(rows[0]["trade_date"])[:10])
        end_d = date.fromisoformat(str(rows[-1]["trade_date"])[:10])
        span_days = max(1, (end_d - start_d).days)
        years = max(span_days / 365.25, 1.0 / 252.0)
        cagr_pct = ((max(final_equity, 1.0) / cap) ** (1.0 / years) - 1.0) * 100.0 if years >= 0.08 else total_return_pct

        sharpe = 0.0
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / float(len(daily_returns))
            var_r = sum((r - mean_r) ** 2 for r in daily_returns) / float(len(daily_returns) - 1)
            std_r = math.sqrt(max(0.0, var_r))
            if std_r > 0:
                ann_factor = 252.0 if tf == "1day" else (252.0 * 12.0)
                sharpe = (mean_r / std_r) * math.sqrt(ann_factor)

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
        return {
            "ok": True,
            "symbol": dl_meta["symbol"],
            "exchange": dl_meta["exchange"],
            "strategy": strat_inst.id,
            "strategy_label": strat_inst.name,
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
            "net_pnl": round(net_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "cagr_pct": round(cagr_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "total_trades": len(trades),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "equity_curve": curve,
            "trades": trades,
            "generated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }


backtest_engine = BacktestEngine()

__all__ = ["BacktestEngine", "backtest_engine"]
