"""
tests/test_streak_platform.py
=============================
Deterministic, zero-network verification suite for Phase 6 Streak-style platform
modules:
1. 2026 Indian Brokerage & Statutory Charges Engine (Zerodha, Upstox, Zero, Custom)
2. Visual No-Code Rule AST Compiler, CRUD Store & Export-to-Python
3. Dual-Mode (Individual & Portfolio) Backtester across all 3 allocation modes
   (`shared_pool`, `equal_split`, `risk_based`) for Visual & Python strategies
4. Zero-dependency Black-Scholes Greeks & Multi-Leg Options Payoff Engine
5. Multi-Timeframe Technicals Analyzer (Gauges, MAs, Oscillators, Pivots, S&R)
6. Unified Deployment Engine (Paper Trade with MTM & 2026 charges, Live 1-Click
   Actionable Alerts, Kill-Switch gating for Live Auto-Execute & Alerts,
   Pause/Stop/Delete, and Reboot Safety)
7. Custom Baskets CRUD & Discover Catalog integrity
"""

from __future__ import annotations

import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

from backtest.engine import BacktestEngine
from backtest.portfolio import PortfolioBacktestEngine
from baskets.store import BasketStore
from charges.calculator import ChargesEngine, list_charge_presets
from core.audit import AuditTrail
from core.historical import HistoricalDataManager
from core.preflight import KillSwitchController
from deploy.manager import DeploymentManager
from discover.catalog import list_discover_catalog
from indicators.toolkit import IndicatorToolkit
from options.payoff import analyze_options_strategy, build_template_legs
from risk.limits import RiskLimitManager
from rules.engine import (
    VisualRuleStore,
    compile_visual_screen,
    compile_visual_strategy,
    evaluate_condition_group,
    export_rule_to_python,
)
from technicals.analyzer import TechnicalsAnalyzer


