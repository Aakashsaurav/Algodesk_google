"""
config.py
---------
Central, dynamically reloadable configuration for AlgoDesk.

Refactored from Algodesk_R4:
- Avoids freezing environment variables at class-definition time so credential
  or broker-selection updates made via the local web UI apply without restart.
- Avoids heavy directory creation side effects on module import (directories
  are created lazily only when persisting state).
- Resolves secrets cleanly from environment variables, .env, or macOS Keychain.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback parser provided below
    load_dotenv = None  # type: ignore[assignment]

IST: timezone = timezone(timedelta(hours=5, minutes=30))
IST_TZ: ZoneInfo = ZoneInfo("Asia/Kolkata")


def _parse_env_file(env_path: Path) -> Dict[str, str]:
    """Minimal .env parser fallback when python-dotenv is absent."""
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                values[key] = val
    except OSError:
        pass
    return values


def _load_env(base_dir: Path, override: bool = False) -> None:
    env_path = base_dir / ".env"
    if load_dotenv is not None:
        if env_path.exists():
            load_dotenv(env_path, override=override)
        return
    for key, val in _parse_env_file(env_path).items():
        if override or key not in os.environ:
            os.environ[key] = val


def keychain_get(service: str, account: str) -> Optional[str]:
    """Read a secret from macOS Keychain or optional keyring backend."""
    try:
        import keyring  # type: ignore

        secret = keyring.get_password(service, account)
        if secret:
            return secret
    except Exception:
        pass

    if platform.system() != "Darwin":
        return None
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def keychain_set(service: str, account: str, secret: str) -> bool:
    """Persist a secret into keyring or macOS Keychain."""
    try:
        import keyring  # type: ignore

        keyring.set_password(service, account, secret)
        return True
    except Exception:
        pass

    if platform.system() != "Darwin":
        return False
    try:
        res = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def keychain_delete(service: str, account: str) -> bool:
    """Remove a secret from keyring or macOS Keychain."""
    deleted = False
    try:
        import keyring  # type: ignore

        keyring.delete_password(service, account)
        deleted = True
    except Exception:
        pass

    if platform.system() == "Darwin":
        try:
            res = subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                deleted = True
        except Exception:
            pass
    return deleted


class AppConfig:
    """
    Reloadable central configuration singleton.
    Uses dynamic properties for environment-backed settings so live UI updates
    take effect immediately across all modules.
    """

    BASE_DIR: Path = Path(__file__).resolve().parent

    UPSTOX_BASE_URL: str = "https://api.upstox.com"
    UPSTOX_AUTH_URL: str = "https://api.upstox.com/v2/login/authorization/token"
    UPSTOX_AUTH_DIALOG_URL: str = "https://api.upstox.com/v2/login/authorization/dialog"
    UPSTOX_PROFILE_URL: str = "https://api.upstox.com/v2/user/profile"

    # Legacy alias kept only so older callers referencing BASE_URL never crash
    BASE_URL: str = UPSTOX_BASE_URL

    KEYRING_SERVICE: str = "algodesk"

    def __init__(self) -> None:
        self._env_mtime: Optional[float] = None
        self.reload_if_changed(force=True)

    def _current_env_mtime(self) -> Optional[float]:
        env_path = self.BASE_DIR / ".env"
        try:
            return env_path.stat().st_mtime if env_path.exists() else None
        except OSError:
            return None

    def reload_if_changed(self, force: bool = False) -> bool:
        """Auto-detect .env creation or edits without restarting the Python server."""
        mtime = self._current_env_mtime()
        if force or (mtime is not None and mtime != self._env_mtime):
            _load_env(self.BASE_DIR, override=True)
            self._env_mtime = mtime
            return True
        return False

    def reload(self) -> None:
        """Re-read .env variables cleanly without restarting the process."""
        self.reload_if_changed(force=True)

    def credential_source(self, env_key: str, keychain_account: str) -> str:
        """Return where a setting was detected ('env', 'keychain', or 'none') without exposing it."""
        self.reload_if_changed()
        if os.getenv(env_key, "").strip():
            return "env"
        if keychain_get(self.KEYRING_SERVICE, keychain_account):
            return "keychain"
        return "none"

    # ── Paths (lazy, no import-time mkdir storm) ──────────────────────────
    @property
    def DATA_DIR(self) -> Path:
        return self.BASE_DIR / "data"

    @property
    def SQLITE_DIR(self) -> Path:
        return self.DATA_DIR / "sqlite"

    @property
    def LOG_FILE(self) -> Path:
        return self.BASE_DIR / "logs" / "app.log"

    @property
    def SELECTION_FILE(self) -> Path:
        return self.DATA_DIR / "broker_selection.json"

    @property
    def UPSTOX_TOKEN_FILE_PATH(self) -> Path:
        rel = os.getenv("TOKEN_FILE_PATH", "broker/upstox/token.json")
        p = Path(rel)
        return p if p.is_absolute() else (self.BASE_DIR / p)

    @property
    def INSTRUMENT_KEY_URL(self) -> str:
        return "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

    @property
    def INSTRUMENT_KEY_PATH(self) -> Path:
        return self.BASE_DIR / "broker" / "upstox" / "complete_instru_list.json"

    # ── Broker Credentials (.env / env > macOS Keychain) ──────────────────
    @property
    def UPSTOX_API_KEY(self) -> str:
        self.reload_if_changed()
        val = os.getenv("UPSTOX_API_KEY", "").strip()
        if val:
            return val
        return keychain_get(self.KEYRING_SERVICE, "upstox_api_key") or ""

    @property
    def UPSTOX_API_SECRET(self) -> str:
        self.reload_if_changed()
        val = os.getenv("UPSTOX_API_SECRET", "").strip()
        if val:
            return val
        return keychain_get(self.KEYRING_SERVICE, "upstox_api_secret") or ""

    @property
    def UPSTOX_REDIRECT_URI(self) -> str:
        self.reload_if_changed()
        return (
            os.getenv("UPSTOX_REDIRECT_URI", "").strip()
            or keychain_get(self.KEYRING_SERVICE, "upstox_redirect_uri")
            or "http://127.0.0.1:8000/auth/upstox/callback"
        ).strip()

    @property
    def UPSTOX_REDIRECT_SSL_CERT_FILE(self) -> str:
        return os.getenv(
            "UPSTOX_REDIRECT_SSL_CERT_FILE",
            str(self.BASE_DIR / "broker" / "upstox" / "cert.pem"),
        )

    @property
    def UPSTOX_REDIRECT_SSL_KEY_FILE(self) -> str:
        return os.getenv(
            "UPSTOX_REDIRECT_SSL_KEY_FILE",
            str(self.BASE_DIR / "broker" / "upstox" / "key.pem"),
        )

    # ── Broker & Data Routing Defaults ────────────────────────────────────
    @property
    def ACTIVE_BROKER(self) -> str:
        return os.getenv("ACTIVE_BROKER", "upstox").strip().lower()

    @property
    def ACTIVE_DATA_CONNECTION(self) -> str:
        return os.getenv("ACTIVE_DATA_CONNECTION", "upstox").strip().lower()

    # ── Capital & Risk Defaults ───────────────────────────────────────────
    @property
    def TOTAL_CAPITAL(self) -> float:
        return float(os.getenv("TOTAL_CAPITAL", "500000"))

    @property
    def MAX_PORTFOLIO_DRAWDOWN(self) -> float:
        return float(os.getenv("MAX_PORTFOLIO_DRAWDOWN", "20.0"))

    @property
    def PER_TRADE_RISK_PERCENT(self) -> float:
        return float(os.getenv("PER_TRADE_RISK_PERCENT", "1.5"))

    @property
    def PAPER_TRADE(self) -> bool:
        return os.getenv("PAPER_TRADE", "True").lower() == "true"

    @property
    def LOG_LEVEL(self) -> str:
        return os.getenv("LOG_LEVEL", "INFO").upper()

    def ensure_runtime_dirs(self) -> None:
        """Create only essential local runtime directories on demand."""
        for directory in (self.DATA_DIR, self.BASE_DIR / "logs", self.UPSTOX_TOKEN_FILE_PATH.parent):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


def setup_logging(level_override: Optional[str] = None) -> logging.Logger:
    """Configure application-wide console logging cleanly without duplicate handlers."""
    level_name = (level_override or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
    return root


def current_ist() -> datetime:
    """Return current timezone-aware datetime in Asia/Kolkata."""
    return datetime.now(tz=IST_TZ)


config = AppConfig()

__all__: List[str] = [
    "AppConfig",
    "IST",
    "IST_TZ",
    "config",
    "current_ist",
    "keychain_delete",
    "keychain_get",
    "keychain_set",
    "setup_logging",
]
