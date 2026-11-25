# AlgoDesk Architecture

AlgoDesk is a single-operator local trading workstation split into a Python trading/analytics backend and a React SPA frontend.

## Module Map

- **`broker/`**: Broker-agnostic contract (`BrokerBase`, `OrderRequest`, `BrokerOrder`, `BrokerPosition`, `AmbiguousSubmissionError`, `TokenBucketRateLimiter`) with concrete adapters for **Upstox** (`broker/upstox/`), **Zerodha Kite** (`broker/zerodha/`), and **YFinance** (`broker/yfinance_adapter.py`) registered via `BrokerRegistry`.
- **`execution/oms.py`**: SQLite-persisted Order Management System (`oms.db`, WAL + thread-local connections). Enforces `client_order_id` idempotency, strict state transitions (terminal states cannot reopen), per-fill trade-book pricing (`get_trades()`), tag-based recovery after ambiguous network failures, and `PaperExecutionAdapter` sharing the live OMS path.
- **`deploy/manager.py`**: Strategy deployment engine (`paper`, `live_alert`, `live_auto`). Performs one-time historical warmup, evaluates completed-bar signals with stale-quote gating, guards against duplicate exits via deterministic intent keys + active-order checks, reconciles fills and broker positions, and persists state in `data/deployments.db`.
- **`risk/limits.py` & `core/preflight.py`**:
  - `RiskLimitManager`: splits pre-trade validation into `check_entry()` (Kill Switch, Watchdog health, daily loss, per-trade notional, max positions, account-wide + pending exposure) and `check_exit()` (always allows risk-reducing exits; prevents accidental position flips).
  - `KillSwitchController`: atomic state persistence failing closed on corruption, cancels working broker orders on engage, and records SHA-256 hash-chained audit logs (`core/audit.py`).
  - `RiskWatchdog`: background risk & reconciliation loop entering a `degraded` state after repeated failures (blocking new entries and alerting Telegram).
- **`backtest/`, `screener/`, `rules/`, `indicators/`, `charges/`**: Vectorized/bar-by-bar backtesting, portfolio simulation, visual rule builder, multi-backend indicators (pure Python / TA-Lib / pandas-ta), and 2026 Indian brokerage/statutory charge calculation.
- **`web/` & `src/`**: FastAPI (`web/app.py`) with stdlib HTTP fallback (`web/local_server.py`), unified business service (`web/service.py`), deny-by-default Bearer auth & login throttling (`web/security.py`), and React/Vite SPA (`src/`). `server.ts` serves the SPA and proxies `/api/*` & `/ws/*` to the local Python hub.

## Order Execution & Reconciliation Flow

1. Strategy generates signal on completed bar (or SL/TP/TSL on live tick).
2. `DeploymentManager` checks quote freshness (entries), pending OMS orders for `(deployment, symbol)`, and deterministic `client_order_id`.
3. `RiskLimitManager` runs `check_entry` (entries) or `check_exit` (exits).
4. `OrderManagementSystem.submit_order()` persists intent before calling `broker.place_order()`.
   - Explicit broker/4xx rejection -> `REJECTED`.
   - Network timeout / 5xx -> `AmbiguousSubmissionError` -> stored as `SUBMITTED` (uncertain) and recovered via `broker.find_order_by_tag()`.
   - Positions and cash update **only** on verified `filled_quantity` deltas.
5. `RiskWatchdog` periodically runs `reconcile_orders()` and `reconcile_positions()`; broker-vs-local position mismatches pause live deployments and alert Telegram.
