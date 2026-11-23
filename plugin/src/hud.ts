// Pure HUD renderer: (frame, link state, clock) -> glasses text.
// Hard limits (enforced by hud.test.ts): ASCII only, <= 46 chars per line, <= 9 lines.
// The G2 font is proportional, so column padding is approximate on the real display.

import type { HudFrame, Position } from './protocol'

export const MAX_COLS = 46
export const MAX_LINES = 9
const SEPARATOR = '-'.repeat(40)
const FOOTER = 'tap: actions  swipe: next  2x: exit'

export type LinkView =
  | { kind: 'unconfigured' } // no VITE_BACKEND_URL
  | { kind: 'no-code' } // no pairing code yet
  | { kind: 'connecting'; attempt: number }
  | { kind: 'open' }
  | { kind: 'retrying'; inSeconds: number }
  | { kind: 'rejected' } // backend closed with 4401

export interface HudInput {
  frame: HudFrame | null
  link: LinkView
  now: Date
  /** ms since the last frame arrived (undefined if none yet) */
  frameAgeMs?: number
}

const STALE_FRAME_MS = 15_000

// --- number formatting ---------------------------------------------------------------------------

function group(n: number, decimals: number): string {
  return n.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

/** Decimals needed to show `sig` significant figures (0..6). */
function sigDecimals(n: number, sig: number): number {
  const a = Math.abs(n)
  const intDigits = a === 0 ? 1 : Math.floor(Math.log10(a)) + 1 // 0.0123 -> -1
  return Math.min(6, Math.max(0, sig - intDigits))
}

/** Hyperliquid prices carry at most 5 significant figures; show exactly that much (live prices). */
export function fmtLive(n: number): string {
  return group(n, sigDecimals(n, 5))
}

/** Compact price for entry / liquidation (mock: "3,412", "2,980"): 4 significant figures. */
export function fmtCompact(n: number): string {
  return group(n, sigDecimals(n, 4))
}

export function fmtSize(n: number): string {
  if (n >= 100) return group(n, 0)
  if (n >= 1) return group(n, 2)
  return n.toFixed(4).replace(/0{1,2}$/, '') // 0.5000 -> 0.50, 0.0123 -> 0.0123
}

export function fmtUsd(n: number): string {
  return `${n < 0 ? '-' : '+'}$${group(Math.abs(n), 2)}`
}

export function fmtPct(n: number): string {
  return `${n < 0 ? '-' : '+'}${Math.abs(n).toFixed(1)}%`
}

export function fmtClock(d: Date): string {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// --- lines ---------------------------------------------------------------------------------------

export function headerLine(now: Date): string {
  return 'TRIPWIRE'.padEnd(20) + 'TESTNET'.padEnd(13) + fmtClock(now)
}

function positionLines(p: Position, index: number, count: number): string[] {
  const lev = `${Number.isInteger(p.leverage) ? p.leverage : p.leverage.toFixed(1)}x`
  const counter = count > 1 ? `  ${index + 1}/${count}` : ''
  const l1 = `${p.coin}-PERP ${p.side} ${fmtSize(p.size)} @ ${fmtCompact(p.entry)}  ${lev}${counter}`
  const l2 = `uPnL ${fmtUsd(p.upnl)} (${fmtPct(p.upnl_pct)})`
  let l3: string
  if (p.liq_px === null || p.liq_dist_pct === null) l3 = 'Liq none'
  else if (p.liq_dist_pct <= 0) l3 = `Liq ${fmtCompact(p.liq_px)}  AT/PAST LIQ`
  else l3 = `Liq ${fmtCompact(p.liq_px)}  ${p.liq_dist_pct.toFixed(1)}% away`
  if (p.mark_source === 'mid') l3 += ' (mid)' // no markPx yet: distance measured on mid
  return [l1, l2, l3]
}

function referenceLines(f: HudFrame): string[] {
  const { coin, mark, mid, mark_source } = f.ref
  const px = mark ?? mid
  const label = mark_source === 'markPx' ? 'mark' : 'mid'
  const l1 = px === null ? `${coin}-PERP  waiting for price...` : `${coin}-PERP  ${fmtLive(px)}  ${label}`
  const l2 = f.account ? 'No open position' : 'No account set (price only)'
  return [l1, l2]
}

function feedLine(f: HudFrame, frameAgeMs: number | undefined): string | null {
  if (frameAgeMs !== undefined && frameAgeMs > STALE_FRAME_MS) {
    return `Link stale ${Math.round(frameAgeMs / 1000)}s - reconnecting`
  }
  if (!f.feed.connected) return 'Hyperliquid feed reconnecting...'
  return null
}

function linkLines(link: LinkView): string[] {
  switch (link.kind) {
    case 'unconfigured':
      return ['No backend URL configured.', 'Set VITE_BACKEND_URL, rebuild.']
    case 'no-code':
      return ['Not paired.', 'Enter pairing code on phone.']
    case 'connecting':
      return [link.attempt > 1 ? `Connecting... (try ${link.attempt})` : 'Connecting to backend...']
    case 'retrying':
      return ['Backend unreachable.', `Retrying in ${link.inSeconds}s...`]
    case 'rejected':
      return ['Pairing code rejected.', 'Fix it on the phone page.']
    case 'open':
      return ['Connected. Waiting for data...']
  }
}

// --- page ----------------------------------------------------------------------------------------

export function renderHud({ frame, link, now, frameAgeMs }: HudInput): string {
  const body: string[] = []
  if (link.kind === 'open' && frame) {
    if (frame.position) body.push(...positionLines(frame.position, frame.view.index, frame.view.count))
    else body.push(...referenceLines(frame))
    const feed = feedLine(frame, frameAgeMs)
    if (feed) body.push(feed)
  } else {
    body.push(...linkLines(link))
  }
  const lines = [headerLine(now), ...body, SEPARATOR, FOOTER]
  return lines.slice(0, MAX_LINES).map(fit).join('\n')
}

/** Last line of defence: strip non-ASCII and truncate to MAX_COLS. */
export function fit(line: string): string {
  // eslint-disable-next-line no-control-regex
  const ascii = line.replace(/[^\x20-\x7E]/g, '?')
  return ascii.length <= MAX_COLS ? ascii : ascii.slice(0, MAX_COLS - 1) + '~'
}
