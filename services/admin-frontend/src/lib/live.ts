/**
 * Demo-build liveness: short polling, a flash on whatever changed, fade-in for new items.
 * Everything is a no-op outside the demo build (`VITE_DEMO=1`), so production keeps its
 * intervals and static rendering.
 */
import { useEffect, useRef } from 'react'
import { isDemo } from './utils'

/** Poll period on every scene-1 screen of the demo build. */
export const DEMO_POLL_MS = 1_500

/** `refetchInterval` for a screen: the short demo period in the demo build, `ms` otherwise. */
export function livePoll(ms: number | false): number | false {
  return isDemo ? DEMO_POLL_MS : ms
}

export function reducedMotion(): boolean {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
}

/** Class that fades/slides a newly mounted element in (demo build only). */
export const enterClass = isDemo ? 'demo-enter' : ''

export type FlashTone = 'teal' | 'red' | 'amber'

const FLASH_RGB: Record<FlashTone, string> = {
  teal: '45, 212, 191',
  red: '248, 113, 113',
  amber: '251, 191, 36',
}

/** Brief outline glow on an element; outline + filter only, so no layout shift and no clash with own shadows. */
export function flashElement(el: Element, tone: FlashTone = 'teal'): void {
  if (!isDemo || reducedMotion() || typeof el.animate !== 'function') return
  const rgb = FLASH_RGB[tone]
  el.animate(
    [
      { outline: `2px solid rgba(${rgb}, 0.95)`, outlineOffset: '1px', filter: 'brightness(1.45)' },
      { outline: `2px solid rgba(${rgb}, 0.55)`, outlineOffset: '1px', filter: 'brightness(1.2)', offset: 0.35 },
      { outline: `2px solid rgba(${rgb}, 0)`, outlineOffset: '1px', filter: 'brightness(1)' },
    ],
    { duration: 1_300, easing: 'ease-out' },
  )
}

/**
 * Ref that flashes its element whenever `signature` changes after the first render.
 * Identical data → identical signature → nothing happens, so a poll with no news is silent.
 */
export function useFlash<T extends Element>(signature: string, tone: FlashTone = 'teal') {
  const ref = useRef<T>(null)
  const previous = useRef(signature)
  useEffect(() => {
    if (previous.current === signature) return
    previous.current = signature
    if (ref.current) flashElement(ref.current, tone)
  }, [signature, tone])
  return ref
}
