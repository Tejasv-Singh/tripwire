// Wire types for /ws (protocol v2) — keep in sync with backend/src/tripwire/{hud,ws,actions}.py
// and docs/ws-protocol.md.

export const PROTOCOL_VERSION = 2

export type Side = 'LONG' | 'SHORT'
export type MarkSource = 'markPx' | 'mid'
/** v2: tap/back replace v1's "actions". The backend decides what a tap means on each screen. */
export type IntentAction = 'next' | 'prev' | 'tap' | 'back'
export type UiMode = 'hud' | 'menu' | 'confirm' | 'sending' | 'result' | 'reply'
export type TradingStatus = 'off' | 'warming' | 'ready' | 'error'
/** blocked = refused by the policy guard; dry_run = signed and logged, not sent */
export type ResultStatus = 'filled' | 'resting' | 'placed' | 'rejected' | 'error' | 'blocked' | 'dry_run'

export interface Position {
  coin: string
  side: Side
  size: number
  entry: number
  mark: number
  upnl: number
  upnl_pct: number
  /** return on initial margin (Hyperliquid's ROE); null if leverage unknown */
  roe_pct: number | null
  liq_px: number | null
  liq_dist_pct: number | null
  leverage: number
  mid: number
  mark_source: MarkSource
  leverage_type: string
}

export type MenuKind = 'actions' | 'sl' | 'alert' | 'draft'

/** A forwarded Telegram message parsed into an order (backend drafts.py). */
export interface DraftInfo {
  coin: string
  side: Side
  notional_usd: number
  leverage: number
  entry_px: number | null // null = market
  sl_px: number | null
  tp_px: number | null
  defaults: string[] // "notional" / "leverage" when the message did not say
}

export interface PolicyVerdict {
  allowed: boolean
  rule: string
  reason: string
}

/** A liquidation-distance alert from the backend risk engine (risk.py). */
export interface AlertInfo {
  coin: string
  side: Side
  size: number
  liq_px: number
  liq_dist_pct: number
  mark: number
  threshold_pct: number
}

export interface MenuScreen {
  mode: 'menu'
  /** actions: Close 25/50/100% + Move SL...; sl: SL to breakeven / tighter 1% */
  kind?: MenuKind
  coin: string
  items: string[]
  cursor: number
  trading: boolean
  note: string | null
  /** kind "sl": the position's current stop-loss, once the exchange answered */
  sl_loaded?: boolean
  current_sl?: number | null
  /** kind "alert": what fired (items: Move SL / Close 50% / Close all / Snooze 5m) */
  alert?: AlertInfo
  /** kind "draft": the forwarded instruction and the policy guard's verdict on it */
  draft?: DraftInfo
  policy?: PolicyVerdict
}

interface OrderScreenBase {
  mode: 'confirm' | 'sending'
  coin: string
  side: Side
  label: string
  expires_in_ms: number
}

/** Reduce-only close (Day 2). `action` was absent in v2.0 frames: treat missing as "close". */
export interface CloseScreen extends OrderScreenBase {
  action?: 'close'
  fraction: number
  size: number
  all: boolean
  exact: boolean
  est_px: number
  slippage_pct: number
}

/** Move the stop-loss: new SL placed first, then the old one cancelled. */
export interface StopScreen extends OrderScreenBase {
  action: 'stop'
  old_sl: number | null
  new_sl: number
  mark: number
  entry: number
  size: number
}

/** Open a position from a forwarded draft (SL and TP attached, reduce-only). */
export interface OpenScreen extends OrderScreenBase {
  action: 'open'
  notional: number
  leverage: number
  entry: number | null
  sl: number | null
  tp: number | null
  size: number | null // estimate; the adapter rounds to the lot size when sending
  exact: boolean
}

/** The confirm and sending screens show the exact order being confirmed / sent. */
export type OrderScreen = CloseScreen | StopScreen | OpenScreen

export interface ResultScreen {
  mode: 'result'
  coin: string | null
  ok: boolean
  status: ResultStatus
  message: string
  label: string | null
}

/** After a real fill: a short drafted reply to the last Telegram Business chat. */
export interface ReplyScreen {
  mode: 'reply'
  coin: string | null
  to: string // "@alice"
  text: string // <= 60 characters
  source: 'openrouter' | 'template'
  /** the order result the offer follows (kept on screen) */
  result_status?: ResultStatus
  result_message?: string
}

/** Which screen the backend's ActionFlow is on for this client (actions.py). */
export type UiState = { mode: 'hud' } | MenuScreen | OrderScreen | ResultScreen | ReplyScreen

/** Telegram Business messages of the last 10 minutes (first 30 chars, backend memory only). */
export interface InboxSummary {
  count: number
  from: string
  preview: string
}

export interface TradingInfo {
  enabled: boolean
  status: TradingStatus
  /** orders are built and signed but NOT sent (DRY_RUN=true) */
  dry_run?: boolean
  /** "hyperliquid" | "fake" (demo) | "bybit_demo" */
  exchange?: string
}

export interface HudFrame {
  type: 'hud'
  v: number
  seq: number
  ts: number
  network: 'testnet'
  feed: { connected: boolean; age_ms: number | null }
  account: boolean
  /** positions belong to WATCH_ADDRESS (someone else's): read-only, the menu is disabled */
  watch: boolean
  /** shortened address being streamed ("0x1234...abcd"), "" in price-only mode */
  address: string
  view: { index: number; count: number }
  position: Position | null
  ref: { coin: string; mid: number | null; mark: number | null; mark_source: MarkSource }
  ui: UiState
  trading: TradingInfo
  inbox?: InboxSummary | null
}

export interface HelloFrame {
  type: 'hello'
  v: number
  network: 'testnet'
  min_interval_ms: number
  heartbeat_ms: number
  trading: TradingInfo
}

export interface AckFrame {
  type: 'ack'
  action: IntentAction
  ok: boolean
  mode: UiMode
  view: { index: number; count: number }
  note?: string
}

/** Sent once when a confirmed close comes back from Hyperliquid (the hud frame shows it too). */
export interface OrderFrame {
  type: 'order'
  ok: boolean
  status: ResultStatus
  message: string
  coin: string
  /** close / stop / open */
  kind?: string
  /** close only (0.25 / 0.5 / 1); null for stop-loss moves and opens */
  fraction: number | null
}

export interface ErrorFrame {
  type: 'error'
  error: string
  raw?: string
}

/** Result of sending a drafted reply. */
export interface ReplyFrame {
  type: 'reply'
  ok: boolean
  to: string
}

export type ServerFrame = HudFrame | HelloFrame | AckFrame | OrderFrame | ReplyFrame | ErrorFrame

export interface IntentMessage {
  type: 'intent'
  action: IntentAction
}

/** Backend closes with this code when the pairing code is wrong. */
export const CLOSE_BAD_PAIRING_CODE = 4401

export function parseServerFrame(raw: string): ServerFrame | null {
  try {
    const msg = JSON.parse(raw) as { type?: unknown }
    if (msg && typeof msg === 'object' && typeof msg.type === 'string') return msg as ServerFrame
  } catch {
    /* not JSON (e.g. an HTML error page) */
  }
  return null
}