def _make_synthetic_candles(
    n: int = 240,
    start_price: float = 1000.0,
    wave_amp: float = 85.0,
    drift: float = 0.6,
    phase: float = 0.0,
    start_date: date = date(2025, 1, 1),
) -> List[Dict[str, Any]]:
    """Generate deterministic oscillating + trending daily OHLCV candles that
    reliably trigger EMA crossover, RSI, and Supertrend entries and exits."""
    rows: List[Dict[str, Any]] = []
    cur_date = start_date
    for i in range(n):
        while cur_date.weekday() >= 5:
            cur_date += timedelta(days=1)
        close = max(
            50.0,
            start_price + drift * i + wave_amp * math.sin((i / 9.0) + phase),
        )
        open_p = close * (1.0 - 0.004 * math.cos(i * 0.5))
        high = max(open_p, close) * 1.012
        low = min(open_p, close) * 0.988
        vol = 120000 + int(35000 * abs(math.sin(i * 0.4)))
        rows.append(
            {
                "ts_ist": f"{cur_date.isoformat()}T09:15:00+05:30",
                "trade_date": cur_date.isoformat(),
                "open": round(open_p, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": vol,
                "oi": 0,
                "source": "synthetic",
            }
        )
        cur_date += timedelta(days=1)
    return rows


class TestChargesEngine2026(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ChargesEngine()

    def test_presets_metadata(self) -> None:
        presets = list_charge_presets()
        self.assertIn("zerodha", presets["profiles"])
        self.assertIn("upstox", presets["profiles"])
        self.assertIn("zero", presets["profiles"])
        self.assertEqual(len(presets["segments"]), 4)

    def test_zerodha_vs_upstox_delivery(self) -> None:
        z_cfg = self.engine.resolve_config(profile="zerodha", segment="equity_delivery", overrides={"slippage_bps": 0.0})
        u_cfg = self.engine.resolve_config(profile="upstox", segment="equity_delivery", overrides={"slippage_bps": 0.0})

        z_rt = self.engine.calculate_round_trip(cfg=z_cfg, entry_price=1000.0, exit_price=1100.0, quantity=100)["charges"]
        u_rt = self.engine.calculate_round_trip(cfg=u_cfg, entry_price=1000.0, exit_price=1100.0, quantity=100)["charges"]

        # Zerodha equity delivery brokerage is ₹0; Upstox charges brokerage up to ₹20/leg
        self.assertAlmostEqual(z_rt["brokerage"], 0.0, places=2)
        self.assertGreater(u_rt["brokerage"], 0.0)
        self.assertLessEqual(u_rt["brokerage"], 40.0)
        # Delivery STT is 0.1% on both buy & sell = 0.001 * (100000 + 110000) = 210
        self.assertAlmostEqual(z_rt["stt"], 210.0, places=2)
        self.assertAlmostEqual(u_rt["stt"], 210.0, places=2)
        # Delivery DP charges apply on sell
        self.assertGreater(z_rt["dp_charges"], 0.0)
        self.assertGreater(u_rt["dp_charges"], 0.0)
        # 18% GST strictly on brokerage + exchange_txn + sebi_fee (NOT STT or stamp duty)
        expected_u_gst = 0.18 * (u_rt["brokerage"] + u_rt["exchange_txn"] + u_rt["sebi_fee"])
        self.assertAlmostEqual(u_rt["gst"], expected_u_gst, places=1)

    def test_2026_futures_and_options_stt_rates(self) -> None:
        fut_cfg = self.engine.resolve_config(profile="zerodha", segment="equity_futures", overrides={"slippage_bps": 0.0})
        fut_sell = self.engine.calculate_leg(cfg=fut_cfg, side="SELL", price=2000.0, quantity=500)
        # April 2026 NSE Futures STT = 0.05% on sell turnover (1,000,000 * 0.0005 = 500)
        self.assertAlmostEqual(fut_sell.stt, 500.0, places=2)

        opt_cfg = self.engine.resolve_config(profile="upstox", segment="equity_options", overrides={"slippage_bps": 0.0})
        opt_sell = self.engine.calculate_leg(cfg=opt_cfg, side="SELL", price=200.0, quantity=500)
        # April 2026 NSE Options STT = 0.15% on sell premium turnover (100,000 * 0.0015 = 150)
        self.assertAlmostEqual(opt_sell.stt, 150.0, places=2)
        # Flat ₹20 brokerage per options order
        self.assertAlmostEqual(opt_sell.brokerage, 20.0, places=2)

    def test_zero_cost_and_custom_overrides(self) -> None:
        zero_cfg = self.engine.resolve_config(profile="zero", segment="equity_intraday")
        z_rt = self.engine.calculate_round_trip(cfg=zero_cfg, entry_price=500.0, exit_price=520.0, quantity=50)["charges"]
        self.assertAlmostEqual(z_rt["total_charges"], 0.0, places=4)

        custom_cfg = self.engine.resolve_config(
            profile="zerodha",
            segment="equity_intraday",
            overrides={"brokerage_flat_per_order": 10.0, "brokerage_is_flat_only": True, "slippage_bps": 10.0},
        )
        c_rt = self.engine.calculate_round_trip(cfg=custom_cfg, entry_price=1000.0, exit_price=1000.0, quantity=100)["charges"]
        self.assertAlmostEqual(c_rt["brokerage"], 20.0, places=2)
        self.assertAlmostEqual(c_rt["slippage_cost"], 200.0, places=2)


class TestVisualRuleEngine(unittest.TestCase):
    def test_visual_ast_crossover_and_export_to_python(self) -> None:
        candles = _make_synthetic_candles(n=180, start_price=1000.0, wave_amp=90.0)
        rule_def = {
            "id": "vis_test_ema",
            "kind": "strategy",
            "mode": "visual",
            "name": "Test EMA 9/21 Visual",
            "description": "EMA 9 crosses above EMA 21 with RSI > 45",
            "timeframe": "1day",
            "segment": "equity_intraday",
            "stop_loss_pct": 3.0,
            "take_profit_pct": 8.0,
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {"type": "ema", "period": 9, "offset": 0},
                        "operator": "crosses_above",
                        "right": {"type": "ema", "period": 21, "offset": 0},
                    },
                    {
                        "left": {"type": "rsi", "period": 14, "offset": 0},
                        "operator": ">",
                        "right": {"type": "number", "value": 45},
                    },
                ],
            },
            "exit": {
                "logic": "OR",
                "conditions": [
                    {
                        "left": {"type": "ema", "period": 9, "offset": 0},
                        "operator": "crosses_below",
                        "right": {"type": "ema", "period": 21, "offset": 0},
                    }
                ],
            },
        }
        tk = IndicatorToolkit(candles, backend="builtin")
        strat = compile_visual_strategy(rule_def)
        strat.prepare(candles, {}, tk)
        enters = 0
        exits = 0
        for idx, bar in enumerate(candles):
            e_in, e_out = strat.signal_at(idx, bar)
            if e_in:
                enters += 1
            if e_out:
                exits += 1
        self.assertGreater(enters, 0)
        self.assertGreater(exits, 0)

        # Export to standalone Python plugin inside a temp dir & verify valid syntax
        with tempfile.TemporaryDirectory() as tmp:
            exported = export_rule_to_python(rule_def, kind="strategy", target_dir=Path(tmp))
            self.assertTrue(exported["ok"])
            compile(exported["code"], "<visual_export>", "exec")
            self.assertIn("class ", exported["code"])
            self.assertIn("def signal_at", exported["code"])

        # Evaluate condition group directly
        self.assertTrue(
            evaluate_condition_group(
                {
                    "logic": "AND",
                    "conditions": [
                        {
                            "left": {"type": "close", "offset": 0},
                            "operator": ">",
                            "right": {"type": "number", "value": 0},
                        }
                    ],
                },
                candles,
                tk,
                index=len(candles) - 1,
            )
        )

    def test_visual_screen_and_store_crud(self) -> None:
        candles = _make_synthetic_candles(n=120, start_price=500.0, drift=2.0, wave_amp=10.0)
        screen_def = {
            "id": "vis_scan_uptrend",
            "kind": "screen",
            "name": "Close Above EMA 20",
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {"type": "close", "offset": 0},
                        "operator": ">",
                        "right": {"type": "ema", "period": 20, "offset": 0},
                    }
                ],
            },
        }
        tk = IndicatorToolkit(candles, backend="builtin")
        screen = compile_visual_screen(screen_def)
        out = screen.evaluate(candles, {}, tk)
        self.assertTrue(out.matched)
        self.assertGreater(out.score, 0.0)

        with tempfile.TemporaryDirectory() as tmp:
            store = VisualRuleStore(store_file=Path(tmp) / "rules.json")
            initial_list = store.list_rules()
            self.assertGreaterEqual(len(initial_list), 2)
            saved = store.save(screen_def)
            self.assertEqual(saved["id"], "vis_scan_uptrend")
            self.assertIsNotNone(store.get("vis_scan_uptrend"))
            deleted = store.delete("vis_scan_uptrend")
            self.assertEqual(deleted["removed"], 1)
            self.assertIsNone(store.get("vis_scan_uptrend"))


