"""
screener/sr_engine.py
=====================
Deterministic, point-in-time (zero-repainting) Multi-Timeframe Support &
Resistance (S&R) quantitative engine for AlgoDesk.

Implements:
1. Confirmed fractal pivot detection (strict right-side confirmation, deterministic
   equal-high / equal-low plateau handling, minimum pivot separation).
2. ATR-normalized bounded complete-linkage horizontal price clustering (prevents
   single-linkage chaining, keeps Support and Resistance strictly separate).
3. Historical completed-candle interaction analysis (independent touch episodes,
   successful reactions, reversal quality, uncontrolled crossing penalties).
4. Normalized 0-100 zone strength scoring across 7 capped/saturating components
   whose weights sum to 100.
5. Major / Minor / Weak classification.
6. Multi-timeframe confluence detection across 5m, 15m, 1h, 1d, 1w without
   double-counting overlapping evidence.
7. Event classification: NEAR_SUPPORT, NEAR_RESISTANCE, SUPPORT_BOUNCE,
   RESISTANCE_REJECTION, RESISTANCE_BREAKOUT, SUPPORT_BREAKDOWN,
   BREAKOUT_RETEST, BREAKDOWN_RETEST, and FALSE_BREAKOUT.
8. Time-of-day bucket relative volume confirmation for intraday bars with
   rolling-volume fallback for daily/weekly or sparse intraday history.
9. Strict point-in-time as_of_date enforcement including incomplete weekly candle
   exclusion.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.historical import normalize_timeframe, resample_ohlcv_rows

# Canonical multi-timeframe definitions
SR_DEFAULT_TIMEFRAMES: Tuple[str, ...] = (
    "5minute",
    "15minute",
    "60minute",
    "1day",
    "1week",
)

TF_SHORT_LABELS: Dict[str, str] = {
    "5minute": "5m",
    "15minute": "15m",
    "60minute": "1h",
    "1day": "1d",
    "1week": "1w",
}

TF_HIERARCHY_RANK: Dict[str, int] = {
    "1week": 5,
    "1day": 4,
    "60minute": 3,
    "15minute": 2,
    "5minute": 1,
}

# Contribution of each agreeing OTHER timeframe to the 15-point confluence bucket
TF_CONFLUENCE_WEIGHT: Dict[str, float] = {
    "1week": 8.0,
    "1day": 6.0,
    "60minute": 4.0,
    "15minute": 2.5,
    "5minute": 1.5,
}

# Explicit strength scoring weights — validated sum == 100
SR_STRENGTH_WEIGHTS: Dict[str, float] = {
    "touches": 20.0,
    "reactions": 20.0,
    "volume": 15.0,
    "recency": 10.0,
    "age": 10.0,
    "confluence": 15.0,
    "reversal": 10.0,
}

assert abs(sum(SR_STRENGTH_WEIGHTS.values()) - 100.0) < 1e-9, "SR_STRENGTH_WEIGHTS must sum to 100"

INTRADAY_TIMEFRAMES = {"1minute", "5minute", "15minute", "30minute", "60minute"}

ALL_SR_EVENTS: Tuple[str, ...] = (
    "NEAR_SUPPORT",
    "NEAR_RESISTANCE",
    "SUPPORT_BOUNCE",
    "RESISTANCE_REJECTION",
    "RESISTANCE_BREAKOUT",
    "SUPPORT_BREAKDOWN",
    "BREAKOUT_RETEST",
    "BREAKDOWN_RETEST",
    "FALSE_BREAKOUT",
)

DEFAULT_SR_PARAMS: Dict[str, Any] = {
    "left_bars": 5,
    "right_bars": 5,
    "min_pivot_separation": 3,
    "atr_period": 14,
    "cluster_atr_multiplier": 0.50,
    "cluster_min_pct": 0.25,
    "major_strength": 70,
    "minor_strength": 40,
    "minimum_touches": 2,
    "near_distance_atr": 0.50,
    "breakout_buffer_atr": 0.20,
    "reversal_distance_atr": 0.50,
    "retest_tolerance_atr": 0.25,
    "relative_volume_threshold": 1.50,
}


def short_tf_label(tf: str) -> str:
    norm = normalize_timeframe(tf)
    if norm in TF_SHORT_LABELS:
        return TF_SHORT_LABELS[norm]
    if norm.endswith("minute"):
        return f"{norm[:-6]}m"
    if norm.endswith("day"):
        return f"{norm[:-3]}d"
    if norm.endswith("week"):
        return f"{norm[:-4]}w"
    return norm


@dataclass(frozen=True)
class ConfirmedPivot:
    price: float
    pivot_type: str  # "HIGH" | "LOW"
    index: int
    confirm_index: int
    occurrence_ts: str
    confirmation_ts: str
    timeframe: str
    volume: float
    atr: float


@dataclass
class SRZone:
    zone_id: str
    level_type: str  # "Support" | "Resistance"
    zone_low: float
    zone_high: float
    representative_price: float
    first_occurrence: str
    most_recent_occurrence: str
    first_index: int
    most_recent_index: int
    timeframe: str
    atr_at_zone: float
    pivots: List[ConfirmedPivot] = field(default_factory=list)
    touches: int = 0
    reactions: int = 0
    uncontrolled_crossings: int = 0
    avg_rel_volume: float = 1.0
    avg_mfe_atr: float = 0.0
    base_strength: float = 0.0
    confluence_bonus: float = 0.0
    strength: float = 0.0
    strength_breakdown: Dict[str, float] = field(default_factory=dict)
    classification: str = "Weak"  # "Major" | "Minor" | "Weak"
    status: str = "active"  # "active" | "broken"
    confluence_id: str = ""
    confluence_timeframes: List[str] = field(default_factory=list)
    confluence_count: int = 1
    confluence_highest_strength: float = 0.0
    confluence_strength: float = 0.0


@dataclass
class SREvent:
    event: str
    event_ts: str
    confirmation_ts: str
    event_quality: float
    invalidation_price: float
    relative_volume: float
    volume_confirmed: bool
    distance_pct: float
    distance_atr: float


# ── Point-in-time helpers ─────────────────────────────────────────────────


def _extract_trade_date(row: Dict[str, Any]) -> str:
    td = row.get("trade_date")
    if td:
        return str(td)[:10]
    ts = str(row.get("ts_ist") or "")
    return ts[:10]


def _extract_ts(row: Dict[str, Any]) -> str:
    return str(row.get("ts_ist") or row.get("trade_date") or "")


def slice_candles_as_of(candles: Sequence[Dict[str, Any]], as_of_date: Optional[str]) -> List[Dict[str, Any]]:
    """Strictly remove any candle dated after `as_of_date` (YYYY-MM-DD)."""
    if not as_of_date:
        return list(candles)
    cutoff = str(as_of_date).strip()[:10]
    return [c for c in candles if _extract_trade_date(c) <= cutoff]


def filter_complete_weekly_candles(
    daily_rows: Sequence[Dict[str, Any]],
    as_of_date: Optional[str],
    exchange: str = "NSE",
) -> List[Dict[str, Any]]:
    """
    Resample daily candles (already sliced <= as_of_date) into canonical ISO
    weekly candles and strictly EXCLUDE any incomplete weekly candle whose ISO
    Friday falls after `as_of_date`.

    Guarantees that historical mid-week scans never consume partial weekly bars
    and appending future candles after `as_of_date` produces identical output.
    """
    sliced_daily = slice_candles_as_of(daily_rows, as_of_date)
    if not sliced_daily:
        return []

    weekly = resample_ohlcv_rows(sliced_daily, "1week", exchange=exchange)
    if not weekly:
        return []

    if as_of_date:
        cutoff_d = date.fromisoformat(str(as_of_date).strip()[:10])
    else:
        cutoff_d = date.fromisoformat(_extract_trade_date(sliced_daily[-1]))

    completed: List[Dict[str, Any]] = []
    for row in weekly:
        td_str = _extract_trade_date(row)
        if not td_str:
            continue
        row_d = date.fromisoformat(td_str)
        iso_yr, iso_wk, _ = row_d.isocalendar()
        week_friday = date.fromisocalendar(iso_yr, iso_wk, 5)
        if week_friday <= cutoff_d:
            completed.append(row)
    return completed


# ── Fast pure-Python O(N) ATR & Relative Volume ───────────────────────────


def compute_atr_series(candles: Sequence[Dict[str, Any]], period: int = 14) -> List[float]:
    """Wilder's ATR(period) in pure Python O(N), strictly causal."""
    n = len(candles)
    if n == 0:
        return []
    p = max(1, int(period))
    trs: List[float] = [0.0] * n
    prev_close = float(candles[0]["close"])
    trs[0] = max(0.0, float(candles[0]["high"]) - float(candles[0]["low"]))

    for i in range(1, n):
        hi = float(candles[i]["high"])
        lo = float(candles[i]["low"])
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs[i] = tr
        prev_close = float(candles[i]["close"])

    atrs: List[float] = [0.0] * n
    running_sum = 0.0
    for i in range(min(p, n)):
        running_sum += trs[i]
        val = running_sum / float(i + 1)
        if val <= 0.0:
            val = max(float(candles[i]["close"]) * 0.005, 1e-6)
        atrs[i] = val

    if n > p:
        prev_atr = atrs[p - 1]
        inv_p = 1.0 / float(p)
        pm1 = float(p - 1)
        for i in range(p, n):
            prev_atr = (prev_atr * pm1 + trs[i]) * inv_p
            atrs[i] = prev_atr if prev_atr > 0.0 else max(float(candles[i]["close"]) * 0.005, 1e-6)

    return atrs


