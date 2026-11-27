"""
screener/sr_validation.py
=========================
Statistical Support & Resistance Event Validation & Baseline Benchmark Engine.

Implements §7.2 and §7.4 of the S&R specification:
- Evaluates forward outcomes of strictly-causal S&R events across configurable
  horizons (default 5, 10, 20 bars) with zero look-ahead.
- Computes:
  - Bounce success rate, Rejection success rate, Breakout sustain rate,
    Retest hold rate, False breakout rate
  - Mean & Median Favorable Excursion (MFE in ATR and %)
  - Mean & Median Adverse Excursion (MAE in ATR and %)
  - Average reaction time (bars to first qualifying favorable excursion)
  - Breakdowns by Event Type, Strength Tier (Major / Minor / Weak),
    Timeframe, and Market Regime (Trending / Sideways / Volatile)
- Runs 4 comparable baselines on the EXACT same OHLCV bars and horizons:
  1. Full S&R Engine (ATR-clustered + 7-weight strength scored)
  2. Unscored ATR Clusters (clustering without strength threshold filtering)
  3. Unclustered Fractal Pivots (raw confirmed pivots without clustering)
  4. Rolling High/Low (simple N-bar rolling swing highs/lows)
  5. Random Comparable Price Levels (deterministic seeded RNG)
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from indicators.toolkit import compute_adx_series
from screener.sr_engine import (
    DEFAULT_SR_PARAMS,
    compute_atr_series,
    compute_causal_sr_series,
    detect_confirmed_pivots,
    normalize_strength_weights,
    short_tf_label,
)

BULLISH_SR_EVENTS = {
    "NEAR_SUPPORT",
    "SUPPORT_BOUNCE",
    "RESISTANCE_BREAKOUT",
    "BREAKOUT_RETEST",
}
BEARISH_SR_EVENTS = {
    "NEAR_RESISTANCE",
    "RESISTANCE_REJECTION",
    "SUPPORT_BREAKDOWN",
    "BREAKDOWN_RETEST",
    "FALSE_BREAKOUT",
}


def _classify_market_regimes(candles: Sequence[Dict[str, Any]], atr_series: Sequence[float]) -> List[str]:
    """
    Strictly-causal bar-level regime classification (`Trending`, `Volatile`, `Sideways`)
    using ADX(14) and causal trailing ATR% percentile.
    """
    n = len(candles)
    if n == 0:
        return []
    adx_s = compute_adx_series(list(candles), period=14)["adx"]
    atr_pcts: List[float] = []
    regimes: List[str] = ["Sideways"] * n

    for i in range(n):
        cl = max(float(candles[i]["close"]), 1e-6)
        ap = (float(atr_series[i]) / cl) * 100.0
        atr_pcts.append(ap)
        # Causal trailing window (up to 60 bars <= i)
        win = atr_pcts[max(0, i - 59) : i + 1]
        is_volatile = False
        if len(win) >= 15:
            sorted_win = sorted(win)
            q75 = sorted_win[int(0.75 * (len(sorted_win) - 1))]
            if ap >= q75 and ap >= 1.8:
                is_volatile = True
        adx_val = adx_s[i]
        if is_volatile:
            regimes[i] = "Volatile"
        elif adx_val is not None and adx_val >= 25.0:
            regimes[i] = "Trending"
        else:
            regimes[i] = "Sideways"
    return regimes


def _evaluate_forward_outcome(
    event: Dict[str, Any],
    candles: Sequence[Dict[str, Any]],
    atr_series: Sequence[float],
    horizon: int,
    *,
    success_atr_mult: float = 0.75,
) -> Optional[Dict[str, Any]]:
    """
    Measure strictly-forward price excursions over `candles[t+1 .. t+horizon]`
    for an event emitted on completed bar `t`.
    """
    t = int(event["bar_index"])
    n = len(candles)
    if t + 1 >= n:
        return None
    end_idx = min(n - 1, t + max(1, int(horizon)))
    entry_px = float(candles[t]["close"])
    atr = max(float(event.get("atr") or atr_series[t]), 1e-6)
    ev_name = str(event["event"]).upper()
    inv_px = event.get("invalidation_price")
    inv_f = float(inv_px) if inv_px is not None else None

    is_bull = ev_name in BULLISH_SR_EVENTS
    max_fav = 0.0
    max_adv = 0.0
    reaction_bars: Optional[int] = None
    hit_invalidation = False
    false_breakout_4b = False

    z_low = float(event.get("zone_low") or entry_px)
    z_high = float(event.get("zone_high") or entry_px)

    for k in range(t + 1, end_idx + 1):
        hi = float(candles[k]["high"])
        lo = float(candles[k]["low"])
        cl = float(candles[k]["close"])
        step = k - t

        if is_bull:
            fav = hi - entry_px
            adv = entry_px - lo
            if inv_f is not None and cl < inv_f:
                hit_invalidation = True
            if ev_name == "RESISTANCE_BREAKOUT" and step <= 4 and cl < z_high:
                false_breakout_4b = True
        else:
            fav = entry_px - lo
            adv = hi - entry_px
            if inv_f is not None and cl > inv_f:
                hit_invalidation = True
            if ev_name == "SUPPORT_BREAKDOWN" and step <= 4 and cl > z_low:
                false_breakout_4b = True

        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv
        if reaction_bars is None and fav >= success_atr_mult * atr:
            reaction_bars = step

    end_close = float(candles[end_idx]["close"])
    signed_ret_pct = (
        ((end_close - entry_px) / entry_px) * 100.0
        if is_bull
        else ((entry_px - end_close) / entry_px) * 100.0
    )
    mfe_atr = max_fav / atr
    mae_atr = max_adv / atr
    mfe_pct = (max_fav / entry_px) * 100.0 if entry_px > 0 else 0.0
    mae_pct = (max_adv / entry_px) * 100.0 if entry_px > 0 else 0.0

    # Event-specific qualification: favorable excursion >= threshold OR positive close without invalidation
    if ev_name in {"RESISTANCE_BREAKOUT", "SUPPORT_BREAKDOWN"}:
        succeeded = (not false_breakout_4b) and (not hit_invalidation) and (
            mfe_atr >= success_atr_mult or signed_ret_pct > 0.0
        )
    elif ev_name in {"BREAKOUT_RETEST", "BREAKDOWN_RETEST"}:
        succeeded = (not hit_invalidation) and (mfe_atr >= success_atr_mult or signed_ret_pct > 0.0)
    else:
        succeeded = (mfe_atr >= success_atr_mult and (not hit_invalidation or mfe_atr > mae_atr))

    return {
        "succeeded": bool(succeeded),
        "false_breakout": bool(false_breakout_4b or ev_name == "FALSE_BREAKOUT"),
        "hit_invalidation": bool(hit_invalidation),
        "mfe_atr": round(mfe_atr, 3),
        "mae_atr": round(mae_atr, 3),
        "mfe_pct": round(mfe_pct, 3),
        "mae_pct": round(mae_pct, 3),
        "signed_ret_pct": round(signed_ret_pct, 3),
        "reaction_bars": reaction_bars,
    }


def _aggregate_outcomes(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    count = len(rows)
    if count == 0:
        return {
            "count": 0,
            "success_rate_pct": 0.0,
            "avg_mfe_atr": 0.0,
            "avg_mae_atr": 0.0,
            "avg_mfe_pct": 0.0,
            "avg_mae_pct": 0.0,
            "avg_return_pct": 0.0,
            "edge_ratio": 0.0,
            "avg_reaction_bars": None,
        }
    wins = sum(1 for r in rows if r["succeeded"])
    avg_mfe_atr = sum(r["mfe_atr"] for r in rows) / count
    avg_mae_atr = sum(r["mae_atr"] for r in rows) / count
    avg_mfe_pct = sum(r["mfe_pct"] for r in rows) / count
    avg_mae_pct = sum(r["mae_pct"] for r in rows) / count
    avg_ret = sum(r["signed_ret_pct"] for r in rows) / count
    react = [r["reaction_bars"] for r in rows if r["reaction_bars"] is not None]
    edge = (avg_mfe_atr / avg_mae_atr) if avg_mae_atr > 1e-6 else avg_mfe_atr
    return {
        "count": count,
        "success_rate_pct": round((wins / count) * 100.0, 2),
        "avg_mfe_atr": round(avg_mfe_atr, 3),
        "avg_mae_atr": round(avg_mae_atr, 3),
        "avg_mfe_pct": round(avg_mfe_pct, 2),
        "avg_mae_pct": round(avg_mae_pct, 2),
        "avg_return_pct": round(avg_ret, 2),
        "edge_ratio": round(edge, 3),
        "avg_reaction_bars": round(sum(react) / len(react), 2) if react else None,
    }


def _generate_baseline_events(
    candles: Sequence[Dict[str, Any]],
    atr_series: Sequence[float],
    *,
    timeframe: str,
    params: Dict[str, Any],
    seed: int = 42,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Generate comparable causal events for the 3 non-full baselines on the exact same bars:
    - `unclustered_pivots`: raw confirmed fractal pivots without clustering
    - `rolling_high_low`: simple 20-bar rolling swing high/low touches & breakouts
    - `random_levels`: deterministic random price levels anchored near trailing close
    """
    n = len(candles)
    lb = max(2, int(params.get("left_bars", 5)))
    rb = max(2, int(params.get("right_bars", 5)))
    min_sep = max(1, int(params.get("min_pivot_separation", 3)))
    buf = float(params.get("breakout_buffer_atr", 0.20))

    sh, sl = detect_confirmed_pivots(
        candles,
        atr_series,
        timeframe=timeframe,
        left_bars=lb,
        right_bars=rb,
        min_pivot_separation=min_sep,
    )

    unclustered: List[Dict[str, Any]] = []
    rolling_hl: List[Dict[str, Any]] = []
    rand_ev: List[Dict[str, Any]] = []

    rng = random.Random(seed)
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    # Index confirmed pivots by confirm_index for strictly-causal sweep
    active_hi_pivs: List[float] = []
    active_lo_pivs: List[float] = []
    hi_by_confirm: Dict[int, List[float]] = {}
    lo_by_confirm: Dict[int, List[float]] = {}
    for p in sh:
        hi_by_confirm.setdefault(p.confirm_index, []).append(p.price)
    for p in sl:
        lo_by_confirm.setdefault(p.confirm_index, []).append(p.price)

    roll_win = 20
    for t in range(max(roll_win + 1, lb + rb + 1), n - 1):
        if t in hi_by_confirm:
            active_hi_pivs.extend(hi_by_confirm[t])
            active_hi_pivs = active_hi_pivs[-12:]
        if t in lo_by_confirm:
            active_lo_pivs.extend(lo_by_confirm[t])
            active_lo_pivs = active_lo_pivs[-12:]

        cl = closes[t]
        prev_cl = closes[t - 1]
        atr = max(atr_series[t], 1e-6)
        op = float(candles[t]["open"])

        # Baseline A: Unclustered raw confirmed fractal pivots
        if active_hi_pivs:
            nearest_h = min(active_hi_pivs, key=lambda px: abs(px - cl))
            if cl > nearest_h + buf * atr and prev_cl <= nearest_h + buf * atr:
                unclustered.append(
                    {
                        "bar_index": t,
                        "event": "RESISTANCE_BREAKOUT",
                        "zone_low": nearest_h,
                        "zone_high": nearest_h,
                        "atr": atr,
                        "invalidation_price": nearest_h - buf * atr,
                    }
                )
            elif highs[t] >= nearest_h - 0.15 * atr and cl < nearest_h and cl < op:
                unclustered.append(
                    {
                        "bar_index": t,
                        "event": "RESISTANCE_REJECTION",
                        "zone_low": nearest_h,
                        "zone_high": nearest_h,
                        "atr": atr,
                        "invalidation_price": nearest_h + buf * atr,
                    }
                )
        if active_lo_pivs:
            nearest_l = min(active_lo_pivs, key=lambda px: abs(px - cl))
            if cl < nearest_l - buf * atr and prev_cl >= nearest_l - buf * atr:
                unclustered.append(
                    {
                        "bar_index": t,
                        "event": "SUPPORT_BREAKDOWN",
                        "zone_low": nearest_l,
                        "zone_high": nearest_l,
                        "atr": atr,
                        "invalidation_price": nearest_l + buf * atr,
                    }
                )
            elif lows[t] <= nearest_l + 0.15 * atr and cl > nearest_l and cl > op:
                unclustered.append(
                    {
                        "bar_index": t,
                        "event": "SUPPORT_BOUNCE",
                        "zone_low": nearest_l,
                        "zone_high": nearest_l,
                        "atr": atr,
                        "invalidation_price": nearest_l - buf * atr,
                    }
                )

        # Baseline B: Simple 20-bar rolling swing high/low
        rh = max(highs[t - roll_win : t])
        rl = min(lows[t - roll_win : t])
        if cl > rh + buf * atr and prev_cl <= rh:
            rolling_hl.append(
                {
                    "bar_index": t,
                    "event": "RESISTANCE_BREAKOUT",
                    "zone_low": rh,
                    "zone_high": rh,
                    "atr": atr,
                    "invalidation_price": rh - buf * atr,
                }
            )
        elif cl < rl - buf * atr and prev_cl >= rl:
            rolling_hl.append(
                {
                    "bar_index": t,
                    "event": "SUPPORT_BREAKDOWN",
                    "zone_low": rl,
                    "zone_high": rl,
                    "atr": atr,
                    "invalidation_price": rl + buf * atr,
                }
            )
        elif lows[t] <= rl + 0.25 * atr and cl > rl and cl > op:
            rolling_hl.append(
                {
                    "bar_index": t,
                    "event": "SUPPORT_BOUNCE",
                    "zone_low": rl,
                    "zone_high": rl,
                    "atr": atr,
                    "invalidation_price": rl - buf * atr,
                }
            )

        # Baseline C: Seeded random comparable levels (~10% bar sampling rate)
        if rng.random() < 0.10:
            ev_choice = rng.choice(["SUPPORT_BOUNCE", "RESISTANCE_BREAKOUT", "RESISTANCE_REJECTION", "SUPPORT_BREAKDOWN"])
            lvl = cl + rng.uniform(-0.4, 0.4) * atr
            inv = (lvl - buf * atr) if ev_choice in BULLISH_SR_EVENTS else (lvl + buf * atr)
            rand_ev.append(
                {
                    "bar_index": t,
                    "event": ev_choice,
                    "zone_low": lvl,
                    "zone_high": lvl,
                    "atr": atr,
                    "invalidation_price": inv,
                }
            )

    return {
        "unclustered_pivots": unclustered,
        "rolling_high_low": rolling_hl,
        "random_levels": rand_ev,
    }


