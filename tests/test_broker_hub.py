"""Unit tests for the refactored modular broker hub, security fixes, and routing."""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
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
from core.audit import AuditTrail
from core.feed_manager import HotStandbyFeedManager
from core.historical import HistoricalDataManager, compute_missing_date_ranges
from core.instruments import InstrumentIndex, InstrumentRecord
from core.preflight import KillSwitchController, PreflightChecker
from core.runtime import LiveBotEngine, OrderRequest
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

    def test_sebi_hash_chained_audit_trail_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "audit_chain.db"
            trail = AuditTrail(db_path)

            trail.log_preflight(
                overall_ready=True,
                passed_count=6,
                total_count=6,
                trading_broker="upstox",
                data_connection="upstox",
            )
            trail.log_failover(from_feed="upstox", to_feed="yfinance", reason="WebSocket dropped")
            trail.log_kill_switch(engaged=True, reason="Drill stop", trading_broker="upstox", orders_cancelled=0)

            verified = trail.verify_chain()
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["total_events"], 3)

            # Mutate historical SQLite row directly to simulate log tampering
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE audit_chain SET payload = '{\"tampered\":true}' WHERE rowid = 2")
                conn.commit()

            tampered = trail.verify_chain()
            self.assertFalse(tampered["valid"])
            self.assertEqual(tampered["broken_index"], 1)

    def test_hot_standby_feed_auto_failover_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trail = AuditTrail(Path(tmpdir) / "audit_chain.db")
            state_flags = {"upstox_up": False}

            def upstox_fetcher(syms: list[str]):
                if not state_flags["upstox_up"]:
                    raise ConnectionError("Upstox WS dropped")
                return {
                    s: {"symbol": s, "ltp": 2951.25, "change_pct": 0.4, "source": "upstox", "ts_ist": "2026-11-18T10:15:00+05:30"}
                    for s in syms
                }

            def zerodha_fetcher(_syms: list[str]):
                raise RuntimeError("Zerodha token expired")

            def yfinance_fetcher(syms: list[str]):
                return {
                    s: {"symbol": s, "ltp": 2950.0, "change_pct": 0.3, "source": "yfinance", "ts_ist": "2026-11-18T10:15:00+05:30"}
                    for s in syms
                }

            mgr = HotStandbyFeedManager(
                primary_id="upstox",
                audit=trail,
                fetchers={"upstox": upstox_fetcher, "zerodha": zerodha_fetcher, "yfinance": yfinance_fetcher},
                watchlist_file=Path(tmpdir) / "watchlist.json",
            )

            res1 = mgr.poll_ticks(["RELIANCE"])
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["active_feed"], "yfinance")
            self.assertTrue(res1["in_failover"])
            self.assertEqual(res1["failover_count"], 1)
            self.assertEqual(res1["quotes"][0]["ltp"], 2950.0)

            events = trail.list_events(limit=10)
            self.assertEqual(events[0]["event_type"], "FEED_FAILOVER")
            self.assertEqual(events[0]["payload"]["to_feed"], "yfinance")

            # Restore primary and verify automatic failback
            state_flags["upstox_up"] = True
            res2 = mgr.recover_primary()
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["active_feed"], "upstox")
            self.assertFalse(res2["in_failover"])
            self.assertEqual(res2["quotes"][0]["ltp"], 2951.25)

            # Operator manual drill triggers standby cleanly
            res3 = mgr.trigger_failover(reason="Scheduled failover drill")
            self.assertTrue(res3["in_failover"])
            self.assertEqual(res3["active_feed"], "yfinance")

    def test_kill_switch_persistence_blocks_orders_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trail = AuditTrail(Path(tmpdir) / "audit_chain.db")
            ks_file = Path(tmpdir) / "kill_switch.json"
            ks = KillSwitchController(state_file=ks_file, audit=trail)

            with patch("broker.upstox.broker.UpstoxBroker.get_orders", return_value=[]):
                ks.engage(reason="Max drawdown guard", trading_broker="upstox")

            # Verify persisted disk state survives controller re-instantiation
            ks_reloaded = KillSwitchController(state_file=ks_file, audit=trail)
            self.assertTrue(ks_reloaded.is_engaged())

            # Global kill switch must block LiveBotEngine order execution even when state.kill_switch_engaged is False
            engine = LiveBotEngine(
                trading_broker_id="upstox",
                data_connection_id="yfinance",
                kill_switch=ks_reloaded,
                audit=trail,
            )
            with self.assertRaises(RuntimeError):
                engine.session_manager.submit_signal_order(
                    OrderRequest(trading_symbol="RELIANCE", exchange="NSE", transaction_type="BUY", quantity=1)
                )

            pre = PreflightChecker.run_all(
                trading_broker="upstox",
                data_connection="yfinance",
                symbols=["RELIANCE", "NIFTY"],
                kill_switch=ks_reloaded,
                audit=trail,
                probe_brokers=False,
            )
            self.assertFalse(pre["overall_ready"])
            checks_by_id = {c["id"]: c for c in pre["checks"]}
            self.assertEqual(checks_by_id["kill_switch"]["status"], "FAIL")
            self.assertEqual(checks_by_id["symbol_mapping"]["status"], "PASS")

            ks_reloaded.reset(reason="Verified & cleared", trading_broker="upstox")
            self.assertFalse(ks_reloaded.is_engaged())
            self.assertTrue(trail.verify_chain()["valid"])

    def test_fno_instrument_search_expiry_normalization_and_cross_broker_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            idx = InstrumentIndex(Path(tmpdir) / "instruments.db")

            # Seed Upstox F&O payload with NSE_FO segment & epoch-ms expiry (2026-11-26 IST)
            upstox_payload = [
                {
                    "segment": "NSE_FO",
                    "exchange": "NSE",
                    "trading_symbol": "NIFTY26NOVFUT",
                    "underlying_symbol": "NIFTY",
                    "name": "NIFTY NOV FUT",
                    "instrument_type": "FUT",
                    "instrument_key": "NSE_FO|50001",
                    "exchange_token": "50001",
                    "expiry": 1795631400000,
                    "strike_price": 0,
                    "lot_size": 75,
                    "tick_size": 0.05,
                },
                {
                    "segment": "NSE_FO",
                    "exchange": "NSE",
                    "trading_symbol": "NIFTY26NOV24500CE",
                    "underlying_symbol": "NIFTY",
                    "name": "NIFTY 24500 CE",
                    "instrument_type": "CE",
                    "instrument_key": "NSE_FO|50002",
                    "exchange_token": "50002",
                    "expiry": 1795631400000,
                    "strike_price": 24500,
                    "lot_size": 75,
                    "tick_size": 0.05,
                },
            ]
            # Seed Zerodha Kite CSV payload with NFO FUT & PE
            zerodha_csv = (
                "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
                "910001,3554,RELIANCE26NOVFUT,RELIANCE,0,2026-11-26,0,0.05,250,FUT,NFO-FUT,NFO\n"
                "910002,3555,NIFTY26NOV24500PE,NIFTY,0,2026-11-26,24500,0.05,75,PE,NFO-OPT,NFO\n"
            )

            import gzip
            import io
            import json as _json

            gz_bytes = gzip.compress(_json.dumps(upstox_payload).encode("utf-8"))

            class _FakeResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            with patch("urllib.request.urlopen", return_value=_FakeResp(gz_bytes)):
                up_res = idx.sync_upstox_master()
            self.assertEqual(up_res["indexed_count"], 2)

            with patch("urllib.request.urlopen", return_value=_FakeResp(zerodha_csv.encode("utf-8"))):
                zd_res = idx.sync_zerodha_master()
            self.assertEqual(zd_res["indexed_count"], 2)

            stats = idx.stats()
            self.assertGreaterEqual(stats["fno_instruments"], 4)

            # Multi-token search + contract type filtering
            ce_hits = idx.search("NIFTY 24500 CE", instrument_type="CE")
            self.assertEqual(len(ce_hits), 1)
            self.assertEqual(ce_hits[0]["trading_symbol"], "NIFTY26NOV24500CE")
            self.assertEqual(ce_hits[0]["exchange"], "NFO")
            self.assertEqual(ce_hits[0]["expiry"], "2026-11-26")

            fut_hits = idx.search("RELIANCE", instrument_type="FUT")
            self.assertEqual(len(fut_hits), 1)
            self.assertEqual(fut_hits[0]["asset_symbol"], "RELIANCE")
            self.assertEqual(fut_hits[0]["lot_size"], 250)

            # Structured F&O Contract Finder query
            fno_bundle = idx.list_fno_contracts(underlying="NIFTY", exchange="NFO", contract_type="OPT")
            self.assertIn("NIFTY", fno_bundle["underlyings"])
            self.assertIn("2026-11-26", fno_bundle["expiries"])
            self.assertIn(24500.0, fno_bundle["strikes"])
            self.assertEqual(fno_bundle["count"], 2)

            # Cross-broker key resolution (NFO <-> NSE_FO)
            self.assertEqual(idx.resolve_key("NIFTY26NOVFUT", broker_id="upstox", exchange="NFO", instrument_type="FUT"), "NSE_FO|50001")
            self.assertEqual(idx.resolve_key("NIFTY26NOV24500PE", broker_id="zerodha", exchange="NFO", instrument_type="PE"), "910002")

    def test_fno_historical_download_oi_and_yahoo_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = HistoricalDataManager(Path(tmpdir) / "ohlcv.db")

            # Yahoo Finance must clearly reject NSE/BSE F&O contracts
            with self.assertRaises(ValueError) as ctx:
                mgr.download(
                    symbol="NIFTY26NOVFUT",
                    exchange="NFO",
                    timeframe="1day",
                    from_date="2026-11-02",
                    to_date="2026-11-06",
                    provider="yfinance",
                    instrument_type="FUT",
                )
            self.assertIn("Upstox or Zerodha Kite", str(ctx.exception))

            # F&O download via Upstox fetcher stores & surfaces Open Interest (OI)
            seen_args = []

            def fake_fno_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                seen_args.append((sym, exch, tf, c_start, c_end))
                return [
                    {
                        "ts_ist": "2026-11-02T00:00:00+05:30",
                        "trade_date": "2026-11-02",
                        "open": 24400.0,
                        "high": 24610.0,
                        "low": 24350.0,
                        "close": 24550.0,
                        "volume": 125000,
                        "oi": 4825000,
                    },
                    {
                        "ts_ist": "2026-11-03T00:00:00+05:30",
                        "trade_date": "2026-11-03",
                        "open": 24560.0,
                        "high": 24700.0,
                        "low": 24510.0,
                        "close": 24680.0,
                        "volume": 134000,
                        "oi": 5012000,
                    },
                ]

            res = mgr.download(
                symbol="NFO:NIFTY26NOVFUT",
                exchange="NFO",
                timeframe="1day",
                from_date="2026-11-02",
                to_date="2026-11-03",
                provider="upstox",
                instrument_type="FUT",
                instrument_key="NSE_FO|50001",
                fetcher=fake_fno_fetcher,
            )
            self.assertEqual(res["symbol"], "NIFTY26NOVFUT")
            self.assertEqual(res["exchange"], "NFO")
            self.assertEqual(res["rows_inserted"], 2)
            self.assertEqual(res["snapshot"]["summary"]["latest_oi"], 5012000)
            self.assertEqual(res["snapshot"]["rows"][-1]["oi"], 5012000)

            csv_out = mgr.export_csv(symbol="NIFTY26NOVFUT", exchange="NFO", timeframe="1day")
            self.assertIn("oi", csv_out.splitlines()[0])
            self.assertIn("5012000", csv_out)

    def test_service_list_fno_contracts_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = BrokerHubService(selection_store=SelectionStore(Path(tmpdir) / "sel.json"))
            bundle = svc.list_fno_contracts(underlying="NIFTY", exchange="NFO", contract_type="FO")
            self.assertIn("underlyings", bundle)
            self.assertIn("expiries", bundle)
            self.assertIn("strikes", bundle)
            self.assertIn("contracts", bundle)
            self.assertIn("count", bundle)

    def test_live_feed_subscribe_unsubscribe_persistence_and_listener(self) -> None:
        from core.feed_manager import _fetch_one_yahoo_quote

        with tempfile.TemporaryDirectory() as tmpdir:
            watchlist_path = Path(tmpdir) / "watchlist.json"
            trail = AuditTrail(Path(tmpdir) / "audit_chain.db")
            idx = InstrumentIndex(Path(tmpdir) / "instruments.db")
            idx.upsert_records(
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
                    ),
                    InstrumentRecord(
                        broker_id="upstox",
                        exchange="NFO",
                        segment="NSE_FO",
                        trading_symbol="NIFTY26NOVFUT",
                        asset_symbol="NIFTY",
                        name="NIFTY NOV FUT",
                        instrument_type="FUT",
                        instrument_key="NSE_FO|50001",
                        expiry="2026-11-26",
                        lot_size=75,
                    ),
                ],
            )
            tick_prices = {"RELIANCE": 2950.0, "TCS": 3910.0, "TATAMOTORS": 910.0}

            def fake_yfinance_fetcher(subs: list):
                out = {}
                for raw_item in subs:
                    item = raw_item if isinstance(raw_item, dict) else {"symbol": raw_item, "exchange": "NSE", "instrument_type": ""}
                    sym = item["symbol"]
                    if sym == "RANDOMXYZ123":
                        continue
                    if sym == "NIFTY26NOVFUT":
                        out[sym] = _fetch_one_yahoo_quote(item)
                    else:
                        tick_prices[sym] = tick_prices.get(sym, 1000.0) + 1.5
                        out[sym] = {
                            "symbol": sym,
                            "exchange": item.get("exchange", "NSE"),
                            "instrument_type": item.get("instrument_type", "EQUITY"),
                            "ltp": tick_prices[sym],
                            "prev_close": tick_prices[sym] - 10.0,
                            "open": tick_prices[sym] - 5.0,
                            "high": tick_prices[sym] + 2.0,
                            "low": tick_prices[sym] - 8.0,
                            "change": 10.0,
                            "change_pct": 0.45,
                            "volume": 50000,
                            "oi": 0,
                            "bid": tick_prices[sym] - 0.5,
                            "ask": tick_prices[sym] + 0.5,
                            "source": "yfinance",
                            "transport": "live_chart_poll",
                            "note": "",
                            "ts_ist": "2026-11-18T10:15:00+05:30",
                        }
                return out

            mgr = HotStandbyFeedManager(
                primary_id="yfinance",
                audit=trail,
                fetchers={"yfinance": fake_yfinance_fetcher},
                watchlist_file=watchlist_path,
                instruments=idx,
            )

            # Random/unknown symbol or unknown F&O contract must fail cleanly and NOT enter watchlist
            with self.assertRaises(ValueError):
                mgr.subscribe("RANDOMXYZ123", exchange="NSE", instrument_type="EQUITY")
            with self.assertRaises(ValueError):
                mgr.subscribe("FAKE999999CE", exchange="NFO", instrument_type="CE")
            subscribed_syms = [s["symbol"] for s in mgr.list_subscriptions()]
            self.assertNotIn("RANDOMXYZ123", subscribed_syms)
            self.assertNotIn("FAKE999999CE", subscribed_syms)

            sub_res = mgr.subscribe("TATAMOTORS", exchange="NSE", instrument_type="EQUITY")
            self.assertTrue(sub_res["ok"])
            self.assertEqual(sub_res["subscriptions"][0]["symbol"], "TATAMOTORS")
            self.assertEqual(sub_res["subscriptions"][0]["instrument_key"], "NSE_EQ|INE155A01022")
            self.assertGreaterEqual(len(sub_res["quotes"][0]["history"]), 1)

            # Subscribe a valid F&O contract -> non-blocking note when on Yahoo fallback
            fno_res = mgr.subscribe("NIFTY26NOVFUT", exchange="NFO", instrument_type="FUT")
            fno_quote = next(q for q in fno_res["quotes"] if q["symbol"] == "NIFTY26NOVFUT")
            self.assertIsNone(fno_quote["ltp"])
            self.assertIn("Upstox or Zerodha", fno_quote["note"])

            # Persistence check
            mgr2 = HotStandbyFeedManager(
                primary_id="yfinance",
                audit=trail,
                fetchers={"yfinance": fake_yfinance_fetcher},
                watchlist_file=watchlist_path,
                instruments=idx,
            )
            self.assertIn("TATAMOTORS", [s["symbol"] for s in mgr2.list_subscriptions()])

            # Listener pub/sub receives broadcast upon unsubscribe
            q = mgr2.register_listener()
            try:
                unsub_res = mgr2.unsubscribe("TATAMOTORS", exchange="NSE")
                self.assertEqual(unsub_res["removed"], 1)
                self.assertNotIn("TATAMOTORS", [s["symbol"] for s in unsub_res["subscriptions"]])
                event = q.get(timeout=2.0)
                self.assertEqual(event["type"], "tick_snapshot")
            finally:
                mgr2.unregister_listener(q)

    def test_ws_protocol_rfc6455_accept_key_and_masked_frame_roundtrip(self) -> None:
        import os
        import socket
        from core.ws_protocol import OPCODE_TEXT, compute_accept_key, read_ws_frame, send_ws_text

        # RFC 6455 Section 1.3 canonical test vector
        self.assertEqual(
            compute_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

        srv_sock, cli_sock = socket.socketpair()
        try:
            # Server sends text frame -> client reads unmasked frame
            send_ws_text(srv_sock, '{"type":"tick_snapshot","ok":true}')
            opcode, payload = read_ws_frame(cli_sock)
            self.assertEqual(opcode, OPCODE_TEXT)
            self.assertIn('"tick_snapshot"', payload.decode("utf-8"))

            # Client sends masked frame -> server unmasks accurately
            msg = b'{"action":"subscribe","symbol":"TCS","exchange":"NSE"}'
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
            frame = bytes([0x81, 0x80 | len(msg)]) + mask + masked
            cli_sock.sendall(frame)

            srv_opcode, srv_payload = read_ws_frame(srv_sock)
            self.assertEqual(srv_opcode, OPCODE_TEXT)
            self.assertEqual(srv_payload, msg)
        finally:
            srv_sock.close()
            cli_sock.close()

    def test_fno_active_expiry_filter_large_strike_chain_and_prune_on_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            idx = InstrumentIndex(Path(tmpdir) / "instruments.db")
            today_d = datetime.now(tz=IST_TZ).date()
            expired_str = (today_d - timedelta(days=30)).isoformat()
            upcoming_str = (today_d + timedelta(days=10)).isoformat()

            # Insert 1 expired contract and 150 active NIFTY CE strikes (including 25000)
            records = [
                InstrumentRecord(
                    broker_id="upstox",
                    exchange="NFO",
                    segment="NSE_FO",
                    trading_symbol="NIFTYOLD21000CE",
                    asset_symbol="NIFTY",
                    name="NIFTY OLD CE",
                    instrument_type="CE",
                    instrument_key="NSE_FO|OLD_01",
                    expiry=expired_str,
                    strike=21000.0,
                    lot_size=75,
                )
            ]
            for i in range(150):
                strike_val = 20000.0 + i * 50.0  # spans 20000..27450, includes 25000.0 (index 100)
                records.append(
                    InstrumentRecord(
                        broker_id="upstox",
                        exchange="NFO",
                        segment="NSE_FO",
                        trading_symbol=f"NIFTYNEXT{int(strike_val)}CE",
                        asset_symbol="NIFTY",
                        name=f"NIFTY {int(strike_val)} CE",
                        instrument_type="CE",
                        instrument_key=f"NSE_FO|NEW_{i}",
                        expiry=upcoming_str,
                        strike=strike_val,
                        lot_size=75,
                    )
                )
            idx.upsert_records("upstox", records)

            bundle = idx.list_fno_contracts(underlying="NIFTY", exchange="NFO", contract_type="OPT")
            self.assertNotIn(expired_str, bundle["expiries"])
            self.assertIn(upcoming_str, bundle["expiries"])
            self.assertGreaterEqual(len(bundle["strikes"]), 150)
            self.assertIn(25000.0, bundle["strikes"])

            # Explicit strike=25000 finds exact active contract
            exact_25k = idx.list_fno_contracts(
                underlying="NIFTY",
                exchange="NFO",
                contract_type="CE",
                expiry=upcoming_str,
                strike=25000.0,
            )
            self.assertEqual(exact_25k["contracts"][0]["trading_symbol"], "NIFTYNEXT25000CE")

            # Master sync prunes stale expired F&O rows before loading fresh contracts
            import gzip
            import io
            import json as _json

            gz_fresh = gzip.compress(
                _json.dumps(
                    [
                        {
                            "segment": "NSE_FO",
                            "exchange": "NSE",
                            "trading_symbol": "NIFTYFRESH25000CE",
                            "underlying_symbol": "NIFTY",
                            "name": "NIFTY 25000 CE",
                            "instrument_type": "CE",
                            "instrument_key": "NSE_FO|FRESH_25000",
                            "expiry": upcoming_str,
                            "strike_price": 25000,
                            "lot_size": 75,
                            "tick_size": 0.05,
                        }
                    ]
                ).encode("utf-8")
            )

            class _FakeResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            with patch("urllib.request.urlopen", return_value=_FakeResp(gz_fresh)):
                idx.sync_upstox_master()

            after_sync = idx.search("NIFTYOLD21000CE", broker_id="upstox", exchange="NFO")
            self.assertEqual(after_sync, [])

    def test_historical_today_not_frozen_as_holiday_and_upstox_intraday_merge(self) -> None:
        import io
        import json as _json

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = HistoricalDataManager(Path(tmpdir) / "ohlcv.db")
            today_d = datetime.now(tz=IST_TZ).date()
            past_d = today_d - timedelta(days=5)
            while past_d.weekday() >= 5:
                past_d -= timedelta(days=1)

            calls: list[tuple[date, date]] = []

            def fetcher_empty_today(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                calls.append((c_start, c_end))
                return [
                    {
                        "ts_ist": f"{past_d.isoformat()}T15:00:00+05:30",
                        "trade_date": past_d.isoformat(),
                        "open": 100.0,
                        "high": 105.0,
                        "low": 99.0,
                        "close": 104.0,
                        "volume": 5000,
                        "oi": 120000,
                    }
                ]

            mgr.download(
                symbol="NIFTYNEXT25000CE",
                exchange="NFO",
                timeframe="1day",
                from_date=past_d.isoformat(),
                to_date=today_d.isoformat(),
                provider="upstox",
                instrument_type="CE",
                instrument_key="NSE_FO|NEW_100",
                fetcher=fetcher_empty_today,
            )

            # Second download up to today_d must still re-check recent trailing day instead of freezing as holiday
            calls.clear()
            mgr.download(
                symbol="NIFTYNEXT25000CE",
                exchange="NFO",
                timeframe="1day",
                from_date=past_d.isoformat(),
                to_date=today_d.isoformat(),
                provider="upstox",
                instrument_type="CE",
                instrument_key="NSE_FO|NEW_100",
                fetcher=fetcher_empty_today,
            )
            if today_d.weekday() < 5:
                self.assertGreaterEqual(len(calls), 1)
                self.assertEqual(calls[-1][1], today_d)

            # Verify _fetch_upstox_chunk queries & merges /v2/historical-candle/intraday when chunk_end >= today_d
            requested_urls: list[str] = []

            class _FakeJsonResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def fake_urlopen(req, timeout=12):
                url = req.full_url
                requested_urls.append(url)
                if "/historical-candle/intraday/" in url:
                    payload = {
                        "status": "success",
                        "data": {
                            "candles": [
                                [f"{today_d.isoformat()}T10:00:00+05:30", 110.0, 115.0, 108.0, 114.0, 3000, 150000]
                            ]
                        },
                    }
                else:
                    payload = {
                        "status": "success",
                        "data": {
                            "candles": [
                                [f"{past_d.isoformat()}T15:00:00+05:30", 100.0, 105.0, 99.0, 104.0, 5000, 120000]
                            ]
                        },
                    }
                return _FakeJsonResp(_json.dumps(payload).encode("utf-8"))

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                merged = mgr._fetch_upstox_chunk(
                    "NIFTYNEXT25000CE",
                    "NFO",
                    "30minute",
                    past_d,
                    today_d,
                    instrument_type="CE",
                    instrument_key="NSE_FO|NEW_100",
                )

            # Observation #1: Upstox API must ONLY ever be called with canonical 1minute (never 30minute)
            self.assertTrue(any("/v3/historical-candle/" in u and "/minutes/1/" in u for u in requested_urls))
            self.assertTrue(any("/v3/historical-candle/intraday/" in u and "/minutes/1" in u for u in requested_urls))
            self.assertFalse(any("/minutes/30" in u for u in requested_urls))
            dates_returned = [r["trade_date"] for r in merged]
            self.assertIn(past_d.isoformat(), dates_returned)
            self.assertIn(today_d.isoformat(), dates_returned)
            self.assertEqual(merged, sorted(merged, key=lambda r: r["ts_ist"]))

    def test_live_terminal_routes_and_health_phase5(self) -> None:
        import json as _json
        import threading
        from web.local_server import _AlgoDeskHTTPServer, _StdlibBrokerHandler

        srv = _AlgoDeskHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                health = _json.loads(r.read().decode("utf-8"))
                self.assertEqual(r.status, 200)
                self.assertTrue(health["live_terminal"])
                self.assertGreaterEqual(health["phase"], 5)

            for term_path in ("/terminal", "/terminal/"):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{term_path}", timeout=3) as r:
                    html_body = r.read().decode("utf-8")
                    self.assertEqual(r.status, 200)
                    self.assertIn("Live WebSocket Terminal", html_body)

            for poll_path in ("/api/live/feeds/poll", "/api/live/poll"):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{poll_path}",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    payload = _json.loads(r.read().decode("utf-8"))
                    self.assertEqual(r.status, 200)
                    self.assertIn("active_feed", payload)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_upstox_v3_quotes_and_hft_orders(self) -> None:
        import io
        import json as _json
        from broker.base import BrokerOrder, OrderRequest as BrokerOrderRequest, OrderStatus
        from broker.upstox.broker import UpstoxBroker
        from core.feed_manager import _fetch_live_quotes_upstox

        seen_quote_urls: list[str] = []

        class _FakeJsonResp(io.BytesIO):
            def __init__(self, data: dict, status: int = 200) -> None:
                super().__init__(_json.dumps(data).encode("utf-8"))
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_quote_urlopen(req, timeout=5):
            url = req.full_url
            seen_quote_urls.append(url)
            if "/v3/market-quote/ohlc" in url:
                return _FakeJsonResp(
                    {
                        "status": "success",
                        "data": {
                            "NSE_EQ:RELIANCE": {
                                "last_price": 2965.5,
                                "instrument_token": "NSE_EQ|INE002A01018",
                                "prev_ohlc": {"open": 2940.0, "high": 2960.0, "low": 2930.0, "close": 2950.0, "volume": 100000, "ts": 1794900000000},
                                "live_ohlc": {"open": 2952.0, "high": 2970.0, "low": 2948.0, "close": 2965.5, "volume": 245000, "ts": 1794980000000},
                            },
                            "NSE_EQ:TCS": {
                                "last_price": 3915.0,
                                "instrument_token": "NSE_EQ|INE467B01029",
                                "live_ohlc": {"open": 3900.0, "high": 3920.0, "low": 3895.0, "close": 3915.0, "volume": 0, "ts": 1794980000000},
                            },
                        },
                    }
                )
            if "/v3/market-quote/ltp" in url:
                return _FakeJsonResp(
                    {
                        "status": "success",
                        "data": {
                            "NSE_EQ:TCS": {
                                "last_price": 3915.0,
                                "instrument_token": "NSE_EQ|INE467B01029",
                                "ltq": 10,
                                "volume": 95000,
                                "cp": 3900.0,
                            }
                        },
                    }
                )
            raise AssertionError(f"Unexpected quote URL: {url}")

        with (
            patch("core.feed_manager.upstox_auth.get_valid_token", return_value="dummy-upstox-tok"),
            patch("urllib.request.urlopen", side_effect=fake_quote_urlopen),
        ):
            quotes = _fetch_live_quotes_upstox(["RELIANCE", "TCS"])

        self.assertTrue(any("/v3/market-quote/ohlc" in u for u in seen_quote_urls))
        self.assertTrue(any("/v3/market-quote/ltp" in u for u in seen_quote_urls))
        self.assertEqual(quotes["RELIANCE"]["ltp"], 2965.5)
        self.assertEqual(quotes["RELIANCE"]["prev_close"], 2950.0)
        self.assertEqual(quotes["RELIANCE"]["transport"], "v3_ohlc")
        self.assertEqual(quotes["TCS"]["prev_close"], 3900.0)
        self.assertEqual(quotes["TCS"]["volume"], 95000)

        # Verify UpstoxBroker V3 HFT order endpoints & V3 WS feed authorization without upstox_client SDK
        order_calls: list[tuple[str, str]] = []

        def fake_broker_urlopen(req, timeout=6):
            order_calls.append((req.get_method(), req.full_url))
            if "/v3/feed/market-data-feed/authorize" in req.full_url:
                return _FakeJsonResp({"status": "success", "data": {"authorized_redirect_uri": "wss://api.upstox.com/v3/feed/market-data-feed?code=abc"}})
            if "/v3/order/place" in req.full_url:
                return _FakeJsonResp({"status": "success", "data": {"order_ids": ["261118000012345"]}})
            return _FakeJsonResp({"status": "success", "data": {"order_id": "261118000012345"}})

        broker = UpstoxBroker(access_token="dummy-upstox-tok")
        broker._connected = True
        broker._sdk = None
        broker._order_api = None

        with patch("urllib.request.urlopen", side_effect=fake_broker_urlopen):
            ws_uri = broker.authorize_ws_feed()
            self.assertIn("/v3/feed/market-data-feed", ws_uri)

            placed: BrokerOrder = broker.place_order(
                BrokerOrderRequest(
                    symbol="RELIANCE",
                    side="BUY",
                    quantity=2,
                    order_type="LIMIT",
                    price=2950.0,
                    instrument_key="NSE_EQ|INE002A01018",
                )
            )
            self.assertEqual(placed.order_id, "261118000012345")
            self.assertEqual(placed.status, OrderStatus.PENDING)
            self.assertTrue(broker.modify_order("261118000012345", price=2955.0))
            self.assertTrue(broker.cancel_order("261118000012345"))

        self.assertIn(("POST", "https://api-hft.upstox.com/v3/order/place"), order_calls)
        self.assertIn(("PUT", "https://api-hft.upstox.com/v3/order/modify"), order_calls)
        self.assertIn(("DELETE", "https://api-hft.upstox.com/v3/order/cancel?order_id=261118000012345"), order_calls)

    def test_upstox_index_encoding_fallback_and_primary_failover_reason(self) -> None:
        import io
        import json as _json
        from core.feed_manager import _fetch_live_quotes_upstox

        seen_urls: list[str] = []

        class _FakeJsonResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=5):
            url = req.full_url
            seen_urls.append(url)
            # Must encode "Nifty 50" with %20 instead of +
            self.assertNotIn("Nifty+50", url)
            if "/v3/market-quote/ohlc" in url:
                # Simulate empty OHLC before market open -> falls back to /v3/market-quote/ltp
                return _FakeJsonResp(_json.dumps({"status": "success", "data": {}}).encode("utf-8"))
            if "/v3/market-quote/ltp" in url:
                return _FakeJsonResp(
                    _json.dumps(
                        {
                            "status": "success",
                            "data": {
                                "NSE_INDEX:Nifty 50": {
                                    "last_price": 24150.25,
                                    "instrument_token": "NSE_INDEX|Nifty 50",
                                    "cp": 24100.0,
                                    "volume": 0,
                                }
                            },
                        }
                    ).encode("utf-8")
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with (
            patch("core.feed_manager.upstox_auth.get_valid_token", return_value="dummy-tok"),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            quotes = _fetch_live_quotes_upstox(["NIFTY"])

        self.assertTrue(any("NSE_INDEX%7CNifty%2050" in u for u in seen_urls))
        self.assertIn("NIFTY", quotes)
        self.assertEqual(quotes["NIFTY"]["ltp"], 24150.25)
        self.assertEqual(quotes["NIFTY"]["transport"], "v3_ltp")

        # Verify poll_ticks keeps Upstox's actual error when Zerodha standby also fails
        with tempfile.TemporaryDirectory() as tmpdir:
            trail = AuditTrail(Path(tmpdir) / "audit_chain.db")
            mgr = HotStandbyFeedManager(
                primary_id="upstox",
                audit=trail,
                fetchers={
                    "upstox": lambda _s: (_ for _ in ()).throw(RuntimeError("Upstox HTTP 400: Invalid instrument_key")),
                    "zerodha": lambda _s: (_ for _ in ()).throw(RuntimeError("No valid Zerodha Kite session token for the current trading day.")),
                    "yfinance": lambda syms: {
                        s: {"symbol": s, "ltp": 24100.0, "change_pct": 0.1, "source": "yfinance", "ts_ist": "2026-11-18T10:15:00+05:30"}
                        for s in syms
                    },
                },
                watchlist_file=Path(tmpdir) / "watchlist.json",
            )
            res = mgr.poll_ticks(["NIFTY"])
            self.assertTrue(res["ok"])
            self.assertEqual(res["active_feed"], "yfinance")
            self.assertIn("Upstox HTTP 400: Invalid instrument_key", res["message"])
            self.assertNotIn("No valid Zerodha Kite session token", res["message"])

    def test_phase5_screener_and_gap_only_cache(self) -> None:
        from core.analytics import MarketScreener

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = HistoricalDataManager(Path(tmpdir) / "ohlcv.db")
            screener = MarketScreener(hist_manager=mgr)

            api_calls = 0

            origin = datetime.now(tz=IST_TZ).date() - timedelta(days=500)

            def fake_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                nonlocal api_calls
                api_calls += 1
                rows = []
                cur = c_start
                base = 1000.0 if sym == "TRENDCO" else 800.0
                while cur <= c_end:
                    if cur.weekday() < 5:
                        day_idx = max(0, (cur - origin).days)
                        if sym == "TRENDCO":
                            close_px = base + day_idx * 4.5
                            if day_idx > 200:
                                close_px += (day_idx - 200) * 6.0
                        else:
                            close_px = max(25.0, base * (0.995 ** day_idx))
                        rows.append(
                            {
                                "ts_ist": f"{cur.isoformat()}T15:30:00+05:30",
                                "trade_date": cur.isoformat(),
                                "open": close_px - 1.5,
                                "high": close_px + 2.5,
                                "low": max(1.0, close_px - 3.0),
                                "close": round(close_px, 2),
                                "volume": 250000 if day_idx > 250 else 100000,
                                "oi": 0,
                            }
                        )
                    cur += timedelta(days=1)
                return rows

            first = screener.scan(
                symbols=["TRENDCO", "DIPCO"],
                exchange="NSE",
                timeframe="1day",
                lookback_days=140,
                preset="all",
                provider="upstox",
                fetcher=fake_fetcher,
            )
            self.assertTrue(first["ok"])
            self.assertEqual(first["evaluated_count"], 2)
            self.assertGreater(api_calls, 0)
            by_sym = {r["symbol"]: r for r in first["results"]}
            self.assertEqual(by_sym["TRENDCO"]["supertrend"], "BULL")
            self.assertIsNotNone(by_sym["TRENDCO"]["rsi14"])
            self.assertLess(by_sym["DIPCO"]["rsi14"], 35.0)

            # Oversold preset catches DIPCO
            calls_before_second = api_calls
            oversold = screener.scan(
                symbols=["TRENDCO", "DIPCO"],
                exchange="NSE",
                timeframe="1day",
                lookback_days=140,
                preset="oversold_rebound",
                provider="upstox",
                fetcher=fake_fetcher,
            )
            self.assertEqual(oversold["matched_count"], 1)
            self.assertEqual(oversold["results"][0]["symbol"], "DIPCO")
            self.assertEqual(api_calls, calls_before_second)  # Gap-only cache hit!

    def test_phase5_backtest_engine_strategies_and_metrics(self) -> None:
        from core.analytics import BacktestEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = HistoricalDataManager(Path(tmpdir) / "ohlcv.db")
            engine = BacktestEngine(hist_manager=mgr)

            def wave_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                rows = []
                cur = c_start
                i = 0
                while cur <= c_end:
                    if cur.weekday() < 5:
                        # Down-then-up trend so EMA crossover & RSI & Supertrend fire cleanly
                        px = 1000.0 - i * 4.0 if i < 35 else (860.0 + (i - 35) * 8.5)
                        rows.append(
                            {
                                "ts_ist": f"{cur.isoformat()}T15:30:00+05:30",
                                "trade_date": cur.isoformat(),
                                "open": px - 3.0,
                                "high": px + 5.0,
                                "low": px - 5.0,
                                "close": px,
                                "volume": 120000,
                                "oi": 0,
                            }
                        )
                        i += 1
                    cur += timedelta(days=1)
                return rows

            today_d = datetime.now(tz=IST_TZ).date()
            from_d = (today_d - timedelta(days=150)).isoformat()
            to_d = today_d.isoformat()

            for strat in ("ema_crossover", "rsi_mean_reversion", "supertrend_atr", "vcp_breakout"):
                res = engine.run(
                    symbol="RELIANCE",
                    exchange="NSE",
                    strategy=strat,
                    timeframe="1day",
                    from_date=from_d,
                    to_date=to_d,
                    initial_capital=500000.0,
                    provider="upstox",
                    fetcher=wave_fetcher,
                )
                self.assertTrue(res["ok"])
                self.assertGreaterEqual(res["bars_evaluated"], 60)
                self.assertGreaterEqual(len(res["equity_curve"]), 60)
                self.assertGreaterEqual(res["max_drawdown_pct"], 0.0)
                if strat in ("ema_crossover", "rsi_mean_reversion", "supertrend_atr"):
                    self.assertGreaterEqual(res["total_trades"], 1)

    def test_phase5_risk_limits_breach_triggers_kill_switch_and_audit(self) -> None:
        from core.analytics import RiskLimitManager
        from core.preflight import KillSwitchController

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trail = AuditTrail(root / "audit_chain.db")
            ks = KillSwitchController(state_file=root / "kill_switch.json", audit=trail)
            risk_mgr = RiskLimitManager(
                limits_file=root / "risk_limits.json",
                audit=trail,
                kill_switch=ks,
            )

            saved = risk_mgr.save_limits(
                {
                    "total_capital_inr": 500000.0,
                    "max_capital_per_trade_pct": 10.0,
                    "max_daily_loss_inr": 10000.0,
                    "max_open_positions": 3,
                    "max_gross_exposure_inr": 300000.0,
                    "auto_engage_kill_switch": True,
                }
            )
            self.assertEqual(saved["max_daily_loss_inr"], 10000.0)

            # Evaluate a portfolio breaching daily loss + max positions + gross exposure
            eval_res = risk_mgr.evaluate(
                trading_broker="upstox",
                funds={"available": 200000.0, "used": 300000.0},
                positions=[
                    {"symbol": "RELIANCE", "quantity": 50, "average_price": 2900.0, "last_price": 2800.0, "realized_pnl": 0.0, "unrealized_pnl": -5000.0},
                    {"symbol": "TCS", "quantity": 30, "average_price": 4000.0, "last_price": 3800.0, "realized_pnl": 0.0, "unrealized_pnl": -6000.0},
                    {"symbol": "INFY", "quantity": 40, "average_price": 1500.0, "last_price": 1480.0, "realized_pnl": 0.0, "unrealized_pnl": -800.0},
                    {"symbol": "SBIN", "quantity": 60, "average_price": 800.0, "last_price": 790.0, "realized_pnl": 0.0, "unrealized_pnl": -600.0},
                ],
                orders=[],
            )
            self.assertEqual(eval_res["status"], "BREACHED")
            self.assertTrue(eval_res["kill_switch_auto_triggered"])
            self.assertTrue(ks.is_engaged())
            rules = {b["rule"] for b in eval_res["breaches"]}
            self.assertIn("max_daily_loss_inr", rules)
            self.assertIn("max_open_positions", rules)
            self.assertIn("max_gross_exposure_inr", rules)
            self.assertTrue(trail.verify_chain()["valid"])

    def test_phase5_http_analytics_routes_and_async_jobs(self) -> None:
        import json as _json
        import threading
        import time as _time
        from web.local_server import _AlgoDeskHTTPServer, _StdlibBrokerHandler

        srv = _AlgoDeskHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analytics/meta", timeout=3) as r:
                meta = _json.loads(r.read().decode("utf-8"))
                self.assertEqual(r.status, 200)
                self.assertIn("ema_crossover", meta["strategies"])

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analytics/risk", timeout=3) as r:
                risk = _json.loads(r.read().decode("utf-8"))
                self.assertEqual(r.status, 200)
                self.assertIn("limits", risk)
                self.assertIn("telemetry", risk)

            # Submit an async backtest job against fake seeded data in hub_service's ohlcv
            with patch("backtest.engine.BacktestEngine._load_candles") as mock_loader:
                today_d = datetime.now(tz=IST_TZ).date()
                fake_rows = [
                    {
                        "ts_ist": f"{(today_d - timedelta(days=90 - i)).isoformat()}T15:30:00+05:30",
                        "trade_date": (today_d - timedelta(days=90 - i)).isoformat(),
                        "open": 100.0 + i,
                        "high": 102.0 + i,
                        "low": 99.0 + i,
                        "close": 101.0 + i,
                        "volume": 10000,
                        "oi": 0,
                        "source": "upstox",
                    }
                    for i in range(60)
                ]
                mock_loader.return_value = (
                    fake_rows,
                    {"symbol": "RELIANCE", "exchange": "NSE", "timeframe": "1day", "api_calls_made": 0, "existing_days_skipped": 60},
                )
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/analytics/backtest",
                    data=_json.dumps({"symbol": "RELIANCE", "strategy": "rsi_mean_reversion", "async_job": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=4) as r:
                    started = _json.loads(r.read().decode("utf-8"))
                    self.assertTrue(started["async"])
                    job_id = started["job"]["job_id"]

                # Poll job until completion
                final_job = None
                for _ in range(30):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analytics/jobs/{job_id}", timeout=3) as jr:
                        jb = _json.loads(jr.read().decode("utf-8"))["job"]
                        if jb["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                            final_job = jb
                            break
                    _time.sleep(0.05)

                self.assertIsNotNone(final_job)
                self.assertEqual(final_job["status"], "COMPLETED")
                self.assertEqual(final_job["result"]["symbol"], "RELIANCE")

            # Also verify new analytics meta fields & reload endpoint
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analytics/meta", timeout=3) as r:
                meta_full = _json.loads(r.read().decode("utf-8"))
                self.assertIn("screens", meta_full)
                self.assertIn("strategy_specs", meta_full)
                self.assertIn("universes", meta_full)
                self.assertIn("storage", meta_full)
                self.assertIn("indicator_backends", meta_full)

            reload_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/analytics/reload",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(reload_req, timeout=3) as r:
                reloaded = _json.loads(r.read().decode("utf-8"))
                self.assertTrue(reloaded["ok"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_modular_indicators_toolkit_and_backends(self) -> None:
        from indicators.toolkit import IndicatorToolkit, available_backends

        backends = available_backends()
        self.assertTrue(backends["builtin"])

        candles = []
        base_d = date(2026, 7, 1)
        for i in range(60):
            px = 100.0 + i * 1.5 + (2.0 if i % 2 == 0 else -1.0)
            candles.append(
                {
                    "trade_date": (base_d + timedelta(days=i)).isoformat(),
                    "open": px - 1.0,
                    "high": px + 2.5,
                    "low": px - 2.0,
                    "close": px,
                    "volume": 100000 + i * 1000,
                }
            )
        tk = IndicatorToolkit(candles, backend="builtin")
        sma20 = tk.sma(20)
        ema20 = tk.ema(20)
        rsi14 = tk.rsi(14)
        atr14, atr_pct = tk.atr(14)
        macd = tk.macd()
        bb = tk.bbands(20, 2.0)
        adx = tk.adx(14)
        stoch = tk.stoch(14, 3)
        roc10 = tk.roc(10)
        st_dirs, _ = tk.supertrend(10, 3.0)

        self.assertIsNotNone(sma20[-1])
        self.assertIsNotNone(ema20[-1])
        self.assertGreater(rsi14[-1], 55.0)
        self.assertGreater(atr14[-1], 0.0)
        self.assertGreater(atr_pct[-1], 0.0)
        self.assertIsNotNone(macd["hist"][-1])
        self.assertGreater(bb["upper"][-1], bb["middle"][-1])
        self.assertGreater(bb["middle"][-1], bb["lower"][-1])
        self.assertIsNotNone(adx["adx"][-1])
        self.assertIsNotNone(stoch["k"][-1])
        self.assertIsNotNone(roc10[-1])
        self.assertEqual(st_dirs[-1], 1)

    def test_hot_reload_custom_screen_file_and_error_isolation(self) -> None:
        import time as _time
        from screener.registry import ScreenRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            sdir = Path(tmpdir)
            reg = ScreenRegistry(screens_dir=sdir)
            self.assertEqual(reg.list_screens(), [])

            custom_py = sdir / "my_custom_macd.py"
            custom_py.write_text(
                (
                    "from screener.base import BaseScreen, ParamSpec, ScreenMatch\n"
                    "class MyCustomMacd(BaseScreen):\n"
                    "    id = 'my_custom_macd'\n"
                    "    name = 'Custom MACD Cross'\n"
                    "    params = [ParamSpec('fast', 'Fast', 'int', 12, min_val=2, max_val=50)]\n"
                    "    def evaluate(self, candles, params, toolkit):\n"
                    "        m = toolkit.macd(fast=int(params['fast']))\n"
                    "        return ScreenMatch(matched=(m['hist'][-1] or 0) > 0, signal='MACD_POS', tags=['MACD'], score=25)\n"
                ),
                encoding="utf-8",
            )
            screens = reg.list_screens()
            self.assertEqual(len(screens), 1)
            self.assertEqual(screens[0]["id"], "my_custom_macd")
            self.assertEqual(screens[0]["params"][0]["default"], 12)

            # Edit parameter default on disk -> auto-reloads on mtime change
            _time.sleep(0.02)
            custom_py.write_text(
                custom_py.read_text(encoding="utf-8").replace("12, min_val", "8, min_val"),
                encoding="utf-8",
            )
            screens_reloaded = reg.list_screens()
            self.assertEqual(screens_reloaded[0]["params"][0]["default"], 8)

            # Drop broken syntax file -> isolated in get_errors() without breaking valid screens
            (sdir / "broken_screen.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
            screens_after_err = reg.list_screens()
            self.assertEqual(len(screens_after_err), 1)
            errs = reg.get_errors()
            self.assertIn("broken_screen.py", errs)

    def test_as_of_date_zero_lookahead_and_universe_params(self) -> None:
        from core.instruments import InstrumentIndex, InstrumentRecord
        from screener.engine import ScreenerEngine
        from screener.universes import UniverseResolver

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hist = HistoricalDataManager(db_path=root / "ohlcv.db")
            instr = InstrumentIndex(db_path=root / "instruments.db")
            instr.upsert_records(
                "upstox",
                [
                    InstrumentRecord(
                        broker_id="upstox",
                        exchange="NFO",
                        segment="NSE_FO",
                        trading_symbol=f"SYM{i}26NOVFUT",
                        asset_symbol=f"SYM{i}",
                        name=f"SYM {i} FUT",
                        instrument_type="FUT",
                        instrument_key=f"NSE_FO|SYM{i}",
                        expiry="2026-11-26",
                    )
                    for i in range(25)
                ],
            )
            uni = UniverseResolver(instr_idx=instr, hist_mgr=hist)
            nifty50 = uni.resolve("NIFTY_50")
            self.assertGreaterEqual(len(nifty50["symbols"]), 50)
            fno = uni.resolve("NIFTY_FNO")
            self.assertEqual(len(fno["symbols"]), 25)
            self.assertIn("SYM0", fno["symbols"])
            n500 = uni.resolve("NIFTY_500")
            self.assertGreater(len(n500["symbols"]), 250)

            # Seed candles for ASOFCO: strong rally until 2026-10-15, then crash 2026-10-16..2026-11-10
            rows = []
            cur = date(2026, 7, 1)
            cutoff = date(2026, 10, 15)
            end_d = date(2026, 11, 10)
            px = 100.0
            while cur <= end_d:
                if cur.weekday() < 5:
                    px = px + 3.0 if cur <= cutoff else max(40.0, px - 12.0)
                    rows.append(
                        {
                            "ts_ist": f"{cur.isoformat()}T15:30:00+05:30",
                            "trade_date": cur.isoformat(),
                            "open": px - 1.0,
                            "high": px + 1.5,
                            "low": px - 1.5,
                            "close": px,
                            "volume": 200000,
                            "oi": 0,
                        }
                    )
                cur += timedelta(days=1)

            hist.download(
                symbol="ASOFCO",
                exchange="NSE",
                timeframe="1day",
                from_date="2026-07-01",
                to_date="2026-11-10",
                provider="upstox",
                fetcher=lambda *args: rows,
            )
            self.assertEqual(hist.list_cached_symbols(exchange="NSE", timeframe="1day"), ["ASOFCO"])
            self.assertEqual(hist.list_cached_symbols(exchange="NSE", timeframe="30minute"), [])
            cached_uni = uni.resolve("CACHED_SQLITE", exchange="NSE", timeframe="1day")
            self.assertEqual(cached_uni["symbols"], ["ASOFCO"])
            self.assertGreaterEqual(len(uni.list_universes(exchange="NSE", timeframe="1day")), 7)

            engine = ScreenerEngine(hist_manager=hist, universes=uni)
            past_scan = engine.scan(
                symbols=["ASOFCO"],
                universe="CUSTOM",
                exchange="NSE",
                timeframe="1day",
                lookback_days=120,
                as_of_date="2026-10-15",
                screen_id="all_metrics",
                provider="upstox",
                fetcher=lambda *args: [],
            )
            self.assertEqual(past_scan["as_of_date"], "2026-10-15")
            self.assertEqual(past_scan["evaluated_count"], 1)
            row_past = past_scan["results"][0]
            self.assertLessEqual(row_past["last_date"], "2026-10-15")
            self.assertGreater(row_past["rsi14"], 75.0)
            self.assertEqual(row_past["supertrend"], "BULL")

            # Now test editable parameters on oversold_rebound against late scan
            late_scan = engine.scan(
                symbols=["ASOFCO"],
                universe="CUSTOM",
                exchange="NSE",
                timeframe="1day",
                lookback_days=120,
                as_of_date="2026-11-10",
                screen_id="oversold_rebound",
                params={"rsi_oversold": 35.0, "require_positive_bounce": False},
                provider="upstox",
                fetcher=lambda *args: [],
            )
            self.assertEqual(late_scan["matched_count"], 1)

    def test_risk_validate_order_and_job_cancel_and_hybrid_storage(self) -> None:
        from broker.upstox.broker import UpstoxBroker
        from core.jobs import AsyncJobRunner
        from core.preflight import KillSwitchController
        from core.runtime import CanonicalSessionManager, OrderRequest as RuntimeOrderReq, TradingState
        from risk.limits import RiskLimitManager

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hist = HistoricalDataManager(db_path=root / "ohlcv.db")
            st = hist.storage_info()
            self.assertIn(st["active_mode"], {"hybrid_parquet", "sqlite_fallback"})

            # Pre-trade RiskLimitManager validation wired into CanonicalSessionManager
            trail = AuditTrail(root / "audit.db")
            ks = KillSwitchController(state_file=root / "ks.json", audit=trail)
            risk_mgr = RiskLimitManager(limits_file=root / "risk.json", audit=trail, kill_switch=ks)
            risk_mgr.save_limits(
                {
                    "total_capital_inr": 100000.0,
                    "max_capital_per_trade_pct": 10.0,  # cap ₹10,000 per trade
                    "max_gross_exposure_inr": 50000.0,
                    "max_open_positions": 2,
                }
            )
            broker = UpstoxBroker(access_token="dummy")
            sm = CanonicalSessionManager(
                state=TradingState(),
                broker=broker,
                risk_engine=risk_mgr,
                kill_switch=ks,
                audit=trail,
            )
            with self.assertRaises(RuntimeError) as ctx:
                sm.submit_signal_order(
                    RuntimeOrderReq(trading_symbol="RELIANCE", exchange="NSE", transaction_type="BUY", quantity=10, price=2500.0)
                )
            self.assertIn("Pre-trade risk limit rejected", str(ctx.exception))

            # Cooperative AsyncJobRunner cancel_job
            import threading
            import time as _time

            runner = AsyncJobRunner(max_workers=1)
            started_ev = threading.Event()

            def long_task(progress_cb, cancel_ev):
                started_ev.set()
                for idx in range(100):
                    if cancel_ev.is_set():
                        from core.jobs import JobCancelledError
                        raise JobCancelledError("Stopped mid-scan")
                    progress_cb(idx, 100, f"step {idx}")
                    _time.sleep(0.02)
                return {"ok": True}

            jb = runner.submit("screener", {}, long_task)
            started_ev.wait(timeout=2.0)
            cancelled = runner.cancel_job(jb["job_id"])
            self.assertEqual(cancelled["status"], "CANCELLED")

    def test_canonical_1m_1d_download_and_local_ist_resampling(self) -> None:
        from core.historical import resample_ohlcv_rows, resolve_base_timeframe

        self.assertEqual(resolve_base_timeframe("1minute"), "1minute")
        self.assertEqual(resolve_base_timeframe("5minute"), "1minute")
        self.assertEqual(resolve_base_timeframe("15minute"), "1minute")
        self.assertEqual(resolve_base_timeframe("30minute"), "1minute")
        self.assertEqual(resolve_base_timeframe("60minute"), "1minute")
        self.assertEqual(resolve_base_timeframe("75minute"), "1minute")
        self.assertEqual(resolve_base_timeframe("1day"), "1day")
        self.assertEqual(resolve_base_timeframe("1week"), "1day")
        self.assertEqual(resolve_base_timeframe("1month"), "1day")

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = HistoricalDataManager(Path(tmpdir) / "ohlcv.db")
            seen_tfs: list[str] = []

            def fake_1m_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                seen_tfs.append(tf)
                rows = []
                cur = c_start
                while cur <= c_end:
                    if cur.weekday() < 5:
                        # 15 one-minute bars from 09:15 to 09:29 IST
                        for m in range(15):
                            hh = 9 + (15 + m) // 60
                            mm = (15 + m) % 60
                            base = 100.0 + m
                            rows.append(
                                {
                                    "ts_ist": f"{cur.isoformat()}T{hh:02d}:{mm:02d}:00+05:30",
                                    "trade_date": cur.isoformat(),
                                    "open": base,
                                    "high": base + 2.0,
                                    "low": base - 1.0,
                                    "close": base + 0.5,
                                    "volume": 1000 + m * 10,
                                    "oi": 5000 + m,
                                }
                            )
                    cur += timedelta(days=1)
                return rows

            # Request 15minute -> must call fetcher strictly with "1minute"
            dl = mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="15minute",
                from_date="2026-10-05",
                to_date="2026-10-06",
                provider="yfinance",
                fetcher=fake_1m_fetcher,
            )
            self.assertEqual(seen_tfs, ["1minute"])
            self.assertEqual(dl["base_timeframe"], "1minute")
            self.assertTrue(dl["resampled"])
            self.assertEqual(dl["api_calls_made"], 1)

            # Reading 5minute and 15minute from cache requires 0 extra API calls
            dl_5m = mgr.download(
                symbol="RELIANCE",
                exchange="NSE",
                timeframe="5minute",
                from_date="2026-10-05",
                to_date="2026-10-06",
                provider="yfinance",
                fetcher=fake_1m_fetcher,
            )
            self.assertEqual(dl_5m["api_calls_made"], 0)
            self.assertTrue(dl_5m["cache_hit_only"])

            rec_5m = mgr.get_records(symbol="RELIANCE", exchange="NSE", timeframe="5minute", view="full")
            rec_15m = mgr.get_records(symbol="RELIANCE", exchange="NSE", timeframe="15minute", view="full")
            # 2 trading days * 15 mins = 6 five-minute bars and 2 fifteen-minute bars
            self.assertEqual(rec_5m["total_rows"], 6)
            self.assertEqual(rec_15m["total_rows"], 2)
            self.assertTrue(rec_15m["resampled"])
            self.assertEqual(rec_15m["base_timeframe"], "1minute")

            first_15m = rec_15m["rows"][0]
            self.assertEqual(first_15m["ts_ist"], "2026-10-05T09:15:00+05:30")
            self.assertEqual(first_15m["open"], 100.0)
            self.assertEqual(first_15m["high"], 116.0)   # m=14 -> 114 + 2
            self.assertEqual(first_15m["low"], 99.0)     # m=0 -> 100 - 1
            self.assertEqual(first_15m["close"], 114.5)  # m=14 -> 114 + 0.5
            self.assertEqual(first_15m["volume"], sum(1000 + m * 10 for m in range(15)))
            self.assertEqual(first_15m["oi"], 5014)      # last bar's OI

            # Verify 1day -> 1week & 1month resampling fetches strictly "1day"
            seen_daily_tfs: list[str] = []

            def fake_1d_fetcher(sym: str, exch: str, tf: str, c_start: date, c_end: date):
                seen_daily_tfs.append(tf)
                rows = []
                cur = c_start
                i = 0
                while cur <= c_end:
                    if cur.weekday() < 5:
                        rows.append(
                            {
                                "ts_ist": f"{cur.isoformat()}T15:30:00+05:30",
                                "trade_date": cur.isoformat(),
                                "open": 200.0 + i,
                                "high": 205.0 + i,
                                "low": 195.0 + i,
                                "close": 202.0 + i,
                                "volume": 10000,
                                "oi": 0,
                            }
                        )
                        i += 1
                    cur += timedelta(days=1)
                return rows

            dl_wk = mgr.download(
                symbol="TCS",
                exchange="NSE",
                timeframe="1week",
                from_date="2026-10-05",
                to_date="2026-10-16",
                provider="yfinance",
                fetcher=fake_1d_fetcher,
            )
            self.assertEqual(seen_daily_tfs, ["1day"])
            self.assertEqual(dl_wk["base_timeframe"], "1day")
            self.assertTrue(dl_wk["resampled"])

            rec_wk = mgr.get_records(symbol="TCS", exchange="NSE", timeframe="1week", view="full")
            self.assertEqual(rec_wk["total_rows"], 2)  # 2 ISO weeks (Oct 5-9, Oct 12-16)
            self.assertEqual(rec_wk["rows"][0]["volume"], 50000)

            rec_mo = mgr.get_records(symbol="TCS", exchange="NSE", timeframe="1month", view="full")
            self.assertEqual(rec_mo["total_rows"], 1)  # All in Oct 2026
            self.assertEqual(rec_mo["rows"][0]["volume"], 100000)

            # Arbitrary positive integer minute & day timeframes (e.g. 7minute, 3day, 5day) -> 0 extra API calls
            self.assertEqual(resolve_base_timeframe("2minute"), "1minute")
            self.assertEqual(resolve_base_timeframe("7minute"), "1minute")
            self.assertEqual(resolve_base_timeframe("3day"), "1day")
            self.assertEqual(resolve_base_timeframe("5day"), "1day")

            rec_7m = mgr.get_records(symbol="RELIANCE", exchange="NSE", timeframe="7minute", view="full")
            # 15 mins per day -> buckets [09:15..09:21], [09:22..09:28], [09:29] = 3 bars/day * 2 days = 6 bars
            self.assertEqual(rec_7m["total_rows"], 6)
            self.assertEqual(rec_7m["base_timeframe"], "1minute")
            self.assertTrue(rec_7m["resampled"])

            rec_3d = mgr.get_records(symbol="TCS", exchange="NSE", timeframe="3day", view="full")
            # 10 trading days -> 4 three-day bars (3+3+3+1)
            self.assertEqual(rec_3d["total_rows"], 4)
            self.assertEqual(rec_3d["rows"][0]["volume"], 30000)
            self.assertEqual(rec_3d["rows"][-1]["volume"], 10000)
            self.assertEqual(rec_3d["base_timeframe"], "1day")
            self.assertTrue(rec_3d["resampled"])

            rec_5d = mgr.get_records(symbol="TCS", exchange="NSE", timeframe="5day", view="full")
            self.assertEqual(rec_5d["total_rows"], 2)
            self.assertEqual(rec_5d["rows"][0]["volume"], 50000)

            # Pure resample_ohlcv_rows sanity for MCX 09:00 anchor
            mcx_rows = [
                {"ts_ist": "2026-10-05T09:00:00+05:30", "trade_date": "2026-10-05", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 50, "oi": 100},
                {"ts_ist": "2026-10-05T09:04:00+05:30", "trade_date": "2026-10-05", "open": 11, "high": 15, "low": 10, "close": 14, "volume": 60, "oi": 120},
                {"ts_ist": "2026-10-05T09:05:00+05:30", "trade_date": "2026-10-05", "open": 14, "high": 16, "low": 13, "close": 15, "volume": 70, "oi": 130},
            ]
            mcx_5m = resample_ohlcv_rows(mcx_rows, "5minute", exchange="MCX")
            self.assertEqual(len(mcx_5m), 2)
            self.assertEqual(mcx_5m[0]["ts_ist"], "2026-10-05T09:00:00+05:30")
            self.assertEqual(mcx_5m[0]["volume"], 110)

    def test_indicator_golden_verification_and_http_endpoint(self) -> None:
        import json as _json
        import threading
        from indicators.verify import verify_golden_indicators
        from web.local_server import _AlgoDeskHTTPServer, _StdlibBrokerHandler

        golden = verify_golden_indicators()
        self.assertTrue(golden["ok"], f"Golden indicator checks failed: {golden}")
        self.assertGreaterEqual(golden["total_count"], 7)
        self.assertEqual(golden["passed_count"], golden["total_count"])

        srv = _AlgoDeskHTTPServer(("127.0.0.1", 0), _StdlibBrokerHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/analytics/indicators/verify", timeout=5) as r:
                body = _json.loads(r.read().decode("utf-8"))
                self.assertEqual(r.status, 200)
                self.assertTrue(body["ok"])
                self.assertTrue(body["golden_reference"]["ok"])
                self.assertIn("available_backends", body["backend_parity"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_auto_lookback_and_dynamic_per_screen_columns(self) -> None:
        from screener.engine import ScreenerEngine, compute_auto_lookback_days
        from screener.registry import ScreenRegistry

        # 1) Auto-lookback scaling by required_bars and timeframe
        d_short = compute_auto_lookback_days(required_bars=50, timeframe="1day")
        d_long = compute_auto_lookback_days(required_bars=250, timeframe="1day")
        self.assertGreater(d_long, d_short)
        d_1m = compute_auto_lookback_days(required_bars=100, timeframe="1minute")
        d_15m = compute_auto_lookback_days(required_bars=100, timeframe="15minute")
        d_1w = compute_auto_lookback_days(required_bars=60, timeframe="1week")
        self.assertGreater(d_15m, d_1m)
        self.assertGreater(d_1w, 300)

        # 2) Dynamic columns per screen (all_metrics vs vcp_contraction vs trend_following vs custom screen)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hist = HistoricalDataManager(db_path=root / "ohlcv.db")

            rows = []
            cur = date(2026, 3, 1)
            end_d = date(2026, 10, 15)
            px = 500.0
            i = 0
            while cur <= end_d:
                if cur.weekday() < 5:
                    px += 2.0 + (0.5 if i % 3 == 0 else -0.2)
                    rows.append(
                        {
                            "ts_ist": f"{cur.isoformat()}T15:30:00+05:30",
                            "trade_date": cur.isoformat(),
                            "open": px - 1.0,
                            "high": px + 2.0,
                            "low": px - 2.0,
                            "close": px,
                            "volume": 150000 + i * 500,
                            "oi": 0,
                        }
                    )
                    i += 1
                cur += timedelta(days=1)

            hist.download(
                symbol="DYNCO",
                exchange="NSE",
                timeframe="1day",
                from_date="2026-03-01",
                to_date="2026-10-15",
                provider="upstox",
                fetcher=lambda *args: rows,
            )

            engine = ScreenerEngine(hist_manager=hist)

            # Default all_metrics keeps the standard watchlist columns with live param labels
            scan_all = engine.scan(
                symbols=["DYNCO"],
                exchange="NSE",
                timeframe="1day",
                as_of_date="2026-10-15",
                screen_id="all_metrics",
                params={"ema_fast": 15, "ema_slow": 40, "rsi_period": 10},
                provider="upstox",
                fetcher=lambda *args: [],
            )
            self.assertEqual(scan_all["lookback_mode"], "auto")
            self.assertGreater(scan_all["lookback_days"], 0)
            all_keys = [c["key"] for c in scan_all["columns"]]
            all_labels = [c["label"] for c in scan_all["columns"]]
            self.assertEqual(all_keys, ["ema20", "ema50", "rsi14", "atr_pct", "vol_ratio", "supertrend"])
            self.assertIn("EMA 15", all_labels)
            self.assertIn("EMA 40", all_labels)
            self.assertIn("RSI (10)", all_labels)

            # VCP screen exposes VCP-specific contraction & breakout columns
            scan_vcp = engine.scan(
                symbols=["DYNCO"],
                exchange="NSE",
                timeframe="1day",
                as_of_date="2026-10-15",
                screen_id="vcp_contraction",
                provider="upstox",
                fetcher=lambda *args: [],
            )
            vcp_keys = [c["key"] for c in scan_vcp["columns"]]
            self.assertIn("range_7d_pct", vcp_keys)
            self.assertIn("contraction_ratio", vcp_keys)
            self.assertIn("dist_50d_high_pct", vcp_keys)
            self.assertNotIn("rsi14", vcp_keys)

            # Trend-following screen exposes ADX, +DI, -DI, EMA, Supertrend columns
            scan_trend = engine.scan(
                symbols=["DYNCO"],
                exchange="NSE",
                timeframe="1day",
                as_of_date="2026-10-15",
                screen_id="trend_following",
                params={"adx_period": 21, "ema_fast": 10, "ema_slow": 30},
                provider="upstox",
                fetcher=lambda *args: [],
            )
            trend_keys = [c["key"] for c in scan_trend["columns"]]
            trend_labels = [c["label"] for c in scan_trend["columns"]]
            self.assertIn("adx", trend_keys)
            self.assertIn("plus_di", trend_keys)
            self.assertIn("minus_di", trend_keys)
            self.assertIn("ADX (21)", trend_labels)
            self.assertIn("EMA 10", trend_labels)

            # Custom hot-reloaded .py screen WITHOUT get_columns() auto-infers columns from metrics
            sdir = root / "custom_screens"
            sdir.mkdir()
            (sdir / "custom_roc.py").write_text(
                (
                    "from screener.base import BaseScreen, ParamSpec, ScreenMatch\n"
                    "class CustomRocScreen(BaseScreen):\n"
                    "    id = 'custom_roc'\n"
                    "    name = 'Custom ROC'\n"
                    "    params = [ParamSpec('roc_period', 'ROC Period', 'int', 12, min_val=2, max_val=60)]\n"
                    "    def evaluate(self, candles, params, toolkit):\n"
                    "        roc_v = toolkit.roc(int(params['roc_period']))[-1]\n"
                    "        return ScreenMatch(\n"
                    "            matched=True,\n"
                    "            signal='ROC_UP',\n"
                    "            tags=['ROC'],\n"
                    "            score=30,\n"
                    "            metrics={'roc_pct': round(roc_v or 0.0, 2), 'custom_ratio': 1.85},\n"
                    "        )\n"
                ),
                encoding="utf-8",
            )
            custom_engine = ScreenerEngine(hist_manager=hist, registry=ScreenRegistry(screens_dir=sdir))
            scan_custom = custom_engine.scan(
                symbols=["DYNCO"],
                exchange="NSE",
                timeframe="1day",
                as_of_date="2026-10-15",
                screen_id="custom_roc",
                provider="upstox",
                fetcher=lambda *args: [],
            )
            custom_keys = [c["key"] for c in scan_custom["columns"]]
            self.assertIn("roc_pct", custom_keys)
            self.assertIn("custom_ratio", custom_keys)
            self.assertEqual(scan_custom["results"][0]["custom_ratio"], 1.85)

    def test_slow_ema_233_not_clamped_and_full_cache_convergence(self) -> None:
        from indicators.toolkit import compute_ema_series
        from screener.engine import ScreenerEngine
        from screener.registry import ScreenRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hist = HistoricalDataManager(root / "hist.db", parquet_dir=root / "parquet")
            engine = ScreenerEngine(hist_manager=hist, registry=ScreenRegistry())

            # Seed 1,300+ weekdays (~5 years) of daily candles locally, including future bars
            # past as_of_date to verify both full-history warmup AND strict zero-lookahead.
            start_d = date(2020, 1, 1)
            end_d = date(2026, 6, 30)
            as_of = "2026-03-31"

            full_rows = []
            cur = start_d
            idx = 0
            while cur <= end_d:
                if cur.weekday() < 5:
                    # Non-linear synthetic trend + regime shift so truncated EMA differs visibly
                    px = round(1000.0 + idx * 0.8 + (250.0 if idx > 600 else 0.0) + ((idx % 17) - 8) * 3.2, 2)
                    full_rows.append(
                        {
                            "ts_ist": f"{cur.isoformat()}T15:30:00+05:30",
                            "trade_date": cur.isoformat(),
                            "open": round(px - 2.0, 2),
                            "high": round(px + 5.0, 2),
                            "low": round(px - 5.0, 2),
                            "close": px,
                            "volume": 200000 + (idx % 20) * 5000,
                            "oi": 0,
                        }
                    )
                    idx += 1
                cur += timedelta(days=1)

            # Seed entire local span via download() so gap-only fetcher makes zero network calls on scan
            hist.download(
                symbol="EMACO",
                exchange="NSE",
                timeframe="1day",
                from_date=start_d.isoformat(),
                to_date=end_d.isoformat(),
                provider="upstox",
                fetcher=lambda s, e, tf, c0, c1: [
                    r for r in full_rows if c0.isoformat() <= r["trade_date"] <= c1.isoformat()
                ],
            )

            scan = engine.scan(
                symbols=["EMACO"],
                exchange="NSE",
                timeframe="1day",
                as_of_date=as_of,
                screen_id="all_metrics",
                params={"ema_fast": 21, "ema_slow": 233},
                provider="upstox",
                backend="builtin",
                fetcher=lambda *args: [],
            )
            self.assertTrue(scan["ok"])
            self.assertEqual(scan["params"]["ema_slow"], 233)
            self.assertEqual(scan["params"]["ema_fast"], 21)
            self.assertGreaterEqual(scan["required_bars"], 960)
            self.assertEqual(scan["api_calls_made"], 0)

            col_labels = [c["label"] for c in scan["columns"]]
            self.assertIn("EMA 233", col_labels)
            self.assertNotIn("EMA 200", col_labels)

            row = scan["results"][0]
            self.assertEqual(row["last_date"], as_of)

            # Reference EMA 233 over ALL bars <= as_of_date (must match to 0.00 error & exclude future bars)
            closes_up_to_as_of = [r["close"] for r in full_rows if r["trade_date"] <= as_of]
            self.assertEqual(row["bars"], len(closes_up_to_as_of))
            expected_ema233 = round(compute_ema_series(closes_up_to_as_of, 233)[-1], 2)
            self.assertAlmostEqual(row["ema50"], expected_ema233, places=2)

            # Verify other screens also accept >200 EMA periods without clamping
            for sid, pkey in (
                ("trend_following", "ema_slow"),
                ("momentum_breakout", "ema_slow"),
                ("vcp_contraction", "ema_support"),
            ):
                s_res = engine.scan(
                    symbols=["EMACO"],
                    exchange="NSE",
                    timeframe="1day",
                    as_of_date=as_of,
                    screen_id=sid,
                    params={pkey: 233},
                    provider="upstox",
                    backend="builtin",
                    fetcher=lambda *args: [],
                )
                self.assertEqual(s_res["params"][pkey], 233, f"{sid} clamped {pkey}")
                self.assertIn("EMA 233", [c["label"] for c in s_res["columns"]])


if __name__ == "__main__":
    unittest.main()
