"""
tests/test_production_hardening.py
==================================
12 Mandatory Pre-Live Safety, Execution Authority, Risk Watchdog, Backtest Realism,
Monthly 1-Min Parquet Partitioning & Query-First SQLite `asset_data_history` Tests.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from backtest.base import BacktestStrategy
from backtest.engine import BacktestEngine, compute_sharpe_metrics
from broker.base import (
    AccountBalance,
    AmbiguousSubmissionError,
    BrokerBase,
    BrokerCapabilities,
    BrokerHealthSnapshot,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    ExecutionFill,
    OrderRequest,
    OrderStatus,
    make_broker_tag,
)
from broker.upstox.broker import UpstoxBroker
from broker.zerodha.broker import ZerodhaBroker
from config import IST_TZ
from core.audit import AuditTrail
from core.calendar import get_exchange_schedule, is_exchange_holiday, is_market_open
from core.historical import HistoricalDataManager
from core.preflight import KillSwitchController, PreflightChecker
from deploy.manager import DeploymentManager
from execution.oms import OrderManagementSystem
from notifications.telegram import TelegramNotifier
from risk.limits import RiskLimitManager, RiskWatchdog
from web.local_server import _StdlibBrokerHandler
from web.security import RateLimitError, SecurityManager, is_live_trading_enabled, validate_python_plugin_ast


class _FakeJsonResp(io.BytesIO):
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _StubBroker(BrokerBase):
    """Programmable broker stub for deterministic OMS, reconciliation, and kill-switch tests."""

    name = "stub"
    display_name = "Stub Live Broker"
    stage = "active"
    capabilities = BrokerCapabilities(supports_trading=True, supports_market_data=False)

    def __init__(self) -> None:
        self._connected = True
        self.place_calls: List[OrderRequest] = []
        self.cancelled_ids: List[str] = []
        self.mode: str = "reject"  # reject | timeout_then_filled | partial_then_full | open
        self.broker_orders: List[BrokerOrder] = []
        self.broker_positions: List[BrokerPosition] = []
        self.funds_by_segment: Dict[str, Dict[str, float]] = {
            "equity": {"available_margin": 250000.0, "used_margin": 0.0, "total_balance": 250000.0},
            "commodity": {"available_margin": 75000.0, "used_margin": 0.0, "total_balance": 75000.0},
        }
        self._seq = 0

    def connect(self) -> bool:
        self._connected = True
        return True

    def health_check(self, timeout: float = 1.0) -> BrokerHealthSnapshot:
        return BrokerHealthSnapshot(
            broker_id=self.name,
            display_name=self.display_name,
            stage=self.stage,
            state=ConnectionState.CONNECTED,
            capabilities=self.capabilities,
            message="Stub connected",
        )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        order.require_exchange()
        self.place_calls.append(order)
        self._seq += 1
        oid = f"BRK-{self._seq:04d}"

        if self.mode == "reject":
            bo = BrokerOrder(
                order_id=oid,
                request=order,
                status=OrderStatus.REJECTED,
                filled_quantity=0,
                average_fill_price=0.0,
                rejection_reason="Insufficient margin at exchange",
            )
            self.broker_orders.append(bo)
            return bo

        if self.mode == "timeout_then_filled":
            # Broker actually accepted and filled it, but HTTP response timed out!
            bo = BrokerOrder(
                order_id=oid,
                request=order,
                status=OrderStatus.FILLED,
                filled_quantity=order.quantity,
                average_fill_price=float(order.price or 100.0),
            )
            self.broker_orders.append(bo)
            raise TimeoutError("Read timed out waiting for broker acknowledgement")

        if self.mode == "partial_then_full":
            bo = BrokerOrder(
                order_id=oid,
                request=order,
                status=OrderStatus.PARTIALLY_FILLED,
                filled_quantity=min(30, order.quantity),
                average_fill_price=200.0,
            )
            self.broker_orders.append(bo)
            return bo

        bo = BrokerOrder(
            order_id=oid,
            request=order,
            status=OrderStatus.OPEN,
            filled_quantity=0,
            average_fill_price=0.0,
        )
        self.broker_orders.append(bo)
        return bo

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled_ids.append(order_id)
        for o in self.broker_orders:
            if o.order_id == order_id:
                o.status = OrderStatus.CANCELLED
        return True

    def get_orders(self) -> List[BrokerOrder]:
        return list(self.broker_orders)

    def get_positions(self) -> List[BrokerPosition]:
        return list(self.broker_positions)

    def get_balance(self) -> AccountBalance:
        eq = self.funds_by_segment["equity"]
        return AccountBalance(cash=eq["available_margin"], equity=eq["total_balance"])

    def get_funds(self, segment: str = "equity") -> Dict[str, float]:
        key = "commodity" if str(segment).lower() in {"com", "commodity", "mcx"} else "equity"
        return dict(self.funds_by_segment[key])


class _JsonParquetIO:
    """
    Deterministic file-backed ParquetIO stand-in for test environments without pyarrow.
    Persists rows directly to the target `.parquet` path so real filesystem paths,
    directory layout, file count, and exact `mtime` immutability are verified on disk.
    """

    def __init__(self) -> None:
        self.write_log: List[Path] = []
        self.read_log: List[Path] = []

    def available(self) -> bool:
        return True

    def read_rows(self, path: Path) -> List[Dict[str, Any]]:
        self.read_log.append(path)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def write_rows(self, path: Path, rows: List[Dict[str, Any]]) -> None:
        self.write_log.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        dedup: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            dedup[str(r["ts_ist"])] = dict(r)
        ordered = [dedup[k] for k in sorted(dedup.keys())]
        tmp = path.with_suffix(".tmp.parquet")
        tmp.write_text(json.dumps(ordered), encoding="utf-8")
        os.replace(tmp, path)


class _ShortSignalStrategy(BacktestStrategy):
    id = "test_short_gap"
    name = "Test Short Gap"

    def prepare(self, candles, params, toolkit) -> None:
        self._n = len(candles)

    def signal_at(self, index: int, bar: Dict[str, Any]):
        # Emit entry on bar 5 so short entry fills at bar 6 open
        return (index == 5, False)


class TestProductionHardening(unittest.TestCase):
    """Covers all 12 Mandatory Pre-Live & Data Integrity Acceptance Tests."""

    # ── Test 1: Live Order Rejection -> 0 fills, 0 positions, 0 PnL ───────
    def test_01_live_order_rejection_produces_zero_fills_or_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            broker = _StubBroker()
            broker.mode = "reject"

            req = OrderRequest(
                symbol="RELIANCE",
                exchange="NSE",
                side="BUY",
                quantity=10,
                order_type="MARKET",
                product="MIS",
                price=2500.0,
            )
            rec, delta_qty, fill_px = oms.submit_order(
                req,
                broker,
                deployment_id="dep-1",
                exec_mode="live_auto",
            )
            self.assertEqual(rec["status"], OrderStatus.REJECTED.value)
            self.assertEqual(rec["filled_quantity"], 0)
            self.assertEqual(delta_qty, 0)
            self.assertEqual(fill_px, 0.0)
            self.assertIn("Insufficient margin", rec["rejection_reason"])

            # Verify DeploymentManager live_auto never creates a synthetic position on REJECTED
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            mgr = DeploymentManager(
                db_path=tmp / "dep.db",
                oms=oms,
                kill_switch=ks,
                risk_mgr=risk,
                audit=audit,
                broker_resolver=lambda _bid: broker,
                require_live_env_lock=False,
            )
            from charges.calculator import charges_engine

            dep_state = {
                "id": "dep-live-rej",
                "name": "Live Rej Test",
                "exec_mode": "live_auto",
                "strategy_id": "ema_crossover",
                "exchange": "NSE",
                "spec": {"product": "MIS", "segment": "equity_intraday", "charges_profile": "zerodha"},
                "initial_capital": 100000.0,
                "cash": 100000.0,
                "realized_pnl": 0.0,
                "total_charges": 0.0,
                "positions": {},
                "trades": [],
                "instrument_keys": {"RELIANCE": "NSE_EQ|INE002A01018"},
            }
            cfg = charges_engine.resolve_config(profile="zerodha", segment="equity_intraday")
            dispatch_res = mgr._dispatch_live_broker_order_locked(
                dep_state,
                "RELIANCE",
                "BUY",
                10,
                2500.0,
                "test_signal",
                cfg,
            )
            self.assertEqual(dispatch_res["kind"], "rejected")
            self.assertEqual(dispatch_res["order"]["status"], OrderStatus.REJECTED.value)
            self.assertEqual(dep_state["positions"], {})
            self.assertEqual(dep_state["trades"], [])
            self.assertEqual(dep_state["realized_pnl"], 0.0)

    # ── Test 2: Timeout & Idempotency -> No Duplicate + Reconcile Recovery ─
    def test_02_live_order_timeout_idempotency_and_reconcile_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            broker = _StubBroker()
            broker.mode = "timeout_then_filled"

            cid = "adk-idemp-timeout-001"
            req = OrderRequest(
                symbol="TCS",
                exchange="NSE",
                side="BUY",
                quantity=5,
                order_type="LIMIT",
                product="MIS",
                price=3800.0,
                client_order_id=cid,
            )
            rec1, delta1, _ = oms.submit_order(req, broker, deployment_id="dep-2", exec_mode="live_auto")
            self.assertEqual(delta1, 0)
            self.assertEqual(len(broker.place_calls), 1)
            self.assertIn(rec1["status"], {OrderStatus.SUBMITTED.value, OrderStatus.PENDING.value})

            # Retry with same client_order_id must NOT place a duplicate broker order
            rec2, delta2, _ = oms.submit_order(req, broker, deployment_id="dep-2", exec_mode="live_auto")
            self.assertEqual(len(broker.place_calls), 1)
            self.assertEqual(rec2["client_order_id"], cid)
            self.assertEqual(delta2, 0)

            # Reconciliation polls broker get_orders() and recovers the filled execution cleanly
            reconcile_res = oms.reconcile_orders(broker, deployment_id="dep-2")
            deltas = reconcile_res["fills_applied"]
            self.assertEqual(len(deltas), 1)
            self.assertEqual(deltas[0]["client_order_id"], cid)
            self.assertEqual(deltas[0]["delta_qty"], 5)
            self.assertEqual(deltas[0]["status"], OrderStatus.FILLED.value)
            stored = oms.get_order_by_client_id(cid)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["filled_quantity"], 5)
            self.assertAlmostEqual(stored["average_fill_price"], 3800.0)

    # ── Test 3: Partial Fill (30/100) then Complete (100/100) ─────────────
    def test_03_partial_fill_deltas_accumulate_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            broker = _StubBroker()
            broker.mode = "partial_then_full"

            req = OrderRequest(
                symbol="INFY",
                exchange="NSE",
                side="BUY",
                quantity=100,
                order_type="LIMIT",
                product="CNC",
                price=200.0,
                client_order_id="adk-partial-001",
            )
            rec, delta1, fill_px1 = oms.submit_order(
                req, broker, deployment_id="dep-3", exec_mode="live_auto"
            )
            self.assertEqual(rec["status"], OrderStatus.PARTIALLY_FILLED.value)
            self.assertEqual(delta1, 30)
            self.assertAlmostEqual(fill_px1, 200.0)
            self.assertEqual(rec["filled_quantity"], 30)

            # Reconcile while still 30/100 -> zero extra delta
            self.assertEqual(oms.reconcile_orders(broker, deployment_id="dep-3")["fills_applied"], [])

            # Now broker completes remaining 70 shares -> total 100/100
            broker.broker_orders[0].filled_quantity = 100
            broker.broker_orders[0].average_fill_price = 202.0
            broker.broker_orders[0].status = OrderStatus.FILLED

            reconcile_res = oms.reconcile_orders(broker, deployment_id="dep-3")
            deltas = reconcile_res["fills_applied"]
            self.assertEqual(len(deltas), 1)
            self.assertEqual(deltas[0]["delta_qty"], 70)
            self.assertEqual(deltas[0]["status"], OrderStatus.FILLED.value)
            stored = oms.get_order_by_client_id("adk-partial-001")
            self.assertEqual(stored["filled_quantity"], 100)

            # Another reconcile must emit nothing (terminal)
            self.assertEqual(oms.reconcile_orders(broker, deployment_id="dep-3")["fills_applied"], [])

    # ── Test 4: SHORT/COVER Direction, Per-Order Product & Mandatory Exchange
    def test_04_short_cover_direction_product_and_mandatory_exchange(self) -> None:
        # Mandatory exchange check when neither exchange nor prefixed instrument_key is supplied
        blank_req = OrderRequest(symbol="RELIANCE", side="BUY", quantity=1)
        with self.assertRaises(ValueError):
            blank_req.require_exchange()

        # Verify UpstoxBroker payload for SHORT (CNC -> D) & COVER (NRML -> D/I) & blank exchange rejection
        upstox_payloads: List[Dict[str, Any]] = []

        def fake_upstox_urlopen(req, timeout=6):
            if req.data:
                upstox_payloads.append(json.loads(req.data.decode("utf-8")))
            return _FakeJsonResp({"status": "success", "data": {"order_ids": ["UPX-99"]}})

        ub = UpstoxBroker(access_token="tok-u")
        ub._connected = True
        ub._sdk = None
        ub._order_api = None

        rej = ub.place_order(OrderRequest(symbol="RELIANCE", side="BUY", quantity=1))
        self.assertEqual(rej.status, OrderStatus.REJECTED)

        with patch("urllib.request.urlopen", side_effect=fake_upstox_urlopen):
            ub.place_order(OrderRequest(symbol="RELIANCE", exchange="NSE", side="SHORT", quantity=5, product="CNC"))
            ub.place_order(OrderRequest(symbol="RELIANCE", exchange="NSE", side="COVER", quantity=5, product="MIS"))

        self.assertEqual(upstox_payloads[0]["transaction_type"], "SELL")
        self.assertEqual(upstox_payloads[0]["product"], "D")
        self.assertEqual(upstox_payloads[1]["transaction_type"], "BUY")
        self.assertEqual(upstox_payloads[1]["product"], "I")

        # Verify ZerodhaBroker payload for SHORT/COVER + NRML/CNC + blank exchange rejection
        kite_payloads: List[Dict[str, Any]] = []

        class _FakeKite:
            VARIETY_REGULAR = "regular"

            def place_order(self, **kwargs):
                kite_payloads.append(kwargs)
                return "KITE-101"

        zb = ZerodhaBroker(access_token="tok-z")
        zb._connected = True
        zb._kite = _FakeKite()

        z_rej = zb.place_order(OrderRequest(symbol="GOLD", side="SHORT", quantity=1))
        self.assertEqual(z_rej.status, OrderStatus.REJECTED)

        zb.place_order(OrderRequest(symbol="GOLD", exchange="MCX", side="SHORT", quantity=1, product="NRML"))
        zb.place_order(OrderRequest(symbol="GOLD", exchange="MCX", side="COVER", quantity=1, product="NRML"))
        self.assertEqual(kite_payloads[0]["transaction_type"], "SELL")
        self.assertEqual(kite_payloads[0]["exchange"], "MCX")
        self.assertEqual(kite_payloads[0]["product"], "NRML")
        self.assertEqual(kite_payloads[1]["transaction_type"], "BUY")

    # ── Test 5: Live Trading Master Lock Default-Off ──────────────────────
    def test_05_live_trading_master_lock_default_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            mgr = DeploymentManager(
                db_path=tmp / "dep.db",
                kill_switch=ks,
                risk_mgr=risk,
                audit=audit,
                require_live_env_lock=True,
            )

            with patch.dict(os.environ, {"ALGODESK_ENABLE_LIVE_TRADING": "false"}, clear=False):
                self.assertFalse(is_live_trading_enabled())
                with self.assertRaises(PermissionError):
                    mgr.deploy(
                        name="Blocked Live Auto",
                        exec_mode="live_auto",
                        strategy_id="ema_crossover",
                        symbols=["RELIANCE"],
                        exchange="NSE",
                        timeframe="1day",
                        initial_capital=100000,
                    )
                # Paper trading still works cleanly while live trading is locked
                with patch.object(mgr, "_refresh_deploy_ws_scope", return_value=None):
                    paper_dep = mgr.deploy(
                        name="Allowed Paper",
                        exec_mode="paper",
                        strategy_id="ema_crossover",
                        symbols=["RELIANCE"],
                        exchange="NSE",
                        timeframe="1day",
                        initial_capital=100000,
                        start_worker=False,
                    )
                self.assertEqual(paper_dep["deployment"]["exec_mode"], "paper")
                self.assertEqual(paper_dep["deployment"]["evaluation_mode"], "completed_bar")

            with patch.dict(os.environ, {"ALGODESK_ENABLE_LIVE_TRADING": "true"}, clear=False):
                self.assertTrue(is_live_trading_enabled())
                with patch.object(mgr, "_refresh_deploy_ws_scope", return_value=None):
                    live_dep = mgr.deploy(
                        name="Unlocked Live Auto",
                        exec_mode="live_auto",
                        strategy_id="ema_crossover",
                        symbols=["RELIANCE"],
                        exchange="NSE",
                        timeframe="1day",
                        initial_capital=100000,
                        start_worker=False,
                    )
                self.assertEqual(live_dep["deployment"]["exec_mode"], "live_auto")

    # ── Test 6: Plugin AST Validator Blocks Unsafe Imports/Calls ──────────
    def test_06_plugin_ast_validator_blocks_unsafe_code(self) -> None:
        dangerous_samples = [
            "import subprocess\nsubprocess.Popen(['id'])",
            "import os\nos.system('whoami')",
            "x = eval('1+1')",
            "m = __import__('os')",
            "f = open('/etc/passwd')",
            "x = ().__class__.__bases__[0].__subclasses__()",
        ]
        for code in dangerous_samples:
            violations = validate_python_plugin_ast(code, "bad.py")
            self.assertTrue(len(violations) > 0, f"Expected violation for: {code}")

        safe_code = """
