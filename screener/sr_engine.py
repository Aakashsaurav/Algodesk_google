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

SR_EVENT_CODES: Dict[str, int] = {
    "NONE": 0,
    "NEAR_SUPPORT": 1,
    "NEAR_RESISTANCE": 2,
    "SUPPORT_BOUNCE": 3,
    "RESISTANCE_REJECTION": 4,
    "RESISTANCE_BREAKOUT": 5,
    "SUPPORT_BREAKDOWN": 6,
    "BREAKOUT_RETEST": 7,
    "BREAKDOWN_RETEST": 8,
    "FALSE_BREAKOUT": 9,
}

SR_RANK_TIER_LABELS: Dict[int, str] = {
    1: "Tier 1 · Major Breakout / Retest",
    2: "Tier 2 · Major Bounce / Rejection",
    3: "Tier 3 · Near Major Level",
    4: "Tier 4 · Minor Breakout / Retest",
    5: "Tier 5 · Minor Bounce / Rejection",
    6: "Tier 6 · Near Minor Level",
}

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
    "htf_confluence": False,
    # Configurable 7-component strength weights (normalized to sum == 100)
    "weight_touches": 20.0,
    "weight_reactions": 20.0,
    "weight_volume": 15.0,
    "weight_recency": 10.0,
    "weight_age": 10.0,
    "weight_confluence": 15.0,
    "weight_reversal": 10.0,
}


