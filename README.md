# AlgoDesk

Local-first, broker-agnostic algorithmic trading, backtesting, and market screening workstation for Indian markets (NSE, BSE, NFO, MCX).

## Quickstart (macOS / Linux)

```bash
# 1. Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-web.txt
# Optional analytics & Parquet storage:
# pip install -r requirements-analytics.txt

# 2. Configure environment
cp .env.example .env

# 3. Frontend dependencies & production SPA bundle
npm install
npm run build

# 4. Launch local server (binds to 127.0.0.1 by default)
npm run dev
# Or run Python hub directly:
# python3 -m web.local_server --host 127.0.0.1 --port 8000
```

## Security & Safety Defaults

- **Live Trading Locked by Default**: `ALGODESK_ENABLE_LIVE_TRADING="false"` blocks real broker orders until explicitly enabled in `.env`. Paper trading and 1-click alert previews work safely out of the box.
- **Auth Enforced by Default**: All `/api/*` endpoints require a Bearer token (deny-by-default; only `/api/health`, `/api/security/status`, `/api/security/login`, and `/api/security/claim` are public).
- **First-Run Claim**: If `ALGODESK_API_TOKEN` is unset, AlgoDesk generates a one-time bootstrap code at `data/.bootstrap_code` (`0o600`) and prints it once on startup so only the local operator can claim an admin passcode.
- **Fail-Closed Kill Switch & Exit Priority**: Risk-reducing exits (`SELL` / `COVER`) are never blocked by the Kill Switch or per-trade notional caps. Corrupted kill-switch state fails closed (`engaged=True`) for new entries.
- **Admin-Only Python Plugins**: Custom `.py` screens and strategies execute in-process; upload only trusted operator code.
- **macOS Keychain**: `ALGODESK_USE_KEYCHAIN="false"` by default to store broker tokens in local `0o600` files without repeated macOS Keychain prompts.

## Running Tests & Verification

```bash
# Python unit & hardening suite
python3 -m unittest discover -s tests -v

# Frontend TypeScript check & production build
npm run lint
npm run build
```

See `ARCHITECTURE.md` for system modules, execution lifecycle, and OMS/risk design.
