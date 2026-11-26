"""
Comprehensive Audit Remediation & Production Readiness Gate Test Suite.

Covers all accepted & modified audit recommendations:
- Modularity (§2.2 B/C): zero-core-edit broker & data-provider plugins via BrokerRegistry,
  singleton pooling, capability discovery, custom historical/instrument/WS/feed dispatch.
- Market Depth (L5 & L30): Upstox protobuf L5/L30 decoding + change_mode, Zerodha 184-byte
  binary L5 decoding, MarketDepth.walk_book() VWAP slippage, FeedManager.get_market_depth().
- Gate 1 (Data & Backtesting): C-05 corporate action split/dividend adjustments & consistency
  checks, data quality score; C-06 point-in-time universe snapshots & survivorship warnings;
  C-04 F&O lot-size enforcement & expiry settlement; M-02 short-window (<90d) CAGR suppression;
  reproducibility manifest.
- Gate 2 (Live Execution, Risk, OMS & Security): C-01 zero/negative/NaN price fail-closed;
  C-02 broker-authoritative account risk context & external positions; C-03 stale/unknown/
  delayed Yahoo quote fail-closed blocking; C-04 OMS lot/tick/expiry pre-dispatch checks;
  C-07 RiskWatchdog independent health dimensions; Finding 8 OMS error classification,
  durable oms_order_events, manual intervention escalation & operator resolution;
  H-02 composite position identity reconciliation; H-08 encrypted credential vault roundtrip,
  tamper detection & legacy migration; OAuth postMessage origin validation; §9 SQLite online
  backup & atomic restore.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

from broker.base import (
    AccountBalance,
    AmbiguousSubmissionError,
    BrokerBase,
    BrokerCapabilities,
    BrokerHealthSnapshot,
    BrokerOrder,
    BrokerPosition,
    DepthLevel,
    ExecutionFill,
    InstrumentContract,
    MarketDataProviderBase,
    MarketDepth,
    OrderRequest,
    OrderRejectedError,
    TransientBrokerError,
    build_position_identity,
)
from broker.registry import BrokerRegistry
from config import AppConfig, config
from core.backup import DatabaseBackupManager, verify_sqlite_integrity
from core.broker_ws import (
    BrokerWebSocketHub,
    UpstoxWSFeedClient,
    ZerodhaWSFeedClient,
    decode_upstox_feed_response,
    decode_zerodha_binary_packets,
)
from core.feed_manager import HotStandbyFeedManager
from core.historical import (
    HistoricalDataManager,
    apply_corporate_actions,
    compute_data_quality_score,
)
from core.instruments import InstrumentIndex
from deploy.manager import DeploymentManager, _evaluate_quote_freshness
from execution.oms import OrderManagementSystem, PaperExecutionAdapter
from risk.limits import RiskLimitManager, RiskWatchdog
from screener.universes import UniverseResolver
from web.security import (
    PLUGIN_TRUST_NOTICE,
    validate_oauth_redirect_origin,
)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Protobuf wire-format encoder helpers for testing Upstox v3 L5 & L30 ──────


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    v = int(val)
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _pb_field_varint(field_num: int, val: int) -> bytes:
    return _encode_varint((field_num << 3) | 0) + _encode_varint(val)


def _pb_field_double(field_num: int, val: float) -> bytes:
    return _encode_varint((field_num << 3) | 1) + struct.pack("<d", float(val))


def _pb_field_bytes(field_num: int, payload: bytes) -> bytes:
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(payload)) + payload


def _build_upstox_feed_response(
    instrument_key: str,
    ltp: float,
    cp: float,
    ltt_ms: int,
    levels: int = 30,
    tbq: float = 12500.0,
    tsq: float = 9800.0,
) -> bytes:
    """Build a valid Upstox v3 protobuf FeedResponse with ``levels`` bid/ask quotes."""
    # LTPC: field 1=ltp(double), 2=ltt(varint), 3=ltq(varint), 4=cp(double)
    ltpc = (
        _pb_field_double(1, ltp)
        + _pb_field_varint(2, ltt_ms)
        + _pb_field_varint(3, 50)
        + _pb_field_double(4, cp)
    )
    # MarketLevel: repeated field 1 = Quote(bidQ=1, bidP=2, askQ=3, askP=4)
    quotes_pb = bytearray()
    for i in range(levels):
        bid_p = round(ltp - 0.05 * (i + 1), 2)
        ask_p = round(ltp + 0.05 * (i + 1), 2)
        q_pb = (
            _pb_field_varint(1, 100 + i * 10)
            + _pb_field_double(2, bid_p)
            + _pb_field_varint(3, 120 + i * 10)
            + _pb_field_double(4, ask_p)
        )
        quotes_pb.extend(_pb_field_bytes(1, q_pb))
    market_level = bytes(quotes_pb)
    # MarketFullFeed: 1=ltpc, 2=marketLevel, 6=vtt, 7=oi, 8=tbq, 9=tsq
    market_ff = (
        _pb_field_bytes(1, ltpc)
        + _pb_field_bytes(2, market_level)
        + _pb_field_varint(6, 450000)
        + _pb_field_double(7, 12000.0)
        + _pb_field_varint(8, int(tbq))
        + _pb_field_varint(9, int(tsq))
    )
    # FullFeed: 1=marketFF
    full_feed = _pb_field_bytes(1, market_ff)
    # Feed: 2=ff
    feed = _pb_field_bytes(2, full_feed)
    # map<string, Feed> entry: 1=key, 2=value
    entry = _pb_field_bytes(1, instrument_key.encode("utf-8")) + _pb_field_bytes(2, feed)
    # FeedResponse: 1=type(2=live_feed), 2=feeds map entry, 3=currentTs
    return (
        _pb_field_varint(1, 2)
        + _pb_field_bytes(2, entry)
        + _pb_field_varint(3, ltt_ms)
    )


def _build_zerodha_184_packet(
    token: int,
    ltp: float,
    prev_close: float,
    ltt_epoch: int,
    tbq: int = 8500,
    tsq: int = 7200,
) -> bytes:
    """Build a valid Zerodha KiteConnect 184-byte full-mode binary packet with L5 depth."""
    header = struct.pack(
        ">IIIIIIIIIIIIIIII",
        token,
        int(round(ltp * 100)),
        25,  # last_traded_qty
        int(round(ltp * 100)),  # avg_traded_price
        150000,  # volume
        tbq,
        tsq,
        int(round((ltp - 5) * 100)),  # open
        int(round((ltp + 10) * 100)),  # high
        int(round((ltp - 8) * 100)),  # low
        int(round(prev_close * 100)),  # close
        ltt_epoch,  # ltt
        24000,  # oi
        25000,  # oi_day_high
        21000,  # oi_day_low
        ltt_epoch,  # exchange_ts
    )
    assert len(header) == 64
    depth_bytes = bytearray()
    # 5 bids (12 bytes each: >IIHH -> qty, price*100, orders, pad)
    for i in range(5):
        depth_bytes.extend(
            struct.pack(">IIHH", 100 + i * 25, int(round((ltp - 0.05 * (i + 1)) * 100)), 3 + i, 0)
        )
    # 5 asks (12 bytes each)
    for i in range(5):
        depth_bytes.extend(
            struct.pack(">IIHH", 110 + i * 20, int(round((ltp + 0.05 * (i + 1)) * 100)), 2 + i, 0)
        )
    pkt = header + bytes(depth_bytes)
    assert len(pkt) == 184
    return struct.pack(">HH", 1, len(pkt)) + pkt


# ── Custom Plugin Broker & Data Provider for Modularity Tests (§2.2 B/C) ─────


class _CustomMockBroker(BrokerBase):
    broker_id = "fyers_demo"
    display_name = "Fyers Demo Plugin"
    capabilities = BrokerCapabilities(
        supports_trading=True,
        supports_market_data=True,
        supports_historical=True,
        supports_instrument_master=True,
        supports_websocket=True,
        max_depth_levels=30,
        execution_grade_feed=True,
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connected = True

    def connect(self) -> bool:
        self._connected = True
        return True

    def is_authenticated(self) -> bool:
        return True

    def health_check(self, timeout: float = 2.0) -> BrokerHealthSnapshot:
        return BrokerHealthSnapshot(
            broker_id=self.broker_id,
            display_name=self.display_name,
            status="CONNECTED",
            authenticated=True,
            message="ok",
        )

    def place_order(self, order: OrderRequest) -> Optional[BrokerOrder]:
        return BrokerOrder(
            order_id="FY-1001",
            symbol=order.symbol,
            exchange=order.exchange,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            product=order.product,
            status="FILLED",
            filled_quantity=order.quantity,
            average_price=order.price or 100.0,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self) -> Sequence[BrokerPosition]:
        return []

    def get_balance(self) -> AccountBalance:
        return AccountBalance(available_cash=500_000.0, total_equity=500_000.0)

    def get_orders(self) -> Sequence[BrokerOrder]:
        return []

    def get_trades(self) -> Sequence[ExecutionFill]:
        return []

    def fetch_market_depth(
        self,
        symbol: str,
        exchange: str = "NSE",
        *,
        instrument_key: Optional[str] = None,
        levels: int = 5,
    ) -> Optional[MarketDepth]:
        n = min(max(int(levels), 1), 30)
        return MarketDepth(
            symbol=symbol.upper(),
            exchange=exchange.upper(),
            source=self.broker_id,
            depth_levels=n,
            bids=[DepthLevel(price=round(1000.0 - 0.1 * (i + 1), 2), quantity=50 * (i + 1), orders=2) for i in range(n)],
            asks=[DepthLevel(price=round(1000.0 + 0.1 * (i + 1), 2), quantity=60 * (i + 1), orders=3) for i in range(n)],
            total_buy_qty=sum(50 * (i + 1) for i in range(n)),
            total_sell_qty=sum(60 * (i + 1) for i in range(n)),
            ts_ist=datetime.now(IST).isoformat(),
        )

    def fetch_historical_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        *,
        instrument_key: Optional[str] = None,
        adjustment_mode: str = "raw",
    ) -> List[Dict[str, Any]]:
        return [
            {
                "date": start_date,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 10000,
                "oi": 0,
            }
        ]

    def sync_instruments(self, index: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"ok": True, "provider": self.broker_id, "count": 42}


class _CustomDataOnlyProvider(MarketDataProviderBase):
    broker_id = "polygon_demo"
    display_name = "Polygon Demo Feed"
    capabilities = BrokerCapabilities(
        supports_trading=False,
        supports_market_data=True,
        supports_historical=True,
        supports_instrument_master=False,
        supports_websocket=False,
        requires_oauth=False,
        max_depth_levels=0,
        execution_grade_feed=False,
    )

    def fetch_historical_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        *,
        instrument_key: Optional[str] = None,
        adjustment_mode: str = "raw",
    ) -> List[Dict[str, Any]]:
        return [
            {
                "date": start_date,
                "open": 250.0,
                "high": 255.0,
                "low": 248.0,
                "close": 253.0,
                "volume": 5000,
                "oi": 0,
            }
        ]


# ── Main Test Cases ──────────────────────────────────────────────────────────


class TestModularityAndMarketDepth(unittest.TestCase):
    """Tests §2.2 B/C modularity and Level 5 / Level 30 market depth across WS & API."""

    def tearDown(self) -> None:
        BrokerRegistry.unregister("fyers_demo")
        BrokerRegistry.unregister("polygon_demo")
        BrokerRegistry.reset_instances()

    def test_01_modular_broker_and_data_provider_plugin_registration_and_pooling(self) -> None:
        BrokerRegistry.register_plugin("fyers_demo", _CustomMockBroker)
        BrokerRegistry.register_data_provider("polygon_demo", _CustomDataOnlyProvider)

        # Singleton pooling preserves instance across calls unless fresh=True
        a1 = BrokerRegistry.get("fyers_demo")
        a2 = BrokerRegistry.get("fyers_demo")
        a3 = BrokerRegistry.get("fyers_demo", fresh=True)
        self.assertIs(a1, a2)
        self.assertIsNot(a1, a3)

        # Capability-based discovery distinguishes trading brokers vs data-only providers
        self.assertIn("fyers_demo", BrokerRegistry.list_trading_brokers())
        self.assertNotIn("polygon_demo", BrokerRegistry.list_trading_brokers())
        self.assertIn("polygon_demo", BrokerRegistry.list_market_data_providers())
        self.assertIn("fyers_demo", BrokerRegistry.list_historical_providers())
        self.assertIn("polygon_demo", BrokerRegistry.list_historical_providers())

        # Data-only provider cannot place orders
        dp = BrokerRegistry.get("polygon_demo")
        with self.assertRaises(OrderRejectedError):
            dp.place_order(OrderRequest(symbol="RELIANCE", exchange="NSE", side="BUY", quantity=1))

        # HistoricalDataManager dispatches seamlessly to custom plugin without core edits
        with tempfile.TemporaryDirectory() as td:
            hdm = HistoricalDataManager(db_path=Path(td) / "ohlcv.db")
            rows = hdm._fetch_chunk_from_provider(
                "polygon_demo", "RELIANCE", "NSE", "1day", "2026-01-05", "2026-01-05"
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close"], 253.0)

            # InstrumentIndex.sync_provider dispatches modularly to custom broker
            idx = InstrumentIndex(db_path=Path(td) / "instr.db")
            sync_res = idx.sync_provider("fyers_demo")
            self.assertTrue(sync_res.get("ok"))
            self.assertEqual(sync_res.get("count"), 42)

    def test_02_upstox_protobuf_l5_and_l30_depth_decoding_and_mode_switch(self) -> None:
        ltt_ms = int(datetime(2026, 6, 15, 10, 30, 0, tzinfo=IST).timestamp() * 1000)
        client = UpstoxWSFeedClient(depth_levels=30)
        self.assertEqual(client._depth_levels, 30)
        self.assertEqual(client._feed_mode, "full_d30")

        key_map = {"NSE_EQ|INE002A01018": {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "EQUITY"}}
        raw_l30 = _build_upstox_feed_response("NSE_EQ|INE002A01018", 2950.0, 2920.0, ltt_ms, levels=30)
        ticks_map, _ = decode_upstox_feed_response(raw_l30, key_map)
        self.assertIn("RELIANCE", ticks_map)
        tick = ticks_map["RELIANCE"]
        self.assertAlmostEqual(tick["ltp"], 2950.0, places=2)
        self.assertEqual(tick["depth_levels"], 30)
        self.assertEqual(len(tick["depth"]["bids"]), 30)
        self.assertEqual(len(tick["depth"]["asks"]), 30)
        self.assertAlmostEqual(tick["bid"], 2949.95, places=2)
        self.assertAlmostEqual(tick["ask"], 2950.05, places=2)
        self.assertEqual(tick["depth"]["total_buy_qty"], 12500.0)
        self.assertEqual(tick["depth"]["total_sell_qty"], 9800.0)
        self.assertIn("2026-06-15T10:30:00", tick["source_timestamp_ist"])

        # Switch live mode to L5
        client.set_depth_levels(5)
        self.assertEqual(client._depth_levels, 5)
        self.assertEqual(client._feed_mode, "full")

        raw_l5 = _build_upstox_feed_response("NSE_EQ|INE002A01018", 2951.0, 2920.0, ltt_ms, levels=5)
        ticks_l5, _ = decode_upstox_feed_response(raw_l5, key_map)
        self.assertEqual(len(ticks_l5["RELIANCE"]["depth"]["bids"]), 5)
        self.assertEqual(len(ticks_l5["RELIANCE"]["depth"]["asks"]), 5)

    def test_03_zerodha_184_byte_binary_l5_depth_decoding(self) -> None:
        ltt_epoch = int(datetime(2026, 6, 15, 11, 15, 0, tzinfo=IST).timestamp())
        token_map = {738561: {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "EQUITY"}}
        pkt = _build_zerodha_184_packet(738561, 2950.0, 2910.0, ltt_epoch)
        ticks_map = decode_zerodha_binary_packets(pkt, token_map)
        self.assertIn("RELIANCE", ticks_map)
        tick = ticks_map["RELIANCE"]
        self.assertAlmostEqual(tick["ltp"], 2950.0, places=2)
        self.assertEqual(tick["depth_levels"], 5)
        self.assertEqual(len(tick["depth"]["bids"]), 5)
        self.assertEqual(len(tick["depth"]["asks"]), 5)
        self.assertEqual(tick["depth"]["bids"][0]["orders"], 3)
        self.assertEqual(tick["depth"]["asks"][0]["orders"], 2)
        self.assertEqual(tick["depth"]["total_buy_qty"], 8500)
        self.assertEqual(tick["depth"]["total_sell_qty"], 7200)
        self.assertIn("2026-06-15T11:15:00", tick["source_timestamp_ist"])

    def test_04_market_depth_walk_book_and_feed_manager_depth_api(self) -> None:
        BrokerRegistry.register_plugin("fyers_demo", _CustomMockBroker)
        depth = BrokerRegistry.get("fyers_demo").fetch_market_depth("RELIANCE", "NSE", levels=30)
        self.assertIsNotNone(depth)
        self.assertEqual(len(depth.bids), 30)
        self.assertEqual(len(depth.asks), 30)
        self.assertGreater(depth.spread, 0)
        self.assertGreater(depth.spread_bps, 0)

        # Walk book across multiple ask levels for a large BUY order (qty=200: 60@1000.1 + 120@1000.2 + 20@1000.3)
        walk = depth.walk_book("BUY", 200)
        expected_vwap = (60 * 1000.1 + 120 * 1000.2 + 20 * 1000.3) / 200.0
        self.assertEqual(walk["filled_qty"], 200)
        self.assertAlmostEqual(walk["vwap"], expected_vwap, places=4)
        self.assertGreater(walk["slippage_bps"], 0.0)

        # HotStandbyFeedManager.get_market_depth fetches L30 cleanly from custom plugin via BrokerRegistry
        fm = HotStandbyFeedManager()
        res = fm.get_market_depth("RELIANCE", "NSE", levels=30, broker_id="fyers_demo")
        self.assertTrue(res.get("ok"))
        self.assertEqual(len(res["depth"]["bids"]), 30)
        self.assertEqual(len(res["depth"]["asks"]), 30)

        # BrokerWebSocketHub can dynamically register external clients
        hub = BrokerWebSocketHub()
        hub.register_client("fyers_demo", UpstoxWSFeedClient(depth_levels=30))
        self.assertTrue(hub.supports("fyers_demo"))


class TestGate1DataAndBacktesting(unittest.TestCase):
    """Tests Gate 1: C-04 (lot/expiry), C-05 (adjustments & quality), C-06 (PIT universes), M-02 (CAGR)."""

    def test_05_c05_corporate_action_split_volume_and_dividend_modes(self) -> None:
        raw_rows = [
            {"date": "2025-05-01", "open": 2000.0, "high": 2040.0, "low": 1980.0, "close": 2000.0, "volume": 1000},
            {"date": "2025-05-02", "open": 1000.0, "high": 1020.0, "low": 990.0, "close": 1010.0, "volume": 2000},
        ]
        actions = [
            {"ex_date": "2025-05-02", "action_type": "SPLIT", "ratio": 2.0, "cash_amount": 0.0},
            {"ex_date": "2025-05-02", "action_type": "DIVIDEND", "ratio": 1.0, "cash_amount": 20.0},
        ]

        # Split-adjusted: prices divided by 2, volume multiplied by 2 -> notional turnover preserved!
        split_adj = apply_corporate_actions(raw_rows, actions, adjustment_mode="split_adjusted")
        self.assertAlmostEqual(split_adj[0]["close"], 1000.0, places=4)
        self.assertEqual(split_adj[0]["volume"], 2000)
        self.assertAlmostEqual(
            split_adj[0]["close"] * split_adj[0]["volume"],
            raw_rows[0]["close"] * raw_rows[0]["volume"],
            places=2,
        )
        # Post-ex-date bar untouched
        self.assertAlmostEqual(split_adj[1]["close"], 1010.0, places=4)

        # Total-return also applies dividend factor on pre-ex-date close
        tot_ret = apply_corporate_actions(raw_rows, actions, adjustment_mode="total_return")
        self.assertLess(tot_ret[0]["close"], split_adj[0]["close"])

        # Data quality score catches OHLC inversions and zero-volume spikes
        bad_rows = [
            {"date": "2025-05-01", "open": 100.0, "high": 95.0, "low": 105.0, "close": 100.0, "volume": 0},
            {"date": "2025-05-02", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 500},
        ]
        dq = compute_data_quality_score(bad_rows, "1day")
        self.assertLess(dq["quality_score"], 100.0)
        self.assertGreater(dq["invalid_ohlc_bars"], 0)
        self.assertGreater(dq["zero_volume_bars"], 0)

    def test_06_c06_point_in_time_universe_snapshots_and_survivorship_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ur = UniverseResolver(snapshots_db_path=Path(td) / "universes.db")
            ur.save_universe_snapshot("DEFAULT_WATCHLIST", "2020-01-01", ["RELIANCE", "TCS", "YESBANK"], source="nse_archive")
            ur.save_universe_snapshot("DEFAULT_WATCHLIST", "2025-01-01", ["RELIANCE", "TCS", "TRENT"], source="nse_archive")

            # As of 2021-06-01 -> resolves 2020 snapshot (includes YESBANK, excludes TRENT)
            pit = ur.resolve_universe_as_of("DEFAULT_WATCHLIST", "2021-06-01")
            self.assertTrue(pit["point_in_time"])
            self.assertIsNone(pit["survivorship_warning"])
            self.assertIn("YESBANK", pit["symbols"])
            self.assertNotIn("TRENT", pit["symbols"])

            # As of 2018-01-01 (before any snapshot) -> falls back with explicit survivorship_warning
            early = ur.resolve_universe_as_of("DEFAULT_WATCHLIST", "2018-01-01")
            self.assertFalse(early["point_in_time"])
            self.assertTrue(bool(early["survivorship_warning"]))

    def test_07_c04_and_m02_fno_lot_size_expiry_settlement_and_short_window_cagr(self) -> None:
        from backtest.portfolio import PortfolioBacktestEngine

        # Create a 30-day synthetic dataset (<90 days -> cagr_pct MUST be None!)
        start = datetime(2026, 1, 2)
        rows = []
        for i in range(30):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            px = 200.0 + i * 2.0
            rows.append(
                {
                    "trade_date": d,
                    "ts_ist": f"{d}T15:30:00+05:30",
                    "open": px,
                    "high": px + 3.0,
                    "low": px - 2.0,
                    "close": px + 1.0,
                    "volume": 100000,
                }
            )

        class _StubHDM:
            def get_records(self, **kwargs):
                return {"ok": True, "records": list(rows)}

            def check_adjustment_consistency(self, symbols, exchange="NSE", timeframe="1day"):
                return {"consistent": True, "warning": None}

        # Always-buy visual rule
        rule_def = {
            "id": "always_long",
            "name": "Always Long",
            "kind": "strategy",
            "side": "BUY",
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "left": {"type": "close"},
                        "operator": ">",
                        "right": {"type": "number", "value": 100},
                    }
                ],
            },
            "exit": {"logic": "AND", "conditions": []},
        }

        fut_contract = InstrumentContract(
            symbol="NIFTY26JANFUT",
            exchange="NFO",
            segment="FUT",
            lot_size=25,
            tick_size=0.05,
            expiry="2026-01-20",
        )
        with tempfile.TemporaryDirectory() as td:
            engine = PortfolioBacktestEngine(hist_manager=_StubHDM(), runs_file=Path(td) / "runs.json")
            res = engine.run(
                mode="portfolio",
                allocation_mode="equal_split",
                strategy="always_long",
                rule_def=rule_def,
                symbols=["NIFTY26JANFUT"],
                exchange="NFO",
                timeframe="1day",
                from_date="2026-01-02",
                to_date="2026-01-31",
                initial_capital=100_000.0,
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                trailing_sl_pct=0.0,
                charges_profile="zero",
                spread_bps=5.0,
                contracts={"NIFTY26JANFUT": fut_contract},
                fetcher=lambda *args, **kwargs: list(rows),
            )
            self.assertTrue(res["ok"])
            # M-02: Window is 29 days (<90d), so cagr_pct must be None and sample_warning present
            self.assertIsNone(res["cagr_pct"])
            self.assertTrue(bool(res.get("sample_warning")))
            self.assertIn("reproducibility_manifest", res)
            # C-04: Trade qty must be a multiple of lot_size=25, and position settled on expiry 2026-01-20
            self.assertGreaterEqual(len(res["trades"]), 1)
            for tr in res["trades"]:
                self.assertEqual(tr["qty"] % 25, 0)
                self.assertLessEqual(tr["entry_date"], "2026-01-20")
            self.assertEqual(res["trades"][0]["exit_reason"], "EXPIRY_SETTLEMENT")


class TestGate2LiveExecutionRiskOmsAndSecurity(unittest.TestCase):
    """Tests Gate 2: C-01, C-02, C-03, C-04, C-07, Finding 8, H-02, H-08, origin validation, backup."""

    def test_08_c01_risk_manager_fails_closed_on_invalid_prices_and_lot_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rlm = RiskLimitManager(limits_file=Path(td) / "risk.json")
            for bad_px in (None, 0, 0.0, -150.0, float("nan"), float("inf"), "not-a-number"):
                chk = rlm.check_entry(
                    symbol="RELIANCE",
                    quantity=10,
                    price=bad_px,
                    current_open_positions=0,
                    daily_loss_inr=0.0,
                )
                self.assertFalse(chk["allowed"], f"Expected invalid price {bad_px!r} to be rejected")

            # Contract lot-size violation rejected
            fut_contract = InstrumentContract(
                symbol="NIFTY26JANFUT", exchange="NFO", segment="FUT", lot_size=25, tick_size=0.05
            )
            chk_lot = rlm.check_entry(
                symbol="NIFTY26JANFUT",
                quantity=10,
                price=24000.0,
                current_open_positions=0,
                daily_loss_inr=0.0,
                contract=fut_contract,
            )
            self.assertFalse(chk_lot["allowed"])

    def test_09_c07_risk_watchdog_independent_health_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rlm = RiskLimitManager(limits_file=Path(td) / "risk.json")
            wd = RiskWatchdog(
                risk_mgr=rlm,
                state_provider=lambda: {
                    "funds": {"available": 500_000.0, "used": 0.0},
                    "positions": [],
                    "orders": [],
                },
                degraded_threshold=2,
            )
            # Fail oms_reconciliation twice -> degraded
            wd.report_dimension("oms_reconciliation", healthy=False, error="Broker orderbook 502")
            wd.report_dimension("oms_reconciliation", healthy=False, error="Broker orderbook 502")
            self.assertTrue(wd.is_degraded)
            self.assertIn("oms_reconciliation", wd.unhealthy_dimensions())

            # A passing risk_calculations run MUST NOT clear oms_reconciliation failure!
            wd.run_once()
            self.assertTrue(wd.is_degraded)
            self.assertIn("oms_reconciliation", wd.unhealthy_dimensions())

            # Explicitly recovering oms_reconciliation clears degraded mode
            wd.report_dimension("oms_reconciliation", healthy=True)
            self.assertFalse(wd.is_degraded)
            self.assertEqual(wd.unhealthy_dimensions(), [])

    def test_10_c04_and_finding8_oms_validation_error_classification_and_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            oms = OrderManagementSystem(db_path=Path(td) / "oms.db", manual_intervention_sec=0.01)

            # 1) C-04: OMS rejects invalid lot_size, tick_size, and expired contract before dispatch
            fut = InstrumentContract(
                symbol="NIFTY26JANFUT",
                exchange="NFO",
                segment="FUT",
                lot_size=25,
                tick_size=0.05,
                expiry="2020-01-30",  # expired
            )
            paper = PaperExecutionAdapter(profile="realistic_spread", spread_bps=10.0)

            res_exp, _, _ = oms.submit_order(
                OrderRequest(symbol="NIFTY26JANFUT", exchange="NFO", side="BUY", quantity=25, order_type="MARKET"),
                paper,
                deployment_id="dep-1",
                exec_mode="paper",
                contract=fut,
            )
            self.assertEqual(res_exp["status"], "REJECTED")
            self.assertIn("expired", res_exp["rejection_reason"].lower())

            fut_active = InstrumentContract(
                symbol="NIFTY29JANFUT",
                exchange="NFO",
                segment="FUT",
                lot_size=25,
                tick_size=0.05,
                expiry="2099-01-29",
            )
            res_tick, _, _ = oms.submit_order(
                OrderRequest(
                    symbol="NIFTY29JANFUT",
                    exchange="NFO",
                    side="BUY",
                    quantity=25,
                    order_type="LIMIT",
                    price=24000.03,  # not multiple of 0.05
                ),
                paper,
                deployment_id="dep-1",
                exec_mode="paper",
                contract=fut_active,
            )
            self.assertEqual(res_tick["status"], "REJECTED")
            self.assertIn("tick", res_tick["rejection_reason"].lower())

            # 2) Finding 8: Definite OrderRejectedError transitions straight to REJECTED
            class _RejectBroker(_CustomMockBroker):
                def place_order(self, order: OrderRequest):
                    raise OrderRejectedError("Insufficient margin (HTTP 400)")

            res_def, _, _ = oms.submit_order(
                OrderRequest(symbol="RELIANCE", exchange="NSE", side="BUY", quantity=1),
                _RejectBroker(),
                deployment_id="dep-1",
                exec_mode="live_auto",
            )
            self.assertEqual(res_def["status"], "REJECTED")
            meta_def = json.loads(res_def.get("metadata_json") or "{}")
            self.assertEqual(meta_def.get("failure_class"), "definite_rejection")

            # 3) Finding 8: Ambiguous submission error -> SUBMITTED (uncertain) -> escalates to MANUAL_INTERVENTION -> resolved
            class _TimeoutBroker(_CustomMockBroker):
                def place_order(self, order: OrderRequest):
                    raise AmbiguousSubmissionError("Gateway timeout waiting for response")

                def get_orders(self):
                    return []  # broker doesn't show the order yet

            res_amb, _, _ = oms.submit_order(
                OrderRequest(symbol="TCS", exchange="NSE", side="BUY", quantity=5, price=3500.0),
                _TimeoutBroker(),
                deployment_id="dep-2",
                exec_mode="live_auto",
            )
            meta_amb = json.loads(res_amb.get("metadata_json") or "{}")
            self.assertTrue(meta_amb.get("uncertain"))
            self.assertEqual(meta_amb.get("failure_class"), "ambiguous")

            # Backdate created_at_ist by 10 minutes to simulate exceeding the reconciliation deadline
            ten_min_ago = (datetime.now(IST) - timedelta(minutes=10)).isoformat(timespec="seconds")
            conn = oms._connect()
            conn.execute(
                "UPDATE oms_orders SET created_at_ist = ? WHERE client_order_id = ?",
                (ten_min_ago, res_amb["client_order_id"]),
            )
            conn.commit()
            rec = oms.reconcile_orders(_TimeoutBroker())
            self.assertGreaterEqual(len(rec.get("manual_intervention_required") or []), 1)
            ord_after = oms.get_order_by_client_id(res_amb["client_order_id"])
            meta_after = json.loads(ord_after.get("metadata_json") or "{}")
            self.assertTrue(meta_after.get("needs_manual_intervention"))

            # Operator resolves manual intervention -> FILLED & durable oms_order_events recorded
            resolved = oms.resolve_manual_order(
                res_amb["client_order_id"],
                resolution_status="FILLED",
                filled_quantity=5,
                average_fill_price=3502.5,
                note="Confirmed in Upstox orderbook",
                actor="operator",
            )
            self.assertEqual(resolved["status"], "FILLED")
            self.assertEqual(resolved["filled_quantity"], 5)
            events = oms.list_order_events(res_amb["client_order_id"])
            self.assertGreaterEqual(len(events), 3)
            oms.close_connections()

    def test_11_c02_c03_h02_deployment_freshness_account_risk_and_composite_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rlm = RiskLimitManager(limits_file=Path(td) / "risk.json")
            oms = OrderManagementSystem(db_path=Path(td) / "oms.db")
            wd = RiskWatchdog(risk_mgr=rlm)
            dm = DeploymentManager(
                db_path=Path(td) / "deploy.db",
                risk_mgr=rlm,
                oms=oms,
                watchdog=wd,
                broker_resolver=lambda _bid: None,
            )

            # C-03: Unknown/missing timestamp quote fails closed
            chk_missing = _evaluate_quote_freshness(
                {"ltp": 100.0, "source": "upstox"}, timeframe="1minute", exec_mode="live_auto"
            )
            self.assertFalse(chk_missing["fresh"])
            self.assertIn("timestamp", chk_missing["reason"].lower())

            # C-03: Yahoo delayed feed fails closed for live_auto even with recent timestamp
            now_iso = datetime.now(IST).isoformat()
            chk_yf = _evaluate_quote_freshness(
                {"ltp": 100.0, "source": "yfinance", "ts_ist": now_iso, "execution_grade": False},
                timeframe="1day",
                exec_mode="live_auto",
            )
            self.assertFalse(chk_yf["fresh"])
            self.assertIn("execution-grade", chk_yf["reason"].lower())

            # C-03: Fresh Upstox execution-grade quote passes
            chk_live = _evaluate_quote_freshness(
                {"ltp": 100.0, "source": "upstox", "ts_ist": now_iso, "execution_grade": True},
                timeframe="1minute",
                exec_mode="live_auto",
            )
            self.assertTrue(chk_live["fresh"])

            # H-02 & C-02: Composite identity separates CNC vs MIS & tracks external manual positions
            class _LivePositionsBroker(_CustomMockBroker):
                def get_positions(self):
                    return [
                        BrokerPosition(
                            symbol="RELIANCE",
                            exchange="NSE",
                            quantity=15,
                            average_price=2900.0,
                            market_price=2920.0,
                            product="CNC",
                            direction=1,
                        ),
                        BrokerPosition(
                            symbol="RELIANCE",
                            exchange="NSE",
                            quantity=10,
                            average_price=2910.0,
                            market_price=2920.0,
                            product="MIS",
                            direction=1,
                        ),
                    ]

                def get_balance(self):
                    return AccountBalance(available_cash=420_000.0, cash=420_000.0, equity=490_000.0)

                def get_funds(self):
                    return {"available": 420_000.0, "used": 70_000.0}

            k_cnc = build_position_identity(exchange="NSE", symbol="RELIANCE", product="CNC")
            k_mis = build_position_identity(exchange="NSE", symbol="RELIANCE", product="MIS")
            self.assertNotEqual(k_cnc, k_mis)

            # Register a running live deployment watching INFY so reconciliation inspects broker account
            dm._runtime["dep-live"] = {
                "id": "dep-live",
                "name": "Live INFY",
                "exec_mode": "live_auto",
                "status": "RUNNING",
                "exchange": "NSE",
                "spec": {"symbols": ["INFY"], "product": "MIS"},
                "positions": {},
                "instrument_keys": {},
                "trades": [],
                "realized_pnl": 0.0,
                "stop_event": threading.Event(),
            }

            live_broker = _LivePositionsBroker()
            recon = dm.reconcile_positions(
                broker_adapter=live_broker, isolate_external_positions=True
            )
            self.assertTrue(recon["ok"])
            self.assertEqual(len(recon["external_positions"]), 2)
            self.assertTrue(all(p["externally_managed"] for p in recon["external_positions"]))

            # C-02: Account risk context reflects authoritative broker positions (including external) & funds
            dummy_rt = dm._runtime["dep-live"]
            with dm._lock:
                open_cnt, gross_exp, pending_exp, daily_loss, wd_deg, avail_margin = dm._account_risk_context_locked(
                    dummy_rt, broker_adapter=live_broker
                )
            self.assertEqual(open_cnt, 2)
            self.assertGreater(gross_exp, 70_000.0)
            self.assertAlmostEqual(avail_margin, 420_000.0, places=2)
            oms.close_connections()

    def test_12_h08_encrypted_credential_vault_migration_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vault_path = Path(td) / ".credentials.json"
            key_path = Path(td) / ".vault.key"
            # Seed a legacy plaintext vault
            vault_path.write_text(json.dumps({"UPSTOX_API_KEY": "legacy-secret-123"}), encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ALGODESK_VAULT_FILE": str(vault_path),
                    "ALGODESK_VAULT_KEY_FILE": str(key_path),
                    "ALGODESK_USE_KEYCHAIN": "0",
                },
                clear=False,
            ):
                os.environ.pop("ALGODESK_VAULT_KEY", None)
                cfg = AppConfig()
                # Reading legacy plaintext auto-migrates it to enc_v1 on disk!
                data = cfg._read_local_vault()
                self.assertEqual(data.get("UPSTOX_API_KEY"), "legacy-secret-123")
                raw_on_disk = json.loads(vault_path.read_text(encoding="utf-8"))
                self.assertEqual(raw_on_disk.get("format"), "enc_v1")
                self.assertNotIn("legacy-secret-123", vault_path.read_text(encoding="utf-8"))

                # Save a new credential and verify roundtrip
                cfg.save_local_credential("ZERODHA_API_SECRET", "zerodha_api_secret", "zk-top-secret-999")
                reloaded = cfg._read_local_vault()
                self.assertEqual(reloaded.get("zerodha_api_secret"), "zk-top-secret-999")
                self.assertTrue(cfg.vault_status()["encrypted"])

                # Tamper with ciphertext -> fail-closed (ValueError / empty read, never leaks)
                tampered = json.loads(vault_path.read_text(encoding="utf-8"))
                tampered["ciphertext"] = "AAAA" + tampered["ciphertext"][4:]
                with self.assertRaises(ValueError):
                    cfg._decrypt_vault_payload(tampered)

    def test_13_security_origin_validation_plugin_notice_and_sqlite_backup_restore(self) -> None:
        # H-05 trust notice explicitly states AST is NOT a sandbox for untrusted code
        self.assertIn("not a sandbox", PLUGIN_TRUST_NOTICE.lower())

        # OAuth origin helper never returns wildcard '*'
        self.assertEqual(validate_oauth_redirect_origin("http://127.0.0.1:8000/callback"), "http://127.0.0.1:8000")
        self.assertEqual(validate_oauth_redirect_origin("https://evil.example.com/x"), "http://127.0.0.1:8000")
        self.assertNotEqual(validate_oauth_redirect_origin(None), "*")

        # §9: SQLite online backup + integrity check + atomic restore
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "oms.db"
            backup_dir = Path(td) / "backups"
            conn = sqlite3.connect(str(db_file))
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
            conn.execute("INSERT INTO t (val) VALUES ('before_backup')")
            conn.commit()
            conn.close()

            mgr = DatabaseBackupManager(backup_root=backup_dir, databases={"oms": db_file})
            b_res = mgr.create_backup(label="pre_upgrade")
            self.assertTrue(b_res["ok"])
            self.assertTrue(b_res["databases"]["oms"]["integrity_ok"])
            snap_id = b_res["snapshot_id"]

            # Mutate live DB after backup
            conn = sqlite3.connect(str(db_file))
            conn.execute("UPDATE t SET val = 'corrupted_after'")
            conn.commit()
            conn.close()

            # Restore snapshot atomically
            r_res = mgr.restore_backup(snap_id)
            self.assertTrue(r_res["ok"])
            self.assertTrue(verify_sqlite_integrity(db_file))

            conn = sqlite3.connect(str(db_file))
            val = conn.execute("SELECT val FROM t WHERE id = 1").fetchone()[0]
            conn.close()
            self.assertEqual(val, "before_backup")


if __name__ == "__main__":
    unittest.main()