def normalize_strength_weights(params: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """
    Resolve and normalize the 7 strength-scoring weights from `params` so they
    are always non-negative and sum cleanly to 100.0.
    """
    p = params or {}
    key_map = (
        ("touches", "weight_touches"),
        ("reactions", "weight_reactions"),
        ("volume", "weight_volume"),
        ("recency", "weight_recency"),
        ("age", "weight_age"),
        ("confluence", "weight_confluence"),
        ("reversal", "weight_reversal"),
    )
    raw: Dict[str, float] = {}
    for canonical, param_key in key_map:
        default_v = SR_STRENGTH_WEIGHTS[canonical]
        val = p.get(param_key, default_v)
        try:
            fv = float(val)
            if not math.isfinite(fv) or fv < 0.0:
                fv = default_v
        except (TypeError, ValueError):
            fv = default_v
        raw[canonical] = fv

    total = sum(raw.values())
    if total <= 1e-9:
        return dict(SR_STRENGTH_WEIGHTS)
    if abs(total - 100.0) <= 1e-6:
        return raw
    scale = 100.0 / total
    return {k: round(v * scale, 4) for k, v in raw.items()}


def compute_sr_rank_tier(classification: str, event: str) -> int:
    """
    6-tier priority ranking from the S&R specification:
      1: Major Breakout / Breakdown / Retest
      2: Major Bounce / Rejection
      3: Near Major Support / Resistance (or False Breakout)
      4: Minor Breakout / Breakdown / Retest
      5: Minor Bounce / Rejection
      6: Near Minor Support / Resistance / Weak
    """
    cls_u = str(classification or "").strip().lower()
    ev_u = str(event or "").strip().upper()
    is_major = cls_u == "major"
    if ev_u in {"RESISTANCE_BREAKOUT", "SUPPORT_BREAKDOWN", "BREAKOUT_RETEST", "BREAKDOWN_RETEST"}:
        return 1 if is_major else 4
    if ev_u in {"SUPPORT_BOUNCE", "RESISTANCE_REJECTION"}:
        return 2 if is_major else 5
    return 3 if is_major else 6


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

        # ── Explicit saturating 0-100 strength components (configurable weights sum = 100) ──
        weights = normalize_strength_weights(params)
        base_cap = max(0.0, 100.0 - weights["confluence"])

        # 1. Independent touches
        s_touches = weights["touches"] * (1.0 - math.exp(-0.55 * max(0, touches_count)))

        # 2. Successful reactions
        evaluable_touches = max(1, len([e for e in episodes if e[1] < n - 1]) or touches_count)
        reaction_rate = min(1.0, reactions_count / float(evaluable_touches))
        reaction_count_sat = 1.0 - math.exp(-0.75 * max(0, reactions_count))
        s_reactions = weights["reactions"] * (
            0.55 * reaction_count_sat + 0.45 * reaction_rate * min(1.0, reactions_count / 2.0)
        )

        # 3. Volume quality
        vol_norm = min(1.0, max(0.0, (zone.avg_rel_volume - 0.50) / 1.50))
        s_volume = weights["volume"] * vol_norm

        # 4. Recency
        bars_since_last = max(0, (n - 1) - last_idx)
        half_life = max(25.0, n * 0.25)
        s_recency = weights["recency"] * math.exp(-math.log(2.0) * bars_since_last / half_life)

        # 5. Zone age / durability
        span_bars = max(0, last_idx - first_idx)
        if touches_count >= 2:
            s_age = weights["age"] * (1.0 - math.exp(-span_bars / max(15.0, n * 0.15)))
        else:
            s_age = weights["age"] * 0.20 * min(1.0, ((n - 1) - first_idx) / 20.0)

        # 6. Reversal quality
        s_reversal = weights["reversal"] * (1.0 - math.exp(-0.65 * max(0.0, zone.avg_mfe_atr)))

        # Penalty for repeated uncontrolled crossings / whipsaw through zone
        excess_crossings = max(0, uncontrolled_crossings - 1)
        crossing_penalty = min(35.0, excess_crossings * 9.0)

        base_raw = s_touches + s_reactions + s_volume + s_recency + s_age + s_reversal - crossing_penalty
        zone.base_strength = round(max(0.0, min(base_cap, base_raw)), 2)
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
            weights = normalize_strength_weights(params)
            conf_cap = weights["confluence"]
            conf_scale = conf_cap / max(1e-6, SR_STRENGTH_WEIGHTS["confluence"])
            alignment_bonus = min(conf_cap, 4.5 * conf_scale * (len(distinct_tfs) - 1))
            combined_conf_strength = round(min(100.0, weighted_base + alignment_bonus), 1)

            for g in grp:
                # Each distinct OTHER agreeing timeframe contributes once (no double counting)
                other_tfs = [t for t in distinct_tfs if t != g.timeframe]
                bonus = min(
                    conf_cap,
                    sum(TF_CONFLUENCE_WEIGHT.get(t, 2.0) * conf_scale for t in other_tfs),
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
    symbol: str,
    exchange: str,
    candles_by_tf: Dict[str, Sequence[Dict[str, Any]]],
    params: Optional[Dict[str, Any]] = None,
    as_of_date: Optional[str] = None,
    fno_eligible: bool = False,
    api_calls: int = 0,
    include_weak: bool = False,
    instrument_category: str = "EQ",
    contract_expiry: str = "",
    data_source: str = "",
    adjustment_basis: str = "split_adjusted",
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
            rank_tier = compute_sr_rank_tier(zone.classification, ev.event)
            row_id = f"{exchange}:{symbol}:{tf_short}:{zone.zone_id}:{ev.event}"
            rows.append(
                {
                    "row_id": row_id,
                    "symbol": symbol,
                    "exchange": exchange,
                    "instrument_category": str(instrument_category or "EQ").upper(),
                    "contract_expiry": str(contract_expiry or ""),
                    "fno_eligible": bool(fno_eligible),
                    "timeframe": tf_short,
                    "canonical_timeframe": tf,
                    "event": ev.event,
                    "level_type": zone.level_type,
                    "classification": zone.classification,
                    "rank_tier": rank_tier,
                    "rank_tier_label": SR_RANK_TIER_LABELS.get(rank_tier, f"Tier {rank_tier}"),
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
                    "atr": round(float(atr_series[-1]), 3),
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
                    "data_source": str(data_source or ""),
                    "adjustment_basis": str(adjustment_basis or "split_adjusted"),
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
            -int(r["rank_tier"]),
            float(r["strength"]),
            int(r["confluence_count"]),
            -abs(float(r["distance_pct"])),
        ),
        reverse=True,
    )

    return {
        "symbol": symbol,
        "exchange": exchange,
        "instrument_category": str(instrument_category or "EQ").upper(),
        "contract_expiry": str(contract_expiry or ""),
        "fno_eligible": bool(fno_eligible),
        "evaluated_timeframes": [short_tf_label(t) for t in evaluated_tfs],
        "rows": rows,
        "zones_by_tf": zones_by_tf,
        "missing_data": missing_data,
    }


# ── Step 6: Fast Strictly-Causal Bar-by-Bar S&R Indicator Series ──────────


def _infer_timeframe_from_candles(candles: Sequence[Dict[str, Any]]) -> str:
    """Infer canonical timeframe from candle spacing when caller omits timeframe."""
    if len(candles) < 2:
        return "1day"
    dates = [_extract_trade_date(c) for c in candles[: min(40, len(candles))]]
    if len(set(dates)) < len(dates):
        # Intraday: estimate median minute step on same-day consecutive bars
        deltas: List[int] = []
        for i in range(1, min(60, len(candles))):
            ts0 = _extract_ts(candles[i - 1])
            ts1 = _extract_ts(candles[i])
            if ts0[:10] == ts1[:10] and len(ts0) >= 16 and len(ts1) >= 16:
                try:
                    t0 = datetime.fromisoformat(ts0[:19])
                    t1 = datetime.fromisoformat(ts1[:19])
                    mins = int(round((t1 - t0).total_seconds() / 60.0))
                    if 1 <= mins <= 120:
                        deltas.append(mins)
                except ValueError:
                    pass
        if deltas:
            med = int(round(statistics.median(deltas)))
            if med <= 2:
                return "1minute"
            if med <= 8:
                return "5minute"
            if med <= 22:
                return "15minute"
            if med <= 45:
                return "30minute"
            return "60minute"
        return "15minute"
    return "1day"


def _htf_targets_for(norm_tf: str) -> List[str]:
    if norm_tf in {"1minute", "5minute"}:
        return ["60minute", "1day"]
    if norm_tf in {"15minute", "30minute"}:
        return ["60minute", "1day"]
    if norm_tf == "60minute":
        return ["1day"]
    if norm_tf == "1day":
        return ["1week"]
    return []


def _htf_bar_completion_key(bar: Dict[str, Any], htf: str) -> str:
    """
    Return the earliest timestamp/date string at which an HTF bar is guaranteed
    to be CLOSED so lower-timeframe bars never peek into an in-progress HTF candle.
    """
    ts = _extract_ts(bar)
    if htf == "1week":
        d_str = _extract_trade_date(bar)
        try:
            d = date.fromisoformat(d_str)
            # Weekly bar completes at end of its calendar week (Sunday); safe on or after next Monday
            sunday = d + timedelta(days=(6 - d.weekday()))
            return f"{sunday.isoformat()}T23:59:59"
        except ValueError:
            return f"{d_str}T23:59:59"
    if htf == "1day":
        d_str = _extract_trade_date(bar)
        return f"{d_str}T15:30:00"
    if htf == "60minute":
        if len(ts) >= 16 and "T" in ts:
            try:
                dt = datetime.fromisoformat(ts[:19]) + timedelta(minutes=60)
                return dt.isoformat(timespec="seconds")
            except ValueError:
                pass
    return ts


class _CausalZoneState:
    """Incremental strictly-causal state tracker for a single S&R zone."""

    __slots__ = (
        "zone",
        "episodes",
        "in_episode",
        "ep_start",
        "ep_end",
        "ep_peak_rv",
        "last_ep_end",
        "left_zone_since_last",
        "uncontrolled_crossings",
        "last_decisive_side",
        "status",
        "reactions",
        "mfe_list",
        "pending_eps",
        "eval_cursor",
        "last_break_idx",
        "last_break_dir",
        "peak_strength",
        "start_bar_index",
        "start_ts",
        "last_active_idx",
        "last_active_ts",
    )

    def __init__(self, zone: SRZone, start_idx: int, start_ts: str) -> None:
        self.zone = zone
        self.episodes: List[Tuple[int, int, float]] = []
        self.in_episode = False
        self.ep_start = -1
        self.ep_end = -1
        self.ep_peak_rv = 1.0
        self.last_ep_end = -999
        self.left_zone_since_last = True
        self.uncontrolled_crossings = 0
        self.last_decisive_side = 0
        self.status = "Active"
        self.reactions = 0
        self.mfe_list: List[float] = []
        # Pending touch episodes awaiting reaction outcome: [e_end, atr_e, max_fav, breached, confirmed]
        self.pending_eps: List[List[Any]] = []
        self.eval_cursor = -1
        self.last_break_idx = -999
        self.last_break_dir = 0  # +1 breakout above res, -1 breakdown below sup
        self.peak_strength = 0.0
        self.start_bar_index = start_idx
        self.start_ts = start_ts
        self.last_active_idx = start_idx
        self.last_active_ts = start_ts


def _advance_causal_zone_to(
    st: _CausalZoneState,
    bar_idx: int,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    atr_series: Sequence[float],
    rel_vol_series: Sequence[float],
    *,
    breakout_buf: float,
    reversal_dist: float,
    reaction_window: int,
    min_episode_gap: int,
    weights: Dict[str, float],
    major_thresh: float,
    minor_thresh: float,
    confluence_bonus: float,
    confluence_tfs: List[str],
    candles: Sequence[Dict[str, Any]],
) -> None:
    """Advance a single zone's causal touch/reaction/strength state up to `bar_idx` (inclusive)."""
    z = st.zone
    z_low = z.zone_low
    z_high = z.zone_high
    is_support = z.level_type == "Support"
    start_k = max(z.first_index, st.eval_cursor + 1)

    for k in range(start_k, bar_idx + 1):
        atr_k = max(atr_series[k], 1e-6)
        touch_pad = 0.12 * atr_k
        hi_k = highs[k]
        lo_k = lows[k]
        cl_k = closes[k]
        rv_k = rel_vol_series[k]

        # 1. Update pending episode reactions strictly on completed bar k
        still_pending: List[List[Any]] = []
        for pep in st.pending_eps:
            e_end, atr_e, max_fav, breached, confirmed = pep
            if k > e_end + reaction_window:
                continue
            if not breached and not confirmed:
                if is_support:
                    if cl_k < z_low - breakout_buf * atr_k:
                        breached = True
                    else:
                        fav = max(hi_k - z_high, cl_k - z_low)
                        if fav > max_fav:
                            max_fav = fav
                        if max_fav >= reversal_dist * atr_e:
                            confirmed = True
                            st.reactions += 1
                            st.mfe_list.append(max_fav / atr_e)
                else:
                    if cl_k > z_high + breakout_buf * atr_k:
                        breached = True
                    else:
                        fav = max(z_low - lo_k, z_high - cl_k)
                        if fav > max_fav:
                            max_fav = fav
                        if max_fav >= reversal_dist * atr_e:
                            confirmed = True
                            st.reactions += 1
                            st.mfe_list.append(max_fav / atr_e)
            if not breached and not confirmed and k < e_end + reaction_window:
                pep[2] = max_fav
                pep[3] = breached
                pep[4] = confirmed
                still_pending.append(pep)
        st.pending_eps = still_pending

        # 2. Decisive side / crossing / break tracking
        if cl_k > z_high + breakout_buf * atr_k:
            side = 1
            if not is_support and st.last_decisive_side != 1:
                st.status = "Broken"
                st.last_break_idx = k
                st.last_break_dir = 1
        elif cl_k < z_low - breakout_buf * atr_k:
            side = -1
            if is_support and st.last_decisive_side != -1:
                st.status = "Broken"
                st.last_break_idx = k
                st.last_break_dir = -1
        else:
            side = 0

        if side != 0:
            if st.last_decisive_side != 0 and side != st.last_decisive_side:
                st.uncontrolled_crossings += 1
            st.last_decisive_side = side

        # 3. Touch episode detection
        touches_bar = (lo_k <= z_high + touch_pad) and (hi_k >= z_low - touch_pad)
        if not touches_bar:
            if cl_k > z_high + 0.30 * atr_k or cl_k < z_low - 0.30 * atr_k:
                st.left_zone_since_last = True

        if touches_bar:
            if not st.in_episode:
                if (k - st.last_ep_end) >= min_episode_gap and (st.left_zone_since_last or st.last_ep_end < 0):
                    st.in_episode = True
                    st.ep_start = k
                    st.ep_end = k
                    st.ep_peak_rv = rv_k
                    st.left_zone_since_last = False
            else:
                st.ep_end = k
                if rv_k > st.ep_peak_rv:
                    st.ep_peak_rv = rv_k
        else:
            if st.in_episode:
                ep = (st.ep_start, st.ep_end, st.ep_peak_rv)
                st.episodes.append(ep)
                st.last_ep_end = st.ep_end
                st.in_episode = False
                # Queue completed touch episode for causal reaction evaluation
                e_end = ep[1]
                atr_e = max(atr_series[e_end], 1e-6)
                ep_breached = (
                    (closes[e_end] < z_low - breakout_buf * atr_e)
                    if is_support
                    else (closes[e_end] > z_high + breakout_buf * atr_e)
                )
                if not ep_breached:
                    # Also evaluate bar k (the bar immediately following ep_end) right now
                    fav_now = max(hi_k - z_high, cl_k - z_low) if is_support else max(z_low - lo_k, z_high - cl_k)
                    now_breached = (
                        (cl_k < z_low - breakout_buf * atr_k)
                        if is_support
                        else (cl_k > z_high + breakout_buf * atr_k)
                    )
                    if not now_breached and fav_now >= reversal_dist * atr_e:
                        st.reactions += 1
                        st.mfe_list.append(fav_now / atr_e)
                    elif not now_breached and k < e_end + reaction_window:
                        st.pending_eps.append([e_end, atr_e, max(0.0, fav_now), False, False])

    st.eval_cursor = bar_idx

    # Compute causal 0-100 score strictly using data up to bar_idx (n_eff = bar_idx + 1)
    n_eff = bar_idx + 1
    ep_count = len(st.episodes) + (1 if st.in_episode else 0)
    touches_count = max(ep_count, 1 if z.pivots else 0)
    first_idx = st.episodes[0][0] if st.episodes else (st.ep_start if st.in_episode else z.first_index)
    last_idx = st.ep_end if st.in_episode else (st.episodes[-1][1] if st.episodes else z.most_recent_index)

    rv_vals = [e[2] for e in st.episodes]
    if st.in_episode:
        rv_vals.append(st.ep_peak_rv)
    avg_rv = (sum(rv_vals) / len(rv_vals)) if rv_vals else 1.0
    avg_mfe = (sum(st.mfe_list) / len(st.mfe_list)) if st.mfe_list else 0.0

    base_cap = max(0.0, 100.0 - weights["confluence"])
    s_touches = weights["touches"] * (1.0 - math.exp(-0.55 * max(0, touches_count)))
    evaluable_touches = max(1, len(st.episodes) or touches_count)
    reaction_rate = min(1.0, st.reactions / float(evaluable_touches))
    reaction_count_sat = 1.0 - math.exp(-0.75 * max(0, st.reactions))
    s_reactions = weights["reactions"] * (
        0.55 * reaction_count_sat + 0.45 * reaction_rate * min(1.0, st.reactions / 2.0)
    )
    vol_norm = min(1.0, max(0.0, (avg_rv - 0.50) / 1.50))
    s_volume = weights["volume"] * vol_norm
    bars_since_last = max(0, bar_idx - last_idx)
    half_life = max(25.0, n_eff * 0.25)
    s_recency = weights["recency"] * math.exp(-math.log(2.0) * bars_since_last / half_life)
    span_bars = max(0, last_idx - first_idx)
    if touches_count >= 2:
        s_age = weights["age"] * (1.0 - math.exp(-span_bars / max(15.0, n_eff * 0.15)))
    else:
        s_age = weights["age"] * 0.20 * min(1.0, (bar_idx - first_idx) / 20.0)
    s_reversal = weights["reversal"] * (1.0 - math.exp(-0.65 * max(0.0, avg_mfe)))
    excess_crossings = max(0, st.uncontrolled_crossings - 1)
    crossing_penalty = min(35.0, excess_crossings * 9.0)

    base_raw = s_touches + s_reactions + s_volume + s_recency + s_age + s_reversal - crossing_penalty
    z.base_strength = round(max(0.0, min(base_cap, base_raw)), 2)
    z.confluence_bonus = round(min(weights["confluence"], max(0.0, confluence_bonus)), 2)
    z.confluence_timeframes = list(confluence_tfs)
    z.confluence_count = max(1, len(confluence_tfs))
    z.strength = round(max(0.0, min(100.0, z.base_strength + z.confluence_bonus)), 1)
    z.touches = touches_count
    z.reactions = st.reactions
    z.uncontrolled_crossings = st.uncontrolled_crossings
    z.status = "Invalidated" if st.uncontrolled_crossings >= 4 else st.status
    z.first_index = first_idx
    z.most_recent_index = last_idx
    z.first_occurrence = _extract_ts(candles[first_idx])
    z.most_recent_occurrence = _extract_ts(candles[last_idx])
    z.avg_rel_volume = round(avg_rv, 3)
    z.avg_mfe_atr = round(avg_mfe, 3)
    z.strength_breakdown = {
        "touches": round(s_touches, 2),
        "reactions": round(s_reactions, 2),
        "volume": round(s_volume, 2),
        "recency": round(s_recency, 2),
        "age": round(s_age, 2),
        "reversal": round(s_reversal, 2),
        "confluence": z.confluence_bonus,
        "crossing_penalty": round(crossing_penalty, 2),
    }
    if z.strength >= major_thresh:
        z.classification = "Major"
    elif z.strength >= minor_thresh:
        z.classification = "Minor"
    else:
        z.classification = "Weak"

    if z.strength > st.peak_strength:
        st.peak_strength = z.strength
    if z.status != "Invalidated":
        st.last_active_idx = bar_idx
        st.last_active_ts = _extract_ts(candles[bar_idx])


def _classify_causal_bar_event(
    st: _CausalZoneState,
    bar_idx: int,
    candles: Sequence[Dict[str, Any]],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    atr_series: Sequence[float],
    rel_vol_series: Sequence[float],
    *,
    near_dist_atr: float,
    breakout_buf: float,
    reversal_dist: float,
    retest_tol: float,
    vol_thresh: float,
) -> Optional[Tuple[str, float]]:
    """
    Fast strictly-causal event classifier at `bar_idx` using only completed bars `<= bar_idx`.
    Returns `(event_name, invalidation_price)` or `None`.
    """
    if bar_idx < 2:
        return None
    z = st.zone
    z_low = z.zone_low
    z_high = z.zone_high
    is_support = z.level_type == "Support"
    cl = closes[bar_idx]
    prev_cl = closes[bar_idx - 1]
    hi = highs[bar_idx]
    lo = lows[bar_idx]
    atr = max(atr_series[bar_idx], 1e-6)
    prev_atr = max(atr_series[bar_idx - 1], 1e-6)
    rv = rel_vol_series[bar_idx]
    op = float(candles[bar_idx]["open"])
    rng = max(hi - lo, 1e-9)

    # 1. FALSE_BREAKOUT: recent break (within 1..4 bars) that snapped back inside/across the zone
    if st.last_break_idx >= 0 and 1 <= (bar_idx - st.last_break_idx) <= 4:
        if not is_support and st.last_break_dir == 1 and cl < z_high:
            return "FALSE_BREAKOUT", round(max(hi, z_high + breakout_buf * atr), 2)
        if is_support and st.last_break_dir == -1 and cl > z_low:
            return "FALSE_BREAKOUT", round(min(lo, z_low - breakout_buf * atr), 2)

    # 2. BREAKOUT_RETEST / BREAKDOWN_RETEST: broken 2..15 bars ago, retested & held on bar_idx
    if st.last_break_idx >= 0 and 2 <= (bar_idx - st.last_break_idx) <= 15:
        if not is_support and st.last_break_dir == 1:
            touched_top = lo <= z_high + retest_tol * atr and lo >= z_low - retest_tol * atr
            held = cl >= z_high and cl > op
            if touched_top and held:
                return "BREAKOUT_RETEST", round(z_low - breakout_buf * atr, 2)
        if is_support and st.last_break_dir == -1:
            touched_bot = hi >= z_low - retest_tol * atr and hi <= z_high + retest_tol * atr
            held = cl <= z_low and cl < op
            if touched_bot and held:
                return "BREAKDOWN_RETEST", round(z_high + breakout_buf * atr, 2)

    # 3. RESISTANCE_BREAKOUT / SUPPORT_BREAKDOWN on completed bar_idx
    if not is_support:
        if cl > z_high + breakout_buf * atr and prev_cl <= z_high + breakout_buf * prev_atr:
            if (cl - lo) / rng >= 0.45 or rv >= vol_thresh:
                return "RESISTANCE_BREAKOUT", round(z_low - breakout_buf * atr, 2)
    else:
        if cl < z_low - breakout_buf * atr and prev_cl >= z_low - breakout_buf * prev_atr:
            if (hi - cl) / rng >= 0.45 or rv >= vol_thresh:
                return "SUPPORT_BREAKDOWN", round(z_high + breakout_buf * atr, 2)

    # 4. SUPPORT_BOUNCE / RESISTANCE_REJECTION (touch within last 1..4 bars + confirmed reversal)
    lookback = min(bar_idx, 4)
    if is_support:
        touched_recent = any(
            lows[k] <= z_high + 0.18 * max(atr_series[k], 1e-6)
            and closes[k] >= z_low - breakout_buf * max(atr_series[k], 1e-6)
            for k in range(bar_idx - lookback, bar_idx + 1)
        )
        if touched_recent and cl > z_high and cl > op and (cl - min(lows[bar_idx - lookback : bar_idx + 1])) >= reversal_dist * atr:
            return "SUPPORT_BOUNCE", round(z_low - breakout_buf * atr, 2)
    else:
        touched_recent = any(
            highs[k] >= z_low - 0.18 * max(atr_series[k], 1e-6)
            and closes[k] <= z_high + breakout_buf * max(atr_series[k], 1e-6)
            for k in range(bar_idx - lookback, bar_idx + 1)
        )
        if touched_recent and cl < z_low and cl < op and (max(highs[bar_idx - lookback : bar_idx + 1]) - cl) >= reversal_dist * atr:
            return "RESISTANCE_REJECTION", round(z_high + breakout_buf * atr, 2)

    # 5. NEAR_SUPPORT / NEAR_RESISTANCE
    edge_dist = (cl - z_high) if cl > z_high else ((z_low - cl) if cl < z_low else 0.0)
    if (edge_dist / atr) <= near_dist_atr:
        if is_support and cl >= z_low - 0.15 * atr:
            return "NEAR_SUPPORT", round(z_low - breakout_buf * atr, 2)
        if (not is_support) and cl <= z_high + 0.15 * atr:
            return "NEAR_RESISTANCE", round(z_high + breakout_buf * atr, 2)
    return None


def compute_causal_sr_series(
    candles: Sequence[Dict[str, Any]],
    *,
    timeframe: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    htf_confluence: Optional[bool] = None,
    min_strength: Optional[float] = None,
    collect_events: bool = True,
    collect_spans: bool = True,
) -> Dict[str, Any]:
    """
    Strictly-causal (zero-lookahead) bar-by-bar Support & Resistance indicator engine.

    Guarantee: for every bar `t` in `0..N-1`, all emitted series values, active
    zones, strength scores, HTF confluence bonuses, and events depend ONLY on
    completed candles `candles[0..t]`. Truncating the input at `candles[:t+1]`
    yields identical values at index `t`.
    """
    n = len(candles)
    eff_params = {**DEFAULT_SR_PARAMS, **(params or {})}
    if htf_confluence is not None:
        eff_params["htf_confluence"] = bool(htf_confluence)
    use_htf = bool(eff_params.get("htf_confluence", False))

    norm_tf = normalize_timeframe(timeframe) if timeframe else _infer_timeframe_from_candles(candles)
    tf_short = short_tf_label(norm_tf)
    weights = normalize_strength_weights(eff_params)

    left_bars = max(1, int(eff_params.get("left_bars", 5)))
    right_bars = max(1, int(eff_params.get("right_bars", 5)))
    min_sep = max(1, int(eff_params.get("min_pivot_separation", 3)))
    atr_period = max(2, int(eff_params.get("atr_period", 14)))
    cluster_mult = float(eff_params.get("cluster_atr_multiplier", 0.50))
    cluster_min_pct = float(eff_params.get("cluster_min_pct", 0.25))
    major_thresh = float(eff_params.get("major_strength", 70))
    minor_thresh = float(eff_params.get("minor_strength", 40))
    min_str_filter = float(min_strength if min_strength is not None else eff_params.get("min_strength", minor_thresh))
    near_dist_atr = float(eff_params.get("near_distance_atr", 0.50))
    breakout_buf = float(eff_params.get("breakout_buffer_atr", 0.20))
    reversal_dist = float(eff_params.get("reversal_distance_atr", 0.50))
    retest_tol = float(eff_params.get("retest_tolerance_atr", 0.25))
    vol_thresh = float(eff_params.get("relative_volume_threshold", 1.50))
    reaction_window = max(3, min(12, right_bars + 3))
    min_episode_gap = max(2, min(5, right_bars))

    # Pre-allocate bar-aligned series
    out_nearest_sup: List[Optional[float]] = [None] * n
    out_nearest_res: List[Optional[float]] = [None] * n
    out_sup_low: List[Optional[float]] = [None] * n
    out_sup_high: List[Optional[float]] = [None] * n
    out_res_low: List[Optional[float]] = [None] * n
    out_res_high: List[Optional[float]] = [None] * n
    out_sup_str: List[Optional[float]] = [None] * n
    out_res_str: List[Optional[float]] = [None] * n
    out_sup_touches: List[Optional[float]] = [None] * n
    out_res_touches: List[Optional[float]] = [None] * n
    out_dist_sup_pct: List[Optional[float]] = [None] * n
    out_dist_res_pct: List[Optional[float]] = [None] * n
    out_conf_count: List[Optional[float]] = [None] * n
    out_event_code: List[Optional[float]] = [0.0] * n
    event_flags: Dict[str, List[Optional[float]]] = {
        f"sr_{ev.lower()}": [0.0] * n for ev in ALL_SR_EVENTS
    }

    if n == 0:
        return {
            "timeframe": norm_tf,
            "sr_nearest_support": out_nearest_sup,
            "sr_nearest_resistance": out_nearest_res,
            "sr_support_low": out_sup_low,
            "sr_support_high": out_sup_high,
            "sr_resistance_low": out_res_low,
            "sr_resistance_high": out_res_high,
            "sr_support_strength": out_sup_str,
            "sr_resistance_strength": out_res_str,
            "sr_support_touches": out_sup_touches,
            "sr_resistance_touches": out_res_touches,
            "sr_dist_support_pct": out_dist_sup_pct,
            "sr_dist_resistance_pct": out_dist_res_pct,
            "sr_confluence_count": out_conf_count,
            "sr_event_code": out_event_code,
            **event_flags,
            "zone_spans": [],
            "events": [],
            "weights": weights,
        }

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    vols = [max(0.0, float(c.get("volume") or 0.0)) for c in candles]
    ts_list = [_extract_ts(c) for c in candles]
    atr_series = compute_atr_series(candles, period=atr_period)
    rel_vol_series = compute_relative_volume_series(candles, timeframe=norm_tf)

    # Optional completed-bar Higher-Timeframe (HTF) causal zone snapshots
    # Each entry: (htf_norm, [(completion_key, [SRZone, ...]), ...])
    htf_timelines: List[Tuple[str, List[Tuple[str, List[SRZone]]]]] = []
    if use_htf:
        for htf in _htf_targets_for(norm_tf):
            try:
                htf_bars = resample_ohlcv_rows(list(candles), htf)
            except Exception:
                htf_bars = []
            if len(htf_bars) < left_bars + right_bars + 3:
                continue
            htf_highs = [float(b["high"]) for b in htf_bars]
            htf_lows = [float(b["low"]) for b in htf_bars]
            htf_vols = [max(0.0, float(b.get("volume") or 0.0)) for b in htf_bars]
            htf_atr = compute_atr_series(htf_bars, period=atr_period)
            htf_sh: List[ConfirmedPivot] = []
            htf_sl: List[ConfirmedPivot] = []
            htf_timeline: List[Tuple[str, List[SRZone]]] = []
            cur_htf_zones: List[SRZone] = []
            for h_idx in range(len(htf_bars)):
                cand_p = h_idx - right_bars
                changed = False
                if cand_p >= left_bars:
                    hp = htf_highs[cand_p]
                    is_h = True
                    min_h = hp
                    for j in range(cand_p - left_bars, cand_p):
                        if htf_highs[j] >= hp:
                            is_h = False
                            break
                        if htf_highs[j] < min_h:
                            min_h = htf_highs[j]
                    if is_h:
                        for j in range(cand_p + 1, cand_p + right_bars + 1):
                            if htf_highs[j] > hp:
                                is_h = False
                                break
                            if htf_highs[j] < min_h:
                                min_h = htf_highs[j]
                    if is_h and hp > min_h:
                        piv = ConfirmedPivot(
                            price=hp,
                            pivot_type="HIGH",
                            index=cand_p,
                            confirm_index=h_idx,
                            occurrence_ts=_extract_ts(htf_bars[cand_p]),
                            confirmation_ts=_extract_ts(htf_bars[h_idx]),
                            timeframe=htf,
                            volume=htf_vols[cand_p],
                            atr=float(htf_atr[cand_p]),
                        )
                        if htf_sh and (cand_p - htf_sh[-1].index) < min_sep:
                            if piv.price > htf_sh[-1].price:
                                htf_sh[-1] = piv
                                changed = True
                        else:
                            htf_sh.append(piv)
                            changed = True

                    lp = htf_lows[cand_p]
                    is_l = True
                    max_l = lp
                    for j in range(cand_p - left_bars, cand_p):
                        if htf_lows[j] <= lp:
                            is_l = False
                            break
                        if htf_lows[j] > max_l:
                            max_l = htf_lows[j]
                    if is_l:
                        for j in range(cand_p + 1, cand_p + right_bars + 1):
                            if htf_lows[j] < lp:
                                is_l = False
                                break
                            if htf_lows[j] > max_l:
                                max_l = htf_lows[j]
                    if is_l and lp < max_l:
                        piv = ConfirmedPivot(
                            price=lp,
                            pivot_type="LOW",
                            index=cand_p,
                            confirm_index=h_idx,
                            occurrence_ts=_extract_ts(htf_bars[cand_p]),
                            confirmation_ts=_extract_ts(htf_bars[h_idx]),
                            timeframe=htf,
                            volume=htf_vols[cand_p],
                            atr=float(htf_atr[cand_p]),
                        )
                        if htf_sl and (cand_p - htf_sl[-1].index) < min_sep:
                            if piv.price < htf_sl[-1].price:
                                htf_sl[-1] = piv
                                changed = True
                        else:
                            htf_sl.append(piv)
                            changed = True
                if changed:
                    cur_htf_zones = (
                        cluster_pivots_into_zones(
                            htf_sl,
                            "Support",
                            htf,
                            cluster_atr_multiplier=cluster_mult,
                            cluster_min_pct=cluster_min_pct,
                        )
                        + cluster_pivots_into_zones(
                            htf_sh,
                            "Resistance",
                            htf,
                            cluster_atr_multiplier=cluster_mult,
                            cluster_min_pct=cluster_min_pct,
                        )
                    )
                htf_timeline.append((_htf_bar_completion_key(htf_bars[h_idx], htf), list(cur_htf_zones)))
            if htf_timeline:
                htf_timelines.append((htf, htf_timeline))

    htf_cursors: List[int] = [-1] * len(htf_timelines)
    conf_scale = weights["confluence"] / max(1e-6, SR_STRENGTH_WEIGHTS["confluence"])

    # Causal pivot pools (updated strictly at bar t == p + right_bars)
    confirmed_highs: List[ConfirmedPivot] = []
    confirmed_lows: List[ConfirmedPivot] = []
    active_states: Dict[str, _CausalZoneState] = {}
    all_zone_history: Dict[str, _CausalZoneState] = {}
    recorded_events: List[Dict[str, Any]] = []

    event_precedence = {
        "FALSE_BREAKOUT": 1,
        "BREAKOUT_RETEST": 2,
        "BREAKDOWN_RETEST": 2,
        "RESISTANCE_BREAKOUT": 3,
        "SUPPORT_BREAKDOWN": 3,
        "SUPPORT_BOUNCE": 4,
        "RESISTANCE_REJECTION": 4,
        "NEAR_SUPPORT": 5,
        "NEAR_RESISTANCE": 5,
    }

    for t in range(n):
        ts_t = ts_list[t]
        cl_t = closes[t]
        atr_t = max(atr_series[t], 1e-6)

        # Advance HTF completed-bar cursors strictly (htf_completion_key <= ts_t)
        active_htf_zones: List[Tuple[str, List[SRZone]]] = []
        for idx_h, (htf_name, htf_tl) in enumerate(htf_timelines):
            cur_c = htf_cursors[idx_h]
            while cur_c + 1 < len(htf_tl) and htf_tl[cur_c + 1][0] <= ts_t:
                cur_c += 1
            htf_cursors[idx_h] = cur_c
            if cur_c >= 0 and htf_tl[cur_c][1]:
                active_htf_zones.append((htf_name, htf_tl[cur_c][1]))

        # Check newly confirmed fractal pivot at candidate index p = t - right_bars
        cand_p = t - right_bars
        highs_changed = False
        lows_changed = False
        if cand_p >= left_bars:
            hp = highs[cand_p]
            is_h = True
            min_h = hp
            for j in range(cand_p - left_bars, cand_p):
                if highs[j] >= hp:
                    is_h = False
                    break
                if highs[j] < min_h:
                    min_h = highs[j]
            if is_h:
                for j in range(cand_p + 1, cand_p + right_bars + 1):
                    if highs[j] > hp:
                        is_h = False
                        break
                    if highs[j] < min_h:
                        min_h = highs[j]
            if is_h and hp > min_h:
                piv = ConfirmedPivot(
                    price=hp,
                    pivot_type="HIGH",
                    index=cand_p,
                    confirm_index=t,
                    occurrence_ts=ts_list[cand_p],
                    confirmation_ts=ts_t,
                    timeframe=norm_tf,
                    volume=vols[cand_p],
                    atr=float(atr_series[cand_p]),
                )
                if confirmed_highs and (cand_p - confirmed_highs[-1].index) < min_sep:
                    if piv.price > confirmed_highs[-1].price:
                        confirmed_highs[-1] = piv
                        highs_changed = True
                else:
                    confirmed_highs.append(piv)
                    highs_changed = True

            lp = lows[cand_p]
            is_l = True
            max_l = lp
            for j in range(cand_p - left_bars, cand_p):
                if lows[j] <= lp:
                    is_l = False
                    break
                if lows[j] > max_l:
                    max_l = lows[j]
            if is_l:
                for j in range(cand_p + 1, cand_p + right_bars + 1):
                    if lows[j] < lp:
                        is_l = False
                        break
                    if lows[j] > max_l:
                        max_l = lows[j]
            if is_l and lp < max_l:
                piv = ConfirmedPivot(
                    price=lp,
                    pivot_type="LOW",
                    index=cand_p,
                    confirm_index=t,
                    occurrence_ts=ts_list[cand_p],
                    confirmation_ts=ts_t,
                    timeframe=norm_tf,
                    volume=vols[cand_p],
                    atr=float(atr_series[cand_p]),
                )
                if confirmed_lows and (cand_p - confirmed_lows[-1].index) < min_sep:
                    if piv.price < confirmed_lows[-1].price:
                        confirmed_lows[-1] = piv
                        lows_changed = True
                else:
                    confirmed_lows.append(piv)
                    lows_changed = True

        # Re-cluster ONLY the side that gained a new confirmed pivot on bar t
        if lows_changed or highs_changed:
            new_zones: List[SRZone] = []
            if lows_changed:
                new_zones.extend(
                    cluster_pivots_into_zones(
                        confirmed_lows,
                        "Support",
                        norm_tf,
                        cluster_atr_multiplier=cluster_mult,
                        cluster_min_pct=cluster_min_pct,
                    )
                )
            else:
                new_zones.extend(st.zone for st in active_states.values() if st.zone.level_type == "Support")

            if highs_changed:
                new_zones.extend(
                    cluster_pivots_into_zones(
                        confirmed_highs,
                        "Resistance",
                        norm_tf,
                        cluster_atr_multiplier=cluster_mult,
                        cluster_min_pct=cluster_min_pct,
                    )
                )
            else:
                new_zones.extend(st.zone for st in active_states.values() if st.zone.level_type == "Resistance")

            next_active: Dict[str, _CausalZoneState] = {}
            for z in new_zones:
                prev_st = active_states.get(z.zone_id)
                if (
                    prev_st is not None
                    and abs(prev_st.zone.zone_low - z.zone_low) < 1e-9
                    and abs(prev_st.zone.zone_high - z.zone_high) < 1e-9
                    and len(prev_st.zone.pivots) == len(z.pivots)
                ):
                    next_active[z.zone_id] = prev_st
                else:
                    # Reuse original span start if a zone refined slightly around the same ID
                    hist_st = all_zone_history.get(z.zone_id)
                    s_idx = hist_st.start_bar_index if hist_st is not None else t
                    s_ts = hist_st.start_ts if hist_st is not None else ts_t
                    st = _CausalZoneState(z, s_idx, s_ts)
                    next_active[z.zone_id] = st
                    all_zone_history[z.zone_id] = st
            active_states = next_active

        if not active_states:
            continue

        # Advance every active zone incrementally to bar t and check HTF confluence
        best_sup: Optional[SRZone] = None
        best_sup_dist = float("inf")
        best_res: Optional[SRZone] = None
        best_res_dist = float("inf")
        best_event_tuple: Optional[Tuple[int, float, str, SRZone, float]] = None

        for st in active_states.values():
            z = st.zone
            # Check completed HTF confluence for this zone
            conf_tfs = [tf_short]
            conf_bonus = 0.0
            if active_htf_zones:
                for htf_name, htf_zlist in active_htf_zones:
                    if any(_zones_overlap_mtf(z, hz) for hz in htf_zlist):
                        conf_tfs.append(short_tf_label(htf_name))
                        conf_bonus += TF_CONFLUENCE_WEIGHT.get(htf_name, 3.0) * conf_scale

            _advance_causal_zone_to(
                st,
                t,
                highs,
                lows,
                closes,
                atr_series,
                rel_vol_series,
                breakout_buf=breakout_buf,
                reversal_dist=reversal_dist,
                reaction_window=reaction_window,
                min_episode_gap=min_episode_gap,
                weights=weights,
                major_thresh=major_thresh,
                minor_thresh=minor_thresh,
                confluence_bonus=conf_bonus,
                confluence_tfs=conf_tfs,
                candles=candles,
            )

            if z.status == "Invalidated":
                continue

            # Track nearest qualifying Support below/at price and Resistance above/at price
            if z.strength >= min_str_filter:
                if z.level_type == "Support" and z.zone_low <= cl_t + 0.5 * atr_t:
                    d_sup = max(0.0, cl_t - z.representative_price)
                    if d_sup < best_sup_dist:
                        best_sup_dist = d_sup
                        best_sup = z
                elif z.level_type == "Resistance" and z.zone_high >= cl_t - 0.5 * atr_t:
                    d_res = max(0.0, z.representative_price - cl_t)
                    if d_res < best_res_dist:
                        best_res_dist = d_res
                        best_res = z

                ev_pair = _classify_causal_bar_event(
                    st,
                    t,
                    candles,
                    highs,
                    lows,
                    closes,
                    atr_series,
                    rel_vol_series,
                    near_dist_atr=near_dist_atr,
                    breakout_buf=breakout_buf,
                    reversal_dist=reversal_dist,
                    retest_tol=retest_tol,
                    vol_thresh=vol_thresh,
                )
                if ev_pair is not None:
                    ev_name, inv_px = ev_pair
                    event_flags[f"sr_{ev_name.lower()}"][t] = 1.0
                    prec = event_precedence.get(ev_name, 9)
                    cand_rank = (prec, -z.strength)
                    if best_event_tuple is None or cand_rank < (best_event_tuple[0], best_event_tuple[1]):
                        best_event_tuple = (prec, -z.strength, ev_name, z, inv_px)
                    if collect_events:
                        recorded_events.append(
                            {
                                "bar_index": t,
                                "ts": ts_t,
                                "event": ev_name,
                                "level_type": z.level_type,
                                "zone_id": z.zone_id,
                                "zone_low": round(z.zone_low, 2),
                                "zone_high": round(z.zone_high, 2),
                                "representative_price": round(z.representative_price, 2),
                                "close": round(cl_t, 2),
                                "strength": z.strength,
                                "classification": z.classification,
                                "touches": z.touches,
                                "reactions": z.reactions,
                                "confluence_count": z.confluence_count,
                                "confluence_timeframes": list(z.confluence_timeframes),
                                "atr": round(atr_t, 4),
                                "relative_volume": rel_vol_series[t],
                                "invalidation_price": inv_px,
                                "timeframe": tf_short,
                            }
                        )

        if best_sup is not None:
            out_nearest_sup[t] = round(best_sup.representative_price, 2)
            out_sup_low[t] = round(best_sup.zone_low, 2)
            out_sup_high[t] = round(best_sup.zone_high, 2)
            out_sup_str[t] = best_sup.strength
            out_sup_touches[t] = float(best_sup.touches)
            if cl_t > 0:
                out_dist_sup_pct[t] = round(((cl_t - best_sup.representative_price) / cl_t) * 100.0, 3)

        if best_res is not None:
            out_nearest_res[t] = round(best_res.representative_price, 2)
            out_res_low[t] = round(best_res.zone_low, 2)
            out_res_high[t] = round(best_res.zone_high, 2)
            out_res_str[t] = best_res.strength
            out_res_touches[t] = float(best_res.touches)
            if cl_t > 0:
                out_dist_res_pct[t] = round(((best_res.representative_price - cl_t) / cl_t) * 100.0, 3)

        c_cnt = max(
            best_sup.confluence_count if best_sup is not None else 0,
            best_res.confluence_count if best_res is not None else 0,
        )
        out_conf_count[t] = float(c_cnt) if c_cnt > 0 else 0.0

        if best_event_tuple is not None:
            out_event_code[t] = float(SR_EVENT_CODES.get(best_event_tuple[2], 0))

    zone_spans: List[Dict[str, Any]] = []
    if collect_spans:
        for st in all_zone_history.values():
            z = st.zone
            if st.peak_strength < minor_thresh:
                continue
            zone_spans.append(
                {
                    "zone_id": z.zone_id,
                    "level_type": z.level_type,
                    "zone_low": round(z.zone_low, 2),
                    "zone_high": round(z.zone_high, 2),
                    "representative_price": round(z.representative_price, 2),
                    "start_bar_index": st.start_bar_index,
                    "start_ts": st.start_ts,
                    "end_bar_index": st.last_active_idx,
                    "end_ts": st.last_active_ts,
                    "peak_strength": round(st.peak_strength, 1),
                    "final_strength": z.strength,
                    "classification": "Major" if st.peak_strength >= major_thresh else "Minor",
                    "touches": z.touches,
                    "reactions": z.reactions,
                    "confluence_count": z.confluence_count,
                    "confluence_timeframes": list(z.confluence_timeframes),
                    "status": z.status,
                    "timeframe": tf_short,
                }
            )
        zone_spans.sort(
            key=lambda s: (
                1 if s["classification"] == "Major" else 0,
                float(s["peak_strength"]),
                int(s["end_bar_index"]),
            ),
            reverse=True,
        )

    return {
        "timeframe": norm_tf,
        "sr_nearest_support": out_nearest_sup,
        "sr_nearest_resistance": out_nearest_res,
        "sr_support_low": out_sup_low,
        "sr_support_high": out_sup_high,
        "sr_resistance_low": out_res_low,
        "sr_resistance_high": out_res_high,
        "sr_support_strength": out_sup_str,
        "sr_resistance_strength": out_res_str,
        "sr_support_touches": out_sup_touches,
        "sr_resistance_touches": out_res_touches,
        "sr_dist_support_pct": out_dist_sup_pct,
        "sr_dist_resistance_pct": out_dist_res_pct,
        "sr_confluence_count": out_conf_count,
        "sr_event_code": out_event_code,
        **event_flags,
        "zone_spans": zone_spans[:40],
        "events": recorded_events,
        "weights": weights,
    }


__all__ = [
    "ALL_SR_EVENTS",
    "ConfirmedPivot",
    "DEFAULT_SR_PARAMS",
    "INTRADAY_TIMEFRAMES",
    "SRZone",
    "SREvent",
    "SR_DEFAULT_TIMEFRAMES",
    "SR_EVENT_CODES",
    "SR_RANK_TIER_LABELS",
    "SR_STRENGTH_WEIGHTS",
    "TF_CONFLUENCE_WEIGHT",
    "TF_HIERARCHY_RANK",
    "TF_SHORT_LABELS",
    "analyze_single_timeframe",
    "analyze_zone_interactions_and_strength",
    "classify_zone_event",
    "cluster_pivots_into_zones",
    "compute_atr_series",
    "compute_causal_sr_series",
    "compute_mtf_confluence_and_classify",
    "compute_relative_volume_series",
    "compute_sr_rank_tier",
    "detect_confirmed_pivots",
    "evaluate_symbol_mtf",
    "filter_complete_weekly_candles",
    "normalize_strength_weights",
    "short_tf_label",
    "slice_candles_as_of",
]
