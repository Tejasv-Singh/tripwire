import { defineConfig } from 'vitest/config'

export default defineConfig({
  // strictPort: the simulator, CORS_ORIGINS and the README all assume 5173 — fail loudly instead
  // of silently moving to 5174.
  server: { host: true, port: 5173, strictPort: true },
  build: { target: 'esnext' },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
})
