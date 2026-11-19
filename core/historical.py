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

VALID_TIMEFRAMES = {
    "1day": {
        "label": "1 Day",
        "upstox": "day",
        "upstox_unit": "days",
        "upstox_interval": "1",
        "zerodha": "day",
        "yahoo": "1d",
        "max_chunk_days": 365,
    },
    "30minute": {
        "label": "30 Min",
        "upstox": "30minute",
        "upstox_unit": "minutes",
        "upstox_interval": "30",
        "zerodha": "30minute",
        "yahoo": "30m",
        "max_chunk_days": 30,
    },
    "1minute": {
        "label": "1 Min",
        "upstox": "1minute",
        "upstox_unit": "minutes",
        "upstox_interval": "1",
        "zerodha": "minute",
        "yahoo": "1m",
        "max_chunk_days": 7,
    },
}

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
) -> List[Tuple[date, date]]:
    """
    Compute minimal contiguous [gap_start, gap_end] date intervals covering ONLY
    absent weekdays (Mon–Fri) not yet in `covered_dates`.

    Properties:
    - Never schedules a chunk that overlaps any covered weekday.
    - Skips isolated weekends automatically when bounded by covered weekdays.
    - Bridges intermediate weekends when adjacent weekdays on both sides are absent
      so consecutive weeks are fetched in minimal API calls.
    """
    if start_date > end_date:
        return []

    gaps: List[Tuple[date, date]] = []
    gap_start: Optional[date] = None
    last_missing_weekday: Optional[date] = None

    cursor = start_date
    while cursor <= end_date:
        is_weekday = cursor.weekday() < 5
        if is_weekday:
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


