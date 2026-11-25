// Pure HUD renderer: (frame, link state, clock) -> glasses text.
// Hard limits (enforced by hud.test.ts): <= 46 chars per line, <= 9 lines, ASCII only -- except
// the glyphs of the frame's glyph set (GLYPH_SETS below: ▲ ━ ● when the backend says
// GLYPHS=unicode). The G2 font is proportional, so column padding is approximate on the display.

import type {
  EndScreen,
  HudFrame,
  SessionSummaryInfo,
  SummaryScreen,
  MenuScreen,
  OrderScreen,
  Position,
  ReplyScreen,
  ResultScreen,
  ResultStatus,
  UiMode,
} from './protocol'

export const MAX_COLS = 46
export const MAX_LINES = 9
const BODY_LINES = MAX_LINES - 3 // header + separator + footer are always shown

/** The few non-ASCII glyphs Tripwire draws, and their ASCII fallbacks.
 *  - unicode: GLYPHS=unicode in the backend .env (the default)
 *  - ascii:   GLYPHS=ascii, for a display that shows boxes instead of the glyphs
 *  - legacy:  frames without the field (older backend): exactly the Day-2 look
 *  `live` is the dot after TESTNET while data is fresh ('' = no dot). */
export const GLYPH_SETS = {
  // 26 x ━ ≈ 520 of 576 px on the simulator's G2 font (20 only reached ~400, BUILD_LOG §28)
  unicode: { risk: '▲', rule: '━'.repeat(26), live: '●', allowed: '▲━●' },
  ascii: { risk: '/!\\', rule: '-'.repeat(40), live: '*', allowed: '' },
  legacy: { risk: '▲', rule: '-'.repeat(40), live: '', allowed: '▲' },
} as const
export type GlyphSet = (typeof GLYPH_SETS)[keyof typeof GLYPH_SETS]
/** Heads the alert screen (Day 2 name; checked in the simulator, docs/BUILD_LOG.md §20). */
export const RISK_GLYPH = GLYPH_SETS.unicode.risk
/** No frame for this long, or prices this old -> STALE on the glasses (Day 3 §26). */
export const STALE_MS = 3_000
const RECONNECT_HINT_MS = 8_000 // = link.ts WATCHDOG_MS: it drops a silent socket after this long
export const FOOTERS: Record<UiMode | 'link' | 'watch', string> = {
  hud: 'tap: menu  swipe: next  hold: talk  2x: end',
  menu: 'tap: select  swipe: move  2x: back',
  confirm: 'tap: SEND  swipe/2x: cancel',
  sending: 'sending... please wait',
  result: 'tap/swipe/2x: back to HUD',
  reply: 'tap: send reply  2x: skip',
  end: 'tap: end + summary  swipe: back  2x: exit',
  summary: 'tap: post to journal  swipe: HUD  2x: exit',
  link: 'tap: actions  swipe: next  2x: exit',
  watch: 'watch only  swipe: next  hold: talk  2x: end', // WATCH_ADDRESS: no trading menu
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
  /** push-to-talk held right now (local, so "listening" shows before the backend answers) */
  listening?: boolean
  /** ms since the last frame arrived (undefined if none yet) */
  frameAgeMs?: number
}

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

/** `tag` marks a special mode next to the name, e.g. "WATCH" (read-only, someone else's account).
 *  `dot` follows TESTNET: the live glyph while data is fresh, "!" when it is stale. */
export function headerLine(now: Date, tag = '', dot = ''): string {
  const net = dot ? `TESTNET ${dot}` : 'TESTNET'
  return `TRIPWIRE${tag ? ` ${tag}` : ''}`.padEnd(20) + net.padEnd(13) + fmtClock(now)
}

export function glyphSet(frame: HudFrame | null): GlyphSet {
  const g = frame?.glyphs
  return g === 'unicode' ? GLYPH_SETS.unicode : g === 'ascii' ? GLYPH_SETS.ascii : GLYPH_SETS.legacy
}

/** How stale the screen is, or null when fresh: the link first (no frame from the backend; it
 *  sends one at least every second), then the prices inside the last frame. */
