export interface ChargeBreakdown {
  turnover: number;
  brokerage: number;
  stt: number;
  exchange_txn: number;
  sebi_fee: number;
  gst: number;
  stamp_duty: number;
  dp_charges: number;
  slippage_cost: number;
  statutory_and_brokerage: number;
  total_charges: number;
}

export interface ChargesConfigPreset {
  profile: string;
  segment: string;
  exchange: string;
  brokerage_flat_per_order: number;
  brokerage_pct: number;
  brokerage_is_flat_only: boolean;
  stt_buy_pct: number;
  stt_sell_pct: number;
  exchange_txn_pct: number;
  sebi_pct: number;
  gst_pct: number;
  stamp_duty_buy_pct: number;
  dp_charge_per_sell_scrip: number;
  slippage_bps: number;
}

export interface VisualOperandSpec {
  type: string;
  period?: number;
  multiplier?: number;
  param2?: number;
  offset?: number;
  value?: number;
  expr?: string;
}

export interface VisualCondition {
  kind?: 'formula' | 'structured';
  expression?: string;
  left: VisualOperandSpec;
  operator: string;
  right: VisualOperandSpec;
  pct?: number;
  enabled?: boolean;
  timeframe?: string;
}

export interface VisualConditionGroup {
  logic: 'AND' | 'OR';
  conditions: Array<VisualCondition | VisualConditionGroup>;
  enabled?: boolean;
}

export interface VisualRuleDef {
  id?: string;
  kind: 'strategy' | 'screen';
  mode?: 'visual';
  builder_mode?: 'basic' | 'advance';
  category?: string;
  name: string;
  description?: string;
  universe?: string;
  basket_id?: string;
  default_symbols?: string[];
  timeframe?: string;
  chart_type?: 'candlestick' | 'heikin_ashi';
  side?: 'BUY' | 'SELL';
  segment?: string;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  trailing_sl_pct?: number;
  exit_unit?: 'pct' | 'points' | 'pnl';
  entry: VisualConditionGroup;
  exit?: VisualConditionGroup;
  seeded?: boolean;
  updated_at_ist?: string;
}

export interface BasketItem {
  id: string;
  name: string;
  description: string;
  exchange: string;
  symbols: string[];
  updated_at_ist?: string;
}

export interface DiscoverItem {
  id: string;
  category: 'strategy' | 'screen' | 'options';
  mode: 'visual' | 'python' | 'options';
  name: string;
  tagline: string;
  timeframe: string;
  segment: string;
  tags: string[];
  target_id: string;
  default_symbols?: string[];
  default_params?: Record<string, number | string | boolean>;
  default_spot?: number;
  default_step?: number;
  default_lot_size?: number;
}

export interface ParamSpecMeta {
  name: string;
  label: string;
  kind: string;
  default: number | string | boolean;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  help?: string;
}

export interface ScreenSpecMeta {
  id: string;
  name: string;
  description: string;
  source_file?: string;
  min_bars?: number;
  supports_multi_timeframe?: boolean;
  default_timeframes?: string[];
  params?: ParamSpecMeta[];
}

export interface StrategySpecMeta {
  id: string;
  name: string;
  description: string;
  source_file?: string;
  params: ParamSpecMeta[];
}

export interface UniverseMeta {
  id: string;
  label: string;
  count: number;
  source?: string;
}

export interface StreakMetaPayload {
  ok: boolean;
  selections: { trading_broker: string; data_connection: string };
  screens: ScreenSpecMeta[];
  strategy_specs: StrategySpecMeta[];
  universes: UniverseMeta[];
  timeframes: Array<{ id: string; label: string; base: string; resampled: boolean }>;
  indicator_backends: Record<string, { available?: boolean; version?: string; label?: string }> | string[];
  charge_presets: {
    segments: Array<{ id: string; label: string }>;
    profiles: Record<string, Record<string, ChargesConfigPreset>>;
  };
  allocation_modes: Array<{ id: string; label: string }>;
  signal_rankings: Array<{ id: string; label: string }>;
  visual_operands: Array<{ id: string; label: string; kind: string; default_period: number | null }>;
  visual_operators: Array<{ id: string; label: string }>;
  visual_rules: VisualRuleDef[];
  baskets: BasketItem[];
  discover: DiscoverItem[];
  options_templates: Array<{ id: string; name: string; outlook: string; description: string }>;
  recent_backtests: BacktestRunSummary[];
  deployments_summary: DeploymentSummary;
}

