// Tripwire plugin entry point.
//   1. waitForEvenAppBridge()
//   2. one glasses page, one full-screen text container (glasses.ts)
//   3. WebSocket to the backend (link.ts); every hud frame -> renderHud() -> textContainerUpgrade
//   4. ring/temple input -> intents (input.ts); FOREGROUND_ENTER -> reconnect
//   5. tap opens the actions menu; the backend runs the menu / two-tap confirm (protocol v2) and
//      every hud frame carries `ui` (which screen to draw) — the plugin never decides to trade
//   6. long press = push-to-talk (voice.ts): mic PCM streams to the backend as binary frames

import { AudioInputSource, waitForEvenAppBridge, type EvenAppBridge } from '@evenrealities/even_hub_sdk'
import { fetchHealth } from './api'
import { BACKEND_URL, DEV_PAIRING_CODE, PAIRING_STORAGE_KEY } from './config'
import { GlassesDisplay } from './glasses'
import { currentMode, renderHud, type LinkView } from './hud'
import { commandFromEvent } from './input'
import { BackendLink } from './link'
import { PROTOCOL_VERSION, type HudFrame } from './protocol'
import { logLine, mountUi, setChip, setHealth, setMirror, setPairingCodeField } from './ui'
import { PushToTalk } from './voice'

const CLOCK_TICK_MS = 10_000 // keeps HH:MM and "link stale" fresh between frames
const HEALTH_POLL_MS = 30_000

let bridge: EvenAppBridge | null = null
let display: GlassesDisplay | null = null
let link: BackendLink | null = null
let frame: HudFrame | null = null
let linkView: LinkView = { kind: 'connecting', attempt: 1 }
let pairingCode = ''
let ptt: PushToTalk | null = null // created after the link; read by render() before that

mountUi(BACKEND_URL, { onSavePairingCode: savePairingCode })

bridge = await waitForEvenAppBridge()
pairingCode = await loadPairingCode(bridge)
setPairingCodeField(pairingCode)

display = new GlassesDisplay(bridge, logLine)
await display.init(currentText())

link = new BackendLink({
  origin: BACKEND_URL,
  getPairingCode: () => pairingCode,
  onFrame: (f) => {
    frame = f
    render()
  },
  onLink: (view) => {
    linkView = view
    updateChip()
    render()
  },
  onOther: (f) => {
    if (f.type === 'ack') {
      const where = f.view.count ? `${f.view.index + 1}/${f.view.count}` : 'no positions'
      logLine(`ack ${f.action} -> ${f.mode}, ${where}${f.note ? ` (${f.note})` : ''}`)
    } else if (f.type === 'order') {
      const pct = f.fraction != null ? ` ${Math.round(f.fraction * 100)}%` : ''
      logLine(`ORDER ${f.kind ?? 'close'} ${f.coin}${pct}: ${f.status} - ${f.message}`)
    } else if (f.type === 'reply') {
      logLine(f.ok ? `reply sent to ${f.to}` : `reply to ${f.to} FAILED`)
    } else if (f.type === 'voice') {
      if (f.state !== 'listening') {
        logLine(`voice ${f.state}: "${f.text ?? ''}" -> ${f.command ?? 'no command'}${f.note ? ` (${f.note})` : ''}`)
      }
    } else if (f.type === 'error') logLine(`backend error: ${f.error}`)
    else if (f.type === 'hello') {
      const mismatch = f.v !== PROTOCOL_VERSION ? ` (plugin speaks v${PROTOCOL_VERSION}!)` : ''
      logLine(`hello: protocol v${f.v}${mismatch}, ${f.network}, trading ${f.trading?.status ?? '?'}`)
    }
  },
  log: logLine,
})
link.connect()

ptt = new PushToTalk({
  openMic: () => bridge?.audioControl(true, AudioInputSource.Glasses) ?? Promise.resolve(false),
  closeMic: async () => {
    await bridge?.audioControl(false)
  },
  sendJson: (m) => link?.sendJson(m) ?? false,
  sendBinary: (b) => link?.sendBinary(b) ?? false,
  now: () => Date.now(),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  onChange: () => render(),
  log: logLine,
})

