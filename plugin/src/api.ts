import { BACKEND_URL, NGROK_SKIP_HEADER } from './config'

/** fetch() against the backend. Always adds `ngrok-skip-browser-warning: true`; use this for
 *  every HTTP call from the plugin (never call fetch() with the backend URL directly). */
export function backendFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  for (const [k, v] of Object.entries(NGROK_SKIP_HEADER)) headers.set(k, v)
  return fetch(`${BACKEND_URL}${path}`, { ...init, headers })
}

export interface Health {
  ok: boolean
  network: string
  hyperliquid: { connected: boolean; last_msg_age_ms: number | null; account_configured: boolean }
  telegram: { status: string; detail: string }
  ws_clients: number
}

export async function fetchHealth(timeoutMs = 5000): Promise<Health> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await backendFetch('/healthz', { signal: ctrl.signal })
    const type = res.headers.get('content-type') ?? ''
    if (!type.includes('application/json')) {
      // ngrok's interstitial is HTML; surfacing this makes the failure obvious
      throw new Error(`expected JSON from /healthz, got ${type || 'no content-type'} (${res.status})`)
    }
    return (await res.json()) as Health
  } finally {
    clearTimeout(timer)
  }
}
