import { cn } from '@/lib/utils'

const statusColors: Record<string, string> = {
  // Task statuses
  backlog: 'bg-zinc-700 text-zinc-200',
  todo: 'bg-blue-900 text-blue-200',
  in_dev: 'bg-yellow-900 text-yellow-200',
  in_ci: 'bg-purple-900 text-purple-200',
  testing: 'bg-cyan-900 text-cyan-200',
  done: 'bg-green-900 text-green-200',
  blocked: 'bg-red-900 text-red-200',
  waiting_human_review: 'bg-orange-900 text-orange-200',
  failed: 'bg-red-950 text-red-300',
  cancelled: 'bg-zinc-800 text-zinc-400',
  // Project statuses
  active: 'bg-green-900 text-green-200',
  scaffolding: 'bg-blue-900 text-blue-200',
  deploying: 'bg-purple-900 text-purple-200',
  // Server statuses
  provisioning: 'bg-yellow-900 text-yellow-200',
  ready: 'bg-green-900 text-green-200',
  error: 'bg-red-900 text-red-200',
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        statusColors[status] ?? 'bg-zinc-700 text-zinc-200',
      )}
    >
      {status.replace(/_/g, ' ')}
    </span>
  )
}
