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

import mimetypes
from pathlib import Path
from typing import Optional, Tuple

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
DIST_DIR = REPO_ROOT / "dist"
STATIC_SPA_DIR = WEB_DIR / "static_spa"
LEGACY_INDEX_PATH = WEB_DIR / "templates" / "index.html"
LEGACY_TERMINAL_PATH = WEB_DIR / "templates" / "terminal.html"

# Ensure consistent MIME types on minimal Linux/macOS Python installs
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/json", ".json")


def resolve_spa_dir() -> Optional[Path]:
    """Return the freshest available built React SPA directory, or None."""
    candidates = [d for d in (DIST_DIR, STATIC_SPA_DIR) if (d / "index.html").is_file()]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Prefer whichever bundle was built more recently
    candidates.sort(key=lambda d: (d / "index.html").stat().st_mtime, reverse=True)
    return candidates[0]


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

    # Fallback to legacy templates only if no SPA bundle exists
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
    "has_spa_bundle",
    "render_spa_html",
    "resolve_spa_asset",
    "resolve_spa_dir",
]
