import { describe, expect, it } from 'vitest'
import {
  MAX_COLS,
  MAX_LINES,
  fit,
  fmtCompact,
  fmtLive,
  fmtPct,
  fmtSize,
  fmtUsd,
  headerLine,
  renderHud,
  type LinkView,
} from './hud'
import type { HudFrame, Position } from './protocol'

const NOW = new Date(2026, 10, 23, 9, 5)

const ethLong: Position = {
  coin: 'ETH', side: 'LONG', size: 0.5, entry: 3412, mark: 3494.4, upnl: 41.2,
  upnl_pct: 2.4150, liq_px: 2980, liq_dist_pct: 12.66, leverage: 5, mid: 3494.4,
  mark_source: 'markPx', leverage_type: 'cross',
}

function frame(over: Partial<HudFrame> = {}): HudFrame {
  return {
    type: 'hud', v: 1, seq: 1, ts: 0, network: 'testnet',
    feed: { connected: true, age_ms: 300 }, account: false,
    view: { index: 0, count: 0 }, position: null,
    ref: { coin: 'ETH', mid: 1721.75, mark: 1721.9, mark_source: 'markPx' },
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
  it('renders the position mock exactly', () => {
    const text = renderHud({
      frame: frame({ account: true, position: ethLong, view: { index: 0, count: 1 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(text.split('\n')).toEqual([
      'TRIPWIRE            TESTNET      09:05',
      'ETH-PERP LONG 0.50 @ 3,412  5x',
      'uPnL +$41.20 (+2.4%)',
      'Liq 2,980  12.7% away',
      '----------------------------------------',
      'tap: actions  swipe: next  2x: exit',
    ])
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
      upnl_pct: -1.67, liq_px: 66000, liq_dist_pct: -0.4, leverage: 12.5, mark_source: 'mid',
    }
    const text = renderHud({
      frame: frame({ account: true, position: short, view: { index: 1, count: 3 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    expect(text).toContain('BTC-PERP SHORT 0.012 @ 61,280  12.5x  2/3')
    expect(text).toContain('uPnL -$12.30 (-1.7%)')
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
      upnl: -987654321.99, upnl_pct: -99999.9, liq_px: 123456789, liq_dist_pct: 9999, leverage: 50,
    }
    const text = renderHud({
      frame: frame({ account: true, position: huge, view: { index: 98, count: 99 } }),
      link: { kind: 'open' },
      now: NOW,
    })
    assertFits(text)
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

  it('fit() strips non-ASCII and truncates', () => {
    expect(fit('price · 1')).toBe('price ? 1')
    const long = 'x'.repeat(60)
    expect(fit(long)).toHaveLength(MAX_COLS)
    expect(fit(long).endsWith('~')).toBe(true)
  })
})
