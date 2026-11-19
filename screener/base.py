"""
screener/base.py
================
Base contracts and editable parameter specifications for drop-in `.py` screens.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from indicators.toolkit import IndicatorToolkit


@dataclass(frozen=True)
class ParamSpec:
    """Declarative parameter schema rendered dynamically in the web UI."""

    name: str
    label: str
    type: str = "float"  # "int" | "float" | "bool" | "str"
    default: Any = 0
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    step: Optional[float] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def coerce(self, raw: Any) -> Any:
        if raw is None or raw == "":
            return self.default
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            return text in {"1", "true", "yes", "on"}
        if self.type == "int":
            val = int(round(float(raw)))
            if self.min_val is not None:
                val = max(int(self.min_val), val)
            if self.max_val is not None:
                val = min(int(self.max_val), val)
            return val
        if self.type == "float":
            val_f = float(raw)
            if self.min_val is not None:
                val_f = max(float(self.min_val), val_f)
            if self.max_val is not None:
                val_f = min(float(self.max_val), val_f)
            return val_f
        return str(raw).strip()


@dataclass
class ScreenMatch:
    """Structured evaluation result returned by a `BaseScreen` implementation."""

    matched: bool
    signal: str = "NEUTRAL"
    tags: List[str] = field(default_factory=list)
    score: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)


class BaseScreen(ABC):
    """
    Base class for all drop-in market screens in `screener/screens/*.py`.
    Any subclass placed in `screener/screens/` is automatically discovered and
    hot-reloaded on save without restarting the server.
    """

    id: str = "custom_screen"
    name: str = "Custom Screen"
    description: str = ""
    category: str = "technical"
    min_bars: int = 15
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
            "category": self.category,
            "min_bars": self.min_bars,
            "params": [p.to_dict() for p in self.params],
        }

    @abstractmethod
    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        params: Dict[str, Any],
        toolkit: IndicatorToolkit,
    ) -> ScreenMatch:
        """Evaluate OHLCV bars (already sliced <= as_of_date) and return ScreenMatch."""


__all__ = ["BaseScreen", "ParamSpec", "ScreenMatch"]
