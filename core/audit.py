"""
core/audit.py
=============
Phase 4 Immutable SEBI Hash-Chained Audit Trail (`data/sqlite/audit_chain.db`).

Design:
- Append-only SQLite WAL ledger linking every critical trading safety event
  (kill switch activation/reset, feed failover, preflight gate, order events).
- Cryptographic chaining:
    payload_hash = SHA-256(canonical JSON of payload)
    prev_hash    = chain_hash of previous row ("0" * 64 for genesis)
    chain_hash   = SHA-256(f"{prev_hash}|{payload_hash}|{ts_ist}|{event_type}|{actor}")
- Tamper-evident verification via `verify_chain()`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import IST_TZ, config

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))


class AuditTrail:
    """Thread-safe, append-only SEBI hash-chained audit ledger backed by SQLite."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._explicit_path = db_path
        self._lock = threading.RLock()
        self._initialized_paths: set[Path] = set()

    @property
    def db_path(self) -> Path:
        return self._explicit_path or config.AUDIT_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        if path not in self._initialized_paths:
            with self._lock:
                if path not in self._initialized_paths:
                    self._init_schema(conn)
                    self._initialized_paths.add(path)
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_chain (
                id TEXT PRIMARY KEY,
                ts_ist TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                chain_hash TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_chain (ts_ist);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_chain (event_type);")
        conn.commit()

    def log(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        clean_type = (event_type or "SYSTEM_EVENT").strip().upper()
        clean_actor = (actor or "system").strip()
        payload_dict = dict(payload or {})
        payload_json = _canonical_json(payload_dict)
        payload_hash = _sha256_hex(payload_json)
        ts_ist = datetime.now(tz=IST_TZ).isoformat(timespec="microseconds")
        event_id = uuid.uuid4().hex[:16]

        with self._lock:
            with self._connect() as conn:
                head = conn.execute(
                    "SELECT chain_hash FROM audit_chain ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                prev_hash = str(head["chain_hash"]) if head else GENESIS_HASH
                chain_hash = _sha256_hex(
                    f"{prev_hash}|{payload_hash}|{ts_ist}|{clean_type}|{clean_actor}"
                )
                conn.execute(
                    """
                    INSERT INTO audit_chain
                    (id, ts_ist, event_type, actor, payload, payload_hash, prev_hash, chain_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        ts_ist,
                        clean_type,
                        clean_actor,
                        payload_json,
                        payload_hash,
                        prev_hash,
                        chain_hash,
                    ),
                )
                conn.commit()

        return {
            "id": event_id,
            "ts_ist": ts_ist,
            "event_type": clean_type,
            "actor": clean_actor,
            "payload": payload_dict,
            "payload_hash": payload_hash,
            "prev_hash": prev_hash,
            "chain_hash": chain_hash,
        }

    def log_kill_switch(
        self,
        *,
        engaged: bool,
        reason: str,
        actor: str = "user",
        trading_broker: str = "",
        orders_cancelled: int = 0,
    ) -> Dict[str, Any]:
        return self.log(
            event_type="KILL_SWITCH_ENGAGED" if engaged else "KILL_SWITCH_RESET",
            actor=actor,
            payload={
                "engaged": bool(engaged),
                "reason": reason,
                "trading_broker": trading_broker,
                "orders_cancelled": int(orders_cancelled),
            },
        )

    def log_failover(
        self,
        *,
        from_feed: str,
        to_feed: str,
        reason: str,
        actor: str = "feed_manager",
    ) -> Dict[str, Any]:
        return self.log(
            event_type="FEED_FAILOVER",
            actor=actor,
            payload={
                "from_feed": from_feed,
                "to_feed": to_feed,
                "reason": reason,
            },
        )

    def log_preflight(
        self,
        *,
        overall_ready: bool,
        passed_count: int,
        total_count: int,
        trading_broker: str,
        data_connection: str,
        actor: str = "user",
    ) -> Dict[str, Any]:
        return self.log(
            event_type="PREFLIGHT_CHECK",
            actor=actor,
            payload={
                "overall_ready": bool(overall_ready),
                "passed_count": int(passed_count),
                "total_count": int(total_count),
                "trading_broker": trading_broker,
                "data_connection": data_connection,
            },
        )

    def verify_chain(self) -> Dict[str, Any]:
        """Walk the entire audit ledger from genesis and verify cryptographic integrity."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts_ist, event_type, actor, payload, payload_hash, prev_hash, chain_hash
                    FROM audit_chain
                    ORDER BY rowid ASC
                    """
                ).fetchall()

        expected_prev = GENESIS_HASH
        for idx, row in enumerate(rows):
            if str(row["prev_hash"]) != expected_prev:
                return {
                    "valid": False,
                    "total_events": len(rows),
                    "broken_index": idx,
                    "broken_event_id": row["id"],
                    "reason": "prev_hash linkage mismatch",
                    "head_hash": expected_prev,
                }
            recomputed_payload_hash = _sha256_hex(str(row["payload"]))
            if recomputed_payload_hash != str(row["payload_hash"]):
                return {
                    "valid": False,
                    "total_events": len(rows),
                    "broken_index": idx,
                    "broken_event_id": row["id"],
                    "reason": "payload_hash tampered or corrupted",
                    "head_hash": expected_prev,
                }
            recomputed_chain = _sha256_hex(
                f"{row['prev_hash']}|{row['payload_hash']}|{row['ts_ist']}|{row['event_type']}|{row['actor']}"
            )
            if recomputed_chain != str(row["chain_hash"]):
                return {
                    "valid": False,
                    "total_events": len(rows),
                    "broken_index": idx,
                    "broken_event_id": row["id"],
                    "reason": "chain_hash mismatch",
                    "head_hash": expected_prev,
                }
            expected_prev = str(row["chain_hash"])

        return {
            "valid": True,
            "total_events": len(rows),
            "broken_index": None,
            "broken_event_id": None,
            "reason": "All cryptographic hash links verified." if rows else "Audit ledger initialized (0 events yet).",
            "head_hash": expected_prev,
        }

    def list_events(self, limit: int = 25) -> List[Dict[str, Any]]:
        capped = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts_ist, event_type, actor, payload, payload_hash, prev_hash, chain_hash
                FROM audit_chain
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (capped,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                parsed_payload = json.loads(r["payload"])
            except Exception:
                parsed_payload = {"raw": r["payload"]}
            out.append(
                {
                    "id": r["id"],
                    "ts_ist": r["ts_ist"],
                    "event_type": r["event_type"],
                    "actor": r["actor"],
                    "payload": parsed_payload,
                    "payload_hash": r["payload_hash"],
                    "prev_hash": r["prev_hash"],
                    "chain_hash": r["chain_hash"],
                }
            )
        return out


audit_trail = AuditTrail()

__all__ = ["AuditTrail", "GENESIS_HASH", "audit_trail"]