class HistoricalDataManager:
    """Thread-safe Hybrid SQLite/Parquet OHLCV store and gap-only multi-provider downloader."""

    def __init__(self, db_path: Optional[Path] = None, parquet_dir: Optional[Path] = None) -> None:
        self._explicit_path = db_path
        self._explicit_parquet_dir = parquet_dir
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

    def _parquet_file(self, symbol: str, exchange: str, timeframe: str) -> Path:
        safe_sym = "".join(ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_" for ch in symbol.upper())
        safe_exch = "".join(ch if ch.isalnum() else "_" for ch in exchange.upper()) or "NSE"
        safe_tf = "".join(ch if ch.isalnum() else "_" for ch in timeframe.lower()) or "1day"
        return self.parquet_dir / safe_exch / safe_tf / f"{safe_sym}.parquet"

    def storage_info(self) -> Dict[str, Any]:
        engine = _parquet_engine()
        p_dir = self.parquet_dir
        p_files = 0
        if p_dir.exists():
            p_files = sum(1 for _ in p_dir.rglob("*.parquet"))
        return {
            "configured_mode": config.OHLCV_STORAGE_MODE,
            "active_mode": "hybrid_parquet" if engine is not None else "sqlite_fallback",
            "parquet_available": engine is not None,
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
        conn.commit()

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
                # Never treat today (or yesterday marked 'holiday' without data) as permanently frozen
                if r["status"] == "holiday" and d_val >= (today_ist - timedelta(days=1)):
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
        tf_meta = VALID_TIMEFRAMES[timeframe]
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

        interval = VALID_TIMEFRAMES[timeframe]["zerodha"]
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

        interval = VALID_TIMEFRAMES[timeframe]["yahoo"]
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
        quote = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
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
            o = opens[idx] if idx < len(opens) and opens[idx] is not None else closes[idx]
            h = highs[idx] if idx < len(highs) and highs[idx] is not None else closes[idx]
            l = lows[idx] if idx < len(lows) and lows[idx] is not None else closes[idx]
            v = volumes[idx] if idx < len(volumes) and volumes[idx] is not None else 0
            out.append(
                {
                    "ts_ist": dt_ist.isoformat(timespec="seconds"),
                    "trade_date": dt_ist.date().isoformat(),
                    "open": round(float(o), 4),
                    "high": round(float(h), 4),
                    "low": round(float(l), 4),
                    "close": round(float(closes[idx]), 4),
                    "volume": int(v),
                    "oi": 0,
                }
            )
        return out

    # ── Gap-Only Download Orchestration ───────────────────────────────────

    def download(
        self,
        *,
        symbol: str,
        exchange: str = "NSE",
        timeframe: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        provider: Optional[str] = None,
        instrument_type: Optional[str] = None,
        instrument_key: Optional[str] = None,
        fetcher: Optional[Callable[[str, str, str, date, date], List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """
        Download ONLY absent trading weekdays for (symbol, exchange, timeframe)
        in [from_date, to_date]. Guarantees 0 API calls for dates already covered.
        """
        clean_sym = _normalize_symbol(symbol)
        if not clean_sym:
            raise ValueError("Trading symbol is required.")
        clean_exch = (exchange or "NSE").strip().upper()
        tf = (timeframe or "1day").strip().lower()
        if tf not in VALID_TIMEFRAMES:
            raise ValueError(f"Invalid timeframe '{tf}'. Choose from: {', '.join(VALID_TIMEFRAMES.keys())}")

        today_ist = datetime.now(tz=IST_TZ).date()
        end_d = _parse_iso_date(to_date, default=today_ist)
        if end_d > today_ist:
            end_d = today_ist
        start_d = _parse_iso_date(from_date, default=(end_d - timedelta(days=30)))
        if start_d > end_d:
            raise ValueError(f"from_date ({start_d}) must be on or before to_date ({end_d}).")

        eff_provider = (provider or config.ACTIVE_DATA_CONNECTION or "yfinance").strip().lower()
        covered = self._get_covered_dates(clean_sym, clean_exch, tf, start_d, end_d)

        # Count weekdays requested vs already cached
        total_weekdays = 0
        skipped_existing_days = 0
        cursor = start_d
        while cursor <= end_d:
            if cursor.weekday() < 5:
                total_weekdays += 1
                if cursor in covered:
                    skipped_existing_days += 1
            cursor += timedelta(days=1)

        missing_gaps = compute_missing_date_ranges(start_d, end_d, covered)
        max_chunk = int(VALID_TIMEFRAMES[tf]["max_chunk_days"])

        api_calls_made = 0
        rows_inserted = 0
        fetched_ranges: List[Dict[str, str]] = []

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
                        chunk_rows = fetcher(clean_sym, clean_exch, tf, c_start, c_end)
                    else:
                        chunk_rows = self._fetch_chunk_from_provider(
                            eff_provider,
                            clean_sym,
                            clean_exch,
                            tf,
                            c_start,
                            c_end,
                            instrument_type=instrument_type,
                            instrument_key=instrument_key,
                        )

                    for item in chunk_rows:
                        dates_with_data.add(item["trade_date"])
                        all_candles.append(item)

                    # Mark weekdays inside this fetched chunk so existing/holiday days are never re-called
                    d_cur = c_start
                    while d_cur <= c_end:
                        if d_cur.weekday() < 5 and d_cur not in covered:
                            checked_weekdays.append(d_cur)
                        d_cur += timedelta(days=1)

            engine = _parquet_engine()
            storage_backend = "sqlite"
            parquet_meta: Optional[Tuple[int, str, str]] = None

            with self._connect() as conn:
                if all_candles:
                    if engine is not None:
                        pd, pa, pq = engine
                        p_file = self._parquet_file(clean_sym, clean_exch, tf)
                        p_file.parent.mkdir(parents=True, exist_ok=True)
                        new_df = pd.DataFrame(
                            [
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
                        )
                        frames = []
                        if p_file.exists():
                            frames.append(pq.read_table(p_file).to_pandas())
                        else:
                            # Migrate any legacy SQLite rows on first Parquet write
                            legacy_rows = conn.execute(
                                "SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                                "FROM candles WHERE symbol = ? AND exchange = ? AND timeframe = ?",
                                (clean_sym, clean_exch, tf),
                            ).fetchall()
                            if legacy_rows:
                                frames.append(pd.DataFrame([dict(r) for r in legacy_rows]))
                        frames.append(new_df)
                        merged_df = pd.concat(frames, ignore_index=True)
                        merged_df = merged_df.drop_duplicates(subset=["ts_ist"], keep="last")
                        merged_df = merged_df.sort_values("ts_ist").reset_index(drop=True)
                        tmp_file = p_file.with_suffix(".tmp.parquet")
                        table = pa.Table.from_pandas(merged_df, preserve_index=False)
                        pq.write_table(table, tmp_file, compression="snappy")
                        os.replace(tmp_file, p_file)
                        storage_backend = "parquet"
                        rows_inserted = len(all_candles)
                        parquet_meta = (
                            int(len(merged_df)),
                            str(merged_df["trade_date"].iloc[0]),
                            str(merged_df["trade_date"].iloc[-1]),
                        )
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
                                    tf,
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
                            (clean_sym, clean_exch, tf),
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
                                tf,
                                parquet_meta[0],
                                parquet_meta[1],
                                parquet_meta[2],
                                eff_provider,
                                storage_backend,
                                now_ist_str,
                            ),
                        )

                coverage_rows = []
                for d_item in checked_weekdays:
                    d_iso = d_item.isoformat()
                    if d_iso in dates_with_data:
                        coverage_rows.append((clean_sym, clean_exch, tf, d_iso, "data", now_ist_str))
                    elif d_item < (today_ist - timedelta(days=1)):
                        # Only mark past non-recent weekdays as holiday (never freeze today or yesterday if broker hasn't published EOD yet)
                        coverage_rows.append((clean_sym, clean_exch, tf, d_iso, "holiday", now_ist_str))

                if coverage_rows:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO download_coverage
                        (symbol, exchange, timeframe, covered_date, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        coverage_rows,
                    )
                conn.commit()

        snapshot = self.get_records(
            symbol=clean_sym,
            exchange=clean_exch,
            timeframe=tf,
            from_date=start_d.isoformat(),
            to_date=end_d.isoformat(),
            view="snapshot",
        )

        return {
            "ok": True,
            "symbol": clean_sym,
            "exchange": clean_exch,
            "timeframe": tf,
            "provider": eff_provider,
            "requested_range": {"from": start_d.isoformat(), "to": end_d.isoformat()},
            "total_weekdays_in_range": total_weekdays,
            "existing_days_skipped": skipped_existing_days,
            "missing_days_count": total_weekdays - skipped_existing_days,
            "missing_ranges_fetched": fetched_ranges,
            "api_calls_made": api_calls_made,
            "rows_inserted": rows_inserted,
            "cache_hit_only": api_calls_made == 0,
            "message": (
                f"All {skipped_existing_days} trading days already cached locally — 0 API calls made."
                if api_calls_made == 0
                else f"Fetched {len(fetched_ranges)} missing gap(s) via {api_calls_made} API call(s); skipped {skipped_existing_days} existing day(s)."
            ),
            "snapshot": snapshot,
        }

    # ── Snapshot & Full Data Inspection (0 API calls) ─────────────────────

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
    ) -> Dict[str, Any]:
        clean_sym = _normalize_symbol(symbol)
        clean_exch = (exchange or "NSE").strip().upper()
        tf = (timeframe or "1day").strip().lower()
        mode = "full" if (view or "").strip().lower() == "full" else "snapshot"
        from_iso = _parse_iso_date(from_date).isoformat() if from_date else None
        to_iso = _parse_iso_date(to_date).isoformat() if to_date else None

        # Hybrid read path: prefer Parquet file if present & pyarrow/pandas installed
        p_file = self._parquet_file(clean_sym, clean_exch, tf)
        engine = _parquet_engine()
        if engine is not None and p_file.exists():
            _pd, _pa, pq = engine
            df = pq.read_table(p_file).to_pandas()
            if from_iso:
                df = df[df["trade_date"] >= from_iso]
            if to_iso:
                df = df[df["trade_date"] <= to_iso]
            df = df.sort_values("ts_ist").reset_index(drop=True)
            total_rows = int(len(df))
            if total_rows == 0:
                return {
                    "symbol": clean_sym,
                    "exchange": clean_exch,
                    "timeframe": tf,
                    "view_mode": mode,
                    "storage_backend": "parquet",
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

            first_open = float(df["open"].iloc[0])
            latest_close = float(df["close"].iloc[-1])
            latest_oi = int(df["oi"].iloc[-1]) if "oi" in df.columns else 0
            change_pct = (
                round(((latest_close - first_open) / first_open) * 100.0, 2)
                if first_open != 0
                else None
            )
            if mode == "full":
                sliced = df.iloc[max(0, int(offset)) : (max(0, int(offset)) + int(limit))] if (limit and limit > 0) else df
            else:
                if total_rows <= 15:
                    sliced = df
                else:
                    sliced = _pd.concat([df.iloc[:5], df.iloc[-10:]], ignore_index=True).drop_duplicates(subset=["ts_ist"])
            rows_out = sliced.to_dict(orient="records")
            return {
                "symbol": clean_sym,
                "exchange": clean_exch,
                "timeframe": tf,
                "view_mode": mode,
                "storage_backend": "parquet",
                "total_rows": total_rows,
                "returned_rows": len(rows_out),
                "is_truncated": len(rows_out) < total_rows,
                "summary": {
                    "start_ts": str(df["ts_ist"].iloc[0]),
                    "end_ts": str(df["ts_ist"].iloc[-1]),
                    "min_low": round(float(df["low"].min()), 4),
                    "max_high": round(float(df["high"].max()), 4),
                    "latest_close": round(latest_close, 4),
                    "change_pct": change_pct,
                    "total_volume": int(df["volume"].sum()),
                    "latest_oi": latest_oi,
                },
                "rows": rows_out,
            }

        clauses = ["symbol = ?", "exchange = ?", "timeframe = ?"]
        params: List[Any] = [clean_sym, clean_exch, tf]
        if from_iso:
            clauses.append("trade_date >= ?")
            params.append(from_iso)
        if to_iso:
            clauses.append("trade_date <= ?")
            params.append(to_iso)

        where_sql = " AND ".join(clauses)

        with self._connect() as conn:
            agg = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt,
                       MIN(ts_ist) AS min_ts,
                       MAX(ts_ist) AS max_ts,
                       MIN(low) AS min_low,
                       MAX(high) AS max_high,
                       SUM(volume) AS total_volume
                FROM candles
                WHERE {where_sql}
                """,
                params,
            ).fetchone()

            total_rows = int(agg["cnt"] or 0)
            first_row = conn.execute(
                f"SELECT open, close FROM candles WHERE {where_sql} ORDER BY ts_ist ASC LIMIT 1",
                params,
            ).fetchone()
            last_row = conn.execute(
                f"SELECT close, oi FROM candles WHERE {where_sql} ORDER BY ts_ist DESC LIMIT 1",
                params,
            ).fetchone()

            rows_out: List[Dict[str, Any]] = []
            if total_rows > 0:
                if mode == "full":
                    sql = (
                        f"SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                        f"FROM candles WHERE {where_sql} ORDER BY ts_ist ASC"
                    )
                    q_params = list(params)
                    if limit is not None and limit > 0:
                        sql += " LIMIT ? OFFSET ?"
                        q_params.extend([int(limit), max(0, int(offset))])
                    rows_out = [dict(r) for r in conn.execute(sql, q_params).fetchall()]
                else:
                    if total_rows <= 15:
                        rows_out = [
                            dict(r)
                            for r in conn.execute(
                                f"SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                                f"FROM candles WHERE {where_sql} ORDER BY ts_ist ASC",
                                params,
                            ).fetchall()
                        ]
                    else:
                        head = [
                            dict(r)
                            for r in conn.execute(
                                f"SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                                f"FROM candles WHERE {where_sql} ORDER BY ts_ist ASC LIMIT 5",
                                params,
                            ).fetchall()
                        ]
                        tail = list(
                            reversed(
                                [
                                    dict(r)
                                    for r in conn.execute(
                                        f"SELECT ts_ist, trade_date, open, high, low, close, volume, oi, source "
                                        f"FROM candles WHERE {where_sql} ORDER BY ts_ist DESC LIMIT 10",
                                        params,
                                    ).fetchall()
                                ]
                            )
                        )
                        seen_ts = {r["ts_ist"] for r in head}
                        rows_out = head + [r for r in tail if r["ts_ist"] not in seen_ts]

        first_open = float(first_row["open"]) if first_row else None
        latest_close = float(last_row["close"]) if last_row else None
        latest_oi = int(last_row["oi"] or 0) if last_row else 0
        change_pct = (
            round(((latest_close - first_open) / first_open) * 100.0, 2)
            if (first_open and latest_close is not None and first_open != 0)
            else None
        )

        return {
            "symbol": clean_sym,
            "exchange": clean_exch,
            "timeframe": tf,
            "view_mode": mode,
            "storage_backend": "sqlite",
            "total_rows": total_rows,
            "returned_rows": len(rows_out),
            "is_truncated": len(rows_out) < total_rows,
            "summary": {
                "start_ts": agg["min_ts"],
                "end_ts": agg["max_ts"],
                "min_low": round(float(agg["min_low"]), 4) if agg["min_low"] is not None else None,
                "max_high": round(float(agg["max_high"]), 4) if agg["max_high"] is not None else None,
                "latest_close": round(latest_close, 4) if latest_close is not None else None,
                "change_pct": change_pct,
                "total_volume": int(agg["total_volume"] or 0),
                "latest_oi": latest_oi,
            },
            "rows": rows_out,
        }

    def list_catalog(self) -> List[Dict[str, Any]]:
        """List all locally stored datasets (Parquet + SQLite) without network calls."""
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
        tf_filter = (timeframe or "").strip().lower()
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
    "HistoricalDataManager",
    "VALID_TIMEFRAMES",
    "compute_missing_date_ranges",
    "historical_data_manager",
]
