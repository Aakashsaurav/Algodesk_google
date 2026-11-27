"""
tests/test_api_usage_counter.py
===============================
Unit tests for OutboundApiUsageTracker and global outbound third-party HTTP hooks:
- Rolling 1-minute window expiry & reset
- Rolling 1-hour window expiry & reset
- IST calendar-day rollover reset on each new day (00:00 IST)
- SQLite persistence across restarts
- Host classification across brokers, data providers, exchanges, Telegram, and unknown third parties
- Localhost / loopback exclusion
- Telegram bot token path redaction
- Low-level http.client.HTTPConnection.putrequest hook without mutating urllib.request.urlopen
"""

from __future__ import annotations

import http.client
import tempfile
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from config import IST_TZ
from core.api_usage import (
    OutboundApiUsageTracker,
    classify_third_party_host,
    install_outbound_api_hooks,
    is_local_host,
    sanitize_path,
)


class _FakeClock:
    def __init__(self, dt: datetime) -> None:
        self._ts = dt.timestamp()

    def __call__(self) -> float:
        return self._ts

    def advance(self, seconds: float) -> None:
        self._ts += float(seconds)


class TestOutboundApiUsageCounter(unittest.TestCase):
    def test_host_classification_and_localhost_exclusion(self) -> None:
        self.assertIsNone(classify_third_party_host("127.0.0.1"))
        self.assertIsNone(classify_third_party_host("localhost:3000"))
        self.assertIsNone(classify_third_party_host("::1"))
        self.assertTrue(is_local_host("127.0.0.1"))
        self.assertTrue(is_local_host("mybox.local"))

        self.assertEqual(classify_third_party_host("api.kite.trade")[:2], ("zerodha", "Zerodha Kite"))
        self.assertEqual(classify_third_party_host("api-hft.upstox.com")[:2], ("upstox", "Upstox"))
        self.assertEqual(classify_third_party_host("query1.finance.yahoo.com")[:2], ("yfinance", "Yahoo Finance"))
        self.assertEqual(classify_third_party_host("nsearchives.nseindia.com")[:2], ("nse_india", "NSE India"))
        self.assertEqual(classify_third_party_host("api.telegram.org")[:2], ("telegram", "Telegram"))
        self.assertEqual(classify_third_party_host("api.dhan.co")[:2], ("dhan", "Dhan"))
        # Unknown third-party host is auto-captured by registrable domain
        unknown = classify_third_party_host("v2.custom-data.io")
        self.assertIsNotNone(unknown)
        assert unknown is not None
        self.assertEqual(unknown[1], "custom-data.io")
        self.assertEqual(unknown[2], "Third-Party")

        # Sensitive Telegram bot tokens and query secrets are redacted
        self.assertEqual(
            sanitize_path("/bot123456:ABC-DEF_secret/sendMessage?chat_id=999"),
            "/bot***/sendMessage",
        )

    def test_minute_hour_and_ist_day_resets(self) -> None:
        # Start at 23:59:30 IST on 2026-11-26
        clock = _FakeClock(datetime(2026, 11, 26, 23, 59, 30, tzinfo=IST_TZ))
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "api_usage.db"
            tracker = OutboundApiUsageTracker(db_path=db, clock=clock)
            try:
                # Localhost ignored
                self.assertFalse(tracker.record_call(host="127.0.0.1", method="GET", path="/api/health"))

                # Record calls to Zerodha, Upstox, and Telegram
                self.assertTrue(tracker.record_call(host="api.kite.trade", method="GET", path="/quote"))
                self.assertTrue(tracker.record_call(host="api.kite.trade", method="POST", path="/orders/regular"))
                self.assertTrue(tracker.record_call(host="api.upstox.com", method="GET", path="/v3/market-quote/ltp"))
                self.assertTrue(
                    tracker.record_call(
                        host="api.telegram.org",
                        method="POST",
                        path="/bot999:SECRET/sendMessage",
                    )
                )

                snap0 = tracker.get_snapshot()
                self.assertEqual(snap0["current_day_ist"], "2026-11-26")
                self.assertEqual(snap0["totals"]["calls_1m"], 4)
                self.assertEqual(snap0["totals"]["calls_1h"], 4)
                self.assertEqual(snap0["totals"]["calls_1d"], 4)

                by_id0 = {p["id"]: p for p in snap0["providers"]}
                self.assertEqual(by_id0["zerodha"]["calls_1m"], 2)
                self.assertEqual(by_id0["zerodha"]["calls_1h"], 2)
                self.assertEqual(by_id0["zerodha"]["calls_1d"], 2)
                self.assertEqual(by_id0["telegram"]["last_path"], "/bot***/sendMessage")
                # Core baseline providers always visible even at 0
                self.assertIn("yfinance", by_id0)
                self.assertEqual(by_id0["yfinance"]["calls_1d"], 0)

                # Advance +35s -> 00:00:05 IST on 2026-11-27 (NEW DAY!)
                # Day counter MUST reset to 0 on new IST day, while 1m and 1h rolling windows still hold recent calls
                clock.advance(35.0)
                snap_new_day = tracker.get_snapshot()
                self.assertEqual(snap_new_day["current_day_ist"], "2026-11-27")
                self.assertEqual(snap_new_day["totals"]["calls_1d"], 0)
                self.assertEqual(snap_new_day["totals"]["calls_1m"], 4)
                self.assertEqual(snap_new_day["totals"]["calls_1h"], 4)

                # Make 1 new call on the new day
                tracker.record_call(host="query1.finance.yahoo.com", method="GET", path="/v8/finance/chart/RELIANCE.NS")
                snap_day2 = tracker.get_snapshot()
                self.assertEqual(snap_day2["totals"]["calls_1d"], 1)
                self.assertEqual(snap_day2["totals"]["calls_1m"], 5)

                # Advance +61s -> all 1-minute calls lapse and reset to 0, while 1h keeps all 5 and today keeps 1
                clock.advance(61.0)
                snap_after_1m = tracker.get_snapshot()
                self.assertEqual(snap_after_1m["totals"]["calls_1m"], 0)
                self.assertEqual(snap_after_1m["totals"]["calls_1h"], 5)
                self.assertEqual(snap_after_1m["totals"]["calls_1d"], 1)

                # Advance +3600s -> all 1-hour calls lapse and reset to 0, while today STILL holds 1
                clock.advance(3600.0)
                snap_after_1h = tracker.get_snapshot()
                self.assertEqual(snap_after_1h["totals"]["calls_1m"], 0)
                self.assertEqual(snap_after_1h["totals"]["calls_1h"], 0)
                self.assertEqual(snap_after_1h["totals"]["calls_1d"], 1)
            finally:
                tracker.close()

            # Verify SQLite persistence across tracker restart on same IST day
            tracker2 = OutboundApiUsageTracker(db_path=db, clock=clock)
            try:
                snap_restarted = tracker2.get_snapshot()
                self.assertEqual(snap_restarted["totals"]["calls_1m"], 0)
                self.assertEqual(snap_restarted["totals"]["calls_1h"], 0)
                self.assertEqual(snap_restarted["totals"]["calls_1d"], 1)
                by_id_r = {p["id"]: p for p in snap_restarted["providers"]}
                self.assertEqual(by_id_r["yfinance"]["calls_1d"], 1)
                self.assertEqual(by_id_r["zerodha"]["calls_1d"], 0)
            finally:
                tracker2.close()

    def test_low_level_http_hook_captures_requests_without_mutating_urlopen(self) -> None:
        orig_urlopen = urllib.request.urlopen
        orig_putrequest = http.client.HTTPConnection.putrequest
        clock = _FakeClock(datetime(2026, 11, 26, 11, 0, 0, tzinfo=IST_TZ))
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = OutboundApiUsageTracker(db_path=Path(tmpdir) / "hook.db", clock=clock)
            try:
                install_outbound_api_hooks(tracker)
                # urllib.request.urlopen identity MUST NOT be mutated
                self.assertIs(urllib.request.urlopen, orig_urlopen)

                # Simulate putrequest calls on HTTPConnection without touching the network
                with patch("core.api_usage._ORIG_PUTREQUEST", return_value=None):
                    c1 = http.client.HTTPSConnection("api.kite.trade")
                    c1.putrequest("GET", "/user/profile")
                    c2 = http.client.HTTPSConnection("api.upstox.com")
                    c2.putrequest("GET", "/v3/historical-candle/NSE_EQ/day")
                    c3 = http.client.HTTPConnection("127.0.0.1", 3000)
                    c3.putrequest("GET", "/api/health")

                snap = tracker.get_snapshot()
                self.assertEqual(snap["totals"]["calls_1m"], 2)
                self.assertEqual(snap["totals"]["calls_1d"], 2)
                by_id = {p["id"]: p for p in snap["providers"]}
                self.assertEqual(by_id["zerodha"]["calls_1m"], 1)
                self.assertEqual(by_id["upstox"]["calls_1m"], 1)
            finally:
                http.client.HTTPConnection.putrequest = orig_putrequest  # type: ignore[method-assign]
                install_outbound_api_hooks()
                tracker.close()


if __name__ == "__main__":
    unittest.main()
