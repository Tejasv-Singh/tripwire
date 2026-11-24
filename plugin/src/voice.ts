// Push-to-talk: hold (long press) to talk, release to send. Pure state + injected I/O, tested in
// voice.test.ts.
//
//   LONG_PRESS_EVENT          -> {"type":"voice","action":"start"}, open the G2 microphone
//   audioEvent (100 ms PCM)   -> sent as a BINARY WebSocket frame (16 kHz s16le mono, as is)
//   LONG_PRESS_RELEASE_EVENT  -> close the mic, {"type":"voice","action":"stop"}
//
// The backend transcribes locally (faster-whisper) and only ever ARMS an action; the two-tap
// confirm still sends. Tap-then-hold is the OS's own context-menu gesture, so a long press that
// follows a tap within TAP_GUARD_MS is ignored -- just hold, without tapping first.

export const MAX_TALK_MS = 8_000 // the backend also cuts at 8 s
export const TAP_GUARD_MS = 600

export interface PttDeps {
  openMic(): Promise<boolean>
  closeMic(): Promise<void>
  sendJson(message: object): boolean
  sendBinary(pcm: Uint8Array): boolean
  now(): number
  setTimer(fn: () => void, ms: number): unknown
  clearTimer(handle: unknown): void
  onChange(): void
  log(line: string): void
}

export class PushToTalk {
  listening = false
  private lastTapAt = Number.NEGATIVE_INFINITY
  private timer: unknown = null
  private frames = 0

  constructor(private readonly deps: PttDeps) {}

  /** Remember taps: a long press right after a tap is the OS menu gesture, not push-to-talk. */
  noteTap(): void {
    this.lastTapAt = this.deps.now()
  }

  /** Returns null when listening started, or the reason it didn't. */
  async start(): Promise<string | null> {
    if (this.listening) return null
    if (this.deps.now() - this.lastTapAt < TAP_GUARD_MS) {
      return 'tap-then-hold is the system menu: just hold to talk'
    }
    if (!this.deps.sendJson({ type: 'voice', action: 'start' })) return 'offline: voice not sent'
    this.listening = true
    this.frames = 0
    this.timer = this.deps.setTimer(() => void this.stop(), MAX_TALK_MS)
    this.deps.onChange()
    let opened = false
    try {
      opened = await this.deps.openMic()
    } catch (err) {
      this.deps.log(`mic error: ${(err as Error)?.message ?? String(err)}`)
    }
    if (!opened) {
      await this.reset()
      return 'microphone unavailable'
    }
    return null
  }

  audio(pcm: Uint8Array | undefined): void {
    if (!this.listening || !pcm || pcm.length === 0) return
    if (this.deps.sendBinary(pcm)) this.frames += 1
  }

  async stop(): Promise<void> {
    if (!this.listening) return
    this.listening = false
    this.deps.clearTimer(this.timer)
    this.timer = null
    await this.safeClose()
    this.deps.sendJson({ type: 'voice', action: 'stop' })
    this.deps.log(`voice: sent ${this.frames} audio frames (~${(this.frames / 10).toFixed(1)} s)`)
    this.deps.onChange()
  }

  /** Foreground enter / exit, mic failure: drop the utterance and make sure the mic is off. */
  async reset(): Promise<void> {
    const was = this.listening
    this.listening = false
    if (this.timer !== null) this.deps.clearTimer(this.timer)
    this.timer = null
    if (was) this.deps.sendJson({ type: 'voice', action: 'cancel' })
    await this.safeClose()
    if (was) this.deps.onChange()
  }

  private async safeClose(): Promise<void> {
    try {
      await this.deps.closeMic()
    } catch (err) {
      this.deps.log(`mic close error: ${(err as Error)?.message ?? String(err)}`)
    }
  }
}
