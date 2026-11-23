// Glasses/ring input -> app command. Pure; tested in input.test.ts.

import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk'
import type { IntentAction } from './protocol'

export type Command =
  | { kind: 'intent'; action: IntentAction }
  | { kind: 'exit' } // double tap -> ask the OS to close the app
  | { kind: 'foreground-enter' } // app came back to the front -> reconnect the WS
  | { kind: 'foreground-exit' }
  | { kind: 'shutdown' } // system / abnormal exit -> clean up

/**
 * Swipe mapping. The OS reports a swipe on the text container as a scroll that hit the
 * container's edge: swiping DOWN reaches the bottom (SCROLL_BOTTOM_EVENT), UP reaches the top.
 * Flip these two entries if the direction feels wrong on hardware.
 */
export const SWIPE_TO_ACTION: Partial<Record<OsEventTypeList, IntentAction>> = {
  [OsEventTypeList.SCROLL_BOTTOM_EVENT]: 'next',
  [OsEventTypeList.SCROLL_TOP_EVENT]: 'prev',
}

/**
 * CLICK_EVENT is 0 and protobuf omits zero-valued fields, so a tap arrives as an envelope whose
 * eventType is undefined. Resolve the default INSIDE the envelope check: events without that
 * envelope at all (e.g. audio frames) must not read as a tap. (Lesson from the asr template.)
 */
function eventTypeOf(envelope?: { eventType?: OsEventTypeList }): OsEventTypeList | null {
  if (!envelope) return null
  return envelope.eventType ?? OsEventTypeList.CLICK_EVENT
}

export function commandFromEvent(event: EvenHubEvent): Command | null {
  // Taps/swipes come via textEvent (our container captures events) or sysEvent (ring, lifecycle).
  const types = [eventTypeOf(event.sysEvent), eventTypeOf(event.textEvent)]
  const has = (t: OsEventTypeList) => types.includes(t)

  // Double tap first (before CLICK) and unconditionally, so the user can always exit.
  if (has(OsEventTypeList.DOUBLE_CLICK_EVENT)) return { kind: 'exit' }
  if (has(OsEventTypeList.SYSTEM_EXIT_EVENT) || has(OsEventTypeList.ABNORMAL_EXIT_EVENT)) {
    return { kind: 'shutdown' }
  }
  if (has(OsEventTypeList.FOREGROUND_ENTER_EVENT)) return { kind: 'foreground-enter' }
  if (has(OsEventTypeList.FOREGROUND_EXIT_EVENT)) return { kind: 'foreground-exit' }
  for (const t of types) {
    const action = t === null ? undefined : SWIPE_TO_ACTION[t]
    if (action) return { kind: 'intent', action }
  }
  if (has(OsEventTypeList.CLICK_EVENT)) return { kind: 'intent', action: 'actions' }
  return null
}
