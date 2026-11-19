"""
core/jobs.py
============
Lightweight ThreadPool-backed AsyncJobRunner with cooperative cancellation
for long-running screener scans (e.g. NIFTY 500) and backtests.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from config import IST_TZ


class JobCancelledError(RuntimeError):
    """Raised cooperatively when an operator cancels an active background job."""


class AsyncJobRunner:
    """2-worker thread pool with bounded ring buffer and cancellation events."""

    def __init__(self, max_workers: int = 2, max_history: int = 50) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="AlgoDeskJob")
        self._lock = threading.RLock()
        self._jobs: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._cancel_events: Dict[str, threading.Event] = {}
        self._max_history = max_history

    def _prune_locked(self) -> None:
        while len(self._jobs) > self._max_history:
            oldest_id, _ = self._jobs.popitem(last=False)
            self._cancel_events.pop(oldest_id, None)

    def submit(
        self,
        job_type: str,
        meta: Dict[str, Any],
        fn: Callable[[Callable[[int, int, str], None], threading.Event], Dict[str, Any]],
    ) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        cancel_ev = threading.Event()
        record: Dict[str, Any] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "QUEUED",
            "progress_pct": 0,
            "progress_message": "Queued...",
            "created_at_ist": now_ist,
            "finished_at_ist": None,
            "meta": dict(meta),
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = record
            self._cancel_events[job_id] = cancel_ev
            self._prune_locked()

        def _progress(done: int, total: int, message: str) -> None:
            pct = int(round((done / float(total)) * 100.0)) if total > 0 else 0
            with self._lock:
                if job_id in self._jobs and self._jobs[job_id]["status"] not in {"CANCELLED", "COMPLETED", "FAILED"}:
                    self._jobs[job_id]["status"] = "RUNNING"
                    self._jobs[job_id]["progress_pct"] = max(0, min(100, pct))
                    self._jobs[job_id]["progress_message"] = message

        def _worker() -> None:
            if cancel_ev.is_set():
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "CANCELLED"
                        self._jobs[job_id]["progress_message"] = "Cancelled before execution."
                        self._jobs[job_id]["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
                return

            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["status"] = "RUNNING"
                    self._jobs[job_id]["progress_message"] = f"Starting {job_type}..."
            try:
                res = fn(_progress, cancel_ev)
                with self._lock:
                    if job_id in self._jobs:
                        if cancel_ev.is_set():
                            self._jobs[job_id]["status"] = "CANCELLED"
                            self._jobs[job_id]["progress_message"] = "Cancelled by operator."
                        else:
                            self._jobs[job_id]["status"] = "COMPLETED"
                            self._jobs[job_id]["progress_pct"] = 100
                            self._jobs[job_id]["progress_message"] = "Completed"
                            self._jobs[job_id]["result"] = res
                        self._jobs[job_id]["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            except JobCancelledError as exc:
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "CANCELLED"
                        self._jobs[job_id]["progress_message"] = str(exc) or "Cancelled by operator."
                        self._jobs[job_id]["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            except Exception as exc:
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id]["status"] = "FAILED"
                        self._jobs[job_id]["progress_pct"] = 100
                        self._jobs[job_id]["progress_message"] = str(exc)
                        self._jobs[job_id]["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
                        self._jobs[job_id]["error"] = str(exc)

        self._pool.submit(_worker)
        return self.get_job(job_id) or record

    def run_sync(
        self,
        job_type: str,
        meta: Dict[str, Any],
        fn: Callable[[Callable[[int, int, str], None], threading.Event], Dict[str, Any]],
    ) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now_ist = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        cancel_ev = threading.Event()
        record: Dict[str, Any] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "RUNNING",
            "progress_pct": 0,
            "progress_message": f"Running {job_type}...",
            "created_at_ist": now_ist,
            "finished_at_ist": None,
            "meta": dict(meta),
            "result": None,
            "error": None,
        }
        res = fn(lambda _d, _t, _m: None, cancel_ev)
        record["status"] = "COMPLETED"
        record["progress_pct"] = 100
        record["progress_message"] = "Completed"
        record["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
        record["result"] = res
        with self._lock:
            self._jobs[job_id] = record
            self._cancel_events[job_id] = cancel_ev
            self._prune_locked()
        return record

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError(f"Job '{job_id}' not found.")
            ev = self._cancel_events.get(job_id)
            if ev is not None:
                ev.set()
            if job["status"] in {"QUEUED", "RUNNING"}:
                job["status"] = "CANCELLED"
                job["progress_message"] = "Cancellation requested by operator..."
                job["finished_at_ist"] = datetime.now(tz=IST_TZ).isoformat(timespec="seconds")
            return dict(job)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._jobs.values())
        items.reverse()
        capped = max(1, min(int(limit), 50))
        out: List[Dict[str, Any]] = []
        for item in items[:capped]:
            slim = dict(item)
            if isinstance(slim.get("result"), dict) and "equity_curve" in slim["result"]:
                slim["result"] = {k: v for k, v in slim["result"].items() if k not in {"equity_curve", "trades"}}
            out.append(slim)
        return out


analytics_job_runner = AsyncJobRunner()

__all__ = ["AsyncJobRunner", "JobCancelledError", "analytics_job_runner"]
