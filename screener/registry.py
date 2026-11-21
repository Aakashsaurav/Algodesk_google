"""
screener/registry.py
====================
Auto-discovers and hot-reloads drop-in `.py` screen plugins from `screener/screens/`
using file modification timestamps (`mtime_ns`) so edits take effect immediately
on save without restarting the server.
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
from typing import Any, Dict, List, Optional

import screener.base as _screener_base_mod

logger = logging.getLogger(__name__)

_SCREEN_ALIASES = {
    "all": "all_metrics",
}


class ScreenRegistry:
    """Thread-safe mtime-aware plugin loader for `screener/screens/*.py`."""

    def __init__(self, screens_dir: Optional[Path] = None) -> None:
        self._dir = screens_dir or (Path(__file__).resolve().parent / "screens")
        self._base_file = Path(__file__).resolve().parent / "base.py"
        self._base_mtime: Optional[int] = self._safe_mtime(self._base_file)
        self._lock = threading.RLock()
        self._mtimes: Dict[Path, int] = {}
        self._screens: Dict[str, Any] = {}
        self._file_to_ids: Dict[Path, List[str]] = {}
        self._errors: Dict[str, str] = {}

    @staticmethod
    def _safe_mtime(p: Path) -> Optional[int]:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return None

    def _sync_base_module_locked(self) -> bool:
        """Reload `screener.base` if modified on disk while server is running."""
        global _screener_base_mod
        cur_mtime = self._safe_mtime(self._base_file)
        missing_col = not hasattr(_screener_base_mod, "ColumnSpec")
        if cur_mtime is not None and (cur_mtime != self._base_mtime or missing_col):
            try:
                _screener_base_mod = importlib.reload(_screener_base_mod)
                self._base_mtime = cur_mtime
                return True
            except Exception as exc:
                logger.warning("Failed reloading screener.base: %s", exc)
        return False

    @property
    def screens_dir(self) -> Path:
        return self._dir

    def refresh_if_changed(self, force: bool = False) -> bool:
        """Scan directory and reload only new/modified/removed `.py` files."""
        with self._lock:
            changed = False
            if self._sync_base_module_locked():
                force = True
                changed = True

            if not self._dir.exists():
                self._screens.clear()
                self._mtimes.clear()
                self._file_to_ids.clear()
                return False

            current_files = {
                p
                for p in sorted(self._dir.glob("*.py"))
                if p.is_file() and not p.name.startswith("_")
            }

            # Handle deleted files
            removed_files = set(self._mtimes.keys()) - current_files
            for rf in removed_files:
                changed = True
                for sid in self._file_to_ids.pop(rf, []):
                    self._screens.pop(sid, None)
                self._mtimes.pop(rf, None)
                self._errors.pop(rf.name, None)

            # Handle new or modified files
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
        # Clean previous registrations from this file
        for old_id in self._file_to_ids.pop(fpath, []):
            self._screens.pop(old_id, None)
        self._errors.pop(fpath.name, None)
        self._mtimes[fpath] = mtime

        base_cls = getattr(_screener_base_mod, "BaseScreen")
        mod_name = f"algodesk_screen_{fpath.stem}_{mtime}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(fpath))
            if spec is None or spec.loader is None:
                raise ImportError(f"Could not create import spec for {fpath.name}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)

            discovered_ids: List[str] = []
            for _attr_name, obj in inspect.getmembers(mod):
                if inspect.isclass(obj) and issubclass(obj, base_cls) and obj is not base_cls:
                    if inspect.isabstract(obj):
                        continue
                    instance = obj()
                    sid = str(getattr(instance, "id", "") or fpath.stem).strip().lower()
                    if not sid:
                        continue
                    self._screens[sid] = instance
                    discovered_ids.append(sid)

            self._file_to_ids[fpath] = discovered_ids
            if not discovered_ids:
                self._errors[fpath.name] = "No concrete BaseScreen subclass found in file."
        except Exception as exc:
            self._errors[fpath.name] = f"{exc}\n{traceback.format_exc(limit=3)}"
            logger.warning("Failed loading custom screen %s: %s", fpath.name, exc)
        finally:
            sys.modules.pop(mod_name, None)

    def get(self, screen_id: str) -> BaseScreen:
        self.refresh_if_changed()
        clean_id = (screen_id or "all_metrics").strip().lower()
        clean_id = _SCREEN_ALIASES.get(clean_id, clean_id)
        with self._lock:
            if clean_id not in self._screens:
                avail = ", ".join(sorted(self._screens.keys()))
                raise ValueError(f"Unknown screen '{screen_id}'. Available: {avail}")
            return self._screens[clean_id]

    def get_source_path(self, screen_id: str) -> Optional[Path]:
        self.refresh_if_changed()
        clean_id = (screen_id or "all_metrics").strip().lower()
        clean_id = _SCREEN_ALIASES.get(clean_id, clean_id)
        with self._lock:
            for fpath, ids in self._file_to_ids.items():
                if clean_id in ids:
                    return fpath
        return None

    def list_screens(self) -> List[Dict[str, Any]]:
        self.refresh_if_changed()
        with self._lock:
            id_to_file = {
                sid: fpath.name
                for fpath, ids in self._file_to_ids.items()
                for sid in ids
            }
            items = []
            for sid, scr in self._screens.items():
                meta = dict(scr.metadata())
                if sid in id_to_file:
                    meta["source_file"] = id_to_file[sid]
                items.append(meta)
        # Put 'all_metrics' first, then sort alphabetically by name
        items.sort(key=lambda x: (0 if x["id"] == "all_metrics" else 1, x["name"].lower()))
        return items

    def get_errors(self) -> Dict[str, str]:
        self.refresh_if_changed()
        with self._lock:
            return dict(self._errors)


screen_registry = ScreenRegistry()

__all__ = ["ScreenRegistry", "screen_registry"]
