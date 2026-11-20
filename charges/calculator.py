"""
charges/calculator.py
=====================
Authentic Indian Equity & F&O Trading Charges Engine (NSE / BSE, April 2026 rules).

Models exact broker + statutory deductions for:
- Zerodha (Equity Delivery, Equity Intraday, Equity Futures, Equity Options)
- Upstox  (Equity Delivery, Equity Intraday, Equity Futures, Equity Options)
- Zero Cost (benchmark gross-only mode)
- Custom user overrides (brokerage cap/pct, STT, exchange txn, SEBI, GST,
  stamp duty, delivery DP charges, and slippage bps).

Shared identically by Individual Backtest, Portfolio Backtest, Paper Trade,
and Live Deployment cost attribution so net P&L is consistent end-to-end.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


VALID_SEGMENTS = (
    "equity_delivery",
    "equity_intraday",
    "equity_futures",
    "equity_options",
)

SEGMENT_LABELS = {
    "equity_delivery": "Equity Delivery (CNC)",
    "equity_intraday": "Equity Intraday (MIS)",
    "equity_futures": "Equity Futures (NFO)",
    "equity_options": "Equity Options (NFO)",
}


@dataclass(frozen=True)
class ChargesConfig:
    profile: str = "zerodha"
    segment: str = "equity_intraday"
    exchange: str = "NSE"
    # Brokerage: either flat per order or min(cap, pct * turnover)
    brokerage_flat_per_order: float = 20.0
    brokerage_pct: float = 0.0003  # 0.03% (set 0.0 for flat-only or free delivery)
    brokerage_is_flat_only: bool = False
    # Statutory rates (decimal fractions of turnover)
    stt_buy_pct: float = 0.0
    stt_sell_pct: float = 0.00025  # 0.025% equity intraday sell
    exchange_txn_pct: float = 0.0000307  # ~0.00307% NSE cash
    sebi_pct: float = 0.000001  # ₹10 per crore = 0.0001%
    gst_pct: float = 0.18  # 18% strictly on (brokerage + exchange_txn + sebi)
    stamp_duty_buy_pct: float = 0.00003  # 0.003% buy-side only
    dp_charge_per_sell_scrip: float = 0.0  # Delivery sell DP fee (incl. GST or base)
    slippage_bps: float = 5.0  # Configurable execution slippage in bps per leg

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Built-in 2026 schedules reflecting current Zerodha & Upstox + revised April 2026 STT
PRESET_SCHEDULES: Dict[str, Dict[str, ChargesConfig]] = {
    "zerodha": {
        "equity_delivery": ChargesConfig(
            profile="zerodha",
            segment="equity_delivery",
            brokerage_flat_per_order=0.0,
            brokerage_pct=0.0,
            brokerage_is_flat_only=True,
            stt_buy_pct=0.001,        # 0.1% both sides
            stt_sell_pct=0.001,
            exchange_txn_pct=0.0000307,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00015,  # 0.015% buy-side
            dp_charge_per_sell_scrip=15.34,  # ₹13 + GST per scrip sold
            slippage_bps=5.0,
        ),
        "equity_intraday": ChargesConfig(
            profile="zerodha",
            segment="equity_intraday",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.0003,     # 0.03% or ₹20 whichever is lower
            brokerage_is_flat_only=False,
            stt_buy_pct=0.0,
            stt_sell_pct=0.00025,     # 0.025% sell-side
            exchange_txn_pct=0.0000307,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00003,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=5.0,
        ),
        "equity_futures": ChargesConfig(
            profile="zerodha",
            segment="equity_futures",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.0003,
            brokerage_is_flat_only=False,
            stt_buy_pct=0.0,
            stt_sell_pct=0.0005,      # 0.05% sell-side (revised April 2026)
            exchange_txn_pct=0.0000188,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00002,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=4.0,
        ),
        "equity_options": ChargesConfig(
            profile="zerodha",
            segment="equity_options",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.0,
            brokerage_is_flat_only=True,  # Flat ₹20 per order
            stt_buy_pct=0.0,
            stt_sell_pct=0.0015,      # 0.15% sell-side on premium (revised April 2026)
            exchange_txn_pct=0.0003503,  # NSE options premium txn charge
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00003,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=10.0,
        ),
    },
    "upstox": {
        "equity_delivery": ChargesConfig(
            profile="upstox",
            segment="equity_delivery",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.025,      # Up to ₹20 or 2.5% whichever is lower
            brokerage_is_flat_only=False,
            stt_buy_pct=0.001,
            stt_sell_pct=0.001,
            exchange_txn_pct=0.0000307,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00015,
            dp_charge_per_sell_scrip=18.5,
            slippage_bps=5.0,
        ),
        "equity_intraday": ChargesConfig(
            profile="upstox",
            segment="equity_intraday",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.0005,     # 0.05% or ₹20 whichever is lower
            brokerage_is_flat_only=False,
            stt_buy_pct=0.0,
            stt_sell_pct=0.00025,
            exchange_txn_pct=0.0000307,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00003,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=5.0,
        ),
        "equity_futures": ChargesConfig(
            profile="upstox",
            segment="equity_futures",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.0005,
            brokerage_is_flat_only=False,
            stt_buy_pct=0.0,
            stt_sell_pct=0.0005,      # 0.05% sell-side (revised April 2026)
            exchange_txn_pct=0.0000188,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00002,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=4.0,
        ),
        "equity_options": ChargesConfig(
            profile="upstox",
            segment="equity_options",
            brokerage_flat_per_order=20.0,
            brokerage_pct=0.0,
            brokerage_is_flat_only=True,
            stt_buy_pct=0.0,
            stt_sell_pct=0.0015,      # 0.15% sell-side on premium (revised April 2026)
            exchange_txn_pct=0.0003503,
            sebi_pct=0.000001,
            gst_pct=0.18,
            stamp_duty_buy_pct=0.00003,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=10.0,
        ),
    },
    "zero": {
        seg: ChargesConfig(
            profile="zero",
            segment=seg,
            brokerage_flat_per_order=0.0,
            brokerage_pct=0.0,
            brokerage_is_flat_only=True,
            stt_buy_pct=0.0,
            stt_sell_pct=0.0,
            exchange_txn_pct=0.0,
            sebi_pct=0.0,
            gst_pct=0.0,
            stamp_duty_buy_pct=0.0,
            dp_charge_per_sell_scrip=0.0,
            slippage_bps=0.0,
        )
        for seg in VALID_SEGMENTS
    },
}


@dataclass
class ChargeBreakdown:
    turnover: float = 0.0
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fee: float = 0.0
    gst: float = 0.0
    stamp_duty: float = 0.0
    dp_charges: float = 0.0
    slippage_cost: float = 0.0

    @property
    def statutory_and_brokerage(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi_fee
            + self.gst
            + self.stamp_duty
            + self.dp_charges
        )

    @property
    def total_charges(self) -> float:
        return self.statutory_and_brokerage + self.slippage_cost

    def add(self, other: "ChargeBreakdown") -> None:
        self.turnover += other.turnover
        self.brokerage += other.brokerage
        self.stt += other.stt
        self.exchange_txn += other.exchange_txn
        self.sebi_fee += other.sebi_fee
        self.gst += other.gst
        self.stamp_duty += other.stamp_duty
        self.dp_charges += other.dp_charges
        self.slippage_cost += other.slippage_cost

    def to_dict(self) -> Dict[str, float]:
        return {
            "turnover": round(self.turnover, 2),
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_txn": round(self.exchange_txn, 2),
            "sebi_fee": round(self.sebi_fee, 2),
            "gst": round(self.gst, 2),
            "stamp_duty": round(self.stamp_duty, 2),
            "dp_charges": round(self.dp_charges, 2),
            "slippage_cost": round(self.slippage_cost, 2),
            "statutory_and_brokerage": round(self.statutory_and_brokerage, 2),
            "total_charges": round(self.total_charges, 2),
        }


class ChargesEngine:
    """Stateless, zero-allocation Indian brokerage & statutory charges calculator."""

    def resolve_config(
        self,
        *,
        profile: str = "zerodha",
        segment: str = "equity_intraday",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> ChargesConfig:
        prof = (profile or "zerodha").strip().lower()
        seg = (segment or "equity_intraday").strip().lower()
        if seg not in VALID_SEGMENTS:
            seg = "equity_intraday"
        base_map = PRESET_SCHEDULES.get(prof) or PRESET_SCHEDULES["zerodha"]
        base = base_map[seg]
        if not overrides:
            return base
        data = base.to_dict()
        data["profile"] = prof if prof in PRESET_SCHEDULES else "custom"
        data["segment"] = seg
        for key in (
            "brokerage_flat_per_order",
            "brokerage_pct",
            "stt_buy_pct",
            "stt_sell_pct",
            "exchange_txn_pct",
            "sebi_pct",
            "gst_pct",
            "stamp_duty_buy_pct",
            "dp_charge_per_sell_scrip",
            "slippage_bps",
        ):
            if key in overrides and overrides[key] is not None:
                data[key] = max(0.0, float(overrides[key]))
        if "brokerage_is_flat_only" in overrides and overrides["brokerage_is_flat_only"] is not None:
            data["brokerage_is_flat_only"] = bool(overrides["brokerage_is_flat_only"])
        if "exchange" in overrides and overrides["exchange"]:
            data["exchange"] = str(overrides["exchange"]).strip().upper()
        return ChargesConfig(**data)

    def _leg_brokerage(self, turnover: float, cfg: ChargesConfig) -> float:
        if turnover <= 0:
            return 0.0
        if cfg.brokerage_is_flat_only:
            return float(cfg.brokerage_flat_per_order)
        if cfg.brokerage_pct <= 0:
            return float(cfg.brokerage_flat_per_order)
        pct_brok = turnover * cfg.brokerage_pct
        if cfg.brokerage_flat_per_order > 0:
            return min(float(cfg.brokerage_flat_per_order), pct_brok)
        return pct_brok

    def calculate_leg(
        self,
        *,
        side: str,
        price: float,
        quantity: int,
        cfg: ChargesConfig,
        include_slippage: bool = True,
    ) -> ChargeBreakdown:
        qty = max(0, int(quantity))
        px = max(0.0, float(price))
        turnover = px * qty
        if turnover <= 0:
            return ChargeBreakdown()

        is_buy = (side or "BUY").strip().upper() == "BUY"
        brokerage = self._leg_brokerage(turnover, cfg)
        stt = turnover * (cfg.stt_buy_pct if is_buy else cfg.stt_sell_pct)
        exchange_txn = turnover * cfg.exchange_txn_pct
        sebi_fee = turnover * cfg.sebi_pct
        gst = (brokerage + exchange_txn + sebi_fee) * cfg.gst_pct
        stamp_duty = turnover * cfg.stamp_duty_buy_pct if is_buy else 0.0
        dp_charges = cfg.dp_charge_per_sell_scrip if (not is_buy and cfg.dp_charge_per_sell_scrip > 0) else 0.0
        slippage = turnover * (cfg.slippage_bps / 10000.0) if (include_slippage and cfg.slippage_bps > 0) else 0.0

        return ChargeBreakdown(
            turnover=turnover,
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exchange_txn,
            sebi_fee=sebi_fee,
            gst=gst,
            stamp_duty=stamp_duty,
            dp_charges=dp_charges,
            slippage_cost=slippage,
        )

    def calculate_round_trip(
        self,
        *,
        entry_price: float,
        exit_price: float,
        quantity: int,
        cfg: ChargesConfig,
        direction: str = "LONG",
        include_slippage: bool = True,
    ) -> Dict[str, Any]:
        is_long = (direction or "LONG").strip().upper() != "SHORT"
        buy_px = entry_price if is_long else exit_price
        sell_px = exit_price if is_long else entry_price
        buy_leg = self.calculate_leg(side="BUY", price=buy_px, quantity=quantity, cfg=cfg, include_slippage=include_slippage)
        sell_leg = self.calculate_leg(side="SELL", price=sell_px, quantity=quantity, cfg=cfg, include_slippage=include_slippage)
        combined = ChargeBreakdown()
        combined.add(buy_leg)
        combined.add(sell_leg)

        gross_pnl = (sell_px - buy_px) * max(0, int(quantity))
        net_pnl = gross_pnl - combined.total_charges
        breakeven_pts = (combined.total_charges / max(1, int(quantity))) if quantity > 0 else 0.0

        return {
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "breakeven_points": round(breakeven_pts, 4),
            "charges": combined.to_dict(),
        }


def list_charge_presets() -> Dict[str, Any]:
    return {
        "segments": [{"id": s, "label": SEGMENT_LABELS[s]} for s in VALID_SEGMENTS],
        "profiles": {
            prof: {seg: cfg.to_dict() for seg, cfg in seg_map.items()}
            for prof, seg_map in PRESET_SCHEDULES.items()
        },
    }


charges_engine = ChargesEngine()

__all__ = [
    "ChargeBreakdown",
    "ChargesConfig",
    "ChargesEngine",
    "PRESET_SCHEDULES",
    "SEGMENT_LABELS",
    "VALID_SEGMENTS",
    "charges_engine",
    "list_charge_presets",
]
