"""
baskets/store.py
================
Persistent Custom Baskets CRUD (`data/baskets.json`) allowing traders to group
Equities, Indices, or F&O contracts into reusable baskets for Scanners,
Individual/Portfolio Backtests, and Paper/Live Deployments.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import IST_TZ, config


_DEFAULT_BASKETS: List[Dict[str, Any]] = [
    {
        "id": "basket_largecap_core",
        "name": "Largecap Core 10",
        "description": "High-liquidity NSE largecap leaders for swing & intraday systems.",
        "exchange": "NSE",
        "symbols": [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
            "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK",
        ],
    },
    {
        "id": "basket_bank_pack",
        "name": "Banking Momentum Pack",
        "description": "Major private & PSU bank constituents for sector rotation and breakout setups.",
        "exchange": "NSE",
        "symbols": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK"],
    },
    {
        "id": "basket_it_leaders",
        "name": "IT Leaders",
        "description": "Top Indian IT exporters sensitive to USD/INR & global tech cycles.",
        "exchange": "NSE",
        "symbols": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM"],
    },
]


class BasketStore:
    def __init__(self, store_file: Optional[Path] = None) -> None:
        self._file = store_file or (config.DATA_DIR / "baskets.json")
        self._lock = threading.RLock()
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        with self._lock:
            if self._file.exists():
                return
            self._file.parent.mkdir(parents=True, exist_ok=True)
            now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            seeded = [{**b, "updated_at_ist": now_ist} for b in _DEFAULT_BASKETS]
            self._file.write_text(json.dumps(seeded, indent=2), encoding="utf-8")

    def _read_locked(self) -> List[Dict[str, Any]]:
        self._ensure_seeded()
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [dict(b) for b in raw if isinstance(b, dict) and b.get("id")]
        except Exception:
            pass
        return [dict(b) for b in _DEFAULT_BASKETS]

    def _write_locked(self, items: List[Dict[str, Any]]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def list_baskets(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = self._read_locked()
        for item in items:
            item["count"] = len(item.get("symbols") or [])
        return items

    def get(self, basket_id: str) -> Optional[Dict[str, Any]]:
        clean = (basket_id or "").strip().lower()
        with self._lock:
            for item in self._read_locked():
                if str(item.get("id") or "").lower() == clean:
                    item["count"] = len(item.get("symbols") or [])
                    return item
        return None

    def save(
        self,
        *,
        name: str,
        symbols: List[str],
        exchange: str = "NSE",
        description: str = "",
        basket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Basket name is required.")
        clean_syms: List[str] = []
        seen: set[str] = set()
        for raw_s in symbols or []:
            s = str(raw_s or "").strip().upper()
            if s and s not in seen:
                seen.add(s)
                clean_syms.append(s)
        if not clean_syms:
            raise ValueError("At least one symbol is required in a basket.")

        bid = (basket_id or "").strip().lower()
        if not bid:
            slug = re.sub(r"[^a-z0-9]+", "_", clean_name.lower()).strip("_") or "custom"
            bid = f"basket_{slug}"
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        record = {
            "id": bid,
            "name": clean_name,
            "description": (description or "").strip(),
            "exchange": (exchange or "NSE").strip().upper(),
            "symbols": clean_syms,
            "count": len(clean_syms),
            "updated_at_ist": now_ist,
        }
        with self._lock:
            items = [b for b in self._read_locked() if str(b.get("id") or "").lower() != bid]
            items.insert(0, record)
            self._write_locked(items)
        return record

    def delete(self, basket_id: str) -> Dict[str, Any]:
        clean = (basket_id or "").strip().lower()
        with self._lock:
            items = self._read_locked()
            kept = [b for b in items if str(b.get("id") or "").lower() != clean]
            removed = len(items) - len(kept)
            self._write_locked(kept)
        return {"ok": True, "removed": removed, "id": clean}


basket_store = BasketStore()

__all__ = ["BasketStore", "basket_store"]