class TestDualModePortfolioBacktest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hist = HistoricalDataManager(db_path=Path(self._tmp.name) / "hist.db")
        self.single_engine = BacktestEngine(hist_manager=self.hist)
        self.engine = PortfolioBacktestEngine(
            hist_manager=self.hist,
            single_engine=self.single_engine,
            runs_file=Path(self._tmp.name) / "runs.json",
        )
        self.symbols = ["RELIANCE", "TCS", "INFY"]
        self._synth_map: Dict[str, List[Dict[str, Any]]] = {}
        for idx, sym in enumerate(self.symbols):
            self._synth_map[sym] = _make_synthetic_candles(
                n=220,
                start_price=900.0 + idx * 250.0,
                wave_amp=95.0,
                drift=0.8,
                phase=idx * 1.1,
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fetcher(self, symbol: str, exchange: str, timeframe: str, start: date, end: date) -> List[Dict[str, Any]]:
        rows = self._synth_map.get(symbol.upper(), [])
        s_iso = start.isoformat()
        e_iso = end.isoformat()
        return [r for r in rows if s_iso <= r["trade_date"] <= e_iso]

    def test_individual_mode_python_strategy_with_2026_charges(self) -> None:
        res = self.engine.run(
            mode="individual",
            strategy="ema_crossover",
            symbols=self.symbols,
            exchange="NSE",
            timeframe="1day",
            from_date="2025-01-01",
            to_date="2025-11-30",
            initial_capital=100000.0,
            stop_loss_pct=3.0,
            take_profit_pct=8.0,
            charges_profile="zerodha",
            segment="equity_intraday",
            provider="synthetic",
            params={"fast_period": 9, "slow_period": 21},
            fetcher=self._fetcher,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "individual")
        self.assertEqual(len(res["per_symbol"]), 3)
        self.assertGreater(res["total_trades"], 0)
        self.assertGreater(res["total_charges"], 0.0)
        self.assertLess(res["net_pnl"], res["gross_pnl"])
        self.assertIn("deploy_spec", res)
        self.assertEqual(res["deploy_spec"]["strategy_id"], "ema_crossover")

    def test_portfolio_mode_all_three_allocations_visual_strategy(self) -> None:
        visual_rule = {
            "id": "vis_port_test",
            "kind": "strategy",
            "mode": "visual",
            "name": "Portfolio EMA Cross",
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {"type": "ema", "period": 9},
                        "operator": "crosses_above",
                        "right": {"type": "ema", "period": 21},
                    }
                ],
            },
            "exit": {
                "logic": "OR",
                "conditions": [
                    {
                        "left": {"type": "ema", "period": 9},
                        "operator": "crosses_below",
                        "right": {"type": "ema", "period": 21},
                    }
                ],
            },
        }
        for alloc in ("shared_pool", "equal_split", "risk_based"):
            res = self.engine.run(
                mode="portfolio",
                allocation_mode=alloc,
                signal_ranking="momentum",
                strategy="vis_port_test",
                rule_def=visual_rule,
                symbols=self.symbols,
                exchange="NSE",
                timeframe="1day",
                from_date="2025-01-01",
                to_date="2025-11-30",
                initial_capital=300000.0,
                max_open_positions=2,
                risk_per_trade_pct=1.5,
                stop_loss_pct=3.0,
                take_profit_pct=8.0,
                charges_profile="upstox",
                segment="equity_intraday",
                provider="synthetic",
                fetcher=self._fetcher,
            )
            self.assertTrue(res["ok"], f"Portfolio mode failed for {alloc}")
            self.assertEqual(res["mode"], "portfolio")
            self.assertEqual(res["allocation_mode"], alloc)
            self.assertEqual(res["strategy_mode"], "visual")
            self.assertGreater(res["total_trades"], 0)
            self.assertGreater(res["total_charges"], 0.0)
            self.assertGreater(len(res["equity_curve"]), 10)
            self.assertEqual(len(res["per_symbol"]), 3)
            self.assertIn("charges_breakdown", res)

        runs = self.engine.list_runs()
        self.assertEqual(len(runs), 3)


