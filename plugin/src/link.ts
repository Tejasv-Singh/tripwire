// WebSocket link to the backend: connect, auto-reconnect with backoff, watchdog, send intents.
// Browsers cannot set headers on a WebSocket handshake, so the ngrok skip header can't be sent
// here (it is sent on every fetch instead — see api.ts).

import {
  CLOSE_BAD_PAIRING_CODE,
  parseServerFrame,
  type HudFrame,
  type IntentAction,
  type ServerFrame,
} from './protocol'
import { wsUrlFor } from './config'
import type { LinkView } from './hud'

export interface LinkOptions {
  origin: string
  getPairingCode: () => string
  onFrame: (frame: HudFrame) => void
  onLink: (view: LinkView) => void
  onOther?: (frame: ServerFrame) => void
  log?: (msg: string) => void
}

const BACKOFF_START_MS = 1_000
const BACKOFF_MAX_MS = 15_000
/** No frame for this long (backend heartbeats every 5 s) -> assume the socket is dead. */
const WATCHDOG_MS = 15_000

export class BackendLink {
  private ws: WebSocket | null = null
  private attempt = 0
  private backoffMs = BACKOFF_START_MS
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private countdownTimer: ReturnType<typeof setInterval> | null = null
  private watchdog: ReturnType<typeof setInterval> | null = null
  private lastFrameAt = 0
  private stopped = false

  constructor(private readonly opts: LinkOptions) {}

  get lastFrameAgeMs(): number | undefined {
    return this.lastFrameAt ? Date.now() - this.lastFrameAt : undefined
  }

  /** Open the socket (no-op if already connecting/open). */
  connect(): void {
    this.stopped = false
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return
    this.clearRetry()
    const code = this.opts.getPairingCode()
    if (!this.opts.origin) return this.opts.onLink({ kind: 'unconfigured' })
    if (!code) return this.opts.onLink({ kind: 'no-code' })

    this.attempt += 1
    this.opts.onLink({ kind: 'connecting', attempt: this.attempt })
    const ws = new WebSocket(wsUrlFor(this.opts.origin, code))
    this.ws = ws

    ws.onopen = () => {
      if (ws !== this.ws) return
      this.attempt = 0
      this.backoffMs = BACKOFF_START_MS
      this.lastFrameAt = Date.now()
      this.startWatchdog()
      this.opts.log?.('ws open')
      this.opts.onLink({ kind: 'open' })
    }
    ws.onmessage = (ev: MessageEvent) => {
      if (ws !== this.ws) return
      const frame = parseServerFrame(String(ev.data))
      if (!frame) {
        this.opts.log?.(`non-JSON frame: ${String(ev.data).slice(0, 80)}`)
        return
      }
      this.lastFrameAt = Date.now()
      if (frame.type === 'hud') this.opts.onFrame(frame)
      else this.opts.onOther?.(frame)
    }
    ws.onerror = () => this.opts.log?.('ws error')
    ws.onclose = (ev: CloseEvent) => {
      if (ws !== this.ws) return // an old socket we already replaced
      this.ws = null
      this.stopWatchdog()
      this.opts.log?.(`ws closed ${ev.code}${ev.reason ? ` (${ev.reason})` : ''}`)
      if (this.stopped) return
      if (ev.code === CLOSE_BAD_PAIRING_CODE) {
        // retrying with the same code is pointless; wait for a new code / foreground enter
        this.opts.onLink({ kind: 'rejected' })
        return
      }
      this.scheduleRetry()
    }
  }

  /** Tear down any socket and connect again right away (used on FOREGROUND_ENTER / new code). */
  reconnectNow(): void {
    this.dropSocket()
    this.attempt = 0
    this.backoffMs = BACKOFF_START_MS
    this.connect()
  }

  close(): void {
    this.stopped = true
    this.clearRetry()
    this.dropSocket()
  }

  sendIntent(action: IntentAction): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify({ type: 'intent', action }))
    return true
  }

  // --- internals -------------------------------------------------------------------------------

  private dropSocket(): void {
    this.stopWatchdog()
    const ws = this.ws
    this.ws = null // detach first so its onclose is ignored
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close(1000, 'client reconnect')
  }

  private scheduleRetry(): void {
    const delay = this.backoffMs
    this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS)
    let remaining = Math.ceil(delay / 1000)
    this.opts.onLink({ kind: 'retrying', inSeconds: remaining })
    this.countdownTimer = setInterval(() => {
      remaining = Math.max(0, remaining - 1)
      this.opts.onLink({ kind: 'retrying', inSeconds: remaining })
    }, 1000)
    this.retryTimer = setTimeout(() => {
      this.clearRetry()
      this.connect()
    }, delay)
  }

  private clearRetry(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer)
    if (this.countdownTimer) clearInterval(this.countdownTimer)
    this.retryTimer = null
    this.countdownTimer = null
  }

  private startWatchdog(): void {
    this.stopWatchdog()
    this.watchdog = setInterval(() => {
      if (Date.now() - this.lastFrameAt > WATCHDOG_MS) {
        this.opts.log?.('watchdog: no frames, reconnecting')
        this.dropSocket()
        this.scheduleRetry()
      }
    }, 2_000)
  }

  private stopWatchdog(): void {
    if (this.watchdog) clearInterval(this.watchdog)
    this.watchdog = null
  }
}
