"""
screener/nse_universe_sync.py
=============================
Standalone module to download, validate, and read official NSE Index Constituent
and F&O Market Lot CSV files into `data/universes/`.

Zero hardcoded index constituent lists:
- Downloads official CSVs directly from NSE Indices (`niftyindices.com`) and
  NSE Archives (`nsearchives.nseindia.com`).
- Atomically writes validated CSVs (`*.csv.tmp` -> `*.csv`) so failed network
  calls never corrupt an existing local CSV.
- Supports custom user-dropped `*.csv` files placed into `data/universes/`.
- Can be invoked programmatically, via API (`POST /api/analytics/universes/sync`),
  or directly from the CLI:
      python3 -m screener.nse_universe_sync --all
      python3 -m screener.nse_universe_sync --universe NIFTY_50 NIFTY_FNO
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import IST_TZ, config

logger = logging.getLogger(__name__)

# Pure index contracts that appear in NSE `fo_mktlots.csv` alongside equity F&O underlyings
_NSE_INDEX_UNDERLYINGS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "INDIAVIX",
    "SENSEX",
    "BANKEX",
    "SYMBOL",
}


@dataclass(frozen=True)
class NseUniverseSource:
    """Metadata describing an official NSE CSV universe feed."""

    universe_id: str
    label: str
    filename: str
    urls: Tuple[str, ...]
    kind: str = "index"  # "index" (niftyindices CSV) | "fno" (fo_mktlots CSV)
    description: str = ""


NSE_UNIVERSE_SOURCES: Dict[str, NseUniverseSource] = {
    "NIFTY_50": NseUniverseSource(
        universe_id="NIFTY_50",
        label="NIFTY 50",
        filename="ind_nifty50list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
        ),
        kind="index",
        description="NSE Nifty 50 benchmark index constituents",
    ),
    "NIFTY_NEXT_50": NseUniverseSource(
        universe_id="NIFTY_NEXT_50",
        label="NIFTY Next 50",
        filename="ind_niftynext50list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_niftynext50list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv",
        ),
        kind="index",
        description="NSE Nifty Next 50 index constituents",
    ),
    "NIFTY_100": NseUniverseSource(
        universe_id="NIFTY_100",
        label="NIFTY 100",
        filename="ind_nifty100list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
        ),
        kind="index",
        description="NSE Nifty 100 large-cap index constituents",
    ),
    "NIFTY_200": NseUniverseSource(
        universe_id="NIFTY_200",
        label="NIFTY 200",
        filename="ind_nifty200list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
        ),
        kind="index",
        description="NSE Nifty 200 large & mid-cap index constituents",
    ),
    "NIFTY_500": NseUniverseSource(
        universe_id="NIFTY_500",
        label="NIFTY 500",
        filename="ind_nifty500list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        ),
        kind="index",
        description="NSE Nifty 500 broad market index constituents",
    ),
    "NIFTY_MIDCAP_150": NseUniverseSource(
        universe_id="NIFTY_MIDCAP_150",
        label="NIFTY Midcap 150",
        filename="ind_niftymidcap150list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
        ),
        kind="index",
        description="NSE Nifty Midcap 150 constituents",
    ),
    "NIFTY_SMALLCAP_250": NseUniverseSource(
        universe_id="NIFTY_SMALLCAP_250",
        label="NIFTY Smallcap 250",
        filename="ind_niftysmallcap250list.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
        ),
        kind="index",
        description="NSE Nifty Smallcap 250 constituents",
    ),
    "NIFTY_BANK": NseUniverseSource(
        universe_id="NIFTY_BANK",
        label="NIFTY Bank",
        filename="ind_niftybanklist.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
        ),
        kind="index",
        description="NSE Nifty Bank sectoral index constituents",
    ),
    "NIFTY_IT": NseUniverseSource(
        universe_id="NIFTY_IT",
        label="NIFTY IT",
        filename="ind_niftyitlist.csv",
        urls=(
            "https://www.niftyindices.com/IndexConstituent/ind_niftyitlist.csv",
            "https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv",
        ),
        kind="index",
        description="NSE Nifty IT sectoral index constituents",
    ),
    "NIFTY_FNO": NseUniverseSource(
        universe_id="NIFTY_FNO",
        label="NIFTY F&O",
        filename="fo_mktlots.csv",
        urls=(
            "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
            "https://archives.nseindia.com/content/fo/fo_mktlots.csv",
        ),
        kind="fno",
        description="NSE Equity F&O permitted securities (from official NSE F&O market lots CSV)",
    ),
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.niftyindices.com/",
}


def _default_http_get(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig", errors="replace")


def parse_symbols_from_csv_text(csv_text: str, *, kind: str = "index") -> List[str]:
    """
    Parse clean, deduplicated NSE equity symbols from official NSE CSV content
    (or any custom CSV containing a `Symbol` / `Trading Symbol` column or single column).
    """
    lines = [ln for ln in (csv_text or "").splitlines() if ln.strip()]
    if not lines:
        return []

    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    if not rows:
        return []

    header = [str(cell or "").strip().lower() for cell in rows[0]]
    sym_col: Optional[int] = None
    series_col: Optional[int] = None

    for idx, col in enumerate(header):
        if col in {"symbol", "trading_symbol", "tradingsymbol", "ticker", "nse_symbol"}:
            sym_col = idx
            break
    for idx, col in enumerate(header):
        if col == "series":
            series_col = idx
            break

    start_row = 1 if sym_col is not None else 0
    if sym_col is None:
        # Fallback: single-column plain symbol CSV (optionally with header)
        if len(header) == 1 and header[0] in {"symbol", "symbols", "ticker", "stock"}:
            start_row = 1
        sym_col = 0

    symbols: List[str] = []
    seen: set[str] = set()

    for row in rows[start_row:]:
        if sym_col >= len(row):
            continue
        raw_sym = str(row[sym_col] or "").strip().upper()
        if not raw_sym or raw_sym.startswith("DUMMY") or raw_sym in {"SYMBOL", "UNDERLYING"}:
            continue
        # Reject malformed HTML/error pages accidentally saved as CSV
        if "<" in raw_sym or ">" in raw_sym or " " in raw_sym and kind == "index":
            continue
        if series_col is not None and series_col < len(row):
            series = str(row[series_col] or "").strip().upper()
            if series and series not in {"EQ", "BE", "BZ", "SM", "ST"}:
                continue
        if kind == "fno":
            if raw_sym in _NSE_INDEX_UNDERLYINGS:
                continue
            # NSE fo_mktlots contains blank/separator rows like "Derivatives on Individual Securities"
            if " " in raw_sym or len(raw_sym) > 20:
                continue
        if raw_sym not in seen:
            seen.add(raw_sym)
            symbols.append(raw_sym)
    return symbols


class NseUniverseSync:
    """Downloads, validates, persists, and reads NSE index constituent CSVs."""

    def __init__(
        self,
        universes_dir: Optional[Path] = None,
        http_getter: Optional[Callable[[str, float], str]] = None,
    ) -> None:
        self._dir = Path(universes_dir) if universes_dir else config.UNIVERSES_DIR
        self._http_get = http_getter or _default_http_get
        self._meta_lock = threading.Lock()

    @property
    def universes_dir(self) -> Path:
        return self._dir

    @property
    def meta_path(self) -> Path:
        return self._dir / "_sync_meta.json"

    def _read_meta(self) -> Dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def csv_path_for(self, universe_id: str) -> Optional[Path]:
        uid = (universe_id or "").strip().upper()
        src = NSE_UNIVERSE_SOURCES.get(uid)
        if src:
            return self._dir / src.filename
        # Check custom CSV files dropped into data/universes/
        if uid.startswith("CSV_"):
            stem = uid[4:].lower()
            if self._dir.exists():
                for p in self._dir.glob("*.csv"):
                    if p.stem.lower() == stem:
                        return p
        return None

    def read_universe_csv(self, universe_id: str) -> Tuple[List[str], Dict[str, Any]]:
        """
        Read symbols for `universe_id` from its local CSV file in `data/universes/`.
        Returns `(symbols, info_dict)` without touching the network.
        """
        uid = (universe_id or "").strip().upper()
        src = NSE_UNIVERSE_SOURCES.get(uid)
        path = self.csv_path_for(uid)
        meta = self._read_meta().get(uid, {})

        if path is None or not path.exists():
            return [], {
                "universe_id": uid,
                "exists": False,
                "csv_path": str(path) if path else "",
                "filename": src.filename if src else "",
                "updated_at_ist": meta.get("updated_at_ist"),
                "source_url": meta.get("source_url", ""),
                "symbol_count": 0,
            }

        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            logger.warning("Could not read universe CSV %s: %s", path, exc)
            return [], {
                "universe_id": uid,
                "exists": False,
                "csv_path": str(path),
                "filename": path.name,
                "error": str(exc),
                "symbol_count": 0,
            }

        kind = src.kind if src else "index"
        symbols = parse_symbols_from_csv_text(text, kind=kind)
        mtime_ist = datetime.fromtimestamp(path.stat().st_mtime, tz=IST_TZ).isoformat(timespec="seconds")
        return symbols, {
            "universe_id": uid,
            "exists": True,
            "csv_path": str(path),
            "filename": path.name,
            "updated_at_ist": meta.get("updated_at_ist") or mtime_ist,
            "source_url": meta.get("source_url") or (src.urls[0] if src else "local_csv"),
            "symbol_count": len(symbols),
        }

    def sync_universe(self, universe_id: str, timeout: float = 15.0) -> Dict[str, Any]:
        """Download and atomically save one official NSE universe CSV."""
        uid = (universe_id or "").strip().upper()
        src = NSE_UNIVERSE_SOURCES.get(uid)
        if not src:
            raise ValueError(
                f"Unknown official NSE universe '{universe_id}'. "
                f"Supported: {', '.join(sorted(NSE_UNIVERSE_SOURCES.keys()))}"
            )

        self._dir.mkdir(parents=True, exist_ok=True)
        last_err = ""
        used_url = ""
        csv_text = ""
        symbols: List[str] = []

        for url in src.urls:
            try:
                raw_text = self._http_get(url, timeout)
                parsed = parse_symbols_from_csv_text(raw_text, kind=src.kind)
                if len(parsed) < 5:
                    raise RuntimeError(f"Parsed only {len(parsed)} symbols from {url}")
                csv_text = raw_text
                symbols = parsed
                used_url = url
                break
            except Exception as exc:
                last_err = f"{url}: {exc}"
                logger.debug("NSE CSV fetch failed for %s via %s: %s", uid, url, exc)

        if not symbols:
            return {
                "ok": False,
                "universe_id": uid,
                "label": src.label,
                "filename": src.filename,
                "symbol_count": 0,
                "error": f"Failed to download {src.label} CSV from NSE ({last_err}).",
            }

        dest = self._dir / src.filename
        tmp = dest.with_suffix(".csv.tmp")
        tmp.write_text(csv_text, encoding="utf-8")
        os.replace(tmp, dest)

        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        with self._meta_lock:
            meta = self._read_meta()
            meta[uid] = {
                "universe_id": uid,
                "label": src.label,
                "filename": src.filename,
                "symbol_count": len(symbols),
                "source_url": used_url,
                "updated_at_ist": now_ist,
            }
            self._write_meta(meta)

        return {
            "ok": True,
            "universe_id": uid,
            "label": src.label,
            "filename": src.filename,
            "csv_path": str(dest),
            "symbol_count": len(symbols),
            "source_url": used_url,
            "updated_at_ist": now_ist,
        }

    def sync_all(
        self,
        universe_ids: Optional[List[str]] = None,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """Download and update all (or specified) official NSE universe CSVs in parallel."""
        targets = (
            [u.strip().upper() for u in universe_ids if str(u).strip()]
            if universe_ids
            else list(NSE_UNIVERSE_SOURCES.keys())
        )
        results: Dict[str, Dict[str, Any]] = {}
        synced_count = 0
        failed_count = 0

        with ThreadPoolExecutor(max_workers=min(6, max(1, len(targets)))) as pool:
            futs = {pool.submit(self.sync_universe, uid, timeout): uid for uid in targets}
            for fut in as_completed(futs):
                uid = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {"ok": False, "universe_id": uid, "error": str(exc), "symbol_count": 0}
                results[uid] = res
                if res.get("ok"):
                    synced_count += 1
                else:
                    failed_count += 1

        ordered = [results[uid] for uid in targets if uid in results]
        return {
            "ok": synced_count > 0 and failed_count == 0,
            "partial_ok": synced_count > 0,
            "synced_count": synced_count,
            "failed_count": failed_count,
            "universes_dir": str(self._dir),
            "updated_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
            "results": ordered,
        }

    def discover_custom_csvs(self) -> List[Dict[str, Any]]:
        """Discover any extra user-placed `*.csv` files in `data/universes/`."""
        if not self._dir.exists():
            return []
        known_files = {src.filename.lower() for src in NSE_UNIVERSE_SOURCES.values()}
        extras: List[Dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.csv")):
            if path.name.lower() in known_files:
                continue
            uid = f"CSV_{path.stem.upper()}"
            syms, info = self.read_universe_csv(uid)
            if syms:
                pretty = path.stem.replace("_", " ").replace("-", " ").title()
                extras.append(
                    {
                        "id": uid,
                        "label": f"{pretty} ({len(syms)})",
                        "base_label": pretty,
                        "count": len(syms),
                        "filename": path.name,
                        "updated_at_ist": info.get("updated_at_ist"),
                        "synced": True,
                        "custom_csv": True,
                    }
                )
        return extras

    def list_status(self) -> List[Dict[str, Any]]:
        """Return sync status & symbol counts for every official + custom CSV universe."""
        rows: List[Dict[str, Any]] = []
        for uid, src in NSE_UNIVERSE_SOURCES.items():
            syms, info = self.read_universe_csv(uid)
            count = len(syms)
            rows.append(
                {
                    "id": uid,
                    "label": f"{src.label} ({count})" if count > 0 else f"{src.label} (CSV not synced)",
                    "base_label": src.label,
                    "count": count,
                    "filename": src.filename,
                    "csv_path": info.get("csv_path", ""),
                    "source_url": info.get("source_url") or src.urls[0],
                    "updated_at_ist": info.get("updated_at_ist"),
                    "synced": count > 0,
                    "custom_csv": False,
                    "description": src.description,
                }
            )
        rows.extend(self.discover_custom_csvs())
        return rows


nse_universe_sync = NseUniverseSync()


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Download official NSE index & F&O constituent CSVs into data/universes/.")
    parser.add_argument("--all", action="store_true", help="Sync all configured NSE index & F&O CSVs.")
    parser.add_argument("--universe", nargs="+", help="Specific universe IDs to sync (e.g. NIFTY_50 NIFTY_500 NIFTY_FNO).")
    parser.add_argument("--dir", default=None, help="Override target CSV directory (default: data/universes).")
    args = parser.parse_args()

    syncer = NseUniverseSync(universes_dir=Path(args.dir) if args.dir else None)
    targets = args.universe if args.universe else None
    if not args.all and not targets:
        targets = list(NSE_UNIVERSE_SOURCES.keys())
    summary = syncer.sync_all(universe_ids=targets)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    _cli()

__all__ = [
    "NSE_UNIVERSE_SOURCES",
    "NseUniverseSource",
    "NseUniverseSync",
    "nse_universe_sync",
    "parse_symbols_from_csv_text",
]