def compute_relative_volume_series(
    candles: Sequence[Dict[str, Any]],
    timeframe: str,
    rolling_window: int = 20,
    min_tod_samples: int = 3,
) -> List[float]:
    """
    Causal relative volume baseline (strictly uses bars < i).

    - Intraday timeframes (5m/15m/1h): compares bar volume against prior bars
      from the SAME IST HH:MM time-of-day bucket when >= `min_tod_samples` prior
      observations exist; otherwise falls back to rolling `rolling_window` mean.
    - Daily / Weekly: rolling `rolling_window` mean of prior bars.
    """
    n = len(candles)
    if n == 0:
        return []

    norm_tf = normalize_timeframe(timeframe)
    is_intraday = norm_tf in INTRADAY_TIMEFRAMES
    win = max(3, int(rolling_window))

    rel_vols: List[float] = [1.0] * n
    vols: List[float] = [max(0.0, float(c.get("volume") or 0.0)) for c in candles]

    # Rolling sum over prior `win` bars
    roll_sum = 0.0
    # Time-of-day bucket history (list of recent volumes per HH:MM)
    tod_history: Dict[str, List[float]] = {}
    tod_sums: Dict[str, float] = {}

    for i in range(n):
        v = vols[i]
        baseline = 0.0

        tod_key = ""
        if is_intraday:
            ts = _extract_ts(candles[i])
            if len(ts) >= 16 and "T" in ts:
                tod_key = ts[11:16]
            if tod_key:
                hist = tod_history.get(tod_key)
                if hist and len(hist) >= min_tod_samples:
                    baseline = tod_sums[tod_key] / float(len(hist))

        if baseline <= 0.0 and i > 0:
            denom = min(i, win)
            baseline = roll_sum / float(denom)

        if baseline > 0.0 and v > 0.0:
            rel_vols[i] = round(v / baseline, 4)
        elif baseline > 0.0 and v == 0.0:
            rel_vols[i] = 0.0
        else:
            rel_vols[i] = 1.0

        # Update causal history AFTER computing rel_vols[i]
        roll_sum += v
        if i >= win:
            roll_sum -= vols[i - win]

        if is_intraday and tod_key and v > 0.0:
            hist = tod_history.get(tod_key)
            if hist is None:
                tod_history[tod_key] = [v]
                tod_sums[tod_key] = v
            else:
                hist.append(v)
                tod_sums[tod_key] += v
                if len(hist) > win:
                    oldest = hist.pop(0)
                    tod_sums[tod_key] -= oldest

    return rel_vols


# ── Step 1: Confirmed Fractal Pivot Detection (4.1) ──────────────────────


def detect_confirmed_pivots(
    candles: Sequence[Dict[str, Any]],
    atr_series: Sequence[float],
    timeframe: str,
    left_bars: int = 5,
    right_bars: int = 5,
    min_pivot_separation: int = 3,
) -> Tuple[List[ConfirmedPivot], List[ConfirmedPivot]]:
    """
    Detect confirmed swing highs and swing lows strictly respecting right-side
    confirmation (`confirm_index = i + right_bars <= n - 1`).

    Equal highs/lows are handled deterministically:
    - Swing High: strictly greater than all `left_bars` left candles AND `>=`
      all `right_bars` right candles, and not a flat constant plateau.
    - Swing Low: strictly less than all `left_bars` left candles AND `<=`
      all `right_bars` right candles, and not a flat constant plateau.
    This guarantees that in a flat double-top/bottom plateau, only the leftmost
    bar triggers deterministically with zero duplicate pivots.
    """
    n = len(candles)
    lb = max(1, int(left_bars))
    rb = max(1, int(right_bars))
    sep = max(1, int(min_pivot_separation))

    if n < lb + rb + 1:
        return [], []

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    vols = [float(c.get("volume") or 0.0) for c in candles]

    swing_highs: List[ConfirmedPivot] = []
    swing_lows: List[ConfirmedPivot] = []

    end_i = n - rb  # exclusive; guarantees i + rb <= n - 1
    for i in range(lb, end_i):
        h_i = highs[i]
        # Check Swing High: strict on left, >= on right
        is_high = True
        min_win_h = h_i
        for j in range(i - lb, i):
            hj = highs[j]
            if hj >= h_i:
                is_high = False
                break
            if hj < min_win_h:
                min_win_h = hj
        if is_high:
            for j in range(i + 1, i + rb + 1):
                hj = highs[j]
                if hj > h_i:
                    is_high = False
                    break
                if hj < min_win_h:
                    min_win_h = hj

        if is_high and h_i > min_win_h:
            confirm_idx = i + rb
            piv = ConfirmedPivot(
                price=h_i,
                pivot_type="HIGH",
                index=i,
                confirm_index=confirm_idx,
                occurrence_ts=_extract_ts(candles[i]),
                confirmation_ts=_extract_ts(candles[confirm_idx]),
                timeframe=timeframe,
                volume=vols[i],
                atr=float(atr_series[i]),
            )
            if swing_highs and (i - swing_highs[-1].index) < sep:
                if piv.price > swing_highs[-1].price:
                    swing_highs[-1] = piv
            else:
                swing_highs.append(piv)

        l_i = lows[i]
        # Check Swing Low: strict on left, <= on right
        is_low = True
        max_win_l = l_i
        for j in range(i - lb, i):
            lj = lows[j]
            if lj <= l_i:
                is_low = False
                break
            if lj > max_win_l:
                max_win_l = lj
        if is_low:
            for j in range(i + 1, i + rb + 1):
                lj = lows[j]
                if lj < l_i:
                    is_low = False
                    break
                if lj > max_win_l:
                    max_win_l = lj

        if is_low and l_i < max_win_l:
            confirm_idx = i + rb
            piv = ConfirmedPivot(
                price=l_i,
                pivot_type="LOW",
                index=i,
                confirm_index=confirm_idx,
                occurrence_ts=_extract_ts(candles[i]),
                confirmation_ts=_extract_ts(candles[confirm_idx]),
                timeframe=timeframe,
                volume=vols[i],
                atr=float(atr_series[i]),
            )
            if swing_lows and (i - swing_lows[-1].index) < sep:
                if piv.price < swing_lows[-1].price:
                    swing_lows[-1] = piv
            else:
                swing_lows.append(piv)

    return swing_highs, swing_lows


