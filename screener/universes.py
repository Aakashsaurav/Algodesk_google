"""
screener/universes.py
=====================
Resolves market universes (NIFTY 50, NIFTY Bank, NIFTY IT, NIFTY F&O from SQLite
instruments.db, NIFTY 500, Cached Local OHLCV, or Custom comma-separated symbols).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from core.historical import HistoricalDataManager, historical_data_manager
from core.instruments import InstrumentIndex, instrument_index
from screener.universes_data import (
    DEFAULT_WATCHLIST,
    NIFTY_50,
    NIFTY_500,
    NIFTY_BANK,
    NIFTY_FNO_FALLBACK,
    NIFTY_IT,
)


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
    """Resolves named equity/derivative universes dynamically."""

    def __init__(
        self,
        instr_idx: Optional[InstrumentIndex] = None,
        hist_mgr: Optional[HistoricalDataManager] = None,
    ) -> None:
        self._instr = instr_idx or instrument_index
        self._hist = hist_mgr or historical_data_manager

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
                "label": "Default Watchlist (10)",
                "symbols": syms,
                "source_note": "Built-in flagship watchlist",
            }

        if uid == "NIFTY_50":
            syms = _dedup_symbols(NIFTY_50 + extras)
            return {
                "universe": "NIFTY_50",
                "label": f"NIFTY 50 ({len(NIFTY_50)})",
                "symbols": syms,
                "source_note": "NSE Nifty 50 index constituents",
            }

        if uid == "NIFTY_BANK":
            syms = _dedup_symbols(NIFTY_BANK + extras)
            return {
                "universe": "NIFTY_BANK",
                "label": f"NIFTY Bank ({len(NIFTY_BANK)})",
                "symbols": syms,
                "source_note": "NSE Nifty Bank index constituents",
            }

        if uid == "NIFTY_IT":
            syms = _dedup_symbols(NIFTY_IT + extras)
            return {
                "universe": "NIFTY_IT",
                "label": f"NIFTY IT ({len(NIFTY_IT)})",
                "symbols": syms,
                "source_note": "NSE Nifty IT index constituents",
            }

        if uid == "NIFTY_FNO":
            sqlite_fno = self._fno_underlyings_from_sqlite()
            base_fno = sqlite_fno if len(sqlite_fno) >= 20 else NIFTY_FNO_FALLBACK
            source_note = (
                f"Live SQLite NFO master ({len(sqlite_fno)} underlyings)"
                if len(sqlite_fno) >= 20
                else f"Standard NSE F&O stock list ({len(NIFTY_FNO_FALLBACK)} underlyings; sync NFO master for live list)"
            )
            syms = _dedup_symbols(base_fno + extras)
            return {
                "universe": "NIFTY_FNO",
                "label": f"NIFTY F&O ({len(base_fno)})",
                "symbols": syms,
                "source_note": source_note,
            }

        if uid == "NIFTY_500":
            syms = _dedup_symbols(NIFTY_500 + extras)
            return {
                "universe": "NIFTY_500",
                "label": f"NIFTY 500 ({len(NIFTY_500)})",
                "symbols": syms,
                "source_note": "NSE Nifty 500 broad market constituent universe",
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
        fno_count = len(sqlite_fno) if len(sqlite_fno) >= 20 else len(NIFTY_FNO_FALLBACK)
        cached_count = len(self._hist.list_cached_symbols(exchange=exchange, timeframe=timeframe))
        return [
            {"id": "DEFAULT_WATCHLIST", "label": f"Default Watchlist ({len(DEFAULT_WATCHLIST)})", "count": len(DEFAULT_WATCHLIST)},
            {"id": "NIFTY_50", "label": f"NIFTY 50 ({len(NIFTY_50)})", "count": len(NIFTY_50)},
            {"id": "NIFTY_BANK", "label": f"NIFTY Bank ({len(NIFTY_BANK)})", "count": len(NIFTY_BANK)},
            {"id": "NIFTY_IT", "label": f"NIFTY IT ({len(NIFTY_IT)})", "count": len(NIFTY_IT)},
            {"id": "NIFTY_FNO", "label": f"NIFTY F&O ({fno_count})", "count": fno_count},
            {"id": "NIFTY_500", "label": f"NIFTY 500 ({len(NIFTY_500)})", "count": len(NIFTY_500)},
            {"id": "CACHED_SQLITE", "label": f"Local Cached ({cached_count})", "count": cached_count},
            {"id": "CUSTOM", "label": "Custom (Comma-Separated)", "count": 0},
        ]


universe_resolver = UniverseResolver()

__all__ = ["DEFAULT_WATCHLIST", "UniverseResolver", "universe_resolver"]
