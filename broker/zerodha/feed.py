"""
broker/zerodha/feed.py
----------------------
Lazy-loaded Zerodha KiteTicker WebSocket market-data adapter.

Designed for MacBook Air M2 (8GB): does not import `kiteconnect` at module
load time; SDK loading happens only when live WebSocket streaming is started.
Allows routing live market data through Zerodha even while executing trades
on Upstox (or vice versa).
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


class KiteTickerAdapter:
    """Modular WebSocket market-data adapter wrapping kiteconnect.KiteTicker lazily."""

    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key.strip()
        self.access_token = access_token.strip()
        self._lock = threading.RLock()
        self._ticker: Any = None
        self._connected = False
        self._subscribed_tokens: Set[int] = set()
        self._mode = self.MODE_QUOTE
        self._callbacks: List[Callable[[List[Dict[str, Any]]], None]] = []
        self._last_error: str = ""

    @property
    def is_connected(self) -> bool:
        return self._connected

    def add_tick_callback(self, callback: Callable[[List[Dict[str, Any]]], None]) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def subscribe(self, instrument_tokens: Iterable[int], mode: str = MODE_QUOTE) -> None:
        tokens = [int(t) for t in instrument_tokens]
        if not tokens:
            return
        with self._lock:
            self._subscribed_tokens.update(tokens)
            self._mode = mode
            if self._ticker is not None and self._connected:
                self._ticker.subscribe(tokens)
                self._ticker.set_mode(mode, tokens)

    def unsubscribe(self, instrument_tokens: Iterable[int]) -> None:
        tokens = [int(t) for t in instrument_tokens]
        if not tokens:
            return
        with self._lock:
            self._subscribed_tokens.difference_update(tokens)
            if self._ticker is not None and self._connected:
                self._ticker.unsubscribe(tokens)

    def start(self, threaded: bool = True) -> bool:
        if not self.api_key or not self.access_token:
            self._last_error = "Zerodha API key and valid access_token are required to stream KiteTicker."
            return False

        try:
            kc = importlib.import_module("kiteconnect")
            ticker_cls = getattr(kc, "KiteTicker", None)
            if ticker_cls is None:
                self._last_error = "kiteconnect package does not expose KiteTicker."
                return False
        except Exception as exc:
            self._last_error = f"kiteconnect SDK not installed ({exc})."
            return False

        ticker = ticker_cls(self.api_key, self.access_token)

        def _on_ticks(_ws: Any, ticks: List[Dict[str, Any]]) -> None:
            for cb in list(self._callbacks):
                try:
                    cb(ticks)
                except Exception as exc:
                    logger.debug("KiteTicker callback warning: %s", exc)

        def _on_connect(ws: Any, _response: Any) -> None:
            with self._lock:
                self._connected = True
                self._last_error = ""
                tokens = sorted(self._subscribed_tokens)
                if tokens:
                    ws.subscribe(tokens)
                    ws.set_mode(self._mode, tokens)

        def _on_close(_ws: Any, _code: Any, _reason: Any) -> None:
            with self._lock:
                self._connected = False

        def _on_error(_ws: Any, _code: Any, reason: Any) -> None:
            with self._lock:
                self._last_error = str(reason)

        ticker.on_ticks = _on_ticks
        ticker.on_connect = _on_connect
        ticker.on_close = _on_close
        ticker.on_error = _on_error

        with self._lock:
            self._ticker = ticker

        ticker.connect(threaded=threaded)
        return True

    def stop(self) -> None:
        with self._lock:
            self._connected = False
            if self._ticker is not None:
                try:
                    self._ticker.close()
                except Exception:
                    pass
                self._ticker = None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "provider": "zerodha",
                "connected": self._connected,
                "subscribed_count": len(self._subscribed_tokens),
                "mode": self._mode,
                "last_error": self._last_error,
            }


__all__ = ["KiteTickerAdapter"]
