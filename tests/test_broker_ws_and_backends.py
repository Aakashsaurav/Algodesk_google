"""
tests/test_broker_ws_and_backends.py
====================================
Deterministic offline unit tests for:
1. Indicator backend probing, NumPy 2.x `np.NaN` shim for `pandas_ta`, and
   `backend_diagnostics()`.
2. Upstox v3 Protobuf3 `FeedResponse` binary wire decoder.
3. Zerodha KiteTicker binary packet decoder (8B LTP, 32B Index, 44B Quote, 184B Full+Depth).
4. HotStandbyFeedManager zero-REST-call Broker WebSocket cache & push ingestion.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from core.audit import AuditTrail
from core.broker_ws import (
    BrokerWebSocketHub,
    decode_upstox_feed_response,
    decode_zerodha_binary_packets,
)
from core.feed_manager import HotStandbyFeedManager
from core.instruments import InstrumentIndex
from indicators.toolkit import available_backends, backend_diagnostics, refresh_backend_cache


# ── Protobuf3 Wire Encoding Helpers (for Upstox v3 Test Frames) ──────────────


def _pb_varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_field_varint(field_no: int, value: int) -> bytes:
    return _pb_varint((field_no << 3) | 0) + _pb_varint(value)


def _pb_field_double(field_no: int, value: float) -> bytes:
    return _pb_varint((field_no << 3) | 1) + struct.pack("<d", float(value))


def _pb_field_bytes(field_no: int, raw: bytes) -> bytes:
    return _pb_varint((field_no << 3) | 2) + _pb_varint(len(raw)) + raw


def _pb_field_str(field_no: int, text: str) -> bytes:
    return _pb_field_bytes(field_no, text.encode("utf-8"))


class TestIndicatorBackendDiagnostics(unittest.TestCase):
    def tearDown(self) -> None:
        refresh_backend_cache()

    def test_backend_diagnostics_structure_and_non_import_error_safety(self) -> None:
        refresh_backend_cache()
        orig_import = __import__

        def _fake_import_module(name: str, *args, **kwargs):
            if name == "talib":
                raise OSError("libta_lib.so: cannot open shared object file")
            raise ModuleNotFoundError(f"No module named '{name}'")

        with mock.patch("indicators.toolkit.importlib.import_module", side_effect=_fake_import_module):
            refresh_backend_cache()
            diag = backend_diagnostics()
            avail = available_backends()

        self.assertEqual(diag["python_executable"], sys.executable)
        self.assertTrue(diag["backends"]["builtin"]["available"])
        self.assertFalse(diag["backends"]["talib"]["available"])
        self.assertIn("OSError", diag["backends"]["talib"]["error"])
        self.assertFalse(avail["talib"])
        self.assertTrue(avail["builtin"])

    def test_numpy2_nan_compatibility_shim_for_pandas_ta(self) -> None:
        refresh_backend_cache()
        fake_np = types.ModuleType("numpy")
        fake_np.__version__ = "2.1.0"
        fake_np.nan = float("nan")  # No `NaN` attribute (NumPy 2.x behavior!)

        fake_pd = types.ModuleType("pandas")
        fake_pd.__version__ = "2.2.3"

        fake_pta = types.ModuleType("pandas_ta")
        fake_pta.__version__ = "0.3.14b0"

        def _fake_import_module(name: str, *args, **kwargs):
            if name == "numpy":
                return fake_np
            if name == "pandas":
                return fake_pd
            if name == "pandas_ta":
                # Simulate pandas_ta doing `from numpy import NaN as npNaN`
                if not hasattr(fake_np, "NaN"):
                    raise ImportError("cannot import name 'NaN' from 'numpy'")
                return fake_pta
            raise ModuleNotFoundError(name)

        with mock.patch("indicators.toolkit.importlib.import_module", side_effect=_fake_import_module):
            refresh_backend_cache()
            avail = available_backends()
            diag = backend_diagnostics()

        self.assertTrue(hasattr(fake_np, "NaN"))
        self.assertTrue(avail["pandas_ta"])
        self.assertTrue(diag["backends"]["pandas_ta"]["available"])
        self.assertEqual(diag["backends"]["pandas_ta"]["version"], "0.3.14b0")


class TestBrokerWebSocketDecoders(unittest.TestCase):
    def test_decode_upstox_v3_protobuf_market_and_index_feeds(self) -> None:
        ts_ms = 1730000000000

        # Build MarketFullFeed for RELIANCE
        ltpc_rel = (
            _pb_field_double(1, 2945.50)
            + _pb_field_varint(2, ts_ms)
            + _pb_field_varint(3, 125)
            + _pb_field_double(4, 2900.00)
        )
        quote_lvl1 = (
            _pb_field_varint(1, 500)
            + _pb_field_double(2, 2945.25)
            + _pb_field_varint(3, 450)
            + _pb_field_double(4, 2945.75)
        )
        market_level = _pb_field_bytes(1, quote_lvl1)
        ohlc_1d = (
            _pb_field_str(1, "1d")
            + _pb_field_double(2, 2910.0)
            + _pb_field_double(3, 2960.0)
            + _pb_field_double(4, 2905.0)
            + _pb_field_double(5, 2945.5)
            + _pb_field_varint(6, 5123400)
            + _pb_field_varint(7, ts_ms)
        )
        market_ohlc = _pb_field_bytes(1, ohlc_1d)
        market_ff = (
            _pb_field_bytes(1, ltpc_rel)
            + _pb_field_bytes(2, market_level)
            + _pb_field_bytes(4, market_ohlc)
            + _pb_field_varint(6, 5123400)
            + _pb_field_double(7, 142000.0)
        )
        full_feed_rel = _pb_field_bytes(1, market_ff)
        feed_rel = _pb_field_bytes(2, full_feed_rel)
        map_entry_rel = _pb_field_str(1, "NSE_EQ|INE002A01018") + _pb_field_bytes(2, feed_rel)

        # Build IndexFullFeed for NIFTY
        ltpc_nifty = _pb_field_double(1, 24500.25) + _pb_field_varint(2, ts_ms) + _pb_field_double(4, 24400.0)
        ohlc_nifty_1d = (
            _pb_field_str(1, "1d")
            + _pb_field_double(2, 24410.0)
            + _pb_field_double(3, 24550.0)
            + _pb_field_double(4, 24390.0)
            + _pb_field_double(5, 24500.25)
        )
        index_ff = _pb_field_bytes(1, ltpc_nifty) + _pb_field_bytes(2, _pb_field_bytes(1, ohlc_nifty_1d))
        feed_nifty = _pb_field_bytes(2, _pb_field_bytes(2, index_ff))
        map_entry_nifty = _pb_field_str(1, "NSE_INDEX|Nifty 50") + _pb_field_bytes(2, feed_nifty)

        feed_resp = (
            _pb_field_varint(1, 1)
            + _pb_field_bytes(2, map_entry_rel)
            + _pb_field_bytes(2, map_entry_nifty)
            + _pb_field_varint(3, ts_ms)
        )

        key_meta = {
            "NSE_EQ|INE002A01018": {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "EQUITY"},
            "NSE_INDEX|Nifty 50": {"symbol": "NIFTY", "exchange": "NSE", "instrument_type": "INDEX"},
        }
        quotes, got_ts = decode_upstox_feed_response(feed_resp, key_meta_map=key_meta)
        self.assertEqual(got_ts, ts_ms)
        self.assertIn("RELIANCE", quotes)
        self.assertIn("NIFTY", quotes)

        rel = quotes["RELIANCE"]
        self.assertEqual(rel["ltp"], 2945.5)
        self.assertEqual(rel["prev_close"], 2900.0)
        self.assertEqual(rel["open"], 2910.0)
        self.assertEqual(rel["high"], 2960.0)
        self.assertEqual(rel["low"], 2905.0)
        self.assertEqual(rel["volume"], 5123400)
        self.assertEqual(rel["oi"], 142000)
        self.assertEqual(rel["bid"], 2945.25)
        self.assertEqual(rel["ask"], 2945.75)
        self.assertEqual(rel["change"], 45.5)
        self.assertEqual(rel["transport"], "broker_websocket")

        n50 = quotes["NIFTY"]
        self.assertEqual(n50["ltp"], 24500.25)
        self.assertEqual(n50["prev_close"], 24400.0)
        self.assertEqual(n50["high"], 24550.0)
        self.assertEqual(n50["instrument_type"], "INDEX")

    def test_decode_zerodha_kiteticker_binary_packets(self) -> None:
        # 1) 8-byte LTP packet (INFY token=408065)
        pkt_ltp = struct.pack("!II", 408065, 185025)

        # 2) 32-byte Index Full packet (NIFTY token=256265)
        pkt_idx = struct.pack(
            "!IIIIIIII",
            256265,
            2455050,  # ltp 24550.50
            2460000,  # high
            2440000,  # low
            2445000,  # open
            2440000,  # close
            15050,    # change
            1730000000,  # exchange ts
        )

        # 3) 44-byte Quote packet (TCS token=2953217)
        pkt_quote = struct.pack(
            "!IIIIIIIIIII",
            2953217,
            412500,   # ltp 4125.00
            10,       # ltq
            411000,   # atp
            123456,   # volume
            1000,     # tbq
            900,      # tsq
            410000,   # open
            414000,   # high
            409000,   # low
            408000,   # prev close
        )

        # 4) 184-byte Full packet (RELIANCE token=738561)
        head_64 = struct.pack(
            "!IIIIIIIIIIIIIIII",
            738561,
            295075,   # ltp 2950.75
            50,       # ltq
            294000,   # atp
            987654,   # volume
            25000,    # tbq
            21000,    # tsq
            292000,   # open
            296500,   # high
            291500,   # low
            290000,   # close
            1730000000,  # ltt
            345000,   # oi
            350000,   # oi_high
            340000,   # oi_low
            1730000001,  # exchange_ts
        )
        buy_depth = struct.pack("!IIH2x", 200, 295050, 3) + (b"\x00" * 48)
        sell_depth = struct.pack("!IIH2x", 180, 295100, 2) + (b"\x00" * 48)
        pkt_full = head_64 + buy_depth + sell_depth
        self.assertEqual(len(pkt_full), 184)

        frame = (
            struct.pack("!H", 4)
            + struct.pack("!H", len(pkt_ltp)) + pkt_ltp
            + struct.pack("!H", len(pkt_idx)) + pkt_idx
            + struct.pack("!H", len(pkt_quote)) + pkt_quote
            + struct.pack("!H", len(pkt_full)) + pkt_full
        )

        token_meta = {
            408065: {"symbol": "INFY", "exchange": "NSE", "instrument_type": "EQUITY"},
            256265: {"symbol": "NIFTY", "exchange": "NSE", "instrument_type": "INDEX"},
            2953217: {"symbol": "TCS", "exchange": "NSE", "instrument_type": "EQUITY"},
            738561: {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "EQUITY"},
        }
        quotes = decode_zerodha_binary_packets(frame, token_meta_map=token_meta)
        self.assertEqual(set(quotes.keys()), {"INFY", "NIFTY", "TCS", "RELIANCE"})
        self.assertEqual(quotes["INFY"]["ltp"], 1850.25)
        self.assertEqual(quotes["NIFTY"]["ltp"], 24550.5)
        self.assertEqual(quotes["NIFTY"]["prev_close"], 24400.0)
        self.assertEqual(quotes["TCS"]["volume"], 123456)
        self.assertEqual(quotes["RELIANCE"]["ltp"], 2950.75)
        self.assertEqual(quotes["RELIANCE"]["oi"], 345000)
        self.assertEqual(quotes["RELIANCE"]["bid"], 2950.5)
        self.assertEqual(quotes["RELIANCE"]["ask"], 2951.0)
        self.assertEqual(quotes["RELIANCE"]["transport"], "broker_websocket")


class _FakeConnectedBrokerWS(BrokerWebSocketHub):
    """Test double simulating a live connected Upstox Broker WebSocket without network I/O."""

    def __init__(self, cached_quotes: dict) -> None:
        super().__init__()
        self._fake_quotes = cached_quotes
        self.subscribed_scopes: list[tuple[str, str, int]] = []
        self.released_scopes: list[str] = []

    def set_scope_subscriptions(self, broker_id: str, scope: str, subs) -> None:
        self.subscribed_scopes.append((broker_id, scope, len(subs)))

    def release_scope(self, scope: str, broker_id=None) -> None:
        self.released_scopes.append(scope)

    def is_connected(self, broker_id: str) -> bool:
        return broker_id == "upstox"

    def get_cached_quotes(self, broker_id: str, symbols, *, max_age_sec: float = 30.0):
        if broker_id != "upstox":
            return {}, None
        out = {s: dict(self._fake_quotes[s]) for s in symbols if s in self._fake_quotes}
        return out, 14.5


class TestFeedManagerBrokerWebSocketPath(unittest.TestCase):
    def test_fetch_live_quotes_uses_broker_ws_with_zero_rest_calls(self) -> None:
        rest_calls = 0

        def _counting_rest_fetcher(syms):
            nonlocal rest_calls
            rest_calls += 1
            return {
                s: {"symbol": s, "ltp": 100.0, "source": "upstox", "ts_ist": "2026-10-01T10:00:00+05:30"}
                for s in syms
            }

        fake_ws = _FakeConnectedBrokerWS(
            {
                "RELIANCE": {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "ltp": 2951.25,
                    "prev_close": 2900.0,
                    "volume": 500000,
                    "source": "upstox",
                    "transport": "broker_websocket",
                    "ts_ist": "2026-10-01T10:00:05+05:30",
                },
                "TCS": {
                    "symbol": "TCS",
                    "exchange": "NSE",
                    "ltp": 4120.0,
                    "prev_close": 4080.0,
                    "volume": 250000,
                    "source": "upstox",
                    "transport": "broker_websocket",
                    "ts_ist": "2026-10-01T10:00:05+05:30",
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            fm = HotStandbyFeedManager(
                primary_id="upstox",
                audit=AuditTrail(p / "audit.db"),
                fetchers={"upstox": _counting_rest_fetcher},
                watchlist_file=p / "watchlist.json",
                instruments=InstrumentIndex(p / "inst.db"),
                broker_ws=fake_ws,
            )

            res = fm.fetch_live_quotes_for(["RELIANCE", "TCS"], exchange="NSE", provider="upstox")
            self.assertTrue(res["ok"])
            self.assertEqual(res["transport"], "broker_websocket")
            self.assertEqual(res["batch_count"], 0)
            self.assertEqual(res["latency_ms"], 14.5)
            self.assertEqual(rest_calls, 0, "REST API should NOT be called when Broker WebSocket is live!")
            self.assertEqual(res["quotes"]["RELIANCE"]["ltp"], 2951.25)

            # Also verify push tick ingestion updates watchlist quotes directly
            fm._on_broker_ws_ticks("upstox", fake_ws._fake_quotes, 12.0)
            st = fm.status()
            rel_q = next(q for q in st["quotes"] if q["symbol"] == "RELIANCE")
            self.assertEqual(rel_q["ltp"], 2951.25)
            self.assertEqual(rel_q["transport"], "broker_websocket")


if __name__ == "__main__":
    unittest.main()
