// Build-time configuration. Values come from plugin/.env.local (see plugin/.env.example).
// Nothing secret lives here: the plugin never holds API keys — every third-party call goes
// through the backend. The pairing code is a per-install shared secret, not an API key.

/** Backend origin without trailing slash, or '' when not configured. */
export const BACKEND_URL: string = normalizeOrigin(import.meta.env.VITE_BACKEND_URL ?? '')

/** Dev convenience only. `import.meta.env.DEV` is the literal `false` in `vite build`, so the
 *  minifier drops the right-hand side and the code never reaches a production bundle. */
export const DEV_PAIRING_CODE: string = import.meta.env.DEV
  ? (import.meta.env.VITE_PAIRING_CODE ?? '').trim()
  : ''

/** Key under which the pairing code is kept in the Even App's per-plugin storage. */
export const PAIRING_STORAGE_KEY = 'tripwire.pairingCode'

/** Every fetch to the backend carries this, so ngrok's free-tier browser warning page is skipped. */
export const NGROK_SKIP_HEADER = { 'ngrok-skip-browser-warning': 'true' } as const

export function normalizeOrigin(raw: string): string {
  return raw.trim().replace(/\/+$/, '')
}

/** https://host -> wss://host/ws?code=… (http -> ws for local dev). */
export function wsUrlFor(origin: string, pairingCode: string): string {
  const base = origin.replace(/^http(s?):\/\//i, (_m, s: string) => `ws${s}://`)
  return `${base}/ws?code=${encodeURIComponent(pairingCode)}`
}
