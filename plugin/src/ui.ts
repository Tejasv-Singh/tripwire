// Companion page shown in the Even App on the phone (and in the simulator's webview).
// Shows link/backend status, a mirror of the glasses text, a pairing-code field and a short log.

type Chip = 'connecting' | 'live' | 'warn' | 'error'

interface UiHandlers {
  onSavePairingCode: (code: string) => void
}

let el: {
  chip: HTMLDivElement
  backend: HTMLDivElement
  health: HTMLDivElement
  mirror: HTMLPreElement
  code: HTMLInputElement
  log: HTMLOListElement
}

const MAX_LOG = 30

export function mountUi(backendUrl: string, handlers: UiHandlers): void {
  const app = document.querySelector<HTMLDivElement>('#app')!
  app.innerHTML = `
    <main class="panel">
      <header>
        <h1>Tripwire</h1>
        <div id="chip" class="chip chip-connecting">Starting...</div>
      </header>
      <section class="card">
        <div class="row"><span class="k">Network</span><span class="v testnet">TESTNET ONLY</span></div>
        <div class="row"><span class="k">Backend</span><span class="v" id="backend"></span></div>
        <div class="row"><span class="k">Health</span><span class="v" id="health">-</span></div>
      </section>
      <section class="card">
        <div class="k">Glasses display</div>
        <pre id="mirror" class="mirror" aria-live="polite"></pre>
      </section>
      <section class="card">
        <label class="k" for="code">Pairing code (from the backend's .env)</label>
        <div class="pair">
          <input id="code" type="password" autocomplete="off" spellcheck="false" placeholder="PAIRING_CODE" />
          <button id="save" type="button">Save &amp; connect</button>
        </div>
      </section>
      <section class="card">
        <div class="k">Log</div>
        <ol id="log" class="log"></ol>
      </section>
      <footer>Tap: actions · swipe: next/prev · double-tap: exit. Not financial advice.</footer>
    </main>`
  el = {
    chip: app.querySelector('#chip')!,
    backend: app.querySelector('#backend')!,
    health: app.querySelector('#health')!,
    mirror: app.querySelector('#mirror')!,
    code: app.querySelector('#code')!,
    log: app.querySelector('#log')!,
  }
  el.backend.textContent = backendUrl || '(VITE_BACKEND_URL not set)'
  app.querySelector<HTMLButtonElement>('#save')!.addEventListener('click', () => {
    const code = el.code.value.trim()
    if (code) handlers.onSavePairingCode(code)
  })
  injectStyles()
}

export function setChip(kind: Chip, text: string): void {
  if (!el) return
  el.chip.className = `chip chip-${kind}`
  el.chip.textContent = text
}

export function setHealth(text: string, ok: boolean): void {
  if (!el) return
  el.health.textContent = text
  el.health.classList.toggle('bad', !ok)
}

export function setMirror(text: string): void {
  if (el) el.mirror.textContent = text
}

export function setPairingCodeField(code: string): void {
  if (el) el.code.value = code
}

export function logLine(msg: string): void {
  console.log(`[tripwire] ${msg}`)
  if (!el) return
  const li = document.createElement('li')
  li.textContent = `${new Date().toLocaleTimeString()}  ${msg}`
  el.log.prepend(li)
  while (el.log.children.length > MAX_LOG) el.log.lastElementChild?.remove()
}

function injectStyles(): void {
  // Even Realities dark surfaces #232323/#2E2E2E/#3E3E3E; OS green #3CFA44; signal red #FF453A.
  const css = `
    :root { color-scheme: dark; }
    html, body { margin: 0; min-height: 100%; background: #232323; color: #E5E5E5;
      font: 15px/1.4 -apple-system, BlinkMacSystemFont, 'Helvetica Neue', system-ui, sans-serif;
      touch-action: manipulation; -webkit-text-size-adjust: 100%; overscroll-behavior: none; }
    .panel { display: flex; flex-direction: column; gap: 12px; max-width: 640px; margin: 0 auto;
      padding: 16px; box-sizing: border-box; }
    header { display: flex; align-items: center; justify-content: space-between; }
    h1 { font-size: 18px; font-weight: 600; margin: 0; letter-spacing: 0.04em; }
    .card { background: #2E2E2E; border: 1px solid #3E3E3E; border-radius: 12px; padding: 12px; }
    .row { display: flex; justify-content: space-between; gap: 12px; padding: 2px 0; }
    .k { color: #919191; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
    .v { text-align: right; word-break: break-all; }
    .v.bad { color: #FF453A; }
    .testnet { color: #3CFA44; font-weight: 600; }
    .chip { font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid #3E3E3E;
      text-transform: uppercase; letter-spacing: 0.04em; }
    .chip-connecting { color: #A7A7A7; }
    .chip-live { color: #3CFA44; border-color: #3CFA44; background: rgba(60,250,68,0.08); }
    .chip-warn { color: #E5E5E5; border-color: #7B7B7B; }
    .chip-error { color: #FF453A; border-color: #FF453A; background: rgba(255,69,58,0.08); }
    .mirror { margin: 8px 0 0; padding: 10px; background: #000; color: #3CFA44; border-radius: 8px;
      font: 13px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre;
      overflow-x: auto; min-height: 9em; }
    .pair { display: flex; gap: 8px; margin-top: 8px; }
    .pair input { flex: 1; min-width: 0; padding: 8px 10px; border-radius: 8px; border: 1px solid #3E3E3E;
      background: #232323; color: #E5E5E5; font: inherit; }
    .pair button { padding: 8px 12px; border-radius: 8px; border: 1px solid #3CFA44; background: transparent;
      color: #3CFA44; font: inherit; }
    .log { margin: 8px 0 0; padding-left: 0; list-style: none; font: 12px/1.4 ui-monospace, Menlo, Consolas, monospace;
      color: #A7A7A7; max-height: 220px; overflow-y: auto; }
    footer { font-size: 12px; color: #7B7B7B; text-align: center; }
  `
  const style = document.createElement('style')
  style.textContent = css
  document.head.appendChild(style)
}
