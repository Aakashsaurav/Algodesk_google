"""
backtest/registry.py
====================
Auto-discovers and hot-reloads drop-in `.py` strategy files from
`backtest/strategies/*.py` using file modification timestamps (`mtime_ns`).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import backtest.base as _backtest_base_mod

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """Thread-safe mtime-aware loader for `backtest/strategies/*.py`."""

    def __init__(self, strategies_dir: Optional[Path] = None) -> None:
        self._dir = strategies_dir or (Path(__file__).resolve().parent / "strategies")
        self._base_file = Path(__file__).resolve().parent / "base.py"
        self._base_mtime: Optional[int] = self._safe_mtime(self._base_file)
        self._lock = threading.RLock()
        self._mtimes: Dict[Path, int] = {}
        self._classes: Dict[str, Type[Any]] = {}
        self._prototypes: Dict[str, Any] = {}
        self._file_to_ids: Dict[Path, List[str]] = {}
        self._errors: Dict[str, str] = {}

    @staticmethod
    def _safe_mtime(p: Path) -> Optional[int]:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return None

    def _sync_base_module_locked(self) -> bool:
        global _backtest_base_mod
        cur_mtime = self._safe_mtime(self._base_file)
        if cur_mtime is not None and cur_mtime != self._base_mtime:
            try:
                _backtest_base_mod = importlib.reload(_backtest_base_mod)
                self._base_mtime = cur_mtime
                return True
            except Exception as exc:
                logger.warning("Failed reloading backtest.base: %s", exc)
        return False

    @property
    def strategies_dir(self) -> Path:
        return self._dir

    def refresh_if_changed(self, force: bool = False) -> bool:
        with self._lock:
            changed = False
            if self._sync_base_module_locked():
                force = True
                changed = True

            if not self._dir.exists():
                self._classes.clear()
                self._prototypes.clear()
                self._mtimes.clear()
                self._file_to_ids.clear()
                return False

            current_files = {
                p
                for p in sorted(self._dir.glob("*.py"))
                if p.is_file() and not p.name.startswith("_")
            }

            for rf in set(self._mtimes.keys()) - current_files:
                changed = True
                for sid in self._file_to_ids.pop(rf, []):
                    self._classes.pop(sid, None)
                    self._prototypes.pop(sid, None)
                self._mtimes.pop(rf, None)
                self._errors.pop(rf.name, None)

            for fpath in current_files:
                try:
                    mtime = fpath.stat().st_mtime_ns
                except OSError:
                    continue
                if not force and self._mtimes.get(fpath) == mtime:
                    continue
                changed = True
                self._load_file_locked(fpath, mtime)

            return changed

    def _load_file_locked(self, fpath: Path, mtime: int) -> None:
        for old_id in self._file_to_ids.pop(fpath, []):
            self._classes.pop(old_id, None)
            self._prototypes.pop(old_id, None)
        self._errors.pop(fpath.name, None)
        self._mtimes[fpath] = mtime

        base_cls = getattr(_backtest_base_mod, "BacktestStrategy")
        mod_name = f"algodesk_strategy_{fpath.stem}_{mtime}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(fpath))
            if spec is None or spec.loader is None:
                raise ImportError(f"Could not load strategy spec for {fpath.name}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)

            discovered_ids: List[str] = []
            for _attr, obj in inspect.getmembers(mod):
                if inspect.isclass(obj) and issubclass(obj, base_cls) and obj is not base_cls:
                    if inspect.isabstract(obj):
                        continue
                    inst = obj()
                    sid = str(getattr(inst, "id", "") or fpath.stem).strip().lower()
                    if not sid:
                        continue
                    self._classes[sid] = obj
                    self._prototypes[sid] = inst
                    discovered_ids.append(sid)

            self._file_to_ids[fpath] = discovered_ids
            if not discovered_ids:
                self._errors[fpath.name] = "No concrete BacktestStrategy subclass found in file."
        except Exception as exc:
            self._errors[fpath.name] = f"{exc}\n{traceback.format_exc(limit=3)}"
            logger.warning("Failed loading strategy %s: %s", fpath.name, exc)
        finally:
            sys.modules.pop(mod_name, None)

    def create(self, strategy_id: str) -> BacktestStrategy:
        self.refresh_if_changed()
        clean_id = (strategy_id or "ema_crossover").strip().lower()
        with self._lock:
            cls = self._classes.get(clean_id)
            if cls is None:
                avail = ", ".join(sorted(self._classes.keys()))
                raise ValueError(f"Unknown strategy '{strategy_id}'. Supported: {avail}")
            return cls()

    def list_strategies(self) -> List[Dict[str, Any]]:
        self.refresh_if_changed()
        with self._lock:
            items = [proto.metadata() for proto in self._prototypes.values()]
        items.sort(key=lambda x: x["name"].lower())
        return items

    def list_strategy_labels(self) -> Dict[str, str]:
        self.refresh_if_changed()
        with self._lock:
            return {sid: proto.name for sid, proto in self._prototypes.items()}

    def get_errors(self) -> Dict[str, str]:
        self.refresh_if_changed()
        with self._lock:
            return dict(self._errors)


strategy_registry = StrategyRegistry()

__all__ = ["StrategyRegistry", "strategy_registry"]
