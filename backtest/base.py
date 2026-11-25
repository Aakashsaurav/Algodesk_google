"""
backtest/base.py
================
Canonical backtest & live-compatible strategy base class with
`IndicatorToolkit` & editable `ParamSpec`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from indicators.toolkit import IndicatorToolkit
from screener.base import ParamSpec


class BacktestStrategy(ABC):
    """
    Base class for drop-in strategies in `backtest/strategies/*.py`.
    Offers O(N) pre-computation in `prepare()` and per-bar `(enter, exit)` signals.
    """

    id: str = "custom_strategy"
    name: str = "Custom Strategy"
    description: str = ""
    params: List[ParamSpec] = []

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

    def get_indicator_series(self) -> Dict[str, List[Optional[float]]]:
        """Optional map of indicator name -> per-bar series used by the strategy."""
        return {}


__all__ = ["BacktestStrategy"]
