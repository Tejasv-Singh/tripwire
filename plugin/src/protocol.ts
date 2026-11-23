// Wire types for /ws — keep in sync with backend/src/tripwire/{hud,ws}.py and docs/ws-protocol.md.

export type Side = 'LONG' | 'SHORT'
export type MarkSource = 'markPx' | 'mid'
export type IntentAction = 'next' | 'prev' | 'actions'

export interface Position {
  coin: string
  side: Side
  size: number
  entry: number
  mark: number
  upnl: number
  upnl_pct: number
  liq_px: number | null
  liq_dist_pct: number | null
  leverage: number
  mid: number
  mark_source: MarkSource
  leverage_type: string
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
}

export interface HelloFrame {
  type: 'hello'
  v: number
  network: 'testnet'
  min_interval_ms: number
  heartbeat_ms: number
}

export interface AckFrame {
  type: 'ack'
  action: IntentAction
  ok: boolean
  view: { index: number; count: number }
  note?: string
}

export interface ErrorFrame {
  type: 'error'
  error: string
  raw?: string
}

export type ServerFrame = HudFrame | HelloFrame | AckFrame | ErrorFrame

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
