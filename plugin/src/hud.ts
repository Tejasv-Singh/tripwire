// Pure HUD renderer: (frame, link state, clock) -> glasses text.
// Hard limits (enforced by hud.test.ts): <= 46 chars per line, <= 9 lines, ASCII only -- except
// RISK_GLYPH, the one non-ASCII character allowed (it heads the liquidation alert screen).
// The G2 font is proportional, so column padding is approximate on the real display.

import type { HudFrame, MenuScreen, OrderScreen, Position, ResultScreen, ResultStatus, UiMode } from './protocol'

export const MAX_COLS = 46
export const MAX_LINES = 9
/** Heads the alert screen. Checked in the simulator (docs/BUILD_LOG.md §18); swap for '/!\' if a
 *  display ever shows a box instead. */
export const RISK_GLYPH = '▲' // "▲"
const BODY_LINES = MAX_LINES - 3 // header + separator + footer are always shown
const SEPARATOR = '-'.repeat(40)
export const FOOTERS: Record<UiMode | 'link' | 'watch', string> = {
  hud: 'tap: actions  swipe: next  2x: exit',
  menu: 'tap: select  swipe: move  2x: back',
  confirm: 'tap: SEND  swipe/2x: cancel',
  sending: 'sending... please wait',
  result: 'tap/swipe/2x: back to HUD',
  link: 'tap: actions  swipe: next  2x: exit',
  watch: 'watch only  swipe: next  2x: exit', // WATCH_ADDRESS: the menu is disabled
}

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

/** Exact order size for the confirm screen: every decimal the backend sent (up to 8). */
export function fmtQty(n: number): string {
  return n.toLocaleString('en-US', { maximumFractionDigits: 8 })
}

