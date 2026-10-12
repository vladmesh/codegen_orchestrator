import { cn } from '@/lib/utils'

const tones = {
  teal: 'bg-teal-400',
  amber: 'bg-amber-400',
  violet: 'bg-violet-400',
} as const

/** "Working right now": a small dot with a soft expanding ring (static under reduced motion). */
export function LiveDot({ tone = 'teal', className, label }: { tone?: keyof typeof tones; className?: string; label?: string }) {
  return (
    <span className={cn('relative inline-flex h-2 w-2 flex-none', className)} role={label ? 'img' : undefined} aria-label={label}>
      <span className={cn('live-ping absolute inset-0 rounded-full', tones[tone])} />
      <span className={cn('relative inline-flex h-2 w-2 rounded-full', tones[tone])} />
    </span>
  )
}
