"""Unit tests for the refactored modular broker hub, security fixes, and routing."""

from __future__ import annotations

import os
import socket
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from broker.base import BrokerHealthSnapshot, BrokerCapabilities, ConnectionState
from broker.registry import BrokerRegistry, SelectionStore
from broker.upstox.auth import AuthManager, current_upstox_session_bounds
from config import IST_TZ, AppConfig
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

    def test_env_auto_detection_live_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "UPSTOX_API_KEY=env_test_key_12345\n"
                "UPSTOX_API_SECRET=env_test_secret_abc\n"
                "UPSTOX_REDIRECT_URI=https://127.0.0.1:5000/\n",
                encoding="utf-8",
            )
            saved_env = {
                k: os.environ.get(k)
                for k in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI")
            }
            try:
                for k in saved_env:
                    os.environ.pop(k, None)

                cfg = AppConfig()
                cfg.BASE_DIR = Path(tmpdir)
                cfg._env_mtime = None

                self.assertTrue(cfg.reload_if_changed())
                self.assertEqual(cfg.UPSTOX_API_KEY, "env_test_key_12345")
                self.assertEqual(cfg.UPSTOX_API_SECRET, "env_test_secret_abc")
                self.assertEqual(cfg.UPSTOX_REDIRECT_URI, "https://127.0.0.1:5000/")
                self.assertEqual(cfg.credential_source("UPSTOX_API_KEY", "upstox_api_key"), "env")
            finally:
                for k, v in saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_auto_capture_loopback_listener_completes_reconnect(self) -> None:
        # Allocate an unused ephemeral port on 127.0.0.1
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.bind(("127.0.0.1", 0))
            free_port = probe_sock.getsockname()[1]

        redirect_uri = f"http://127.0.0.1:{free_port}/"
        connected_snap = BrokerHealthSnapshot(
            broker_id="upstox",
            display_name="Upstox",
            stage="active",
            state=ConnectionState.CONNECTED,
            capabilities=BrokerCapabilities(True, True, True, True),
            latency_ms=12.0,
            account_name="Test Trader",
            client_id="UT1234",
            message="Connected & verified",
            can_reconnect=True,
            has_credentials=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            svc = BrokerHubService(selection_store=SelectionStore(Path(tmpdir) / "sel.json"))

            with (
                patch.dict(
                    os.environ,
                    {
                        "UPSTOX_API_KEY": "dummy_api_key",
                        "UPSTOX_API_SECRET": "dummy_api_secret",
                        "UPSTOX_REDIRECT_URI": redirect_uri,
                    },
                ),
                patch("broker.upstox.broker.UpstoxBroker.complete_auth", return_value=connected_snap) as mock_complete,
            ):
                start_res = svc.start_reconnect("upstox", redirect_uri=redirect_uri, server_port=8000)
                self.assertEqual(start_res["mode"], "oauth")
                self.assertEqual(start_res["capture_mode"], "loopback_listener")

                # Simulate Upstox browser redirect hitting the local listener with ?code=...
                callback_url = f"http://127.0.0.1:{free_port}/?code=AUTO_CODE_789"
                with urllib.request.urlopen(callback_url, timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    body = resp.read().decode("utf-8")
                    self.assertIn("algodesk-oauth-captured", body)

                # Wait briefly for background exchange thread to finish
                deadline = time.time() + 3.0
                status_payload = svc.get_reconnect_status("upstox")
                while status_payload.get("status") in {"listening", "exchanging"} and time.time() < deadline:
                    time.sleep(0.05)
                    status_payload = svc.get_reconnect_status("upstox")

                self.assertEqual(status_payload["status"], "connected")
                mock_complete.assert_called_once()
                self.assertIn("AUTO_CODE_789", mock_complete.call_args.kwargs["code_or_url"])


if __name__ == "__main__":
    unittest.main()
