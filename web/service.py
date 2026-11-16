"""
web/service.py
==============
Core application service powering AlgoDesk's Broker & Data Connectivity Hub.

Memory-conscious for MacBook Air M2 (8GB):
- Never loads the 100k+ Upstox instrument master JSON.
- Probes registered adapters concurrently with short timeouts (<=3.5s).
- Validates trading vs. data selections against adapter capabilities & stage.
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from broker.base import BrokerHealthSnapshot, ConnectionState
from broker.registry import BrokerRegistry, SelectionStore
from broker.upstox.auth import auth_manager
from config import IST_TZ, config

logger = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    clean = (key or "").strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "••••"
    return f"{clean[:4]}{'•' * 4}{clean[-3:]}"


class BrokerHubService:
    """Thread-safe orchestration service for broker probing, OAuth reconnect, and routing."""

    def __init__(self, selection_store: Optional[SelectionStore] = None) -> None:
        self._store = selection_store or SelectionStore()
        self._lock = threading.RLock()
        self._cached_snapshots: Dict[str, BrokerHealthSnapshot] = {}

    def _probe_single(self, broker_id: str, timeout: float = 3.5) -> BrokerHealthSnapshot:
        adapter = BrokerRegistry.get(broker_id)
        snap = adapter.health_check(timeout=timeout)
        with self._lock:
            self._cached_snapshots[broker_id] = snap
        return snap

    def probe_all(self, timeout: float = 3.5) -> List[Dict[str, Any]]:
        """Probe every registered broker/data adapter concurrently."""
        broker_ids = BrokerRegistry.list_available()
        results: Dict[str, BrokerHealthSnapshot] = {}

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(broker_ids)))) as pool:
            future_map = {
                pool.submit(self._probe_single, bid, timeout): bid
                for bid in broker_ids
            }
            for fut in as_completed(future_map):
                bid = future_map[fut]
                try:
                    results[bid] = fut.result()
                except Exception as exc:
                    adapter = BrokerRegistry.get(bid)
                    results[bid] = BrokerHealthSnapshot(
                        broker_id=bid,
                        display_name=adapter.display_name,
                        stage=adapter.stage,
                        state=ConnectionState.ERROR,
                        capabilities=adapter.capabilities,
                        message=f"Probe failed: {exc}",
                        can_reconnect=adapter.capabilities.requires_oauth,
                    )

        return [results[bid].to_dict() for bid in broker_ids]

    def probe_one(self, broker_id: str, timeout: float = 3.5) -> Dict[str, Any]:
        snap = self._probe_single(broker_id, timeout=timeout)
        return snap.to_dict()

    def get_overview(self, refresh: bool = True) -> Dict[str, Any]:
        if refresh or not self._cached_snapshots:
            brokers = self.probe_all()
        else:
            broker_ids = BrokerRegistry.list_available()
            brokers = [self._cached_snapshots[bid].to_dict() for bid in broker_ids if bid in self._cached_snapshots]

        selections = self._store.load()
        return {
            "brokers": brokers,
            "selections": selections,
            "credentials": {
                "upstox": {
                    "has_api_key": bool(config.UPSTOX_API_KEY),
                    "has_api_secret": bool(config.UPSTOX_API_SECRET),
                    "api_key_preview": _mask_key(config.UPSTOX_API_KEY),
                    "redirect_uri": config.UPSTOX_REDIRECT_URI,
                }
            },
            "runtime": {
                "python_version": platform.python_version(),
                "machine": platform.machine(),
                "system": platform.system(),
                "ist_time": datetime.now(tz=IST_TZ).strftime("%d %b %Y, %H:%M:%S IST"),
                "eager_instruments_loaded": False,
                "executable": sys.executable,
            },
        }

    def save_selection(self, trading_broker: str, data_connection: str) -> Dict[str, Any]:
        t_id = trading_broker.strip().lower()
        d_id = data_connection.strip().lower()

        t_adapter = BrokerRegistry.get(t_id)
        d_adapter = BrokerRegistry.get(d_id)

        if t_adapter.stage == "planned":
            raise ValueError(f"{t_adapter.display_name} is planned for Phase 2 and cannot be activated for trading yet.")
        if not t_adapter.capabilities.supports_trading:
            raise ValueError(f"{t_adapter.display_name} does not support order execution.")

        if d_adapter.stage == "planned":
            raise ValueError(f"{d_adapter.display_name} is planned for Phase 2 and cannot be activated for market data yet.")
        if not d_adapter.capabilities.supports_market_data:
            raise ValueError(f"{d_adapter.display_name} does not support market data.")

        saved = self._store.save(trading_broker=t_id, data_connection=d_id)
        return {"ok": True, "selections": saved}

    def save_credentials(
        self,
        broker_id: str,
        api_key: str,
        api_secret: str,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        if bid != "upstox":
            raise ValueError(f"Credential configuration is not active for '{broker_id}' yet.")
        auth_manager.save_credentials(api_key=api_key, api_secret=api_secret, redirect_uri=redirect_uri)
        snap = self.probe_one(bid)
        return {
            "ok": True,
            "broker": snap,
            "credentials": {
                "has_api_key": bool(config.UPSTOX_API_KEY),
                "has_api_secret": bool(config.UPSTOX_API_SECRET),
                "api_key_preview": _mask_key(config.UPSTOX_API_KEY),
                "redirect_uri": config.UPSTOX_REDIRECT_URI,
            },
        }

    def start_reconnect(self, broker_id: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        adapter = BrokerRegistry.get(bid)
        if adapter.stage == "planned":
            raise ValueError(f"{adapter.display_name} is planned for Phase 2.")

        if not adapter.capabilities.requires_oauth:
            snap = self.probe_one(bid)
            return {"mode": "probe", "broker": snap}

        if bid == "upstox" and not auth_manager.has_credentials():
            return {
                "mode": "needs_credentials",
                "broker_id": bid,
                "redirect_uri": redirect_uri or config.UPSTOX_REDIRECT_URI,
                "message": "Enter your Upstox API Key and Secret to generate the OAuth login URL.",
            }

        login_url = adapter.get_login_url(redirect_uri=redirect_uri)
        return {
            "mode": "oauth",
            "broker_id": bid,
            "login_url": login_url,
            "redirect_uri": redirect_uri or config.UPSTOX_REDIRECT_URI,
        }

    def complete_reconnect(
        self,
        broker_id: str,
        code_or_url: str,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        adapter = BrokerRegistry.get(bid)
        snap = adapter.complete_auth(code_or_url=code_or_url, redirect_uri=redirect_uri)
        with self._lock:
            self._cached_snapshots[bid] = snap
        return {"ok": snap.is_connected, "broker": snap.to_dict()}

    def disconnect_broker(self, broker_id: str) -> Dict[str, Any]:
        bid = broker_id.strip().lower()
        adapter = BrokerRegistry.get(bid)
        adapter.disconnect()
        snap = self.probe_one(bid)
        return {"ok": True, "broker": snap}


hub_service = BrokerHubService()

__all__ = ["BrokerHubService", "hub_service"]