# ── Step 2: ATR-Normalized Bounded Complete-Linkage Clustering (4.2) ──────


def _cluster_tolerance(
    prices: Sequence[float],
    atrs: Sequence[float],
    cluster_atr_multiplier: float,
    cluster_min_pct: float,
) -> float:
    med_price = statistics.median(prices)
    med_atr = statistics.median(atrs)
    return max(med_atr * cluster_atr_multiplier, med_price * (cluster_min_pct / 100.0))


def cluster_pivots_into_zones(
    pivots: Sequence[ConfirmedPivot],
    level_type: str,  # "Support" | "Resistance"
    timeframe: str,
    cluster_atr_multiplier: float = 0.50,
    cluster_min_pct: float = 0.25,
) -> List[SRZone]:
    """
    Deterministic complete-linkage hierarchical clustering of pivots into S/R zones.

    - Only merges two clusters when the merged cluster's total diameter
      `(max_price - min_price) <= D`, where `D = max(ATR * mult, Price * min_pct/100)`.
    - Strictly prevents single-linkage chaining and enforces a hard upper bound
      on zone width.
    - Retains all constituent `ConfirmedPivot` objects.
    """
    if not pivots:
        return []

    expected_ptype = "HIGH" if level_type == "Resistance" else "LOW"
    filtered = [p for p in pivots if p.pivot_type == expected_ptype]
    if not filtered:
        return []

    # Sort deterministically by price then chronological index
    ordered = sorted(filtered, key=lambda p: (p.price, p.index))

    # Each cluster is a list of pivots
    clusters: List[List[ConfirmedPivot]] = [[p] for p in ordered]

    # Deterministic complete-linkage agglomerative merge
    while len(clusters) > 1:
        best_pair: Optional[Tuple[int, int]] = None
        best_diam = float("inf")
        best_tie = (float("inf"), 10**9)

        # Since clusters remain sorted by min price, adjacent clusters are
        # primary candidates, though checking all pairs guarantees exactness
        # for small K (typically K <= 60 pivots).
        k = len(clusters)
        for a in range(k):
            ca = clusters[a]
            min_a = ca[0].price
            max_a = ca[-1].price
            for b in range(a + 1, k):
                cb = clusters[b]
                # Early exit when sorted min of cb is already far from max of ca
                min_b = cb[0].price
                max_b = cb[-1].price
                merged_min = min_a if min_a < min_b else min_b
                merged_max = max_a if max_a > max_b else max_b
                diam = merged_max - merged_min

                merged_prices = [p.price for p in ca] + [p.price for p in cb]
                merged_atrs = [p.atr for p in ca] + [p.atr for p in cb]
                tol = _cluster_tolerance(merged_prices, merged_atrs, cluster_atr_multiplier, cluster_min_pct)

                if diam <= tol:
                    tie_key = (merged_min, min(p.index for p in ca + cb))
                    if diam < best_diam - 1e-12 or (abs(diam - best_diam) <= 1e-12 and tie_key < best_tie):
                        best_diam = diam
                        best_pair = (a, b)
                        best_tie = tie_key
                elif min_b - max_a > tol * 2.5:
                    # Ordered by price: further clusters will be even further away
                    break

        if best_pair is None:
            break

        i_a, i_b = best_pair
        merged = sorted(clusters[i_a] + clusters[i_b], key=lambda p: (p.price, p.index))
        clusters[i_a] = merged
        clusters.pop(i_b)
        # Keep clusters sorted deterministically by (min_price, first_index)
        clusters.sort(key=lambda c: (c[0].price, c[0].index))

    tf_tag = short_tf_label(timeframe)
    zones: List[SRZone] = []
    for cl in clusters:
        prices = [p.price for p in cl]
        atrs = [p.atr for p in cl]
        rep_price = float(statistics.median(prices))
        med_atr = float(statistics.median(atrs))
        d_zone = _cluster_tolerance(prices, atrs, cluster_atr_multiplier, cluster_min_pct)

        raw_low = min(prices)
        raw_high = max(prices)
        raw_width = raw_high - raw_low

        # Ensure every zone has a small volatility-scaled thickness while strictly
        # capping total width <= d_zone (never exceeds clustering diameter bound)
        target_min_width = min(d_zone, max(0.20 * d_zone, rep_price * 0.0010))
        if raw_width < target_min_width:
            pad = (target_min_width - raw_width) * 0.5
            z_low = max(0.01, raw_low - pad)
            z_high = raw_high + pad
        else:
            z_low = raw_low
            z_high = raw_high

        by_time = sorted(cl, key=lambda p: p.index)
        first_p = by_time[0]
        last_p = by_time[-1]

        zid = f"{tf_tag}_{level_type[0]}_{rep_price:.2f}_{first_p.index}"
        zones.append(
            SRZone(
                zone_id=zid,
                level_type=level_type,
                zone_low=round(z_low, 4),
                zone_high=round(z_high, 4),
                representative_price=round(rep_price, 4),
                first_occurrence=first_p.occurrence_ts,
                most_recent_occurrence=last_p.occurrence_ts,
                first_index=first_p.index,
                most_recent_index=last_p.index,
                timeframe=timeframe,
                atr_at_zone=round(med_atr, 4),
                pivots=list(by_time),
            )
        )

    zones.sort(key=lambda z: (z.representative_price, z.first_index))
    return zones


# ── Step 3: Historical Interaction Analysis & Zone Strength Scoring (4.3) ─


