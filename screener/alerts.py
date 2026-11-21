"""
screener/alerts.py
==================
Persistent Streak-style Scanner Alerts Manager (`data/scanner_alerts.json`).

Manages user-subscribed scanner alerts ("Set Alerts" from Discover Scanners or
Create Scanner), supports both Visual (Basic/Advance) and Python (`.py`) screens,
and records chronological `ENTERED` / `EXITED` alert history per scanner without
creating any additional broker WebSocket connections.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from baskets.store import basket_store
from config import IST_TZ, config
from screener.engine import ScreenerEngine, screener_engine


def _now_ist() -> str:
    return datetime.now(tz=IST_TZ).isoformat(timespec="seconds")


class ScannerAlertsManager:
    """Thread-safe JSON persistence & evaluation for Scanner Alert subscriptions."""

    def __init__(
        self,
        store_file: Optional[Path] = None,
        engine: Optional[ScreenerEngine] = None,
        scan_runner: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        self._file = store_file or (config.DATA_DIR / "scanner_alerts.json")
        self._engine = engine or screener_engine
        self._scan_runner = scan_runner
        self._lock = threading.RLock()
        self._ensure_file()

    def _ensure_file(self) -> None:
        with self._lock:
            if self._file.exists():
                return
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps({"subscriptions": [], "events": []}, indent=2),
                encoding="utf-8",
            )

    def _read_locked(self) -> Dict[str, Any]:
        self._ensure_file()
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    "subscriptions": list(raw.get("subscriptions") or []),
                    "events": list(raw.get("events") or []),
                }
        except Exception:
            pass
        return {"subscriptions": [], "events": []}

    def _write_locked(self, data: Dict[str, Any]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_alerts(self, subscription_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            data = self._read_locked()
        subs = list(data["subscriptions"])
        events = list(data["events"])
        if subscription_id:
            events = [e for e in events if e.get("subscription_id") == subscription_id]
        sliced = events[:200]
        return {
            "ok": True,
            "subscriptions": subs,
            "events": sliced,
            "history": sliced,
            "active_count": sum(1 for s in subs if s.get("status") == "ACTIVE"),
            "generated_at_ist": _now_ist(),
        }

    def create_or_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or payload.get("screen_name") or "").strip()
        if not name:
            raise ValueError("Scanner name is required to set alerts.")

        mode = str(payload.get("mode") or "basic").strip().lower()
        if mode not in {"basic", "advance", "python", "visual"}:
            mode = "basic"

        now = _now_ist()
        sub_id = str(payload.get("id") or "").strip() or f"sa-{uuid.uuid4().hex[:8]}"

        record: Dict[str, Any] = {
            "id": sub_id,
            "name": name,
            "description": str(payload.get("description") or "").strip(),
            "mode": mode,
            "screen_id": str(payload.get("screen_id") or "").strip(),
            "rule_def": payload.get("rule_def") if isinstance(payload.get("rule_def"), dict) else None,
            "params": payload.get("params") if isinstance(payload.get("params"), dict) else {},
            "universe": str(payload.get("universe") or "NIFTY_50").strip().upper(),
            "basket_id": str(payload.get("basket_id") or "").strip() or None,
            "symbols": [str(s).strip().upper() for s in (payload.get("symbols") or []) if str(s).strip()],
            "exchange": str(payload.get("exchange") or "NSE").strip().upper(),
            "timeframe": str(payload.get("timeframe") or "1day").strip().lower(),
            "chart_type": str(payload.get("chart_type") or "candlestick").strip().lower(),
            "frequency_sec": max(30, min(3600, int(payload.get("frequency_sec") or 60))),
            "status": "ACTIVE",
            "last_checked_ist": None,
            "last_matched_symbols": [],
            "last_matched_count": 0,
            "alerts_triggered": 0,
            "created_at_ist": now,
            "updated_at_ist": now,
        }

        with self._lock:
            data = self._read_locked()
            existing = next((s for s in data["subscriptions"] if s.get("id") == sub_id), None)
            if existing:
                record["created_at_ist"] = existing.get("created_at_ist") or now
                record["last_matched_symbols"] = list(existing.get("last_matched_symbols") or [])
                record["alerts_triggered"] = int(existing.get("alerts_triggered") or 0)
            data["subscriptions"] = [record] + [s for s in data["subscriptions"] if s.get("id") != sub_id]
            self._write_locked(data)

        return {"ok": True, "subscription": record, **self.list_alerts()}

    def control(
        self,
        subscription_id: str,
        action: str,
        *,
        provider: Optional[str] = None,
        fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        act = (action or "").strip().lower()
        with self._lock:
            data = self._read_locked()
            sub = next((s for s in data["subscriptions"] if s.get("id") == subscription_id), None)
            if sub is None:
                raise ValueError(f"Scanner alert '{subscription_id}' not found.")

            if act == "pause":
                sub["status"] = "PAUSED"
                sub["updated_at_ist"] = _now_ist()
                self._write_locked(data)
                return {"ok": True, "subscription": sub, **self.list_alerts()}

            if act == "resume":
                sub["status"] = "ACTIVE"
                sub["updated_at_ist"] = _now_ist()
                self._write_locked(data)
                return {"ok": True, "subscription": sub, **self.list_alerts()}

            if act == "clear_history":
                data["events"] = [e for e in data["events"] if e.get("subscription_id") != subscription_id]
                sub["alerts_triggered"] = 0
                sub["updated_at_ist"] = _now_ist()
                self._write_locked(data)
                return {"ok": True, "subscription": sub, **self.list_alerts()}

            if act != "scan_now":
                raise ValueError(f"Unsupported scanner alert action '{action}'.")

            sub_copy = dict(sub)

        # Perform scan outside the lock so UI/WS remain non-blocking
        custom_syms = list(sub_copy.get("symbols") or [])
        eff_universe = str(sub_copy.get("universe") or "NIFTY_50")
        if sub_copy.get("basket_id"):
            b = basket_store.get(str(sub_copy["basket_id"]))
            if b and b.get("symbols"):
                custom_syms = list(b["symbols"])
                eff_universe = "CUSTOM"

        if self._scan_runner is not None:
            scan_res = self._scan_runner(
                symbols=custom_syms if custom_syms else None,
                universe=eff_universe,
                exchange=str(sub_copy.get("exchange") or "NSE"),
                timeframe=str(sub_copy.get("timeframe") or "1day"),
                screen_id=sub_copy.get("screen_id") or None,
                rule_def=sub_copy.get("rule_def"),
                params=sub_copy.get("params") or None,
                provider=provider,
            )
        else:
            scan_res = self._engine.scan(
                symbols=custom_syms if custom_syms else None,
                universe=eff_universe,
                exchange=str(sub_copy.get("exchange") or "NSE"),
                timeframe=str(sub_copy.get("timeframe") or "1day"),
                screen_id=sub_copy.get("screen_id") or None,
                rule_def=sub_copy.get("rule_def"),
                params=sub_copy.get("params") or None,
                provider=provider,
                fetcher=fetcher,
            )

        raw_rows = scan_res.get("results") if scan_res.get("results") is not None else (scan_res.get("rows") or [])
        matched_rows = [r for r in raw_rows if r.get("matched", True)]
        matched_by_sym = {str(r.get("symbol", "")).upper(): r for r in matched_rows if r.get("symbol")}
        now = _now_ist()
        new_events: List[Dict[str, Any]] = []

        with self._lock:
            data = self._read_locked()
            sub = next((s for s in data["subscriptions"] if s.get("id") == subscription_id), None)
            if sub is None:
                raise ValueError(f"Scanner alert '{subscription_id}' was deleted.")

            prev_syms = set(str(s).upper() for s in (sub.get("last_matched_symbols") or []))
            cur_syms = set(matched_by_sym.keys())

            entered = sorted(cur_syms - prev_syms)
            exited = sorted(prev_syms - cur_syms)

            for sym in entered:
                row = matched_by_sym[sym]
                new_events.append(
                    {
                        "id": f"sae-{uuid.uuid4().hex[:8]}",
                        "subscription_id": subscription_id,
                        "scanner_name": sub["name"],
                        "symbol": sym,
                        "exchange": str(row.get("exchange") or sub.get("exchange") or "NSE"),
                        "transition": "ENTERED",
                        "ltp": float(row.get("ltp") or row.get("close") or 0.0),
                        "change_pct": float(row.get("change_pct") or 0.0),
                        "volume": int(row.get("volume") or 0),
                        "signal": str(row.get("signal") or "MATCH"),
                        "timeframe": str(sub.get("timeframe") or "1day"),
                        "ts_ist": now,
                    }
                )
            for sym in exited:
                new_events.append(
                    {
                        "id": f"sae-{uuid.uuid4().hex[:8]}",
                        "subscription_id": subscription_id,
                        "scanner_name": sub["name"],
                        "symbol": sym,
                        "exchange": str(sub.get("exchange") or "NSE"),
                        "transition": "EXITED",
                        "ltp": 0.0,
                        "change_pct": 0.0,
                        "volume": 0,
                        "signal": "EXIT",
                        "timeframe": str(sub.get("timeframe") or "1day"),
                        "ts_ist": now,
                    }
                )

            sub["last_checked_ist"] = now
            sub["last_matched_symbols"] = sorted(cur_syms)
            sub["last_matched_count"] = len(cur_syms)
            sub["alerts_triggered"] = int(sub.get("alerts_triggered") or 0) + len(new_events)
            sub["updated_at_ist"] = now

            data["events"] = (new_events + data["events"])[:300]
            self._write_locked(data)

        return {
            "ok": True,
            "new_events_count": len(new_events),
            "matched_count": len(matched_by_sym),
            "subscription": sub,
            **self.list_alerts(),
        }

    def delete(self, subscription_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._read_locked()
            before = len(data["subscriptions"])
            data["subscriptions"] = [s for s in data["subscriptions"] if s.get("id") != subscription_id]
            data["events"] = [e for e in data["events"] if e.get("subscription_id") != subscription_id]
            removed = before - len(data["subscriptions"])
            self._write_locked(data)
        return {"ok": True, "removed": removed, "id": subscription_id, **self.list_alerts()}


scanner_alerts_manager = ScannerAlertsManager()

__all__ = ["ScannerAlertsManager", "scanner_alerts_manager"]
