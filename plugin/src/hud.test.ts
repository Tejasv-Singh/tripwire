import { describe, expect, it } from 'vitest'
import {
  FOOTERS,
  GLYPH_SETS,
  MAX_COLS,
  MAX_LINES,
  STALE_MS,
  currentMode,
  fit,
  glyphSet,
  staleness,
  fmtCompact,
  fmtLive,
  fmtPct,
  fmtQty,
  fmtSize,
  fmtUsd,
  headerLine,
  renderHud,
  wrap,
  type LinkView,
} from './hud'
import type { HudFrame, Position, UiState } from './protocol'

const NOW = new Date(2026, 10, 23, 9, 5)

const ethLong: Position = {
  coin: 'ETH', side: 'LONG', size: 0.5, entry: 3412, mark: 3494.4, upnl: 41.2,
  upnl_pct: 2.4150, roe_pct: 12.075, liq_px: 2980, liq_dist_pct: 12.66, leverage: 5, mid: 3494.4,
  mark_source: 'markPx', leverage_type: 'cross',
}

function frame(over: Partial<HudFrame> = {}): HudFrame {
  return {
    type: 'hud', v: 2, seq: 1, ts: 0, network: 'testnet',
    feed: { connected: true, age_ms: 300 }, account: false, watch: false, address: '',
    view: { index: 0, count: 0 }, position: null,
    ref: { coin: 'ETH', mid: 1721.75, mark: 1721.9, mark_source: 'markPx' },
    ui: { mode: 'hud' }, trading: { enabled: false, status: 'off' },
    ...over,
  }
}

function assertFits(text: string): void {
  const lines = text.split('\n')
  expect(lines.length).toBeLessThanOrEqual(MAX_LINES)
  for (const l of lines) {
    expect(l.length, `too long: "${l}"`).toBeLessThanOrEqual(MAX_COLS)
    expect(l).toMatch(/^[\x20-\x7E▲]*$/) // printable ASCII + the one RISK_GLYPH
  }
}

