"""
backtest/base.py
================
Canonical backtest & live-compatible strategy base class extending
`core.runtime.BaseStrategy` with `IndicatorToolkit` & editable `ParamSpec`.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from core.runtime import BaseStrategy, OrderRequest
from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class BacktestStrategy(BaseStrategy):
    """
    Base class for drop-in strategies in `backtest/strategies/*.py`.
    Implements `core.runtime.BaseStrategy.on_bar` while offering fast O(N)
    pre-computation in `prepare()` and per-bar `(enter, exit)` signals.
    """

    id: str = "custom_strategy"
    name: str = "Custom Strategy"
    description: str = ""
    params: List[ParamSpec] = []

    def __init__(self) -> None:
        self._live_bars: List[Dict[str, Any]] = []
        self._in_position: bool = False

    def resolve_params(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = dict(overrides or {})
        resolved: Dict[str, Any] = {}
        for spec in self.params:
            resolved[spec.name] = spec.coerce(raw.get(spec.name, spec.default))
        return resolved

    def metadata(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }

    @abstractmethod
    def prepare(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> None:
        """Pre-compute O(N) indicator series once before iterating bars."""

    @abstractmethod
    def signal_at(self, index: int, bar: Dict[str, Any]) -> Tuple[bool, bool]:
        """Return `(enter_long, exit_long)` booleans at bar `index`."""

    def on_bar(self, bar: Dict[str, Any]) -> Optional[OrderRequest]:
        """Bridge `core.runtime.BaseStrategy` for live bar-by-bar evaluation."""
        self._live_bars.append(bar)
        if len(self._live_bars) < 2:
            return None
        resolved = self.resolve_params()
        tk = IndicatorToolkit(self._live_bars)
        self.prepare(self._live_bars, resolved, tk)
        enter_sig, exit_sig = self.signal_at(len(self._live_bars) - 1, bar)
        sym = str(bar.get("symbol") or "RELIANCE")
        exch = str(bar.get("exchange") or "NSE")
        if not self._in_position and enter_sig:
            self._in_position = True
            return OrderRequest(trading_symbol=sym, exchange=exch, transaction_type="BUY", quantity=1, tag=self.id)
        if self._in_position and exit_sig:
            self._in_position = False
            return OrderRequest(trading_symbol=sym, exchange=exch, transaction_type="SELL", quantity=1, tag=self.id)
        return None


__all__ = ["BacktestStrategy"]
