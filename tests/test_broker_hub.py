"""Unit tests for the refactored modular broker hub, security fixes, and routing."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from broker.base import ConnectionState
from broker.registry import BrokerRegistry, SelectionStore
from broker.upstox.auth import AuthManager, current_upstox_session_bounds
from config import IST_TZ
from web.service import BrokerHubService


class TestBrokerHubArchitecture(unittest.TestCase):
    def test_ist_0330_boundary(self) -> None:
        # 02:15 IST belongs to previous day's 03:30 IST session
        early_morning = datetime(2026, 11, 15, 2, 15, tzinfo=IST_TZ)
        start, expiry = current_upstox_session_bounds(early_morning)
        self.assertEqual(start, datetime(2026, 11, 14, 3, 30, tzinfo=IST_TZ))
        self.assertEqual(expiry, datetime(2026, 11, 15, 3, 30, tzinfo=IST_TZ))

        # Token generated at 09:00 AM yesterday should be expired after 03:30 AM today
        token_yesterday = {"generated_at_ist": "2026-11-14T09:00:00+05:30"}
        with patch("broker.upstox.auth.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 15, 9, 0, tzinfo=IST_TZ)
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            mock_dt.combine.side_effect = datetime.combine
            self.assertFalse(AuthManager._is_token_fresh(token_yesterday))

    def test_registry_has_upstox_zerodha_yfinance(self) -> None:
        available = BrokerRegistry.list_available()
        self.assertIn("upstox", available)
        self.assertIn("zerodha", available)
        self.assertIn("yfinance", available)

        zerodha = BrokerRegistry.get("zerodha")
        snap = zerodha.health_check()
        self.assertEqual(snap.state, ConnectionState.PLANNED)
        self.assertEqual(snap.stage, "planned")

    def test_selection_validation_blocks_invalid_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SelectionStore(Path(tmpdir) / "selection.json")
            svc = BrokerHubService(selection_store=store)

            # Valid: Upstox trading + YFinance data
            res = svc.save_selection("upstox", "yfinance")
            self.assertTrue(res["ok"])
            self.assertEqual(store.load()["trading_broker"], "upstox")
            self.assertEqual(store.load()["data_connection"], "yfinance")

            # Invalid: YFinance cannot execute trades
            with self.assertRaises(ValueError):
                svc.save_selection("yfinance", "yfinance")

            # Invalid: Zerodha is planned, cannot be activated yet
            with self.assertRaises(ValueError):
                svc.save_selection("zerodha", "upstox")

    def test_logout_uses_upstox_base_url(self) -> None:
        mgr = AuthManager()
        with patch.dict(os.environ, {"TOKEN_FILE_PATH": "/tmp/algodesk_test_token.json"}):
            mgr._token_data = {"access_token": "dummy-secret"}
            with patch("urllib.request.urlopen") as mock_open:
                mock_open.return_value.__enter__.return_value.status = 200
                ok = mgr.logout()
                self.assertTrue(ok)
                called_req = mock_open.call_args[0][0]
                self.assertEqual(called_req.full_url, "https://api.upstox.com/v2/logout")
                self.assertIsNone(mgr._token_data)


if __name__ == "__main__":
    unittest.main()
