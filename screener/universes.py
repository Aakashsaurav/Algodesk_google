"""
screener/universes.py
=====================
Resolves market universes strictly from local NSE CSV files maintained by the
separate `screener.nse_universe_sync` module (`data/universes/*.csv`), SQLite
broker NFO master, local cached OHLCV symbols, or custom comma-separated input.

Zero hardcoded index constituent lists.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

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
    """Resolves named equity/derivative universes dynamically from NSE CSVs and local DBs."""

    def __init__(
        self,
        instr_idx: Optional[InstrumentIndex] = None,
        hist_mgr: Optional[HistoricalDataManager] = None,
        csv_sync: Optional[NseUniverseSync] = None,
        auto_sync_missing: bool = True,
    ) -> None:
        self._instr = instr_idx or instrument_index
        self._hist = hist_mgr or historical_data_manager
        self._csv_sync = csv_sync or nse_universe_sync
        self._auto_sync_missing = bool(auto_sync_missing)

    @property
    def csv_sync(self) -> NseUniverseSync:
        return self._csv_sync

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
    ) -> Dict[str, Any]:
        uid = (universe or "CUSTOM").strip().upper()
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