class TestOptionsAndTechnicals(unittest.TestCase):
    def test_options_iron_condor_payoff_and_greeks(self) -> None:
        legs = build_template_legs(
            "iron_condor",
            spot=24200.0,
            step=100.0,
            lot_size=75,
            dte_days=7.0,
            iv_pct=15.0,
        )
        self.assertEqual(len(legs), 4)
        analysis = analyze_options_strategy(
            underlying="NIFTY",
            spot=24200.0,
            legs=legs,
            dte_days=7.0,
            charges_profile="zerodha",
        )
        self.assertTrue(analysis["ok"])
        self.assertFalse(analysis["max_profit_unlimited"])
        self.assertFalse(analysis["max_loss_unlimited"])
        self.assertGreater(analysis["max_profit"], 0.0)
        self.assertLess(analysis["max_loss"], 0.0)
        self.assertEqual(len(analysis["breakevens"]), 2)
        self.assertGreater(analysis["charges"]["total_charges"], 0.0)
        self.assertIn("delta", analysis["net_greeks"])
        self.assertGreater(len(analysis["payoff_curve"]), 50)

    def test_technicals_analyzer_gauges_pivots_and_sr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hist = HistoricalDataManager(db_path=Path(tmp) / "tech.db")
            bars = _make_synthetic_candles(n=250, start_price=1500.0, drift=1.5, wave_amp=50.0)
            analyzer = TechnicalsAnalyzer(hist_manager=hist)
            res = analyzer.analyze(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="1day",
                provider="synthetic",
                fetcher=lambda sym, exch, tf, s, e: list(bars),
            )
            self.assertTrue(res["ok"])
            self.assertIn(res["summary"]["rating"], ("Strong Buy", "Buy", "Neutral", "Sell", "Strong Sell"))
            self.assertEqual(len(res["moving_averages"]["rows"]), 10)
            self.assertGreaterEqual(len(res["oscillators"]["rows"]), 5)
            self.assertIn("pivot", res["pivots"]["classic"])
            self.assertIn("r3", res["pivots"]["camarilla"])


class TestDeploymentManagerLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.db_path = tmp_path / "deployments.db"
        self.hist = HistoricalDataManager(db_path=tmp_path / "hist.db")
        self.audit = AuditTrail(db_path=tmp_path / "audit.db")
        self.kill_switch = KillSwitchController(state_file=tmp_path / "kill.json", audit=self.audit)
        self.risk_mgr = RiskLimitManager(
            limits_file=tmp_path / "risk.json",
            audit=self.audit,
            kill_switch=self.kill_switch,
        )
        self.bars = _make_synthetic_candles(n=120, start_price=1000.0, drift=1.0, wave_amp=20.0)
        self.mgr = DeploymentManager(
            db_path=self.db_path,
            hist_manager=self.hist,
            risk_mgr=self.risk_mgr,
            kill_switch=self.kill_switch,
            audit=self.audit,
        )

    def tearDown(self) -> None:
        for dep in self.mgr.list_deployments()["deployments"]:
            try:
                self.mgr.control(dep["id"], "stop")
            except Exception:
                pass
        self._tmp.cleanup()

    def _hist_fetcher(self, sym: str, exch: str, tf: str, s: date, e: date) -> List[Dict[str, Any]]:
        return list(self.bars)

    def test_paper_trade_fill_mtm_charges_and_square_off(self) -> None:
        res = self.mgr.deploy(
            name="Paper RELIANCE",
            exec_mode="paper",
            strategy_id="ema_crossover",
            symbols=["RELIANCE"],
            exchange="NSE",
            timeframe="1day",
            initial_capital=100000.0,
            stop_loss_pct=5.0,
            take_profit_pct=10.0,
            charges_profile="zerodha",
            segment="equity_intraday",
            params={"fast_period": 9, "slow_period": 21},
            provider="synthetic",
            start_worker=False,
            hist_fetcher=self._hist_fetcher,
        )
        dep_id = res["deployment"]["id"]

        # Trigger deterministic BUY entry at ₹1000
        self.mgr.trigger_manual_signal(dep_id, symbol="RELIANCE", side="BUY", price=1000.0, reason="Test entry")
        snap = self.mgr.list_deployments()
        rec = next(d for d in snap["deployments"] if d["id"] == dep_id)
        self.assertEqual(rec["open_positions_count"], 1)
        self.assertGreater(rec["total_charges"], 0.0)

        # Feed live tick at ₹1050 -> verify positive MTM unrealized P&L
        self.mgr.process_ticks(dep_id, {"RELIANCE": {"ltp": 1050.0, "volume": 10000}})
        snap2 = self.mgr.list_deployments()
        rec2 = next(d for d in snap2["deployments"] if d["id"] == dep_id)
        self.assertGreater(rec2["unrealized_pnl"], 0.0)

        # Square-off -> realizes profit minus 2026 Indian charges
        self.mgr.control(dep_id, "square_off")
        snap3 = self.mgr.list_deployments()
        rec3 = next(d for d in snap3["deployments"] if d["id"] == dep_id)
        self.assertEqual(rec3["open_positions_count"], 0)
        self.assertEqual(rec3["trades_count"], 1)
        self.assertGreater(rec3["realized_pnl"], 0.0)

        # Reboot safety: new DeploymentManager instance reloads persisted deployment as PAUSED
        reloaded = DeploymentManager(
            db_path=self.db_path,
            hist_manager=self.hist,
            risk_mgr=self.risk_mgr,
            kill_switch=self.kill_switch,
            audit=self.audit,
        )
        rec_reloaded = next(d for d in reloaded.list_deployments()["deployments"] if d["id"] == dep_id)
        self.assertEqual(rec_reloaded["status"], "PAUSED")

    def test_live_alert_queue_and_kill_switch_gating(self) -> None:
        res = self.mgr.deploy(
            name="Live Alert RELIANCE",
            exec_mode="live_alert",
            strategy_id="ema_crossover",
            symbols=["RELIANCE"],
            exchange="NSE",
            timeframe="1day",
            initial_capital=100000.0,
            charges_profile="zerodha",
            segment="equity_intraday",
            provider="synthetic",
            start_worker=False,
            hist_fetcher=self._hist_fetcher,
        )
        dep_id = res["deployment"]["id"]

        # Signal creates a PENDING 1-click actionable alert
        self.mgr.trigger_manual_signal(dep_id, symbol="RELIANCE", side="BUY", price=1000.0, reason="Signal alert")
        snap = self.mgr.list_deployments()
        self.assertEqual(len(snap["pending_alerts"]), 1)
        alert_id = snap["pending_alerts"][0]["id"]

        # Engage Kill Switch -> executing alert must be BLOCKED safely
        self.kill_switch.engage(reason="Unit test safety lock")
        res_blocked = self.mgr.resolve_alert(alert_id, action="execute")
        self.assertEqual(res_blocked["status"], "BLOCKED")

        # Live Auto-Execute with Kill Switch active must also block without firing orders
        res_auto = self.mgr.deploy(
            name="Live Auto RELIANCE",
            exec_mode="live_auto",
            strategy_id="ema_crossover",
            symbols=["RELIANCE"],
            exchange="NSE",
            timeframe="1day",
            initial_capital=100000.0,
            charges_profile="zerodha",
            segment="equity_intraday",
            provider="synthetic",
            start_worker=False,
            hist_fetcher=self._hist_fetcher,
        )
        auto_id = res_auto["deployment"]["id"]
        self.mgr.trigger_manual_signal(auto_id, symbol="RELIANCE", side="BUY", price=1000.0, reason="Auto signal")
        snap_auto = self.mgr.list_deployments()
        auto_rec = next(d for d in snap_auto["deployments"] if d["id"] == auto_id)
        self.assertEqual(auto_rec["open_positions_count"], 0)
        self.assertIn("BLOCKED", auto_rec["last_note"].upper())


