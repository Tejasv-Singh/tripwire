import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk'
import { describe, expect, it } from 'vitest'
import { commandFromEvent } from './input'

const text = (eventType?: OsEventTypeList): EvenHubEvent =>
  ({ textEvent: { containerID: 1, containerName: 'hud', eventType } }) as EvenHubEvent
const sys = (eventType?: OsEventTypeList): EvenHubEvent =>
  ({ sysEvent: { eventType } }) as EvenHubEvent

describe('commandFromEvent', () => {
  it('maps swipe down/up to next/prev', () => {
    expect(commandFromEvent(text(OsEventTypeList.SCROLL_BOTTOM_EVENT))).toEqual({ kind: 'intent', action: 'next' })
    expect(commandFromEvent(text(OsEventTypeList.SCROLL_TOP_EVENT))).toEqual({ kind: 'intent', action: 'prev' })
    expect(commandFromEvent(sys(OsEventTypeList.SCROLL_BOTTOM_EVENT))).toEqual({ kind: 'intent', action: 'next' })
  })

  it('maps a tap (eventType omitted on the wire) to the actions intent', () => {
    expect(commandFromEvent(text(undefined))).toEqual({ kind: 'intent', action: 'actions' })
    expect(commandFromEvent(sys(OsEventTypeList.CLICK_EVENT))).toEqual({ kind: 'intent', action: 'actions' })
  })

  it('double tap exits, and wins over anything else', () => {
    expect(commandFromEvent(sys(OsEventTypeList.DOUBLE_CLICK_EVENT))).toEqual({ kind: 'exit' })
    const both = { ...text(undefined), ...sys(OsEventTypeList.DOUBLE_CLICK_EVENT) } as EvenHubEvent
    expect(commandFromEvent(both)).toEqual({ kind: 'exit' })
  })

  it('maps lifecycle events', () => {
    expect(commandFromEvent(sys(OsEventTypeList.FOREGROUND_ENTER_EVENT))).toEqual({ kind: 'foreground-enter' })
    expect(commandFromEvent(sys(OsEventTypeList.FOREGROUND_EXIT_EVENT))).toEqual({ kind: 'foreground-exit' })
    expect(commandFromEvent(sys(OsEventTypeList.SYSTEM_EXIT_EVENT))).toEqual({ kind: 'shutdown' })
    expect(commandFromEvent(sys(OsEventTypeList.ABNORMAL_EXIT_EVENT))).toEqual({ kind: 'shutdown' })
  })

  it('ignores events without a sys/text envelope (audio, list) and unmapped types', () => {
    expect(commandFromEvent({ audioEvent: { audioPcm: new Uint8Array(4) } } as unknown as EvenHubEvent)).toBeNull()
    expect(commandFromEvent({} as EvenHubEvent)).toBeNull()
    expect(commandFromEvent(sys(OsEventTypeList.IMU_DATA_REPORT))).toBeNull()
  })
})
