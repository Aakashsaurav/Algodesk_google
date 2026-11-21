"""
web/spa.py
==========
Shared React/Vite Single-Page Application (SPA) resolver for both FastAPI
(`web/app.py`) and the stdlib HTTP server (`web/local_server.py`).

Resolution order (automatic freshness):
1. `<repo_root>/dist` (if built locally via `npm run build`)
2. `<repo_root>/web/static_spa` (pre-built production bundle shipped with repo so
   `python -m web.local_server --host 127.0.0.1 --port 8000` serves the exact
   modern React UI out of the box without requiring Node.js)
   If both exist, whichever `index.html` was modified more recently wins.
3. Legacy `web/templates/index.html` / `web/templates/terminal.html` fallback
   only when no built SPA bundle is present.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
DIST_DIR = REPO_ROOT / "dist"
STATIC_SPA_DIR = WEB_DIR / "static_spa"
LEGACY_INDEX_PATH = WEB_DIR / "templates" / "index.html"
LEGACY_TERMINAL_PATH = WEB_DIR / "templates" / "terminal.html"

_ASSET_REF_RE = re.compile(r"""(?:src|href)\s*=\s*["'](/assets/[^"'?#]+)["']""")

# Ensure consistent MIME types on minimal Linux/macOS Python installs
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/json", ".json")


def _extract_local_asset_paths(html_text: str) -> List[str]:
    return _ASSET_REF_RE.findall(html_text)


def is_complete_spa_bundle(spa_dir: Path) -> bool:
    """Verify index.html exists AND every local /assets/... file it references exists."""
    index_file = spa_dir / "index.html"
    if not index_file.is_file():
        return False
    try:
        html_text = index_file.read_text(encoding="utf-8")
    except OSError:
        return False
    refs = _extract_local_asset_paths(html_text)
    if not refs:
        return False
    try:
        spa_root = spa_dir.resolve()
        for ref in refs:
            target = (spa_root / ref.lstrip("/")).resolve()
            target.relative_to(spa_root)
            if not target.is_file():
                return False
    except (ValueError, OSError):
        return False
    return True


def resolve_spa_dir() -> Optional[Path]:
    """Return the freshest complete built React SPA directory, or None."""
    candidates = [d for d in (DIST_DIR, STATIC_SPA_DIR) if is_complete_spa_bundle(d)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Prefer whichever complete bundle was built more recently
    candidates.sort(key=lambda d: (d / "index.html").stat().st_mtime, reverse=True)
    return candidates[0]


def ensure_spa_bundle(auto_build: bool = True) -> Optional[Path]:
    """
    Ensure a complete React SPA bundle is available on disk.
    If `dist/` and `web/static_spa/` are missing or incomplete (e.g. hashed JS/CSS
    files were deleted or omitted), automatically run `npm run build` when Node/npm
    and `package.json` are present locally.
    """
    existing = resolve_spa_dir()
    if existing is not None:
        return existing

    if not auto_build:
        return None

    pkg_json = REPO_ROOT / "package.json"
    npm_bin = shutil.which("npm")
    if not pkg_json.is_file() or not npm_bin:
        logger.warning(
            "React SPA assets are missing or incomplete and npm is unavailable; "
            "run 'npm install && npm run build' to generate the frontend bundle."
        )
        return None

    try:
        if not (REPO_ROOT / "node_modules").is_dir():
            logger.info("Frontend bundle incomplete and node_modules missing — running 'npm install'...")
            install_res = subprocess.run(
                [npm_bin, "install", "--no-audit", "--no-fund"],
                cwd=str(REPO_ROOT),
                check=False,
                timeout=240,
            )
            if install_res.returncode != 0:
                logger.warning("Standard 'npm install' exited %s — retrying with '--legacy-peer-deps'...", install_res.returncode)
                subprocess.run(
                    [npm_bin, "install", "--legacy-peer-deps", "--no-audit", "--no-fund"],
                    cwd=str(REPO_ROOT),
                    check=True,
                    timeout=240,
                )
        logger.info("Frontend bundle missing or incomplete — rebuilding via 'npm run build'...")
        subprocess.run(
            [npm_bin, "run", "build"],
            cwd=str(REPO_ROOT),
            check=True,
            timeout=120,
        )
    except Exception as exc:
        logger.warning("Automatic frontend build failed: %s", exc)
        return None

    rebuilt = resolve_spa_dir()
    if rebuilt is not None:
        logger.info("Frontend SPA bundle rebuilt successfully at %s", rebuilt)
    return rebuilt


def has_spa_bundle() -> bool:
    return resolve_spa_dir() is not None


def render_spa_html(route_path: str = "/") -> Optional[str]:
    """Return the SPA index.html (or legacy template fallback) for HTML routes."""
    spa_dir = resolve_spa_dir()
    if spa_dir is not None:
        html_text = (spa_dir / "index.html").read_text(encoding="utf-8")
        if route_path.rstrip("/") == "/terminal" and "Live WebSocket Terminal" not in html_text:
            html_text = html_text.replace(
                "</title>",
                " · Live WebSocket Terminal</title>",
                1,
            )
        return html_text

    # Fallback to legacy templates only if no complete SPA bundle exists
    fallback = LEGACY_TERMINAL_PATH if route_path.rstrip("/") == "/terminal" else LEGACY_INDEX_PATH
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8")
    return None


def resolve_spa_asset(url_path: str) -> Optional[Tuple[bytes, str, str]]:
    """
    Safely resolve a static asset from the active SPA bundle.
    Returns `(content_bytes, content_type, cache_control)` or None if not found.
    """
    spa_dir = resolve_spa_dir()
    if spa_dir is None:
        return None

    rel = url_path.lstrip("/")
    if not rel or rel.startswith(("api/", "ws/", "auth/")):
        return None

    try:
        spa_root = spa_dir.resolve()
        candidate = (spa_root / rel).resolve()
        candidate.relative_to(spa_root)
    except (ValueError, OSError):
        return None

    if not candidate.is_file():
        return None

    ctype, _ = mimetypes.guess_type(str(candidate))
    content_type = ctype or "application/octet-stream"
    if content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "image/svg+xml"}:
        content_type = f"{content_type}; charset=utf-8"

    # Hashed Vite assets under /assets/ can be cached aggressively; others no-cache
    cache_control = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
    return candidate.read_bytes(), content_type, cache_control


__all__ = [
    "DIST_DIR",
    "LEGACY_INDEX_PATH",
    "LEGACY_TERMINAL_PATH",
    "STATIC_SPA_DIR",
    "ensure_spa_bundle",
    "has_spa_bundle",
    "is_complete_spa_bundle",
    "render_spa_html",
    "resolve_spa_asset",
    "resolve_spa_dir",
]
