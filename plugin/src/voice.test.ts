import { describe, expect, it } from 'vitest'
import { MAX_TALK_MS, PushToTalk, TAP_GUARD_MS, type PttDeps } from './voice'

function harness(opts: { micOk?: boolean; online?: boolean } = {}) {
  const sent: object[] = []
  const bins: Uint8Array[] = []
  const mic: string[] = []
  const timers: { fn: () => void; ms: number }[] = []
  let now = 10_000
  const deps: PttDeps = {
    openMic: async () => {
      mic.push('open')
      return opts.micOk ?? true
    },
    closeMic: async () => {
      mic.push('close')
    },
    sendJson: (m) => (opts.online ?? true) && sent.push(m) > 0,
    sendBinary: (b) => bins.push(b) > 0,
    now: () => now,
    setTimer: (fn, ms) => timers.push({ fn, ms }),
    clearTimer: () => undefined,
    onChange: () => undefined,
    log: () => undefined,
  }
  return { ptt: new PushToTalk(deps), sent, bins, mic, timers, tick: (ms: number) => (now += ms) }
}

describe('PushToTalk', () => {
  it('hold -> start + mic on; audio streams; release -> mic off + stop', async () => {
    const h = harness()
    expect(await h.ptt.start()).toBeNull()
    expect(h.ptt.listening).toBe(true)
    h.ptt.audio(new Uint8Array(3200))
    h.ptt.audio(new Uint8Array(3200))
    await h.ptt.stop()
    expect(h.sent).toEqual([{ type: 'voice', action: 'start' }, { type: 'voice', action: 'stop' }])
    expect(h.bins).toHaveLength(2)
    expect(h.mic).toEqual(['open', 'close'])
    expect(h.ptt.listening).toBe(false)
  })

  it('ignores audio when not held, and a long press right after a tap (the OS menu gesture)', async () => {
    const h = harness()
    h.ptt.audio(new Uint8Array(3200))
    expect(h.bins).toHaveLength(0)
    h.ptt.noteTap()
    h.tick(TAP_GUARD_MS - 100)
    expect(await h.ptt.start()).toMatch(/system menu/)
    expect(h.sent).toEqual([])
    h.tick(200)
    expect(await h.ptt.start()).toBeNull()
  })

  it('auto-stops after MAX_TALK_MS', async () => {
    const h = harness()
    await h.ptt.start()
    expect(h.timers[0].ms).toBe(MAX_TALK_MS)
    h.timers[0].fn()
    await Promise.resolve()
    await Promise.resolve()
    expect(h.ptt.listening).toBe(false)
    expect(h.sent.at(-1)).toEqual({ type: 'voice', action: 'stop' })
  })

  it('offline or no microphone: nothing is left half-open', async () => {
    const offline = harness({ online: false })
    expect(await offline.ptt.start()).toMatch(/offline/)
    expect(offline.mic).toEqual([])
    const noMic = harness({ micOk: false })
    expect(await noMic.ptt.start()).toBe('microphone unavailable')
    expect(noMic.ptt.listening).toBe(false)
    expect(noMic.sent.at(-1)).toEqual({ type: 'voice', action: 'cancel' })
    expect(noMic.mic).toEqual(['open', 'close'])
  })

  it('reset (foreground enter/exit) cancels a held utterance and always closes the mic', async () => {
    const h = harness()
    await h.ptt.start()
    await h.ptt.reset()
    expect(h.sent.at(-1)).toEqual({ type: 'voice', action: 'cancel' })
    await h.ptt.reset() // idle: no message, but the mic is still forced closed
    expect(h.sent.filter((m) => (m as { action: string }).action === 'cancel')).toHaveLength(1)
    expect(h.mic.filter((x) => x === 'close')).toHaveLength(2)
  })
})