describe('renderHud', () => {
  it('renders the position mock exactly (plus the Day-2 ROE figure)', () => {
    const text = renderHud({
      frame: frame({ account: true, position: ethLong, view: { index: 0, count: 1 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(text.split('\n')).toEqual([
      'TRIPWIRE            TESTNET      09:05',
      'ETH-PERP LONG 0.50 @ 3,412  5x',
      'uPnL +$41.20 (+2.4%)  ROE +12.1%',
      'Liq 2,980  12.7% away',
      '----------------------------------------',
      'tap: menu  swipe: next  hold: talk  2x: end',
    ])
  })

  it('WATCH mode: header tag, watch-only footer, and the watched address when flat', () => {
    const watched = frame({
      account: true, watch: true, address: '0xb7bd...6422', position: ethLong,
      view: { index: 0, count: 1 },
    })
    const lines = renderHud({ frame: watched, link: { kind: 'open' }, now: NOW }).split('\n')
    expect(lines[0]).toBe('TRIPWIRE WATCH      TESTNET      09:05')
    expect(lines.at(-1)).toBe(FOOTERS.watch)
    expect(lines.at(-1)).toBe('watch only  swipe: next  hold: talk  2x: end')
    const flat = renderHud({
      frame: frame({ account: true, watch: true, address: '0xb7bd...6422' }), link: { kind: 'open' }, now: NOW,
    })
    expect(flat).toContain('Watching 0xb7bd...6422: no position')
    assertFits(flat)
    // link down: no WATCH tag (we don't know what the backend is doing)
    const down = renderHud({ frame: watched, link: { kind: 'retrying', inSeconds: 2 }, now: NOW })
    expect(down.split('\n')[0]).toBe(headerLine(NOW))
  })

  it('shows the ETH mark price when there is no position', () => {
    const text = renderHud({ frame: frame(), link: { kind: 'open' }, now: NOW })
    expect(text).toContain('ETH-PERP  1,721.9  mark')
    expect(text).toContain('No account set (price only)')
    assertFits(text)
  })

  it('labels a mid fallback and shows waiting before the first price', () => {
    const mid = renderHud({
      frame: frame({ ref: { coin: 'ETH', mid: 1721.75, mark: 1721.75, mark_source: 'mid' } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(mid).toContain('1,721.8  mid')
    const none = renderHud({
      frame: frame({ ref: { coin: 'ETH', mid: null, mark: null, mark_source: 'mid' } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(none).toContain('waiting for price')
  })

  it('shows a position counter, short side, losses, and liq edge cases', () => {
    const short: Position = {
      ...ethLong, coin: 'BTC', side: 'SHORT', size: 0.012, entry: 61280, upnl: -12.3,
      upnl_pct: -1.67, roe_pct: null, liq_px: 66000, liq_dist_pct: -0.4, leverage: 12.5,
      mark_source: 'mid',
    }
    const text = renderHud({
      frame: frame({ account: true, position: short, view: { index: 1, count: 3 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(text).toContain('BTC-PERP SHORT 0.012 @ 61,280  12.5x  2/3')
    expect(text.split('\n')).toContain('uPnL -$12.30 (-1.7%)') // no ROE when it is null
    expect(text).toContain('Liq 66,000  AT/PAST LIQ (mid)')
    assertFits(text)
    const noLiq = renderHud({
      frame: frame({ account: true, position: { ...ethLong, liq_px: null, liq_dist_pct: null },
                     view: { index: 0, count: 1 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(noLiq).toContain('Liq none')
  })

  it('reports feed and link problems', () => {
    const feedDown = renderHud({
      frame: frame({ feed: { connected: false, age_ms: 40_000 } }), link: { kind: 'open' }, now: NOW,
    })
    expect(feedDown).toContain('Hyperliquid feed reconnecting...')
    const stale = renderHud({ frame: frame(), link: { kind: 'open' }, now: NOW, frameAgeMs: 20_000 })
    expect(stale).toContain('STALE: no update for 20s - reconnecting')
    const views: LinkView[] = [
      { kind: 'unconfigured' }, { kind: 'no-code' }, { kind: 'connecting', attempt: 1 },
      { kind: 'connecting', attempt: 4 }, { kind: 'retrying', inSeconds: 8 },
      { kind: 'rejected' }, { kind: 'open' },
    ]
    for (const link of views) {
      const text = renderHud({ frame: null, link, now: NOW })
      assertFits(text)
      expect(text.split('\n')[0]).toBe(headerLine(NOW))
    }
    expect(renderHud({ frame: null, link: { kind: 'rejected' }, now: NOW })).toContain('rejected')
  })

  it('never exceeds limits even with absurd inputs', () => {
    const huge: Position = {
      ...ethLong, coin: 'SUPERLONGCOINNAME', size: 123456789.123, entry: 98765432.1,
      upnl: -987654321.99, upnl_pct: -99999.9, roe_pct: -4999995, liq_px: 123456789,
      liq_dist_pct: 9999, leverage: 50,
    }
    const text = renderHud({
      frame: frame({ account: true, position: huge, view: { index: 98, count: 99 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    assertFits(text)
  })
})

describe('stale indicator and glyph sets (Day 3)', () => {
  const pos = { account: true, position: ethLong, view: { index: 0, count: 1 } }

  it('flags no update for >3 s, and says reconnecting once the watchdog kicks in', () => {
    const fresh = frame({ ...pos, feed: { connected: true, age_ms: 0 } })
    const at = (ms: number) => renderHud({ frame: fresh, link: { kind: 'open' }, now: NOW, frameAgeMs: ms })
    expect(at(900)).not.toContain('STALE')
    expect(at(3_000)).not.toContain('STALE') // exactly 3 s is still fresh
    expect(at(3_400)).toContain('STALE: no update for 3s')
    expect(at(3_400)).not.toContain('reconnecting')
    expect(at(9_000)).toContain('STALE: no update for 9s - reconnecting')
    for (const ms of [3_400, 9_000, 600_000]) assertFits(at(ms))
  })

  it('flags old prices inside a fresh frame (feed age + frame age)', () => {
    const at = (age: number, frameAgeMs = 0) =>
      renderHud({ frame: frame({ ...pos, feed: { connected: true, age_ms: age } }), link: { kind: 'open' }, now: NOW, frameAgeMs })
    expect(at(2_500)).not.toContain('STALE')
    expect(at(2_500, 900)).toContain('STALE: prices 3s old')
    expect(at(12_000)).toContain('STALE: prices 12s old')
    // feed down: the reconnecting line, not a second STALE line
    const down = renderHud({ frame: frame({ feed: { connected: false, age_ms: 40_000 } }), link: { kind: 'open' }, now: NOW })
    expect(down).toContain('Hyperliquid feed reconnecting...')
    expect(down).not.toContain('STALE')
  })

  it('unicode: live dot, heavy rule and the RISK triangle; ! when stale', () => {
    const f = frame({ ...pos, glyphs: 'unicode' })
    const lines = renderHud({ frame: f, link: { kind: 'open' }, now: NOW }).split('\n')
    expect(lines[0]).toBe('TRIPWIRE            TESTNET ●    09:05')
    expect(lines.at(-2)).toBe(GLYPH_SETS.unicode.rule)
    expect(lines.at(-2)).toMatch(/^━+$/)
    const stale = renderHud({ frame: f, link: { kind: 'open' }, now: NOW, frameAgeMs: 5_000 }).split('\n')
    expect(stale[0]).toBe('TRIPWIRE            TESTNET !    09:05')
    const alert: UiState = {
      mode: 'menu', kind: 'alert', coin: 'ETH', items: ['Move SL'], cursor: 0, trading: true, note: null,
      alert: { coin: 'ETH', side: 'LONG', size: 0.5, liq_px: 2980, liq_dist_pct: 3.87, mark: 3100, threshold_pct: 5 },
    }
    const risk = renderHud({ frame: frame({ ...pos, glyphs: 'unicode', ui: alert }), link: { kind: 'open' }, now: NOW })
    expect(risk.split('\n')[1]).toBe('▲ RISK  ETH-PERP LONG 0.50')
    for (const l of risk.split('\n')) expect(l).toMatch(/^[\x20-\x7E▲━●]*$/)
  })

  it('ascii: the same screens with /!\\ - * only (GLYPHS=ascii)', () => {
    const f = frame({ ...pos, glyphs: 'ascii' })
    const text = renderHud({ frame: f, link: { kind: 'open' }, now: NOW })
    const lines = text.split('\n')
    expect(lines[0]).toBe('TRIPWIRE            TESTNET *    09:05')
    expect(lines.at(-2)).toBe('-'.repeat(40))
    for (const l of lines) expect(l).toMatch(/^[\x20-\x7E]*$/)
    const alert: UiState = {
      mode: 'menu', kind: 'alert', coin: 'ETH', items: ['Move SL'], cursor: 0, trading: true, note: null,
      alert: { coin: 'ETH', side: 'LONG', size: 0.5, liq_px: 2980, liq_dist_pct: 3.87, mark: 3100, threshold_pct: 5 },
    }
    const risk = renderHud({ frame: frame({ ...pos, glyphs: 'ascii', ui: alert }), link: { kind: 'open' }, now: NOW })
    expect(risk.split('\n')[1]).toBe('/!\\ RISK  ETH-PERP LONG 0.50')
    // a unicode glyph smuggled into a line is replaced in ascii mode
    expect(fit('x ● y', GLYPH_SETS.ascii.allowed)).toBe('x ? y')
  })

  it('no glyphs field (older backend): exactly the Day-2 look, no dot; link down: no dot', () => {
    expect(glyphSet(frame())).toBe(GLYPH_SETS.legacy)
    const lines = renderHud({ frame: frame(pos), link: { kind: 'open' }, now: NOW }).split('\n')
    expect(lines[0]).toBe(headerLine(NOW))
    const down = renderHud({ frame: frame({ ...pos, glyphs: 'unicode' }), link: { kind: 'retrying', inSeconds: 2 }, now: NOW })
    expect(down.split('\n')[0]).toBe(headerLine(NOW))
  })

  it('staleness() prefers the link, ignores a feed that says it is down', () => {
    expect(staleness(frame(), 0)).toBeNull()
    expect(staleness(frame(), 4_000)).toEqual({ what: 'link', s: 4 })
    expect(staleness(frame({ feed: { connected: true, age_ms: 5_000 } }))).toEqual({ what: 'prices', s: 5 })
    expect(staleness(frame({ feed: { connected: false, age_ms: 50_000 } }))).toBeNull()
    expect(staleness(frame({ feed: { connected: true, age_ms: null } }))).toBeNull()
    expect(STALE_MS).toBe(3_000)
  })
})

describe('action screens', () => {
  const pos = { account: true, position: ethLong, view: { index: 0, count: 1 } }
  const at = (ui: UiState, over: Partial<HudFrame> = {}) =>
    renderHud({ frame: frame({ ...pos, ui, ...over }), link: { kind: 'open' }, now: NOW })
  const menu: UiState = {
    mode: 'menu', coin: 'ETH', items: ['Close 25%', 'Close 50%', 'Close 100%', 'Back'],
    cursor: 1, trading: true, note: null,
  }
  const confirm: UiState = {
    mode: 'confirm', coin: 'ETH', side: 'LONG', label: 'Close 50%', fraction: 0.5, size: 0.25,
    all: false, exact: true, est_px: 3494.4, slippage_pct: 5, expires_in_ms: 4200,
  }

  it('renders the menu with a cursor, reduce-only tag and its own footer', () => {
    expect(at(menu).split('\n')).toEqual([
      'TRIPWIRE            TESTNET      09:05',
      'ACTIONS  ETH-PERP LONG 0.50  reduce-only',
      '  Close 25%',
      '> Close 50%',
      '  Close 100%',
      '  Back',
      '----------------------------------------',
      FOOTERS.menu,
    ])
  })

  it('says when trading is off, shows notes, and survives the position closing', () => {
    expect(at({ ...menu, trading: false })).toContain('Trading off: read-only (no agent key)')
    expect(at({ ...menu, note: 'Confirm expired - not sent' })).toContain('Confirm expired - not sent')
    expect(at(menu, { position: null })).toContain('ACTIONS  ETH-PERP  position closed')
  })

  it('renders the confirm screen with the exact order', () => {
    expect(at(confirm).split('\n')).toEqual([
      'TRIPWIRE            TESTNET      09:05',
      'CONFIRM  Close 50%  ETH-PERP LONG',
      'SELL 0.25 ETH  reduce-only IOC',
      'at ~3,494.4 mark, max 5% slippage',
      'Tap again within 5s to SEND.',
      '----------------------------------------',
      'tap: SEND  swipe/2x: cancel',
    ])
  })

  it('renders a forwarded draft: header, order, levels, policy verdict', () => {
    const draft: UiState = {
      mode: 'menu', kind: 'draft', coin: 'BTC', items: ['Place order', 'Discard'], cursor: 0,
      trading: true, note: null,
      draft: { coin: 'BTC', side: 'LONG', notional_usd: 100, leverage: 1, entry_px: 96500, sl_px: 95000,
               tp_px: 99000, defaults: ['notional', 'leverage'] },
      policy: { allowed: true, rule: 'open_within_limits', reason: 'within limits, has a stop-loss' },
    }
    expect(at(draft).split('\n').slice(1, 7)).toEqual([
      'FORWARDED - your instruction, not advice',
      'BTC LONG $100* 1x*  @96,500',
      'SL 95,000  TP 99,000',
      'Policy OK',
      '> Place order',
      '  Discard',
    ])
    const blocked: UiState = {
      ...draft, items: ['Discard'],
      policy: { allowed: false, rule: 'max_notional', reason: 'Notional $1,000 > max $250' },
    }
    expect(at(blocked)).toContain('BLOCKED: Notional $1,000 > max $250')
    assertFits(at(blocked))
  })

  it('renders the open-order confirm screen', () => {
    const open: UiState = {
      mode: 'confirm', action: 'open', coin: 'BTC', side: 'LONG', label: 'Place order', notional: 100,
      leverage: 1, entry: 96500, sl: 95000, tp: 99000, size: 100 / 96500, exact: false, expires_in_ms: 5000,
    }
    expect(at(open).split('\n').slice(1, 6)).toEqual([
      'CONFIRM  Place order  BTC-PERP LONG',
      'BUY ~0.001036 BTC  $100 1x  @96,500',
      'SL 95,000  TP 99,000  attached',
      'your forwarded instruction, not advice',
      'Tap again within 5s to SEND.',
    ])
    assertFits(at({ ...open, entry: null, size: null }))
  })

  it('shows new Telegram Business messages as one HUD line', () => {
    const lines = at({ mode: 'hud' }, { inbox: { count: 2, from: '@alice', preview: 'still in ETH or did you cl' } })
      .split('\n')
    expect(lines[4]).toBe('TG 2 new @alice: still in ETH or did you cl')
    expect(at({ mode: 'hud' }, { inbox: null })).not.toContain('TG ')
  })

  it('renders the reply offer after a fill', () => {
    const reply: UiState = { mode: 'reply', coin: 'ETH', to: '@alice', text: 'Trimmed half of my ETH.', source: 'template' }
    const lines = at(reply).split('\n')
    expect(lines.slice(1, 4)).toEqual(['REPLY to @alice on Telegram?', '"Trimmed half of my ETH."', 'drafted from a template'])
    expect(lines.at(-1)).toBe('tap: send reply  2x: skip')
    assertFits(at({ ...reply, text: 'x'.repeat(60), to: '@' + 'y'.repeat(40) }))
    // the fill result stays on screen above the offer
    const withResult = at({ ...reply, result_status: 'placed', result_message: 'SL set at 1650; old SL cancelled' })
    expect(withResult.split('\n').slice(1, 3)).toEqual([
      'PLACED  SL set at 1650; old SL cancelled',
      'REPLY to @alice on Telegram?',
    ])
    assertFits(withResult)
  })

  it('push-to-talk takes over the header: listening, then what was heard (+ note)', () => {
    const listening = renderHud({ frame: frame(pos), link: { kind: 'open' }, now: NOW, listening: true })
    expect(listening.split('\n')[0]).toBe('MIC  listening... release to send')
    const heard = at(
      { mode: 'hud' },
      { voice: { state: 'heard', text: 'close half', note: null } },
    ).split('\n')
    expect(heard[0]).toBe('MIC "close half"')
    expect(heard[1]).toBe('ETH-PERP LONG 0.50 @ 3,412  5x')
    const status = at({ mode: 'hud' }, { voice: { state: 'heard', text: 'status', note: '1 pos  uPnL +$41.20  DEMO' } })
    expect(status.split('\n').slice(0, 2)).toEqual(['MIC "status"', '1 pos  uPnL +$41.20  DEMO'])
    expect(at({ mode: 'hud' }, { voice: { state: 'transcribing', text: '', note: null } }).split('\n')[0]).toBe('MIC  ...')
    expect(at({ mode: 'hud' }, { voice: { state: 'error', text: '', note: 'VOICE_ENABLED=false' } }).split('\n')[0]).toBe(
      'MIC  VOICE_ENABLED=false',
    )
    // ended on a result screen with the same text (WATCH refusal): shown once, not twice
    const msg = 'Watch only - not your account, nothing sent'
    const blocked = at(
      { mode: 'result', coin: 'BTC', ok: false, status: 'blocked', message: msg, label: 'Close 50%' },
      { voice: { state: 'heard', text: 'close half', note: msg } },
    ).split('\n')
    expect(blocked.slice(0, 3)).toEqual(['MIC "close half"', 'BLOCKED  Close 50%  BTC-PERP', msg])
    expect(blocked.filter((l) => l === msg)).toHaveLength(1)
    // link down: the normal header, never a stale MIC line
    const down = renderHud({ frame: frame({ voice: { state: 'heard', text: 'x', note: null } }), link: { kind: 'retrying', inSeconds: 2 }, now: NOW })
    expect(down.split('\n')[0]).toBe(headerLine(NOW))
  })

  it('renders End session? with live numbers, and the summary with the journal result', () => {
    const t0 = new Date(2026, 10, 25, 14, 2).getTime()
    const summary = {
      started_ms: t0, ended_ms: null, duration_s: 5280, mode: 'WATCH 0x7149...7bd0 (read-only)',
      trades: 2, dry_runs: 1, realized: 12.4, unrealized: -3.1, session_pnl: 9.3, max_drawdown: -18.2,
      alerts_fired: 2, alerts_acted: 1, alerts_snoozed: 1, policy_blocks: 1,
    }
    const end = at({ mode: 'end', coin: null, summary }).split('\n')
    expect(end.slice(1, 4)).toEqual([
      'END SESSION?  14:02-now (1h28m)',
      'Trades 2  Session P&L +$9.30',
      'Max drawdown -$18.20',
    ])
    expect(end.at(-1)).toBe(FOOTERS.end)
    const done = { ...summary, ended_ms: t0 + 5280_000 }
    const lines = at({ mode: 'summary', coin: null, summary: done, posted: null, note: null }).split('\n')
    expect(lines.slice(1, 7)).toEqual([
      'SESSION 14:02-15:30 (1h28m)',
      'Trades 2 (+1 dry)  Realized +$12.40',
      'uPnL now -$3.10  Session +$9.30',
      'Max drawdown -$18.20  Blocks 1',
      'Alerts 2 fired 1 acted 1 snoozed',
      'WATCH 0x7149...7bd0 (read-only)',
    ])
    expect(lines.at(-1)).toBe('tap: post to journal  swipe: HUD  2x: exit')
    const posted = at({ mode: 'summary', coin: null, summary: done, posted: true, note: 'posted to the journal' })
    expect(posted).toContain('Posted to journal')
    const failed = at({ mode: 'summary', coin: null, summary: done, posted: false, note: 'JOURNAL_CHAT_ID not set in .env' })
    expect(failed).toContain('Not posted: JOURNAL_CHAT_ID not set in .env')
    assertFits(failed)
  })

  it('renders the RISK alert screen with its four actions', () => {
    const alert: UiState = {
      mode: 'menu', kind: 'alert', coin: 'ETH', items: ['Move SL', 'Close 50%', 'Close all', 'Snooze 5m'],
      cursor: 0, trading: true, note: null,
      alert: { coin: 'ETH', side: 'LONG', size: 0.5, liq_px: 2980, liq_dist_pct: 3.87, mark: 3100, threshold_pct: 5 },
    }
    const lines = at(alert).split('\n')
    expect(lines.slice(1, 7)).toEqual([
      '▲ RISK  ETH-PERP LONG 0.50',
      'Liq 2,980  3.9% from mark (<5%)',
      '> Move SL',
      '  Close 50%',
      '  Close all',
      '  Snooze 5m',
    ])
    expect(lines.at(-1)).toBe(FOOTERS.menu)
    assertFits(at(alert))
    expect(fit('▲ RISK • x')).toBe('▲ RISK ? x') // only the RISK glyph survives
  })

  it('renders the Move SL submenu with the current stop (or that it is loading)', () => {
    const sl: UiState = {
      mode: 'menu', kind: 'sl', coin: 'ETH', items: ['SL to breakeven', 'SL tighter 1%', 'Back'],
      cursor: 0, trading: true, note: null, sl_loaded: true, current_sl: 3300,
    }
    expect(at(sl).split('\n').slice(1, 6)).toEqual([
      'MOVE SL  ETH-PERP LONG 0.50',
      'SL now 3,300  entry 3,412  mark 3,494',
      '> SL to breakeven',
      '  SL tighter 1%',
      '  Back',
    ])
    expect(at({ ...sl, sl_loaded: false })).toContain('SL now loading...')
    expect(at({ ...sl, current_sl: null })).toContain('SL now none')
  })

  it('renders the stop-loss confirm screen', () => {
    const stop: UiState = {
      mode: 'confirm', action: 'stop', coin: 'ETH', side: 'LONG', label: 'SL to breakeven',
      old_sl: 3300, new_sl: 3412, mark: 3494.4, entry: 3412, size: 0.5, expires_in_ms: 4100,
    }
    expect(at(stop).split('\n')).toEqual([
      'TRIPWIRE            TESTNET      09:05',
      'CONFIRM  SL to breakeven  ETH-PERP LONG',
      'SL 3,300 -> 3,412  reduce-only',
      'new SL placed first, then old cancelled',
      'mark 3,494.4  entry 3,412',
      'Tap again within 5s to SEND.',
      '----------------------------------------',
      FOOTERS.confirm,
    ])
    expect(at({ ...stop, old_sl: null })).toContain('SL none -> 3,412')
    expect(at({ ...stop, mode: 'sending' })).toContain('SL 3,300 -> 3,412  reduce-only')
  })

  it('DRY RUN and DEMO are shown in the header; dry run confirms say SIGN, not SEND', () => {
    const dry = { trading: { enabled: true, status: 'ready' as const, dry_run: true, exchange: 'hyperliquid' } }
    const text = at(confirm, dry).split('\n')
    expect(text[0]).toBe('TRIPWIRE DRY RUN    TESTNET      09:05')
    expect(text).toContain('Tap again within 5s to SIGN (dry run).')
    const demo = { trading: { enabled: true, status: 'ready' as const, dry_run: false, exchange: 'fake' } }
    expect(at(menu, demo).split('\n')[0]).toBe('TRIPWIRE DEMO       TESTNET      09:05')
    // dry run flag without trading enabled (e.g. no account): no tag
    const off = { trading: { enabled: false, status: 'off' as const, dry_run: true, exchange: 'hyperliquid' } }
    expect(at(menu, off).split('\n')[0]).toBe(headerLine(NOW))
  })

  it('confirm: BUY to close a short, ALL for 100%, ~ when the size is not rounded yet', () => {
    expect(at({ ...confirm, side: 'SHORT' })).toContain('BUY 0.25 ETH  reduce-only IOC')
    expect(at({ ...confirm, all: true, size: 0.5, label: 'Close 100%' })).toContain('SELL ALL 0.5 ETH')
    expect(at({ ...confirm, exact: false, size: 0.125 })).toContain('SELL ~0.125 ETH')
  })

  it('renders sending and results, wrapping long exchange errors', () => {
    expect(at({ ...confirm, mode: 'sending' })).toContain('SENDING  Close 50%  ETH-PERP')
    const filled = at({
      mode: 'result', coin: 'ETH', ok: true, status: 'filled',
      message: 'Filled 0.25 @ 3499.1', label: 'Close 50%',
    })
    expect(filled.split('\n').slice(1, 3)).toEqual(['FILLED  Close 50%  ETH-PERP', 'Filled 0.25 @ 3499.1'])
    const long = at({
      mode: 'result', coin: 'ETH', ok: false, status: 'rejected', label: 'Close 25%',
      message: 'Order could not immediately match against any resting orders. asset=4 and more',
    })
    expect(long).toContain('NOT DONE  Close 25%  ETH-PERP')
    assertFits(long)
  })

  it('every action screen fits, even with a feed warning and long notes', () => {
    const screens: UiState[] = [
      { ...menu, trading: false, note: 'x'.repeat(80) },
      confirm,
      { ...confirm, mode: 'sending' },
      {
        mode: 'result', coin: 'SUPERLONGCOINNAME', ok: false, status: 'error', label: 'Close 100%',
        message: 'y'.repeat(300),
      },
    ]
    for (const ui of screens) {
      const text = at(ui, { feed: { connected: false, age_ms: 99_000 } })
      assertFits(text)
      expect(text.split('\n').at(-1)).toBe(FOOTERS[ui.mode]) // the footer is never pushed off
    }
  })

  it('falls back to the HUD screen (and its exit footer) when the link is down', () => {
    expect(currentMode(frame({ ui: menu }), { kind: 'open' })).toBe('menu')
    expect(currentMode(frame({ ui: menu }), { kind: 'retrying', inSeconds: 3 })).toBe('hud')
    expect(currentMode(null, { kind: 'open' })).toBe('hud')
    const down = renderHud({ frame: frame({ ui: menu }), link: { kind: 'retrying', inSeconds: 3 }, now: NOW })
    expect(down.split('\n').at(-1)).toBe(FOOTERS.link) // offline: a double tap exits
  })
})

describe('formatters', () => {
  it('formats prices with HL-style significant figures', () => {
    expect(fmtLive(1721.9)).toBe('1,721.9')
    expect(fmtLive(61280)).toBe('61,280')
    expect(fmtLive(150.23)).toBe('150.23')
    expect(fmtLive(0.12345)).toBe('0.12345')
    expect(fmtCompact(3412)).toBe('3,412')
    expect(fmtCompact(2980.4)).toBe('2,980')
    expect(fmtCompact(150.26)).toBe('150.3')
  })

  it('formats size, usd and pct', () => {
    expect(fmtSize(0.5)).toBe('0.50')
    expect(fmtSize(0.0123)).toBe('0.0123')
    expect(fmtSize(2)).toBe('2.00')
    expect(fmtSize(250)).toBe('250')
    expect(fmtUsd(41.2)).toBe('+$41.20')
    expect(fmtUsd(-1234.5)).toBe('-$1,234.50')
    expect(fmtPct(2.415)).toBe('+2.4%')
    expect(fmtPct(-0.04)).toBe('-0.0%')
  })

  it('fmtQty keeps every decimal the backend rounded to', () => {
    expect(fmtQty(0.125)).toBe('0.125')
    expect(fmtQty(0.00012345)).toBe('0.00012345')
    expect(fmtQty(1234.5)).toBe('1,234.5')
  })

  it('wrap() breaks on words and hard-splits long words', () => {
    expect(wrap('aa bb cc', 5)).toEqual(['aa bb', 'cc'])
    expect(wrap('abcdefghij', 4)).toEqual(['abcd', 'efgh', 'ij'])
    expect(wrap('  ', 4)).toEqual([])
  })

  it('fit() strips non-ASCII and truncates', () => {
    expect(fit('price · 1')).toBe('price ? 1')
    const long = 'x'.repeat(60)
    expect(fit(long)).toHaveLength(MAX_COLS)
    expect(fit(long).endsWith('~')).toBe(true)
  })
})
