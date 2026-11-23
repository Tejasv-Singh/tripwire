/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** HTTPS origin of the Tripwire backend, e.g. https://caucus-strategic-endorse.ngrok-free.dev */
  readonly VITE_BACKEND_URL?: string
  /** DEV ONLY pairing code; stripped from production builds (see src/config.ts). */
  readonly VITE_PAIRING_CODE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