const unsubscribe = bridge.onEvenHubEvent((event) => {
  if (event.audioEvent) {
    ptt?.audio(event.audioEvent.audioPcm) // only forwarded while push-to-talk is held
    return
  }
  // the backend's screen decides what a double tap means (exit on the HUD, back elsewhere);
  // with the link down it is always the HUD, so the user can always leave
  const cmd = commandFromEvent(event, currentMode(frame, linkView))
  if (!cmd || !link) return
  switch (cmd.kind) {
    case 'voice-start':
      void ptt?.start().then((reason) => reason && logLine(`voice: ${reason}`))
      break
    case 'voice-stop':
      void ptt?.stop()
      break
    case 'intent': {
      if (cmd.action === 'tap') ptt?.noteTap()
      const sent = link.sendIntent(cmd.action)
      logLine(sent ? `intent ${cmd.action}` : `intent ${cmd.action} dropped (offline)`)
      break
    }
    case 'exit':
      void bridge?.shutDownPageContainer(1) // OS shows its exit confirmation
      break
    case 'foreground-enter':
      logLine('foreground enter -> reconnect + re-arm mic')
      link.reconnectNow()
      void ptt?.reset() // mic closed and ready for the next push-to-talk
      break
    case 'foreground-exit':
      logLine('foreground exit -> mic off')
      void ptt?.reset()
      break
    case 'shutdown':
      cleanup()
      break
  }
})

setInterval(render, CLOCK_TICK_MS)
void pollHealth()
setInterval(() => void pollHealth(), HEALTH_POLL_MS)

let cleanedUp = false
function cleanup(): void {
  if (cleanedUp) return
  cleanedUp = true
  link?.close()
  unsubscribe()
}
window.addEventListener('beforeunload', cleanup)

// --- helpers -------------------------------------------------------------------------------------

function currentText(): string {
  return renderHud({
    frame,
    link: linkView,
    now: new Date(),
    frameAgeMs: link?.lastFrameAgeMs,
    listening: ptt?.listening ?? false,
  })
}

function render(): void {
  const text = currentText()
  setMirror(text)
  display?.show(text)
}

function updateChip(): void {
  switch (linkView.kind) {
    case 'open':
      return setChip('live', 'Live')
    case 'connecting':
      return setChip('connecting', 'Connecting')
    case 'retrying':
      return setChip('warn', `Retry ${linkView.inSeconds}s`)
    case 'rejected':
      return setChip('error', 'Bad pairing code')
    case 'no-code':
      return setChip('warn', 'Not paired')
    case 'unconfigured':
      return setChip('error', 'No backend URL')
  }
}

async function savePairingCode(code: string): Promise<void> {
  pairingCode = code
  if (!bridge || !link) return // bridge not ready yet; the code is used once it is
  try {
    await bridge.setLocalStorage(PAIRING_STORAGE_KEY, code)
    logLine('pairing code saved')
  } catch (err) {
    logLine(`could not save pairing code: ${(err as Error)?.message ?? String(err)}`)
  }
  link.reconnectNow()
}

async function loadPairingCode(b: EvenAppBridge): Promise<string> {
  try {
    const stored = ((await b.getLocalStorage(PAIRING_STORAGE_KEY)) ?? '').trim()
    if (stored) return stored
  } catch (err) {
    logLine(`getLocalStorage failed: ${(err as Error)?.message ?? String(err)}`)
  }
  return DEV_PAIRING_CODE
}

async function pollHealth(): Promise<void> {
  if (!BACKEND_URL) return
  try {
    const h = await fetchHealth()
    const hl = h.hyperliquid.connected ? 'HL live' : 'HL down'
    setHealth(`${hl} · telegram ${h.telegram.status} · ${h.ws_clients} client(s)`, h.hyperliquid.connected)
  } catch (err) {
    setHealth(`unreachable: ${(err as Error)?.message ?? String(err)}`, false)
  }
}
