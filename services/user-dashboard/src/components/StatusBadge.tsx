import { cn } from '@/lib/utils'

const variants: Record<string, string> = {
  active: 'bg-success/10 text-success',
  draft: 'bg-muted text-muted-foreground',
  up: 'bg-success/10 text-success',
  down: 'bg-danger/10 text-danger',
}

export default function StatusBadge({ status }: { status: string }) {
  return (
    <span className={cn(
      'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
      variants[status] ?? 'bg-muted text-muted-foreground',
    )}>
      {status}
    </span>
  )
}