def analyze_zone_interactions_and_strength(
    zones: Sequence[SRZone],
    candles: Sequence[Dict[str, Any]],
    atr_series: Sequence[float],
    rel_vol_series: Sequence[float],
    params: Dict[str, Any],
) -> None:
    """
    Evaluate historical completed-candle interactions for each zone and compute
    its pre-confluence base strength (0..85, with remaining 15 reserved for MTF
    confluence) and breakdown. Mutates zones in-place.
    """
    n = len(candles)
    if n == 0 or not zones:
        return

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    breakout_buf = float(params.get("breakout_buffer_atr", 0.20))
    reversal_dist = float(params.get("reversal_distance_atr", 0.50))
    right_bars = int(params.get("right_bars", 5))
    reaction_window = max(right_bars, 5)
    min_episode_gap = max(2, int(params.get("min_pivot_separation", 3)) - 1)

    for zone in zones:
        z_low = zone.zone_low
        z_high = zone.zone_high
        is_support = zone.level_type == "Support"

        # Scan chronologically from the first confirmed pivot occurrence through last completed bar
        start_i = max(0, zone.first_index)

        episodes: List[Tuple[int, int, float]] = []  # (ep_start, ep_end, peak_rel_vol)
        in_episode = False
        ep_start = -1
        ep_end = -1
        ep_peak_rv = 1.0
        last_ep_end = -999
        left_zone_since_last = True

        uncontrolled_crossings = 0
        prev_side = 0  # +1 above zone, -1 below zone, 0 inside
        last_decisive_status = "active"

        for i in range(start_i, n):
            atr_i = atr_series[i]
            hi = highs[i]
            lo = lows[i]
            cl = closes[i]
            touch_pad = 0.15 * atr_i

            touches_bar = (lo <= z_high + touch_pad) and (hi >= z_low - touch_pad)

            # Track side & uncontrolled crossings through the zone
            if cl > z_high + breakout_buf * atr_i:
                cur_side = 1
            elif cl < z_low - breakout_buf * atr_i:
                cur_side = -1
            else:
                cur_side = 0

            if cur_side != 0 and prev_side != 0 and cur_side != prev_side:
                uncontrolled_crossings += 1
            if cur_side != 0:
                prev_side = cur_side

            # Track active vs broken status
            if is_support:
                if cl < z_low - breakout_buf * atr_i:
                    last_decisive_status = "broken"
                elif cl >= z_low:
                    last_decisive_status = "active"
            else:
                if cl > z_high + breakout_buf * atr_i:
                    last_decisive_status = "broken"
                elif cl <= z_high:
                    last_decisive_status = "active"

            # Track whether price meaningfully moved away between touch episodes
            if not touches_bar:
                if is_support and lo > z_high + 0.25 * atr_i:
                    left_zone_since_last = True
                elif (not is_support) and hi < z_low - 0.25 * atr_i:
                    left_zone_since_last = True
                elif abs(cl - zone.representative_price) > 0.35 * atr_i:
                    left_zone_since_last = True

            if touches_bar:
                if not in_episode:
                    if (i - last_ep_end) >= min_episode_gap and (left_zone_since_last or last_ep_end < 0):
                        in_episode = True
                        ep_start = i
                        ep_end = i
                        ep_peak_rv = rel_vol_series[i]
                        left_zone_since_last = False
                else:
                    ep_end = i
                    if rel_vol_series[i] > ep_peak_rv:
                        ep_peak_rv = rel_vol_series[i]
            else:
                if in_episode:
                    episodes.append((ep_start, ep_end, ep_peak_rv))
                    last_ep_end = ep_end
                    in_episode = False

        if in_episode:
            episodes.append((ep_start, ep_end, ep_peak_rv))

        # Ensure constituent confirmed pivots are counted if bar-scan merged none
        touches_count = max(len(episodes), 1 if zone.pivots else 0)

        # Evaluate successful reactions & reversal quality on completed subsequent bars
        reactions_count = 0
        mfe_list: List[float] = []
        rel_vols_used: List[float] = []

        for e_start, e_end, e_rv in episodes:
            rel_vols_used.append(e_rv)
            atr_e = atr_series[e_end]
            # Need at least 1 completed bar after episode end to verify reaction
            if e_end >= n - 1:
                continue

            window_end = min(n - 1, e_end + reaction_window)
            breached = False
            max_favorable = 0.0

            if is_support:
                # Episode itself must not have decisively broken down
                if closes[e_end] < z_low - breakout_buf * atr_e:
                    continue
                for k in range(e_end + 1, window_end + 1):
                    if closes[k] < z_low - breakout_buf * atr_series[k]:
                        breached = True
                        break
                    fav = highs[k] - z_high
                    if fav > max_favorable:
                        max_favorable = fav
                    fav_close = closes[k] - z_low
                    if fav_close > max_favorable:
                        max_favorable = fav_close
                if not breached and max_favorable >= reversal_dist * atr_e:
                    reactions_count += 1
                    mfe_list.append(max_favorable / atr_e)
            else:
                if closes[e_end] > z_high + breakout_buf * atr_e:
                    continue
                for k in range(e_end + 1, window_end + 1):
                    if closes[k] > z_high + breakout_buf * atr_series[k]:
                        breached = True
                        break
                    fav = z_low - lows[k]
                    if fav > max_favorable:
                        max_favorable = fav
                    fav_close = z_high - closes[k]
                    if fav_close > max_favorable:
                        max_favorable = fav_close
                if not breached and max_favorable >= reversal_dist * atr_e:
                    reactions_count += 1
                    mfe_list.append(max_favorable / atr_e)

        first_idx = episodes[0][0] if episodes else zone.first_index
        last_idx = episodes[-1][1] if episodes else zone.most_recent_index
        zone.first_index = first_idx
        zone.most_recent_index = last_idx
        zone.first_occurrence = _extract_ts(candles[first_idx])
        zone.most_recent_occurrence = _extract_ts(candles[last_idx])
        zone.touches = touches_count
        zone.reactions = reactions_count
        zone.uncontrolled_crossings = uncontrolled_crossings
        zone.status = last_decisive_status
        zone.avg_rel_volume = round(sum(rel_vols_used) / len(rel_vols_used), 3) if rel_vols_used else 1.0
        zone.avg_mfe_atr = round(sum(mfe_list) / len(mfe_list), 3) if mfe_list else 0.0

        # ── Explicit saturating 0-100 strength components (weights sum = 100) ──
        # 1. Independent touches (weight 20)
        s_touches = SR_STRENGTH_WEIGHTS["touches"] * (1.0 - math.exp(-0.55 * max(0, touches_count)))

        # 2. Successful reactions (weight 20)
        evaluable_touches = max(1, len([e for e in episodes if e[1] < n - 1]) or touches_count)
        reaction_rate = min(1.0, reactions_count / float(evaluable_touches))
        reaction_count_sat = 1.0 - math.exp(-0.75 * max(0, reactions_count))
        s_reactions = SR_STRENGTH_WEIGHTS["reactions"] * (
            0.55 * reaction_count_sat + 0.45 * reaction_rate * min(1.0, reactions_count / 2.0)
        )

        # 3. Volume quality (weight 15)
        vol_norm = min(1.0, max(0.0, (zone.avg_rel_volume - 0.50) / 1.50))
        s_volume = SR_STRENGTH_WEIGHTS["volume"] * vol_norm

        # 4. Recency (weight 10)
        bars_since_last = max(0, (n - 1) - last_idx)
        half_life = max(25.0, n * 0.25)
        s_recency = SR_STRENGTH_WEIGHTS["recency"] * math.exp(-math.log(2.0) * bars_since_last / half_life)

        # 5. Zone age / durability (weight 10)
        span_bars = max(0, last_idx - first_idx)
        if touches_count >= 2:
            s_age = SR_STRENGTH_WEIGHTS["age"] * (1.0 - math.exp(-span_bars / max(15.0, n * 0.15)))
        else:
            s_age = SR_STRENGTH_WEIGHTS["age"] * 0.20 * min(1.0, ((n - 1) - first_idx) / 20.0)

        # 6. Reversal quality (weight 10)
        s_reversal = SR_STRENGTH_WEIGHTS["reversal"] * (1.0 - math.exp(-0.65 * max(0.0, zone.avg_mfe_atr)))

        # Penalty for repeated uncontrolled crossings / whipsaw through zone
        excess_crossings = max(0, uncontrolled_crossings - 1)
        crossing_penalty = min(35.0, excess_crossings * 9.0)

        base_raw = s_touches + s_reactions + s_volume + s_recency + s_age + s_reversal - crossing_penalty
        zone.base_strength = round(max(0.0, min(85.0, base_raw)), 2)
        zone.confluence_bonus = 0.0
        zone.strength = round(max(0.0, min(100.0, zone.base_strength)), 1)
        zone.strength_breakdown = {
            "touches": round(s_touches, 2),
            "reactions": round(s_reactions, 2),
            "volume": round(s_volume, 2),
            "recency": round(s_recency, 2),
            "age": round(s_age, 2),
            "reversal": round(s_reversal, 2),
            "confluence": 0.0,
            "crossing_penalty": round(crossing_penalty, 2),
        }


# ── Step 4: Multi-Timeframe Confluence & Major/Minor Classification ───────


def _zones_overlap_mtf(z1: SRZone, z2: SRZone) -> bool:
    if z1.level_type != z2.level_type:
        return False
    min_atr = min(z1.atr_at_zone, z2.atr_at_zone)
    min_px = min(z1.representative_price, z2.representative_price)
    tol = max(0.25 * min_atr, 0.0025 * min_px)
    return (z1.zone_low - tol <= z2.zone_high) and (z2.zone_low - tol <= z1.zone_high)


