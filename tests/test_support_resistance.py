"""
tests/test_support_resistance.py
================================
Comprehensive deterministic test suite (100% synthetic OHLCV, zero internet or
broker dependencies) for the Multi-Timeframe Support & Resistance Scanner.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from config import IST_TZ
from core.historical import HistoricalDataManager
from screener.engine import ScreenerEngine
from screener.registry import ScreenRegistry
from screener.sr_engine import (
    DEFAULT_SR_PARAMS,
    SR_STRENGTH_WEIGHTS,
    ConfirmedPivot,
    analyze_single_timeframe,
    analyze_zone_interactions_and_strength,
    classify_zone_event,
    cluster_pivots_into_zones,
    compute_atr_series,
    compute_mtf_confluence_and_classify,
    compute_relative_volume_series,
    detect_confirmed_pivots,
    evaluate_symbol_mtf,
    filter_complete_weekly_candles,
)
from screener.universes import UniverseResolver


def _make_daily_candle(
    d: date,
    o: float,
    h: float,
    l: float,
    c: float,
    vol: int = 100000,
) -> Dict[str, Any]:
    ts = datetime(d.year, d.month, d.day, 9, 15, 0, tzinfo=IST_TZ).isoformat(timespec="seconds")
    return {
        "ts_ist": ts,
        "trade_date": d.isoformat(),
        "open": round(float(o), 4),
        "high": round(float(h), 4),
        "low": round(float(l), 4),
        "close": round(float(c), 4),
        "volume": int(vol),
        "oi": 0,
        "source": "synthetic",
    }


def _make_series_from_closes(
    closes: List[float],
    start: date = date(2024, 1, 1),
    spread: float = 1.0,
    volumes: List[int] | None = None,
    highs_override: Dict[int, float] | None = None,
    lows_override: Dict[int, float] | None = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cur = start
    hi_ov = highs_override or {}
    lo_ov = lows_override or {}
    for idx, c in enumerate(closes):
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        prev_c = closes[idx - 1] if idx > 0 else c
        o = prev_c
        h = hi_ov.get(idx, max(o, c) + spread)
        l = lo_ov.get(idx, min(o, c) - spread)
        v = volumes[idx] if (volumes and idx < len(volumes)) else 100000
        rows.append(_make_daily_candle(cur, o, h, l, c, vol=v))
        cur += timedelta(days=1)
    return rows


def _make_oscillating_sr_series(
    n_cycles: int = 4,
    support: float = 100.0,
    resistance: float = 120.0,
    tail_bars: List[tuple[float, float, float, float, int]] | None = None,
    start: date = date(2024, 1, 1),
) -> List[Dict[str, Any]]:
    """
    Build a clean multi-touch range-bound series oscillating between `support`
    and `resistance` with well-formed confirmed swing highs and swing lows,
    followed by optional `tail_bars` of (open, high, low, close, volume).
    """
    # Each cycle: 16 bars: down to support (bar 5, >= left_bars=5), up to resistance (bar 12), back toward mid
    pattern = [
        111.0, 109.0, 106.5, 104.5, 102.0,
        support,  # swing low (index 5 in cycle 0 -> has 5 left bars)
        103.5, 106.5, 110.0, 113.5, 116.5, 118.5,
        resistance,  # swing high
        117.0, 114.0, 111.5,
    ]
    rows: List[Dict[str, Any]] = []
    cur = start
    for cyc in range(n_cycles):
        for j, px in enumerate(pattern):
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
            # Small deterministic jitter within cluster tolerance
            jitter = ((cyc % 3) - 1) * 0.15
            if j == 5:
                lo = support + jitter
                cl = support + 1.2
                op = support + 2.0
                hi = support + 2.8
                vol = 180000
            elif j == 12:
                hi = resistance + jitter
                cl = resistance - 1.2
                op = resistance - 2.0
                lo = resistance - 2.8
                vol = 180000
            else:
                op = px
                cl = px
                hi = px + 1.1
                lo = px - 1.1
                vol = 100000
            rows.append(_make_daily_candle(cur, op, hi, lo, cl, vol=vol))
            cur += timedelta(days=1)

    if tail_bars:
        for op, hi, lo, cl, vol in tail_bars:
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
            rows.append(_make_daily_candle(cur, op, hi, lo, cl, vol=vol))
            cur += timedelta(days=1)
    return rows


def _make_intraday_1m_series(
    trading_days: int = 8,
    support: float = 100.0,
    resistance: float = 120.0,
    end_date: date = date(2025, 3, 28),
) -> List[Dict[str, Any]]:
    """Generate clean NSE 1-minute bars (09:15-15:29 IST) oscillating between S & R."""
    dates: List[date] = []
    d = end_date
    while len(dates) < trading_days:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()

    rows: List[Dict[str, Any]] = []
    mid = (support + resistance) * 0.5
    amp = (resistance - support) * 0.5
    bar_idx = 0
    import math

    for td in dates:
        base_dt = datetime(td.year, td.month, td.day, 9, 15, 0, tzinfo=IST_TZ)
        for m in range(375):
            dt = base_dt + timedelta(minutes=m)
            # Slow intraday sine wave hitting support and resistance cleanly
            phase = (bar_idx / 45.0) * math.pi
            px = mid + amp * math.sin(phase)
            hi = min(resistance + 0.15, px + 0.25)
            lo = max(support - 0.15, px - 0.25)
            # Time-of-day U-shaped intraday volume profile
            tod_mult = 2.0 if (m < 30 or m > 345) else 1.0
            rows.append(
                {
                    "ts_ist": dt.isoformat(timespec="seconds"),
                    "trade_date": td.isoformat(),
                    "open": round(px, 2),
                    "high": round(hi, 2),
                    "low": round(lo, 2),
                    "close": round(px, 2),
                    "volume": int(1000 * tod_mult),
                    "oi": 0,
                    "source": "synthetic",
                }
            )
            bar_idx += 1
    return rows


class TestSupportResistanceScanner(unittest.TestCase):
    # ── 1. PIVOT DETECTION TESTS ──────────────────────────────────────────

    def test_pivot_detection_confirmation_equal_highs_and_separation(self) -> None:
        # Clear peak at index 8 (120) and trough at index 18 (90), plus an unconfirmed peak at index 24 (130)
        closes = [
            100, 102, 105, 108, 111, 114, 116, 118,
            120,  # idx 8: confirmed swing high (confirmed at idx 13)
            117, 114, 111, 108, 105, 102, 99, 96, 93,
            90,   # idx 18: confirmed swing low (confirmed at idx 23)
            94, 98, 103, 109, 115,
            130,  # idx 24: unconfirmed (only 2 right bars < 5)
            125, 122,
        ]
        candles = _make_series_from_closes(closes, spread=0.5)
        atrs = compute_atr_series(candles, period=14)
        highs, lows = detect_confirmed_pivots(
            candles, atrs, timeframe="1day", left_bars=5, right_bars=5, min_pivot_separation=3
        )

        self.assertEqual([p.index for p in highs], [8])
        self.assertEqual(highs[0].confirm_index, 13)
        self.assertEqual(highs[0].confirmation_ts, candles[13]["ts_ist"])
        # Unconfirmed idx 24 must NOT appear!
        self.assertTrue(all(p.confirm_index < len(candles) for p in highs + lows))
        self.assertEqual([p.index for p in lows], [18])
        self.assertEqual(lows[0].confirm_index, 23)

        # Deterministic equal highs plateau: [..., 120, 120, 120, ...] -> single pivot at leftmost bar
        plateau = [100, 102, 104, 106, 108, 110, 120, 120, 120, 110, 108, 106, 104, 102, 100]
        p_candles = _make_series_from_closes(plateau, spread=0.0)
        p_atrs = compute_atr_series(p_candles, period=5)
        p_highs, _ = detect_confirmed_pivots(p_candles, p_atrs, timeframe="1day", left_bars=3, right_bars=3, min_pivot_separation=3)
        self.assertEqual(len(p_highs), 1)
        self.assertEqual(p_highs[0].index, 6)

        # Completely flat series produces ZERO spurious pivots
        flat = _make_series_from_closes([100.0] * 25, spread=0.0)
        f_h, f_l = detect_confirmed_pivots(flat, compute_atr_series(flat), timeframe="1day", left_bars=3, right_bars=3)
        self.assertEqual(f_h, [])
        self.assertEqual(f_l, [])

        # Min pivot separation test: two peaks 2 bars apart with min_pivot_separation=4 -> keeps stronger only
        close_peaks = [100, 102, 104, 112, 106, 115, 104, 102, 100, 98, 96]
        cp_candles = _make_series_from_closes(close_peaks, spread=0.0)
        cp_highs, _ = detect_confirmed_pivots(
            cp_candles, compute_atr_series(cp_candles, 5), timeframe="1day", left_bars=1, right_bars=1, min_pivot_separation=4
        )
        self.assertEqual(len(cp_highs), 1)
        self.assertEqual(cp_highs[0].index, 5)
        self.assertEqual(cp_highs[0].price, 115.0)

    # ── 2. CLUSTERING TESTS ───────────────────────────────────────────────

    def test_clustering_bounded_width_no_chaining_no_sr_mixing_and_determinism(self) -> None:
        # ATR = 2.0 -> D = max(2.0 * 0.50, 100 * 0.0025) = 1.0
        def _piv(px: float, ptype: str, idx: int) -> ConfirmedPivot:
            return ConfirmedPivot(
                price=px,
                pivot_type=ptype,
                index=idx,
                confirm_index=idx + 5,
                occurrence_ts=f"2024-01-{idx + 1:02d}T09:15:00+05:30",
                confirmation_ts=f"2024-01-{idx + 6:02d}T09:15:00+05:30",
                timeframe="1day",
                volume=100000.0,
                atr=2.0,
            )

        # Nearby pivots (100.0, 100.4, 100.8 -> span 0.8 <= 1.0) merge; distant (115.0) stays separate
        pivs = [_piv(100.0, "HIGH", 1), _piv(100.4, "HIGH", 8), _piv(100.8, "HIGH", 15), _piv(115.0, "HIGH", 22)]
        zones_a = cluster_pivots_into_zones(pivs, level_type="Resistance", timeframe="1day")
        zones_b = cluster_pivots_into_zones(list(reversed(pivs)), level_type="Resistance", timeframe="1day")
        self.assertEqual(len(zones_a), 2)
        self.assertEqual(len(zones_a[0].pivots), 3)
        self.assertAlmostEqual(zones_a[0].representative_price, 100.4, places=3)
        self.assertEqual([z.zone_id for z in zones_a], [z.zone_id for z in zones_b])

        # S/R never mix: passing HIGH pivots to Support clustering yields 0 zones
        self.assertEqual(cluster_pivots_into_zones(pivs, level_type="Support", timeframe="1day"), [])

        # Anti-chaining ladder: 10 pivots spaced 0.45 apart (total span 4.05 > D=1.0)
        # Single-linkage would chain all 10 into one giant zone of width 4.05.
        # Complete-linkage MUST split them so EVERY zone width <= D (~1.0)!
        ladder = [_piv(100.0 + i * 0.45, "LOW", i + 1) for i in range(10)]
        l_zones = cluster_pivots_into_zones(ladder, level_type="Support", timeframe="1day")
        self.assertGreaterEqual(len(l_zones), 4)
        for z in l_zones:
            self.assertLessEqual(z.zone_high - z.zone_low, 1.05)

    # ── 3. STRENGTH SCORING & CLASSIFICATION TESTS ────────────────────────

    def test_strength_weights_independent_touches_recency_and_whipsaw_penalty(self) -> None:
        self.assertAlmostEqual(sum(SR_STRENGTH_WEIGHTS.values()), 100.0, places=6)

        # Clean oscillating S&R produces high-strength Major/Minor zones bounded in [0, 100]
        clean_candles = _make_oscillating_sr_series(n_cycles=4, support=100.0, resistance=120.0)
        res_clean = analyze_single_timeframe(clean_candles, timeframe="1day", params=DEFAULT_SR_PARAMS)
        self.assertTrue(res_clean["ok"])
        sup_zones = [z for z in res_clean["zones"] if z.level_type == "Support"]
        self.assertGreaterEqual(len(sup_zones), 1)
        s_zone = sup_zones[0]
        self.assertGreaterEqual(s_zone.strength, 0.0)
        self.assertLessEqual(s_zone.strength, 100.0)
        self.assertEqual(s_zone.touches, 4)  # 4 independent cycles -> 4 independent touch episodes
        self.assertGreaterEqual(s_zone.reactions, 3)

        # Consecutive candles sitting inside the zone count as ONE episode, not 10 touches!
        base = _make_oscillating_sr_series(n_cycles=2, support=100.0, resistance=120.0)
        # Append 8 consecutive flat bars hovering right at support 100.2
        tail = [(100.3, 100.8, 99.9, 100.2, 100000) for _ in range(8)]
        flat_sit = _make_oscillating_sr_series(n_cycles=2, support=100.0, resistance=120.0, tail_bars=tail)
        res_sit = analyze_single_timeframe(flat_sit, timeframe="1day", params=DEFAULT_SR_PARAMS)
        s_sit = [z for z in res_sit["zones"] if z.level_type == "Support" and abs(z.representative_price - 100.0) < 2.0][0]
        self.assertEqual(s_sit.touches, 3)  # 2 cycles + 1 episode for all 8 consecutive bars!

        # Repeated uncontrolled whipsaw crossings reduce strength compared to clean support
        whipsaw_tail = [
            (102.0, 102.5, 94.0, 95.0, 100000),
            (95.0, 106.0, 94.5, 105.0, 100000),
            (105.0, 105.5, 94.0, 95.0, 100000),
            (95.0, 106.0, 94.5, 105.0, 100000),
            (105.0, 105.5, 94.0, 95.0, 100000),
            (95.0, 106.0, 94.5, 105.0, 100000),
        ]
        whipsaw = _make_oscillating_sr_series(n_cycles=3, support=100.0, resistance=120.0, tail_bars=whipsaw_tail)
        res_whip = analyze_single_timeframe(whipsaw, timeframe="1day", params=DEFAULT_SR_PARAMS)
        s_whip = [z for z in res_whip["zones"] if z.level_type == "Support" and abs(z.representative_price - 100.0) < 2.0][0]
        self.assertGreater(s_whip.uncontrolled_crossings, 2)
        self.assertLess(s_whip.strength, s_zone.strength)

        # Recency: append 60 bars hovering at 115 (far from support 100) -> support recency drops
        distant_tail = [(115.0, 116.0, 114.0, 115.0, 100000) for _ in range(60)]
        old_sup = _make_oscillating_sr_series(n_cycles=4, support=100.0, resistance=120.0, tail_bars=distant_tail)
        res_old = analyze_single_timeframe(old_sup, timeframe="1day", params=DEFAULT_SR_PARAMS)
        s_old = [z for z in res_old["zones"] if z.level_type == "Support" and abs(z.representative_price - 100.0) < 2.0][0]
        self.assertLess(s_old.strength_breakdown["recency"], s_zone.strength_breakdown["recency"])

        # Confluence never double-counts duplicate zones from the same TF
        z_1d = s_zone
        z_1w_a = analyze_single_timeframe(clean_candles, timeframe="1week", params=DEFAULT_SR_PARAMS)["zones"][0]
        z_1w_b = analyze_single_timeframe(clean_candles, timeframe="1week", params=DEFAULT_SR_PARAMS)["zones"][0]
        z_1w_b.zone_id = z_1w_a.zone_id + "_dup"
        compute_mtf_confluence_and_classify({"1day": [z_1d], "1week": [z_1w_a, z_1w_b]}, DEFAULT_SR_PARAMS)
        self.assertEqual(z_1d.confluence_count, 2)  # Only 2 distinct TFs (1w, 1d), not 3!
        self.assertLessEqual(z_1d.confluence_bonus, 8.0)  # Only 1week's weight once!

    # ── 4. ALL 9 EVENT CLASSIFICATION TESTS ───────────────────────────────

    def _run_event_scenario(self, tail_bars: List[tuple[float, float, float, float, int]]) -> Dict[str, Any]:
        candles = _make_oscillating_sr_series(n_cycles=4, support=100.0, resistance=120.0, tail_bars=tail_bars)
        out = evaluate_symbol_mtf(
            symbol="SYNTH",
            exchange="NSE",
            candles_by_tf={"1day": candles},
            params=DEFAULT_SR_PARAMS,
            fno_eligible=True,
        )
        return {r["event"]: r for r in out["rows"]}

    def test_all_nine_price_interaction_events_and_volume_rules(self) -> None:
        # 1) NEAR_SUPPORT (single-bar touch at support WITHOUT subsequent recovery -> NEAR_SUPPORT, NOT SUPPORT_BOUNCE!)
        ev_near_sup = self._run_event_scenario([
            (108.0, 108.5, 104.5, 105.0, 100000),
            (105.0, 105.2, 100.2, 100.8, 120000),
        ])
        self.assertIn("NEAR_SUPPORT", ev_near_sup)
        self.assertNotIn("SUPPORT_BOUNCE", ev_near_sup)

        # 2) NEAR_RESISTANCE
        ev_near_res = self._run_event_scenario([
            (114.0, 117.0, 113.5, 116.5, 100000),
            (116.5, 119.8, 116.0, 119.3, 120000),
        ])
        self.assertIn("NEAR_RESISTANCE", ev_near_res)

        # 3) SUPPORT_BOUNCE (touch followed by completed-candle recovery >= 0.50 ATR)
        ev_bounce = self._run_event_scenario([
            (106.0, 106.5, 100.0, 101.0, 150000),
            (101.2, 105.5, 101.0, 104.8, 190000),
        ])
        self.assertIn("SUPPORT_BOUNCE", ev_bounce)
        self.assertGreater(ev_bounce["SUPPORT_BOUNCE"]["event_quality"], 50.0)

        # 4) RESISTANCE_REJECTION (touch followed by completed-candle downward reaction)
        ev_rej = self._run_event_scenario([
            (115.0, 120.0, 114.5, 118.8, 150000),
            (118.5, 118.8, 114.0, 114.8, 190000),
        ])
        self.assertIn("RESISTANCE_REJECTION", ev_rej)

        # 5) RESISTANCE_BREAKOUT (completed close > zone_high + 0.20 ATR)
        # Also verify high volume boosts event_quality vs low volume
        ev_bo_high_vol = self._run_event_scenario([
            (116.0, 119.0, 115.5, 118.5, 100000),
            (119.0, 125.0, 118.8, 124.2, 260000),
        ])
        ev_bo_low_vol = self._run_event_scenario([
            (116.0, 119.0, 115.5, 118.5, 100000),
            (119.0, 125.0, 118.8, 124.2, 70000),
        ])
        self.assertIn("RESISTANCE_BREAKOUT", ev_bo_high_vol)
        self.assertIn("RESISTANCE_BREAKOUT", ev_bo_low_vol)
        self.assertTrue(ev_bo_high_vol["RESISTANCE_BREAKOUT"]["volume_confirmed"])
        self.assertFalse(ev_bo_low_vol["RESISTANCE_BREAKOUT"]["volume_confirmed"])
        self.assertGreater(
            ev_bo_high_vol["RESISTANCE_BREAKOUT"]["event_quality"],
            ev_bo_low_vol["RESISTANCE_BREAKOUT"]["event_quality"],
        )

        # High volume alone WITHOUT breakout price close must NOT create RESISTANCE_BREAKOUT
        ev_vol_only = self._run_event_scenario([
            (114.0, 119.2, 113.5, 118.9, 500000),
        ])
        self.assertNotIn("RESISTANCE_BREAKOUT", ev_vol_only)

        # 6) SUPPORT_BREAKDOWN
        ev_bd = self._run_event_scenario([
            (104.0, 104.5, 100.8, 101.2, 100000),
            (101.0, 101.2, 95.0, 95.8, 250000),
        ])
        self.assertIn("SUPPORT_BREAKDOWN", ev_bd)

        # 7) BREAKOUT_RETEST (breakout -> pullback to broken resistance -> hold above as support)
        ev_bo_retest = self._run_event_scenario([
            (117.0, 119.0, 116.5, 118.5, 100000),
            (119.0, 125.5, 118.8, 124.8, 250000),  # confirmed breakout
            (124.8, 126.0, 122.5, 123.5, 130000),
            (123.5, 123.8, 120.2, 121.0, 110000),  # pullback retest to broken resistance ~120
            (121.2, 124.5, 120.8, 123.8, 180000),  # holds as support!
        ])
        self.assertIn("BREAKOUT_RETEST", ev_bo_retest)

        # 8) BREAKDOWN_RETEST (breakdown -> rally back to broken support -> rejected downward)
        ev_bd_retest = self._run_event_scenario([
            (103.0, 103.5, 100.8, 101.2, 100000),
            (101.0, 101.2, 95.0, 95.5, 250000),    # confirmed breakdown
            (95.5, 97.5, 94.8, 96.8, 130000),
            (96.8, 99.9, 96.5, 99.0, 110000),      # retest of broken support ~100
            (98.8, 99.2, 95.5, 96.2, 180000),      # holds as resistance!
        ])
        self.assertIn("BREAKDOWN_RETEST", ev_bd_retest)

        # 9) FALSE_BREAKOUT (confirmed breakout bar followed by completed close back inside/below zone)
        ev_false_bo = self._run_event_scenario([
            (117.0, 119.0, 116.5, 118.5, 100000),
            (119.0, 125.0, 118.8, 124.0, 220000),  # confirmed breakout
            (123.5, 123.8, 117.5, 118.2, 240000),  # slams back below resistance -> FALSE_BREAKOUT!
        ])
        self.assertIn("FALSE_BREAKOUT", ev_false_bo)
        self.assertGreater(ev_false_bo["FALSE_BREAKOUT"]["invalidation_price"], 124.0)

    def test_intraday_tod_volume_baseline(self) -> None:
        rows = _make_intraday_1m_series(trading_days=6, support=100.0, resistance=120.0)
        # Spike volume ONLY on the last 09:15 bar vs prior 09:15 bars (which all have 2000 vol)
        from core.historical import resample_ohlcv_rows

        bars_5m = resample_ohlcv_rows(rows, "5minute", exchange="NSE")
        # Multiply last bar volume by 3x
        bars_5m[-1]["volume"] = int(bars_5m[-1]["volume"] * 3)
        rvs = compute_relative_volume_series(bars_5m, timeframe="5minute")
        self.assertGreater(rvs[-1], 2.5)

    # ── 5. MULTI-TIMEFRAME, DATA REUSE & MISSING DATA TESTS ───────────────

    def test_mtf_registry_base_data_reuse_confluence_and_missing_intraday(self) -> None:
        reg = ScreenRegistry()
        screen = reg.get("support_resistance")
        meta = screen.metadata()
        self.assertTrue(meta["supports_multi_timeframe"])
        self.assertEqual(meta["default_timeframes"], ["5minute", "15minute", "60minute", "1day", "1week"])
        param_names = [p["name"] for p in meta["params"]]
        expected_14 = [
            "left_bars",
            "right_bars",
            "min_pivot_separation",
            "atr_period",
            "cluster_atr_multiplier",
            "cluster_min_pct",
            "major_strength",
            "minor_strength",
            "minimum_touches",
            "near_distance_atr",
            "breakout_buffer_atr",
            "reversal_distance_atr",
            "retest_tolerance_atr",
            "relative_volume_threshold",
        ]
        self.assertEqual(param_names, expected_14)

        with tempfile.TemporaryDirectory() as tmp:
            hm = HistoricalDataManager(
                db_path=Path(tmp) / "hist.db",
                parquet_dir=Path(tmp) / "parquet",
            )
            eng = ScreenerEngine(hist_manager=hm, registry=reg, universes=UniverseResolver())

            # Build daily candles spanning ~2 years (100+ weeks) with clear S&R at 100 and 120
            daily_rows = _make_oscillating_sr_series(
                n_cycles=26,
                support=100.0,
                resistance=120.0,
                tail_bars=[(106.0, 106.5, 100.0, 101.0, 180000), (101.2, 105.0, 101.0, 104.5, 220000)],
                start=date(2023, 6, 1),
            )
            last_d = date.fromisoformat(daily_rows[-1]["trade_date"])
            intraday_1m = _make_intraday_1m_series(trading_days=10, support=100.0, resistance=120.0, end_date=last_d)

            fetch_calls: List[tuple[str, str]] = []
            dl_calls: List[tuple[str, str]] = []
            orig_download = hm.download

            def spy_download(*args: Any, **kwargs: Any) -> Dict[str, Any]:
                dl_calls.append((str(kwargs.get("symbol", "")).upper(), str(kwargs.get("timeframe", ""))))
                return orig_download(*args, **kwargs)

            hm.download = spy_download  # type: ignore[method-assign]

            def fake_fetcher(sym: str, exch: str, tf: str, start_d: date, end_d: date, **kwargs: Any) -> List[Dict[str, Any]]:
                fetch_calls.append((sym, tf))
                if sym == "NOINTRA" and tf == "1minute":
                    raise RuntimeError("Provider has no intraday 1m retention for NOINTRA")
                if tf == "1minute":
                    return [r for r in intraday_1m if start_d.isoformat() <= r["trade_date"] <= end_d.isoformat()]
                if tf == "1day":
                    return [r for r in daily_rows if start_d.isoformat() <= r["trade_date"] <= end_d.isoformat()]
                raise AssertionError(f"Engine MUST ONLY fetch 1minute or 1day base series, got {tf}!")

            res = eng.scan(
                symbols=["RELIANCE", "NOINTRA"],
                universe="CUSTOM",
                exchange="NSE",
                screen_id="support_resistance",
                timeframes=["5minute", "15minute", "60minute", "1day", "1week"],
                as_of_date=last_d.isoformat(),
                provider="yfinance",
                fetcher=fake_fetcher,
            )

            self.assertTrue(res["ok"])
            self.assertTrue(res["multi_timeframe"])
            # Strictly only canonical base timeframes 1minute and 1day fetched — ONCE per base per symbol!
            self.assertTrue(all(tf in {"1minute", "1day"} for _, tf in fetch_calls))
            rel_dl_tfs = [tf for sym, tf in dl_calls if sym == "RELIANCE"]
            self.assertEqual(rel_dl_tfs.count("1minute"), 1)
            self.assertEqual(rel_dl_tfs.count("1day"), 1)

            # Second identical scan for RELIANCE must reuse cache with 0 API calls
            res2 = eng.scan(
                symbols=["RELIANCE"],
                universe="CUSTOM",
                exchange="NSE",
                screen_id="support_resistance",
                timeframes=["5minute", "15minute", "60minute", "1day", "1week"],
                as_of_date=last_d.isoformat(),
                provider="yfinance",
                fetcher=fake_fetcher,
            )
            self.assertEqual(res2["api_calls_made"], 0)

            # NOINTRA must report missing 5m, 15m, 1h explicitly AND still preserve 1d/1w results!
            nointra_missing_tfs = {m["timeframe"] for m in res["missing_data"] if m["symbol"] == "NOINTRA"}
            self.assertTrue({"5m", "15m", "1h"}.issubset(nointra_missing_tfs))
            nointra_rows = [r for r in res["results"] if r["symbol"] == "NOINTRA"]
            self.assertTrue(len(nointra_rows) > 0)
            self.assertTrue(all(r["timeframe"] in {"1d", "1w"} for r in nointra_rows))

            # RELIANCE should detect multi-timeframe confluence across aligned 100/120 levels
            rel_rows = [r for r in res["results"] if r["symbol"] == "RELIANCE"]
            self.assertTrue(len(rel_rows) > 0)
            self.assertTrue(any(r["confluence_count"] >= 2 and r["confluence_id"] for r in rel_rows))
            # Weak zones must never leak into default output
            self.assertTrue(all(r["classification"] in {"Major", "Minor"} for r in res["results"]))

    # ── 6. POINT-IN-TIME / ZERO-LOOKAHEAD TESTS ───────────────────────────

    def test_point_in_time_zero_lookahead_and_incomplete_weekly_exclusion(self) -> None:
        # Build daily series ending on a Wednesday (mid-week)
        # 2024-06-12 is a Wednesday; 2024-06-14 is Friday of that week.
        base_rows = _make_oscillating_sr_series(n_cycles=8, support=100.0, resistance=120.0, start=date(2024, 1, 1))
        # Find a Wednesday near the end
        wed_idx = None
        for idx in range(len(base_rows) - 1, 40, -1):
            d_obj = date.fromisoformat(base_rows[idx]["trade_date"])
            if d_obj.weekday() == 2:  # Wednesday
                wed_idx = idx
                break
        self.assertIsNotNone(wed_idx)
        assert wed_idx is not None
        as_of_wed = base_rows[wed_idx]["trade_date"]
        as_of_wed_date = date.fromisoformat(as_of_wed)

        # Verify incomplete weekly candle is EXCLUDED on Wednesday
        weekly_wed = filter_complete_weekly_candles(base_rows, as_of_date=as_of_wed, exchange="NSE")
        self.assertTrue(len(weekly_wed) > 0)
        for wbar in weekly_wed:
            wd = date.fromisoformat(wbar["trade_date"])
            iso_y, iso_w, _ = wd.isocalendar()
            friday = date.fromisocalendar(iso_y, iso_w, 5)
            self.assertLessEqual(friday, as_of_wed_date)

        # Now construct TWO datasets:
        # Dataset A: ends strictly on as_of_wed
        # Dataset B: Dataset A + 30 extreme wild future candles (crashing to 50 and surging to 250)
        ds_a = base_rows[: wed_idx + 1]
        future_tail = []
        d_cur = as_of_wed_date + timedelta(days=1)
        for k in range(30):
            while d_cur.weekday() >= 5:
                d_cur += timedelta(days=1)
            px = 250.0 if k % 2 == 0 else 50.0
            future_tail.append(_make_daily_candle(d_cur, px, px + 10.0, px - 10.0, px, vol=999999))
            d_cur += timedelta(days=1)
        ds_b = ds_a + future_tail

        out_a = evaluate_symbol_mtf(
            symbol="PITTEST",
            exchange="NSE",
            candles_by_tf={
                "1day": ds_a,
                "1week": filter_complete_weekly_candles(ds_a, as_of_date=as_of_wed),
            },
            params=DEFAULT_SR_PARAMS,
            as_of_date=as_of_wed,
        )
        out_b = evaluate_symbol_mtf(
            symbol="PITTEST",
            exchange="NSE",
            candles_by_tf={
                "1day": ds_b,
                "1week": filter_complete_weekly_candles(ds_b, as_of_date=as_of_wed),
            },
            params=DEFAULT_SR_PARAMS,
            as_of_date=as_of_wed,
        )

        # Output MUST be 100% identical despite 30 extreme future candles appended!
        self.assertEqual(out_a["rows"], out_b["rows"])
        self.assertEqual(
            [(z.zone_id, z.strength, z.touches, z.reactions) for z in out_a["zones_by_tf"].get("1day", [])],
            [(z.zone_id, z.strength, z.touches, z.reactions) for z in out_b["zones_by_tf"].get("1day", [])],
        )
        self.assertEqual(
            [(z.zone_id, z.strength, z.touches, z.reactions) for z in out_a["zones_by_tf"].get("1week", [])],
            [(z.zone_id, z.strength, z.touches, z.reactions) for z in out_b["zones_by_tf"].get("1week", [])],
        )


if __name__ == "__main__":
    unittest.main()