export interface ScannerColumnSpec {
  key: string;
  label: string;
  fmt?: string;
  format?: string;
  decimals?: number;
  digits?: number;
  align?: 'left' | 'right';
}

export interface ScannerResultRow {
  symbol: string;
  exchange: string;
  close: number;
  ltp?: number;
  change_pct: number;
  volume?: number;
  signal?: string;
  score?: number;
  matched?: boolean;
  last_date?: string;
  live_updated_ist?: string;
  tick_direction?: 'up' | 'down' | 'flat';
  is_live?: boolean;
  [key: string]: unknown;
}

export interface ScannerAlertSubscription {
  id: string;
  name: string;
  description: string;
  mode: 'basic' | 'advance' | 'python' | 'visual';
  screen_id: string;
  rule_def?: VisualRuleDef | null;
  params?: Record<string, unknown>;
  universe: string;
  basket_id?: string | null;
  symbols: string[];
  exchange: string;
  timeframe: string;
  chart_type: string;
  frequency_sec: number;
  status: 'ACTIVE' | 'PAUSED';
  last_checked_ist?: string | null;
  last_matched_symbols: string[];
  last_matched_count: number;
  alerts_triggered: number;
  created_at_ist: string;
  updated_at_ist: string;
}

export interface ScannerAlertEvent {
  id: string;
  subscription_id: string;
  scanner_name: string;
  symbol: string;
  exchange: string;
  transition: 'ENTERED' | 'EXITED';
  ltp: number;
  change_pct: number;
  volume: number;
  signal: string;
  timeframe: string;
  ts_ist: string;
}

export interface ScannerDraftPreset {
  mode: 'basic' | 'advance' | 'python';
  rule?: VisualRuleDef;
  pythonScreenId?: string;
  name?: string;
  description?: string;
  universe?: string;
  basketId?: string;
  timeframe?: string;
  chartType?: 'candlestick' | 'heikin_ashi';
  isCopy?: boolean;
}

export interface StrategyDraftPreset {
  mode: 'basic' | 'advance' | 'python';
  rule?: VisualRuleDef;
  pythonStrategyId?: string;
  name?: string;
  description?: string;
  symbols?: string[];
  universe?: string;
  timeframe?: string;
  chartType?: 'candlestick' | 'heikin_ashi';
  segment?: string;
  isCopy?: boolean;
  openFullResult?: BacktestFullResult | null;
}

export interface DeploySpec {
  backtest_run_id?: string;
  mode?: string;
  allocation_mode?: string;
  strategy_id: string;
  strategy_name?: string;
  strategy_mode: 'visual' | 'python';
  rule_def?: VisualRuleDef | null;
  params?: Record<string, unknown>;
  symbols: string[];
  exchange: string;
  timeframe: string;
  initial_capital: number;
  max_open_positions?: number;
  risk_per_trade_pct?: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  trailing_sl_pct: number;
  charges_profile: string;
  segment: string;
  product?: string;
  evaluation_mode?: 'completed_bar' | 'intrabar';
  charges_overrides?: Record<string, unknown>;
}

export interface BacktestRunSummary {
  run_id: string;
  mode: 'individual' | 'portfolio';
  allocation_mode?: string;
  strategy: string;
  strategy_label: string;
  strategy_mode: 'visual' | 'python';
  symbols: string[];
  symbol_source?: string;
  timeframe: string;
  from_date: string;
  to_date: string;
  initial_capital: number;
  final_equity: number;
  gross_pnl: number;
  total_charges: number;
  net_pnl: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  win_rate_pct: number;
  total_trades: number;
  charges_profile: string;
  segment: string;
  deploy_spec: DeploySpec;
  generated_at_ist: string;
}

export interface BacktestTradeRow {
  symbol?: string;
  side?: 'LONG' | 'SHORT';
  entry_date: string;
  exit_date: string;
  entry_bar_idx?: number;
  exit_bar_idx?: number;
  qty: number;
  entry_price: number;
  exit_price: number;
  gross_pnl: number;
  charges: number;
  pnl: number;
  return_pct: number;
  exit_reason: string;
}

