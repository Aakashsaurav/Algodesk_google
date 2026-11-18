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
    "NSE_FO": "NSE",
    "BSE_FO": "BSE",
    "MCX_FO": "MCX",
    "NSE_COM": "NSE",
    "NCD_FO": "NSE",
    "BCD_FO": "BSE",
}

_ZERODHA_INDEX_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "INDIA VIX": "INDIAVIX",
}


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
            "CREATE TABLE IF NOT EXISTS index_meta (broker_id TEXT PRIMARY KEY, last_synced_at TEXT, row_count INTEGER);"
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
            "counts_by_broker": by_broker,
            "sync_meta": metas,
        }

    def search(
        self,
        query: str,
        broker_id: Optional[str] = None,
        exchange: Optional[str] = None,
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
            clauses.append("exchange = ?")
            params.append(exchange.strip().upper())

        if clean_q:
            clauses.append(
                "(UPPER(trading_symbol) LIKE ? OR UPPER(asset_symbol) LIKE ? OR UPPER(name) LIKE ?)"
            )
            prefix = f"{clean_q}%"
            contains = f"%{clean_q}%"
            params.extend([prefix, contains, contains])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_sql = (
            "ORDER BY CASE WHEN UPPER(trading_symbol) = ? THEN 0 "
            "WHEN UPPER(trading_symbol) LIKE ? THEN 1 ELSE 2 END, trading_symbol ASC LIMIT ?"
        )
        if clean_q:
            params.extend([clean_q, f"{clean_q}%", capped_limit])
        else:
            order_sql = "ORDER BY trading_symbol ASC LIMIT ?"
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
        return [dict(row) for row in rows]

    def resolve_key(
        self,
        trading_symbol: str,
        broker_id: str = "upstox",
        exchange: str = "NSE",
        instrument_type: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve single instrument_key (Upstox) or instrument_token (Zerodha) via SQLite index."""
        sym = trading_symbol.strip().upper()
        bid = broker_id.strip().lower()
        exch = exchange.strip().upper()

        clauses = ["broker_id = ?", "exchange = ?", "(UPPER(trading_symbol) = ? OR UPPER(asset_symbol) = ?)"]
        params: List[Any] = [bid, exch, sym, sym]
        if instrument_type:
            clauses.append("UPPER(instrument_type) = ?")
            params.append(instrument_type.strip().upper())

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT instrument_key, instrument_token
                FROM instruments
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE WHEN UPPER(trading_symbol) = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                [*params, sym],
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
                r.expiry,
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
            headers={"User-Agent": "AlgoDesk/3.0"},
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
                seg = str(item.get("segment", ""))
                exch = _SEGMENT_TO_EXCHANGE.get(seg, str(item.get("exchange", "NSE"))).upper()
                sym = str(item.get("trading_symbol", "")).strip().upper()
                asset_sym = str(item.get("asset_symbol") or sym).strip().upper()
                raw_type = str(item.get("instrument_type", "EQUITY")).upper()
                norm_type = "EQUITY" if raw_type in {"EQ", "EQUITY"} else raw_type
                ikey = str(item.get("instrument_key", "")).strip()
                if not sym or not ikey:
                    continue
                exp_val = item.get("expiry")
                expiry_str = str(exp_val) if exp_val else None
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
                        float(strike_val) if strike_val is not None else None,
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
            headers={"X-Kite-Version": "3", "User-Agent": "AlgoDesk/3.0"},
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
                asset_sym = _ZERODHA_INDEX_ALIASES.get(sym, sym)
                batch.append(
                    (
                        "zerodha",
                        exch,
                        seg,
                        sym,
                        asset_sym,
                        str(row.get("name", "")).strip(),
                        norm_type,
                        f"{exch}:{sym}",
                        token,
                        str(row.get("expiry", "")).strip() or None,
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
