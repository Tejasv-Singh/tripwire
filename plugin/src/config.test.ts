import { describe, expect, it } from 'vitest'
import { normalizeOrigin, wsUrlFor } from './config'
import { parseServerFrame } from './protocol'

describe('config', () => {
  it('derives the WebSocket URL from the backend origin', () => {
    expect(wsUrlFor('https://caucus-strategic-endorse.ngrok-free.dev', 'abc'))
      .toBe('wss://caucus-strategic-endorse.ngrok-free.dev/ws?code=abc')
    expect(wsUrlFor('http://127.0.0.1:8000', 'a b&c')).toBe('ws://127.0.0.1:8000/ws?code=a%20b%26c')
  })

  it('normalises origins', () => {
    expect(normalizeOrigin('  https://x.dev/// ')).toBe('https://x.dev')
  })
})

describe('parseServerFrame', () => {
  it('accepts typed JSON and rejects HTML (e.g. an ngrok warning page)', () => {
    expect(parseServerFrame('{"type":"hello","v":1}')?.type).toBe('hello')
    expect(parseServerFrame('<!DOCTYPE html><html>ngrok</html>')).toBeNull()
    expect(parseServerFrame('{"no":"type"}')).toBeNull()
  })
})
