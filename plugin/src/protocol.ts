// Wire types for /ws (protocol v2) — keep in sync with backend/src/tripwire/{hud,ws,actions}.py
// and docs/ws-protocol.md.

export const PROTOCOL_VERSION = 2

export type Side = 'LONG' | 'SHORT'
export type MarkSource = 'markPx' | 'mid'
/** v2: tap/back replace v1's "actions". The backend decides what a tap means on each screen. */
export type IntentAction = 'next' | 'prev' | 'tap' | 'back'
export type UiMode = 'hud' | 'menu' | 'confirm' | 'sending' | 'result'
export type TradingStatus = 'off' | 'warming' | 'ready' | 'error'

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

/** Which screen the backend's ActionFlow is on for this client (actions.py). */
export type UiState =
  | { mode: 'hud' }
  | {
      mode: 'menu'
      coin: string
      items: string[]
      cursor: number
      trading: boolean
      note: string | null
    }
  | {
      mode: 'confirm' | 'sending'
      coin: string
      side: Side
      label: string
      fraction: number
      size: number
      all: boolean
      exact: boolean
      est_px: number
      slippage_pct: number
      expires_in_ms: number
    }
  | {
      mode: 'result'
      coin: string | null
      ok: boolean
      status: 'filled' | 'resting' | 'rejected' | 'error'
      message: string
      label: string | null
    }

/** The confirm and sending screens share one shape: the exact order being confirmed / sent. */
export type OrderScreen = Extract<UiState, { mode: 'confirm' | 'sending' }>
export type MenuScreen = Extract<UiState, { mode: 'menu' }>
export type ResultScreen = Extract<UiState, { mode: 'result' }>

export interface TradingInfo {
  enabled: boolean
  status: TradingStatus
}

export interface HudFrame {
  type: 'hud'
  v: number
  seq: number
  ts: number
  network: 'testnet'
  feed: { connected: boolean; age_ms: number | null }
  account: boolean
  view: { index: number; count: number }
  position: Position | null
  ref: { coin: string; mid: number | null; mark: number | null; mark_source: MarkSource }
  ui: UiState
  trading: TradingInfo
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
  status: 'filled' | 'resting' | 'rejected' | 'error'
  message: string
  coin: string
  fraction: number
}

export interface ErrorFrame {
  type: 'error'
  error: string
  raw?: string
}

export type ServerFrame = HudFrame | HelloFrame | AckFrame | OrderFrame | ErrorFrame

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
