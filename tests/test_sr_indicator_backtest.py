"""
tests/test_sr_indicator_backtest.py
===================================
Comprehensive tests for the Support & Resistance indicator across screening,
causal bar-by-bar backtesting, chart zone overlays, EQ/FUT segment filtering,
7-component strength weighting, visual rules + Python export parity, and
statistical event validation with 4 baseline comparisons.
"""

from __future__ import annotations

import math
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

from backtest.engine import BacktestEngine
from backtest.portfolio import PortfolioBacktestEngine
from backtest.registry import strategy_registry
from core.historical import HistoricalDataManager
from indicators.toolkit import IndicatorToolkit
from rules.engine import (
    compile_visual_screen,
    compile_visual_strategy,
    export_rule_to_python,
)
from screener.engine import (
    ScreenerEngine,
    is_futures_contract,
    is_option_contract,
    normalize_segments,
)
from screener.sr_engine import (
    compute_causal_sr_series,
    compute_sr_rank_tier,
    evaluate_symbol_mtf,
    normalize_strength_weights,
)
from screener.sr_validation import evaluate_sr_validation


def _make_structured_candles(n: int = 240, base_price: float = 1000.0, start_d: date = date(2024, 1, 2)) -> List[Dict[str, Any]]:
    """
    Generate deterministic OHLCV bars with recurring range-bound oscillations,
    clear swing supports (~960) & resistances (~1050), followed by a clean
    breakout, pullback retest, and trend continuation so every S&R event type
    is exercised deterministically.
    """
    rows: List[Dict[str, Any]] = []
    cur_d = start_d
    for i in range(n):
        while cur_d.weekday() >= 5:
            cur_d += timedelta(days=1)

        if i < 140:
            # Oscillate between ~960 and ~1045 (multiple clean touches & bounces)
            wave = math.sin(i * 0.28) * 42.0 + math.sin(i * 0.09) * 8.0
            mid = base_price + wave
        elif i < 165:
            # Breakout above ~1045 resistance up to ~1120
            step = i - 140
            mid = 1045.0 + step * 3.2 + math.sin(step * 0.4) * 5.0
        elif i < 185:
            # Pullback retest toward ~1050 flipped support, then bounce
            step = i - 165
            if step < 10:
                mid = 1120.0 - step * 6.8
            else:
                mid = 1052.0 + (step - 10) * 6.5
        else:
            # Second consolidation + gentle oscillations around ~1115
            step = i - 185
            mid = 1115.0 + math.sin(step * 0.30) * 35.0

        o = mid - 3.0 if (i % 2 == 0) else mid + 2.5
        c = mid + 3.5 if (i % 2 == 0) else mid - 2.0
        h = max(o, c) + 6.5 + (i % 4)
        l = min(o, c) - 6.0 - (i % 3)
        vol = 120_000 + (i % 7) * 25_000 + (180_000 if 138 <= i <= 146 else 0)
        ts = f"{cur_d.isoformat()}T09:15:00+05:30"
        rows.append(
            {
                "trade_date": cur_d.isoformat(),
                "ts_ist": ts,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": int(vol),
            }
        )
        cur_d += timedelta(days=1)
    return rows


