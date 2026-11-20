"""
options/payoff.py
=================
Zero-Dependency Options Strategy Builder, Black-Scholes Greeks & Payoff Engine.

Features:
- Analytical Black-Scholes pricing & Greeks (`Delta`, `Gamma`, `Theta`, `Vega`)
  using Python stdlib `math.erf` (no SciPy required).
- Multi-leg builder supporting absolute strikes or dynamic ATM offsets
  (`ATM`, `ATM+200`, `ATM-200`), CE/PE, BUY/SELL, custom lot sizes & IV.
- Computes Expiry Payoff curve, T+0 (mark-to-market) Payoff curve, exact
  breakeven prices, Max Profit, Max Loss, Net Credit/Debit, estimated span
  margin, and real 2026 Indian options charges (via `charges.calculator`).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from charges.calculator import charges_engine


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(
    *,
    spot: float,
    strike: float,
    days_to_expiry: float,
    iv_pct: float = 15.0,
    rate_pct: float = 6.5,
    option_type: str = "CE",
) -> Dict[str, float]:
    """Compute theoretical option price and Greeks (`delta`, `gamma`, `theta`/day, `vega`/1% IV)."""
    s = max(0.01, float(spot))
    k = max(0.01, float(strike))
    t = max(1e-6, float(days_to_expiry) / 365.0)
    sigma = max(0.005, float(iv_pct) / 100.0)
    r = float(rate_pct) / 100.0
    is_call = (option_type or "CE").strip().upper() in {"CE", "CALL", "C"}

    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    pdf_d1 = _norm_pdf(d1)
    disc = math.exp(-r * t)

    if is_call:
        price = s * nd1 - k * disc * nd2
        delta = nd1
        theta_annual = -(s * pdf_d1 * sigma) / (2.0 * sqrt_t) - r * k * disc * nd2
    else:
        price = k * disc * _norm_cdf(-d2) - s * _norm_cdf(-d1)
        delta = nd1 - 1.0
        theta_annual = -(s * pdf_d1 * sigma) / (2.0 * sqrt_t) + r * k * disc * _norm_cdf(-d2)

    gamma = pdf_d1 / (s * sigma * sqrt_t)
    vega = (s * pdf_d1 * sqrt_t) / 100.0  # per 1% IV move
    theta_day = theta_annual / 365.0

    return {
        "price": round(max(0.0, price), 2),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta_day, 2),
        "vega": round(vega, 2),
    }


@dataclass
class OptionLeg:
    option_type: str  # "CE" or "PE"
    action: str       # "BUY" or "SELL"
    strike: float
    lots: int = 1
    lot_size: int = 75
    premium: Optional[float] = None
    iv_pct: float = 15.0
    expiry: str = ""
    trading_symbol: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


OPTIONS_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "iron_condor": {
        "id": "iron_condor",
        "name": "Iron Condor (Defined Risk)",
        "description": "Sell OTM Call & Put and buy further OTM protection wings.",
        "legs": [
            {"option_type": "CE", "action": "SELL", "offset_steps": 2, "lots": 1},
            {"option_type": "CE", "action": "BUY", "offset_steps": 4, "lots": 1},
            {"option_type": "PE", "action": "SELL", "offset_steps": -2, "lots": 1},
            {"option_type": "PE", "action": "BUY", "offset_steps": -4, "lots": 1},
        ],
    },
    "short_straddle": {
        "id": "short_straddle",
        "name": "Short Straddle (ATM Theta)",
        "description": "Sell ATM Call and ATM Put to harvest time decay on rangebound sessions.",
        "legs": [
            {"option_type": "CE", "action": "SELL", "offset_steps": 0, "lots": 1},
            {"option_type": "PE", "action": "SELL", "offset_steps": 0, "lots": 1},
        ],
    },
    "short_strangle": {
        "id": "short_strangle",
        "name": "Short Strangle (Wide Range)",
        "description": "Sell OTM Call (+2 steps) and OTM Put (-2 steps) for wider breakevens.",
        "legs": [
            {"option_type": "CE", "action": "SELL", "offset_steps": 2, "lots": 1},
            {"option_type": "PE", "action": "SELL", "offset_steps": -2, "lots": 1},
        ],
    },
    "bull_call_spread": {
        "id": "bull_call_spread",
        "name": "Bull Call Spread",
        "description": "Buy ATM Call and sell OTM Call (+2 steps) for capped directional upside.",
        "legs": [
            {"option_type": "CE", "action": "BUY", "offset_steps": 0, "lots": 1},
            {"option_type": "CE", "action": "SELL", "offset_steps": 2, "lots": 1},
        ],
    },
    "bear_put_spread": {
        "id": "bear_put_spread",
        "name": "Bear Put Spread",
        "description": "Buy ATM Put and sell OTM Put (-2 steps) for defined-risk bearish view.",
        "legs": [
            {"option_type": "PE", "action": "BUY", "offset_steps": 0, "lots": 1},
            {"option_type": "PE", "action": "SELL", "offset_steps": -2, "lots": 1},
        ],
    },
    "long_straddle": {
        "id": "long_straddle",
        "name": "Long Straddle (Volatility Breakout)",
        "description": "Buy ATM Call and ATM Put ahead of event volatility or breakout moves.",
        "legs": [
            {"option_type": "CE", "action": "BUY", "offset_steps": 0, "lots": 1},
            {"option_type": "PE", "action": "BUY", "offset_steps": 0, "lots": 1},
        ],
    },
}


def build_template_legs(
    template_id: str,
    *,
    spot: float,
    strike_step: Optional[float] = None,
    step: Optional[float] = None,
    lot_size: int = 75,
    days_to_expiry: Optional[float] = None,
    dte_days: Optional[float] = None,
    iv_pct: float = 15.0,
) -> List[Dict[str, Any]]:
    tpl = OPTIONS_TEMPLATES.get((template_id or "").strip().lower())
    if tpl is None:
        raise ValueError(f"Unknown options template '{template_id}'.")
    eff_step = max(1.0, float(step if step is not None else (strike_step if strike_step is not None else 50.0)))
    eff_dte = max(0.25, float(dte_days if dte_days is not None else (days_to_expiry if days_to_expiry is not None else 7.0)))
    atm = round(float(spot) / eff_step) * eff_step
    legs: List[Dict[str, Any]] = []
    for raw in tpl["legs"]:
        strike = atm + int(raw["offset_steps"]) * eff_step
        g = black_scholes_greeks(
            spot=spot,
            strike=strike,
            days_to_expiry=eff_dte,
            iv_pct=iv_pct,
            option_type=raw["option_type"],
        )
        side = str(raw["action"]).upper()
        legs.append(
            {
                "option_type": raw["option_type"],
                "side": side,
                "action": side,
                "strike": round(strike, 2),
                "lots": int(raw.get("lots", 1)),
                "lot_size": int(lot_size),
                "premium": g["price"],
                "iv_pct": iv_pct,
            }
        )
    return legs


_TEMPLATE_OUTLOOK = {
    "iron_condor": "Neutral / Rangebound",
    "short_straddle": "Neutral / High Theta",
    "short_strangle": "Neutral / Wide Range",
    "bull_call_spread": "Moderately Bullish",
    "bear_put_spread": "Moderately Bearish",
    "long_straddle": "High Volatility / Breakout",
}


def list_options_templates() -> List[Dict[str, Any]]:
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "outlook": _TEMPLATE_OUTLOOK.get(t["id"], "Multi-Leg"),
            "description": t["description"],
            "leg_count": len(t["legs"]),
        }
        for t in OPTIONS_TEMPLATES.values()
    ]


def analyze_options_strategy(
    *,
    spot: float,
    legs: List[Dict[str, Any]],
    underlying: str = "NIFTY",
    days_to_expiry: Optional[float] = None,
    dte_days: Optional[float] = None,
    rate_pct: Optional[float] = None,
    risk_free_rate: Optional[float] = None,
    charges_profile: str = "zerodha",
    charges_overrides: Optional[Dict[str, Any]] = None,
    range_pct: float = 8.0,
    points: int = 81,
) -> Dict[str, Any]:
    """
    Compute full multi-leg options payoff curve (Expiry + T+0), breakevens,
    net Greeks, estimated margin, and real Indian options trading charges.
    """
    from charges.calculator import ChargeBreakdown

    s0 = max(1.0, float(spot))
    dte = max(0.25, float(dte_days if dte_days is not None else (days_to_expiry if days_to_expiry is not None else 7.0)))
    if rate_pct is not None:
        eff_rate_pct = float(rate_pct)
    elif risk_free_rate is not None:
        r_val = float(risk_free_rate)
        eff_rate_pct = r_val * 100.0 if r_val < 1.0 else r_val
    else:
        eff_rate_pct = 6.5

    if not legs:
        raise ValueError("At least one option leg is required.")

    cfg = charges_engine.resolve_config(
        profile=charges_profile,
        segment="equity_options",
        overrides=charges_overrides,
    )

    normalized_legs: List[Dict[str, Any]] = []
    net_delta = net_gamma = net_theta = net_vega = 0.0
    net_credit = 0.0
    total_charges_bd = ChargeBreakdown()
    sell_notional = 0.0
    buy_debit = 0.0

    for raw in legs:
        otype = str(raw.get("option_type") or "CE").strip().upper()
        action = str(raw.get("side") or raw.get("action") or "BUY").strip().upper()
        strike = float(raw.get("strike") or s0)
        lots = max(1, int(raw.get("lots") or 1))
        lot_size = max(1, int(raw.get("lot_size") or 75))
        qty = lots * lot_size
        iv_pct = max(1.0, float(raw.get("iv_pct") or 15.0))

        g = black_scholes_greeks(
            spot=s0,
            strike=strike,
            days_to_expiry=dte,
            iv_pct=iv_pct,
            rate_pct=eff_rate_pct,
            option_type=otype,
        )
        prem = float(raw["premium"]) if raw.get("premium") not in (None, "") else g["price"]
        sign = 1.0 if action == "BUY" else -1.0

        net_delta += sign * g["delta"] * qty
        net_gamma += sign * g["gamma"] * qty
        net_theta += sign * g["theta"] * qty
        net_vega += sign * g["vega"] * qty
        net_credit += (-sign) * prem * qty

        if action == "SELL":
            sell_notional += s0 * qty
        else:
            buy_debit += prem * qty

        leg_chg = charges_engine.calculate_leg(side=action, price=prem, quantity=qty, cfg=cfg, include_slippage=True)
        total_charges_bd.add(leg_chg)

        normalized_legs.append(
            {
                "option_type": otype,
                "side": action,
                "action": action,
                "strike": round(strike, 2),
                "lots": lots,
                "lot_size": lot_size,
                "qty": qty,
                "premium": round(prem, 2),
                "bs_price": round(g["price"], 2),
                "iv_pct": round(iv_pct, 2),
                "expiry": str(raw.get("expiry") or ""),
                "trading_symbol": str(raw.get("trading_symbol") or f"{underlying.upper()} {int(strike)} {otype}"),
                "delta": round(sign * g["delta"], 4),
                "gamma": round(sign * g["gamma"], 6),
                "theta": round(sign * g["theta"], 2),
                "vega": round(sign * g["vega"], 2),
                "greeks": g,
                "entry_charges": round(leg_chg.total_charges, 2),
            }
        )

    total_entry_charges = total_charges_bd.total_charges

    strikes = [l["strike"] for l in normalized_legs]
    span = max(s0 * (float(range_pct or 8.0) / 100.0), (max(strikes) - min(strikes)) * 1.35, s0 * 0.04)
    lo = max(1.0, min(s0, min(strikes)) - span * 0.5)
    hi = max(s0, max(strikes)) + span * 0.5
    n_pts = max(25, min(161, int(points or 81)))

    payoff_curve: List[Dict[str, float]] = []
    for i in range(n_pts):
        px = lo + (hi - lo) * (i / float(n_pts - 1))
        exp_pnl = 0.0
        t0_pnl = 0.0
        for l in normalized_legs:
            sign = 1.0 if l["action"] == "BUY" else -1.0
            intrinsic = max(0.0, px - l["strike"]) if l["option_type"] == "CE" else max(0.0, l["strike"] - px)
            exp_pnl += sign * (intrinsic - l["premium"]) * l["qty"]
            now_g = black_scholes_greeks(
                spot=px,
                strike=l["strike"],
                days_to_expiry=dte,
                iv_pct=l["iv_pct"],
                rate_pct=eff_rate_pct,
                option_type=l["option_type"],
            )
            t0_pnl += sign * (now_g["price"] - l["premium"]) * l["qty"]
        payoff_curve.append(
            {
                "spot": round(px, 2),
                "expiry_pnl": round(exp_pnl - total_entry_charges, 2),
                "expiry_gross_pnl": round(exp_pnl, 2),
                "t0_pnl": round(t0_pnl - total_entry_charges, 2),
            }
        )

    # Detect exact breakeven crossings on net expiry curve
    breakevens: List[float] = []
    for i in range(1, len(payoff_curve)):
        y1 = payoff_curve[i - 1]["expiry_pnl"]
        y2 = payoff_curve[i]["expiry_pnl"]
        if y1 == 0:
            breakevens.append(payoff_curve[i - 1]["spot"])
        elif (y1 < 0 and y2 > 0) or (y1 > 0 and y2 < 0):
            x1, x2 = payoff_curve[i - 1]["spot"], payoff_curve[i]["spot"]
            frac = abs(y1) / (abs(y1) + abs(y2))
            breakevens.append(round(x1 + frac * (x2 - x1), 2))

    exp_vals = [p["expiry_pnl"] for p in payoff_curve]
    max_profit = max(exp_vals)
    max_loss = min(exp_vals)

    # Check tail slopes to identify theoretically unlimited profit/loss
    right_slope = payoff_curve[-1]["expiry_pnl"] - payoff_curve[-2]["expiry_pnl"]
    unlimited_profit = right_slope > 1e-3
    unlimited_loss = right_slope < -1e-3

    # Practical NSE SPAN+Exposure hedge margin estimate
    if sell_notional > 0:
        est_margin = max(abs(max_loss), sell_notional * (0.045 if not unlimited_loss else 0.11)) + buy_debit
    else:
        est_margin = buy_debit

    risk_reward = (
        round(abs(max_profit / max_loss), 2)
        if (not unlimited_profit and not unlimited_loss and max_loss < 0)
        else None
    )
    net_greeks = {
        "delta": round(net_delta, 2),
        "gamma": round(net_gamma, 4),
        "theta": round(net_theta, 2),
        "vega": round(net_vega, 2),
    }

    return {
        "ok": True,
        "underlying": underlying.strip().upper(),
        "spot": round(s0, 2),
        "days_to_expiry": round(dte, 2),
        "dte_days": round(dte, 2),
        "net_premium": round(net_credit, 2),
        "net_premium_label": "Net Credit" if net_credit >= 0 else "Net Debit",
        "max_profit": round(max_profit, 2),
        "max_profit_unlimited": unlimited_profit,
        "max_loss": round(max_loss, 2),
        "max_loss_unlimited": unlimited_loss,
        "risk_reward": risk_reward,
        "breakevens": breakevens[:4],
        "estimated_margin_inr": round(est_margin, 2),
        "charges": total_charges_bd.to_dict(),
        "net_greeks": net_greeks,
        "legs": normalized_legs,
        "summary": {
            "net_credit": round(net_credit, 2),
            "entry_charges": round(total_entry_charges, 2),
            "max_profit": None if unlimited_profit else round(max_profit, 2),
            "max_loss": None if unlimited_loss else round(max_loss, 2),
            "unlimited_profit": unlimited_profit,
            "unlimited_loss": unlimited_loss,
            "breakevens": breakevens[:4],
            "estimated_margin": round(est_margin, 2),
            "risk_reward": risk_reward,
        },
        "greeks": net_greeks,
        "payoff_curve": payoff_curve,
    }


__all__ = [
    "OPTIONS_TEMPLATES",
    "OptionLeg",
    "analyze_options_strategy",
    "black_scholes_greeks",
    "build_template_legs",
    "list_options_templates",
]
