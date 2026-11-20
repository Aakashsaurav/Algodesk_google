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
}

export interface VisualCondition {
  left: VisualOperandSpec;
  operator: string;
  right: VisualOperandSpec;
}

export interface VisualConditionGroup {
  logic: 'AND' | 'OR';
  conditions: VisualCondition[];
}

export interface VisualRuleDef {
  id?: string;
  kind: 'strategy' | 'screen';
  mode?: 'visual';
  name: string;
  description?: string;
  timeframe?: string;
  segment?: string;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  trailing_sl_pct?: number;
  entry: VisualConditionGroup;
  exit?: VisualConditionGroup;
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

export interface StrategySpecMeta {
  id: string;
  name: string;
  description: string;
  source_file?: string;
  params: Array<{
    name: string;
    label: string;
    kind: string;
    default: number | string | boolean;
    min?: number;
    max?: number;
    step?: number;
    options?: string[];
  }>;
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
  screens: Array<{ id: string; name: string; description: string }>;
  strategy_specs: StrategySpecMeta[];
  universes: UniverseMeta[];
  timeframes: Array<{ id: string; label: string; base: string; resampled: boolean }>;
  indicator_backends: string[];
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

export interface BacktestFullResult extends BacktestRunSummary {
  ok: boolean;
  cagr_pct: number;
  sharpe_ratio: number;
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
  trades: Array<{
    symbol?: string;
    entry_date: string;
    exit_date: string;
    qty: number;
    entry_price: number;
    exit_price: number;
    gross_pnl: number;
    charges: number;
    pnl: number;
    return_pct: number;
    exit_reason: string;
  }>;
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

export interface DeploymentRecord {
  id: string;
  name: string;
  exec_mode: 'paper' | 'live_alert' | 'live_auto';
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
  | 'discover'
  | 'scanners'
  | 'strategies'
  | 'options'
  | 'technicals'
  | 'deployed'
  | 'baskets'
  | 'terminal'
  | 'connections';
