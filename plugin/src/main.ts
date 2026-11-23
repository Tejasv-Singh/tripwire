// Tripwire plugin entry point.
//   1. waitForEvenAppBridge()
//   2. one glasses page, one full-screen text container (glasses.ts)
//   3. WebSocket to the backend (link.ts); every hud frame -> renderHud() -> textContainerUpgrade
//   4. ring/temple input -> intents (input.ts); FOREGROUND_ENTER -> reconnect

import { waitForEvenAppBridge, type EvenAppBridge } from '@evenrealities/even_hub_sdk'
import { fetchHealth } from './api'
import { BACKEND_URL, DEV_PAIRING_CODE, PAIRING_STORAGE_KEY } from './config'
import { GlassesDisplay } from './glasses'
import { renderHud, type LinkView } from './hud'
import { commandFromEvent } from './input'
import { BackendLink } from './link'
import type { HudFrame } from './protocol'
import { logLine, mountUi, setChip, setHealth, setMirror, setPairingCodeField } from './ui'

const CLOCK_TICK_MS = 10_000 // keeps HH:MM and "link stale" fresh between frames
const HEALTH_POLL_MS = 30_000

let bridge: EvenAppBridge | null = null
let display: GlassesDisplay | null = null
let link: BackendLink | null = null
let frame: HudFrame | null = null
let linkView: LinkView = { kind: 'connecting', attempt: 1 }
let pairingCode = ''

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
      logLine(`ack ${f.action} -> ${f.view.index + 1}/${f.view.count}${f.note ? ` (${f.note})` : ''}`)
    } else if (f.type === 'error') logLine(`backend error: ${f.error}`)
    else if (f.type === 'hello') logLine(`hello: protocol v${f.v}, ${f.network}`)
  },
  log: logLine,
})
link.connect()

const unsubscribe = bridge.onEvenHubEvent((event) => {
  const cmd = commandFromEvent(event)
  if (!cmd || !link) return
  switch (cmd.kind) {
    case 'intent': {
      if (cmd.action === 'actions') console.log('[tripwire] actions intent (menu not implemented yet)')
      const sent = link.sendIntent(cmd.action)
      logLine(sent ? `intent ${cmd.action}` : `intent ${cmd.action} dropped (offline)`)
      break
    }
    case 'exit':
      void bridge?.shutDownPageContainer(1) // OS shows its exit confirmation
      break
    case 'foreground-enter':
      logLine('foreground enter -> reconnect')
      link.reconnectNow()
      break
    case 'foreground-exit':
      logLine('foreground exit')
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
  return renderHud({ frame, link: linkView, now: new Date(), frameAgeMs: link?.lastFrameAgeMs })
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
