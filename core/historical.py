"""
core/historical.py
------------------
Phase 3 Gap-Only Historical Data Downloader & SQLite OHLCV Store.

Core Guarantees:
1. Zero Redundant API Calls:
   Maintains a per-(symbol, exchange, timeframe, date) coverage ledger in SQLite.
   Before downloading, `compute_missing_date_ranges()` inspects existing coverage
   and schedules API requests ONLY for absent trading weekdays (skipping both
   already-downloaded dates and weekends/checked market holidays).
2. Snapshot & Full Data Inspection:
   Supports instant `snapshot` previews (summary stats + head/tail sample rows)
   as well as `full` dataset retrieval and CSV export from local SQLite without
   any outbound API calls.
3. Multi-Provider Support (uses active Data Connection):
   - Upstox Historical Candle API
   - Zerodha Kite Historical API
   - Yahoo Finance Chart API (public fallback)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from broker.upstox.auth import auth_manager as upstox_auth
from broker.zerodha.auth import zerodha_auth_manager as zerodha_auth
from config import IST_TZ, config
from core.calendar import is_exchange_holiday
from core.instruments import instrument_index

logger = logging.getLogger(__name__)


def _parquet_engine() -> Optional[Tuple[Any, Any, Any]]:
    """Return (pandas, pyarrow, pyarrow.parquet) if installed & hybrid mode active."""
    if config.OHLCV_STORAGE_MODE == "sqlite":
        return None
    try:
        import pandas as pd  # type: ignore
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore

        return pd, pa, pq
    except ImportError:
        return None


class DefaultParquetIO:
    """Default Parquet I/O backed by pandas + pyarrow (Snappy compression)."""

    def available(self) -> bool:
        return _parquet_engine() is not None

    def read_rows(self, path: Path) -> List[Dict[str, Any]]:
        engine = _parquet_engine()
        if engine is None or not path.exists():
            return []
        _pd, _pa, pq = engine
        df = pq.read_table(path).to_pandas()
        if df.empty:
            return []
        return df.to_dict(orient="records")

    def write_rows(self, path: Path, rows: List[Dict[str, Any]]) -> None:
        engine = _parquet_engine()
        if engine is None:
            raise RuntimeError("Parquet engine (pandas/pyarrow) is not available.")
        pd, pa, pq = engine
        path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows)
        df = df.drop_duplicates(subset=["ts_ist"], keep="last")
        df = df.sort_values("ts_ist").reset_index(drop=True)
        tmp_file = path.with_suffix(".tmp.parquet")
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, tmp_file, compression="snappy")
        os.replace(tmp_file, path)

VALID_TIMEFRAMES: Dict[str, Dict[str, Any]] = {
    "1minute": {
        "label": "1 Min",
        "base": "1minute",
        "minutes": 1,
        "upstox": "1minute",
        "upstox_unit": "minutes",
        "upstox_interval": "1",
        "zerodha": "minute",
        "yahoo": "1m",
        "max_chunk_days": 7,
    },
    "3minute": {"label": "3 Min", "base": "1minute", "minutes": 3},
    "5minute": {"label": "5 Min", "base": "1minute", "minutes": 5},
    "10minute": {"label": "10 Min", "base": "1minute", "minutes": 10},
    "15minute": {"label": "15 Min", "base": "1minute", "minutes": 15},
    "30minute": {"label": "30 Min", "base": "1minute", "minutes": 30},
    "60minute": {"label": "60 Min (1H)", "base": "1minute", "minutes": 60},
    "75minute": {"label": "75 Min", "base": "1minute", "minutes": 75},
    "120minute": {"label": "2 Hour", "base": "1minute", "minutes": 120},
    "1day": {
        "label": "1 Day",
        "base": "1day",
        "minutes": None,
        "upstox": "day",
        "upstox_unit": "days",
        "upstox_interval": "1",
        "zerodha": "day",
        "yahoo": "1d",
        "max_chunk_days": 365,
    },
    "1week": {"label": "1 Week", "base": "1day", "minutes": None},
    "1month": {"label": "1 Month", "base": "1day", "minutes": None},
}

_TIMEFRAME_ALIASES: Dict[str, str] = {
    "1m": "1minute",
    "1min": "1minute",
    "minute": "1minute",
    "3m": "3minute",
    "3min": "3minute",
    "5m": "5minute",
    "5min": "5minute",
    "10m": "10minute",
    "10min": "10minute",
    "15m": "15minute",
    "15min": "15minute",
    "30m": "30minute",
    "30min": "30minute",
    "60m": "60minute",
    "60min": "60minute",
    "1h": "60minute",
    "1hr": "60minute",
    "1hour": "60minute",
    "75m": "75minute",
    "75min": "75minute",
    "120m": "120minute",
    "2h": "120minute",
    "2hour": "120minute",
    "1d": "1day",
    "d": "1day",
    "day": "1day",
    "daily": "1day",
    "1w": "1week",
    "w": "1week",
    "week": "1week",
    "weekly": "1week",
    "1mo": "1month",
    "mo": "1month",
    "month": "1month",
    "monthly": "1month",
}


import re

_TIMEFRAME_PATTERN = re.compile(
    r"^\s*(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks|mo|mon|month|months)\s*$"
)


def parse_timeframe(raw_timeframe: Optional[str]) -> Tuple[int, str]:
    """
    Parse any user timeframe into `(count, canonical_unit)` where `canonical_unit`
    is one of `'minute'`, `'day'`, `'week'`, `'month'`.
    Supports arbitrary positive integers like `1minute`, `2minute`, `7minute`,
    `75minute`, `1day`, `2day`, `5day`, `30day`, as well as shorthand (`5m`, `2d`, `1h`).
    """
    raw = (raw_timeframe or "1day").strip().lower()
    raw = _TIMEFRAME_ALIASES.get(raw, raw)
    m = _TIMEFRAME_PATTERN.match(raw)
    if not m:
        raise ValueError(
            f"Invalid timeframe '{raw_timeframe}'. Use a positive integer with 'minute' or 'day' (e.g. 1minute, 5minute, 1day, 3day)."
        )
    count = int(m.group(1))
    unit_tok = m.group(2)
    if count < 1:
        raise ValueError(f"Timeframe interval must be >= 1, got '{raw_timeframe}'.")

    if unit_tok in {"m", "min", "mins", "minute", "minutes"}:
        if count > 1440:
            raise ValueError(f"Minute timeframe cannot exceed 1440 minutes (got {count}).")
        return count, "minute"
    if unit_tok in {"h", "hr", "hrs", "hour", "hours"}:
        mins = count * 60
        if mins > 1440:
            raise ValueError(f"Hour timeframe cannot exceed 24 hours (got {count}h).")
        return mins, "minute"
    if unit_tok in {"d", "day", "days"}:
        if count > 365:
            raise ValueError(f"Day timeframe cannot exceed 365 days (got {count}).")
        return count, "day"
    if unit_tok in {"w", "wk", "week", "weeks"}:
        if count == 1:
            return 1, "week"
        return count * 7, "day"
    if unit_tok in {"mo", "mon", "month", "months"}:
        if count == 1:
            return 1, "month"
        return count * 30, "day"
    raise ValueError(f"Unsupported timeframe unit in '{raw_timeframe}'.")


def normalize_timeframe(raw_timeframe: Optional[str]) -> str:
    count, unit = parse_timeframe(raw_timeframe)
    return f"{count}{unit}"


def resolve_base_timeframe(raw_timeframe: Optional[str]) -> str:
    """
    Canonical download/storage policy:
    - Any sub-day timeframe (unit == 'minute') maps to '1minute'
    - '1day' and any multi-day timeframe (unit in {'day', 'week', 'month'}) maps to '1day'
    External broker/Yahoo APIs are ONLY ever called for '1minute' or '1day'.
    """
    _count, unit = parse_timeframe(raw_timeframe)
    return "1minute" if unit == "minute" else "1day"


def resample_ohlcv_rows(
    rows: List[Dict[str, Any]],
    timeframe: str,
    *,
    exchange: str = "NSE",
) -> List[Dict[str, Any]]:
    """
    Resample canonical '1minute' or '1day' OHLCV+OI rows into any derived timeframe.

    Rules:
    - Intraday (`<N>minute` for any N >= 2): Anchored to Indian market open
      (09:15 IST for NSE/BSE/NFO/BFO; 09:00 IST for MCX).
    - Multi-day (`<N>day` for any N >= 2): Grouped into consecutive N-trading-day candles.
    - Calendar (`1week`, `1month`): Grouped by ISO week (Mon-Fri) or calendar month.
    - Aggregation: open=first, high=max, low=min, close=last, volume=sum, oi=last.
    """
    count, unit = parse_timeframe(timeframe)
    tf = f"{count}{unit}"
    base_tf = "1minute" if unit == "minute" else "1day"
    if tf == base_tf or not rows:
        return list(rows)

    clean_exch = (exchange or "NSE").strip().upper()
    anchor_minute_of_day = 9 * 60 if clean_exch in {"MCX", "MCX_FO"} else (9 * 60 + 15)

    ordered = sorted(rows, key=lambda r: str(r.get("ts_ist") or ""))
    buckets: Dict[Any, Dict[str, Any]] = {}
    bucket_order: List[Any] = []

    for row_idx, row in enumerate(ordered):
        ts_raw = str(row.get("ts_ist") or "")
        if not ts_raw:
            continue
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        else:
            dt = dt.astimezone(IST_TZ)

        if unit == "minute":
            step = count
            mod = dt.hour * 60 + dt.minute
            if mod >= anchor_minute_of_day:
                offset = ((mod - anchor_minute_of_day) // step) * step
                bucket_mod = anchor_minute_of_day + offset
            else:
                bucket_mod = (mod // step) * step
            b_hr, b_min = divmod(min(bucket_mod, 23 * 60 + 59), 60)
            b_dt = datetime(dt.year, dt.month, dt.day, b_hr, b_min, 0, tzinfo=IST_TZ)
            b_key: Any = b_dt.isoformat(timespec="seconds")
            b_ts_ist = str(b_key)
            b_trade_date = dt.date().isoformat()
        elif unit == "day":
            # Group every `count` consecutive trading-day sessions chronologically
            b_key = row_idx // count
            b_ts_ist = ts_raw
            b_trade_date = dt.date().isoformat()
        elif tf == "1week":
            iso_yr, iso_wk, _ = dt.isocalendar()
            b_key = (iso_yr, iso_wk)
            b_ts_ist = ts_raw
            b_trade_date = dt.date().isoformat()
        elif tf == "1month":
            b_key = (dt.year, dt.month)
            b_ts_ist = ts_raw
            b_trade_date = dt.date().isoformat()
        else:
            b_key = ts_raw
            b_ts_ist = ts_raw
            b_trade_date = dt.date().isoformat()

        o_v = float(row["open"])
        h_v = float(row["high"])
        l_v = float(row["low"])
        c_v = float(row["close"])
        vol_v = int(row.get("volume", 0) or 0)
        oi_v = int(row.get("oi", 0) or 0)
        src_v = str(row.get("source") or "")

        existing = buckets.get(b_key)
        if existing is None:
            buckets[b_key] = {
                "ts_ist": b_ts_ist,
                "trade_date": b_trade_date,
                "open": o_v,
                "high": h_v,
                "low": l_v,
                "close": c_v,
                "volume": vol_v,
                "oi": oi_v,
                "source": src_v,
            }
            bucket_order.append(b_key)
        else:
            if h_v > existing["high"]:
                existing["high"] = h_v
            if l_v < existing["low"]:
                existing["low"] = l_v
            existing["close"] = c_v
            existing["volume"] += vol_v
            existing["oi"] = oi_v
            if unit in {"day", "week", "month"}:
                existing["trade_date"] = b_trade_date
                existing["ts_ist"] = b_ts_ist
            if src_v:
                existing["source"] = src_v

    return [buckets[k] for k in bucket_order]

_YAHOO_SYMBOL_OVERRIDES = {
    "NIFTY": "^NSEI",
    "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NIFTY BANK": "^NSEBANK",
    "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX",
}


def _parse_iso_date(raw: Optional[str], default: Optional[date] = None) -> date:
    if not raw or not str(raw).strip():
        if default is not None:
            return default
        raise ValueError("Date is required in YYYY-MM-DD format.")
    return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()


def _normalize_symbol(raw_symbol: str) -> str:
    sym = (raw_symbol or "").strip().upper()
    for prefix in (
        "NSE:",
        "BSE:",
        "NFO:",
        "BFO:",
        "MCX:",
        "NSE_EQ|",
        "BSE_EQ|",
        "NSE_FO|",
        "BSE_FO|",
        "MCX_FO|",
        "NSE_INDEX|",
        "BSE_INDEX|",
    ):
        if sym.startswith(prefix):
            sym = sym[len(prefix):].strip()
    if sym.endswith(".NS") or sym.endswith(".BO"):
        sym = sym[:-3].strip()
    return sym


def _is_fno_contract(symbol: str, exchange: str, instrument_type: Optional[str] = None) -> bool:
    ex = (exchange or "").strip().upper()
    if ex in {"NFO", "BFO", "MCX", "NSE_FO", "BSE_FO", "MCX_FO", "NCD", "BCD"}:
        return True
    it = (instrument_type or "").strip().upper()
    if it and (it.startswith("FUT") or it.startswith("OPT") or it in {"CE", "PE", "FO", "FNO", "F&O"}):
        return True
    sym = (symbol or "").strip().upper()
    if len(sym) > 5 and any(ch.isdigit() for ch in sym) and (sym.endswith("FUT") or sym.endswith("CE") or sym.endswith("PE")):
        return True
    return False


def compute_missing_date_ranges(
    start_date: date,
    end_date: date,
    covered_dates: Set[date],
    *,
    exchange: Optional[str] = None,
) -> List[Tuple[date, date]]:
    """
    Compute minimal contiguous [gap_start, gap_end] date intervals covering ONLY
    absent trading weekdays (Mon–Fri, excluding verified exchange holidays when
    `exchange` is supplied) not yet in `covered_dates`.

    Properties:
    - Never schedules a chunk that overlaps any covered weekday.
    - Skips isolated weekends and verified exchange holidays automatically.
    - Bridges intermediate weekends when adjacent trading weekdays on both sides
      are absent so consecutive weeks are fetched in minimal API calls.
    """
    if start_date > end_date:
        return []

    gaps: List[Tuple[date, date]] = []
    gap_start: Optional[date] = None
    last_missing_weekday: Optional[date] = None

    cursor = start_date
    while cursor <= end_date:
        is_weekday = cursor.weekday() < 5
        is_holiday = bool(exchange and is_exchange_holiday(cursor, exchange))
        if is_weekday and not is_holiday:
            if cursor not in covered_dates:
                if gap_start is None:
                    gap_start = cursor
                last_missing_weekday = cursor
            else:
                if gap_start is not None and last_missing_weekday is not None:
                    gaps.append((gap_start, last_missing_weekday))
                    gap_start = None
                    last_missing_weekday = None
        cursor += timedelta(days=1)

    if gap_start is not None and last_missing_weekday is not None:
        gaps.append((gap_start, last_missing_weekday))

    return gaps


def _split_chunk(start_d: date, end_d: date, max_days: int) -> List[Tuple[date, date]]:
    chunks: List[Tuple[date, date]] = []
    cur = start_d
    step = max(1, max_days - 1)
    while cur <= end_d:
        nxt = min(end_d, cur + timedelta(days=step))
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return chunks


def _normalize_adjustment_mode(raw: Optional[str]) -> str:
    mode = (raw or "raw").strip().lower()
    return "adjusted" if mode in {"adjusted", "adj", "split_adjusted"} else "raw"


def _month_keys_in_range(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[List[str]]:
    if not start_iso or not end_iso:
        return None
    s_d = date.fromisoformat(start_iso[:10])
    e_d = date.fromisoformat(end_iso[:10])
    keys: List[str] = []
    y, m = s_d.year, s_d.month
    while (y, m) <= (e_d.year, e_d.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return keys


class HistoricalDataManager:
    """
    Thread-safe Hybrid SQLite/Parquet OHLCV store and gap-only multi-provider downloader.

    Storage invariants:
    - `1minute` intraday Parquet data is partitioned into ONE file per calendar month
      (`YYYY-MM.parquet`) per asset so prior months are never rewritten.
    - SQLite `asset_data_history` catalog tracks `(symbol, exchange, timeframe, provider,
      adjustment_mode, start_date, end_date, missing_dates, verified_holidays, parquet_months)`
      and is queried FIRST on every OHLCV request before any outbound API call is made.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        parquet_dir: Optional[Path] = None,
        parquet_io: Optional[Any] = None,
    ) -> None:
        self._explicit_path = db_path
        self._explicit_parquet_dir = parquet_dir
        self._parquet_io = parquet_io or DefaultParquetIO()
        self._lock = threading.RLock()
        self._initialized_paths: set[Path] = set()

    @property
    def db_path(self) -> Path:
        return self._explicit_path or config.OHLCV_DB_PATH

    @property
    def parquet_dir(self) -> Path:
        if self._explicit_parquet_dir is not None:
            return self._explicit_parquet_dir
        if self._explicit_path is not None:
            return self._explicit_path.parent / "parquet"
        return config.PARQUET_DIR

    @staticmethod
    def _safe_token(value: str, fallback: str = "X") -> str:
        cleaned = "".join(ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_" for ch in (value or "").strip())
        return cleaned or fallback

    def _legacy_parquet_file(self, symbol: str, exchange: str, timeframe: str) -> Path:
        safe_sym = self._safe_token(symbol.upper(), "SYM")
        safe_exch = self._safe_token(exchange.upper(), "NSE")
        safe_tf = self._safe_token(timeframe.lower(), "1day")
        return self.parquet_dir / safe_exch / safe_tf / f"{safe_sym}.parquet"

    def _parquet_file(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        *,
        provider: str = "yfinance",
        adjustment_mode: str = "raw",
        month_key: Optional[str] = None,
    ) -> Path:
        safe_prov = self._safe_token(provider.lower(), "yfinance")
        safe_adj = _normalize_adjustment_mode(adjustment_mode)
        safe_exch = self._safe_token(exchange.upper(), "NSE")
        safe_tf = self._safe_token(timeframe.lower(), "1day")
        safe_sym = self._safe_token(symbol.upper(), "SYM")
        if safe_tf == "1minute":
            mk = self._safe_token(month_key or "unknown", "unknown")
            return self.parquet_dir / safe_prov / safe_adj / safe_exch / "1minute" / safe_sym / f"{mk}.parquet"
        return self.parquet_dir / safe_prov / safe_adj / safe_exch / safe_tf / f"{safe_sym}.parquet"

    def _parquet_monthly_dir(
        self,
        symbol: str,
        exchange: str,
        *,
        provider: str = "yfinance",
        adjustment_mode: str = "raw",
    ) -> Path:
        safe_prov = self._safe_token(provider.lower(), "yfinance")
        safe_adj = _normalize_adjustment_mode(adjustment_mode)
        safe_exch = self._safe_token(exchange.upper(), "NSE")
        safe_sym = self._safe_token(symbol.upper(), "SYM")
        return self.parquet_dir / safe_prov / safe_adj / safe_exch / "1minute" / safe_sym

    def _parquet_available(self) -> bool:
        try:
            return bool(self._parquet_io and self._parquet_io.available())
        except Exception:
            return False

    def storage_info(self) -> Dict[str, Any]:
        avail = self._parquet_available()
        p_dir = self.parquet_dir
        p_files = 0
        if p_dir.exists():
            p_files = sum(1 for _ in p_dir.rglob("*.parquet"))
        return {
            "configured_mode": config.OHLCV_STORAGE_MODE,
            "active_mode": "hybrid_parquet" if avail else "sqlite_fallback",
            "parquet_available": avail,
            "parquet_monthly_1m": True,
            "parquet_dir": str(p_dir),
            "parquet_files": p_files,
            "sqlite_ledger_path": str(self.db_path),
        }

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        if path not in self._initialized_paths:
            with self._lock:
                if path not in self._initialized_paths:
                    self._init_schema(conn)
                    self._initialized_paths.add(path)
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts_ist TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL DEFAULT 0,
                oi INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange, timeframe, ts_ist)
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles (symbol, exchange, timeframe, trade_date);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_coverage (
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                covered_date TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange, timeframe, covered_date)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_catalog (
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                provider TEXT NOT NULL,
                storage_backend TEXT NOT NULL DEFAULT 'sqlite',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange, timeframe)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_data_history (
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                provider TEXT NOT NULL,
                adjustment_mode TEXT NOT NULL DEFAULT 'raw',
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                missing_dates_json TEXT NOT NULL DEFAULT '[]',
                verified_holidays_json TEXT NOT NULL DEFAULT '[]',
                parquet_months_json TEXT NOT NULL DEFAULT '[]',
                row_count INTEGER NOT NULL DEFAULT 0,
                storage_backend TEXT NOT NULL DEFAULT 'sqlite',
                last_synced_at TEXT NOT NULL,
                PRIMARY KEY (symbol, exchange, timeframe, provider, adjustment_mode)
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_asset_history_lookup ON asset_data_history (symbol, exchange, timeframe);"
        )
        conn.commit()

    def _row_to_history_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            missing_dates = json.loads(row["missing_dates_json"] or "[]")
        except Exception:
            missing_dates = []
        try:
            verified_holidays = json.loads(row["verified_holidays_json"] or "[]")
        except Exception:
            verified_holidays = []
        try:
            parquet_months = json.loads(row["parquet_months_json"] or "[]")
        except Exception:
            parquet_months = []
        return {
            "symbol": str(row["symbol"]),
            "exchange": str(row["exchange"]),
            "timeframe": str(row["timeframe"]),
            "provider": str(row["provider"]),
            "adjustment_mode": str(row["adjustment_mode"]),
            "start_date": str(row["start_date"]),
            "end_date": str(row["end_date"]),
            "missing_dates": list(missing_dates),
            "verified_holidays": list(verified_holidays),
            "parquet_months": list(parquet_months),
            "row_count": int(row["row_count"] or 0),
            "storage_backend": str(row["storage_backend"] or "sqlite"),
            "last_synced_at": str(row["last_synced_at"] or ""),
        }

    def get_asset_history(
        self,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        *,
        provider: Optional[str] = None,
        adjustment_mode: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Query the SQLite asset_data_history catalog for an individual asset/timeframe."""
        clean_sym = _normalize_symbol(symbol)
        clean_exch = (exchange or "NSE").strip().upper()
        base_tf = resolve_base_timeframe(timeframe)
        clauses = ["symbol = ?", "exchange = ?", "timeframe = ?"]
        params: List[Any] = [clean_sym, clean_exch, base_tf]
        if provider:
            clauses.append("provider = ?")
            params.append(provider.strip().lower())
        if adjustment_mode:
            clauses.append("adjustment_mode = ?")
            params.append(_normalize_adjustment_mode(adjustment_mode))
        where_sql = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM asset_data_history WHERE {where_sql} ORDER BY last_synced_at DESC LIMIT 1",
                params,
            ).fetchone()
        return self._row_to_history_dict(row) if row is not None else None

    def list_asset_history(
        self,
        *,
        symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return all rows from the SQLite asset_data_history catalog."""
        clauses: List[str] = []
        params: List[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(_normalize_symbol(symbol))
        if exchange:
            clauses.append("exchange = ?")
            params.append(exchange.strip().upper())
        if timeframe:
            clauses.append("timeframe = ?")
            params.append(resolve_base_timeframe(timeframe))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM asset_data_history {where_sql} ORDER BY last_synced_at DESC, symbol ASC",
                params,
            ).fetchall()
        return [self._row_to_history_dict(r) for r in rows]

    def _upsert_asset_history(
        self,
        conn: sqlite3.Connection,
        *,
        symbol: str,
        exchange: str,
        base_tf: str,
        provider: str,
        adjustment_mode: str,
        storage_backend: str,
        now_ist_str: str,
        observed_dates: Set[str],
        checked_start: date,
        checked_end: date,
        parquet_months: Optional[List[str]] = None,
        row_count_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Recompute and persist the authoritative SQLite asset_data_history row."""
        existing = conn.execute(
            """
            SELECT * FROM asset_data_history
            WHERE symbol = ? AND exchange = ? AND timeframe = ? AND provider = ? AND adjustment_mode = ?
            """,
            (symbol, exchange, base_tf, provider, adjustment_mode),
        ).fetchone()

        # Gather all dates that actually have data in download_coverage
        cov_rows = conn.execute(
            """
            SELECT covered_date, status FROM download_coverage
            WHERE symbol = ? AND exchange = ? AND timeframe = ?
            """,
            (symbol, exchange, base_tf),
        ).fetchall()
        data_dates: Set[str] = set(observed_dates)
        for r in cov_rows:
            if str(r["status"]) == "data":
                data_dates.add(str(r["covered_date"]))

        # Determine overall start_date / end_date span from actual data (so pre-retention
        # empty lookbacks do not pollute missing_dates, while mid-range and trailing
        # provider outages are always recorded in missing_dates).
        data_span_candidates: List[str] = list(data_dates)
        if existing is not None:
            if existing["start_date"]:
                data_span_candidates.append(str(existing["start_date"]))
            if existing["end_date"]:
                data_span_candidates.append(str(existing["end_date"]))

        if data_span_candidates:
            span_start_str = min(data_span_candidates)
            span_end_str = max(max(data_span_candidates), checked_end.isoformat())
        else:
            span_start_str = checked_start.isoformat()
            span_end_str = checked_end.isoformat()

        span_start = date.fromisoformat(span_start_str[:10])
        span_end = date.fromisoformat(span_end_str[:10])

        # Compute exact missing_dates (trading weekdays in [span_start, span_end] without data
        # and NOT verified exchange holidays) and verified_holidays.
        missing_dates: List[str] = []
        verified_holidays: List[str] = []
        cur = span_start
        while cur <= span_end:
            if cur.weekday() < 5:
                d_iso = cur.isoformat()
                if is_exchange_holiday(cur, exchange):
                    verified_holidays.append(d_iso)
                elif d_iso not in data_dates:
                    missing_dates.append(d_iso)
            cur += timedelta(days=1)

        existing_months: Set[str] = set()
        if existing is not None:
            try:
                existing_months.update(json.loads(existing["parquet_months_json"] or "[]"))
            except Exception:
                pass
        if parquet_months:
            existing_months.update(parquet_months)
        sorted_months = sorted(existing_months)

        if row_count_override is not None:
            total_rows = int(row_count_override)
        elif existing is not None:
            total_rows = int(existing["row_count"] or 0)
        else:
            agg = conn.execute(
                "SELECT COUNT(*) AS cnt FROM candles WHERE symbol = ? AND exchange = ? AND timeframe = ?",
                (symbol, exchange, base_tf),
            ).fetchone()
            total_rows = int(agg["cnt"] or 0) if agg else 0

        conn.execute(
            """
            INSERT OR REPLACE INTO asset_data_history
            (symbol, exchange, timeframe, provider, adjustment_mode, start_date, end_date,
             missing_dates_json, verified_holidays_json, parquet_months_json,
             row_count, storage_backend, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                exchange,
                base_tf,
                provider,
                adjustment_mode,
                span_start_str,
                span_end_str,
                json.dumps(missing_dates),
                json.dumps(verified_holidays),
                json.dumps(sorted_months),
                total_rows,
                storage_backend,
                now_ist_str,
            ),
        )
        return {
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": base_tf,
            "provider": provider,
            "adjustment_mode": adjustment_mode,
            "start_date": span_start_str,
            "end_date": span_end_str,
            "missing_dates": missing_dates,
            "verified_holidays": verified_holidays,
            "parquet_months": sorted_months,
            "row_count": total_rows,
            "storage_backend": storage_backend,
            "last_synced_at": now_ist_str,
        }

    def _get_covered_dates(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start_d: date,
        end_d: date,
    ) -> Set[date]:
        today_ist = datetime.now(tz=IST_TZ).date()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT covered_date, status FROM download_coverage
                WHERE symbol = ? AND exchange = ? AND timeframe = ?
                  AND covered_date >= ? AND covered_date <= ?
                """,
                (symbol, exchange, timeframe, start_d.isoformat(), end_d.isoformat()),
            ).fetchall()
        covered: Set[date] = set()
        for r in rows:
            try:
                d_val = date.fromisoformat(r["covered_date"])
                status = str(r["status"] or "")
                # Provider outages/missing dates MUST be retried, never treated as permanently covered.
                if status == "provider_missing":
                    continue
                if status in {"holiday", "exchange_holiday"}:
                    # Only skip verified exchange holidays (never freeze recent or non-holiday outages)
                    if d_val >= (today_ist - timedelta(days=1)):
                        continue
                    if not is_exchange_holiday(d_val, exchange):
                        continue
                covered.add(d_val)
            except ValueError:
                pass
        return covered

    # ── Provider Fetchers (single chunk HTTP) ─────────────────────────────

    def _fetch_chunk_from_provider(
        self,
        provider: str,
        symbol: str,
        exchange: str,
        timeframe: str,
        chunk_start: date,
        chunk_end: date,
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
        adjustment_mode: str = "raw",
    ) -> List[Dict[str, Any]]:
        if provider == "upstox":
            return self._fetch_upstox_chunk(
                symbol,
                exchange,
                timeframe,
                chunk_start,
                chunk_end,
                instrument_type=instrument_type,
                instrument_key=instrument_key,
            )
        if provider == "zerodha":
            return self._fetch_zerodha_chunk(
                symbol,
                exchange,
                timeframe,
                chunk_start,
                chunk_end,
                instrument_type=instrument_type,
                instrument_key=instrument_key,
            )
        if provider == "yfinance":
            return self._fetch_yahoo_chunk(
                symbol,
                exchange,
                timeframe,
                chunk_start,
                chunk_end,
                instrument_type=instrument_type,
                adjustment_mode=adjustment_mode,
            )
        raise ValueError(f"Unsupported historical data provider '{provider}'.")

    def _fetch_upstox_chunk(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        chunk_start: date,
        chunk_end: date,
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        ikey = (
            instrument_key.strip()
            if instrument_key and "|" in instrument_key
            else instrument_index.resolve_key(
                symbol,
                broker_id="upstox",
                exchange=exchange,
                instrument_type=instrument_type,
            )
        )
        if not ikey:
            sync_meta = instrument_index.stats().get("sync_meta", {})
            if "upstox" not in sync_meta:
                try:
                    instrument_index.sync_upstox_master()
                    ikey = instrument_index.resolve_key(
                        symbol,
                        broker_id="upstox",
                        exchange=exchange,
                        instrument_type=instrument_type,
                    )
                except Exception:
                    pass
        if not ikey:
            raise ValueError(
                f"Contract '{symbol}' ({exchange}) was not found in Upstox instruments. "
                "Click Sync Upstox Master or select the exact contract from the F&O Finder."
            )
        base_tf = resolve_base_timeframe(timeframe)
        tf_meta = VALID_TIMEFRAMES[base_tf]
        unit = tf_meta["upstox_unit"]
        interval = tf_meta["upstox_interval"]
        encoded_key = urllib.parse.quote(ikey, safe="")
        url = (
            f"{config.UPSTOX_BASE_URL}/v3/historical-candle/"
            f"{encoded_key}/{unit}/{interval}/{chunk_end.isoformat()}/{chunk_start.isoformat()}"
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AlgoDesk/5.0",
        }
        try:
            headers["Authorization"] = f"Bearer {upstox_auth.get_valid_token()}"
        except RuntimeError:
            pass

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 404:
                raise ValueError(
                    f"Upstox returned no historical candles for {exchange}:{symbol} ({timeframe}). Try Yahoo Finance or verify symbol."
                ) from exc
            raise RuntimeError(f"Upstox V3 historical API HTTP {exc.code}: {err_body[:180]}") from exc

        candles = ((payload.get("data") or {}).get("candles")) or []
        by_ts: Dict[str, Dict[str, Any]] = {}

        def _ingest_candles(raw_list: List[Any]) -> None:
            for row in raw_list:
                if not row or len(row) < 6:
                    continue
                ts_raw = str(row[0])
                dt_ist = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(IST_TZ)
                if not (chunk_start <= dt_ist.date() <= chunk_end):
                    continue
                ts_iso = dt_ist.isoformat(timespec="seconds")
                by_ts[ts_iso] = {
                    "ts_ist": ts_iso,
                    "trade_date": dt_ist.date().isoformat(),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": int(row[5] or 0),
                    "oi": int(row[6] or 0) if len(row) > 6 else 0,
                }

        _ingest_candles(candles)

        # Upstox V3 /v3/historical-candle covers settled sessions; merge
        # /v3/historical-candle/intraday/{key}/{unit}/{interval} when window reaches today.
        today_ist = datetime.now(tz=IST_TZ).date()
        if chunk_end >= today_ist:
            intra_url = (
                f"{config.UPSTOX_BASE_URL}/v3/historical-candle/intraday/"
                f"{encoded_key}/{unit}/{interval}"
            )
            intra_req = urllib.request.Request(intra_url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(intra_req, timeout=8) as resp:
                    intra_payload = json.loads(resp.read().decode("utf-8"))
                intra_candles = ((intra_payload.get("data") or {}).get("candles")) or []
                _ingest_candles(intra_candles)
            except Exception:
                pass

        return [by_ts[k] for k in sorted(by_ts.keys())]

    def _fetch_zerodha_chunk(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        chunk_start: date,
        chunk_end: date,
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        api_key = zerodha_auth.api_key
        if not api_key:
            raise ValueError("Zerodha API credentials required for Kite historical download.")
        token = zerodha_auth.get_valid_token()
        inst_token = (
            instrument_key.strip()
            if instrument_key and instrument_key.strip().isdigit()
            else instrument_index.resolve_key(
                symbol,
                broker_id="zerodha",
                exchange=exchange,
                instrument_type=instrument_type,
            )
        )
        if not inst_token:
            sync_meta = instrument_index.stats().get("sync_meta", {})
            if "zerodha" not in sync_meta:
                try:
                    instrument_index.sync_zerodha_master()
                    inst_token = instrument_index.resolve_key(
                        symbol,
                        broker_id="zerodha",
                        exchange=exchange,
                        instrument_type=instrument_type,
                    )
                except Exception:
                    pass
        if not inst_token:
            raise ValueError(
                f"Contract '{symbol}' ({exchange}) was not found in Zerodha Kite instruments. "
                "Click Sync Zerodha Master or select the exact contract from the F&O Finder."
            )

        base_tf = resolve_base_timeframe(timeframe)
        interval = VALID_TIMEFRAMES[base_tf]["zerodha"]
        params = urllib.parse.urlencode(
            {
                "from": f"{chunk_start.isoformat()} 09:00:00",
                "to": f"{chunk_end.isoformat()} 15:35:00",
                "oi": "1",
            }
        )
        url = f"{config.ZERODHA_BASE_URL}/instruments/historical/{inst_token}/{interval}?{params}"
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{token}",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 404:
                raise ValueError(
                    f"Zerodha returned no historical candles for {exchange}:{symbol} ({timeframe})."
                ) from exc
            raise RuntimeError(f"Zerodha historical API HTTP {exc.code}: {err_body[:180]}") from exc

        candles = ((payload.get("data") or {}).get("candles")) or []
        out: List[Dict[str, Any]] = []
        for row in candles:
            if not row or len(row) < 6:
                continue
            dt_ist = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(IST_TZ)
            out.append(
                {
                    "ts_ist": dt_ist.isoformat(timespec="seconds"),
                    "trade_date": dt_ist.date().isoformat(),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": int(row[5] or 0),
                    "oi": int(row[6] or 0) if len(row) > 6 else 0,
                }
            )
        return out

    def _fetch_yahoo_chunk(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        chunk_start: date,
        chunk_end: date,
        instrument_type: Optional[str] = None,
        adjustment_mode: str = "raw",
    ) -> List[Dict[str, Any]]:
        clean = symbol.strip().upper()
        if _is_fno_contract(clean, exchange, instrument_type):
            raise ValueError(
                "Yahoo Finance does not provide Indian F&O (Futures/Options) historical contracts. "
                "Select Upstox or Zerodha Kite as Source and ensure your broker instrument master is synced."
            )
        if clean in _YAHOO_SYMBOL_OVERRIDES:
            ticker = _YAHOO_SYMBOL_OVERRIDES[clean]
        elif "." in clean or clean.startswith("^"):
            ticker = clean
        else:
            suffix = ".BO" if exchange == "BSE" else ".NS"
            ticker = f"{clean}{suffix}"

        base_tf = resolve_base_timeframe(timeframe)
        interval = VALID_TIMEFRAMES[base_tf]["yahoo"]
        p1_dt = datetime.combine(chunk_start, time(0, 0), tzinfo=IST_TZ)
        p2_dt = datetime.combine(chunk_end + timedelta(days=1), time(0, 0), tzinfo=IST_TZ)
        params = urllib.parse.urlencode(
            {
                "period1": str(int(p1_dt.timestamp())),
                "period2": str(int(p2_dt.timestamp())),
                "interval": interval,
                "includePrePost": "false",
            }
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 AlgoDesk/3.0", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 404:
                raise ValueError(
                    f"Symbol '{clean}' ({ticker}) was not found on Yahoo Finance for {exchange}. "
                    "Verify the ticker spelling or pick an instrument from the SQLite lookup above."
                ) from exc
            raise RuntimeError(f"Yahoo Finance chart API HTTP {exc.code}: {err_body[:180]}") from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            desc = (chart["error"] or {}).get("description") or str(chart["error"])
            raise RuntimeError(f"Yahoo Finance error for {ticker}: {desc}")

        results = chart.get("result") or []
        if not results:
            return []

        res0 = results[0]
        timestamps = res0.get("timestamp") or []
        indicators = res0.get("indicators") or {}
        quote = (indicators.get("quote") or [{}])[0]
        adj_block = (indicators.get("adjclose") or [{}])[0]
        adj_closes = adj_block.get("adjclose") or []
        use_adjusted = _normalize_adjustment_mode(adjustment_mode) == "adjusted"

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        out: List[Dict[str, Any]] = []
        for idx, ts_unix in enumerate(timestamps):
            if idx >= len(closes) or closes[idx] is None:
                continue
            dt_ist = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc).astimezone(IST_TZ)
            # Clip strictly within requested gap window
            if not (chunk_start <= dt_ist.date() <= chunk_end):
                continue
            raw_c = float(closes[idx])
            o = float(opens[idx]) if idx < len(opens) and opens[idx] is not None else raw_c
            h = float(highs[idx]) if idx < len(highs) and highs[idx] is not None else raw_c
            l = float(lows[idx]) if idx < len(lows) and lows[idx] is not None else raw_c
            c_val = raw_c
            if use_adjusted and idx < len(adj_closes) and adj_closes[idx] is not None and raw_c > 0:
                adj_c = float(adj_closes[idx])
                ratio = adj_c / raw_c
                o *= ratio
                h *= ratio
                l *= ratio
                c_val = adj_c
            v = volumes[idx] if idx < len(volumes) and volumes[idx] is not None else 0
            out.append(
                {
                    "ts_ist": dt_ist.isoformat(timespec="seconds"),
                    "trade_date": dt_ist.date().isoformat(),
                    "open": round(o, 4),
                    "high": round(h, 4),
                    "low": round(l, 4),
                    "close": round(c_val, 4),
                    "volume": int(v),
                    "oi": 0,
                }
            )
        return out

    # ── Gap-Only Download Orchestration (Query-First SQLite History) ──────

    def _check_history_catalog_covers_range(
        self,
        history: Optional[Dict[str, Any]],
        start_d: date,
        end_d: date,
        exchange: str,
    ) -> bool:
        """
        Return True if the SQLite `asset_data_history` row already guarantees full
        coverage of [start_d, end_d] with zero retryable missing dates inside the span.
        """
        if not history:
            return False
        h_start = str(history.get("start_date") or "")
        h_end = str(history.get("end_date") or "")
        if not h_start or not h_end:
            return False
        if h_start > start_d.isoformat() or h_end < end_d.isoformat():
            return False
        missing_set = set(history.get("missing_dates") or [])
        s_iso = start_d.isoformat()
        e_iso = end_d.isoformat()
        for m_d in missing_set:
            if s_iso <= m_d <= e_iso:
                return False
        return True

    def download(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        provider: Optional[str] = None,
        adjustment_mode: str = "raw",
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
        fetcher: Optional[Callable[[str, str, str, date, date], List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """
        Download ONLY absent trading weekdays for (symbol, exchange, timeframe)
        in [from_date, to_date].

        Query-First Contract:
        1. Queries SQLite `asset_data_history` FIRST before making any outbound API call.
        2. If `[from_date, to_date]` is inside `[history.start_date, history.end_date]`
           with no overlapping `missing_dates`, immediately serves from local storage
           with 0 API calls (`catalog_hit=True`).
        3. Skips weekends and verified exchange holidays (`core.calendar.is_exchange_holiday`);
           non-holiday empty weekdays are recorded in `missing_dates` (retryable) rather than
           permanently frozen as holidays.
        4. For `1minute` base data with Parquet enabled, writes ONE Parquet file per
           calendar month (`YYYY-MM.parquet`) and NEVER rewrites prior untouched months.
        """
        clean_sym = _normalize_symbol(symbol)
        if not clean_sym:
            raise ValueError("Trading symbol is required.")
        clean_exch = (exchange or "NSE").strip().upper()
        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)
        adj_mode = _normalize_adjustment_mode(adjustment_mode)

        today_ist = datetime.now(tz=IST_TZ).date()
        end_d = _parse_iso_date(to_date, default=today_ist)
        if end_d > today_ist:
            end_d = today_ist
        start_d = _parse_iso_date(from_date, default=(end_d - timedelta(days=30)))
        if start_d > end_d:
            raise ValueError(f"from_date ({start_d}) must be on or before to_date ({end_d}).")

        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()

        # ── Step 1: Query SQLite `asset_data_history` FIRST ──────────────────
        history_entry = self.get_asset_history(
            clean_sym,
            clean_exch,
            base_tf,
            provider=eff_provider,
            adjustment_mode=adj_mode,
        )
        # Count trading weekdays (excluding verified exchange holidays) in requested span
        total_weekdays = 0
        cursor = start_d
        while cursor <= end_d:
            if cursor.weekday() < 5 and not is_exchange_holiday(cursor, clean_exch):
                total_weekdays += 1
            cursor += timedelta(days=1)

        if self._check_history_catalog_covers_range(history_entry, start_d, end_d, clean_exch):
            snapshot = self.get_records(
                symbol=clean_sym,
                exchange=clean_exch,
                timeframe=tf,
                from_date=start_d.isoformat(),
                to_date=end_d.isoformat(),
                view="snapshot",
                provider=eff_provider,
                adjustment_mode=adj_mode,
            )
            resampled_note = f" (resampled from canonical {base_tf})" if tf != base_tf else ""
            return {
                "ok": True,
                "symbol": clean_sym,
                "exchange": clean_exch,
                "timeframe": tf,
                "base_timeframe": base_tf,
                "resampled": tf != base_tf,
                "provider": eff_provider,
                "adjustment_mode": adj_mode,
                "requested_range": {"from": start_d.isoformat(), "to": end_d.isoformat()},
                "total_weekdays_in_range": total_weekdays,
                "existing_days_skipped": total_weekdays,
                "missing_days_count": 0,
                "missing_ranges_fetched": [],
                "api_calls_made": 0,
                "rows_inserted": 0,
                "cache_hit_only": True,
                "catalog_hit": True,
                "asset_history": history_entry,
                "message": (
                    f"SQLite asset_data_history satisfied all {total_weekdays} trading days locally in {base_tf} — 0 API calls made{resampled_note}."
                ),
                "snapshot": snapshot,
            }

        # ── Step 2: Compute exact uncovered trading-day gaps ─────────────────
        covered = self._get_covered_dates(clean_sym, clean_exch, base_tf, start_d, end_d)
        skipped_existing_days = 0
        cursor = start_d
        while cursor <= end_d:
            if cursor.weekday() < 5 and not is_exchange_holiday(cursor, clean_exch):
                if cursor in covered:
                    skipped_existing_days += 1
            cursor += timedelta(days=1)

        missing_gaps = compute_missing_date_ranges(start_d, end_d, covered, exchange=clean_exch)
        max_chunk = int(VALID_TIMEFRAMES[base_tf]["max_chunk_days"])

        api_calls_made = 0
        rows_inserted = 0
        fetched_ranges: List[Dict[str, str]] = []
        updated_history = history_entry

        if missing_gaps:
            if fetcher is None and eff_provider == "yfinance" and _is_fno_contract(clean_sym, clean_exch, instrument_type):
                raise ValueError(
                    "Yahoo Finance does not support Indian Futures & Options contracts. "
                    "Switch Source to Upstox or Zerodha Kite to download F&O data."
                )
            now_ist_str = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            all_candles: List[Dict[str, Any]] = []
            dates_with_data: Set[str] = set()
            checked_weekdays: List[date] = []

            for g_start, g_end in missing_gaps:
                for c_start, c_end in _split_chunk(g_start, g_end, max_chunk):
                    api_calls_made += 1
                    fetched_ranges.append({"from": c_start.isoformat(), "to": c_end.isoformat()})
                    if fetcher is not None:
                        chunk_rows = fetcher(clean_sym, clean_exch, base_tf, c_start, c_end)
                    else:
                        chunk_rows = self._fetch_chunk_from_provider(
                            eff_provider,
                            clean_sym,
                            clean_exch,
                            base_tf,
                            c_start,
                            c_end,
                            instrument_type=instrument_type,
                            instrument_key=instrument_key,
                            adjustment_mode=adj_mode,
                        )

                    for item in chunk_rows:
                        dates_with_data.add(item["trade_date"])
                        all_candles.append(item)

                    d_cur = c_start
                    while d_cur <= c_end:
                        if d_cur.weekday() < 5 and d_cur not in covered:
                            checked_weekdays.append(d_cur)
                        d_cur += timedelta(days=1)

            use_parquet = self._parquet_available()
            storage_backend = "parquet" if use_parquet else "sqlite"
            parquet_meta: Optional[Tuple[int, str, str]] = None
            touched_months: List[str] = []

            with self._connect() as conn:
                if all_candles:
                    if use_parquet:
                        normalized_new = [
                            {
                                "ts_ist": str(c["ts_ist"]),
                                "trade_date": str(c["trade_date"]),
                                "open": float(c["open"]),
                                "high": float(c["high"]),
                                "low": float(c["low"]),
                                "close": float(c["close"]),
                                "volume": int(c.get("volume", 0)),
                                "oi": int(c.get("oi", 0)),
                                "source": eff_provider,
                            }
                            for c in all_candles
                        ]
                        if base_tf == "1minute":
                            # Partition strictly by calendar month YYYY-MM — NEVER rewrite untouched prior months!
                            by_month: Dict[str, List[Dict[str, Any]]] = {}
                            for rec in normalized_new:
                                mk = rec["trade_date"][:7]
                                by_month.setdefault(mk, []).append(rec)
                            for mk, m_rows in sorted(by_month.items()):
                                p_file = self._parquet_file(
                                    clean_sym,
                                    clean_exch,
                                    base_tf,
                                    provider=eff_provider,
                                    adjustment_mode=adj_mode,
                                    month_key=mk,
                                )
                                existing_m = self._parquet_io.read_rows(p_file) if p_file.exists() else []
                                merged_by_ts: Dict[str, Dict[str, Any]] = {str(r["ts_ist"]): r for r in existing_m}
                                for r in m_rows:
                                    merged_by_ts[str(r["ts_ist"])] = r
                                ordered_m = [merged_by_ts[k] for k in sorted(merged_by_ts.keys())]
                                self._parquet_io.write_rows(p_file, ordered_m)
                                touched_months.append(mk)

                            # Compute aggregate metadata across all monthly files for this asset
                            m_dir = self._parquet_monthly_dir(
                                clean_sym,
                                clean_exch,
                                provider=eff_provider,
                                adjustment_mode=adj_mode,
                            )
                            month_files = sorted(m_dir.glob("*.parquet")) if m_dir.exists() else []
                            all_months = [mf.stem for mf in month_files]
                            touched_months = all_months
                            total_cnt = 0
                            first_td: Optional[str] = None
                            last_td: Optional[str] = None
                            for mf in month_files:
                                m_read = self._parquet_io.read_rows(mf)
                                total_cnt += len(m_read)
                                if m_read:
                                    d0 = str(m_read[0]["trade_date"])
                                    d1 = str(m_read[-1]["trade_date"])
                                    if first_td is None or d0 < first_td:
                                        first_td = d0
                                    if last_td is None or d1 > last_td:
                                        last_td = d1
                            if total_cnt > 0 and first_td and last_td:
                                parquet_meta = (total_cnt, first_td, last_td)
                        else:
                            p_file = self._parquet_file(
                                clean_sym,
                                clean_exch,
                                base_tf,
                                provider=eff_provider,
                                adjustment_mode=adj_mode,
                            )
                            existing_rows: List[Dict[str, Any]] = []
                            if p_file.exists():
                                existing_rows = self._parquet_io.read_rows(p_file)
                            else:
                                legacy_p = self._legacy_parquet_file(clean_sym, clean_exch, base_tf)
                                if legacy_p.exists():
                                    existing_rows = self._parquet_io.read_rows(legacy_p)
                                else:
                                    legacy_sql = conn.execute(
                                        "SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                                        "FROM candles WHERE symbol = ? AND exchange = ? AND timeframe = ?",
                                        (clean_sym, clean_exch, base_tf),
                                    ).fetchall()
                                    if legacy_sql:
                                        existing_rows = [dict(r) for r in legacy_sql]
                            merged_by_ts = {str(r["ts_ist"]): r for r in existing_rows}
                            for r in normalized_new:
                                merged_by_ts[str(r["ts_ist"])] = r
                            ordered_all = [merged_by_ts[k] for k in sorted(merged_by_ts.keys())]
                            self._parquet_io.write_rows(p_file, ordered_all)
                            if ordered_all:
                                parquet_meta = (
                                    len(ordered_all),
                                    str(ordered_all[0]["trade_date"]),
                                    str(ordered_all[-1]["trade_date"]),
                                )
                        rows_inserted = len(all_candles)
                    else:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO candles
                            (symbol, exchange, timeframe, ts_ist, trade_date,
                             open, high, low, close, volume, oi, source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    clean_sym,
                                    clean_exch,
                                    base_tf,
                                    c["ts_ist"],
                                    c["trade_date"],
                                    float(c["open"]),
                                    float(c["high"]),
                                    float(c["low"]),
                                    float(c["close"]),
                                    int(c.get("volume", 0)),
                                    int(c.get("oi", 0)),
                                    eff_provider,
                                )
                                for c in all_candles
                            ],
                        )
                        rows_inserted = len(all_candles)
                        agg_cat = conn.execute(
                            "SELECT COUNT(*) AS cnt, MIN(trade_date) AS s_d, MAX(trade_date) AS e_d "
                            "FROM candles WHERE symbol = ? AND exchange = ? AND timeframe = ?",
                            (clean_sym, clean_exch, base_tf),
                        ).fetchone()
                        if agg_cat and int(agg_cat["cnt"] or 0) > 0:
                            parquet_meta = (int(agg_cat["cnt"]), str(agg_cat["s_d"]), str(agg_cat["e_d"]))

                    if parquet_meta is not None:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO dataset_catalog
                            (symbol, exchange, timeframe, row_count, start_date, end_date, provider, storage_backend, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                clean_sym,
                                clean_exch,
                                base_tf,
                                parquet_meta[0],
                                parquet_meta[1],
                                parquet_meta[2],
                                eff_provider,
                                storage_backend,
                                now_ist_str,
                            ),
                        )

                # Determine earliest known data date so pre-retention/pre-listing empty
                # chunks are marked `no_history` instead of triggering endless retries,
                # while mid-range and trailing non-holiday empty weekdays are marked
                # `provider_missing` (retryable).
                known_min_data_iso: Optional[str] = min(dates_with_data) if dates_with_data else None
                if history_entry and history_entry.get("start_date"):
                    h_min = str(history_entry["start_date"])
                    if known_min_data_iso is None or h_min < known_min_data_iso:
                        known_min_data_iso = h_min

                coverage_rows = []
                for d_item in checked_weekdays:
                    d_iso = d_item.isoformat()
                    if d_iso in dates_with_data:
                        coverage_rows.append((clean_sym, clean_exch, base_tf, d_iso, "data", now_ist_str))
                    elif is_exchange_holiday(d_item, clean_exch):
                        coverage_rows.append((clean_sym, clean_exch, base_tf, d_iso, "exchange_holiday", now_ist_str))
                    elif known_min_data_iso is not None and d_iso < known_min_data_iso and d_item < (today_ist - timedelta(days=1)):
                        coverage_rows.append((clean_sym, clean_exch, base_tf, d_iso, "no_history", now_ist_str))
                    else:
                        # Never freeze a mid-range or recent non-holiday outage as a holiday; mark retryable provider_missing!
                        coverage_rows.append((clean_sym, clean_exch, base_tf, d_iso, "provider_missing", now_ist_str))

                if coverage_rows:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO download_coverage
                        (symbol, exchange, timeframe, covered_date, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        coverage_rows,
                    )

                updated_history = self._upsert_asset_history(
                    conn,
                    symbol=clean_sym,
                    exchange=clean_exch,
                    base_tf=base_tf,
                    provider=eff_provider,
                    adjustment_mode=adj_mode,
                    storage_backend=storage_backend,
                    now_ist_str=now_ist_str,
                    observed_dates=dates_with_data,
                    checked_start=start_d,
                    checked_end=end_d,
                    parquet_months=touched_months,
                    row_count_override=parquet_meta[0] if parquet_meta is not None else None,
                )
                conn.commit()
        elif history_entry is None:
            # Backfill asset_data_history if pre-existing coverage already existed without a history row
            now_ist_str = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            with self._connect() as conn:
                updated_history = self._upsert_asset_history(
                    conn,
                    symbol=clean_sym,
                    exchange=clean_exch,
                    base_tf=base_tf,
                    provider=eff_provider,
                    adjustment_mode=adj_mode,
                    storage_backend="parquet" if self._parquet_available() else "sqlite",
                    now_ist_str=now_ist_str,
                    observed_dates={d.isoformat() for d in covered},
                    checked_start=start_d,
                    checked_end=end_d,
                )
                conn.commit()

        snapshot = self.get_records(
            symbol=clean_sym,
            exchange=clean_exch,
            timeframe=tf,
            from_date=start_d.isoformat(),
            to_date=end_d.isoformat(),
            view="snapshot",
            provider=eff_provider,
            adjustment_mode=adj_mode,
        )

        resampled_note = f" (resampled from canonical {base_tf})" if tf != base_tf else ""
        return {
            "ok": True,
            "symbol": clean_sym,
            "exchange": clean_exch,
            "timeframe": tf,
            "base_timeframe": base_tf,
            "resampled": tf != base_tf,
            "provider": eff_provider,
            "adjustment_mode": adj_mode,
            "requested_range": {"from": start_d.isoformat(), "to": end_d.isoformat()},
            "total_weekdays_in_range": total_weekdays,
            "existing_days_skipped": skipped_existing_days,
            "missing_days_count": max(0, total_weekdays - skipped_existing_days),
            "missing_ranges_fetched": fetched_ranges,
            "api_calls_made": api_calls_made,
            "rows_inserted": rows_inserted,
            "cache_hit_only": api_calls_made == 0,
            "catalog_hit": False,
            "asset_history": updated_history,
            "message": (
                f"All {skipped_existing_days} trading days already cached locally in {base_tf} — 0 API calls made{resampled_note}."
                if api_calls_made == 0
                else f"Fetched {len(fetched_ranges)} missing {base_tf} gap(s) via {api_calls_made} API call(s); skipped {skipped_existing_days} existing day(s){resampled_note}."
            ),
            "snapshot": snapshot,
        }

    # ── Snapshot & Full Data Inspection (0 API calls) ─────────────────────

    def _read_raw_base_rows(
        self,
        *,
        clean_sym: str,
        clean_exch: str,
        base_tf: str,
        from_iso: Optional[str],
        to_iso: Optional[str],
        provider: Optional[str] = None,
        adjustment_mode: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Read canonical base rows ('1minute' monthly Parquet or '1day' Parquet or SQLite)."""
        if self._parquet_available():
            # Resolve provider + adjustment_mode from explicit args or latest asset_data_history
            eff_prov = (provider or "").strip().lower()
            eff_adj = _normalize_adjustment_mode(adjustment_mode) if adjustment_mode else ""
            if not eff_prov or not eff_adj:
                hist = self.get_asset_history(clean_sym, clean_exch, base_tf)
                if hist:
                    eff_prov = eff_prov or str(hist.get("provider") or "yfinance")
                    eff_adj = eff_adj or str(hist.get("adjustment_mode") or "raw")
                else:
                    eff_prov = eff_prov or (config.ACTIVE_DATA_CONNECTION or "yfinance")
                    eff_adj = eff_adj or "raw"

            if base_tf == "1minute":
                m_dir = self._parquet_monthly_dir(
                    clean_sym,
                    clean_exch,
                    provider=eff_prov,
                    adjustment_mode=eff_adj,
                )
                if m_dir.exists():
                    wanted_months = _month_keys_in_range(from_iso, to_iso)
                    month_files = sorted(m_dir.glob("*.parquet"))
                    if wanted_months is not None:
                        wanted_set = set(wanted_months)
                        month_files = [mf for mf in month_files if mf.stem in wanted_set]
                    collected: List[Dict[str, Any]] = []
                    for mf in month_files:
                        for row in self._parquet_io.read_rows(mf):
                            td = str(row.get("trade_date") or "")
                            if from_iso and td < from_iso:
                                continue
                            if to_iso and td > to_iso:
                                continue
                            collected.append(row)
                    if collected:
                        collected.sort(key=lambda r: str(r.get("ts_ist") or ""))
                        return collected, "parquet"
            else:
                p_file = self._parquet_file(
                    clean_sym,
                    clean_exch,
                    base_tf,
                    provider=eff_prov,
                    adjustment_mode=eff_adj,
                )
                if p_file.exists():
                    rows_p = [
                        r
                        for r in self._parquet_io.read_rows(p_file)
                        if (not from_iso or str(r.get("trade_date") or "") >= from_iso)
                        and (not to_iso or str(r.get("trade_date") or "") <= to_iso)
                    ]
                    rows_p.sort(key=lambda r: str(r.get("ts_ist") or ""))
                    return rows_p, "parquet"

            # Legacy single-file parquet fallback
            legacy_p = self._legacy_parquet_file(clean_sym, clean_exch, base_tf)
            if legacy_p.exists():
                rows_leg = [
                    r
                    for r in self._parquet_io.read_rows(legacy_p)
                    if (not from_iso or str(r.get("trade_date") or "") >= from_iso)
                    and (not to_iso or str(r.get("trade_date") or "") <= to_iso)
                ]
                rows_leg.sort(key=lambda r: str(r.get("ts_ist") or ""))
                return rows_leg, "parquet"

        clauses = ["symbol = ?", "exchange = ?", "timeframe = ?"]
        params: List[Any] = [clean_sym, clean_exch, base_tf]
        if from_iso:
            clauses.append("trade_date >= ?")
            params.append(from_iso)
        if to_iso:
            clauses.append("trade_date <= ?")
            params.append(to_iso)
        where_sql = " AND ".join(clauses)
        with self._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    f"SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                    f"FROM candles WHERE {where_sql} ORDER BY ts_ist ASC",
                    params,
                ).fetchall()
            ]
        return rows, "sqlite"

    def get_records(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        view: str = "snapshot",
        limit: Optional[int] = None,
        offset: int = 0,
        provider: Optional[str] = None,
        adjustment_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_sym = _normalize_symbol(symbol)
        clean_exch = (exchange or "NSE").strip().upper()
        tf = normalize_timeframe(timeframe)
        base_tf = resolve_base_timeframe(tf)
        mode = "full" if (view or "").strip().lower() == "full" else "snapshot"
        from_iso = _parse_iso_date(from_date).isoformat() if from_date else None
        to_iso = _parse_iso_date(to_date).isoformat() if to_date else None

        # Read canonical base timeframe (1minute or 1day) and strictly filter dates BEFORE resampling
        raw_rows, storage_backend = self._read_raw_base_rows(
            clean_sym=clean_sym,
            clean_exch=clean_exch,
            base_tf=base_tf,
            from_iso=from_iso,
            to_iso=to_iso,
            provider=provider,
            adjustment_mode=adjustment_mode,
        )

        # Fallback: if legacy pre-canonical rows exist stored directly under tf, read them too
        if not raw_rows and tf != base_tf:
            legacy_rows, legacy_backend = self._read_raw_base_rows(
                clean_sym=clean_sym,
                clean_exch=clean_exch,
                base_tf=tf,
                from_iso=from_iso,
                to_iso=to_iso,
                provider=provider,
                adjustment_mode=adjustment_mode,
            )
            if legacy_rows:
                raw_rows = legacy_rows
                storage_backend = legacy_backend
                resampled_rows = raw_rows
            else:
                resampled_rows = []
        else:
            resampled_rows = (
                resample_ohlcv_rows(raw_rows, tf, exchange=clean_exch)
                if tf != base_tf
                else raw_rows
            )

        total_rows = len(resampled_rows)
        if total_rows == 0:
            return {
                "symbol": clean_sym,
                "exchange": clean_exch,
                "timeframe": tf,
                "base_timeframe": base_tf,
                "resampled": tf != base_tf,
                "view_mode": mode,
                "storage_backend": storage_backend,
                "total_rows": 0,
                "returned_rows": 0,
                "is_truncated": False,
                "summary": {
                    "start_ts": None,
                    "end_ts": None,
                    "min_low": None,
                    "max_high": None,
                    "latest_close": None,
                    "change_pct": None,
                    "total_volume": 0,
                    "latest_oi": 0,
                },
                "rows": [],
            }

        first_open = float(resampled_rows[0]["open"])
        latest_close = float(resampled_rows[-1]["close"])
        latest_oi = int(resampled_rows[-1].get("oi", 0) or 0)
        min_low = min(float(r["low"]) for r in resampled_rows)
        max_high = max(float(r["high"]) for r in resampled_rows)
        total_vol = sum(int(r.get("volume", 0) or 0) for r in resampled_rows)
        change_pct = (
            round(((latest_close - first_open) / first_open) * 100.0, 2)
            if first_open != 0
            else None
        )

        if mode == "full":
            if limit is not None and limit > 0:
                start_idx = max(0, int(offset))
                rows_out = resampled_rows[start_idx : start_idx + int(limit)]
            else:
                rows_out = resampled_rows
        else:
            if total_rows <= 15:
                rows_out = resampled_rows
            else:
                head = resampled_rows[:5]
                tail = resampled_rows[-10:]
                seen_ts = {r["ts_ist"] for r in head}
                rows_out = head + [r for r in tail if r["ts_ist"] not in seen_ts]

        return {
            "symbol": clean_sym,
            "exchange": clean_exch,
            "timeframe": tf,
            "base_timeframe": base_tf,
            "resampled": tf != base_tf,
            "view_mode": mode,
            "storage_backend": storage_backend,
            "total_rows": total_rows,
            "returned_rows": len(rows_out),
            "is_truncated": len(rows_out) < total_rows,
            "summary": {
                "start_ts": str(resampled_rows[0]["ts_ist"]),
                "end_ts": str(resampled_rows[-1]["ts_ist"]),
                "min_low": round(min_low, 4),
                "max_high": round(max_high, 4),
                "latest_close": round(latest_close, 4),
                "change_pct": change_pct,
                "total_volume": total_vol,
                "latest_oi": latest_oi,
            },
            "rows": rows_out,
        }

    def list_catalog(self) -> List[Dict[str, Any]]:
        """List all locally stored datasets (Parquet + SQLite + asset_data_history) without network calls."""
        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        with self._connect() as conn:
            sqlite_rows = conn.execute(
                """
                SELECT symbol, exchange, timeframe,
                       COUNT(*) AS row_count,
                       MIN(trade_date) AS start_date,
                       MAX(trade_date) AS end_date,
                       MAX(source) AS provider
                FROM candles
                GROUP BY symbol, exchange, timeframe
                """
            ).fetchall()
            for r in sqlite_rows:
                item = dict(r)
                item["storage_backend"] = "sqlite"
                merged[(item["symbol"], item["exchange"], item["timeframe"])] = item

            cat_rows = conn.execute(
                """
                SELECT symbol, exchange, timeframe, row_count, start_date, end_date, provider, storage_backend
                FROM dataset_catalog
                """
            ).fetchall()
            for r in cat_rows:
                item = dict(r)
                merged[(item["symbol"], item["exchange"], item["timeframe"])] = item

            hist_rows = conn.execute(
                """
                SELECT * FROM asset_data_history ORDER BY last_synced_at ASC
                """
            ).fetchall()
            for r in hist_rows:
                h = self._row_to_history_dict(r)
                key = (h["symbol"], h["exchange"], h["timeframe"])
                base = merged.get(key) or {
                    "symbol": h["symbol"],
                    "exchange": h["exchange"],
                    "timeframe": h["timeframe"],
                    "row_count": h["row_count"],
                    "start_date": h["start_date"],
                    "end_date": h["end_date"],
                    "provider": h["provider"],
                    "storage_backend": h["storage_backend"],
                }
                base["adjustment_mode"] = h["adjustment_mode"]
                base["missing_dates"] = h["missing_dates"]
                base["missing_dates_count"] = len(h["missing_dates"])
                base["verified_holidays"] = h["verified_holidays"]
                base["parquet_months"] = h["parquet_months"]
                base["last_synced_at"] = h["last_synced_at"]
                merged[key] = base

        out = list(merged.values())
        out.sort(key=lambda d: (str(d.get("end_date") or ""), str(d.get("symbol") or "")), reverse=True)
        return out

    def list_cached_symbols(
        self,
        exchange: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> List[str]:
        """Return unique cached symbols stored in either Parquet or SQLite."""
        ex_filter = (exchange or "").strip().upper()
        tf_filter = resolve_base_timeframe(timeframe) if timeframe else ""
        syms: Set[str] = set()
        for ds in self.list_catalog():
            if ex_filter and str(ds.get("exchange") or "").upper() != ex_filter:
                continue
            if tf_filter and str(ds.get("timeframe") or "").lower() != tf_filter:
                continue
            sym = str(ds.get("symbol") or "").strip().upper()
            if sym:
                syms.add(sym)
        return sorted(syms)

    def export_csv(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> str:
        full_data = self.get_records(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            view="full",
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts_ist", "trade_date", "open", "high", "low", "close", "volume", "oi", "source"])
        for r in full_data["rows"]:
            writer.writerow(
                [r["ts_ist"], r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["oi"], r["source"]]
            )
        return buf.getvalue()


historical_data_manager = HistoricalDataManager()

__all__ = [
    "DefaultParquetIO",
    "HistoricalDataManager",
    "VALID_TIMEFRAMES",
    "compute_missing_date_ranges",
    "historical_data_manager",
    "normalize_timeframe",
    "resample_ohlcv_rows",
    "resolve_base_timeframe",
]