export function fmtClock(d: Date): string {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// --- lines ---------------------------------------------------------------------------------------

/** `tag` marks a special mode next to the name, e.g. "WATCH" (read-only, someone else's account). */
export function headerLine(now: Date, tag = ''): string {
  return `TRIPWIRE${tag ? ` ${tag}` : ''}`.padEnd(20) + 'TESTNET'.padEnd(13) + fmtClock(now)
}

function positionLines(p: Position, index: number, count: number): string[] {
  const lev = `${Number.isInteger(p.leverage) ? p.leverage : p.leverage.toFixed(1)}x`
  const counter = count > 1 ? `  ${index + 1}/${count}` : ''
  const l1 = `${p.coin}-PERP ${p.side} ${fmtSize(p.size)} @ ${fmtCompact(p.entry)}  ${lev}${counter}`
  let l2 = `uPnL ${fmtUsd(p.upnl)} (${fmtPct(p.upnl_pct)})`
  if (p.roe_pct !== null && p.roe_pct !== undefined) l2 += `  ROE ${fmtPct(p.roe_pct)}`
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
  const l2 = f.watch
    ? `Watching ${f.address}: no position`
    : f.account
      ? 'No open position'
      : 'No account set (price only)'
  return [l1, l2]
}

function feedLine(f: HudFrame, frameAgeMs: number | undefined): string | null {
  if (frameAgeMs !== undefined && frameAgeMs > STALE_FRAME_MS) {
    return `Link stale ${Math.round(frameAgeMs / 1000)}s - reconnecting`
  }
  if (!f.feed.connected) return 'Hyperliquid feed reconnecting...'
  return null
}

// --- action screens (backend ActionFlow; see backend/src/tripwire/actions.py) --------------------

function positionTag(p: Position | null, coin: string | null): string {
  if (p) return `${p.coin}-PERP ${p.side} ${fmtSize(p.size)}`
  return coin ? `${coin}-PERP  position closed` : 'position closed'
}

function slInfoLine(f: HudFrame, ui: MenuScreen): string {
  const now = !ui.sl_loaded ? 'loading...' : ui.current_sl == null ? 'none' : fmtCompact(ui.current_sl)
  const p = f.position
  return p ? `SL now ${now}  entry ${fmtCompact(p.entry)}  mark ${fmtCompact(p.mark)}` : `SL now ${now}`
}

function alertLines(ui: MenuScreen): string[] {
  const a = ui.alert
  if (!a) return [`${RISK_GLYPH} RISK  ${ui.coin}-PERP`]
  return [
    `${RISK_GLYPH} RISK  ${a.coin}-PERP ${a.side} ${fmtSize(a.size)}`,
    `Liq ${fmtCompact(a.liq_px)}  ${a.liq_dist_pct.toFixed(1)}% from mark (<${a.threshold_pct}%)`,
  ]
}

function menuLines(f: HudFrame, ui: MenuScreen): string[] {
  const tag = positionTag(f.position, ui.coin)
  const lines =
    ui.kind === 'alert'
      ? alertLines(ui)
      : ui.kind === 'sl'
        ? [`MOVE SL  ${tag}`, slInfoLine(f, ui)]
        : [`ACTIONS  ${tag}  reduce-only`]
  ui.items.forEach((item, i) => lines.push(`${i === ui.cursor ? '>' : ' '} ${item}`))
  const note = ui.note ?? (ui.trading ? null : 'Trading off: read-only (no agent key)')
  if (note) lines.push(note)
  return lines
}

/** What will actually be sent, in one line. */
function orderLine(ui: OrderScreen): string {
  if (ui.action === 'stop') {
    const old = ui.old_sl == null ? 'none' : fmtCompact(ui.old_sl)
    return `SL ${old} -> ${fmtCompact(ui.new_sl)}  reduce-only`
  }
  const verb = ui.side === 'LONG' ? 'SELL' : 'BUY' // closing a long sells, closing a short buys
  const qty = ui.all ? `ALL ${fmtQty(ui.size)}` : `${ui.exact ? '' : '~'}${fmtQty(ui.size)}`
  return `${verb} ${qty} ${ui.coin}  reduce-only IOC`
}

function confirmLines(ui: OrderScreen, dryRun: boolean): string[] {
  const secs = Math.max(1, Math.ceil(ui.expires_in_ms / 1000))
  const detail =
    ui.action === 'stop'
      ? [
          ui.old_sl == null ? 'adds a stop for the whole position' : 'new SL placed first, then old cancelled',
          `mark ${fmtLive(ui.mark)}  entry ${fmtCompact(ui.entry)}`,
        ]
      : [`at ~${fmtLive(ui.est_px)} mark, max ${ui.slippage_pct}% slippage`]
  return [
    `CONFIRM  ${ui.label}  ${ui.coin}-PERP ${ui.side}`,
    orderLine(ui),
    ...detail,
    dryRun ? `Tap again within ${secs}s to SIGN (dry run).` : `Tap again within ${secs}s to SEND.`,
  ]
}

/** Header tag: what kind of session this is, so nobody mistakes it for live trading. */
export function headerTag(frame: HudFrame | null, link: LinkView): string {
  if (link.kind !== 'open' || !frame) return ''
  if (frame.watch) return 'WATCH' // someone else's positions, read-only
  if (frame.trading?.exchange === 'fake') return 'DEMO' // in-memory fake exchange
  if (frame.trading?.enabled && frame.trading.dry_run) return 'DRY RUN' // signed, never sent
  return ''
}

function sendingLines(ui: OrderScreen): string[] {
  return [`SENDING  ${ui.label}  ${ui.coin}-PERP`, orderLine(ui), 'Waiting for Hyperliquid testnet...']
}

const RESULT_TITLES: Record<ResultStatus, string> = {
  filled: 'FILLED',
  resting: 'RESTING',
  placed: 'PLACED',
  rejected: 'NOT DONE',
  error: 'ERROR',
  blocked: 'BLOCKED', // policy guard said no (backend policy.py); nothing was sent
  dry_run: 'DRY RUN', // signed, logged, NOT sent (DRY_RUN=true)
}

function resultLines(ui: ResultScreen): string[] {
  const what = [ui.label, ui.coin ? `${ui.coin}-PERP` : null].filter(Boolean).join('  ')
  return [`${RESULT_TITLES[ui.status]}  ${what}`.trimEnd(), ...wrap(ui.message, MAX_COLS).slice(0, 3)]
}

function screenLines(f: HudFrame): string[] {
  const ui = f.ui ?? { mode: 'hud' }
  switch (ui.mode) {
    case 'menu':
      return menuLines(f, ui)
    case 'confirm':
      return confirmLines(ui, f.trading?.dry_run === true)
    case 'sending':
      return sendingLines(ui)
    case 'result':
      return resultLines(ui)
    case 'hud':
      return f.position ? positionLines(f.position, f.view.index, f.view.count) : referenceLines(f)
  }
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

/** Screen the plugin is showing: the backend's ui.mode while the link is up, else the HUD. */
export function currentMode(frame: HudFrame | null, link: LinkView): UiMode {
  return link.kind === 'open' && frame?.ui ? frame.ui.mode : 'hud'
}

export function renderHud({ frame, link, now, frameAgeMs }: HudInput): string {
  const body: string[] = []
  const mode = currentMode(frame, link)
  if (link.kind === 'open' && frame) {
    body.push(...screenLines(frame))
    const feed = feedLine(frame, frameAgeMs)
    if (feed) body.push(feed)
  } else {
    body.push(...linkLines(link))
  }
  const tag = headerTag(frame, link)
  const footer = link.kind !== 'open' ? FOOTERS.link : tag === 'WATCH' && mode === 'hud' ? FOOTERS.watch : FOOTERS[mode]
  const lines = [headerLine(now, tag), ...body.slice(0, BODY_LINES), SEPARATOR, footer]
  return lines.map(fit).join('\n')
}

/** Word-wrap to `width` columns (long words are hard-split). */
export function wrap(text: string, width: number): string[] {
  const out: string[] = []
  let line = ''
  for (const word of text.split(/\s+/).filter(Boolean)) {
    let w = word
    while (w.length > width) {
      if (line) out.push(line)
      line = ''
      out.push(w.slice(0, width))
      w = w.slice(width)
    }
    if (!line) line = w
    else if (line.length + 1 + w.length <= width) line += ` ${w}`
    else {
      out.push(line)
      line = w
    }
  }
  if (line) out.push(line)
  return out
}

/** Last line of defence: strip non-ASCII and truncate to MAX_COLS. */
export function fit(line: string): string {
  // eslint-disable-next-line no-control-regex
  const ascii = line.replace(/[^\x20-\x7E▲]/g, '?') // printable ASCII + RISK_GLYPH only
  return ascii.length <= MAX_COLS ? ascii : ascii.slice(0, MAX_COLS - 1) + '~'
}
