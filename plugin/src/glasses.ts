// Glasses display: ONE page with ONE full-screen text container, created once.
// Live numbers are pushed with textContainerUpgrade (in-place text swap) — never a page rebuild.

import {
  CreateStartUpPageContainer,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk'

export const CONTAINER_ID = 1
export const CONTAINER_NAME = 'hud'
const WIDTH = 576 // full G2 canvas
const HEIGHT = 288

export class GlassesDisplay {
  private lastShown = ''
  private pending: string | null = null
  private pumping = false

  constructor(
    private readonly bridge: EvenAppBridge,
    private readonly log: (msg: string) => void = () => {},
    /** called after each text actually reached the glasses (latency acks, main.ts) */
    private readonly onRendered: (text: string) => void = () => {},
  ) {}

  /** Create the startup page. Must be called once before any other glasses UI call. */
  async init(initialText: string): Promise<boolean> {
    const hud = new TextContainerProperty({
      xPosition: 0,
      yPosition: 0,
      width: WIDTH,
      height: HEIGHT,
      borderWidth: 0,
      borderColor: 5,
      paddingLength: 4,
      containerID: CONTAINER_ID,
      containerName: CONTAINER_NAME,
      content: initialText,
      isEventCapture: 1, // exactly one container captures taps/swipes
    })
    const result = await this.bridge.createStartUpPageContainer(
      new CreateStartUpPageContainer({ containerTotalNum: 1, textObject: [hud] }),
    )
    const ok = result === StartUpPageCreateResult.success
    if (ok) this.lastShown = initialText
    else this.log(`createStartUpPageContainer failed: ${String(result)}`)
    return ok
  }

  /**
   * Show text. Latest-wins: if an upgrade is in flight, only the newest pending text is sent next
   * (the BLE render queue is slow, so we never stack writes). Identical text is skipped.
   */
  show(text: string): void {
    this.pending = text
    if (!this.pumping) void this.pump()
  }

  private async pump(): Promise<void> {
    this.pumping = true
    try {
      while (this.pending !== null) {
        const text = this.pending
        this.pending = null
        if (text === this.lastShown) continue
        const ok = await this.bridge.textContainerUpgrade(
          new TextContainerUpgrade({
            containerID: CONTAINER_ID,
            containerName: CONTAINER_NAME,
            content: text,
          }),
        )
        if (ok) {
          this.lastShown = text
          this.onRendered(text)
        } else this.log('textContainerUpgrade returned false')
      }
    } catch (err) {
      this.log(`textContainerUpgrade threw: ${(err as Error)?.message ?? String(err)}`)
    } finally {
      this.pumping = false
    }
  }
}
