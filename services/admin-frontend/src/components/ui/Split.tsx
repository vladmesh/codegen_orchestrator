import { useSyncExternalStore, type ReactNode } from 'react'
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels'
import { cn } from '@/lib/utils'

const WIDE = '(min-width: 1024px)'

function subscribe(cb: () => void) {
  const mq = window.matchMedia(WIDE)
  mq.addEventListener('change', cb)
  return () => mq.removeEventListener('change', cb)
}

/** True on screens wide enough for side-by-side panels. */
function useWide(): boolean {
  return useSyncExternalStore(subscribe, () => window.matchMedia(WIDE).matches, () => true)
}

/**
 * Two panels side by side with an IDE-like drag handle between them. The layout is remembered
 * per `id` (localStorage); a double click on the handle restores the default split.
 * Narrow screens get the two panels stacked, without a handle.
 */
export function Split({
  id,
  left,
  right,
  defaultLeft = 60,
  minLeft = 25,
  minRight = 20,
  className,
  stackedClassName,
}: {
  id: string
  left: ReactNode
  right: ReactNode
  /** Initial width of the left panel, percent. */
  defaultLeft?: number
  minLeft?: number
  minRight?: number
  className?: string
  stackedClassName?: string
}) {
  const wide = useWide()
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: `brimos-split-${id}`, storage: localStorage })
  if (!wide) {
    return (
      <div className={cn('grid gap-4', stackedClassName)}>
        <div className="min-w-0">{left}</div>
        <div className="min-w-0">{right}</div>
      </div>
    )
  }
  return (
    <Group
      id={id}
      orientation="horizontal"
      defaultLayout={defaultLayout}
      onLayoutChanged={onLayoutChanged}
      className={cn('min-w-0', className)}
    >
      <Panel id={`${id}-a`} defaultSize={`${defaultLeft}%`} minSize={`${minLeft}%`} className="min-w-0">
        {left}
      </Panel>
      <ResizeHandle />
      <Panel id={`${id}-b`} defaultSize={`${100 - defaultLeft}%`} minSize={`${minRight}%`} className="min-w-0">
        {right}
      </Panel>
    </Group>
  )
}

/** The drag handle: a thin rule that lights up teal on hover and while dragging. */
export function ResizeHandle({ className }: { className?: string }) {
  return (
    <Separator
      className={cn(
        'group relative w-4 shrink-0 cursor-col-resize outline-none',
        'before:absolute before:inset-y-2 before:left-1/2 before:w-px before:-translate-x-1/2 before:bg-zinc-800 before:transition-colors',
        'hover:before:w-0.5 hover:before:bg-teal-400/70 data-[separator=active]:before:w-0.5 data-[separator=active]:before:bg-teal-400',
        'focus-visible:before:bg-teal-400',
        className,
      )}
    >
      <span
        aria-hidden
        className="absolute left-1/2 top-1/2 h-8 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-zinc-700 bg-zinc-900 transition-colors group-hover:border-teal-400/70"
      />
    </Separator>
  )
}