from backtest.base import BacktestStrategy, StrategySignal

class SafeStrat(BacktestStrategy):
    id = "safe_strat"
    name = "Safe Strat"
    def evaluate(self, bars, toolkit):
        return []
"""
        self.assertEqual(validate_python_plugin_ast(safe_code, "safe.py"), [])

    # ── Test 7: Bearer Auth Middleware (401 without token, 200 with token) ─
    def test_07_bearer_auth_enforcement_and_claim_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sec = SecurityManager(auth_file=Path(tmpdir) / ".admin_auth.json")
            with patch.dict(os.environ, {"ALGODESK_API_TOKEN": "", "ALGODESK_REQUIRE_AUTH": "false"}, clear=False):
                st0 = sec.status()
                self.assertTrue(st0["needs_claim"])
                self.assertFalse(st0["auth_enforced"])

                claimed = sec.claim_passcode("strong-pass-2026")
                token = claimed["token"]
                self.assertTrue(token.startswith("adk_"))
                self.assertTrue(sec.is_auth_enforced())
                self.assertFalse(sec.status()["needs_claim"])

                # Second claim must be rejected (locked once claimed)
                with self.assertRaises(ValueError):
                    sec.claim_passcode("attacker-pass-999")

                # Verify Bearer check directly
                self.assertFalse(sec.verify_bearer(None))
                self.assertFalse(sec.verify_bearer("Bearer wrong-token"))
                self.assertTrue(sec.verify_bearer(f"Bearer {token}"))

                # Verify live HTTP 401 on protected endpoint via local_server handler
                with patch("web.local_server.security_manager", sec):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
                    port = server.server_address[1]
                    th = threading.Thread(target=server.serve_forever, daemon=True)
                    th.start()
                    try:
                        req_no_auth = urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/notifications/telegram",
                            data=json.dumps({"enabled": True}).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            urllib.request.urlopen(req_no_auth, timeout=4)
                        self.assertEqual(ctx.exception.code, 401)

                        req_with_auth = urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/notifications/telegram",
                            data=json.dumps({"enabled": True}).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {token}",
                            },
                            method="POST",
                        )
                        with urllib.request.urlopen(req_with_auth, timeout=4) as resp:
                            self.assertEqual(resp.status, 200)
                    finally:
                        server.shutdown()
                        server.server_close()

    # ── Test 8: Headless RiskWatchdog Breaches -> KillSwitch + Cancel + TG ─
    def test_08_headless_risk_watchdog_engages_kill_switch_cancels_orders_and_alerts_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            risk.save_limits({"max_daily_loss_inr": 5000.0, "auto_engage_kill_switch": True})

            sent_alerts: List[Dict[str, str]] = []
            notifier = TelegramNotifier(
                settings_file=tmp / "tg.json",
                sender=lambda tok, cid, txt: sent_alerts.append({"token": tok, "chat_id": cid, "text": txt}) or {"ok": True},
            )
            notifier.save_settings({"enabled": True, "chat_id": "12345678", "notify_daily_loss_breach": True})

            broker = _StubBroker()
            broker.mode = "open"
            open_order = broker.place_order(
                OrderRequest(symbol="RELIANCE", exchange="NSE", side="BUY", quantity=20, order_type="LIMIT", price=2500.0)
            )
            self.assertEqual(open_order.status, OrderStatus.OPEN)

            watchdog = RiskWatchdog(
                risk_mgr=risk,
                interval_sec=5.0,
                notifier=notifier,
                state_provider=lambda: {
                    "trading_broker": "stub",
                    "broker_adapter": broker,
                    "funds": {"available_margin": 100000.0, "used_margin": 50000.0, "total_balance": 150000.0},
                    "positions": [
                        {
                            "symbol": "RELIANCE",
                            "quantity": 20,
                            "average_price": 2500.0,
                            "last_price": 2200.0,
                            "realized_pnl": 0.0,
                            "unrealized_pnl": -6000.0,
                        }
                    ],
                    "orders": [
                        {
                            "order_id": open_order.order_id,
                            "status": "OPEN",
                            "remaining_quantity": 20,
                            "price": 2500.0,
                        }
                    ],
                },
            )
            res = watchdog.run_once()
            self.assertTrue(res["kill_switch_auto_triggered"])
            self.assertTrue(ks.is_engaged())
            self.assertIn(open_order.order_id, broker.cancelled_ids)
            self.assertGreaterEqual(len(sent_alerts), 1)
            self.assertIn("DAILY_LOSS_BREACH", sent_alerts[0]["text"])
            # Pending order exposure (20 * 2500 = 50,000) included alongside position exposure (20 * 2200 = 44,000)
            self.assertAlmostEqual(res["telemetry"]["pending_order_exposure_inr"], 50000.0)
            self.assertAlmostEqual(res["telemetry"]["gross_exposure_inr"], 94000.0)

            # Verify send_test() delivers a TEST alert cleanly
            test_res = notifier.send_test()
            self.assertTrue(test_res["ok"])
            self.assertTrue(test_res["sent"])
            self.assertTrue(any("[AlgoDesk | TEST]" in a["text"] for a in sent_alerts))

    # ── Test 9: Multi-Exchange Calendar & Preflight (NSE vs MCX & Holidays) ─
    def test_09_multi_exchange_calendar_holidays_and_segment_margins(self) -> None:
        # 20:00 IST on a regular trading Thursday (2026-09-10): NSE closed, MCX open
        evening_ist = datetime(2026, 9, 10, 20, 0, 0, tzinfo=IST_TZ)
        self.assertFalse(is_market_open(now_ist=evening_ist, exchange="NSE"))
        self.assertTrue(is_market_open(now_ist=evening_ist, exchange="MCX"))

        # Republic Day 2026-01-26 is a verified NSE holiday
        self.assertTrue(is_exchange_holiday(date(2026, 1, 26), "NSE"))
        self.assertFalse(is_market_open(now_ist=datetime(2026, 1, 26, 11, 0, 0, tzinfo=IST_TZ), exchange="NSE"))

        # Segment margin routing
        self.assertEqual(get_exchange_schedule("NSE").segment, "equity")
        self.assertEqual(get_exchange_schedule("NSE").upstox_margin_segment, "SEC")
        self.assertEqual(get_exchange_schedule("MCX").segment, "commodity")
        self.assertEqual(get_exchange_schedule("MCX").upstox_margin_segment, "COM")

        # PreflightChecker respects MCX open at 20:00 IST while warning/failing NSE
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            mcx_res = PreflightChecker.run_all(
                trading_broker="upstox",
                data_connection="yfinance",
                symbols=["GOLD"],
                exchange="MCX",
                kill_switch=ks,
                audit=audit,
                now_ist=evening_ist,
                probe_brokers=False,
            )
            nse_res = PreflightChecker.run_all(
                trading_broker="upstox",
                data_connection="yfinance",
                symbols=["RELIANCE"],
                exchange="NSE",
                kill_switch=ks,
                audit=audit,
                now_ist=evening_ist,
                probe_brokers=False,
            )
            mcx_hours = next(c for c in mcx_res["checks"] if c["id"] == "market_hours")
            nse_hours = next(c for c in nse_res["checks"] if c["id"] == "market_hours")
            self.assertTrue(mcx_hours["passed"])
            self.assertFalse(nse_hours["passed"])

    # ── Test 10: Backtest Sharpe Consistency, Short Gap-Stop & Completed-Bar
    def test_10_backtest_sharpe_short_gap_stop_and_completed_bar_cache(self) -> None:
        # Build identical daily equity trajectory sampled at 1day vs 5minute
        daily_curve = []
        five_min_curve = []
        eq = 100000.0
        daily_changes = [0.008, -0.003, 0.011, -0.002, 0.009, 0.005, -0.004, 0.007, 0.006, 0.003]
        start_day = date(2026, 7, 6)
        for i, chg in enumerate(daily_changes):
            d_str = (start_day + timedelta(days=i)).isoformat()
            prev_eq = eq
            eq = eq * (1.0 + chg)
            daily_curve.append({"date": d_str, "equity": eq})
            # 10 intraday bars ending at exact same EOD equity
            for b in range(10):
                frac = (b + 1) / 10.0
                b_eq = prev_eq + (eq - prev_eq) * frac
                hh, mm = divmod(9 * 60 + 15 + b * 5, 60)
                five_min_curve.append({"date": f"{d_str} {hh:02d}:{mm:02d}", "equity": b_eq})

        s_day = compute_sharpe_metrics(daily_curve, initial_capital=100000.0, timeframe="1day", exchange="NSE")
        s_5m = compute_sharpe_metrics(five_min_curve, initial_capital=100000.0, timeframe="5minute", exchange="NSE")
        self.assertAlmostEqual(s_day["sharpe_ratio"], s_5m["sharpe_ratio"], places=2)
        self.assertEqual(s_5m["sharpe_method"], "daily_resampled_sqrt252")

        # Short gap-through-stop fills at bar_open (not optimistic sl_px)
        bars = []
        d0 = date(2026, 7, 1)
        trading_days: List[date] = []
        cur = d0
        while len(trading_days) < 20:
            if cur.weekday() < 5 and not is_exchange_holiday(cur, "NSE"):
                trading_days.append(cur)
            cur += timedelta(days=1)
        for idx, td in enumerate(trading_days):
            if idx == 7:
                # Gap UP past 2% SL (102.0) -> opens at 108.0! Short stop must fill at 108.0, not 102.0
                o, h, l, c = 108.0, 109.0, 107.0, 108.5
            elif idx > 7:
                o, h, l, c = 108.5, 109.0, 108.0, 108.5
            else:
                o, h, l, c = 100.0, 101.0, 99.0, 100.0
            bars.append(
                {
                    "ts_ist": f"{td.isoformat()}T09:15:00+05:30",
                    "trade_date": td.isoformat(),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": 10000,
                }
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            hist_mgr = HistoricalDataManager(db_path=Path(tmpdir) / "bt.db")
            engine = BacktestEngine(hist_manager=hist_mgr)
            with patch.object(engine, "_resolve_strategy", return_value=(_ShortSignalStrategy(), "python")):
                res = engine.run(
                    symbol="TEST",
                    exchange="NSE",
                    strategy="test_short_gap",
                    side="SELL",
                    timeframe="1day",
                    from_date=trading_days[0].isoformat(),
                    to_date=trading_days[-1].isoformat(),
                    initial_capital=100000.0,
                    stop_loss_pct=2.0,
                    take_profit_pct=10.0,
                    slippage_bps=0.0,
                    fee_bps=0.0,
                    charges_profile="zero",
                    fetcher=lambda *_a, **_kw: list(bars),
                )
        self.assertEqual(len(res["trades"]), 1)
        tr = res["trades"][0]
        self.assertEqual(tr["side"], "SHORT")
        self.assertAlmostEqual(tr["entry_price"], 100.0)
        self.assertGreaterEqual(tr["exit_price"], 108.0)
        self.assertIn("STOP_LOSS", tr["exit_reason"])

    # ── Test 11: Monthly 1-Minute Parquet Partitioning & Immutability ─────
    def test_11_monthly_one_minute_parquet_partitioning_and_prior_month_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            pio = _JsonParquetIO()
            mgr = HistoricalDataManager(
                db_path=tmp / "ohlcv.db",
                parquet_dir=tmp / "parquet",
                parquet_io=pio,
            )

            def minute_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                rows = []
                cur = c_start
                while cur <= c_end:
                    if cur.weekday() < 5 and not is_exchange_holiday(cur, exch):
                        for m in range(10):  # 09:15..09:24 -> 2 complete 5m bars per day
                            hh, mm = divmod(9 * 60 + 15 + m, 60)
                            px = 1000.0 + m
                            rows.append(
                                {
                                    "ts_ist": f"{cur.isoformat()}T{hh:02d}:{mm:02d}:00+05:30",
                                    "trade_date": cur.isoformat(),
                                    "open": px,
                                    "high": px + 2.0,
                                    "low": px - 1.0,
                                    "close": px + 1.0,
                                    "volume": 100,
                                    "oi": 0,
                                }
                            )
                    cur += timedelta(days=1)
                return rows

            # 1) Download Jan 2026 (2026-01-05..2026-01-07)
            mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-01-05",
                to_date="2026-01-07",
                provider="upstox",
                fetcher=minute_fetcher,
            )
            jan_file = tmp / "parquet" / "upstox" / "raw" / "NSE" / "1minute" / "RELIANCE" / "2026-01.parquet"
            self.assertTrue(jan_file.exists(), f"Expected monthly parquet at {jan_file}")
            jan_mtime_before = jan_file.stat().st_mtime_ns
            writes_before = len(pio.write_log)

            # Small sleep so any rewrite would observably advance mtime
            time.sleep(0.02)

            # 2) Download Feb 2026 (2026-02-02..2026-02-04)
            mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-02-02",
                to_date="2026-02-04",
                provider="upstox",
                fetcher=minute_fetcher,
            )
            feb_file = tmp / "parquet" / "upstox" / "raw" / "NSE" / "1minute" / "RELIANCE" / "2026-02.parquet"
            self.assertTrue(feb_file.exists(), f"Expected monthly parquet at {feb_file}")

            # Jan file MUST NOT have been rewritten when adding Feb data
            self.assertEqual(jan_file.stat().st_mtime_ns, jan_mtime_before)
            new_writes = pio.write_log[writes_before:]
            self.assertEqual(new_writes, [feb_file])

            # Multi-month read + 5minute resampling across Jan & Feb
            full_1m = mgr.get_records(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-01-05",
                to_date="2026-02-04",
                view="full",
            )
            self.assertEqual(full_1m["total_rows"], 60)  # 6 days * 10 mins
            resampled_5m = mgr.get_records(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="5minute",
                from_date="2026-01-05",
                to_date="2026-02-04",
                view="full",
            )
            self.assertEqual(resampled_5m["total_rows"], 12)  # 6 days * 2 five-min bars

    # ── Test 12: Query-First SQLite `asset_data_history` Catalog ──────────
    def test_12_query_first_sqlite_asset_data_history_and_missing_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_path = tmp / "ohlcv.db"
            pio = _JsonParquetIO()
            mgr = HistoricalDataManager(
                db_path=db_path,
                parquet_dir=tmp / "parquet",
                parquet_io=pio,
            )

            outage_day = date(2026, 1, 28)  # Non-holiday Wednesday
            holiday_day = date(2026, 1, 26)  # Verified NSE Republic Day holiday
            simulate_outage = {"active": True}
            api_calls: List[tuple[date, date]] = []

            def fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                api_calls.append((c_start, c_end))
                rows = []
                cur = c_start
                while cur <= c_end:
                    if cur.weekday() < 5 and not is_exchange_holiday(cur, exch):
                        if simulate_outage["active"] and cur == outage_day:
                            cur += timedelta(days=1)
                            continue
                        rows.append(
                            {
                                "ts_ist": f"{cur.isoformat()}T09:15:00+05:30",
                                "trade_date": cur.isoformat(),
                                "open": 500.0,
                                "high": 510.0,
                                "low": 495.0,
                                "close": 505.0,
                                "volume": 5000,
                                "oi": 0,
                            }
                        )
                    cur += timedelta(days=1)
                return rows

            # 1) Initial download across Jan 22 .. Jan 30 (Jan 26 Republic Day splits into 2 holiday-free chunks!)
            res1 = mgr.download(
                symbol="SBIN",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-01-22",
                to_date="2026-01-30",
                provider="upstox",
                fetcher=fetcher,
            )
            self.assertEqual(res1["api_calls_made"], 2)

            # Inspect SQLite `asset_data_history` directly
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT timeframe, start_date, end_date, missing_dates_json FROM asset_data_history WHERE symbol = 'SBIN'"
                ).fetchone()
            self.assertIsNotNone(row)
            tf, s_date, e_date, missing_json = row
            missing_list = json.loads(missing_json)
            self.assertEqual(tf, "1minute")
            self.assertEqual(s_date, "2026-01-22")
            self.assertEqual(e_date, "2026-01-30")
            # Non-holiday outage day MUST be in missing_dates; verified holiday MUST NOT be in missing_dates
            self.assertIn(outage_day.isoformat(), missing_list)
            self.assertNotIn(holiday_day.isoformat(), missing_list)

            # 2) Subsequent covered sub-range request (Jan 22 .. Jan 27) -> hits SQLite catalog FIRST -> 0 API calls!
            api_calls.clear()
            res2 = mgr.download(
                symbol="SBIN",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-01-22",
                to_date="2026-01-27",
                provider="upstox",
                fetcher=fetcher,
            )
            self.assertEqual(res2["api_calls_made"], 0)
            self.assertTrue(res2["cache_hit_only"])
            self.assertTrue(res2.get("catalog_hit"))
            self.assertEqual(api_calls, [])

            # 3) Provider recovers -> requesting Jan 22 .. Jan 30 fetches ONLY the missing outage day (Jan 28)
            simulate_outage["active"] = False
            api_calls.clear()
            res3 = mgr.download(
                symbol="SBIN",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-01-22",
                to_date="2026-01-30",
                provider="upstox",
                fetcher=fetcher,
            )
            self.assertEqual(api_calls, [(outage_day, outage_day)])
            self.assertEqual(res3["api_calls_made"], 1)

            hist = mgr.get_asset_history(symbol="SBIN", exchange="NSE", timeframe="1minute")
            self.assertIsNotNone(hist)
            self.assertEqual(hist["missing_dates"], [])

            # 4) Re-request full Jan 22 .. Jan 30 now -> strictly 0 API calls (catalog hit)
            api_calls.clear()
            res4 = mgr.download(
                symbol="SBIN",
                exchange="NSE",
                timeframe="1minute",
                from_date="2026-01-22",
                to_date="2026-01-30",
                provider="upstox",
                fetcher=fetcher,
            )
            self.assertEqual(res4["api_calls_made"], 0)
            self.assertTrue(res4.get("catalog_hit"))

    # ── Test 13: Broker get_funds HTTP 403 Never Recurses & Clean GET Headers ──
    def test_13_broker_funds_http_403_no_recursion_and_clean_get_headers(self) -> None:
        upstox = UpstoxBroker(access_token="dummy-upstox-tok", initial_balance=250000.0)
        upstox._connected = True
        seen_upstox_reqs: List[Any] = []

        def fake_upstox_403(req, timeout=4.0):
            seen_upstox_reqs.append(req)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=fake_upstox_403):
            funds = upstox.get_funds(segment="equity")

        # Must make strictly 1 HTTP call (no get_funds -> super().get_funds -> get_balance -> get_funds recursion)
        self.assertEqual(len(seen_upstox_reqs), 1)
        self.assertEqual(funds["available"], 250000.0)
        hdrs = {k.lower(): v for k, v in seen_upstox_reqs[0].header_items()}
        self.assertIn("user-agent", hdrs)
        self.assertNotIn("content-type", hdrs)

        # Verify ZerodhaBroker.get_funds also makes strictly 1 call on HTTP error (no recursion)
        zerodha = ZerodhaBroker(access_token="dummy-kite-tok", initial_balance=180000.0)
        zerodha._connected = True
        zerodha._kite = None
        seen_kite_reqs: List[Any] = []

        def fake_kite_403(req, timeout=4.0):
            seen_kite_reqs.append(req)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=fake_kite_403):
            k_funds = zerodha.get_funds(segment="equity")

        self.assertEqual(len(seen_kite_reqs), 1)
        self.assertEqual(k_funds["available"], 180000.0)

    # ── Test 14 (C-01): Duplicate Exits Eliminated While Exit Order OPEN ──
    def test_14_c01_duplicate_exit_eliminated_while_exit_order_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            broker = _StubBroker()
            broker.mode = "open"  # Broker acknowledges order as OPEN (not yet filled)

            mgr = DeploymentManager(
                db_path=tmp / "dep.db",
                oms=oms,
                kill_switch=ks,
                risk_mgr=risk,
                audit=audit,
                broker_resolver=lambda _bid: broker,
                require_live_env_lock=False,
            )
            with patch.object(mgr, "_refresh_deploy_ws_scope", return_value=None):
                dep = mgr.deploy(
                    name="Duplicate Exit Guard",
                    exec_mode="live_auto",
                    strategy_id="ema_crossover",
                    symbols=["RELIANCE"],
                    exchange="NSE",
                    timeframe="1day",
                    initial_capital=100000.0,
                    stop_loss_pct=2.0,
                    start_worker=False,
                )
            did = dep["deployment"]["id"]
            # Inject an existing live LONG position with stop-loss at 980
            with mgr._lock:
                rt = mgr._runtime[did]
                rt["status"] = "RUNNING"
                rt["positions"]["RELIANCE"] = {
                    "qty": 10,
                    "entry_price": 1000.0,
                    "entry_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
                    "last_price": 1000.0,
                    "highest_price": 1000.0,
                    "entry_charges": 0.0,
                }

            now_iso = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            # Fire 5 consecutive ticks below stop-loss while broker exit order stays OPEN
            for _ in range(5):
                mgr.process_ticks(
                    did,
                    {"RELIANCE": {"ltp": 970.0, "volume": 1000, "ts_ist": now_iso}},
                )

            sell_calls = [c for c in broker.place_calls if c.side.upper() == "SELL"]
            self.assertEqual(len(sell_calls), 1, f"Expected strictly 1 SELL exit, got {len(sell_calls)}")
            self.assertTrue(oms.has_active_order(did, "RELIANCE"))

    # ── Test 15 (C-02): Ambiguous Network Failure -> SUBMITTED + Tag Recovery ─
    def test_15_c02_ambiguous_network_failure_and_tag_recovery(self) -> None:
        # 1) UpstoxBroker: TimeoutError raises AmbiguousSubmissionError; HTTP 400 returns REJECTED
        ub = UpstoxBroker(access_token="tok-u")
        ub._connected = True
        ub._sdk = None
        ub._order_api = None

        with patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
            with self.assertRaises(AmbiguousSubmissionError):
                ub.place_order(
                    OrderRequest(
                        symbol="RELIANCE",
                        exchange="NSE",
                        side="BUY",
                        quantity=2,
                        client_order_id="cid-upx-ambig-001",
                    )
                )

        def fake_http_400(req, timeout=8):
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"errors":[{"message":"Invalid quantity"}]}'),
            )

        with patch("urllib.request.urlopen", side_effect=fake_http_400):
            rej_u = ub.place_order(
                OrderRequest(symbol="RELIANCE", exchange="NSE", side="BUY", quantity=2, client_order_id="cid-upx-400")
            )
        self.assertEqual(rej_u.status, OrderStatus.REJECTED)
        self.assertIn("Invalid quantity", rej_u.rejection_reason)

        # 2) ZerodhaBroker: TimeoutError raises AmbiguousSubmissionError; explicit 4xx returns REJECTED
        zb = ZerodhaBroker(access_token="tok-z")
        zb._connected = True
        zb._kite = None
        with patch("urllib.request.urlopen", side_effect=TimeoutError("socket timeout")):
            with self.assertRaises(AmbiguousSubmissionError):
                zb.place_order(
                    OrderRequest(symbol="TCS", exchange="NSE", side="BUY", quantity=1, client_order_id="cid-z-ambig-001")
                )

        with patch("urllib.request.urlopen", side_effect=fake_http_400):
            rej_z = zb.place_order(
                OrderRequest(symbol="TCS", exchange="NSE", side="BUY", quantity=1, client_order_id="cid-z-400")
            )
        self.assertEqual(rej_z.status, OrderStatus.REJECTED)

        # 3) OMS handles AmbiguousSubmissionError -> SUBMITTED (uncertain), then recovers via broker tag!
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            sent_alerts: List[str] = []
            notifier = TelegramNotifier(
                settings_file=tmp / "tg.json",
                sender=lambda _tok, _cid, txt: sent_alerts.append(txt) or {"ok": True},
            )
            notifier.save_settings({"enabled": True, "chat_id": "999999"})
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit, notifier=notifier)

            class _AmbiguousTagBroker(_StubBroker):
                def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
                    self.place_calls.append(order)
                    tag = make_broker_tag(order.client_order_id)
                    # Broker actually executed the order under the shortened 20-char tag,
                    # but dropped the HTTP response!
                    self.broker_orders.append(
                        BrokerOrder(
                            order_id="BRK-TAG-777",
                            request=OrderRequest(
                                symbol=order.symbol,
                                exchange=order.exchange,
                                side=order.side,
                                quantity=order.quantity,
                                price=order.price,
                                client_order_id=tag,
                            ),
                            status=OrderStatus.FILLED,
                            filled_quantity=order.quantity,
                            average_fill_price=1505.0,
                            raw={"tag": tag},
                        )
                    )
                    raise AmbiguousSubmissionError("Gateway 504 after dispatch")

            ambig_broker = _AmbiguousTagBroker()
            cid = "dep-long-client-order-id-20261124-0001"
            req = OrderRequest(
                symbol="INFY",
                exchange="NSE",
                side="BUY",
                quantity=4,
                order_type="MARKET",
                product="MIS",
                price=1500.0,
                client_order_id=cid,
            )
            rec, delta, _ = oms.submit_order(req, ambig_broker, deployment_id="dep-c02", exec_mode="live_auto")
            self.assertEqual(rec["status"], OrderStatus.SUBMITTED.value)
            self.assertNotEqual(rec["status"], OrderStatus.REJECTED.value)
            self.assertEqual(delta, 0)
            self.assertTrue(any("ORDER_UNCERTAIN" in m for m in sent_alerts))

            # Reconcile recovers the filled order using make_broker_tag matching
            rec_res = oms.reconcile_orders(ambig_broker, deployment_id="dep-c02")
            self.assertEqual(len(rec_res["fills_applied"]), 1)
            self.assertEqual(rec_res["fills_applied"][0]["delta_qty"], 4)
            stored = oms.get_order_by_client_id(cid)
            self.assertEqual(stored["status"], OrderStatus.FILLED.value)
            self.assertEqual(stored["broker_order_id"], "BRK-TAG-777")
            self.assertAlmostEqual(stored["average_fill_price"], 1505.0)

    # ── Test 16 (C-03): Risk Entry vs Exit Split & Fail-Closed Kill Switch ──
    def test_16_c03_risk_entry_vs_exit_split_and_fail_closed_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            ks_file = tmp / "ks.json"
            ks = KillSwitchController(state_file=ks_file, audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            risk.save_limits({"total_capital_inr": 100000.0, "max_capital_per_trade_pct": 15.0})

            # Engage kill switch AND test notional (₹50,000) way above 15% cap (₹15,000)
            ks.engage(reason="Emergency test halt")
            entry_chk = risk.validate_order(symbol="RELIANCE", quantity=20, price=2500.0, transaction_type="BUY")
            self.assertFalse(entry_chk["allowed"])

            exit_chk = risk.validate_order(
                symbol="RELIANCE",
                quantity=20,
                price=2500.0,
                transaction_type="SELL",
                open_position_qty=20,
            )
            self.assertTrue(exit_chk["allowed"], f"Exit must never be blocked by kill switch/cap: {exit_chk}")

            # Exit quantity > open position must be blocked to prevent flipping short
            flip_chk = risk.check_exit(
                symbol="RELIANCE",
                quantity=25,
                price=2500.0,
                transaction_type="SELL",
                open_position_qty=20,
            )
            self.assertFalse(flip_chk["allowed"])
            self.assertTrue(any("flip" in r.lower() for r in flip_chk["reasons"]))

            # Corrupted kill-switch JSON file MUST fail closed (engaged=True, corrupted=True)
            ks.reset()
            self.assertFalse(ks.is_engaged())
            ks_file.write_text("{corrupt json not parseable", encoding="utf-8")
            st = ks.status()
            self.assertTrue(st["engaged"])
            self.assertTrue(st.get("corrupted"))
            self.assertTrue(ks.is_engaged())

    # ── Test 17 (C-05/C-06): Auth-by-Default, Bootstrap Code, Lockout & AST ──
    def test_17_c05_c06_auth_by_default_bootstrap_secret_rate_limit_and_plugin_ast(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sec = SecurityManager(
                auth_file=tmp / ".admin_auth.json",
                bootstrap_file=tmp / ".bootstrap_code",
            )
            env_clean = {k: v for k, v in os.environ.items() if k not in {"ALGODESK_REQUIRE_AUTH", "ALGODESK_API_TOKEN"}}
            with patch.dict(os.environ, env_clean, clear=True):
                # Auth is enforced by default even before passcode claim
                self.assertTrue(sec.is_auth_enforced())
                self.assertTrue(sec.is_protected_request("GET", "/api/overview"))
                self.assertTrue(sec.is_protected_request("POST", "/api/streak/deployments/reconcile"))
                self.assertFalse(sec.is_protected_request("GET", "/api/health"))
                self.assertFalse(sec.is_protected_request("GET", "/api/security/status"))
                self.assertFalse(sec.is_protected_request("POST", "/api/security/login"))
                self.assertFalse(sec.is_protected_request("POST", "/api/security/claim"))

                # Bootstrap secret created with 0o600 and required to claim passcode
                code = sec.ensure_bootstrap_code()
                self.assertIsNotNone(code)
                self.assertTrue((tmp / ".bootstrap_code").exists())

                with self.assertRaises(PermissionError):
                    sec.claim_passcode("strong-pass-2026", bootstrap_code="WRONG-CODE", client_key="10.0.0.1")

                claimed = sec.claim_passcode("strong-pass-2026", bootstrap_code=code, client_key="10.0.0.1")
                self.assertTrue(claimed["ok"])
                self.assertFalse((tmp / ".bootstrap_code").exists())

                # 5 failed logins trigger sliding-window RateLimitError (HTTP 429)
                for _ in range(5):
                    with self.assertRaises(PermissionError):
                        sec.login("wrong-pass-attempt", client_key="10.0.0.2")
                with self.assertRaises(RateLimitError):
                    sec.login("strong-pass-2026", client_key="10.0.0.2")

                # Live HTTP check: GET /api/overview -> 401 without token; GET /api/health -> 200
                with patch("web.local_server.security_manager", sec):
                    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
                    port = srv.server_address[1]
                    th = threading.Thread(target=srv.serve_forever, daemon=True)
                    th.start()
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=4) as r:
                            self.assertEqual(r.status, 200)
                        with self.assertRaises(urllib.error.HTTPError) as ctx_401:
                            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=4)
                        self.assertEqual(ctx_401.exception.code, 401)
                    finally:
                        srv.shutdown()
                        srv.server_close()

        # Strengthened AST checks block getattr, allow_pickle, read_pickle, and dunder access
        for bad_snippet in [
            "x = getattr(obj, 'foo')",
            "import numpy as np\narr = np.load('a.npy', allow_pickle=True)",
            "import pandas as pd\ndf = pd.read_pickle('a.pkl')",
            "y = x.__dict__",
        ]:
            viols = validate_python_plugin_ast(bad_snippet, "audit_test.py")
            self.assertTrue(len(viols) > 0, f"Expected AST violation for: {bad_snippet}")

    # ── Test 18 (H-03): Position Reconciliation Halts Live Deployment ──────
    def test_18_h03_position_reconciliation_halts_live_deployment_on_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            sent_alerts: List[str] = []
            notifier = TelegramNotifier(
                settings_file=tmp / "tg.json",
                sender=lambda _tok, _cid, txt: sent_alerts.append(txt) or {"ok": True},
            )
            notifier.save_settings({"enabled": True, "chat_id": "888888"})

            broker = _StubBroker()
            # Broker actually holds 0 shares (e.g. manual square-off or missed fill), while AlgoDesk thinks +10
            broker.broker_positions = []

            mgr = DeploymentManager(
                db_path=tmp / "dep.db",
                oms=oms,
                kill_switch=ks,
                risk_mgr=risk,
                audit=audit,
                broker_resolver=lambda _bid: broker,
                require_live_env_lock=False,
            )
            with patch.object(mgr, "_refresh_deploy_ws_scope", return_value=None):
                dep = mgr.deploy(
                    name="Mismatch Halt Test",
                    exec_mode="live_auto",
                    strategy_id="ema_crossover",
                    symbols=["RELIANCE"],
                    exchange="NSE",
                    timeframe="1day",
                    initial_capital=100000.0,
                    start_worker=False,
                )
            did = dep["deployment"]["id"]
            with mgr._lock:
                mgr._runtime[did]["status"] = "RUNNING"
                mgr._runtime[did]["positions"]["RELIANCE"] = {
                    "qty": 10,
                    "entry_price": 2500.0,
                    "entry_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
                    "last_price": 2500.0,
                    "highest_price": 2500.0,
                    "entry_charges": 0.0,
                }

            rec = mgr.reconcile_positions(broker_adapter=broker, notifier=notifier)
            self.assertTrue(rec["ok"])
            self.assertEqual(len(rec["mismatches"]), 1)
            self.assertIn(did, rec["halted_deployments"])
            self.assertEqual(mgr.get_deployment(did)["deployment"]["status"], "PAUSED")
            self.assertTrue(any("POSITION_MISMATCH" in m for m in sent_alerts))
            self.assertTrue(audit.verify_chain()["valid"])

    # ── Test 19 (H-04): OMS Monotonic State Machine & Trade Price Attribution ─
    def test_19_h04_oms_state_machine_monotonic_and_trade_price_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)

            class _TradePriceBroker(_StubBroker):
                def __init__(self) -> None:
                    super().__init__()
                    self.trades: List[ExecutionFill] = []

                def get_trades(self) -> List[ExecutionFill]:
                    return list(self.trades)

            broker = _TradePriceBroker()
            broker.mode = "partial_then_full"  # Initial fill: 30 @ 200.0
            req = OrderRequest(
                symbol="HDFCBANK",
                exchange="NSE",
                side="BUY",
                quantity=100,
                order_type="LIMIT",
                product="CNC",
                price=200.0,
                client_order_id="cid-trade-px-001",
            )
            rec, delta1, px1 = oms.submit_order(req, broker, deployment_id="dep-tr", exec_mode="live_auto")
            self.assertEqual(delta1, 30)
            self.assertAlmostEqual(px1, 200.0)
            self.assertAlmostEqual(oms.get_pending_exposure_inr(), 70 * 200.0)

            # Second tranche: 70 shares filled at 210.0 -> overall broker average is 207.0,
            # but per-fill trade record gives exact tranche price 210.0!
            oid = rec["broker_order_id"]
            broker.broker_orders[0].filled_quantity = 100
            broker.broker_orders[0].average_fill_price = 207.0
            broker.broker_orders[0].status = OrderStatus.FILLED
            broker.trades = [
                ExecutionFill(fill_id="T1", order_id=oid, symbol="HDFCBANK", exchange="NSE", side="BUY", quantity=30, price=200.0),
                ExecutionFill(fill_id="T2", order_id=oid, symbol="HDFCBANK", exchange="NSE", side="BUY", quantity=70, price=210.0),
            ]
            r2 = oms.reconcile_orders(broker, deployment_id="dep-tr")
            self.assertEqual(len(r2["fills_applied"]), 1)
            self.assertEqual(r2["fills_applied"][0]["delta_qty"], 70)
            self.assertAlmostEqual(r2["fills_applied"][0]["fill_price"], 210.0, places=2)

            # Stale broker snapshot reporting OPEN / 30 filled MUST NOT reopen terminal FILLED order
            broker.broker_orders[0].status = OrderStatus.OPEN
            broker.broker_orders[0].filled_quantity = 30
            r3 = oms.reconcile_orders(broker, deployment_id="dep-tr")
            self.assertEqual(r3["fills_applied"], [])
            stored = oms.get_order_by_client_id("cid-trade-px-001")
            self.assertEqual(stored["status"], OrderStatus.FILLED.value)
            self.assertEqual(stored["filled_quantity"], 100)

    # ── Test 20 (H-05): Stale Quotes Block New Entries ─────────────────────
    def test_20_h05_stale_quote_blocks_new_entries_with_one_shot_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            sent_alerts: List[str] = []
            notifier = TelegramNotifier(
                settings_file=tmp / "tg.json",
                sender=lambda _tok, _cid, txt: sent_alerts.append(txt) or {"ok": True},
            )
            notifier.save_settings({"enabled": True, "chat_id": "777777"})

            broker = _StubBroker()
            broker.mode = "open"
            mgr = DeploymentManager(
                db_path=tmp / "dep.db",
                oms=oms,
                kill_switch=ks,
                risk_mgr=risk,
                audit=audit,
                notifier=notifier,
                broker_resolver=lambda _bid: broker,
                require_live_env_lock=False,
            )
            with patch.object(mgr, "_refresh_deploy_ws_scope", return_value=None):
                dep = mgr.deploy(
                    name="Stale Quote Guard",
                    exec_mode="live_auto",
                    strategy_id="ema_crossover",
                    symbols=["RELIANCE"],
                    exchange="NSE",
                    timeframe="1day",
                    initial_capital=100000.0,
                    evaluation_mode="tick",
                    start_worker=False,
                )
            did = dep["deployment"]["id"]
            # Seed 6 historical bars so strategy evaluates immediately on incoming tick
            seed_bars = [
                {
                    "ts_ist": f"2026-11-1{i}T09:15:00+05:30",
                    "open": 2500.0,
                    "high": 2510.0,
                    "low": 2490.0,
                    "close": 2505.0,
                    "volume": 1000,
                }
                for i in range(1, 7)
            ]
            with mgr._lock:
                rt = mgr._runtime[did]
                rt["status"] = "RUNNING"
                rt["sym_state"]["RELIANCE"] = {
                    "base_bars": seed_bars,
                    "forming_bar": None,
                    "last_ltp": 2505.0,
                    "last_sig_key": "",
                    "last_eval_bar_ts": "",
                    "indicator_eval_count": 0,
                }

            class _AlwaysBuyStrategy(BacktestStrategy):
                id = "always_buy"
                name = "Always Buy"

                def prepare(self, candles, params, toolkit) -> None:
                    pass

                def signal_at(self, index: int, bar: Dict[str, Any]):
                    return (True, False)

            stale_ts = (datetime.now(tz=IST_TZ) - timedelta(seconds=120)).isoformat(timespec="seconds")
            with patch.object(mgr, "_build_strategy", return_value=(_AlwaysBuyStrategy(), "python")):
                r1 = mgr.process_ticks(did, {"RELIANCE": {"ltp": 2500.0, "volume": 100, "ts_ist": stale_ts}})
                mgr.process_ticks(did, {"RELIANCE": {"ltp": 2501.0, "volume": 100, "ts_ist": stale_ts}})

            # Entry blocked despite entry_sig=True; zero broker orders placed; one-shot STALE_QUOTE alert
            self.assertEqual(broker.place_calls, [])
            self.assertTrue(any(a.get("kind") == "blocked" for a in r1.get("actions", [])))
            stale_alerts = [m for m in sent_alerts if "STALE_QUOTE" in m]
            self.assertEqual(len(stale_alerts), 1)

    # ── Test 21 (H-06): Watchdog Degraded Mode Blocks New Entries ──────────
    def test_21_h06_watchdog_degraded_mode_after_consecutive_failures_blocks_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            sent_alerts: List[str] = []
            notifier = TelegramNotifier(
                settings_file=tmp / "tg.json",
                sender=lambda _tok, _cid, txt: sent_alerts.append(txt) or {"ok": True},
            )
            notifier.save_settings({"enabled": True, "chat_id": "666666"})

            def failing_provider():
                raise RuntimeError("Broker API unreachable")

            wd = RiskWatchdog(
                risk_mgr=risk,
                interval_sec=5.0,
                notifier=notifier,
                state_provider=failing_provider,
                degraded_threshold=3,
            )
            self.assertFalse(wd.is_degraded)
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    wd.run_once()
            self.assertTrue(wd.is_degraded)
            self.assertTrue(any("WATCHDOG_DEGRADED" in m for m in sent_alerts))

            chk = risk.check_entry(symbol="TCS", quantity=1, price=3500.0, watchdog_degraded=wd.is_degraded)
            self.assertFalse(chk["allowed"])
            self.assertTrue(any("degraded" in r.lower() for r in chk["reasons"]))

    # ── Test 22 (M-04): Warmup Failure Refuses RUNNING Status ──────────────
    def test_22_m04_warmup_failure_refuses_running_and_surfaces_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audit = AuditTrail(tmp / "audit.db")
            oms = OrderManagementSystem(db_path=tmp / "oms.db", audit=audit)
            ks = KillSwitchController(state_file=tmp / "ks.json", audit=audit)
            risk = RiskLimitManager(limits_file=tmp / "risk.json", kill_switch=ks, audit=audit)
            hist = HistoricalDataManager(db_path=tmp / "hist.db", parquet_dir=tmp / "pq")
            mgr = DeploymentManager(
                db_path=tmp / "dep.db",
                hist_manager=hist,
                oms=oms,
                kill_switch=ks,
                risk_mgr=risk,
                audit=audit,
                require_live_env_lock=False,
            )

            def broken_hist_fetcher(*_args, **_kwargs):
                raise ConnectionError("Historical upstream down")

            with patch.object(mgr, "_refresh_deploy_ws_scope", return_value=None):
                dep = mgr.deploy(
                    name="Warmup Fail Test",
                    exec_mode="paper",
                    strategy_id="ema_crossover",
                    symbols=["RELIANCE"],
                    exchange="NSE",
                    timeframe="1day",
                    initial_capital=100000.0,
                    start_worker=True,
                    hist_fetcher=broken_hist_fetcher,
                )
            did = dep["deployment"]["id"]
            # Wait briefly for warmup worker thread to transition deployment to ERROR
            deadline = time.time() + 4.0
            final_status = None
            while time.time() < deadline:
                snap = mgr.get_deployment(did)["deployment"]
                if snap["status"] == "ERROR":
                    final_status = snap
                    break
                time.sleep(0.05)

            self.assertIsNotNone(final_status, "Deployment with 100% warmup failure must transition to ERROR")
            self.assertEqual(final_status["status"], "ERROR")
            self.assertGreaterEqual(int(final_status.get("error_count") or 0), 1)
            self.assertTrue(bool(final_status.get("last_error")))

    # ── Test 23: Zero SL / TP / TSL Preserved (Never Replaced With Defaults) ─
    def test_23_zero_sl_tp_tsl_preserved_through_http_and_backtest(self) -> None:
        from http.server import ThreadingHTTPServer
        from web import local_server

        captured_kwargs: Dict[str, Any] = {}

        def _fake_streak_bt(**kwargs):
            captured_kwargs.update(kwargs)
            return {
                "ok": True,
                "run_id": "bt-zero-test",
                "deploy_spec": {
                    "stop_loss_pct": kwargs["stop_loss_pct"],
                    "take_profit_pct": kwargs["take_profit_pct"],
                    "trailing_sl_pct": kwargs["trailing_sl_pct"],
                },
                "trades": [],
            }

        server = ThreadingHTTPServer(("127.0.0.1", 0), local_server._StdlibBrokerHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with patch.object(local_server.hub_service, "run_streak_backtest", side_effect=_fake_streak_bt), \
                 patch.object(local_server._StdlibBrokerHandler, "_enforce_auth", return_value=True):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/streak/backtest",
                    data=json.dumps(
                        {
                            "mode": "portfolio",
                            "symbols": ["RELIANCE"],
                            "strategy": "ema_crossover",
                            "timeframe": "1day",
                            "stop_loss_pct": 0,
                            "take_profit_pct": 0,
                            "trailing_sl_pct": 0,
                            "slippage_bps": 0,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(body.get("ok"))
        self.assertEqual(captured_kwargs["stop_loss_pct"], 0.0)
        self.assertEqual(captured_kwargs["take_profit_pct"], 0.0)
        self.assertEqual(captured_kwargs["trailing_sl_pct"], 0.0)
        self.assertEqual(captured_kwargs["slippage_bps"], 0.0)

        # Verify actual backtest engine produces zero SL/TP/TSL exits even on +/-25% swings
        from backtest.engine import BacktestEngine

        # Build 70 bars with big swings that would definitely trip 3% SL or 7% TP
        prices = [100.0] * 30 + [102.0, 105.0, 110.0, 128.0, 88.0, 130.0, 85.0, 125.0, 120.0, 115.0] + [90.0] * 30
        candles = []
        d0 = date(2025, 1, 1)
        for i, c in enumerate(prices):
            day = (d0 + timedelta(days=i)).isoformat()
            candles.append(
                {
                    "trade_date": day,
                    "ts_ist": f"{day}T09:15:00+05:30",
                    "open": c,
                    "high": c * 1.05,
                    "low": c * 0.95,
                    "close": c,
                    "volume": 100000,
                }
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            hist = HistoricalDataManager(db_path=tmp / "hist.db", parquet_dir=tmp / "pq")
            eng = BacktestEngine(hist_manager=hist)
            res = eng.run(
                symbol="TESTZERO",
                exchange="NSE",
                strategy="ema_crossover",
                params={"fast_period": 5, "slow_period": 15},
                timeframe="1day",
                from_date="2025-01-01",
                to_date="2025-03-31",
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                trailing_sl_pct=0.0,
                fetcher=lambda *a, **k: candles,
            )
            self.assertGreaterEqual(len(res["trades"]), 1)
            for tr in res["trades"]:
                reason = str(tr.get("exit_reason") or "").upper()
                self.assertNotIn("STOP_LOSS", reason)
                self.assertNotIn("TAKE_PROFIT", reason)
                self.assertNotIn("TRAILING_SL", reason)

    # ── Test 24: SPA Bundle Staleness Detection & Fallback Auth Modal ────
    def test_24_spa_bundle_staleness_triggers_rebuild_and_fallback_auth(self) -> None:
        import os
        from types import SimpleNamespace
        import web.spa as spa_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"name":"test"}', encoding="utf-8")
            src_dir = root / "src"
            src_dir.mkdir()
            app_tsx = src_dir / "App.tsx"
            app_tsx.write_text("export default function App() { return null; }\n", encoding="utf-8")

            dist_dir = root / "dist"
            assets_dir = dist_dir / "assets"
            assets_dir.mkdir(parents=True)
            (assets_dir / "index-abc.js").write_text("console.log('ok');", encoding="utf-8")
            dist_index = dist_dir / "index.html"
            dist_index.write_text(
                '<!doctype html><html><body><script src="/assets/index-abc.js"></script></body></html>',
                encoding="utf-8",
            )
            (root / "node_modules").mkdir()

            # Make dist/index.html older than src/App.tsx by 60 seconds
            now = time.time()
            os.utime(dist_index, (now - 60, now - 60))
            os.utime(app_tsx, (now, now))

            self.assertTrue(spa_mod.is_complete_spa_bundle(dist_dir))
            self.assertFalse(spa_mod.is_spa_bundle_fresh(dist_dir, repo_root=root))

            build_calls = []

            def fake_run(cmd, cwd=None, check=False, timeout=None):
                build_calls.append(cmd)
                # Simulate npm run build touching dist/index.html to current mtime
                os.utime(dist_index, (now + 5, now + 5))
                return SimpleNamespace(returncode=0)

            with (
                patch.object(spa_mod, "REPO_ROOT", root),
                patch.object(spa_mod, "DIST_DIR", dist_dir),
                patch.object(spa_mod, "STATIC_SPA_DIR", root / "web" / "static_spa"),
                patch("web.spa.shutil.which", return_value="/usr/bin/npm"),
                patch("web.spa.subprocess.run", side_effect=fake_run),
            ):
                resolved = spa_mod.ensure_spa_bundle(auto_build=True)
                self.assertEqual(resolved, dist_dir)
                self.assertEqual(len(build_calls), 1)
                self.assertEqual(build_calls[0][-2:], ["run", "build"])
                self.assertTrue(spa_mod.is_spa_bundle_fresh(dist_dir, repo_root=root))

                # Second call must be a fast no-op without rebuilding
                resolved2 = spa_mod.ensure_spa_bundle(auto_build=True)
                self.assertEqual(resolved2, dist_dir)
                self.assertEqual(len(build_calls), 1)

        # Verify fallback template has inline operator auth modal & login endpoints
        fallback_html = (Path(__file__).resolve().parent.parent / "web" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("operatorAuthBackdrop", fallback_html)
        self.assertIn("/api/security/login", fallback_html)
        self.assertIn("/api/security/claim", fallback_html)
        self.assertNotIn("localStorage", fallback_html.split("installFallbackAuth")[1].split("const state =")[0])

    # ── Test 25: Backtest Chart Endpoint Returns Strategy Indicators ──────
    def test_25_backtest_chart_series_returns_strategy_indicators(self) -> None:
        from backtest.portfolio import PortfolioBacktestEngine

        # Build 75 synthetic daily candles
        candles = []
        d0 = date(2025, 1, 1)
        for i in range(75):
            day = (d0 + timedelta(days=i)).isoformat()
            px = 100.0 + (i * 0.6) + (8.0 if 25 <= i <= 45 else -4.0 if i > 55 else 0.0)
            candles.append(
                {
                    "trade_date": day,
                    "ts_ist": f"{day}T09:15:00+05:30",
                    "open": px - 0.5,
                    "high": px + 2.0,
                    "low": px - 2.0,
                    "close": px,
                    "volume": 100000 + i * 500,
                }
            )

        rule_def = {
            "id": "sma_ema_rsi_test",
            "name": "SMA20/50 + RSI14 / EMA20 Exit",
            "side": "BUY",
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {"type": "sma", "period": 20},
                        "operator": "crosses_above",
                        "right": {"type": "sma", "period": 50},
                    },
                    {
                        "left": {"type": "rsi", "period": 14},
                        "operator": ">",
                        "right": {"type": "number", "value": 45},
                    },
                ],
            },
            "exit": {
                "logic": "OR",
                "conditions": [
                    {
                        "left": {"type": "close"},
                        "operator": "crosses_below",
                        "right": {"type": "ema", "period": 20},
                    }
                ],
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            hist = HistoricalDataManager(db_path=tmp / "hist.db", parquet_dir=tmp / "pq")
            single = BacktestEngine(hist_manager=hist)
            port = PortfolioBacktestEngine(
                single_engine=single,
                hist_manager=hist,
                runs_file=tmp / "runs.json",
            )
            res = port.run(
                symbols=["TESTSTRAT"],
                exchange="NSE",
                strategy="visual_rule",
                rule_def=rule_def,
                mode="individual",
                timeframe="1day",
                from_date="2025-01-01",
                to_date="2025-04-30",
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                trailing_sl_pct=0.0,
                fetcher=lambda *a, **k: candles,
            )
            run_id = res["run_id"]
            chart_payload = port.get_chart_series(run_id=run_id, symbol="TESTSTRAT")
            self.assertTrue(chart_payload["ok"])
            self.assertEqual(chart_payload["symbol"], "TESTSTRAT")
            self.assertEqual(len(chart_payload["bars"]), 75)

            ind_map = {ind["id"]: ind for ind in chart_payload["indicators"]}
            # Must contain strategy's actual indicators, NOT raw CLOSE copy
            self.assertIn("SMA(20)", ind_map)
            self.assertIn("SMA(50)", ind_map)
            self.assertIn("EMA(20)", ind_map)
            self.assertIn("RSI(14)", ind_map)
            self.assertNotIn("CLOSE", ind_map)
            self.assertNotIn("EMA(50)", ind_map)

            # Verify pane classification and alignment
            self.assertEqual(ind_map["SMA(20)"]["pane"], "overlay")
            self.assertEqual(ind_map["SMA(50)"]["pane"], "overlay")
            self.assertEqual(ind_map["EMA(20)"]["pane"], "overlay")
            self.assertEqual(ind_map["RSI(14)"]["pane"], "oscillator")
            self.assertEqual(ind_map["RSI(14)"]["levels"], [30.0, 70.0])
            for ind in chart_payload["indicators"]:
                self.assertEqual(len(ind["values"]), len(chart_payload["bars"]))

            # Verify HTTP route GET /api/streak/backtest/{run_id}/chart
            from web.local_server import _StdlibBrokerHandler, hub_service

            server = ThreadingHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
            srv_port = server.server_address[1]
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                with (
                    patch.object(_StdlibBrokerHandler, "_enforce_auth", return_value=True),
                    patch.object(hub_service, "get_backtest_chart_series", side_effect=port.get_chart_series),
                ):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{srv_port}/api/streak/backtest/{run_id}/chart?symbol=TESTSTRAT",
                        timeout=5,
                    ) as resp:
                        http_body = json.loads(resp.read().decode("utf-8"))
                        self.assertEqual(resp.status, 200)
                        self.assertTrue(http_body["ok"])
                        self.assertIn("SMA(20)", http_body["indicator_columns"])
                        self.assertIn("RSI(14)", http_body["indicator_columns"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()



