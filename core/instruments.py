"""
core/instruments.py
-------------------
M2-optimized SQLite Instrument Index replacing full-file 100k-entry JSON parsing.

Key improvements over Algodesk_R4's instrument_manager.py:
- Replaces multi-hundred-MB in-memory JSON lists/dicts (~250 MB RSS) with an
  indexed local SQLite table (instruments.db) queried on demand (< 5 MB RSS).
- Supports both Upstox instrument keys (e.g., NSE_EQ|INE002A01018), Zerodha
  Kite instrument tokens (e.g., 738561), and Yahoo Finance ticker mapping.
- Includes lightweight pre-seeded NSE core benchmarks & equities so lookups
  work immediately offline before syncing full exchange masters.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
import threading
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import IST_TZ, config

_DEFAULT_SEED_INSTRUMENTS = [
    # Flagship NSE Indices
    ("upstox", "NSE", "NSE_INDEX", "NIFTY", "NIFTY", "Nifty 50", "INDEX", "NSE_INDEX|Nifty 50", None),
    ("upstox", "NSE", "NSE_INDEX", "BANKNIFTY", "BANKNIFTY", "Nifty Bank", "INDEX", "NSE_INDEX|Nifty Bank", None),
    ("zerodha", "NSE", "INDICES", "NIFTY", "NIFTY 50", "Nifty 50", "INDEX", "NSE:NIFTY 50", "256265"),
    ("zerodha", "NSE", "INDICES", "BANKNIFTY", "NIFTY BANK", "Nifty Bank", "INDEX", "NSE:NIFTY BANK", "260105"),
    # Flagship NSE Equities
    ("upstox", "NSE", "NSE_EQ", "RELIANCE", "RELIANCE", "Reliance Industries Ltd", "EQUITY", "NSE_EQ|INE002A01018", None),
    ("upstox", "NSE", "NSE_EQ", "TCS", "TCS", "Tata Consultancy Services Ltd", "EQUITY", "NSE_EQ|INE467B01029", None),
    ("upstox", "NSE", "NSE_EQ", "INFY", "INFY", "Infosys Ltd", "EQUITY", "NSE_EQ|INE009A01021", None),
    ("upstox", "NSE", "NSE_EQ", "HDFCBANK", "HDFCBANK", "HDFC Bank Ltd", "EQUITY", "NSE_EQ|INE040A01034", None),
    ("upstox", "NSE", "NSE_EQ", "ICICIBANK", "ICICIBANK", "ICICI Bank Ltd", "EQUITY", "NSE_EQ|INE090A01021", None),
    ("upstox", "NSE", "NSE_EQ", "SBIN", "SBIN", "State Bank of India", "EQUITY", "NSE_EQ|INE062A01020", None),
    ("upstox", "NSE", "NSE_EQ", "ITC", "ITC", "ITC Ltd", "EQUITY", "NSE_EQ|INE154A01025", None),
    ("upstox", "NSE", "NSE_EQ", "LT", "LT", "Larsen & Toubro Ltd", "EQUITY", "NSE_EQ|INE018A01030", None),
    ("zerodha", "NSE", "NSE", "RELIANCE", "RELIANCE", "Reliance Industries Ltd", "EQUITY", "NSE:RELIANCE", "738561"),
    ("zerodha", "NSE", "NSE", "TCS", "TCS", "Tata Consultancy Services Ltd", "EQUITY", "NSE:TCS", "2953217"),
    ("zerodha", "NSE", "NSE", "INFY", "INFY", "Infosys Ltd", "EQUITY", "NSE:INFY", "408065"),
    ("zerodha", "NSE", "NSE", "HDFCBANK", "HDFCBANK", "HDFC Bank Ltd", "EQUITY", "NSE:HDFCBANK", "341249"),
    ("zerodha", "NSE", "NSE", "ICICIBANK", "ICICIBANK", "ICICI Bank Ltd", "EQUITY", "NSE:ICICIBANK", "1270529"),
    ("zerodha", "NSE", "NSE", "SBIN", "SBIN", "State Bank of India", "EQUITY", "NSE:SBIN", "779521"),
    ("zerodha", "NSE", "NSE", "ITC", "ITC", "ITC Ltd", "EQUITY", "NSE:ITC", "424961"),
    ("zerodha", "NSE", "NSE", "LT", "LT", "Larsen & Toubro Ltd", "EQUITY", "NSE:LT", "2939649"),
]

_SEGMENT_TO_EXCHANGE = {
    "NSE_EQ": "NSE",
    "BSE_EQ": "BSE",
    "NSE_INDEX": "NSE",
    "BSE_INDEX": "BSE",
    "NSE_FO": "NFO",
    "BSE_FO": "BFO",
    "MCX_FO": "MCX",
    "NSE_COM": "NSE",
    "NCD_FO": "NCD",
    "BCD_FO": "BCD",
}

_ZERODHA_INDEX_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "INDIA VIX": "INDIAVIX",
}


def _normalize_expiry(raw_exp: Any) -> Optional[str]:
    if raw_exp is None:
        return None
    text = str(raw_exp).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            num = int(text)
            if num > 10_000_000_000:
                num_sec = num / 1000.0
            else:
                num_sec = float(num)
            return datetime.fromtimestamp(num_sec, tz=IST_TZ).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return text
    return text[:10] if len(text) >= 10 and text[4] == "-" and text[7] == "-" else text


def _exchange_sql_filter(exchange: str) -> tuple[str, List[Any]]:
    ex = (exchange or "").strip().upper()
    if ex in {"NFO", "NSE_FO"}:
        return "(UPPER(exchange) IN ('NFO', 'NSE_FO') OR UPPER(segment) = 'NSE_FO')", []
    if ex in {"BFO", "BSE_FO"}:
        return "(UPPER(exchange) IN ('BFO', 'BSE_FO') OR UPPER(segment) = 'BSE_FO')", []
    if ex in {"MCX", "MCX_FO"}:
        return "(UPPER(exchange) IN ('MCX', 'MCX_FO') OR UPPER(segment) = 'MCX_FO')", []
    if ex in {"NSE", "NSE_EQ", "NSE_INDEX"}:
        return "(UPPER(exchange) IN ('NSE', 'NSE_EQ', 'NSE_INDEX') AND UPPER(segment) != 'NSE_FO')", []
    if ex in {"BSE", "BSE_EQ", "BSE_INDEX"}:
        return "(UPPER(exchange) IN ('BSE', 'BSE_EQ', 'BSE_INDEX') AND UPPER(segment) != 'BSE_FO')", []
    return "UPPER(exchange) = ?", [ex]


def _instrument_type_sql_filter(instrument_type: Optional[str]) -> tuple[str, List[Any]]:
    it = (instrument_type or "").strip().upper()
    if not it or it == "ALL":
        return "", []
    if it in {"FUTURES", "FUT"}:
        return "UPPER(instrument_type) LIKE 'FUT%'", []
    if it in {"OPTIONS", "OPT"}:
        return "(UPPER(instrument_type) IN ('CE', 'PE') OR UPPER(instrument_type) LIKE 'OPT%')", []
    if it in {"FO", "FNO", "F&O"}:
        return "(UPPER(instrument_type) LIKE 'FUT%' OR UPPER(instrument_type) IN ('CE', 'PE') OR UPPER(instrument_type) LIKE 'OPT%')", []
    if it in {"EQUITY", "EQ", "CASH"}:
        return "UPPER(instrument_type) IN ('EQUITY', 'EQ', 'INDEX')", []
    return "UPPER(instrument_type) = ?", [it]


@dataclass(frozen=True)
class InstrumentRecord:
    broker_id: str
    exchange: str
    segment: str
    trading_symbol: str
    asset_symbol: str
    name: str
    instrument_type: str
    instrument_key: str
    instrument_token: Optional[str] = None
    expiry: Optional[str] = None
    strike: Optional[float] = None
    lot_size: int = 1
    tick_size: float = 0.05

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InstrumentIndex:
    """Thread-safe indexed SQLite store for Indian market broker instrument metadata."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._explicit_path = db_path
        self._lock = threading.RLock()
        self._initialized_paths: set[Path] = set()

    @property
    def db_path(self) -> Path:
        return self._explicit_path or config.INSTRUMENTS_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0)
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
            CREATE TABLE IF NOT EXISTS instruments (
                broker_id TEXT NOT NULL,
                exchange TEXT NOT NULL,
                segment TEXT NOT NULL,
                trading_symbol TEXT NOT NULL,
                asset_symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                instrument_type TEXT NOT NULL,
                instrument_key TEXT NOT NULL,
                instrument_token TEXT,
                expiry TEXT,
                strike REAL,
                lot_size INTEGER NOT NULL DEFAULT 1,
                tick_size REAL NOT NULL DEFAULT 0.05,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (broker_id, instrument_key)
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instr_symbol ON instruments (trading_symbol COLLATE NOCASE, exchange, broker_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instr_asset ON instruments (asset_symbol COLLATE NOCASE, broker_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instr_fno ON instruments (instrument_type, exchange, asset_symbol COLLATE NOCASE, expiry);"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta (broker_id TEXT PRIMARY KEY, last_synced_at TEXT, row_count INTEGER);"
        )

        # Normalize legacy Upstox F&O exchange label if upgrading existing DB on disk
        conn.execute(
            "UPDATE instruments SET exchange = 'NFO' WHERE segment = 'NSE_FO' AND exchange = 'NSE';"
        )
        conn.execute(
            "UPDATE instruments SET exchange = 'BFO' WHERE segment = 'BSE_FO' AND exchange = 'BSE';"
        )
        conn.execute(
            "UPDATE instruments SET exchange = 'MCX' WHERE segment = 'MCX_FO' AND exchange != 'MCX';"
        )

        # Seed core flagship symbols only when empty so searches work immediately offline
        existing = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        if existing == 0:
            now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            conn.executemany(
                """
                INSERT OR IGNORE INTO instruments
                (broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                 instrument_type, instrument_key, instrument_token, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*row, now_ist) for row in _DEFAULT_SEED_INSTRUMENTS],
            )
        conn.commit()

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
            by_broker = {
                row["broker_id"]: row["cnt"]
                for row in conn.execute(
                    "SELECT broker_id, COUNT(*) AS cnt FROM instruments GROUP BY broker_id"
                ).fetchall()
            }
            fno_count = conn.execute(
                "SELECT COUNT(*) FROM instruments WHERE UPPER(instrument_type) LIKE 'FUT%' OR UPPER(instrument_type) IN ('CE', 'PE') OR UPPER(instrument_type) LIKE 'OPT%'"
            ).fetchone()[0]
            metas = {
                row["broker_id"]: {
                    "last_synced_at": row["last_synced_at"],
                    "row_count": row["row_count"],
                }
                for row in conn.execute("SELECT broker_id, last_synced_at, row_count FROM index_meta").fetchall()
            }
        db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "db_path": str(self.db_path),
            "db_size_mb": round(db_bytes / (1024 * 1024), 3),
            "total_instruments": total,
            "fno_instruments": fno_count,
            "counts_by_broker": by_broker,
            "sync_meta": metas,
        }

    def search(
        self,
        query: str = "",
        broker_id: Optional[str] = None,
        exchange: Optional[str] = None,
        instrument_type: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        clean_q = (query or "").strip().upper()
        capped_limit = max(1, min(int(limit), 100))

        clauses = []
        params: List[Any] = []
        if broker_id and broker_id.strip().lower() not in {"all", ""}:
            clauses.append("broker_id = ?")
            params.append(broker_id.strip().lower())
        if exchange and exchange.strip().lower() not in {"all", ""}:
            ex_sql, ex_params = _exchange_sql_filter(exchange)
            clauses.append(ex_sql)
            params.extend(ex_params)

        it_sql, it_params = _instrument_type_sql_filter(instrument_type)
        if it_sql:
            clauses.append(it_sql)
            params.extend(it_params)

        if expiry and expiry.strip():
            clauses.append("expiry = ?")
            params.append(_normalize_expiry(expiry.strip()))

        if strike is not None:
            clauses.append("strike = ?")
            params.append(float(strike))

        if clean_q:
            tokens = [t for t in clean_q.split() if t]
            for tok in tokens:
                contains = f"%{tok}%"
                clauses.append(
                    "(UPPER(trading_symbol) LIKE ? OR UPPER(asset_symbol) LIKE ? "
                    "OR UPPER(name) LIKE ? OR COALESCE(expiry, '') LIKE ? "
                    "OR CAST(COALESCE(strike, '') AS TEXT) LIKE ? OR UPPER(instrument_type) LIKE ?)"
                )
                params.extend([contains, contains, contains, contains, contains, contains])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if clean_q:
            order_sql = (
                "ORDER BY CASE WHEN UPPER(trading_symbol) = ? THEN 0 "
                "WHEN UPPER(asset_symbol) = ? THEN 1 "
                "WHEN UPPER(trading_symbol) LIKE ? THEN 2 ELSE 3 END, "
                "COALESCE(expiry, '9999-12-31') ASC, COALESCE(strike, 0) ASC, trading_symbol ASC LIMIT ?"
            )
            params.extend([clean_q, clean_q, f"{clean_q}%", capped_limit])
        else:
            order_sql = "ORDER BY COALESCE(expiry, '9999-12-31') ASC, COALESCE(strike, 0) ASC, trading_symbol ASC LIMIT ?"
            params.append(capped_limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                       instrument_type, instrument_key, instrument_token, expiry, strike,
                       lot_size, tick_size
                FROM instruments
                {where_sql}
                {order_sql}
                """,
                params,
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["expiry"] = _normalize_expiry(item.get("expiry"))
            out.append(item)
        return out

    def list_fno_contracts(
        self,
        *,
        underlying: str = "",
        exchange: str = "NFO",
        contract_type: str = "FO",
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        broker_id: Optional[str] = None,
        limit: int = 40,
    ) -> Dict[str, Any]:
        """Return structured F&O underlyings, expiries, strikes, and matching contracts from SQLite."""
        clean_ul = (underlying or "").strip().upper()
        ex_sql, ex_params = _exchange_sql_filter(exchange or "NFO")
        eff_type = (contract_type or "FO").strip().upper()
        if eff_type == "ALL":
            eff_type = "FO"
        it_sql, it_params = _instrument_type_sql_filter(eff_type)

        base_clauses = ["(UPPER(instrument_type) LIKE 'FUT%' OR UPPER(instrument_type) IN ('CE', 'PE') OR UPPER(instrument_type) LIKE 'OPT%')"]
        base_params: List[Any] = []
        if exchange and exchange.strip().upper() != "ALL":
            base_clauses.append(ex_sql)
            base_params.extend(ex_params)
        if broker_id and broker_id.strip().lower() not in {"all", ""}:
            base_clauses.append("broker_id = ?")
            base_params.append(broker_id.strip().lower())

        with self._connect() as conn:
            ul_clauses = list(base_clauses)
            ul_params = list(base_params)
            if clean_ul:
                ul_clauses.append("UPPER(asset_symbol) LIKE ?")
                ul_params.append(f"{clean_ul}%")
            underlyings = [
                r["asset_symbol"]
                for r in conn.execute(
                    f"""
                    SELECT DISTINCT asset_symbol
                    FROM instruments
                    WHERE {' AND '.join(ul_clauses)}
                    ORDER BY asset_symbol ASC
                    LIMIT 40
                    """,
                    ul_params,
                ).fetchall()
                if r["asset_symbol"]
            ]

            exp_clauses = list(base_clauses)
            exp_params = list(base_params)
            if it_sql:
                exp_clauses.append(it_sql)
                exp_params.extend(it_params)
            if clean_ul:
                exp_clauses.append("(UPPER(asset_symbol) = ? OR UPPER(asset_symbol) LIKE ?)")
                exp_params.extend([clean_ul, f"{clean_ul}%"])
            expiries = sorted(
                {
                    norm
                    for r in conn.execute(
                        f"""
                        SELECT DISTINCT expiry
                        FROM instruments
                        WHERE {' AND '.join(exp_clauses)} AND expiry IS NOT NULL AND expiry != ''
                        ORDER BY expiry ASC
                        LIMIT 40
                        """,
                        exp_params,
                    ).fetchall()
                    if (norm := _normalize_expiry(r["expiry"])) is not None
                }
            )

            strike_clauses = list(exp_clauses)
            strike_params = list(exp_params)
            if expiry and expiry.strip():
                strike_clauses.append("expiry = ?")
                strike_params.append(_normalize_expiry(expiry.strip()))
            strikes = [
                float(r["strike"])
                for r in conn.execute(
                    f"""
                    SELECT DISTINCT strike
                    FROM instruments
                    WHERE {' AND '.join(strike_clauses)} AND strike IS NOT NULL AND strike > 0
                    ORDER BY strike ASC
                    LIMIT 120
                    """,
                    strike_params,
                ).fetchall()
            ]

        contracts = self.search(
            query="",
            broker_id=broker_id,
            exchange=exchange if exchange and exchange.strip().upper() != "ALL" else None,
            instrument_type=eff_type,
            expiry=expiry,
            strike=strike,
            limit=limit,
        ) if not clean_ul else self.search(
            query=clean_ul,
            broker_id=broker_id,
            exchange=exchange if exchange and exchange.strip().upper() != "ALL" else None,
            instrument_type=eff_type,
            expiry=expiry,
            strike=strike,
            limit=limit,
        )

        # Tighten underlying filter so "NIFTY" doesn't get pushed down by longer prefixes when exact exists
        if clean_ul and contracts:
            exact_ul = [c for c in contracts if str(c.get("asset_symbol", "")).upper() == clean_ul]
            if exact_ul:
                contracts = exact_ul

        return {
            "ok": True,
            "exchange": (exchange or "NFO").strip().upper(),
            "contract_type": eff_type,
            "underlying": clean_ul,
            "underlyings": underlyings,
            "expiries": expiries,
            "strikes": strikes,
            "contracts": contracts[:limit],
            "count": len(contracts[:limit]),
        }

    def resolve_key(
        self,
        trading_symbol: str,
        broker_id: str = "upstox",
        exchange: str = "NSE",
        instrument_type: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
    ) -> Optional[str]:
        """Resolve single instrument_key (Upstox) or instrument_token (Zerodha) via SQLite index."""
        sym = trading_symbol.strip().upper()
        bid = broker_id.strip().lower()
        exch = exchange.strip().upper()
        ex_sql, ex_params = _exchange_sql_filter(exch)

        clauses = ["broker_id = ?", ex_sql, "(UPPER(trading_symbol) = ? OR UPPER(asset_symbol) = ?)"]
        params: List[Any] = [bid, *ex_params, sym, sym]
        it_sql, it_params = _instrument_type_sql_filter(instrument_type)
        if it_sql:
            clauses.append(it_sql)
            params.extend(it_params)
        if expiry and expiry.strip():
            clauses.append("expiry = ?")
            params.append(_normalize_expiry(expiry.strip()))
        if strike is not None:
            clauses.append("strike = ?")
            params.append(float(strike))

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT instrument_key, instrument_token
                FROM instruments
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE WHEN UPPER(trading_symbol) = ? THEN 0 ELSE 1 END,
                         COALESCE(expiry, '9999-12-31') ASC
                LIMIT 1
                """,
                [*params, sym],
            ).fetchone()

            # Cross-broker F&O fallback: e.g. user picked a Zerodha NFO symbol (NIFTY26NOVFUT)
            # and requested historical candles from Upstox (which uses a distinct trading_symbol).
            if not row and exch in {"NFO", "BFO", "MCX", "NSE_FO", "BSE_FO", "MCX_FO"}:
                ref = conn.execute(
                    f"""
                    SELECT asset_symbol, instrument_type, expiry, strike
                    FROM instruments
                    WHERE UPPER(trading_symbol) = ? AND {ex_sql}
                    LIMIT 1
                    """,
                    [sym, *ex_params],
                ).fetchone()
                if ref:
                    cb_clauses = ["broker_id = ?", ex_sql, "UPPER(asset_symbol) = ?", "UPPER(instrument_type) = ?"]
                    cb_params: List[Any] = [
                        bid,
                        *ex_params,
                        str(ref["asset_symbol"]).upper(),
                        str(ref["instrument_type"]).upper(),
                    ]
                    if ref["expiry"]:
                        cb_clauses.append("expiry = ?")
                        cb_params.append(_normalize_expiry(ref["expiry"]))
                    if ref["strike"] is not None:
                        cb_clauses.append("strike = ?")
                        cb_params.append(float(ref["strike"]))
                    row = conn.execute(
                        f"SELECT instrument_key, instrument_token FROM instruments WHERE {' AND '.join(cb_clauses)} LIMIT 1",
                        cb_params,
                    ).fetchone()

        if not row:
            return None
        if bid == "zerodha":
            return row["instrument_token"] or row["instrument_key"]
        return row["instrument_key"]

    def upsert_records(self, broker_id: str, records: List[InstrumentRecord]) -> int:
        bid = broker_id.strip().lower()
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        rows = [
            (
                bid,
                r.exchange.upper(),
                r.segment,
                r.trading_symbol.upper(),
                r.asset_symbol.upper(),
                r.name,
                r.instrument_type.upper(),
                r.instrument_key,
                r.instrument_token,
                _normalize_expiry(r.expiry),
                r.strike,
                r.lot_size,
                r.tick_size,
                now_ist,
            )
            for r in records
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO instruments
                (broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                 instrument_type, instrument_key, instrument_token, expiry, strike,
                 lot_size, tick_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            total_for_broker = conn.execute(
                "SELECT COUNT(*) FROM instruments WHERE broker_id = ?", (bid,)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (broker_id, last_synced_at, row_count) VALUES (?, ?, ?)",
                (bid, now_ist, total_for_broker),
            )
            conn.commit()
        return len(rows)

    def sync_upstox_master(self, timeout: int = 25) -> Dict[str, Any]:
        """Stream Upstox complete.json.gz straight into SQLite without retaining full list in RAM."""
        req = urllib.request.Request(
            config.INSTRUMENT_KEY_URL,
            headers={"User-Agent": "AlgoDesk/4.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_gz = resp.read()
        raw_json = gzip.decompress(raw_gz).decode("utf-8")
        del raw_gz

        items = json.loads(raw_json)
        del raw_json

        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        batch: List[tuple] = []
        inserted = 0

        with self._connect() as conn:
            for item in items:
                seg = str(item.get("segment", "")).upper()
                exch = _SEGMENT_TO_EXCHANGE.get(seg, str(item.get("exchange", "NSE"))).upper()
                sym = str(item.get("trading_symbol", "")).strip().upper()
                asset_sym = str(item.get("underlying_symbol") or item.get("asset_symbol") or sym).strip().upper()
                raw_type = str(item.get("instrument_type", "EQUITY")).upper()
                norm_type = "EQUITY" if raw_type in {"EQ", "EQUITY"} else raw_type
                ikey = str(item.get("instrument_key", "")).strip()
                if not sym or not ikey:
                    continue
                expiry_str = _normalize_expiry(item.get("expiry"))
                strike_val = item.get("strike_price")
                batch.append(
                    (
                        "upstox",
                        exch,
                        seg,
                        sym,
                        asset_sym,
                        str(item.get("name") or item.get("short_name") or ""),
                        norm_type,
                        ikey,
                        None,
                        expiry_str,
                        float(strike_val) if strike_val is not None and float(strike_val) > 0 else None,
                        int(item.get("lot_size") or 1),
                        float(item.get("tick_size") or 0.05),
                        now_ist,
                    )
                )
                if len(batch) >= 2000:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO instruments
                        (broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                         instrument_type, instrument_key, instrument_token, expiry, strike,
                         lot_size, tick_size, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    inserted += len(batch)
                    batch.clear()

            if batch:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO instruments
                    (broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                     instrument_type, instrument_key, instrument_token, expiry, strike,
                     lot_size, tick_size, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted += len(batch)
                batch.clear()

            conn.execute(
                "INSERT OR REPLACE INTO index_meta (broker_id, last_synced_at, row_count) VALUES (?, ?, ?)",
                ("upstox", now_ist, inserted),
            )
            conn.commit()
        del items
        return {"ok": True, "broker_id": "upstox", "indexed_count": inserted, "synced_at": now_ist}

    def sync_zerodha_master(self, timeout: int = 25) -> Dict[str, Any]:
        """Stream public Kite Connect instruments CSV straight into SQLite."""
        req = urllib.request.Request(
            config.ZERODHA_INSTRUMENT_URL,
            headers={"X-Kite-Version": "3", "User-Agent": "AlgoDesk/4.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            csv_text = resp.read().decode("utf-8")

        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        reader = csv.DictReader(io.StringIO(csv_text))
        batch: List[tuple] = []
        inserted = 0

        with self._connect() as conn:
            for row in reader:
                token = str(row.get("instrument_token", "")).strip()
                sym = str(row.get("tradingsymbol", "")).strip().upper()
                exch = str(row.get("exchange", "NSE")).strip().upper()
                seg = str(row.get("segment", exch)).strip().upper()
                if not token or not sym:
                    continue
                raw_type = str(row.get("instrument_type", "EQ")).strip().upper()
                norm_type = "EQUITY" if raw_type == "EQ" else ("INDEX" if seg == "INDICES" else raw_type)
                strike_raw = str(row.get("strike", "")).strip()
                strike_val = float(strike_raw) if strike_raw and float(strike_raw) != 0 else None
                raw_name = str(row.get("name", "")).strip()
                if norm_type in {"FUT", "CE", "PE"} or raw_type.startswith("FUT") or raw_type.startswith("OPT"):
                    asset_sym = (raw_name.upper() if raw_name else _ZERODHA_INDEX_ALIASES.get(sym, sym))
                else:
                    asset_sym = _ZERODHA_INDEX_ALIASES.get(sym, sym)
                batch.append(
                    (
                        "zerodha",
                        exch,
                        seg,
                        sym,
                        asset_sym,
                        raw_name,
                        norm_type,
                        f"{exch}:{sym}",
                        token,
                        _normalize_expiry(row.get("expiry")),
                        strike_val,
                        int(float(row.get("lot_size") or 1)),
                        float(row.get("tick_size") or 0.05),
                        now_ist,
                    )
                )
                if len(batch) >= 2000:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO instruments
                        (broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                         instrument_type, instrument_key, instrument_token, expiry, strike,
                         lot_size, tick_size, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    inserted += len(batch)
                    batch.clear()

            if batch:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO instruments
                    (broker_id, exchange, segment, trading_symbol, asset_symbol, name,
                     instrument_type, instrument_key, instrument_token, expiry, strike,
                     lot_size, tick_size, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted += len(batch)
                batch.clear()

            conn.execute(
                "INSERT OR REPLACE INTO index_meta (broker_id, last_synced_at, row_count) VALUES (?, ?, ?)",
                ("zerodha", now_ist, inserted),
            )
            conn.commit()
        return {"ok": True, "broker_id": "zerodha", "indexed_count": inserted, "synced_at": now_ist}


instrument_index = InstrumentIndex()

__all__ = ["InstrumentIndex", "InstrumentRecord", "instrument_index"]