export function staleness(f: HudFrame, frameAgeMs = 0): { what: 'link' | 'prices'; s: number } | null {
  if (frameAgeMs > STALE_MS) return { what: 'link', s: Math.round(frameAgeMs / 1000) }
  if (!f.feed.connected || f.feed.age_ms === null) return null // the feed line says so already
  const age = f.feed.age_ms + frameAgeMs
  return age > STALE_MS ? { what: 'prices', s: Math.round(age / 1000) } : null
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
  const stale = staleness(f, frameAgeMs)
  if (stale?.what === 'link') {
    const hint = (frameAgeMs ?? 0) >= RECONNECT_HINT_MS ? ' - reconnecting' : ''
    return `STALE: no update for ${stale.s}s${hint}`
  }
  if (!f.feed.connected) return 'Hyperliquid feed reconnecting...'
  if (stale?.what === 'prices') return `STALE: prices ${stale.s}s old`
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

function alertLines(ui: MenuScreen, g: GlyphSet): string[] {
  const a = ui.alert
  if (!a) return [`${g.risk} RISK  ${ui.coin}-PERP`]
  return [
    `${g.risk} RISK  ${a.coin}-PERP ${a.side} ${fmtSize(a.size)}`,
    `Liq ${fmtCompact(a.liq_px)}  ${a.liq_dist_pct.toFixed(1)}% from mark (<${a.threshold_pct}%)`,
  ]
}

export const DRAFT_HEADER = 'FORWARDED - your instruction, not advice'

const fmtEntry = (px: number | null): string => (px == null ? 'market' : `@${fmtLive(px)}`)
const fmtLevels = (sl: number | null, tp: number | null): string =>
  [sl == null ? 'no SL' : `SL ${fmtLive(sl)}`, tp == null ? null : `TP ${fmtLive(tp)}`].filter(Boolean).join('  ')

function draftLines(ui: MenuScreen): string[] {
  const d = ui.draft
  if (!d) return [DRAFT_HEADER]
  const lev = `${d.leverage}x${d.defaults.includes('leverage') ? '*' : ''}`
  const usd = `$${d.notional_usd.toLocaleString('en-US')}${d.defaults.includes('notional') ? '*' : ''}`
  const verdict = !ui.policy ? 'Policy: not checked' : ui.policy.allowed ? 'Policy OK' : `BLOCKED: ${ui.policy.reason}`
  return [DRAFT_HEADER, `${d.coin} ${d.side} ${usd} ${lev}  ${fmtEntry(d.entry_px)}`, fmtLevels(d.sl_px, d.tp_px), verdict]
}

function menuLines(f: HudFrame, ui: MenuScreen): string[] {
  const tag = positionTag(f.position, ui.coin)
  const lines =
    ui.kind === 'draft'
      ? draftLines(ui)
      : ui.kind === 'alert'
        ? alertLines(ui, glyphSet(f))
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
  if (ui.action === 'open') {
    const verb = ui.side === 'LONG' ? 'BUY' : 'SELL'
    const qty = ui.size == null ? '' : ` ~${fmtQty(Number(ui.size.toPrecision(4)))}`
    return `${verb}${qty} ${ui.coin}  $${ui.notional.toLocaleString('en-US')} ${ui.leverage}x  ${fmtEntry(ui.entry)}`
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
      : ui.action === 'open'
        ? [`${fmtLevels(ui.sl, ui.tp)}  attached`, 'your forwarded instruction, not advice']
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

/** "TG 1 new @alice: still in ETH?" -- Telegram Business messages of the last 10 minutes. */
export function inboxLine(f: HudFrame): string | null {
  const i = f.inbox
  return i && i.count > 0 ? `TG ${i.count} new ${i.from}: ${i.preview}` : null
}

function replyLines(ui: ReplyScreen): string[] {
  const by = ui.source === 'openrouter' ? 'drafted by OpenRouter' : 'drafted from a template'
  // the fill stays visible above the offer (a template draft replaces the result instantly)
  const done = ui.result_status ? [`${RESULT_TITLES[ui.result_status]}  ${ui.result_message ?? ''}`.trimEnd()] : []
  return [...done, `REPLY to ${ui.to} on Telegram?`, ...wrap(`"${ui.text}"`, MAX_COLS).slice(0, 2), by]
}

// --- session (screen 6) --------------------------------------------------------------------------

function hhmm(ms: number): string {
  return fmtClock(new Date(ms))
}

export function fmtDuration(s: number): string {
  const m = Math.floor(s / 60)
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

function sessionSpan(s: SessionSummaryInfo): string {
  return `${hhmm(s.started_ms)}-${s.ended_ms ? hhmm(s.ended_ms) : 'now'} (${fmtDuration(s.duration_s)})`
}

function endLines(ui: EndScreen): string[] {
  const s = ui.summary
  if (!s) return ['END SESSION?']
  return [
    `END SESSION?  ${sessionSpan(s)}`,
    `Trades ${s.trades}  Session P&L ${fmtUsd(s.session_pnl)}`,
    `Max drawdown ${fmtUsd(s.max_drawdown)}`,
  ]
}

function summaryLines(ui: SummaryScreen): string[] {
  const s = ui.summary
  const trades = `Trades ${s.trades}${s.dry_runs ? ` (+${s.dry_runs} dry)` : ''}`
  const last =
    ui.posted === true
      ? 'Posted to journal'
      : ui.posted === false
        ? ui.note?.startsWith('DRY RUN') // no JOURNAL_CHAT_ID: printed to the backend log
          ? ui.note
          : `Not posted: ${ui.note ?? ''}`
        : s.mode
  return [
    `SESSION ${sessionSpan(s)}`,
    `${trades}  Realized ${fmtUsd(s.realized)}`,
    `uPnL now ${fmtUsd(s.unrealized)}  Session ${fmtUsd(s.session_pnl)}`,
    `Max drawdown ${fmtUsd(s.max_drawdown)}  Blocks ${s.policy_blocks}`,
    `Alerts ${s.alerts_fired} fired ${s.alerts_acted} acted ${s.alerts_snoozed} snoozed`,
    last,
  ]
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
    case 'reply':
      return replyLines(ui)
    case 'end':
      return endLines(ui)
    case 'summary':
      return summaryLines(ui)
    case 'hud': {
      const lines = f.position ? positionLines(f.position, f.view.index, f.view.count) : referenceLines(f)
      const tg = inboxLine(f)
      return tg ? [...lines, tg] : lines
    }
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

/** Push-to-talk replaces the header line while it is relevant (~2 s after speaking). */
export function voiceHeader(frame: HudFrame | null, listening: boolean): string | null {
  if (listening) return 'MIC  listening... release to send'
  const v = frame?.voice
  if (!v) return null
  if (v.state === 'listening') return 'MIC  listening... release to send'
  if (v.state === 'transcribing') return 'MIC  ...'
  if (v.state === 'error') return `MIC  ${v.note ?? 'voice unavailable'}`
  return v.text ? `MIC "${v.text}"` : 'MIC  (nothing heard)'
}

export function renderHud({ frame, link, now, frameAgeMs, listening = false }: HudInput): string {
  const body: string[] = []
  const mode = currentMode(frame, link)
  const open = link.kind === 'open'
  const mic = open ? voiceHeader(frame, listening) : null
  if (open && frame) {
    const note = frame.voice?.state === 'heard' ? frame.voice.note : null
    // a voice command that ended on a result (e.g. BLOCKED watch only) already shows that text
    const repeated = frame.ui?.mode === 'result' && frame.ui.message === note
    if (mic && note && !repeated) body.push(note)
    body.push(...screenLines(frame))
    const feed = feedLine(frame, frameAgeMs)
    if (feed) body.push(feed)
  } else {
    body.push(...linkLines(link))
  }
  const tag = headerTag(frame, link)
  const footer = link.kind !== 'open' ? FOOTERS.link : tag === 'WATCH' && mode === 'hud' ? FOOTERS.watch : FOOTERS[mode]
  const g = glyphSet(frame)
  const dot = open && frame && g.live ? (staleness(frame, frameAgeMs) ? '!' : g.live) : ''
  const lines = [mic ?? headerLine(now, tag, dot), ...body.slice(0, BODY_LINES), g.rule, footer]
  return lines.map((l) => fit(l, g.allowed)).join('\n')
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

/** Last line of defence: printable ASCII plus the `allowed` glyphs only (anything else becomes
 *  '?'), at most MAX_COLS. The default keeps the Day-2 rule: ASCII + RISK_GLYPH. */
export function fit(line: string, allowed: string = GLYPH_SETS.legacy.allowed): string {
  const ascii = Array.from(line, (ch) => ((ch >= ' ' && ch <= '~') || allowed.includes(ch) ? ch : '?')).join('')
  return ascii.length <= MAX_COLS ? ascii : ascii.slice(0, MAX_COLS - 1) + '~'
}
