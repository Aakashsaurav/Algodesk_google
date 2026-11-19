"""
config.py
---------
Central, dynamically reloadable configuration for AlgoDesk.

Refactored from Algodesk_R4:
- Avoids freezing environment variables at class-definition time so credential
  or broker-selection updates made via the local web UI apply without restart.
- Avoids heavy directory creation side effects on module import (directories
  are created lazily only when persisting state).
- Resolves secrets cleanly from environment variables / .env, local 0o600 vault,
  or opt-in macOS Keychain (disabled by default to prevent macOS Keychain
  password/ACL popups on unsigned local Python processes).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import stat
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

# If the user denies/cancels a macOS Keychain prompt once (when opt-in is enabled),
# stop nagging for the rest of the process lifetime.
_KEYCHAIN_PROMPT_DENIED = False


def is_keychain_enabled() -> bool:
    """
    OS Keychain integration is opt-in (default False) because unsigned local
    Python / venv binaries on macOS trigger repeated SecurityAgent login/ACL
    popups on read/write. Local 0o600 vault + .env storage requires zero popups.
    """
    return os.getenv("ALGODESK_USE_KEYCHAIN", "false").strip().lower() in {"1", "true", "yes"}


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
    """Read a secret from OS Keychain ONLY when opt-in ALGODESK_USE_KEYCHAIN=true."""
    global _KEYCHAIN_PROMPT_DENIED
    if not is_keychain_enabled() or _KEYCHAIN_PROMPT_DENIED:
        return None

    try:
        import keyring  # type: ignore

        secret = keyring.get_password(service, account)
        return secret or None
    except ImportError:
        pass
    except Exception:
        _KEYCHAIN_PROMPT_DENIED = True
        return None

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
        # Exit code 128 = user canceled Keychain prompt; 51/36 = ACL/lock denied
        if res.returncode in {36, 51, 128}:
            _KEYCHAIN_PROMPT_DENIED = True
    except Exception:
        pass
    return None


def keychain_set(service: str, account: str, secret: str) -> bool:
    """Persist a secret into OS Keychain ONLY when opt-in ALGODESK_USE_KEYCHAIN=true."""
    global _KEYCHAIN_PROMPT_DENIED
    if not is_keychain_enabled() or _KEYCHAIN_PROMPT_DENIED:
        return False

    try:
        import keyring  # type: ignore

        keyring.set_password(service, account, secret)
        return True
    except ImportError:
        pass
    except Exception:
        _KEYCHAIN_PROMPT_DENIED = True
        return False

    if platform.system() != "Darwin":
        return False
    try:
        res = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode in {36, 51, 128}:
            _KEYCHAIN_PROMPT_DENIED = True
        return res.returncode == 0
    except Exception:
        return False


def keychain_delete(service: str, account: str) -> bool:
    """Remove a secret from OS Keychain ONLY when opt-in ALGODESK_USE_KEYCHAIN=true."""
    if not is_keychain_enabled():
        return False

    try:
        import keyring  # type: ignore

        keyring.delete_password(service, account)
        return True
    except ImportError:
        pass
    except Exception:
        return False

    if platform.system() == "Darwin":
        try:
            res = subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return res.returncode == 0
        except Exception:
            pass
    return False


class AppConfig:
    """
    Reloadable central configuration singleton.
    Uses dynamic properties for environment-backed settings so live UI updates
    take effect immediately across all modules.
    """

    BASE_DIR: Path = Path(__file__).resolve().parent

    UPSTOX_BASE_URL: str = "https://api.upstox.com"
    UPSTOX_HFT_URL: str = "https://api-hft.upstox.com"
    # Note: OAuth2 dialog/token/logout and user profile/funds endpoints remain /v2 per Upstox docs;
    # Trading, Market Quote (OHLC/LTP/Option Greek), Historical/Intraday Candles & WS Feed use /v3.
    UPSTOX_AUTH_URL: str = "https://api.upstox.com/v2/login/authorization/token"
    UPSTOX_AUTH_DIALOG_URL: str = "https://api.upstox.com/v2/login/authorization/dialog"
    UPSTOX_PROFILE_URL: str = "https://api.upstox.com/v2/user/profile"
    UPSTOX_FEED_AUTH_URL: str = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

    ZERODHA_BASE_URL: str = "https://api.kite.trade"
    ZERODHA_LOGIN_URL: str = "https://kite.zerodha.com/connect/login"
    ZERODHA_SESSION_URL: str = "https://api.kite.trade/session/token"
    ZERODHA_PROFILE_URL: str = "https://api.kite.trade/user/profile"

    # Legacy alias kept only so older callers referencing BASE_URL never crash
    BASE_URL: str = UPSTOX_BASE_URL

    KEYRING_SERVICE: str = "algodesk"

    def __init__(self) -> None:
        self._env_mtime: Optional[float] = None
        self.reload_if_changed(force=True)

    @property
    def USE_KEYCHAIN(self) -> bool:
        return is_keychain_enabled()

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

    # ── Paths (lazy, no import-time mkdir storm) ──────────────────────────
    @property
    def DATA_DIR(self) -> Path:
        return self.BASE_DIR / "data"

    @property
    def SQLITE_DIR(self) -> Path:
        return self.DATA_DIR / "sqlite"

    @property
    def INSTRUMENTS_DB_PATH(self) -> Path:
        rel = os.getenv("ALGODESK_INSTRUMENTS_DB", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.SQLITE_DIR / "instruments.db"

    @property
    def OHLCV_DB_PATH(self) -> Path:
        rel = os.getenv("ALGODESK_OHLCV_DB", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.SQLITE_DIR / "ohlcv.db"

    @property
    def PARQUET_DIR(self) -> Path:
        rel = os.getenv("ALGODESK_PARQUET_DIR", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.DATA_DIR / "parquet"

    @property
    def OHLCV_STORAGE_MODE(self) -> str:
        # 'hybrid' (SQLite coverage ledger + Parquet candles when pyarrow/pandas installed, else SQLite)
        # or 'sqlite' (SQLite only)
        return os.getenv("ALGODESK_OHLCV_STORAGE", "hybrid").strip().lower() or "hybrid"

    @property
    def AUDIT_DB_PATH(self) -> Path:
        rel = os.getenv("ALGODESK_AUDIT_DB", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.SQLITE_DIR / "audit_chain.db"

    @property
    def ZERODHA_INSTRUMENT_URL(self) -> str:
        return "https://api.kite.trade/instruments"

    @property
    def LOG_FILE(self) -> Path:
        return self.BASE_DIR / "logs" / "app.log"

    @property
    def SELECTION_FILE(self) -> Path:
        return self.DATA_DIR / "broker_selection.json"

    @property
    def RISK_LIMITS_FILE(self) -> Path:
        rel = os.getenv("ALGODESK_RISK_LIMITS_FILE", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.DATA_DIR / "risk_limits.json"

    @property
    def KILL_SWITCH_FILE(self) -> Path:
        rel = os.getenv("ALGODESK_KILL_SWITCH_FILE", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.DATA_DIR / "kill_switch.json"

    @property
    def CREDENTIALS_VAULT_FILE(self) -> Path:
        rel = os.getenv("ALGODESK_VAULT_FILE", "").strip()
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else (self.BASE_DIR / p)
        return self.DATA_DIR / ".credentials.json"

    # ── Local 0o600 Credentials Vault (popup-free persistence) ────────────
    def _read_local_vault(self) -> Dict[str, str]:
        path = self.CREDENTIALS_VAULT_FILE
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): str(v) for k, v in raw.items() if v is not None}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_local_credential(
        self,
        env_key: str,
        vault_account: str,
        value: str,
    ) -> None:
        """
        Persist credential into process env and local 0o600 vault file without
        triggering macOS Keychain UI prompts. Optionally mirrors to Keychain only
        when ALGODESK_USE_KEYCHAIN=true.
        """
        clean = value.strip()
        if not clean:
            return
        os.environ[env_key] = clean

        try:
            self.ensure_runtime_dirs()
            vault_path = self.CREDENTIALS_VAULT_FILE
            vault = self._read_local_vault()
            vault[vault_account] = clean
            vault_path.write_text(json.dumps(vault, indent=2), encoding="utf-8")
            os.chmod(vault_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            logging.getLogger(__name__).warning("Could not write local credentials vault: %s", exc)

        if self.USE_KEYCHAIN:
            keychain_set(self.KEYRING_SERVICE, vault_account, clean)

    def _resolve_credential(self, env_key: str, account_key: str, default: str = "") -> str:
        self.reload_if_changed()
        env_val = os.getenv(env_key, "").strip()
        if env_val:
            return env_val

        vault_val = self._read_local_vault().get(account_key, "").strip()
        if vault_val:
            return vault_val

        if self.USE_KEYCHAIN:
            kc_val = keychain_get(self.KEYRING_SERVICE, account_key)
            if kc_val:
                return kc_val.strip()

        return default

    def credential_source(self, env_key: str, keychain_account: str) -> str:
        """Return where a setting was detected ('env', 'local_vault', 'keychain', or 'none')."""
        self.reload_if_changed()
        if os.getenv(env_key, "").strip():
            return "env"
        if self._read_local_vault().get(keychain_account, "").strip():
            return "local_vault"
        if self.USE_KEYCHAIN and keychain_get(self.KEYRING_SERVICE, keychain_account):
            return "keychain"
        return "none"

    @property
    def UPSTOX_TOKEN_FILE_PATH(self) -> Path:
        rel = os.getenv("TOKEN_FILE_PATH", "broker/upstox/token.json")
        p = Path(rel)
        return p if p.is_absolute() else (self.BASE_DIR / p)

    @property
    def ZERODHA_TOKEN_FILE_PATH(self) -> Path:
        rel = os.getenv("ZERODHA_TOKEN_FILE_PATH", "broker/zerodha/token.json")
        p = Path(rel)
        return p if p.is_absolute() else (self.BASE_DIR / p)

    @property
    def INSTRUMENT_KEY_URL(self) -> str:
        return "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

    @property
    def INSTRUMENT_KEY_PATH(self) -> Path:
        return self.BASE_DIR / "broker" / "upstox" / "complete_instru_list.json"

    # ── Broker Credentials (.env / env > 0o600 local vault > opt-in Keychain)
    @property
    def UPSTOX_API_KEY(self) -> str:
        return self._resolve_credential("UPSTOX_API_KEY", "upstox_api_key")

    @property
    def UPSTOX_API_SECRET(self) -> str:
        return self._resolve_credential("UPSTOX_API_SECRET", "upstox_api_secret")

    @property
    def UPSTOX_REDIRECT_URI(self) -> str:
        return self._resolve_credential(
            "UPSTOX_REDIRECT_URI",
            "upstox_redirect_uri",
            default="http://127.0.0.1:8000/auth/upstox/callback",
        )

    @property
    def ZERODHA_API_KEY(self) -> str:
        return self._resolve_credential("ZERODHA_API_KEY", "zerodha_api_key")

    @property
    def ZERODHA_API_SECRET(self) -> str:
        return self._resolve_credential("ZERODHA_API_SECRET", "zerodha_api_secret")

    @property
    def ZERODHA_REDIRECT_URI(self) -> str:
        return self._resolve_credential(
            "ZERODHA_REDIRECT_URI",
            "zerodha_redirect_uri",
            default="http://127.0.0.1:8000/auth/zerodha/callback",
        )

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
        for directory in (
            self.DATA_DIR,
            self.SQLITE_DIR,
            self.BASE_DIR / "logs",
            self.UPSTOX_TOKEN_FILE_PATH.parent,
            self.ZERODHA_TOKEN_FILE_PATH.parent,
        ):
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
    "is_keychain_enabled",
    "keychain_delete",
    "keychain_get",
    "keychain_set",
    "setup_logging",
]
