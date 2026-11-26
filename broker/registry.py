"""
broker/registry.py
==================
Capability-aware modular Broker & Data Connection Registry + SelectionStore.

Supports registering full brokers (Upstox, Zerodha Kite, and custom plugins)
as well as read-only market data providers (YFinance fallback) under one clean
contract, with optional `importlib.metadata` entry-point discovery and isolated
registry instances for clean dependency injection and testing.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Type

from broker.base import BrokerBase
from broker.data_sources import YFinanceDataAdapter
from broker.upstox.broker import UpstoxBroker
from broker.zerodha.broker import ZerodhaBroker
from config import IST_TZ, config

logger = logging.getLogger(__name__)


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
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", suffix=".tmp", dir=str(self._path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                try:
                    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return payload


class IsolatedBrokerRegistry:
    """
    Instance-scoped broker & data-provider registry with managed adapter pooling
    for clean dependency injection and isolated unit tests.
    """

    def __init__(self, initial: Optional[Dict[str, Type[BrokerBase]]] = None) -> None:
        self._lock = threading.RLock()
        self._brokers: Dict[str, Type[BrokerBase]] = {}
        self._order: List[str] = []
        self._instances: Dict[str, BrokerBase] = {}
        self._ws_factories: Dict[str, object] = {}
        if initial:
            for name, broker_cls in initial.items():
                self.register(name, broker_cls)

    def register(self, name: str, broker_class: Type[BrokerBase]) -> None:
        if not isinstance(broker_class, type) or not issubclass(broker_class, BrokerBase):
            raise TypeError(f"Broker adapter '{name}' must be a subclass of BrokerBase.")
        key = name.strip().lower()
        if not key:
            raise ValueError("Broker adapter name cannot be empty.")
        with self._lock:
            self._brokers[key] = broker_class
            self._instances.pop(key, None)
            if key not in self._order:
                self._order.append(key)

    def unregister(self, name: str) -> bool:
        key = name.strip().lower()
        with self._lock:
            existed = key in self._brokers
            self._brokers.pop(key, None)
            self._instances.pop(key, None)
            self._ws_factories.pop(key, None)
            if key in self._order:
                self._order.remove(key)
            return existed

    def has(self, name: str) -> bool:
        key = name.strip().lower()
        with self._lock:
            return key in self._brokers

    def register_ws_client_factory(self, name: str, factory: object) -> None:
        key = name.strip().lower()
        if not key or not callable(factory):
            raise ValueError("Valid provider name and callable factory required.")
        with self._lock:
            self._ws_factories[key] = factory

    def get_ws_client_factory(self, name: str) -> Optional[object]:
        key = name.strip().lower()
        with self._lock:
            return self._ws_factories.get(key)

    def get(self, name: str, *, fresh: bool = False, **kwargs) -> BrokerBase:
        """
        Return a managed shared adapter instance when called with default args
        (preserving rate-limiter state & connection caches), or a dedicated
        instance when `fresh=True` or custom kwargs are supplied.
        """
        key = name.strip().lower()
        with self._lock:
            cls = self.get_class(key)
            if fresh or kwargs:
                return cls(**kwargs)
            inst = self._instances.get(key)
            if inst is None or not isinstance(inst, cls):
                inst = cls()
                self._instances[key] = inst
            return inst

    def reset_instances(self, name: Optional[str] = None) -> None:
        with self._lock:
            if name:
                self._instances.pop(name.strip().lower(), None)
            else:
                self._instances.clear()

    def get_class(self, name: str) -> Type[BrokerBase]:
        key = name.strip().lower()
        with self._lock:
            if key not in self._brokers:
                raise ValueError(f"Connection '{name}' not found. Available: {list(self._order)}")
            return self._brokers[key]

    def list_available(self) -> List[str]:
        with self._lock:
            return list(self._order)

    def list_by_capability(self, capability_attr: str) -> List[str]:
        with self._lock:
            out: List[str] = []
            for key in self._order:
                cls = self._brokers[key]
                caps = getattr(cls, "capabilities", None)
                if caps is not None and bool(getattr(caps, capability_attr, False)):
                    out.append(key)
            return out


class BrokerRegistry:
    """
    Central modular registry for brokers & market-data providers with a managed
    adapter instance lifecycle (one shared instance per configured broker by
    default so rate limiters, session caches, and connection states persist).
    """

    _lock = threading.RLock()
    _brokers: Dict[str, Type[BrokerBase]] = {}
    _order: List[str] = []
    _instances: Dict[str, BrokerBase] = {}
    _ws_factories: Dict[str, object] = {}

    @classmethod
    def register(cls, name: str, broker_class: Type[BrokerBase]) -> None:
        if not isinstance(broker_class, type) or not issubclass(broker_class, BrokerBase):
            raise TypeError(f"Broker adapter '{name}' must be a subclass of BrokerBase.")
        key = name.strip().lower()
        if not key:
            raise ValueError("Broker adapter name cannot be empty.")
        with cls._lock:
            cls._brokers[key] = broker_class
            cls._instances.pop(key, None)
            if key not in cls._order:
                cls._order.append(key)

    @classmethod
    def register_plugin(
        cls,
        name: str,
        broker_class: Type[BrokerBase],
        *,
        ws_client_factory: Optional[object] = None,
    ) -> None:
        """
        Register an external broker or data-provider plugin after validating its
        `BrokerBase` contract. Optionally binds a custom WebSocket feed factory
        so adding a broker or market-data provider requires zero edits to core modules.
        """
        cls.register(name, broker_class)
        if ws_client_factory is not None:
            cls.register_ws_client_factory(name, ws_client_factory)

    @classmethod
    def register_data_provider(
        cls,
        name: str,
        provider_class: Type[BrokerBase],
        *,
        ws_client_factory: Optional[object] = None,
    ) -> None:
        """Semantic alias for registering a market-data / historical-data provider plugin."""
        cls.register_plugin(name, provider_class, ws_client_factory=ws_client_factory)

    @classmethod
    def register_ws_client_factory(cls, name: str, factory: object) -> None:
        key = name.strip().lower()
        if not key or not callable(factory):
            raise ValueError("Valid provider name and callable factory required.")
        with cls._lock:
            cls._ws_factories[key] = factory

    @classmethod
    def get_ws_client_factory(cls, name: str) -> Optional[object]:
        key = name.strip().lower()
        with cls._lock:
            return cls._ws_factories.get(key)

    @classmethod
    def unregister(cls, name: str) -> bool:
        key = name.strip().lower()
        with cls._lock:
            existed = key in cls._brokers
            cls._brokers.pop(key, None)
            cls._instances.pop(key, None)
            cls._ws_factories.pop(key, None)
            if key in cls._order:
                cls._order.remove(key)
            return existed

    @classmethod
    def has(cls, name: str) -> bool:
        key = name.strip().lower()
        with cls._lock:
            return key in cls._brokers

    @classmethod
    def create_isolated(cls, copy_existing: bool = True) -> IsolatedBrokerRegistry:
        """Create an isolated registry instance without mutating global class state."""
        with cls._lock:
            seed = {k: cls._brokers[k] for k in cls._order} if copy_existing else None
            ws_copy = dict(cls._ws_factories) if copy_existing else {}
        iso = IsolatedBrokerRegistry(initial=seed)
        for k, fac in ws_copy.items():
            iso.register_ws_client_factory(k, fac)
        return iso

    @classmethod
    def discover_entry_points(cls, group: str = "algodesk.brokers") -> List[str]:
        """Discover and register third-party broker/data-provider adapters declared via Python entry points."""
        loaded: List[str] = []
        try:
            eps = importlib.metadata.entry_points()
            selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])
        except Exception as exc:
            logger.debug("Broker entry-point discovery skipped: %s", exc)
            return loaded

        for ep in selected:
            try:
                adapter_cls = ep.load()
                if isinstance(adapter_cls, type) and issubclass(adapter_cls, BrokerBase):
                    cls.register_plugin(ep.name, adapter_cls)
                    loaded.append(ep.name.strip().lower())
            except Exception as exc:
                logger.warning("Failed to load broker plugin '%s': %s", getattr(ep, "name", "?"), exc)
        return loaded

    @classmethod
    def get(cls, name: str, *, fresh: bool = False, **kwargs) -> BrokerBase:
        """
        Return the managed shared adapter instance for `name` (preserving
        `TokenBucketRateLimiter` and connection caches across services).
        Pass `fresh=True` or custom kwargs to instantiate an uncached adapter.
        """
        key = name.strip().lower()
        with cls._lock:
            broker_cls = cls.get_class(key)
            if fresh or kwargs:
                return broker_cls(**kwargs)
            inst = cls._instances.get(key)
            if inst is None or not isinstance(inst, broker_cls):
                inst = broker_cls()
                cls._instances[key] = inst
            return inst

    @classmethod
    def reset_instances(cls, name: Optional[str] = None) -> None:
        """Drop cached managed adapter instance(s) (useful on auth refresh or tests)."""
        with cls._lock:
            if name:
                cls._instances.pop(name.strip().lower(), None)
            else:
                cls._instances.clear()

    @classmethod
    def disconnect_all(cls) -> None:
        with cls._lock:
            instances = list(cls._instances.values())
            cls._instances.clear()
        for inst in instances:
            try:
                inst.disconnect()
            except Exception:
                pass

    @classmethod
    def get_class(cls, name: str) -> Type[BrokerBase]:
        key = name.strip().lower()
        with cls._lock:
            if key not in cls._brokers:
                raise ValueError(f"Connection '{name}' not found. Available: {list(cls._order)}")
            return cls._brokers[key]

    @classmethod
    def list_available(cls) -> List[str]:
        with cls._lock:
            return list(cls._order)

    @classmethod
    def list_by_capability(cls, capability_attr: str) -> List[str]:
        with cls._lock:
            out: List[str] = []
            for key in cls._order:
                broker_cls = cls._brokers[key]
                caps = getattr(broker_cls, "capabilities", None)
                if caps is not None and bool(getattr(caps, capability_attr, False)):
                    out.append(key)
            return out

    @classmethod
    def list_trading_brokers(cls) -> List[str]:
        return cls.list_by_capability("supports_trading")

    @classmethod
    def list_market_data_providers(cls) -> List[str]:
        return cls.list_by_capability("supports_market_data")

    @classmethod
    def list_historical_providers(cls) -> List[str]:
        return cls.list_by_capability("supports_historical")

    @classmethod
    def list_ws_providers(cls) -> List[str]:
        return cls.list_by_capability("supports_websocket")

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


# Register built-in broker & market-data adapters
BrokerRegistry.register("upstox", UpstoxBroker)
BrokerRegistry.register("zerodha", ZerodhaBroker)
BrokerRegistry.register("yfinance", YFinanceDataAdapter)
BrokerRegistry.discover_entry_points()
BrokerRegistry.discover_entry_points("algodesk.data_providers")

__all__ = ["BrokerRegistry", "IsolatedBrokerRegistry", "SelectionStore"]