def compute_mtf_confluence_and_classify(
    zones_by_tf: Dict[str, List[SRZone]],
    params: Dict[str, Any],
) -> None:
    """
    Detect overlapping support/resistance zones across distinct timeframes,
    assign deterministic `confluence_id`, add non-double-counted higher-TF
    confluence score (0..15) into each zone's 0..100 strength, and classify
    every zone as Major / Minor / Weak.
    """
    major_thresh = float(params.get("major_strength", 70))
    minor_thresh = float(params.get("minor_strength", 40))

    # Flatten all zones with deterministic ordering
    all_zones: List[SRZone] = []
    for tf in sorted(zones_by_tf.keys(), key=lambda t: (-TF_HIERARCHY_RANK.get(t, 0), t)):
        all_zones.extend(zones_by_tf[tf])

    # Reset confluence fields cleanly
    for z in all_zones:
        z.confluence_id = ""
        z.confluence_timeframes = [short_tf_label(z.timeframe)]
        z.confluence_count = 1
        z.confluence_highest_strength = z.base_strength
        z.confluence_strength = z.base_strength
        z.confluence_bonus = 0.0

    # Group by level_type separately (Support never conflates with Resistance)
    for ltype in ("Support", "Resistance"):
        typed_zones = [z for z in all_zones if z.level_type == ltype]
        if len(typed_zones) < 2:
            continue

        # Sort deterministically by hierarchy (higher TF first), then price
        typed_zones.sort(key=lambda z: (-TF_HIERARCHY_RANK.get(z.timeframe, 0), z.representative_price, z.zone_id))

        # Bounded clustering across timeframes (prevents chaining wide ladders)
        groups: List[List[SRZone]] = []
        for z in typed_zones:
            placed = False
            for grp in groups:
                # Must overlap with the group's anchor (highest-TF zone) AND stay bounded
                anchor = grp[0]
                if z.timeframe == anchor.timeframe:
                    continue
                if _zones_overlap_mtf(anchor, z):
                    # Verify total representative price span remains bounded
                    rep_prices = [g.representative_price for g in grp] + [z.representative_price]
                    max_atr = max(max(g.atr_at_zone for g in grp), z.atr_at_zone)
                    max_px = max(rep_prices)
                    if (max(rep_prices) - min(rep_prices)) <= max(2.0 * max_atr, 0.02 * max_px):
                        grp.append(z)
                        placed = True
                        break
            if not placed:
                groups.append([z])

        for grp in groups:
            distinct_tfs = sorted(
                {g.timeframe for g in grp},
                key=lambda t: (-TF_HIERARCHY_RANK.get(t, 0), t),
            )
            if len(distinct_tfs) < 2:
                continue

            # Weighted representative price (higher TF carries higher weight)
            w_sum = sum(float(TF_HIERARCHY_RANK.get(g.timeframe, 1)) for g in grp)
            w_price = sum(g.representative_price * float(TF_HIERARCHY_RANK.get(g.timeframe, 1)) for g in grp) / w_sum
            conf_id = f"CONF_{ltype[0]}_{w_price:.2f}"
            short_tfs = [short_tf_label(t) for t in distinct_tfs]

            # Best single-TF base strength per distinct timeframe (prevents double-counting
            # if a timeframe happened to contribute two nearby zones)
            best_base_by_tf: Dict[str, float] = {}
            for g in grp:
                cur_best = best_base_by_tf.get(g.timeframe, -1.0)
                if g.base_strength > cur_best:
                    best_base_by_tf[g.timeframe] = g.base_strength

            highest_single_base = max(best_base_by_tf.values())

            # Non-double-counted combined confluence strength (0..100):
            # Hierarchy-weighted average of distinct TF base strengths + alignment bonus
            tf_w_total = sum(float(TF_HIERARCHY_RANK.get(t, 1)) for t in distinct_tfs)
            weighted_base = (
                sum(best_base_by_tf[t] * float(TF_HIERARCHY_RANK.get(t, 1)) for t in distinct_tfs)
                / max(1.0, tf_w_total)
            )
            alignment_bonus = min(SR_STRENGTH_WEIGHTS["confluence"], 4.5 * (len(distinct_tfs) - 1))
            combined_conf_strength = round(min(100.0, weighted_base + alignment_bonus), 1)

            for g in grp:
                # Each distinct OTHER agreeing timeframe contributes once (no double counting)
                other_tfs = [t for t in distinct_tfs if t != g.timeframe]
                bonus = min(
                    SR_STRENGTH_WEIGHTS["confluence"],
                    sum(TF_CONFLUENCE_WEIGHT.get(t, 2.0) for t in other_tfs),
                )
                g.confluence_id = conf_id
                g.confluence_timeframes = list(short_tfs)
                g.confluence_count = len(distinct_tfs)
                g.confluence_bonus = round(bonus, 2)
                g.confluence_highest_strength = round(highest_single_base, 1)
                g.confluence_strength = combined_conf_strength

    # Finalize normalized 0..100 strength and Major/Minor/Weak classification
    for z in all_zones:
        final_s = max(0.0, min(100.0, z.base_strength + z.confluence_bonus))
        z.strength = round(final_s, 1)
        z.strength_breakdown["confluence"] = round(z.confluence_bonus, 2)
        if z.strength >= major_thresh:
            z.classification = "Major"
        elif z.strength >= minor_thresh:
            z.classification = "Minor"
        else:
            z.classification = "Weak"


# ── Step 5: Price Interaction / Event Classification (4.5) ────────────────