class TestBasketsAndDiscover(unittest.TestCase):
    def test_baskets_crud_and_discover_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BasketStore(store_file=Path(tmp) / "baskets.json")
            self.assertGreaterEqual(len(store.list_baskets()), 3)
            created = store.save(
                name="My IT Focus",
                description="Top IT names",
                exchange="NSE",
                symbols=["tcs", "infy", "hcltech", "TCS"],
            )
            self.assertEqual(created["symbols"], ["TCS", "INFY", "HCLTECH"])
            deleted = store.delete(created["id"])
            self.assertEqual(deleted["removed"], 1)

        catalog = list_discover_catalog()
        cats = {item["category"] for item in catalog}
        self.assertEqual(cats, {"strategy", "screen", "options"})
        modes = {item["mode"] for item in catalog}
        self.assertTrue({"visual", "python", "options"}.issubset(modes))

    def test_local_server_serves_built_react_spa_and_assets(self) -> None:
        import json as _json
        import re
        import threading
        import urllib.error
        import urllib.request
        from web.local_server import _AlgoDeskHTTPServer, _StdlibBrokerHandler
        from web.spa import has_spa_bundle

        self.assertTrue(has_spa_bundle())
        srv = _AlgoDeskHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
                body = r.read().decode("utf-8")
                self.assertEqual(r.status, 200)
                self.assertIn('<div id="root"></div>', body)
                js_match = re.search(r'src="(/assets/[^"]+\.js)"', body)
                css_match = re.search(r'href="(/assets/[^"]+\.css)"', body)
                self.assertIsNotNone(js_match)
                self.assertIsNotNone(css_match)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}{js_match.group(1)}", timeout=3) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("javascript", r.headers.get("Content-Type", ""))
                self.assertGreater(len(r.read()), 10000)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}{css_match.group(1)}", timeout=3) as r:
                self.assertEqual(r.status, 200)
                self.assertIn("text/css", r.headers.get("Content-Type", ""))

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                health = _json.loads(r.read().decode("utf-8"))
                self.assertTrue(health.get("spa"))
                self.assertGreaterEqual(health.get("phase", 0), 6)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/assets/../../config.py", timeout=3)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


