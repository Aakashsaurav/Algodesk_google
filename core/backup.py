"""
core/backup.py
==============
Verified SQLite backup and restore utility for AlgoDesk's stateful operational
databases (`oms.db`, `deployments.db`, `ohlcv.db`, `instruments.db`,
`audit_chain.db`, `universes.db`) using SQLite's online `.backup()` API and
`PRAGMA integrity_check`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import IST_TZ, config


def _default_databases() -> Dict[str, Path]:
    return {
        "oms": config.OMS_DB_PATH,
        "deployments": config.SQLITE_DIR / "deployments.db",
        "ohlcv": config.OHLCV_DB_PATH,
        "instruments": config.INSTRUMENTS_DB_PATH,
        "audit": config.AUDIT_DB_PATH,
        "universes": config.SQLITE_DIR / "universes.db",
    }


def verify_sqlite_integrity(db_path: Path) -> bool:
    """Run SQLite PRAGMA integrity_check on a database file."""
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        return bool(row and str(row[0]).strip().lower() == "ok")
    finally:
        conn.close()


class DatabaseBackupManager:
    """Creates and restores consistent point-in-time SQLite database snapshots."""

    def __init__(
        self,
        backup_root: Optional[Path] = None,
        databases: Optional[Dict[str, Path]] = None,
    ) -> None:
        self._explicit_root = backup_root
        self._explicit_dbs = databases

    @property
    def backup_root(self) -> Path:
        return self._explicit_root or (config.DATA_DIR / "backups")

    def _resolve_databases(self) -> Dict[str, Path]:
        return dict(self._explicit_dbs) if self._explicit_dbs is not None else _default_databases()

    def create_backup(
        self,
        *,
        label: str = "",
        include: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a verified online SQLite backup snapshot of all existing state DBs."""
        now_ist = datetime.now(tz=IST_TZ)
        safe_label = "".join(c if (c.isalnum() or c in {"-", "_"}) else "_" for c in (label or "").strip())[:32]
        snap_id = now_ist.strftime("snap_%Y%m%d_%H%M%S") + (f"_{safe_label}" if safe_label else "")
        snap_dir = self.backup_root / snap_id
        snap_dir.mkdir(parents=True, exist_ok=True)

        dbs = self._resolve_databases()
        wanted = {k.strip().lower() for k in include} if include else None
        backed_up: Dict[str, Dict[str, Any]] = {}
        skipped: List[str] = []

        for name, src_path in dbs.items():
            if wanted is not None and name not in wanted:
                continue
            if not src_path.exists():
                skipped.append(name)
                continue
            dst_path = snap_dir / f"{name}.db"
            src_conn = sqlite3.connect(str(src_path), timeout=15.0)
            dst_conn = sqlite3.connect(str(dst_path), timeout=15.0)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

            integrity_ok = verify_sqlite_integrity(dst_path)
            if not integrity_ok:
                dst_path.unlink(missing_ok=True)
                raise RuntimeError(f"Backup integrity check failed for database '{name}'.")
            backed_up[name] = {
                "filename": dst_path.name,
                "source_path": str(src_path),
                "size_bytes": dst_path.stat().st_size,
                "integrity_ok": True,
            }

        manifest = {
            "snapshot_id": snap_id,
            "label": safe_label,
            "created_at_ist": now_ist.isoformat(timespec="seconds"),
            "databases": backed_up,
            "skipped": skipped,
        }
        (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {"ok": True, **manifest, "backup_dir": str(snap_dir)}

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backup snapshots ordered newest first."""
        root = self.backup_root
        if not root.exists():
            return []
        snaps: List[Dict[str, Any]] = []
        for child in sorted(root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            m_file = child / "manifest.json"
            if not m_file.exists():
                continue
            try:
                data = json.loads(m_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["backup_dir"] = str(child)
                    snaps.append(data)
            except Exception:
                continue
        return snaps

    def restore_backup(
        self,
        snapshot_id: str,
        *,
        targets: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Restore verified SQLite database files from a named backup snapshot.
        Validates integrity BEFORE replacing target files and writes atomically via temp file.
        """
        safe_id = "".join(c for c in (snapshot_id or "").strip() if c.isalnum() or c in {"-", "_", "."})
        if not safe_id or ".." in safe_id:
            raise ValueError("Invalid snapshot_id.")
        snap_dir = (self.backup_root / safe_id).resolve()
        if not snap_dir.exists() or not snap_dir.is_dir() or self.backup_root.resolve() not in snap_dir.parents:
            raise FileNotFoundError(f"Backup snapshot '{snapshot_id}' was not found.")

        m_file = snap_dir / "manifest.json"
        if not m_file.exists():
            raise FileNotFoundError(f"Snapshot manifest missing in '{snapshot_id}'.")
        manifest = json.loads(m_file.read_text(encoding="utf-8"))
        snap_dbs: Dict[str, Any] = manifest.get("databases") or {}
        live_dbs = self._resolve_databases()
        wanted = {t.strip().lower() for t in targets} if targets else None

        restored: List[str] = []
        for name, meta in snap_dbs.items():
            if wanted is not None and name not in wanted:
                continue
            if name not in live_dbs:
                continue
            src_file = snap_dir / str(meta["filename"])
            if not verify_sqlite_integrity(src_file):
                raise RuntimeError(f"Snapshot file for '{name}' failed SQLite integrity check; restore aborted.")
            dst_path = live_dbs[name]
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_dst = dst_path.with_suffix(".restore.tmp")
            shutil.copy2(src_file, tmp_dst)
            # Clear stale WAL/SHM sidecars if present before atomic swap
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{dst_path}{suffix}")
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
            os.replace(tmp_dst, dst_path)
            restored.append(name)

        return {
            "ok": True,
            "snapshot_id": safe_id,
            "restored_databases": restored,
            "restored_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
        }


backup_manager = DatabaseBackupManager()

__all__ = [
    "DatabaseBackupManager",
    "backup_manager",
    "verify_sqlite_integrity",
]
