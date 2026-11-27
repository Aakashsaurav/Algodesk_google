"""
tests/test_upstox_analytics_and_idle_polling.py
===============================================
Verifies:
1. Idle Upstox API polling fix:
   - OMS.reconcile_orders() makes ZERO broker REST calls when no active orders exist.
   - RiskWatchdog enters zero-API idle_standby when is_active_callback() is False,
     and immediately resumes polling when live trading activity becomes True.
2. Upstox 1-Year Read-Only Analytics Token (UPSTOX_ANALYTICS_TOKEN):
   - AuthManager saves, resolves, prioritizes for data connections, and clears cleanly.
   - Daily OAuth (get_valid_token) stays strictly required for trading / order APIs.
   - Historical candles, live quotes, WebSocket authorize, and health_check all use
     the Analytics Token when configured.
   - Preflight passes Data Feed with Analytics Token while still blocking live Trading
     without daily OAuth.
   - Credential summary masks the Analytics Token and never returns plaintext secrets.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from broker.base import AccountBalance, BrokerBase, BrokerCapabilities, BrokerHealthSnapshot, ConnectionState
from broker.upstox.auth import AuthManager
from broker.upstox.broker import UpstoxBroker
from core.broker_ws import UpstoxWSFeedClient
from core.feed_manager import _fetch_live_quotes_upstox
from core.historical import HistoricalDataManager
from core.preflight import PreflightChecker
from execution.oms import OrderManagementSystem
from risk.limits import RiskLimitManager, RiskWatchdog
from web.service import BrokerHubService


class _FakeHTTPResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _CountingBroker(BrokerBase):
    name = "upstox"
    display_name = "Upstox"
    stage = "active"
    capabilities = BrokerCapabilities(
        supports_trading=True,
        supports_market_data=True,
        supports_websocket=True,
        requires_oauth=True,
    )

    def __init__(self) -> None:
        self.calls: list[str] = []

    def health_check(self, timeout: float = 3.5) -> BrokerHealthSnapshot:
        self.calls.append("health_check")
        return BrokerHealthSnapshot(
            broker_id="upstox",
            display_name="Upstox",
            stage="active",
            state=ConnectionState.CONNECTED,
            capabilities=self.capabilities,
        )

    def connect(self) -> bool:
        return True

    def disconnect(self) -> bool:
        return True

    def place_order(self, order):
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        self.calls.append("get_positions")
        return []

    def get_balance(self) -> AccountBalance:
        return AccountBalance(cash=100000.0)

    def get_funds(self, segment: str = "equity"):
        self.calls.append("get_funds")
        return {"available_margin": 100000.0, "used_margin": 0.0}

    def get_orders(self):
        self.calls.append("get_orders")
        return []

    def get_trades(self):
        self.calls.append("get_trades")
        return []


class TestIdleUpstoxPollingFix(unittest.TestCase):
    def test_oms_reconcile_orders_skips_broker_calls_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            oms = OrderManagementSystem(db_path=Path(tmpdir) / "oms.db")
            broker = _CountingBroker()
            res = oms.reconcile_orders(broker)
            self.assertTrue(res["ok"])
            self.assertTrue(res.get("skipped_idle"))
            self.assertEqual(res["checked"], 0)
            self.assertEqual(broker.calls, [])

    def test_risk_watchdog_idle_standby_makes_zero_broker_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            risk_mgr = RiskLimitManager(limits_file=Path(tmpdir) / "risk.json")
            broker = _CountingBroker()
            active_flag = {"active": False}
            reconcile_calls = {"count": 0}

            def _reconcile():
                reconcile_calls["count"] += 1
                return {"ok": True}

            wd = RiskWatchdog(
                risk_mgr,
                interval_sec=1.0,
                broker_provider=lambda: ("upstox", broker),
                reconcile_callback=_reconcile,
                reconcile_every_n=1,
                is_active_callback=lambda: active_flag["active"],
            )
            # Speed up loop wait for unit test determinism
            wd._interval = 0.05
            wd.start()
            try:
                time.sleep(0.22)
                st_idle = wd.status()
                self.assertTrue(st_idle["running"])
                self.assertTrue(st_idle["idle_standby"])
                self.assertEqual(broker.calls, [])
                self.assertEqual(reconcile_calls["count"], 0)

                # Flip live activity on -> watchdog must resume immediately
                active_flag["active"] = True
                time.sleep(0.22)
                st_active = wd.status()
                self.assertFalse(st_active["idle_standby"])
                self.assertGreaterEqual(len(broker.calls), 4)
                self.assertGreaterEqual(reconcile_calls["count"], 1)
            finally:
                wd.stop()


class TestUpstoxAnalyticsToken(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "ALGODESK_VAULT_FILE",
                "ALGODESK_VAULT_KEY_FILE",
                "TOKEN_FILE_PATH",
                "UPSTOX_ANALYTICS_TOKEN",
                "UPSTOX_API_KEY",
                "UPSTOX_API_SECRET",
            )
        }
        os.environ["ALGODESK_VAULT_FILE"] = str(self._tmp / ".credentials.json")
        os.environ["ALGODESK_VAULT_KEY_FILE"] = str(self._tmp / ".vault.key")
        os.environ["TOKEN_FILE_PATH"] = str(self._tmp / "upstox_token.json")
        os.environ.pop("UPSTOX_ANALYTICS_TOKEN", None)
        os.environ.pop("UPSTOX_API_KEY", None)
        os.environ.pop("UPSTOX_API_SECRET", None)

    def tearDown(self) -> None:
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmpdir.cleanup()

    def test_save_prioritize_and_clear_analytics_token_without_affecting_oauth(self) -> None:
        auth = AuthManager()
        self.assertFalse(auth.has_analytics_token())
        self.assertEqual(auth.get_data_token_mode(), "none")

        sample_tok = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.analytics_1yr_token_sample"
        auth.save_analytics_token(f"Bearer {sample_tok}")

        self.assertTrue(auth.has_analytics_token())
        self.assertTrue(auth.has_data_token())
        self.assertEqual(auth.get_data_token_mode(), "analytics_token")
        self.assertEqual(auth.get_data_token(), sample_tok)

        # Trading OAuth token must still be strictly absent
        self.assertFalse(auth.is_authenticated())
        with self.assertRaises(RuntimeError):
            auth.get_valid_token()

        # Clear removes from env & vault cleanly
        auth.clear_analytics_token()
        self.assertFalse(auth.has_analytics_token())
        self.assertEqual(auth.get_data_token_mode(), "none")

    def test_historical_quotes_and_ws_use_analytics_token(self) -> None:
        sample_tok = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.analytics_data_paths_test"
        from broker.upstox.auth import auth_manager as global_upstox_auth

        global_upstox_auth.invalidate_local()
        global_upstox_auth.save_analytics_token(sample_tok)
        captured_headers: list[tuple[str, str]] = []

        def _fake_urlopen(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            auth_hdr = req.headers.get("Authorization") or req.get_header("Authorization") or ""
            captured_headers.append((url, auth_hdr))
            if "/v3/historical-candle/" in url:
                return _FakeHTTPResponse(
                    {
                        "status": "success",
                        "data": {
                            "candles": [
                                ["2026-04-01T00:00:00+05:30", 100.0, 105.0, 99.0, 104.0, 10000, 0]
                            ]
                        },
                    }
                )
            if "/v3/market-quote/ltp" in url:
                return _FakeHTTPResponse(
                    {
                        "status": "success",
                        "data": {
                            "NSE_EQ:RELIANCE": {
                                "instrument_token": "NSE_EQ|INE002A01018",
                                "last_price": 2950.5,
                                "cp": 2920.0,
                            }
                        },
                    }
                )
            if "/v3/market-quote/ohlc" in url:
                return _FakeHTTPResponse({"status": "success", "data": {}})
            if "/v3/feed/market-data-feed/authorize" in url:
                return _FakeHTTPResponse(
                    {
                        "status": "success",
                        "data": {"authorized_redirect_uri": "wss://wsfeeder-api.upstox.com/test"},
                    }
                )
            return _FakeHTTPResponse({"status": "success", "data": {}})

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            # 1. Historical candles
            hdm = HistoricalDataManager(db_path=self._tmp / "ohlcv.db")
            rows = hdm._fetch_upstox_chunk(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="1day",
                chunk_start=date(2026, 4, 1),
                chunk_end=date(2026, 4, 1),
                instrument_key="NSE_EQ|INE002A01018",
            )
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["close"], 104.0)

            # 2. Live quotes
            quotes = _fetch_live_quotes_upstox(
                [{"symbol": "RELIANCE", "exchange": "NSE", "instrument_key": "NSE_EQ|INE002A01018"}]
            )
            self.assertIn("RELIANCE", quotes)
            self.assertAlmostEqual(quotes["RELIANCE"]["ltp"], 2950.5)

            # 3. WebSocket authorize via UpstoxBroker and UpstoxWSFeedClient
            broker = UpstoxBroker()
            ws_url_1 = broker.authorize_ws_feed()
            self.assertEqual(ws_url_1, "wss://wsfeeder-api.upstox.com/test")

            ws_client = UpstoxWSFeedClient(on_ticks=lambda _b, _q: None)
            ws_url_2, _hdrs = ws_client._build_connection_params()
            self.assertEqual(ws_url_2, "wss://wsfeeder-api.upstox.com/test")

            # 4. Health check verifies Analytics Token via /v3/market-quote/ltp
            snap = broker.health_check()
            self.assertEqual(snap.state, ConnectionState.CONNECTED)
            self.assertEqual(snap.auth_mode, "analytics_token")
            self.assertFalse(broker._connected)  # NOT a trading session

        expected_auth = f"Bearer {sample_tok}"
        self.assertGreaterEqual(len(captured_headers), 5)
        for url, auth_hdr in captured_headers:
            self.assertEqual(auth_hdr, expected_auth, f"Expected analytics token header for {url}")

        # 5. Preflight: data_connection passes with Analytics Token, trading_auth still fails without OAuth
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            pf = PreflightChecker.run_all(
                trading_broker="upstox",
                data_connection="upstox",
                symbols=["RELIANCE"],
                exchange="NSE",
                probe_brokers=True,
            )
        checks_by_id = {c["id"]: c for c in pf["checks"]}
        self.assertEqual(checks_by_id["data_connection"]["status"], "PASS")
        self.assertEqual(checks_by_id["trading_auth"]["status"], "FAIL")
        self.assertIn("Analytics Token", checks_by_id["trading_auth"]["message"])

        # 6. Credential summary masks Analytics Token and never leaks plaintext
        svc = BrokerHubService()
        summary = svc._credential_summary()["upstox"]
        self.assertTrue(summary["has_analytics_token"])
        self.assertEqual(summary["data_token_mode"], "analytics_token")
        self.assertNotIn(sample_tok, json.dumps(summary))
        self.assertIn("••••", summary["analytics_token_preview"])


if __name__ == "__main__":
    unittest.main()