class TestStreakScannerAndStrategyParity(unittest.TestCase):
    def test_new_visual_operands_pct_operators_enabled_flag_and_heikin_ashi(self) -> None:
        from rules.engine import to_heikin_ashi_candles

        candles = _make_synthetic_candles(n=90, start_price=1000.0, drift=2.5, wave_amp=15.0)
        # Force a clear +3.5% jump on the last candle for up_by_pct testing
        prev_c = candles[-2]["close"]
        candles[-1]["close"] = round(prev_c * 1.035, 2)
        candles[-1]["high"] = max(candles[-1]["high"], candles[-1]["close"])

        ha = to_heikin_ashi_candles(candles)
        self.assertEqual(len(ha), len(candles))
        self.assertNotEqual(ha[-1]["open"], candles[-1]["open"])

        rule_def = {
            "id": "vis_streak_parity_test",
            "kind": "screen",
            "name": "Streak Parity Check",
            "chart_type": "heikin_ashi",
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {"type": "close", "offset": 0},
                        "operator": "up_by_pct",
                        "right": {"type": "close", "offset": -1},
                        "pct": 1.0,
                        "enabled": True,
                    },
                    {
                        "left": {"type": "close", "offset": 0},
                        "operator": ">",
                        "right": {"type": "wma", "period": 10, "offset": 0},
                        "enabled": True,
                    },
                    {
                        "left": {"type": "vwap", "period": 14, "offset": 0},
                        "operator": ">",
                        "right": {"type": "number", "value": 0},
                        "enabled": True,
                    },
                    {
                        "left": {"type": "rsi_ma", "period": 14, "param2": 9, "offset": 0},
                        "operator": ">",
                        "right": {"type": "number", "value": 0},
                        "enabled": True,
                    },
                    {
                        # Disabled impossible condition must be skipped
                        "left": {"type": "close", "offset": 0},
                        "operator": "<",
                        "right": {"type": "number", "value": -999},
                        "enabled": False,
                    },
                ],
            },
        }
        tk = IndicatorToolkit(candles, backend="builtin")
        screen = compile_visual_screen(rule_def)
        res = screen.evaluate(candles, {}, tk)
        self.assertTrue(res.matched)

    def test_inline_rule_def_scan_with_as_of_date(self) -> None:
        from screener.engine import ScreenerEngine

        with tempfile.TemporaryDirectory() as tmp:
            hist = HistoricalDataManager(db_path=Path(tmp) / "hist.db")
            bars = _make_synthetic_candles(n=120, start_price=800.0, drift=3.0, wave_amp=5.0)
            target_date = bars[80]["trade_date"]
            engine = ScreenerEngine(hist_manager=hist)
            inline_rule = {
                "kind": "screen",
                "name": "Inline Above EMA",
                "entry": {
                    "logic": "AND",
                    "conditions": [
                        {
                            "left": {"type": "close", "offset": 0},
                            "operator": ">",
                            "right": {"type": "ema", "period": 10, "offset": 0},
                        }
                    ],
                },
            }
            out = engine.scan(
                screen_id="visual:inline",
                rule_def=inline_rule,
                symbols=["RELIANCE", "TCS"],
                exchange="NSE",
                timeframe="1day",
                as_of_date=target_date,
                provider="synthetic",
                fetcher=lambda sym, exch, tf, s, e: [b for b in bars if b["trade_date"] <= e.isoformat()],
            )
            self.assertTrue(out["ok"])
            self.assertEqual(out["as_of_date"], target_date)
            self.assertGreaterEqual(len(out["results"]), 1)
            self.assertIn("ltp", out["results"][0])
            self.assertIn("volume", out["results"][0])
            self.assertLessEqual(out["results"][0]["last_date"], target_date)

    def test_scanner_alerts_manager_lifecycle_and_diffing(self) -> None:
        from screener.alerts import ScannerAlertsManager

        with tempfile.TemporaryDirectory() as tmp:
            calls = {"n": 0}

            def fake_scan(**kwargs: Any) -> Dict[str, Any]:
                calls["n"] += 1
                if calls["n"] == 1:
                    rows = [{"symbol": "RELIANCE", "exchange": "NSE", "ltp": 2500.0, "change_pct": 1.8, "volume": 100000, "signal": "BULLISH", "matched": True}]
                else:
                    rows = [{"symbol": "TCS", "exchange": "NSE", "ltp": 3900.0, "change_pct": 2.1, "volume": 80000, "signal": "BULLISH", "matched": True}]
                return {"ok": True, "results": rows}

            mgr = ScannerAlertsManager(store_file=Path(tmp) / "alerts.json", scan_runner=fake_scan)
            created = mgr.create_or_update({"name": "Breakout Watch", "screen_id": "rsi_oversold", "universe": "nifty50"})
            sub_id = created["subscription"]["id"]
            first = mgr.control(sub_id, "scan_now")
            self.assertEqual(first["subscription"]["last_matched_symbols"], ["RELIANCE"])

            # Second scan: RELIANCE exits, TCS enters
            mgr.control(sub_id, "scan_now")
            snap = mgr.list_alerts()
            transitions = {(e["symbol"], e["transition"]) for e in snap["history"]}
            self.assertIn(("RELIANCE", "ENTERED"), transitions)
            self.assertIn(("RELIANCE", "EXITED"), transitions)
            self.assertIn(("TCS", "ENTERED"), transitions)

            # Pause, resume, delete
            paused = mgr.control(sub_id, "pause")
            self.assertEqual(paused["subscription"]["status"], "PAUSED")
            resumed = mgr.control(sub_id, "resume")
            self.assertEqual(resumed["subscription"]["status"], "ACTIVE")
            mgr.delete(sub_id)
            self.assertEqual(len(mgr.list_alerts()["subscriptions"]), 0)

    def test_python_plugin_save_validates_syntax_and_blocks_traversal(self) -> None:
        from web.service import BrokerHubService

        svc = BrokerHubService()
        # Path traversal must be rejected
        with self.assertRaises(ValueError):
            svc.save_python_plugin(kind="screen", filename="../../evil.py", code="x = 1\n")

        # Syntax error must be rejected before writing
        with self.assertRaises(ValueError):
            svc.save_python_plugin(kind="screen", filename="bad_syntax_test.py", code="def broken(:\n    pass\n")

        # Source loader must succeed for built-in plugins
        src = svc.get_python_plugin_source(kind="strategy", plugin_id="ema_crossover")
        self.assertTrue(src["ok"])
        self.assertIn("class ", src["code"])

    def test_spa_bundle_integrity_rejects_missing_assets(self) -> None:
        from web.spa import is_complete_spa_bundle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "index.html").write_text(
                '<!doctype html><html><head>'
                '<script type="module" src="/assets/index-ABC123.js"></script>'
                '<link rel="stylesheet" href="/assets/index-XYZ789.css">'
                '</head><body><div id="root"></div></body></html>',
                encoding="utf-8",
            )
            # Missing JS and CSS must be rejected so server never serves a blank screen
            self.assertFalse(is_complete_spa_bundle(root))

            (root / "assets" / "index-ABC123.js").write_text("console.log('ok');", encoding="utf-8")
            # Still missing CSS
            self.assertFalse(is_complete_spa_bundle(root))

            (root / "assets" / "index-XYZ789.css").write_text("body{}", encoding="utf-8")
            self.assertTrue(is_complete_spa_bundle(root))


if __name__ == "__main__":
    unittest.main()

