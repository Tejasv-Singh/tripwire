import { describe, expect, it } from 'vitest'
import {
  FOOTERS,
  MAX_COLS,
  MAX_LINES,
  currentMode,
  fit,
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
    expect(l).toMatch(/^[\x20-\x7E]*$/) // printable ASCII only
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
      'tap: actions  swipe: next  2x: exit',
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
    expect(lines.at(-1)).toBe('watch only  swipe: next  2x: exit')
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
    expect(stale).toContain('Link stale 20s')
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
    expect(down.split('\n').at(-1)).toBe(FOOTERS.hud)
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
