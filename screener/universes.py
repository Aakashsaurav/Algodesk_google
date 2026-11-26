"""
screener/universes.py
=====================
Resolves market universes strictly from local NSE CSV files maintained by the
separate `screener.nse_universe_sync` module (`data/universes/*.csv`), SQLite
broker NFO master, local cached OHLCV symbols, or custom comma-separated input.

Zero hardcoded index constituent lists.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import IST_TZ, config
from core.historical import HistoricalDataManager, historical_data_manager
from core.instruments import InstrumentIndex, instrument_index
from screener.nse_universe_sync import (
    NSE_UNIVERSE_SOURCES,
    NseUniverseSync,
    nse_universe_sync,
)

# Small convenience starter symbols ONLY when the user runs CUSTOM with empty input
DEFAULT_WATCHLIST: List[str] = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "AXISBANK",
]


def _dedup_symbols(raw: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        sym = str(item or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


class UniverseResolver:
    """
    Resolves named equity/derivative universes dynamically from NSE CSVs, local DBs,
    and point-in-time historical universe snapshots (preventing survivorship bias).
    """

    def __init__(
        self,
        instr_idx: Optional[InstrumentIndex] = None,
        hist_mgr: Optional[HistoricalDataManager] = None,
        csv_sync: Optional[NseUniverseSync] = None,
        auto_sync_missing: bool = True,
        snapshots_db_path: Optional[Path] = None,
    ) -> None:
        self._instr = instr_idx or instrument_index
        self._hist = hist_mgr or historical_data_manager
        self._csv_sync = csv_sync or nse_universe_sync
        self._auto_sync_missing = bool(auto_sync_missing)
        self._explicit_snapshots_db = snapshots_db_path
        self._lock = threading.RLock()
        self._initialized_paths: set[Path] = set()

    @property
    def csv_sync(self) -> NseUniverseSync:
        return self._csv_sync

    @property
    def snapshots_db_path(self) -> Path:
        return self._explicit_snapshots_db or (config.SQLITE_DIR / "universes.db")

    def _connect_snapshots(self) -> sqlite3.Connection:
        path = self.snapshots_db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        if path not in self._initialized_paths:
            with self._lock:
                if path not in self._initialized_paths:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS universe_snapshots (
                            universe_id TEXT NOT NULL,
                            effective_date TEXT NOT NULL,
                            symbols_json TEXT NOT NULL,
                            delisted_symbols_json TEXT NOT NULL DEFAULT '[]',
                            source TEXT NOT NULL DEFAULT 'manual',
                            notes TEXT NOT NULL DEFAULT '',
                            created_at_ist TEXT NOT NULL,
                            PRIMARY KEY (universe_id, effective_date)
                        );
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_universe_snap_lookup ON universe_snapshots (universe_id, effective_date DESC);"
                    )
                    conn.commit()
                    self._initialized_paths.add(path)
        return conn

    def save_universe_snapshot(
        self,
        universe_id: str,
        effective_date: str,
        symbols: List[str],
        *,
        delisted_symbols: Optional[List[str]] = None,
        source: str = "manual",
        notes: str = "",
    ) -> Dict[str, Any]:
        """Persist a point-in-time constituent snapshot for survivorship-free historical testing."""
        uid = (universe_id or "").strip().upper()
        if not uid:
            raise ValueError("universe_id is required.")
        eff_date = str(effective_date or "").strip()[:10]
        if len(eff_date) != 10:
            raise ValueError("effective_date must be in YYYY-MM-DD format.")
        datetime.strptime(eff_date, "%Y-%m-%d")
        clean_syms = _dedup_symbols(symbols)
        if not clean_syms:
            raise ValueError("At least one constituent symbol is required for a universe snapshot.")
        clean_delisted = _dedup_symbols(delisted_symbols or [])
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        with self._connect_snapshots() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO universe_snapshots
                (universe_id, effective_date, symbols_json, delisted_symbols_json, source, notes, created_at_ist)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    eff_date,
                    json.dumps(clean_syms),
                    json.dumps(clean_delisted),
                    (source or "manual").strip(),
                    (notes or "").strip(),
                    now_ist,
                ),
            )
            conn.commit()
        return {
            "ok": True,
            "universe_id": uid,
            "effective_date": eff_date,
            "symbols": clean_syms,
            "symbol_count": len(clean_syms),
            "delisted_symbols": clean_delisted,
            "source": (source or "manual").strip(),
            "notes": (notes or "").strip(),
            "created_at_ist": now_ist,
        }

    def list_universe_snapshots(self, universe_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List stored point-in-time universe snapshots."""
        with self._connect_snapshots() as conn:
            if universe_id:
                rows = conn.execute(
                    """
                    SELECT * FROM universe_snapshots
                    WHERE universe_id = ?
                    ORDER BY effective_date DESC
                    """,
                    (universe_id.strip().upper(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM universe_snapshots
                    ORDER BY universe_id ASC, effective_date DESC
                    """
                ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            syms = json.loads(r["symbols_json"] or "[]")
            delisted = json.loads(r["delisted_symbols_json"] or "[]")
            out.append(
                {
                    "universe_id": str(r["universe_id"]),
                    "effective_date": str(r["effective_date"]),
                    "symbols": syms,
                    "symbol_count": len(syms),
                    "delisted_symbols": delisted,
                    "source": str(r["source"] or "manual"),
                    "notes": str(r["notes"] or ""),
                    "created_at_ist": str(r["created_at_ist"] or ""),
                }
            )
        return out

    def resolve_universe_as_of(
        self,
        universe_id: str,
        as_of_date: str,
        *,
        custom_symbols: Optional[List[str]] = None,
        exchange: str = "NSE",
        timeframe: str = "1day",
    ) -> Dict[str, Any]:
        """
        Resolve universe constituents as of `as_of_date` (YYYY-MM-DD).
        Uses the latest persisted snapshot with `effective_date <= as_of_date`.
        If no point-in-time snapshot exists, falls back to the current constituent list
        and explicitly emits a `survivorship_warning`.
        """
        uid = (universe_id or "CUSTOM").strip().upper()
        as_of = str(as_of_date or "").strip()[:10]
        datetime.strptime(as_of, "%Y-%m-%d")
        extras = _dedup_symbols(custom_symbols or [])

        with self._connect_snapshots() as conn:
            row = conn.execute(
                """
                SELECT * FROM universe_snapshots
                WHERE universe_id = ? AND effective_date <= ?
                ORDER BY effective_date DESC
                LIMIT 1
                """,
                (uid, as_of),
            ).fetchone()

        if row is not None:
            snap_syms = json.loads(row["symbols_json"] or "[]")
            delisted = json.loads(row["delisted_symbols_json"] or "[]")
            syms = _dedup_symbols(snap_syms + extras)
            eff_date = str(row["effective_date"])
            return {
                "universe": uid,
                "label": f"{uid} (as of {eff_date}, {len(syms)})",
                "symbols": syms,
                "as_of_date": as_of,
                "effective_date": eff_date,
                "point_in_time": True,
                "survivorship_warning": None,
                "delisted_symbols": delisted,
                "source_note": f"Point-in-time snapshot ({eff_date}, source={row['source']})",
            }

        fallback = self.resolve(
            universe=uid,
            custom_symbols=custom_symbols,
            exchange=exchange,
            timeframe=timeframe,
        )
        fallback["as_of_date"] = as_of
        fallback["effective_date"] = None
        fallback["point_in_time"] = False
        fallback["delisted_symbols"] = []
        fallback["survivorship_warning"] = (
            f"No historical snapshot found for {uid} on or before {as_of}. "
            "Falling back to current constituent list, which may introduce survivorship bias."
        )
        return fallback

    def _fno_underlyings_from_sqlite(self) -> List[str]:
        path = self._instr.db_path
        if not path.exists():
            return []
        try:
            with sqlite3.connect(str(path), timeout=5.0) as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT UPPER(asset_symbol) AS sym
                    FROM instruments
                    WHERE (UPPER(exchange) IN ('NFO', 'NSE_FO') OR UPPER(segment) = 'NSE_FO')
                      AND asset_symbol IS NOT NULL
                      AND TRIM(asset_symbol) != ''
                      AND UPPER(asset_symbol) NOT IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50')
                    ORDER BY sym
                    """
                ).fetchall()
                return [str(r[0]).strip().upper() for r in rows if r and r[0]]
        except sqlite3.Error:
            return []

    def _load_nse_csv_universe(self, uid: str) -> tuple[List[str], Dict[str, Any]]:
        syms, info = self._csv_sync.read_universe_csv(uid)
        if not syms and self._auto_sync_missing and uid in NSE_UNIVERSE_SOURCES:
            sync_res = self._csv_sync.sync_universe(uid)
            if sync_res.get("ok"):
                syms, info = self._csv_sync.read_universe_csv(uid)
        return syms, info

    def resolve(
        self,
        universe: str = "CUSTOM",
        *,
        custom_symbols: Optional[List[str]] = None,
        exchange: str = "NSE",
        timeframe: str = "1day",
        as_of_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = (universe or "CUSTOM").strip().upper()
        if as_of_date and uid not in {"CUSTOM", "DEFAULT_WATCHLIST", "CACHED_SQLITE"}:
            return self.resolve_universe_as_of(
                uid,
                as_of_date,
                custom_symbols=custom_symbols,
                exchange=exchange,
                timeframe=timeframe,
            )

        extras = _dedup_symbols(custom_symbols or [])

        if uid == "CUSTOM":
            syms = extras if extras else list(DEFAULT_WATCHLIST)
            return {
                "universe": "CUSTOM",
                "label": "Custom Watchlist" if extras else "Default Watchlist",
                "symbols": syms,
                "source_note": "Comma-separated custom symbols",
            }

        if uid == "DEFAULT_WATCHLIST":
            syms = _dedup_symbols(DEFAULT_WATCHLIST + extras)
            return {
                "universe": "DEFAULT_WATCHLIST",
                "label": f"Default Watchlist ({len(DEFAULT_WATCHLIST)})",
                "symbols": syms,
                "source_note": "Starter flagship watchlist",
            }

        if uid == "NIFTY_FNO":
            sqlite_fno = self._fno_underlyings_from_sqlite()
            if len(sqlite_fno) >= 20:
                syms = _dedup_symbols(sqlite_fno + extras)
                return {
                    "universe": "NIFTY_FNO",
                    "label": f"NIFTY F&O ({len(sqlite_fno)})",
                    "symbols": syms,
                    "source_note": f"Live SQLite NFO master ({len(sqlite_fno)} underlyings)",
                }
            csv_syms, info = self._load_nse_csv_universe("NIFTY_FNO")
            if not csv_syms:
                raise ValueError(
                    "NIFTY F&O CSV (`fo_mktlots.csv`) is not synced locally and SQLite NFO master is empty. "
                    "Click 'Sync NSE CSVs' or run `python3 -m screener.nse_universe_sync --universe NIFTY_FNO`."
                )
            syms = _dedup_symbols(csv_syms + extras)
            return {
                "universe": "NIFTY_FNO",
                "label": f"NIFTY F&O ({len(csv_syms)})",
                "symbols": syms,
                "source_note": f"NSE CSV {info.get('filename', 'fo_mktlots.csv')} (updated {info.get('updated_at_ist') or 'local'})",
            }

        if uid in NSE_UNIVERSE_SOURCES or uid.startswith("CSV_"):
            csv_syms, info = self._load_nse_csv_universe(uid)
            src = NSE_UNIVERSE_SOURCES.get(uid)
            label_base = src.label if src else uid[4:].replace("_", " ").title()
            if not csv_syms:
                fname = src.filename if src else f"{uid[4:].lower()}.csv"
                raise ValueError(
                    f"Universe CSV '{fname}' for {label_base} is not available in `{self._csv_sync.universes_dir}`. "
                    f"Click 'Sync NSE CSVs' or run `python3 -m screener.nse_universe_sync --universe {uid}`."
                )
            syms = _dedup_symbols(csv_syms + extras)
            return {
                "universe": uid,
                "label": f"{label_base} ({len(csv_syms)})",
                "symbols": syms,
                "source_note": f"NSE CSV {info.get('filename', '')} (updated {info.get('updated_at_ist') or 'local'})",
            }

        if uid == "CACHED_SQLITE":
            cached = self._hist.list_cached_symbols(exchange=exchange, timeframe=timeframe)
            syms = _dedup_symbols(cached + extras)
            return {
                "universe": "CACHED_SQLITE",
                "label": f"Local Cached Symbols ({len(cached)})",
                "symbols": syms,
                "source_note": f"Symbols with local Parquet/SQLite OHLCV history ({exchange} {timeframe})",
            }

        raise ValueError(f"Unknown universe '{universe}'.")

    def list_universes(self, exchange: str = "NSE", timeframe: str = "1day") -> List[Dict[str, Any]]:
        sqlite_fno = self._fno_underlyings_from_sqlite()
        cached_count = len(self._hist.list_cached_symbols(exchange=exchange, timeframe=timeframe))

        items: List[Dict[str, Any]] = [
            {
                "id": "DEFAULT_WATCHLIST",
                "label": f"Default Watchlist ({len(DEFAULT_WATCHLIST)})",
                "count": len(DEFAULT_WATCHLIST),
                "synced": True,
                "source": "watchlist",
            }
        ]

        for entry in self._csv_sync.list_status():
            uid = entry["id"]
            if uid == "NIFTY_FNO" and len(sqlite_fno) >= 20:
                items.append(
                    {
                        **entry,
                        "label": f"NIFTY F&O ({len(sqlite_fno)})",
                        "count": len(sqlite_fno),
                        "synced": True,
                        "source": "sqlite_nfo",
                    }
                )
            else:
                items.append({**entry, "source": "nse_csv" if not entry.get("custom_csv") else "custom_csv"})

        items.append(
            {
                "id": "CACHED_SQLITE",
                "label": f"Local Cached ({cached_count})",
                "count": cached_count,
                "synced": True,
                "source": "local_ohlcv",
            }
        )
        items.append(
            {
                "id": "CUSTOM",
                "label": "Custom (Comma-Separated)",
                "count": 0,
                "synced": True,
                "source": "custom",
            }
        )
        return items


universe_resolver = UniverseResolver()

__all__ = ["DEFAULT_WATCHLIST", "UniverseResolver", "universe_resolver"]
