"""
web/security.py
===============
Production-safety authentication, live-trading master lock, login throttling,
one-time bootstrap claim secret, and static AST defense-in-depth checks for
Admin-Only Trusted Python plugins.

Guarantees:
1. Default-Off Live Trading Master Lock (`ALGODESK_ENABLE_LIVE_TRADING=false`):
   Prevents accidental real-money order submission until explicitly enabled.
2. Auth-By-Default & Deny-By-Default `/api/*` Protection:
   - `ALGODESK_REQUIRE_AUTH` defaults to `"true"` unless explicitly disabled.
   - Every `/api/*` endpoint (GET, POST, PUT, PATCH, DELETE) requires a valid
     Bearer token EXCEPT the explicit allow-list:
       - `GET /api/health`
       - `GET /api/security/status`
       - `POST /api/security/login`
       - `POST /api/security/claim`
   - First-run passcode claim requires a one-time bootstrap code persisted at
     `data/.bootstrap_code` (`0o600`, logged once on server boot) whenever auth
     is enforced or a bootstrap file exists.
   - Sliding-window rate limiting locks out repeated failed login/claim attempts
     (HTTP 429 via `RateLimitError`).
3. Admin-Only Trusted Python Plugin Policy + Static AST Guardrail:
   - Uploaded Python screens & strategies execute inside the operator's local
     AlgoDesk server process and are strictly **Admin-Only Trusted Code**.
   - `validate_python_plugin_ast` acts as a defense-in-depth guardrail blocking
     dangerous imports, dynamic attribute reflection (`getattr`/`setattr`/dunders),
     and pickle deserialization (`allow_pickle`, `read_pickle`, `to_pickle`).
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from config import IST_TZ, config, is_live_trading_enabled as _is_live_trading_enabled

logger = logging.getLogger(__name__)


class RateLimitError(PermissionError):
    """Raised when login or bootstrap claim attempts exceed the rate limit."""

    def __init__(self, message: str, retry_after_sec: int = 60) -> None:
        super().__init__(message)
        self.retry_after_sec = max(1, int(retry_after_sec))


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
    "posix",
    "nt",
    "io",
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
    "getattr",
    "delattr",
    "setattr",
    "memoryview",
}

# Dangerous method calls on imported data libraries or objects
_BANNED_METHOD_NAMES: Set[str] = {
    "system",
    "popen",
    "spawn",
    "remove",
    "unlink",
    "rmdir",
    "read_pickle",
    "to_pickle",
    "load_pickle",
    "fromfile",
    "tofile",
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
    "__class__",
    "__dict__",
    "__getattribute__",
    "__setattr__",
    "__delattr__",
}

_BANNED_KEYWORD_ARGS: Set[str] = {
    "allow_pickle",
}

PLUGIN_TRUST_NOTICE = (
    "Admin-Only Trusted Code: Python plugins execute in-process inside the local AlgoDesk server. "
    "Static AST checks are defense-in-depth only; upload only code you wrote or trust."
)


def validate_python_plugin_ast(code: str, filename: str = "plugin.py") -> List[str]:
    """
    Validate Python plugin source using static AST inspection (defense-in-depth).
    Returns a list of violation strings (empty list means passed static checks).
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
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _BANNED_METHOD_NAMES:
                violations.append(
                    f"Line {node.lineno}: call to '.{node.func.attr}()' is blocked by plugin safety policy."
                )
            for kw in node.keywords:
                if kw.arg in _BANNED_KEYWORD_ARGS:
                    violations.append(
                        f"Line {node.lineno}: keyword argument '{kw.arg}' is blocked by plugin safety policy."
                    )
        elif isinstance(node, ast.Attribute):
            if node.attr in _BANNED_DUNDER_ATTRS:
                violations.append(
                    f"Line {node.lineno}: attribute access '{node.attr}' is blocked by plugin safety policy."
                )

    return violations


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


# Explicit allow-list of unauthenticated /api/* routes (deny-by-default for everything else)
_PUBLIC_API_ALLOWLIST: Set[Tuple[str, str]] = {
    ("GET", "/api/health"),
    ("GET", "/api/security/status"),
    ("POST", "/api/security/login"),
    ("POST", "/api/security/claim"),
}