export interface BacktestFullResult extends BacktestRunSummary {
  ok: boolean;
  side?: 'LONG' | 'SHORT';
  holding_type?: string;
  start_time?: string | null;
  end_time?: string | null;
  indicator_columns?: string[];
  has_ohlcv_export?: boolean;
  cagr_pct: number | null;
  annualized_return_pct?: number | null;
  duration_days?: number;
  cagr_methodology?: string;
  sample_warning?: string | null;
  reproducibility_manifest?: Record<string, unknown>;
  warnings?: string[];
  sharpe_ratio: number;
  daily_sharpe_ratio?: number;
  bar_sharpe_ratio?: number;
  sharpe_daily?: number;
  sharpe_bar?: number;
  execution_assumptions?: {
    entry_fill?: string;
    stop_fill?: string;
    same_bar_exit?: boolean;
    sharpe_method?: string;
    spread_bps?: number;
    max_volume_participation_pct?: number;
  };
  profit_factor: number;
  charges_breakdown: ChargeBreakdown;
  equity_curve: Array<{ date: string; equity: number; drawdown_pct: number; open_positions?: number }>;
  per_symbol: Array<{
    symbol: string;
    bars: number;
    trades: number;
    win_rate_pct: number;
    gross_pnl: number;
    charges: number;
    net_pnl: number;
    return_pct?: number;
    contribution_pct?: number;
    cagr_pct?: number;
    max_drawdown_pct?: number;
  }>;
  trades: BacktestTradeRow[];
  errors?: Array<{ symbol: string; error: string }>;
}

export interface DeploymentSummary {
  total_deployments: number;
  running_count: number;
  paper_count: number;
  live_count: number;
  paper_net_pnl: number;
  live_net_pnl: number;
  total_charges: number;
  pending_alerts_count: number;
}

export interface OmsOrderRecord {
  client_order_id: string;
  broker_order_id: string;
  deployment_id: string;
  broker_id: string;
  exec_mode?: string;
  mode?: string;
  symbol: string;
  exchange: string;
  side: string;
  product: string;
  order_type: string;
  quantity?: number;
  qty?: number;
  filled_quantity?: number;
  filled_qty?: number;
  average_fill_price?: number;
  avg_price?: number;
  limit_price?: number | null;
  status: string;
  rejection_reason?: string;
  error_class?: string;
  created_at_ist: string;
  updated_at_ist: string;
}

export interface DeploymentRecord {
  id: string;
  name: string;
  exec_mode: 'paper' | 'live_alert' | 'live_auto';
  evaluation_mode?: 'completed_bar' | 'intrabar';
  product?: string;
  status: string;
  strategy_id: string;
  strategy_name: string;
  strategy_mode: 'visual' | 'python';
  symbols: string[];
  exchange: string;
  timeframe: string;
  allocation_mode: string;
  charges_profile: string;
  segment: string;
  stop_loss_pct: number;
  take_profit_pct: number;
  trailing_sl_pct: number;
  initial_capital: number;
  cash: number;
  equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  net_pnl: number;
  return_pct: number;
  total_charges: number;
  open_positions_count: number;
  positions: Array<{
    symbol: string;
    qty: number;
    side?: string;
    entry_price: number;
    last_price: number;
    highest_price: number;
    entry_charges: number;
    entry_at_ist: string;
  }>;
  trades_count: number;
  trades: Array<{
    id: string;
    symbol: string;
    side: string;
    qty: number;
    entry_price: number;
    exit_price: number;
    gross_pnl: number;
    charges: number;
    net_pnl: number;
    reason: string;
    broker_order_id?: string;
    executed_at_ist: string;
  }>;
  last_note: string;
  created_at_ist: string;
  updated_at_ist: string;
}

export interface ActionableAlert {
  id: string;
  deployment_id: string;
  deployment_name: string;
  symbol: string;
  exchange: string;
  instrument_key: string;
  side: 'BUY' | 'SELL';
  qty: number;
  signal_price: number;
  reason: string;
  product: string;
  status: 'PENDING' | 'EXECUTED' | 'DISMISSED' | 'BLOCKED' | 'FAILED';
  broker_order_id?: string;
  note?: string;
  created_at_ist: string;
  resolved_at_ist?: string;
}

export interface DeploymentsPayload {
  ok: boolean;
  summary: DeploymentSummary;
  deployments: DeploymentRecord[];
  alerts: ActionableAlert[];
  pending_alerts: ActionableAlert[];
  kill_switch: { engaged: boolean; reason?: string };
  generated_at_ist: string;
}

export type PlatformNavId =
  | 'dashboard'
  | 'discover_strategies'
  | 'create_strategy'
  | 'deployed'
  | 'options'
  | 'discover_scanners'
  | 'create_scanner'
  | 'scanner_alerts'
  | 'technicals'
  | 'baskets'
  | 'risk'
  | 'terminal'
  | 'connections';
