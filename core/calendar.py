"""
core/calendar.py
================
Multi-exchange trading schedules and verified exchange holiday calendars
for Indian markets (NSE, BSE, NFO, BFO, MCX).

Used by:
- `core/preflight.py`: Exchange-aware market hours & segment margin selection.
- `core/historical.py`: Distinguishing confirmed `EXCHANGE_HOLIDAY` dates from
  retryable `PROVIDER_MISSING` gaps in the SQLite Asset Data History catalog.
- `backtest/engine.py` & `backtest/portfolio.py`: Exact bars-per-day calculation
  for intraday Sharpe annualization across NSE/BSE (375 min) and MCX (870 min).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, Optional, Set

from config import IST_TZ, config


@dataclass(frozen=True)
class ExchangeSchedule:
    exchange: str
    open_time: time
    close_time: time
    segment: str  # "equity" | "commodity"
    upstox_margin_segment: str  # "SEC" | "COM"
    zerodha_margin_segment: str  # "equity" | "commodity"
    session_minutes: int


EXCHANGE_SCHEDULES: Dict[str, ExchangeSchedule] = {
    "NSE": ExchangeSchedule("NSE", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "BSE": ExchangeSchedule("BSE", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "NFO": ExchangeSchedule("NFO", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "BFO": ExchangeSchedule("BFO", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "NSE_EQ": ExchangeSchedule("NSE_EQ", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "BSE_EQ": ExchangeSchedule("BSE_EQ", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "NSE_FO": ExchangeSchedule("NSE_FO", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "BSE_FO": ExchangeSchedule("BSE_FO", time(9, 15), time(15, 30), "equity", "SEC", "equity", 375),
    "MCX": ExchangeSchedule("MCX", time(9, 0), time(23, 30), "commodity", "COM", "commodity", 870),
    "MCX_FO": ExchangeSchedule("MCX_FO", time(9, 0), time(23, 30), "commodity", "COM", "commodity", 870),
}

# Verified Indian Equity (NSE/BSE) Exchange Holidays (2024–2027)
_EQUITY_HOLIDAYS: Set[str] = {
    # 2024
    "2024-01-22", "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29",
    "2024-04-11", "2024-04-17", "2024-05-01", "2024-05-20", "2024-06-17",
    "2024-07-17", "2024-08-15", "2024-10-02", "2024-11-01", "2024-11-15",
    "2024-11-20", "2024-12-25",
    # 2025
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
    "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
    "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
    # 2026
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14",
    "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
    # 2027
    "2027-01-26", "2027-03-22", "2027-03-26", "2027-04-14", "2027-04-15",
    "2027-08-15", "2027-10-02", "2027-10-29", "2027-12-25",
}

# Full-day MCX Commodity Holidays (MCX remains open in evening on many equity holidays)
_MCX_FULL_HOLIDAYS: Set[str] = {
    "2024-01-26", "2024-03-29", "2024-08-15", "2024-10-02", "2024-12-25",
    "2025-01-26", "2025-04-18", "2025-08-15", "2025-10-02", "2025-12-25",
    "2026-01-26", "2026-04-03", "2026-08-15", "2026-10-02", "2026-12-25",
    "2027-01-26", "2027-03-26", "2027-10-02", "2027-12-25",
}


def normalize_exchange(exchange: Optional[str]) -> str:
    clean = (exchange or "").strip().upper()
    return clean or "NSE"


def get_exchange_schedule(exchange: Optional[str]) -> ExchangeSchedule:
    ex = normalize_exchange(exchange)
    return EXCHANGE_SCHEDULES.get(ex, EXCHANGE_SCHEDULES["NSE"])


def _load_custom_holidays(exchange: str, custom_file: Optional[Path] = None) -> Set[str]:
    path = custom_file or (config.DATA_DIR / "exchange_holidays.json")
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            vals = raw.get(exchange.upper()) or raw.get("ALL") or []
            return {str(v)[:10] for v in vals if v}
    except Exception:
        pass
    return set()


def get_exchange_holidays(exchange: Optional[str] = "NSE", custom_file: Optional[Path] = None) -> Set[str]:
    ex = normalize_exchange(exchange)
    base = set(_MCX_FULL_HOLIDAYS) if ex in {"MCX", "MCX_FO"} else set(_EQUITY_HOLIDAYS)
    base.update(_load_custom_holidays(ex, custom_file=custom_file))
    return base


def is_exchange_holiday(target_date: date | str, exchange: Optional[str] = "NSE") -> bool:
    d_iso = target_date.isoformat() if isinstance(target_date, date) else str(target_date)[:10]
    return d_iso in get_exchange_holidays(exchange)


def is_trading_day(target_date: date | str, exchange: Optional[str] = "NSE") -> bool:
    d_obj = target_date if isinstance(target_date, date) else date.fromisoformat(str(target_date)[:10])
    if d_obj.weekday() >= 5:
        return False
    return not is_exchange_holiday(d_obj, exchange=exchange)


def is_market_open(now_ist: Optional[datetime] = None, exchange: Optional[str] = "NSE") -> bool:
    ref = now_ist.astimezone(IST_TZ) if now_ist else datetime.now(tz=IST_TZ)
    if not is_trading_day(ref.date(), exchange=exchange):
        return False
    sched = get_exchange_schedule(exchange)
    t_now = ref.time()
    return sched.open_time <= t_now <= sched.close_time


def session_minutes(exchange: Optional[str] = "NSE") -> int:
    return get_exchange_schedule(exchange).session_minutes


__all__ = [
    "EXCHANGE_SCHEDULES",
    "ExchangeSchedule",
    "get_exchange_holidays",
    "get_exchange_schedule",
    "is_exchange_holiday",
    "is_market_open",
    "is_trading_day",
    "normalize_exchange",
    "session_minutes",
]