class SecurityManager:
    """Manages Bearer token auth, one-time bootstrap code, login throttling, and deny-by-default routing."""

    def __init__(
        self,
        auth_file: Optional[Path] = None,
        bootstrap_file: Optional[Path] = None,
        session_ttl_hours: int = 12,
        max_failed_attempts: int = 5,
        failure_window_sec: float = 60.0,
        lockout_sec: float = 60.0,
    ) -> None:
        self._explicit_auth_file = auth_file
        self._explicit_bootstrap_file = bootstrap_file
        self._session_ttl = timedelta(hours=max(1, int(session_ttl_hours)))
        self._max_failed_attempts = max(1, int(max_failed_attempts))
        self._failure_window_sec = max(5.0, float(failure_window_sec))
        self._lockout_sec = max(5.0, float(lockout_sec))
        self._lock = threading.RLock()
        # session_token -> expires_at_ist
        self._sessions: Dict[str, datetime] = {}
        # client_key -> timestamps of recent failed attempts (monotonic)
        self._failed_attempts: Dict[str, Deque[float]] = {}
        # client_key -> lockout_until (monotonic)
        self._lockouts: Dict[str, float] = {}
        self._bootstrap_logged: bool = False

    @property
    def auth_file(self) -> Path:
        return self._explicit_auth_file or (config.DATA_DIR / ".admin_auth.json")

    @property
    def bootstrap_file(self) -> Path:
        if self._explicit_bootstrap_file is not None:
            return self._explicit_bootstrap_file
        return self.auth_file.parent / ".bootstrap_code"

    def _env_token(self) -> str:
        config.reload_if_changed()
        return os.getenv("ALGODESK_API_TOKEN", "").strip()

    def _require_auth_flag(self) -> bool:
        """Defaults to True (secure by default) unless ALGODESK_REQUIRE_AUTH is explicitly false."""
        config.reload_if_changed()
        raw = os.getenv("ALGODESK_REQUIRE_AUTH")
        if raw is None or not raw.strip():
            return True
        return raw.strip().lower() not in {"0", "false", "no", "off"}

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

    def _read_bootstrap_code(self) -> Optional[str]:
        path = self.bootstrap_file
        if not path.exists():
            return None
        try:
            val = path.read_text(encoding="utf-8").strip()
            return val or None
        except OSError:
            return None

    def ensure_bootstrap_code(self) -> Optional[str]:
        """
        Ensure a one-time bootstrap secret exists at `data/.bootstrap_code` (0o600)
        whenever neither `ALGODESK_API_TOKEN` nor `.admin_auth.json` is configured.
        Logs the code once to server stdout/logs so only the local operator can claim admin access.
        """
        with self._lock:
            if self._env_token() or self._read_passcode_record() is not None:
                return None
            existing = self._read_bootstrap_code()
            if existing:
                if not self._bootstrap_logged:
                    self._bootstrap_logged = True
                    logger.warning(
                        "AlgoDesk first-run bootstrap code: %s (loaded from %s; required to claim admin passcode)",
                        existing,
                        self.bootstrap_file,
                    )
                return existing

            code = f"ADK-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"
            path = self.bootstrap_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{code}\n", encoding="utf-8")
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            self._bootstrap_logged = True
            logger.warning(
                "AlgoDesk first-run bootstrap code: %s (saved to %s with 0600 permissions; required to claim admin passcode)",
                code,
                path,
            )
            return code

    def is_auth_enforced(self) -> bool:
        """
        True by default (unless `ALGODESK_REQUIRE_AUTH=false` with no token/passcode configured).
        Always True whenever `ALGODESK_API_TOKEN` is set or an admin passcode has been claimed.
        """
        return bool(self._env_token() or self._read_passcode_record() or self._require_auth_flag())

    def status(self) -> Dict[str, Any]:
        env_tok = bool(self._env_token())
        has_passcode = self._read_passcode_record() is not None
        enforced = self.is_auth_enforced()
        needs_claim = (not env_tok and not has_passcode)
        bootstrap_required = bool(needs_claim and (self.bootstrap_file.exists() or self._require_auth_flag()))
        if bootstrap_required and not self.bootstrap_file.exists():
            self.ensure_bootstrap_code()
        with self._lock:
            now = datetime.now(tz=IST_TZ)
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            active_sessions = len(self._sessions)
        return {
            "ok": True,
            "auth_enforced": enforced,
            "has_env_token": env_tok,
            "has_passcode": has_passcode,
            "needs_claim": needs_claim,
            "bootstrap_required": bootstrap_required,
            "active_sessions": active_sessions,
            "live_trading_enabled": is_live_trading_enabled(),
            "plugin_trust_notice": PLUGIN_TRUST_NOTICE,
        }

    # ── Rate Limiting Helpers ─────────────────────────────────────────────

    def _check_rate_limit_locked(self, client_key: str) -> None:
        now = time.monotonic()
        until = self._lockouts.get(client_key, 0.0)
        if until > now:
            remaining = max(1, int(until - now) + 1)
            raise RateLimitError(
                f"Too many failed authentication attempts. Locked out for {remaining}s.",
                retry_after_sec=remaining,
            )
        if until > 0.0:
            self._lockouts.pop(client_key, None)

    def _record_auth_failure_locked(self, client_key: str) -> None:
        now = time.monotonic()
        q = self._failed_attempts.setdefault(client_key, deque())
        cutoff = now - self._failure_window_sec
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)
        if len(q) >= self._max_failed_attempts:
            self._lockouts[client_key] = now + self._lockout_sec
            q.clear()

    def _clear_auth_failures_locked(self, client_key: str) -> None:
        self._failed_attempts.pop(client_key, None)
        self._lockouts.pop(client_key, None)

    # ── Passcode Claim & Login ────────────────────────────────────────────

    def claim_passcode(
        self,
        passcode: str,
        *,
        bootstrap_code: Optional[str] = None,
        client_key: str = "local",
    ) -> Dict[str, Any]:
        clean = (passcode or "").strip()
        if len(clean) < 6:
            raise ValueError("Passcode must be at least 6 characters long.")
        with self._lock:
            self._check_rate_limit_locked(client_key)
            if self._env_token():
                raise ValueError("ALGODESK_API_TOKEN is already configured in environment. Sign in with that token.")
            if self._read_passcode_record() is not None:
                raise ValueError("Admin passcode is already configured. Sign in instead.")

            require_bootstrap = self.bootstrap_file.exists() or self._require_auth_flag()
            if require_bootstrap:
                expected_code = self.ensure_bootstrap_code()
                supplied_code = (bootstrap_code or "").strip()
                if not expected_code or not supplied_code or not hmac.compare_digest(supplied_code, expected_code):
                    self._record_auth_failure_locked(client_key)
                    raise PermissionError(
                        "Valid server bootstrap code is required to claim admin passcode (see server console or data/.bootstrap_code)."
                    )

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

            # Consume one-time bootstrap code file
            try:
                self.bootstrap_file.unlink(missing_ok=True)
            except OSError:
                pass

            self._clear_auth_failures_locked(client_key)
            token, expires = self._issue_session_locked()

        return {
            "ok": True,
            "token": token,
            "expires_at_ist": expires.isoformat(timespec="seconds"),
            "status": self.status(),
        }

    def login(self, passcode_or_token: str, *, client_key: str = "local") -> Dict[str, Any]:
        candidate = (passcode_or_token or "").strip()
        if not candidate:
            raise ValueError("Passcode or API token is required.")

        with self._lock:
            self._check_rate_limit_locked(client_key)

        env_tok = self._env_token()
        if env_tok and hmac.compare_digest(candidate, env_tok):
            with self._lock:
                self._clear_auth_failures_locked(client_key)
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
                    self._clear_auth_failures_locked(client_key)
                    token, expires = self._issue_session_locked()
                return {
                    "ok": True,
                    "token": token,
                    "expires_at_ist": expires.isoformat(timespec="seconds"),
                    "status": self.status(),
                }

        with self._lock:
            self._record_auth_failure_locked(client_key)
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
        return self.verify_raw_token(supplied)

    def verify_raw_token(self, token: Optional[str]) -> bool:
        """Validate a raw Bearer token string (used by HTTP middleware and WebSocket auth)."""
        if not self.is_auth_enforced():
            return True
        supplied = (token or "").strip()
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
        """
        Deny-by-default route protection for all `/api/*` endpoints.
        Only explicit allow-listed public endpoints and static SPA assets bypass Bearer auth.
        """
        m = (method or "GET").upper()
        p = (path or "").strip()
        if m == "OPTIONS":
            return False
        # Static SPA pages, assets, and broker OAuth browser redirect callbacks are non-/api/
        if not p.startswith("/api/"):
            return False
        if (m, p) in _PUBLIC_API_ALLOWLIST:
            return False
        return True


security_manager = SecurityManager()

__all__ = [
    "PLUGIN_TRUST_NOTICE",
    "RateLimitError",
    "SecurityManager",
    "is_live_trading_enabled",
    "security_manager",
    "validate_python_plugin_ast",
]
