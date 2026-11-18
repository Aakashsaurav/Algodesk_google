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

VALID_TIMEFRAMES = {
    "1day": {"label": "1 Day", "upstox": "day", "zerodha": "day", "yahoo": "1d", "max_chunk_days": 365},
    "30minute": {"label": "30 Min", "upstox": "30minute", "zerodha": "30minute", "yahoo": "30m", "max_chunk_days": 30},
    "1minute": {"label": "1 Min", "upstox": "1minute", "zerodha": "minute", "yahoo": "1m", "max_chunk_days": 7},
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
    """Thread-safe SQLite OHLCV store and gap-only multi-provider downloader."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._explicit_path = db_path
        self._lock = threading.RLock()
        self._initialized_paths: set[Path] = set()

    @property
    def db_path(self) -> Path:
        return self._explicit_path or config.OHLCV_DB_PATH

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
        conn.commit()

    def _get_covered_dates(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start_d: date,
        end_d: date,
    ) -> Set[date]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT covered_date FROM download_coverage
                WHERE symbol = ? AND exchange = ? AND timeframe = ?
                  AND covered_date >= ? AND covered_date <= ?
                """,
                (symbol, exchange, timeframe, start_d.isoformat(), end_d.isoformat()),
            ).fetchall()
        covered: Set[date] = set()
        for r in rows:
            try:
                covered.add(date.fromisoformat(r["covered_date"]))
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
        interval = VALID_TIMEFRAMES[timeframe]["upstox"]
        encoded_key = urllib.parse.quote(ikey, safe="")
        url = (
            f"{config.UPSTOX_BASE_URL}/v2/historical-candle/"
            f"{encoded_key}/{interval}/{chunk_end.isoformat()}/{chunk_start.isoformat()}"
        )
        headers = {"Accept": "application/json", "User-Agent": "AlgoDesk/3.0"}
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
            raise RuntimeError(f"Upstox historical API HTTP {exc.code}: {err_body[:180]}") from exc

        candles = ((payload.get("data") or {}).get("candles")) or []
        out: List[Dict[str, Any]] = []
        for row in candles:
            if not row or len(row) < 6:
                continue
            ts_raw = str(row[0])
            dt_ist = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(IST_TZ)
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

            with self._connect() as conn:
                if all_candles:
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

                conn.executemany(
                    """
                    INSERT OR REPLACE INTO download_coverage
                    (symbol, exchange, timeframe, covered_date, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            clean_sym,
                            clean_exch,
                            tf,
                            d_item.isoformat(),
                            "data" if d_item.isoformat() in dates_with_data else "holiday",
                            now_ist_str,
                        )
                        for d_item in checked_weekdays
                    ],
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

        clauses = ["symbol = ?", "exchange = ?", "timeframe = ?"]
        params: List[Any] = [clean_sym, clean_exch, tf]
        if from_date:
            clauses.append("trade_date >= ?")
            params.append(_parse_iso_date(from_date).isoformat())
        if to_date:
            clauses.append("trade_date <= ?")
            params.append(_parse_iso_date(to_date).isoformat())

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
                    # Snapshot: up to 15 representative bars (first 5 + latest 10 if larger)
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
        """List all locally stored datasets in ohlcv.db without making any network calls."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, exchange, timeframe,
                       COUNT(*) AS row_count,
                       MIN(trade_date) AS start_date,
                       MAX(trade_date) AS end_date,
                       MAX(source) AS provider
                FROM candles
                GROUP BY symbol, exchange, timeframe
                ORDER BY MAX(trade_date) DESC, symbol ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

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