def classify_zone_event(
    zone: SRZone,
    candles: Sequence[Dict[str, Any]],
    atr_series: Sequence[float],
    rel_vol_series: Sequence[float],
    params: Dict[str, Any],
) -> Optional[SREvent]:
    """
    Classify the current actionable price interaction event on a zone using
    strictly completed candles up to `candles[-1]`.

    Event precedence (most informative confirmed event first):
      1. FALSE_BREAKOUT
      2. BREAKOUT_RETEST / BREAKDOWN_RETEST
      3. RESISTANCE_BREAKOUT / SUPPORT_BREAKDOWN
      4. SUPPORT_BOUNCE / RESISTANCE_REJECTION
      5. NEAR_SUPPORT / NEAR_RESISTANCE
    """
    n = len(candles)
    if n < 3:
        return None

    last_idx = n - 1
    cur = candles[last_idx]
    cur_close = float(cur["close"])
    cur_atr = max(float(atr_series[last_idx]), 1e-6)
    cur_rv = float(rel_vol_series[last_idx])
    cur_ts = _extract_ts(cur)

    near_dist_atr = float(params.get("near_distance_atr", 0.50))
    breakout_buf = float(params.get("breakout_buffer_atr", 0.20))
    reversal_dist = float(params.get("reversal_distance_atr", 0.50))
    retest_tol = float(params.get("retest_tolerance_atr", 0.25))
    vol_thresh = float(params.get("relative_volume_threshold", 1.50))
    right_bars = int(params.get("right_bars", 5))

    z_low = zone.zone_low
    z_high = zone.zone_high
    rep = zone.representative_price
    is_support = zone.level_type == "Support"

    # Volatility-adjusted edge distance
    if cur_close > z_high:
        edge_dist = cur_close - z_high
    elif cur_close < z_low:
        edge_dist = z_low - cur_close
    else:
        edge_dist = 0.0
    distance_atr = round(edge_dist / cur_atr, 3)
    distance_pct = round(((cur_close - rep) / rep) * 100.0, 2) if rep > 0 else 0.0

    # Ensure zone was actually formed & confirmed prior to the event window
    earliest_confirmed_idx = min((p.confirm_index for p in zone.pivots), default=zone.first_index)
    if earliest_confirmed_idx >= last_idx:
        # Zone only just became confirmed on the very last bar — can still be NEAR_*
        if distance_atr <= near_dist_atr:
            ev_name = "NEAR_SUPPORT" if is_support else "NEAR_RESISTANCE"
            inv_px = (z_low - breakout_buf * cur_atr) if is_support else (z_high + breakout_buf * cur_atr)
            q = _compute_event_quality(zone.strength, cur_rv, vol_thresh, 0.70)
            return SREvent(
                event=ev_name,
                event_ts=cur_ts,
                confirmation_ts=cur_ts,
                event_quality=q,
                invalidation_price=round(inv_px, 2),
                relative_volume=round(cur_rv, 2),
                volume_confirmed=cur_rv >= vol_thresh,
                distance_pct=distance_pct,
                distance_atr=distance_atr,
            )
        return None

    def _c_close(idx: int) -> float:
        return float(candles[idx]["close"])

    def _c_high(idx: int) -> float:
        return float(candles[idx]["high"])

    def _c_low(idx: int) -> float:
        return float(candles[idx]["low"])

    # Lookback windows bounded by zone confirmation
    fb_start = max(earliest_confirmed_idx, last_idx - max(6, right_bars + 2))
    retest_start = max(earliest_confirmed_idx, last_idx - max(15, right_bars * 3))
    bo_start = max(earliest_confirmed_idx, last_idx - 2)
    bounce_start = max(earliest_confirmed_idx, last_idx - max(5, right_bars + 1))

    # ── 1. FALSE_BREAKOUT ─────────────────────────────────────────────────
    if not is_support:
        # Prior confirmed resistance breakout followed by completed close back <= z_high
        if cur_close <= z_high + 0.05 * cur_atr:
            for b in range(last_idx - 1, fb_start - 1, -1):
                if _c_close(b) > z_high + breakout_buf * atr_series[b]:
                    # Find first subsequent completed bar closing back <= z_high
                    for c in range(b + 1, n):
                        if _c_close(c) <= z_high:
                            peak_hi = max(_c_high(k) for k in range(b, c + 1))
                            inv_px = peak_hi + 0.10 * cur_atr
                            rv_ev = max(rel_vol_series[b], rel_vol_series[c])
                            q = _compute_event_quality(zone.strength, rv_ev, vol_thresh, 0.90)
                            return SREvent(
                                event="FALSE_BREAKOUT",
                                event_ts=_extract_ts(candles[b]),
                                confirmation_ts=_extract_ts(candles[c]),
                                event_quality=q,
                                invalidation_price=round(inv_px, 2),
                                relative_volume=round(rv_ev, 2),
                                volume_confirmed=rv_ev >= vol_thresh,
                                distance_pct=distance_pct,
                                distance_atr=distance_atr,
                            )
                    break
    else:
        # Prior confirmed support breakdown followed by completed close back >= z_low
        if cur_close >= z_low - 0.05 * cur_atr:
            for b in range(last_idx - 1, fb_start - 1, -1):
                if _c_close(b) < z_low - breakout_buf * atr_series[b]:
                    for c in range(b + 1, n):
                        if _c_close(c) >= z_low:
                            trough_lo = min(_c_low(k) for k in range(b, c + 1))
                            inv_px = max(0.01, trough_lo - 0.10 * cur_atr)
                            rv_ev = max(rel_vol_series[b], rel_vol_series[c])
                            q = _compute_event_quality(zone.strength, rv_ev, vol_thresh, 0.90)
                            return SREvent(
                                event="FALSE_BREAKOUT",
                                event_ts=_extract_ts(candles[b]),
                                confirmation_ts=_extract_ts(candles[c]),
                                event_quality=q,
                                invalidation_price=round(inv_px, 2),
                                relative_volume=round(rv_ev, 2),
                                volume_confirmed=rv_ev >= vol_thresh,
                                distance_pct=distance_pct,
                                distance_atr=distance_atr,
                            )
                    break

    # ── 2. BREAKOUT_RETEST / BREAKDOWN_RETEST ─────────────────────────────
    if not is_support and cur_close >= z_high:
        # Find confirmed breakout bar `b` where b-1 was <= threshold and b > threshold
        bo_bar: Optional[int] = None
        for b in range(last_idx - 2, retest_start - 1, -1):
            if _c_close(b) > z_high + breakout_buf * atr_series[b]:
                if b == 0 or _c_close(b - 1) <= z_high + breakout_buf * atr_series[b - 1]:
                    bo_bar = b
                    break
        if bo_bar is not None:
            # Verify no invalidating breakdown below zone_low after breakout
            valid_after = all(_c_close(k) >= z_low - breakout_buf * atr_series[k] for k in range(bo_bar + 1, n))
            if valid_after:
                # Look for a later pullback touching/within retest_tol of broken resistance, then holding
                for r in range(bo_bar + 1, n):
                    if _c_low(r) <= z_high + retest_tol * atr_series[r] and _c_close(r) >= z_low - 0.15 * atr_series[r]:
                        # Confirm it holds as support on bar r (if close > z_high) or subsequent bar h
                        hold_bar: Optional[int] = None
                        for h in range(r, n):
                            if _c_close(h) > z_high and (_c_close(h) - _c_low(r)) >= 0.25 * atr_series[r]:
                                hold_bar = h
                                break
                        if hold_bar is not None and (last_idx - hold_bar) <= 3:
                            rv_ev = max(rel_vol_series[bo_bar], rel_vol_series[hold_bar])
                            inv_px = z_low - breakout_buf * cur_atr
                            q = _compute_event_quality(zone.strength, rv_ev, vol_thresh, 0.95)
                            return SREvent(
                                event="BREAKOUT_RETEST",
                                event_ts=_extract_ts(candles[r]),
                                confirmation_ts=_extract_ts(candles[hold_bar]),
                                event_quality=q,
                                invalidation_price=round(max(0.01, inv_px), 2),
                                relative_volume=round(rv_ev, 2),
                                volume_confirmed=rv_ev >= vol_thresh,
                                distance_pct=distance_pct,
                                distance_atr=distance_atr,
                            )

    if is_support and cur_close <= z_low:
        bd_bar: Optional[int] = None
        for b in range(last_idx - 2, retest_start - 1, -1):
            if _c_close(b) < z_low - breakout_buf * atr_series[b]:
                if b == 0 or _c_close(b - 1) >= z_low - breakout_buf * atr_series[b - 1]:
                    bd_bar = b
                    break
        if bd_bar is not None:
            valid_after = all(_c_close(k) <= z_high + breakout_buf * atr_series[k] for k in range(bd_bar + 1, n))
            if valid_after:
                for r in range(bd_bar + 1, n):
                    if _c_high(r) >= z_low - retest_tol * atr_series[r] and _c_close(r) <= z_high + 0.15 * atr_series[r]:
                        hold_bar = None
                        for h in range(r, n):
                            if _c_close(h) < z_low and (_c_high(r) - _c_close(h)) >= 0.25 * atr_series[r]:
                                hold_bar = h
                                break
                        if hold_bar is not None and (last_idx - hold_bar) <= 3:
                            rv_ev = max(rel_vol_series[bd_bar], rel_vol_series[hold_bar])
                            inv_px = z_high + breakout_buf * cur_atr
                            q = _compute_event_quality(zone.strength, rv_ev, vol_thresh, 0.95)
                            return SREvent(
                                event="BREAKDOWN_RETEST",
                                event_ts=_extract_ts(candles[r]),
                                confirmation_ts=_extract_ts(candles[hold_bar]),
                                event_quality=q,
                                invalidation_price=round(inv_px, 2),
                                relative_volume=round(rv_ev, 2),
                                volume_confirmed=rv_ev >= vol_thresh,
                                distance_pct=distance_pct,
                                distance_atr=distance_atr,
                            )

    # ── 3. RESISTANCE_BREAKOUT / SUPPORT_BREAKDOWN ────────────────────────
    if not is_support and cur_close > z_high + breakout_buf * cur_atr:
        for b in range(last_idx, bo_start - 1, -1):
            if _c_close(b) > z_high + breakout_buf * atr_series[b]:
                if b > 0 and _c_close(b - 1) <= z_high + breakout_buf * atr_series[b - 1]:
                    rv_b = rel_vol_series[b]
                    inv_px = max(0.01, z_low - 0.10 * cur_atr)
                    q = _compute_event_quality(zone.strength, rv_b, vol_thresh, 0.92 if rv_b >= vol_thresh else 0.72)
                    return SREvent(
                        event="RESISTANCE_BREAKOUT",
                        event_ts=_extract_ts(candles[b]),
                        confirmation_ts=_extract_ts(candles[b]),
                        event_quality=q,
                        invalidation_price=round(inv_px, 2),
                        relative_volume=round(rv_b, 2),
                        volume_confirmed=rv_b >= vol_thresh,
                        distance_pct=distance_pct,
                        distance_atr=distance_atr,
                    )

    if is_support and cur_close < z_low - breakout_buf * cur_atr:
        for b in range(last_idx, bo_start - 1, -1):
            if _c_close(b) < z_low - breakout_buf * atr_series[b]:
                if b > 0 and _c_close(b - 1) >= z_low - breakout_buf * atr_series[b - 1]:
                    rv_b = rel_vol_series[b]
                    inv_px = z_high + 0.10 * cur_atr
                    q = _compute_event_quality(zone.strength, rv_b, vol_thresh, 0.92 if rv_b >= vol_thresh else 0.72)
                    return SREvent(
                        event="SUPPORT_BREAKDOWN",
                        event_ts=_extract_ts(candles[b]),
                        confirmation_ts=_extract_ts(candles[b]),
                        event_quality=q,
                        invalidation_price=round(inv_px, 2),
                        relative_volume=round(rv_b, 2),
                        volume_confirmed=rv_b >= vol_thresh,
                        distance_pct=distance_pct,
                        distance_atr=distance_atr,
                    )

    # ── 4. SUPPORT_BOUNCE / RESISTANCE_REJECTION ──────────────────────────
    # Strictly requires an earlier touch bar `t < last_idx` AND subsequent
    # completed-candle price recovery >= reversal_distance_atr * ATR.
    if is_support and zone.status == "active":
        for t in range(last_idx - 1, bounce_start - 1, -1):
            atr_t = atr_series[t]
            touched = _c_low(t) <= z_high + 0.25 * atr_t and _c_high(t) >= z_low - 0.25 * atr_t
            not_broken = _c_close(t) >= z_low - breakout_buf * atr_t
            if touched and not_broken:
                # Ensure no breakdown occurred between t and last_idx
                if any(_c_close(k) < z_low - breakout_buf * atr_series[k] for k in range(t + 1, n)):
                    continue
                recovery = cur_close - _c_low(t)
                if recovery >= reversal_dist * atr_t and cur_close > _c_close(t) and cur_close >= z_low:
                    rv_ev = max(rel_vol_series[t], cur_rv)
                    inv_px = max(0.01, min(z_low, _c_low(t)) - 0.15 * cur_atr)
                    q = _compute_event_quality(zone.strength, rv_ev, vol_thresh, 0.88)
                    return SREvent(
                        event="SUPPORT_BOUNCE",
                        event_ts=_extract_ts(candles[t]),
                        confirmation_ts=cur_ts,
                        event_quality=q,
                        invalidation_price=round(inv_px, 2),
                        relative_volume=round(rv_ev, 2),
                        volume_confirmed=rv_ev >= vol_thresh,
                        distance_pct=distance_pct,
                        distance_atr=distance_atr,
                    )

    if (not is_support) and zone.status == "active":
        for t in range(last_idx - 1, bounce_start - 1, -1):
            atr_t = atr_series[t]
            touched = _c_high(t) >= z_low - 0.25 * atr_t and _c_low(t) <= z_high + 0.25 * atr_t
            not_broken = _c_close(t) <= z_high + breakout_buf * atr_t
            if touched and not_broken:
                if any(_c_close(k) > z_high + breakout_buf * atr_series[k] for k in range(t + 1, n)):
                    continue
                rejection = _c_high(t) - cur_close
                if rejection >= reversal_dist * atr_t and cur_close < _c_close(t) and cur_close <= z_high:
                    rv_ev = max(rel_vol_series[t], cur_rv)
                    inv_px = max(z_high, _c_high(t)) + 0.15 * cur_atr
                    q = _compute_event_quality(zone.strength, rv_ev, vol_thresh, 0.88)
                    return SREvent(
                        event="RESISTANCE_REJECTION",
                        event_ts=_extract_ts(candles[t]),
                        confirmation_ts=cur_ts,
                        event_quality=q,
                        invalidation_price=round(inv_px, 2),
                        relative_volume=round(rv_ev, 2),
                        volume_confirmed=rv_ev >= vol_thresh,
                        distance_pct=distance_pct,
                        distance_atr=distance_atr,
                    )

    # ── 5. NEAR_SUPPORT / NEAR_RESISTANCE ─────────────────────────────────
    if zone.status == "active" and distance_atr <= near_dist_atr:
        if is_support and cur_close >= z_low - breakout_buf * cur_atr:
            inv_px = max(0.01, z_low - breakout_buf * cur_atr)
            q = _compute_event_quality(zone.strength, cur_rv, vol_thresh, 0.75)
            return SREvent(
                event="NEAR_SUPPORT",
                event_ts=cur_ts,
                confirmation_ts=cur_ts,
                event_quality=q,
                invalidation_price=round(inv_px, 2),
                relative_volume=round(cur_rv, 2),
                volume_confirmed=cur_rv >= vol_thresh,
                distance_pct=distance_pct,
                distance_atr=distance_atr,
            )
        if (not is_support) and cur_close <= z_high + breakout_buf * cur_atr:
            inv_px = z_high + breakout_buf * cur_atr
            q = _compute_event_quality(zone.strength, cur_rv, vol_thresh, 0.75)
            return SREvent(
                event="NEAR_RESISTANCE",
                event_ts=cur_ts,
                confirmation_ts=cur_ts,
                event_quality=q,
                invalidation_price=round(inv_px, 2),
                relative_volume=round(cur_rv, 2),
                volume_confirmed=cur_rv >= vol_thresh,
                distance_pct=distance_pct,
                distance_atr=distance_atr,
            )

    return None


