"""Unit tests for the refactored modular broker hub, security fixes, and routing."""

from __future__ import annotations

import hashlib
import os
import socket
import tempfile
import time
import unittest
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from broker.base import BrokerCapabilities, BrokerHealthSnapshot, ConnectionState
from broker.registry import BrokerRegistry, SelectionStore
from broker.upstox.auth import AuthManager, current_upstox_session_bounds
from broker.zerodha.auth import ZerodhaAuthManager, current_zerodha_session_bounds
from broker.zerodha.feed import KiteTickerAdapter
from config import IST_TZ, AppConfig
from core.instruments import InstrumentIndex, InstrumentRecord
from core.runtime import LiveBotEngine, OrderRequest
from core.historical import HistoricalDataManager, compute_missing_date_ranges
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

    def test_zerodha_ist_0600_boundary(self) -> None:
        # 05:15 IST belongs to previous day's 06:00 IST Kite session
        early_morning = datetime(2026, 11, 16, 5, 15, tzinfo=IST_TZ)
        start, expiry = current_zerodha_session_bounds(early_morning)
        self.assertEqual(start, datetime(2026, 11, 15, 6, 0, tzinfo=IST_TZ))
        self.assertEqual(expiry, datetime(2026, 11, 16, 6, 0, tzinfo=IST_TZ))

        # Token from yesterday 10:00 IST expires after 06:00 AM IST today
        token_yesterday = {"generated_at_ist": "2026-11-15T10:00:00+05:30"}
        with patch("broker.zerodha.auth.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 11, 16, 9, 15, tzinfo=IST_TZ)
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            mock_dt.combine.side_effect = datetime.combine
            self.assertFalse(ZerodhaAuthManager._is_token_fresh(token_yesterday))

    def test_registry_has_active_upstox_zerodha_yfinance(self) -> None:
        available = BrokerRegistry.list_available()
        self.assertIn("upstox", available)
        self.assertIn("zerodha", available)
        self.assertIn("yfinance", available)

        zerodha = BrokerRegistry.get("zerodha")
        self.assertEqual(zerodha.stage, "active")
        self.assertTrue(zerodha.capabilities.supports_trading)
        self.assertTrue(zerodha.capabilities.supports_market_data)
        self.assertTrue(zerodha.capabilities.supports_websocket)
        snap = zerodha.health_check()
        self.assertNotEqual(snap.state, ConnectionState.PLANNED)
        self.assertTrue(snap.can_reconnect)

    def test_selection_validation_supports_cross_broker_and_blocks_invalid_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SelectionStore(Path(tmpdir) / "selection.json")
            svc = BrokerHubService(selection_store=store)

            # Valid: Upstox trading + YFinance data
            res = svc.save_selection("upstox", "yfinance")
            self.assertTrue(res["ok"])
            self.assertEqual(store.load()["trading_broker"], "upstox")
            self.assertEqual(store.load()["data_connection"], "yfinance")

            # Valid in Phase 2: Cross-broker routing (Upstox trading + Zerodha data and vice versa)
            res_cross = svc.save_selection("zerodha", "upstox")
            self.assertTrue(res_cross["ok"])
            self.assertEqual(store.load()["trading_broker"], "zerodha")
            self.assertEqual(store.load()["data_connection"], "upstox")

            # Invalid: YFinance cannot execute trades
            with self.assertRaises(ValueError):
                svc.save_selection("yfinance", "yfinance")

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

    def test_zerodha_checksum_exchange_and_logout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = str(Path(tmpdir) / "kite_token.json")
            mgr = ZerodhaAuthManager()
            with patch.dict(
                os.environ,
                {
                    "ZERODHA_API_KEY": "kite_key_1",
                    "ZERODHA_API_SECRET": "kite_secret_2",
                    "ZERODHA_TOKEN_FILE_PATH": token_path,
                },
            ):
                expected_checksum = hashlib.sha256(b"kite_key_1REQ_TOKEN_99kite_secret_2").hexdigest()
                with patch.object(
                    mgr,
                    "_http_post_session",
                    return_value={"access_token": "kite_acc_tok", "user_id": "AB1234", "user_name": "Kite User"},
                ) as mock_post:
                    data = mgr.generate_token_from_url(
                        "http://127.0.0.1:8000/auth/zerodha/callback?request_token=REQ_TOKEN_99&action=login&status=success"
                    )
                    self.assertEqual(data["access_token"], "kite_acc_tok")
                    sent_payload = mock_post.call_args[0][0]
                    self.assertEqual(sent_payload["checksum"], expected_checksum)
                    self.assertEqual(sent_payload["request_token"], "REQ_TOKEN_99")

                # Verify KiteTicker adapter status without needing kiteconnect SDK imported
                feed = KiteTickerAdapter(api_key="kite_key_1", access_token="kite_acc_tok")
                feed.subscribe([738561, 408065], mode=KiteTickerAdapter.MODE_QUOTE)
                self.assertEqual(feed.status()["subscribed_count"], 2)

                with patch("urllib.request.urlopen") as mock_open:
                    mock_open.return_value.__enter__.return_value.status = 200
                    ok = mgr.logout()
                    self.assertTrue(ok)
                    called_req = mock_open.call_args[0][0]
                    self.assertIn("https://api.kite.trade/session/token?", called_req.full_url)
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

    def test_no_keychain_prompts_by_default_and_local_vault_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir) / ".credentials.json"
            token_path = Path(tmpdir) / "upstox_token.json"
            saved_env = {
                k: os.environ.get(k)
                for k in (
                    "ALGODESK_USE_KEYCHAIN",
                    "ALGODESK_VAULT_FILE",
                    "TOKEN_FILE_PATH",
                    "UPSTOX_API_KEY",
                    "UPSTOX_API_SECRET",
                )
            }
            try:
                os.environ.pop("ALGODESK_USE_KEYCHAIN", None)
                os.environ["ALGODESK_VAULT_FILE"] = str(vault_path)
                os.environ["TOKEN_FILE_PATH"] = str(token_path)

                with (
                    patch("config.subprocess.run") as mock_sec,
                    patch("builtins.__import__", wraps=__import__) as mock_import,
                ):
                    cfg = AppConfig()
                    cfg.BASE_DIR = Path(tmpdir)
                    cfg._env_mtime = None

                    # Save credentials cleanly into 0o600 local vault
                    cfg.save_local_credential("UPSTOX_API_KEY", "upstox_api_key", "local_key_abc123")
                    cfg.save_local_credential("UPSTOX_API_SECRET", "upstox_api_secret", "local_sec_xyz789")

                    # Clear process env to verify reading back directly from local 0o600 vault
                    os.environ.pop("UPSTOX_API_KEY", None)
                    os.environ.pop("UPSTOX_API_SECRET", None)
                    self.assertEqual(cfg.UPSTOX_API_KEY, "local_key_abc123")
                    self.assertEqual(cfg.UPSTOX_API_SECRET, "local_sec_xyz789")
                    self.assertEqual(cfg.credential_source("UPSTOX_API_KEY", "upstox_api_key"), "local_vault")
                    self.assertTrue(vault_path.exists())
                    self.assertEqual(vault_path.stat().st_mode & 0o777, 0o600)

                    # Save & reload token directly in 0o600 token.json without OS Keychain
                    mgr = AuthManager()
                    mgr._save_token({"access_token": "tok_local_only", "generated_at_ist": datetime.now(tz=IST_TZ).isoformat()})
                    self.assertTrue(token_path.exists())
                    loaded = mgr._load_token()
                    self.assertIsNotNone(loaded)
                    self.assertEqual(loaded["access_token"], "tok_local_only")

                    # Verify subprocess security CLI was never invoked
                    mock_sec.assert_not_called()
                    # Verify keyring module was never imported
                    imported_names = [call.args[0] for call in mock_import.call_args_list if call.args]
                    self.assertNotIn("keyring", imported_names)
            finally:
                for k, v in saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_gap_only_historical_download_and_snapshot_vs_full_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = HistoricalDataManager(Path(tmpdir) / "ohlcv.db")
            recorded_calls: list[tuple[date, date]] = []

            def fake_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                recorded_calls.append((c_start, c_end))
                bars = []
                cur = c_start
                while cur <= c_end:
                    if cur.weekday() < 5:
                        # Emit 2 intraday bars per trading day (e.g., morning + afternoon)
                        for idx, hh in enumerate((10, 14)):
                            ts = f"{cur.isoformat()}T{hh:02d}:00:00+05:30"
                            base = 2500.0 + idx * 5.0
                            bars.append(
                                {
                                    "ts_ist": ts,
                                    "trade_date": cur.isoformat(),
                                    "open": base,
                                    "high": base + 12.0,
                                    "low": base - 4.0,
                                    "close": base + 8.0,
                                    "volume": 10000 + idx * 500,
                                    "oi": 0,
                                }
                            )
                    cur += timedelta(days=1)
                return bars

            # 1) Initial download for 2026-10-05..2026-10-09 (5 weekdays -> 10 bars)
            res1 = mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="30minute",
                from_date="2026-10-05",
                to_date="2026-10-09",
                provider="yfinance",
                fetcher=fake_fetcher,
            )
            self.assertEqual(res1["api_calls_made"], 1)
            self.assertEqual(res1["existing_days_skipped"], 0)
            self.assertEqual(res1["missing_days_count"], 5)
            self.assertEqual(res1["rows_inserted"], 10)
            self.assertFalse(res1["cache_hit_only"])
            self.assertEqual(recorded_calls, [(date(2026, 10, 5), date(2026, 10, 9))])

            # 2) Re-request identical range -> strictly 0 API calls
            recorded_calls.clear()
            res2 = mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="30minute",
                from_date="2026-10-05",
                to_date="2026-10-09",
                provider="yfinance",
                fetcher=fake_fetcher,
            )
            self.assertEqual(res2["api_calls_made"], 0)
            self.assertEqual(res2["existing_days_skipped"], 5)
            self.assertEqual(res2["missing_days_count"], 0)
            self.assertTrue(res2["cache_hit_only"])
            self.assertEqual(recorded_calls, [])

            # 3) Expand range to 2026-10-01..2026-10-14 -> only absent leading & trailing ranges fetched
            recorded_calls.clear()
            res3 = mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="30minute",
                from_date="2026-10-01",
                to_date="2026-10-14",
                provider="yfinance",
                fetcher=fake_fetcher,
            )
            self.assertEqual(
                recorded_calls,
                [(date(2026, 10, 1), date(2026, 10, 2)), (date(2026, 10, 12), date(2026, 10, 14))],
            )
            self.assertEqual(res3["api_calls_made"], 2)
            self.assertEqual(res3["existing_days_skipped"], 5)
            self.assertEqual(res3["missing_days_count"], 5)

            # 4) Verify Snapshot (<=15 bars) vs Full (all 20 bars) + CSV export (0 API calls)
            snap = mgr.get_records(symbol="RELIANCE", exchange="NSE", timeframe="30minute", view="snapshot")
            full = mgr.get_records(symbol="RELIANCE", exchange="NSE", timeframe="30minute", view="full")
            self.assertEqual(snap["total_rows"], 20)
            self.assertEqual(snap["returned_rows"], 15)
            self.assertTrue(snap["is_truncated"])
            self.assertEqual(full["total_rows"], 20)
            self.assertEqual(full["returned_rows"], 20)
            self.assertFalse(full["is_truncated"])

            csv_text = mgr.export_csv(symbol="RELIANCE", exchange="NSE", timeframe="30minute")
            self.assertEqual(len(csv_text.strip().splitlines()), 21)

            # Pure unit check for middle-gap detection
            gaps = compute_missing_date_ranges(
                date(2026, 10, 5),
                date(2026, 10, 9),
                {date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 8), date(2026, 10, 9)},
            )
            self.assertEqual(gaps, [(date(2026, 10, 7), date(2026, 10, 7))])

    def test_sqlite_instrument_index_search_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            idx = InstrumentIndex(Path(tmpdir) / "instruments.db")
            stats = idx.stats()
            self.assertGreaterEqual(stats["total_instruments"], 20)

            self.assertEqual(idx.resolve_key("RELIANCE", broker_id="upstox"), "NSE_EQ|INE002A01018")
            self.assertEqual(idx.resolve_key("RELIANCE", broker_id="zerodha"), "738561")

            hits = idx.search("TCS", broker_id="upstox")
            self.assertTrue(any(h["trading_symbol"] == "TCS" for h in hits))

            upserted = idx.upsert_records(
                "upstox",
                [
                    InstrumentRecord(
                        broker_id="upstox",
                        exchange="NSE",
                        segment="NSE_EQ",
                        trading_symbol="TATAMOTORS",
                        asset_symbol="TATAMOTORS",
                        name="Tata Motors Ltd",
                        instrument_type="EQUITY",
                        instrument_key="NSE_EQ|INE155A01022",
                    )
                ],
            )
            self.assertEqual(upserted, 1)
            self.assertEqual(idx.resolve_key("TATAMOTORS", broker_id="upstox"), "NSE_EQ|INE155A01022")

    def test_consolidated_live_bot_engine_and_kill_switch(self) -> None:
        engine = LiveBotEngine(trading_broker_id="upstox", data_connection_id="zerodha")
        self.assertEqual(engine.state.active_trading_broker, "upstox")
        self.assertEqual(engine.state.active_data_connection, "zerodha")
        self.assertIs(engine.session_manager.order_manager, engine.order_manager)

        engine.state.kill_switch_engaged = True
        with self.assertRaises(RuntimeError):
            engine.session_manager.submit_signal_order(
                OrderRequest(
                    trading_symbol="RELIANCE",
                    exchange="NSE",
                    transaction_type="BUY",
                    quantity=1,
                )
            )


if __name__ == "__main__":
    unittest.main()