def evaluate_sr_validation(
    candles_by_symbol: Dict[str, Sequence[Dict[str, Any]]],
    *,
    timeframe: str = "1day",
    params: Optional[Dict[str, Any]] = None,
    horizons: Sequence[int] = (5, 10, 20),
    primary_horizon: int = 10,
    htf_confluence: bool = True,
) -> Dict[str, Any]:
    """
    Run a deterministic, zero-lookahead statistical validation study across one or
    more symbols and compare the full S&R engine against 4 baselines.
    """
    eff_params = {**DEFAULT_SR_PARAMS, **(params or {})}
    eff_params["htf_confluence"] = bool(htf_confluence)
    weights = normalize_strength_weights(eff_params)
    clean_horizons = sorted({max(1, int(h)) for h in (horizons or (5, 10, 20))})
    prim_h = int(primary_horizon) if int(primary_horizon) in clean_horizons else clean_horizons[min(1, len(clean_horizons) - 1)]
    tf_short = short_tf_label(timeframe)
    minor_thresh = float(eff_params.get("minor_strength", 40.0))

    # Collectors keyed by horizon
    primary_outcomes: List[Dict[str, Any]] = []
    by_event: Dict[str, List[Dict[str, Any]]] = {}
    by_tier: Dict[str, List[Dict[str, Any]]] = {"Major": [], "Minor": [], "Weak": []}
    by_regime: Dict[str, List[Dict[str, Any]]] = {"Trending": [], "Sideways": [], "Volatile": []}
    by_horizon: Dict[int, List[Dict[str, Any]]] = {h: [] for h in clean_horizons}

    baseline_outcomes: Dict[str, List[Dict[str, Any]]] = {
        "full_sr_engine": [],
        "unscored_atr_clusters": [],
        "unclustered_pivots": [],
        "rolling_high_low": [],
        "random_levels": [],
    }

    symbols_evaluated: List[str] = []
    total_bars = 0

    for sym_idx, (symbol, candles) in enumerate(sorted(candles_by_symbol.items())):
        if not candles or len(candles) < 35:
            continue
        symbols_evaluated.append(symbol)
        total_bars += len(candles)
        atr_series = compute_atr_series(candles, period=int(eff_params.get("atr_period", 14)))
        regimes = _classify_market_regimes(candles, atr_series)

        # 1. Full S&R Engine (including Weak so we can prove Major > Minor > Weak!)
        full_all = compute_causal_sr_series(
            candles,
            timeframe=timeframe,
            params=eff_params,
            htf_confluence=htf_confluence,
            min_strength=0.0,
            collect_events=True,
            collect_spans=False,
        )
        # De-duplicate consecutive identical (zone_id, event) firings within 3 bars for fair event statistics
        last_seen: Dict[Tuple[str, str], int] = {}
        dedup_all_events: List[Dict[str, Any]] = []
        for ev in full_all.get("events") or []:
            key = (str(ev["zone_id"]), str(ev["event"]))
            b_idx = int(ev["bar_index"])
            if key in last_seen and (b_idx - last_seen[key]) <= 3:
                continue
            last_seen[key] = b_idx
            dedup_all_events.append(ev)

        for ev in dedup_all_events:
            b_idx = int(ev["bar_index"])
            tier = str(ev.get("classification") or "Minor")
            regime = regimes[b_idx] if 0 <= b_idx < len(regimes) else "Sideways"
            strength_val = float(ev.get("strength") or 0.0)

            for h in clean_horizons:
                out = _evaluate_forward_outcome(ev, candles, atr_series, h)
                if out is None:
                    continue
                enriched = {
                    **out,
                    "symbol": symbol,
                    "event": ev["event"],
                    "classification": tier,
                    "strength": strength_val,
                    "regime": regime,
                    "timeframe": tf_short,
                }
                if strength_val >= minor_thresh:
                    by_horizon[h].append(enriched)
                if h == prim_h:
                    # Unscored ATR clusters includes all zones regardless of strength threshold
                    baseline_outcomes["unscored_atr_clusters"].append(enriched)
                    by_tier.setdefault(tier, []).append(enriched)
                    if strength_val >= minor_thresh:
                        primary_outcomes.append(enriched)
                        baseline_outcomes["full_sr_engine"].append(enriched)
                        by_event.setdefault(str(ev["event"]), []).append(enriched)
                        by_regime.setdefault(regime, []).append(enriched)

        # 2. Non-clustered & random baselines on identical bars
        base_evs = _generate_baseline_events(
            candles,
            atr_series,
            timeframe=timeframe,
            params=eff_params,
            seed=1009 + sym_idx * 37,
        )
        for b_name, ev_list in base_evs.items():
            for ev in ev_list:
                out = _evaluate_forward_outcome(ev, candles, atr_series, prim_h)
                if out is not None:
                    baseline_outcomes[b_name].append(out)

    # Headline KPI summary (primary horizon)
    overall = _aggregate_outcomes(primary_outcomes)
    bounce_agg = _aggregate_outcomes(by_event.get("SUPPORT_BOUNCE", []))
    rej_agg = _aggregate_outcomes(by_event.get("RESISTANCE_REJECTION", []))
    bo_rows = by_event.get("RESISTANCE_BREAKOUT", []) + by_event.get("SUPPORT_BREAKDOWN", [])
    bo_agg = _aggregate_outcomes(bo_rows)
    retest_rows = by_event.get("BREAKOUT_RETEST", []) + by_event.get("BREAKDOWN_RETEST", [])
    retest_agg = _aggregate_outcomes(retest_rows)
    false_bo_count = sum(1 for r in bo_rows if r.get("false_breakout"))
    false_bo_rate = round((false_bo_count / len(bo_rows)) * 100.0, 2) if bo_rows else 0.0

    # Baseline comparison table with explicit lift vs full S&R engine
    full_agg = _aggregate_outcomes(baseline_outcomes["full_sr_engine"])
    baseline_labels = {
        "full_sr_engine": "Full S&R Engine (ATR Clustered + 7-Weight Scored)",
        "unscored_atr_clusters": "ATR-Clustered Zones (No Strength Filter)",
        "unclustered_pivots": "Unclustered Confirmed Fractal Pivots",
        "rolling_high_low": "Simple 20-Bar Rolling High / Low",
        "random_levels": "Random Comparable Price Levels (Seeded)",
    }
    baselines_table: List[Dict[str, Any]] = []
    for key in (
        "full_sr_engine",
        "unscored_atr_clusters",
        "unclustered_pivots",
        "rolling_high_low",
        "random_levels",
    ):
        agg = _aggregate_outcomes(baseline_outcomes[key])
        lift_pp = round(full_agg["success_rate_pct"] - agg["success_rate_pct"], 2) if key != "full_sr_engine" else 0.0
        edge_lift = round(full_agg["edge_ratio"] - agg["edge_ratio"], 3) if key != "full_sr_engine" else 0.0
        baselines_table.append(
            {
                "id": key,
                "label": baseline_labels[key],
                **agg,
                "lift_vs_baseline_pp": lift_pp,
                "edge_ratio_lift": edge_lift,
            }
        )

    return {
        "ok": True,
        "timeframe": tf_short,
        "canonical_timeframe": timeframe,
        "primary_horizon_bars": prim_h,
        "horizons": clean_horizons,
        "htf_confluence": bool(htf_confluence),
        "weights": weights,
        "symbols_evaluated": symbols_evaluated,
        "symbol_count": len(symbols_evaluated),
        "total_bars_analyzed": total_bars,
        "summary": {
            "total_events": overall["count"],
            "overall_success_rate_pct": overall["success_rate_pct"],
            "bounce_success_rate_pct": bounce_agg["success_rate_pct"],
            "bounce_count": bounce_agg["count"],
            "rejection_success_rate_pct": rej_agg["success_rate_pct"],
            "rejection_count": rej_agg["count"],
            "breakout_success_rate_pct": bo_agg["success_rate_pct"],
            "breakout_count": bo_agg["count"],
            "retest_success_rate_pct": retest_agg["success_rate_pct"],
            "retest_count": retest_agg["count"],
            "false_breakout_rate_pct": false_bo_rate,
            "avg_mfe_atr": overall["avg_mfe_atr"],
            "avg_mae_atr": overall["avg_mae_atr"],
            "avg_mfe_pct": overall["avg_mfe_pct"],
            "avg_mae_pct": overall["avg_mae_pct"],
            "edge_ratio": overall["edge_ratio"],
            "avg_reaction_bars": overall["avg_reaction_bars"],
        },
        "by_event": [
            {"event": ev_k, **_aggregate_outcomes(ev_rows)}
            for ev_k, ev_rows in sorted(by_event.items(), key=lambda kv: -len(kv[1]))
        ],
        "by_strength_tier": [
            {"tier": tier, **_aggregate_outcomes(by_tier.get(tier, []))}
            for tier in ("Major", "Minor", "Weak")
        ],
        "by_regime": [
            {"regime": reg, **_aggregate_outcomes(by_regime.get(reg, []))}
            for reg in ("Trending", "Sideways", "Volatile")
        ],
        "by_horizon": [
            {"horizon_bars": h, **_aggregate_outcomes(by_horizon.get(h, []))}
            for h in clean_horizons
        ],
        "baselines": baselines_table,
    }


__all__ = [
    "evaluate_sr_validation",
]