class TestSupportResistanceIndicatorBacktest(unittest.TestCase):
    def test_causal_zero_lookahead_exact_prefix_equality(self) -> None:
        """
        Strict no-lookahead proof: values computed on the full history at bar `t`
        must be bit-for-bit identical to values computed on the truncated prefix
        `candles[:t+1]` at bar `t`, BOTH with and without HTF confluence.
        """
        candles = _make_structured_candles(n=120)
        for htf_flag in (False, True):
            full = compute_causal_sr_series(
                candles,
                timeframe="1day",
                params={"left_bars": 4, "right_bars": 4},
                htf_confluence=htf_flag,
            )
            check_indices = [25, 45, 70, 95, 119]
            keys = [
                "sr_nearest_support",
                "sr_nearest_resistance",
                "sr_support_strength",
                "sr_resistance_strength",
                "sr_confluence_count",
                "sr_event_code",
                "sr_resistance_breakout",
                "sr_support_bounce",
            ]
            for t in check_indices:
                prefix = compute_causal_sr_series(
                    candles[: t + 1],
                    timeframe="1day",
                    params={"left_bars": 4, "right_bars": 4},
                    htf_confluence=htf_flag,
                )
                for k in keys:
                    self.assertEqual(
                        full[k][t],
                        prefix[k][t],
                        f"Lookahead mismatch for {k} at bar {t} (htf_confluence={htf_flag})",
                    )

    def test_causal_sr_performance_600_bars(self) -> None:
        """Causal bar-by-bar engine should compute 600 bars quickly for backtesting."""
        candles = _make_structured_candles(n=600)
        t0 = time.perf_counter()
        out = compute_causal_sr_series(
            candles,
            timeframe="1day",
            params={"left_bars": 5, "right_bars": 5},
            htf_confluence=True,
        )
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.5, f"Causal S&R took too long on 600 bars: {elapsed:.2f}s")
        self.assertEqual(len(out["sr_event_code"]), 600)
        self.assertGreater(len(out["zone_spans"]), 0)

    def test_seven_component_weight_normalization_and_six_tier_ranking(self) -> None:
        """Weights always normalize to sum 100 and 6-tier priority ranking orders correctly."""
        norm_w = normalize_strength_weights(
            {
                "weight_touches": 40,
                "weight_reactions": 40,
                "weight_volume": 20,
                "weight_recency": 0,
                "weight_age": 0,
                "weight_confluence": 0,
                "weight_reversal": 0,
            }
        )
        self.assertAlmostEqual(sum(norm_w.values()), 100.0, places=3)
        self.assertAlmostEqual(norm_w["touches"], 40.0, places=3)

        # Unnormalized arbitrary weights still sum to 100
        norm_w2 = normalize_strength_weights({"weight_touches": 50, "weight_reactions": 50})
        self.assertAlmostEqual(sum(norm_w2.values()), 100.0, places=3)

        # 6-tier priority ordering: Tier 1 < Tier 2 < Tier 3 < Tier 4 < Tier 5 < Tier 6
        t1 = compute_sr_rank_tier("Major", "RESISTANCE_BREAKOUT")
        t2 = compute_sr_rank_tier("Major", "SUPPORT_BOUNCE")
        t3 = compute_sr_rank_tier("Major", "NEAR_SUPPORT")
        t4 = compute_sr_rank_tier("Minor", "BREAKOUT_RETEST")
        t5 = compute_sr_rank_tier("Minor", "RESISTANCE_REJECTION")
        t6 = compute_sr_rank_tier("Weak", "NEAR_SUPPORT")
        self.assertEqual([t1, t2, t3, t4, t5, t6], [1, 2, 3, 4, 5, 6])

        # Shifting weights measurably alters zone strength
        candles = _make_structured_candles(n=180)
        eval_default = evaluate_symbol_mtf(
            symbol="RELIANCE",
            exchange="NSE",
            candles_by_tf={"1day": candles},
            params={"min_strength": 10},
        )
        eval_custom = evaluate_symbol_mtf(
            symbol="RELIANCE",
            exchange="NSE",
            candles_by_tf={"1day": candles},
            params={
                "min_strength": 10,
                "weight_touches": 70,
                "weight_reactions": 5,
                "weight_volume": 5,
                "weight_recency": 5,
                "weight_age": 5,
                "weight_confluence": 5,
                "weight_reversal": 5,
            },
        )
        def_zones = eval_default["zones_by_tf"]["1day"]
        cus_zones = eval_custom["zones_by_tf"]["1day"]
        self.assertGreater(len(def_zones), 0)
        self.assertGreater(len(cus_zones), 0)
        s_def = [round(z.strength, 2) for z in def_zones]
        s_cus = [round(z.strength, 2) for z in cus_zones]
        self.assertNotEqual(s_def, s_cus)

    def test_eq_fut_segment_filtering_and_options_exclusion(self) -> None:
        """Options contracts are strictly excluded while cash equities & futures are properly classified."""
        self.assertTrue(is_option_contract("NIFTY26NOV24000CE"))
        self.assertTrue(is_option_contract("RELIANCE26NOV2900PE"))
        self.assertTrue(is_option_contract("BANKNIFTY", instrument_type="OPTIDX"))
        # Cash equities ending in CE/PE/letters must NOT be falsely flagged as options
        self.assertFalse(is_option_contract("FORCE"))
        self.assertFalse(is_option_contract("SPICEJET"))
        self.assertFalse(is_option_contract("RELIANCE"))

        self.assertTrue(is_futures_contract("RELIANCE26NOVFUT"))
        self.assertTrue(is_futures_contract("NIFTY", instrument_type="FUTIDX"))
        self.assertFalse(is_futures_contract("RELIANCE"))

        segs, opts_req = normalize_segments(["EQ", "FUT", "OPT"])
        self.assertEqual(segs, ["EQ", "FUT"])
        self.assertTrue(opts_req)

        # Verify ScreenerEngine excludes option symbols & returns explicit options_excluded_notice
        with tempfile.TemporaryDirectory() as tmpdir:
            hist = HistoricalDataManager(db_path=Path(tmpdir) / "hist.db")
            candles = _make_structured_candles(n=180)
            engine = ScreenerEngine(hist_manager=hist)
            out = engine.scan(
                symbols=["RELIANCE", "NIFTY26NOV24000CE", "BANKNIFTY26NOV52000PE"],
                exchange="NSE",
                segments=["EQ"],
                screen_id="support_resistance",
                timeframes=["1day"],
                params={"min_strength": 10},
                fetcher=lambda **kwargs: candles,
            )
            self.assertEqual(out["options_excluded_count"], 2)
            self.assertIsNotNone(out["options_excluded_notice"])
            self.assertEqual(out["scanned_count"], 1)
            for row in out["results"]:
                self.assertEqual(row["instrument_category"], "EQ")
                self.assertIn("rank_tier", row)

    def test_visual_sr_rules_and_python_export_parity(self) -> None:
        """Visual S&R strategy & screen produce identical results before and after Python export."""
        candles = _make_structured_candles(n=200)
        rule_def = {
            "id": "test_sr_visual",
            "kind": "strategy",
            "name": "Test S&R Visual Breakout",
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {
                            "type": "sr_resistance_breakout",
                            "left_bars": 4,
                            "right_bars": 4,
                            "cluster_atr_multiplier": 0.75,
                            "min_strength": 35,
                        },
                        "operator": "==",
                        "right": {"type": "number", "value": 1},
                    },
                    {
                        "left": {"type": "sr_support_strength", "left_bars": 4, "right_bars": 4},
                        "operator": ">=",
                        "right": {"type": "number", "value": 0},
                    },
                ],
            },
            "exit": {
                "logic": "OR",
                "conditions": [
                    {
                        "left": {"type": "sr_support_breakdown", "left_bars": 4, "right_bars": 4},
                        "operator": "==",
                        "right": {"type": "number", "value": 1},
                    }
                ],
            },
        }
        strat = compile_visual_strategy(rule_def)
        tk = IndicatorToolkit(candles, backend="builtin")
        strat.prepare(candles, {}, tk)
        orig_signals = [strat.signal_at(i, candles[i]) for i in range(len(candles))]
        self.assertGreater(len(tk.get_sr_zone_spans()), 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            exported = export_rule_to_python(rule_def, kind="strategy", target_dir=Path(tmpdir))
            self.assertIn("class ", exported["code"])
            ns: Dict[str, Any] = {}
            exec(exported["code"], ns)  # noqa: S102
            cls_obj = next(
                v
                for v in ns.values()
                if isinstance(v, type)
                and hasattr(v, "signal_at")
                and v.__name__ not in ("BacktestStrategy", "CompiledVisualStrategy")
            )
            exp_strat = cls_obj()
            tk2 = IndicatorToolkit(candles, backend="builtin")
            exp_strat.prepare(candles, {}, tk2)
            exp_signals = [exp_strat.signal_at(i, candles[i]) for i in range(len(candles))]
            self.assertEqual(orig_signals, exp_signals)

        # Also verify Visual Screen with S&R operand
        scr_rule = {**rule_def, "kind": "screen"}
        scr = compile_visual_screen(scr_rule)
        tk3 = IndicatorToolkit(candles, backend="builtin")
        match = scr.evaluate(candles, {}, tk3)
        self.assertIsInstance(match.matched, bool)

    def test_builtin_sr_strategy_backtest_and_chart_zone_overlays(self) -> None:
        """Built-in support_resistance_structure strategy runs cleanly and persists chart sr_zones."""
        strategy_registry.refresh_if_changed(force=True)
        strat = strategy_registry.create("support_resistance_structure")
        self.assertEqual(strat.id, "support_resistance_structure")

        candles = _make_structured_candles(n=220)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            hist = HistoricalDataManager(db_path=tmp_path / "hist.db")
            single = BacktestEngine(hist_manager=hist)
            p_eng = PortfolioBacktestEngine(
                hist_manager=hist,
                single_engine=single,
                runs_file=tmp_path / "runs.json",
            )
            res = p_eng.run(
                mode="portfolio",
                symbols=["RELIANCE"],
                exchange="NSE",
                strategy="support_resistance_structure",
                params={"left_bars": 4, "right_bars": 4, "min_strength": 30, "htf_confluence": True},
                timeframe="1day",
                from_date=candles[0]["trade_date"],
                to_date=candles[-1]["trade_date"],
                initial_capital=200_000.0,
                fetcher=lambda *args, **kwargs: candles,
            )
            self.assertTrue(res["ok"])
            self.assertIn("run_id", res)

            chart = p_eng.get_chart_series(res["run_id"], symbol="RELIANCE")
            self.assertTrue(chart["ok"])
            self.assertGreater(len(chart["sr_zones"]), 0)
            first_z = chart["sr_zones"][0]
            for fld in ("zone_id", "level_type", "zone_low", "zone_high", "start_bar_index", "end_bar_index", "classification"):
                self.assertIn(fld, first_z)

            # Verify indicator series classification puts SR price lines in overlay and strength in oscillator
            ind_panes = {ind["id"]: ind["pane"] for ind in chart["indicators"]}
            has_sr_overlay = any("SR_Support(" in k or "SR_Resistance(" in k for k, p in ind_panes.items() if p == "overlay")
            has_sr_osc = any("Strength" in k for k, p in ind_panes.items() if p == "oscillator")
            self.assertTrue(has_sr_overlay, f"Expected SR price overlay series, got {ind_panes}")
            self.assertTrue(has_sr_osc, f"Expected SR strength oscillator series, got {ind_panes}")

    def test_statistical_validation_report_and_four_baselines(self) -> None:
        """Statistical validation returns all event/tier/regime/horizon splits and 4 baseline comparisons."""
        c1 = _make_structured_candles(n=220, base_price=1000.0)
        c2 = _make_structured_candles(n=220, base_price=2400.0)
        report = evaluate_sr_validation(
            candles_by_symbol={"RELIANCE": c1, "TCS": c2},
            timeframe="1day",
            params={"left_bars": 4, "right_bars": 4, "min_strength": 25},
            horizons=[5, 10, 20],
            primary_horizon=10,
            htf_confluence=True,
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["symbol_count"], 2)
        self.assertGreater(report["summary"]["total_events"], 0)
        for key in (
            "bounce_success_rate_pct",
            "rejection_success_rate_pct",
            "breakout_success_rate_pct",
            "retest_success_rate_pct",
            "false_breakout_rate_pct",
            "avg_mfe_atr",
            "avg_mae_atr",
            "edge_ratio",
            "avg_reaction_bars",
        ):
            self.assertIn(key, report["summary"])

        horizon_bars = [h["horizon_bars"] for h in report["by_horizon"]]
        self.assertEqual(horizon_bars, [5, 10, 20])

        baseline_ids = [b["id"] for b in report["baselines"]]
        self.assertEqual(
            baseline_ids,
            [
                "full_sr_engine",
                "unscored_atr_clusters",
                "unclustered_pivots",
                "rolling_high_low",
                "random_levels",
            ],
        )


if __name__ == "__main__":
    unittest.main()
