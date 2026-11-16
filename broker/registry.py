"""
broker/registry.py
==================
Capability-aware modular Broker & Data Connection Registry + SelectionStore.

Supports registering full brokers (Upstox active now, Zerodha planned for Phase 2)
as well as read-only market data providers (YFinance fallback) under one clean
contract so adding future broker APIs never requires modifying UI or strategy code.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Type

from broker.base import BrokerBase
from broker.data_sources import YFinanceDataAdapter
from broker.upstox.broker import UpstoxBroker
from broker.zerodha.broker import ZerodhaBroker
from config import IST_TZ, config


class SelectionStore:
    """Thread-safe atomic persistence for user's selected Trading and Data connections."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or config.SELECTION_FILE
        self._lock = threading.RLock()

    def load(self) -> Dict[str, str]:
        with self._lock:
            defaults = {
                "trading_broker": config.ACTIVE_BROKER or "upstox",
                "data_connection": config.ACTIVE_DATA_CONNECTION or "upstox",
                "updated_at": "",
            }
            if not self._path.exists():
                return defaults
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                return {
                    "trading_broker": str(raw.get("trading_broker") or defaults["trading_broker"]).lower(),
                    "data_connection": str(raw.get("data_connection") or defaults["data_connection"]).lower(),
                    "updated_at": str(raw.get("updated_at") or ""),
                }
            except (OSError, json.JSONDecodeError):
                return defaults

    def save(self, trading_broker: str, data_connection: str) -> Dict[str, str]:
        from datetime import datetime

        with self._lock:
            config.ensure_runtime_dirs()
            payload = {
                "trading_broker": trading_broker.strip().lower(),
                "data_connection": data_connection.strip().lower(),
                "updated_at": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
            }
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
            return payload


class BrokerRegistry:
    """Central modular registry for brokers and market-data adapters."""

    _brokers: Dict[str, Type[BrokerBase]] = {}
    _order: List[str] = []

    @classmethod
    def register(cls, name: str, broker_class: Type[BrokerBase]) -> None:
        key = name.strip().lower()
        cls._brokers[key] = broker_class
        if key not in cls._order:
            cls._order.append(key)

    @classmethod
    def get(cls, name: str, **kwargs) -> BrokerBase:
        key = name.strip().lower()
        if key not in cls._brokers:
            raise ValueError(f"Connection '{name}' not found. Available: {cls.list_available()}")
        return cls._brokers[key](**kwargs)

    @classmethod
    def get_class(cls, name: str) -> Type[BrokerBase]:
        key = name.strip().lower()
        if key not in cls._brokers:
            raise ValueError(f"Connection '{name}' not found. Available: {cls.list_available()}")
        return cls._brokers[key]

    @classmethod
    def list_available(cls) -> List[str]:
        return list(cls._order)

    @classmethod
    def get_active_trading_broker(cls, store: Optional[SelectionStore] = None) -> BrokerBase:
        sel = (store or SelectionStore()).load()
        return cls.get(sel["trading_broker"])

    @classmethod
    def get_active_data_connection(cls, store: Optional[SelectionStore] = None) -> BrokerBase:
        sel = (store or SelectionStore()).load()
        return cls.get(sel["data_connection"])

    @classmethod
    def get_active_broker(cls) -> BrokerBase:
        """Backward-compatible helper for callers expecting get_active_broker()."""
        return cls.get_active_trading_broker()


# Register Phase 1 & Planned Phase 2 adapters
BrokerRegistry.register("upstox", UpstoxBroker)
BrokerRegistry.register("zerodha", ZerodhaBroker)
BrokerRegistry.register("yfinance", YFinanceDataAdapter)

__all__ = ["BrokerRegistry", "SelectionStore"]
