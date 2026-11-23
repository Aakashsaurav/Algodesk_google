"""
web/security.py
===============
Production-safety authentication, live-trading master lock, and static AST
plugin sandbox guardrails for AlgoDesk.

Guarantees:
1. Default-Off Live Trading Master Lock (`ALGODESK_ENABLE_LIVE_TRADING=false`):
   Prevents accidental real-money order submission until explicitly enabled.
2. Server-Side Bearer Token Auth:
   - Supports static `ALGODESK_API_TOKEN` environment variable.
   - Supports first-run operator passcode claim persisted as a salted `scrypt`
     hash in `data/.admin_auth.json` (0o600), issuing in-memory random Bearer
     session tokens (never hardcoded, never stored in browser localStorage).
   - Enforces auth on mutating/trading/plugin endpoints whenever auth is active
     (`ALGODESK_API_TOKEN` configured, passcode claimed, or `ALGODESK_REQUIRE_AUTH=true`).
3. Static AST Plugin Validator (`validate_python_plugin_ast`):
   Blocks dangerous imports (`subprocess`, `socket`, `shutil`, `ctypes`, `os`,
   `sys`, `urllib`, `requests`, `importlib`, `pickle`, etc.) and unsafe builtins
   (`eval`, `exec`, `compile`, `__import__`, `open`) before writing or importing
   uploaded Python screens or strategies.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config import IST_TZ, config


# Imports strictly forbidden in uploaded Python screens/strategies
_BANNED_ROOT_MODULES: Set[str] = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "ctypes",
    "multiprocessing",
    "threading",
    "signal",
    "pty",
    "fcntl",
    "resource",
    "importlib",
    "builtins",
    "inspect",
    "pathlib",
    "glob",
    "tempfile",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ssl",
    "pickle",
    "marshal",
    "shelve",
    "sqlite3",
    "webbrowser",
    "code",
    "runpy",
    "venv",
    "pip",
}

# Dangerous builtin calls blocked inside plugin code
_BANNED_CALL_NAMES: Set[str] = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "breakpoint",
    "input",
    "globals",
    "locals",
    "vars",
    "delattr",
    "setattr",
}

_BANNED_DUNDER_ATTRS: Set[str] = {
    "__subclasses__",
    "__globals__",
    "__builtins__",
    "__code__",
    "__closure__",
    "__loader__",
    "__spec__",
    "__import__",
    "__mro__",
    "__bases__",
}


def validate_python_plugin_ast(code: str, filename: str = "plugin.py") -> List[str]:
    """
    Validate Python plugin source using static AST inspection.
    Returns a list of violation strings (empty list means safe).
    """
    violations: List[str] = []
    try:
        tree = ast.parse(code, filename=filename)
    except SyntaxError as exc:
        return [f"Syntax error on line {exc.lineno}: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = (alias.name or "").split(".")[0].strip()
                if root_mod in _BANNED_ROOT_MODULES:
                    violations.append(
                        f"Line {node.lineno}: import of '{alias.name}' is blocked by plugin safety policy."
                    )
        elif isinstance(node, ast.ImportFrom):
            root_mod = (node.module or "").split(".")[0].strip()
            if root_mod in _BANNED_ROOT_MODULES:
                violations.append(
                    f"Line {node.lineno}: import from '{node.module}' is blocked by plugin safety policy."
                )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALL_NAMES:
                violations.append(
                    f"Line {node.lineno}: call to '{node.func.id}()' is blocked by plugin safety policy."
                )
            elif isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen", "spawn", "remove", "unlink", "rmdir"}:
                violations.append(
                    f"Line {node.lineno}: call to '.{node.func.attr}()' is blocked by plugin safety policy."
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in _BANNED_DUNDER_ATTRS:
                violations.append(
                    f"Line {node.lineno}: attribute access '{node.attr}' is blocked by plugin safety policy."
                )

    return violations


from config import is_live_trading_enabled as _is_live_trading_enabled


def is_live_trading_enabled() -> bool:
    """
    Master safety switch for real broker order dispatch.
    Defaults strictly to False unless `ALGODESK_ENABLE_LIVE_TRADING=true`.
    """
    return _is_live_trading_enabled()


def _hash_passcode(passcode: str, salt_hex: Optional[str] = None) -> Tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex(), dk.hex()


class SecurityManager:
    """Manages Bearer token auth, first-run scrypt passcode claim, and route protection."""

    def __init__(self, auth_file: Optional[Path] = None, session_ttl_hours: int = 12) -> None:
        self._explicit_auth_file = auth_file
        self._session_ttl = timedelta(hours=max(1, int(session_ttl_hours)))
        self._lock = threading.RLock()
        # session_token -> expires_at_ist
        self._sessions: Dict[str, datetime] = {}

    @property
    def auth_file(self) -> Path:
        return self._explicit_auth_file or (config.DATA_DIR / ".admin_auth.json")

    def _env_token(self) -> str:
        config.reload_if_changed()
        return os.getenv("ALGODESK_API_TOKEN", "").strip()

    def _require_auth_flag(self) -> bool:
        config.reload_if_changed()
        return os.getenv("ALGODESK_REQUIRE_AUTH", "false").strip().lower() in {"1", "true", "yes"}

    def _read_passcode_record(self) -> Optional[Dict[str, Any]]:
        path = self.auth_file
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("salt") and raw.get("hash"):
                return raw
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def is_auth_enforced(self) -> bool:
        """
        True whenever an API token is configured, an operator passcode has been
        claimed, or `ALGODESK_REQUIRE_AUTH=true`.
        """
        return bool(self._env_token() or self._read_passcode_record() or self._require_auth_flag())

    def status(self) -> Dict[str, Any]:
        env_tok = bool(self._env_token())
        has_passcode = self._read_passcode_record() is not None
        enforced = self.is_auth_enforced()
        with self._lock:
            now = datetime.now(tz=IST_TZ)
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            active_sessions = len(self._sessions)
        return {
            "ok": True,
            "auth_enforced": enforced,
            "has_env_token": env_tok,
            "has_passcode": has_passcode,
            "needs_claim": (not env_tok and not has_passcode),
            "active_sessions": active_sessions,
            "live_trading_enabled": is_live_trading_enabled(),
        }

    def claim_passcode(self, passcode: str) -> Dict[str, Any]:
        clean = (passcode or "").strip()
        if len(clean) < 6:
            raise ValueError("Passcode must be at least 6 characters long.")
        with self._lock:
            if self._env_token():
                raise ValueError("ALGODESK_API_TOKEN is already configured in environment. Sign in with that token.")
            if self._read_passcode_record() is not None:
                raise ValueError("Admin passcode is already configured. Sign in instead.")
            salt_hex, hash_hex = _hash_passcode(clean)
            path = self.auth_file
            path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "algorithm": "scrypt",
                "salt": salt_hex,
                "hash": hash_hex,
                "created_at_ist": datetime.now(tz=IST_TZ).isoformat(timespec="seconds"),
            }
            path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            token, expires = self._issue_session_locked()
        return {
            "ok": True,
            "token": token,
            "expires_at_ist": expires.isoformat(timespec="seconds"),
            "status": self.status(),
        }

    def login(self, passcode_or_token: str) -> Dict[str, Any]:
        candidate = (passcode_or_token or "").strip()
        if not candidate:
            raise ValueError("Passcode or API token is required.")

        env_tok = self._env_token()
        if env_tok and hmac.compare_digest(candidate, env_tok):
            with self._lock:
                token, expires = self._issue_session_locked()
            return {
                "ok": True,
                "token": token,
                "expires_at_ist": expires.isoformat(timespec="seconds"),
                "status": self.status(),
            }

        rec = self._read_passcode_record()
        if rec is not None:
            _, calc_hash = _hash_passcode(candidate, salt_hex=str(rec["salt"]))
            if hmac.compare_digest(calc_hash, str(rec["hash"])):
                with self._lock:
                    token, expires = self._issue_session_locked()
                return {
                    "ok": True,
                    "token": token,
                    "expires_at_ist": expires.isoformat(timespec="seconds"),
                    "status": self.status(),
                }

        raise PermissionError("Invalid admin passcode or API token.")

    def _issue_session_locked(self) -> Tuple[str, datetime]:
        token = f"adk_{secrets.token_urlsafe(32)}"
        expires = datetime.now(tz=IST_TZ) + self._session_ttl
        self._sessions[token] = expires
        return token, expires

    def verify_bearer(self, authorization_header: Optional[str]) -> bool:
        """Return True if request is authorized (either auth not enforced or valid Bearer token)."""
        if not self.is_auth_enforced():
            return True
        raw = (authorization_header or "").strip()
        if not raw.lower().startswith("bearer "):
            return False
        supplied = raw[7:].strip()
        if not supplied:
            return False

        env_tok = self._env_token()
        if env_tok and hmac.compare_digest(supplied, env_tok):
            return True

        with self._lock:
            now = datetime.now(tz=IST_TZ)
            exp = self._sessions.get(supplied)
            if exp is not None and exp > now:
                return True
            if exp is not None:
                self._sessions.pop(supplied, None)
        return False

    def is_protected_request(self, method: str, path: str) -> bool:
        """Identify sensitive mutating/trading/credential/plugin routes."""
        m = (method or "GET").upper()
        p = (path or "").strip()
        if m == "GET":
            return False
        # Always allow security claim/login endpoints themselves
        if p in {"/api/security/claim", "/api/security/login"}:
            return False
        protected_prefixes = (
            "/api/streak/plugins/python",
            "/api/streak/orders/",
            "/api/streak/deployments/",
            "/api/live/kill-switch",
            "/api/preflight/kill-switch",
            "/api/analytics/risk",
            "/api/brokers/",
            "/api/selection",
            "/api/selections",
            "/api/notifications/telegram",
        )
        return any(p.startswith(prefix) for prefix in protected_prefixes)


security_manager = SecurityManager()

__all__ = [
    "SecurityManager",
    "is_live_trading_enabled",
    "security_manager",
    "validate_python_plugin_ast",
]
