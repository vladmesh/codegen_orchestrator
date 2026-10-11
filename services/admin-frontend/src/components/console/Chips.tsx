import { cn } from '@/lib/utils'

// One visual language for the three delivery forms of the kit vocabulary, used on every
// console screen: a container (product compose service), a package or library (code inside
// the product process), a platform service, and an external key the user supplied.
export type ChipKind = 'container' | 'package' | 'library' | 'platform' | 'secret'

const chipStyles: Record<ChipKind, string> = {
  container: 'border-zinc-700 bg-zinc-900 text-zinc-300',
  package: 'border-transparent bg-amber-950 text-amber-300',
  library: 'border-dashed border-amber-700 text-amber-300',
  platform: 'border-violet-500 bg-violet-950 text-violet-300',
  secret: 'border-zinc-700 text-zinc-400',
}

export function Chip({
  kind,
  children,
  onClick,
  active,
  title,
}: {
  kind: ChipKind
  children: React.ReactNode
  onClick?: () => void
  active?: boolean
  title?: string
}) {
  const className = cn(
    'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 font-mono text-[11px] leading-5',
    chipStyles[kind],
    active && 'ring-2 ring-teal-400',
    onClick && 'cursor-pointer hover:brightness-125',
  )
  if (onClick) {
    return (
      <button
        type="button"
        title={title}
        className={className}
        onClick={(event) => {
          event.stopPropagation()
          onClick()
        }}
      >
        {children}
      </button>
    )
  }
  return (
    <span className={className} title={title}>
      {children}
    </span>
  )
}

const dotStyles = {
  ok: 'bg-green-500',
  warn: 'bg-amber-500',
  down: 'bg-red-500',
  active: 'bg-teal-400',
  idle: 'bg-zinc-600',
} as const

export type DotTone = keyof typeof dotStyles

export function Dot({ tone }: { tone: DotTone }) {
  return <span className={cn('inline-block h-2 w-2 flex-none rounded-full', dotStyles[tone])} />
}