def _compute_event_quality(
    zone_strength: float,
    rel_vol: float,
    vol_thresh: float,
    clarity_factor: float,
) -> float:
    vol_score = min(1.0, rel_vol / max(0.5, vol_thresh)) * 100.0
    raw = 0.55 * zone_strength + 0.25 * vol_score + 0.20 * (clarity_factor * 100.0)
    return round(max(0.0, min(100.0, raw)), 1)


# ── Single-TF & Multi-TF Orchestrators ────────────────────────────────────


def analyze_single_timeframe(
    candles: Sequence[Dict[str, Any]],
    timeframe: str,
    params: Optional[Dict[str, Any]] = None,
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run pivot detection, clustering, and historical interaction analysis for a
    single timeframe. Returns zones plus precomputed ATR & relative volume.
    """
    eff_params = {**DEFAULT_SR_PARAMS, **(params or {})}
    sliced = slice_candles_as_of(candles, as_of_date)

    # Cap working window for very long intraday series to maintain high throughput
    # while preserving ample structural history (latest 600 bars per timeframe)
    if len(sliced) > 650:
        sliced = sliced[-650:]

    atr_period = int(eff_params["atr_period"])
    left_bars = int(eff_params["left_bars"])
    right_bars = int(eff_params["right_bars"])
    min_sep = int(eff_params["min_pivot_separation"])

    min_required = max(atr_period + 2, left_bars + right_bars + 5)
    if len(sliced) < min_required:
        return {
            "ok": False,
            "reason": f"Insufficient bars ({len(sliced)} < {min_required})",
            "candles": sliced,
            "atr": [],
            "rel_vol": [],
            "pivots_high": [],
            "pivots_low": [],
            "zones": [],
        }

    atr_series = compute_atr_series(sliced, period=atr_period)
    rel_vol_series = compute_relative_volume_series(sliced, timeframe=timeframe)

    pivots_high, pivots_low = detect_confirmed_pivots(
        sliced,
        atr_series,
        timeframe=timeframe,
        left_bars=left_bars,
        right_bars=right_bars,
        min_pivot_separation=min_sep,
    )

    res_zones = cluster_pivots_into_zones(
        pivots_high,
        level_type="Resistance",
        timeframe=timeframe,
        cluster_atr_multiplier=float(eff_params["cluster_atr_multiplier"]),
        cluster_min_pct=float(eff_params["cluster_min_pct"]),
    )
    sup_zones = cluster_pivots_into_zones(
        pivots_low,
        level_type="Support",
        timeframe=timeframe,
        cluster_atr_multiplier=float(eff_params["cluster_atr_multiplier"]),
        cluster_min_pct=float(eff_params["cluster_min_pct"]),
    )

    zones = sup_zones + res_zones
    analyze_zone_interactions_and_strength(zones, sliced, atr_series, rel_vol_series, eff_params)

    return {
        "ok": True,
        "reason": "",
        "candles": sliced,
        "atr": atr_series,
        "rel_vol": rel_vol_series,
        "pivots_high": pivots_high,
        "pivots_low": pivots_low,
        "zones": zones,
    }


def evaluate_symbol_mtf(
    *,
    symbol: str,
    exchange: str,
    candles_by_tf: Dict[str, Sequence[Dict[str, Any]]],
    params: Optional[Dict[str, Any]] = None,
    as_of_date: Optional[str] = None,
    fno_eligible: bool = False,
    api_calls: int = 0,
    include_weak: bool = False,
) -> Dict[str, Any]:
    """
    Evaluate all provided timeframes for a single symbol, compute MTF confluence,
    classify zones (Major / Minor / Weak) and actionable events, and emit
    table-ready rows (one row per qualifying zone/event) plus missing-data records.
    """
    eff_params = {**DEFAULT_SR_PARAMS, **(params or {})}
    min_touches = int(eff_params.get("minimum_touches", 2))

    tf_analyses: Dict[str, Dict[str, Any]] = {}
    zones_by_tf: Dict[str, List[SRZone]] = {}
    missing_data: List[Dict[str, str]] = []

    for tf, raw_candles in candles_by_tf.items():
        norm_tf = normalize_timeframe(tf)
        if not raw_candles:
            missing_data.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": short_tf_label(norm_tf),
                    "canonical_timeframe": norm_tf,
                    "reason": "No historical candles available",
                }
            )
            continue

        analysis = analyze_single_timeframe(
            raw_candles,
            timeframe=norm_tf,
            params=eff_params,
            as_of_date=as_of_date,
        )
        if not analysis["ok"]:
            missing_data.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": short_tf_label(norm_tf),
                    "canonical_timeframe": norm_tf,
                    "reason": analysis["reason"],
                }
            )
            continue

        tf_analyses[norm_tf] = analysis
        zones_by_tf[norm_tf] = analysis["zones"]

    # Step 4: Cross-timeframe confluence + Major/Minor/Weak classification
    compute_mtf_confluence_and_classify(zones_by_tf, eff_params)

    # Step 5: Classify events and build structured output rows
    rows: List[Dict[str, Any]] = []
    evaluated_tfs = sorted(tf_analyses.keys(), key=lambda t: (-TF_HIERARCHY_RANK.get(t, 0), t))

    for tf in evaluated_tfs:
        analysis = tf_analyses[tf]
        candles = analysis["candles"]
        atr_series = analysis["atr"]
        rel_vol_series = analysis["rel_vol"]
        last_bar = candles[-1]
        prev_bar = candles[-2] if len(candles) >= 2 else last_bar
        cur_close = float(last_bar["close"])
        prev_close = float(prev_bar["close"])
        chg_pct = ((cur_close - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
        last_date = _extract_trade_date(last_bar)
        tf_short = short_tf_label(tf)

        for zone in zones_by_tf[tf]:
            if zone.touches < min_touches:
                continue
            if (not include_weak) and zone.classification == "Weak":
                continue

            ev = classify_zone_event(zone, candles, atr_series, rel_vol_series, eff_params)
            if ev is None:
                continue

            conf_label = (
                f"{'+'.join(zone.confluence_timeframes)} ({zone.confluence_count})"
                if zone.confluence_count >= 2
                else tf_short
            )

            row_id = f"{exchange}:{symbol}:{tf_short}:{zone.zone_id}:{ev.event}"
            rows.append(
                {
                    "row_id": row_id,
                    "symbol": symbol,
                    "exchange": exchange,
                    "fno_eligible": bool(fno_eligible),
                    "timeframe": tf_short,
                    "canonical_timeframe": tf,
                    "event": ev.event,
                    "level_type": zone.level_type,
                    "classification": zone.classification,
                    "current_price": round(cur_close, 2),
                    "close": round(cur_close, 2),
                    "change_pct": round(chg_pct, 2),
                    "zone_id": zone.zone_id,
                    "zone_low": round(zone.zone_low, 2),
                    "zone_high": round(zone.zone_high, 2),
                    "representative_price": round(zone.representative_price, 2),
                    "distance_pct": ev.distance_pct,
                    "distance_atr": ev.distance_atr,
                    "strength": zone.strength,
                    "score": int(round(zone.strength)),
                    "touches": zone.touches,
                    "reactions": zone.reactions,
                    "relative_volume": ev.relative_volume,
                    "volume_confirmed": ev.volume_confirmed,
                    "confluence": conf_label,
                    "confluence_id": zone.confluence_id,
                    "confluence_count": zone.confluence_count,
                    "confluence_timeframes": list(zone.confluence_timeframes),
                    "confluence_strength": zone.confluence_strength,
                    "status": zone.status,
                    "event_ts": ev.event_ts,
                    "confirmation_ts": ev.confirmation_ts,
                    "event_quality": ev.event_quality,
                    "invalidation_price": ev.invalidation_price,
                    "first_occurrence": zone.first_occurrence,
                    "most_recent_occurrence": zone.most_recent_occurrence,
                    "strength_breakdown": dict(zone.strength_breakdown),
                    "bars": len(candles),
                    "last_date": last_date,
                    "signal": ev.event,
                    "tags": [zone.classification.upper(), tf_short.upper(), zone.level_type.upper()],
                    "matched": True,
                    "api_calls": api_calls,
                }
            )

    rows.sort(
        key=lambda r: (
            1 if r["classification"] == "Major" else 0,
            float(r["strength"]),
            -abs(float(r["distance_pct"])),
        ),
        reverse=True,
    )

    return {
        "symbol": symbol,
        "exchange": exchange,
        "fno_eligible": bool(fno_eligible),
        "evaluated_timeframes": [short_tf_label(t) for t in evaluated_tfs],
        "rows": rows,
        "zones_by_tf": zones_by_tf,
        "missing_data": missing_data,
    }


__all__ = [
    "ALL_SR_EVENTS",
    "ConfirmedPivot",
    "DEFAULT_SR_PARAMS",
    "INTRADAY_TIMEFRAMES",
    "SRZone",
    "SREvent",
    "SR_DEFAULT_TIMEFRAMES",
    "SR_STRENGTH_WEIGHTS",
    "TF_CONFLUENCE_WEIGHT",
    "TF_HIERARCHY_RANK",
    "TF_SHORT_LABELS",
    "analyze_single_timeframe",
    "analyze_zone_interactions_and_strength",
    "classify_zone_event",
    "cluster_pivots_into_zones",
    "compute_atr_series",
    "compute_mtf_confluence_and_classify",
    "compute_relative_volume_series",
    "detect_confirmed_pivots",
    "evaluate_symbol_mtf",
    "filter_complete_weekly_candles",
    "short_tf_label",
    "slice_candles_as_of",
]
