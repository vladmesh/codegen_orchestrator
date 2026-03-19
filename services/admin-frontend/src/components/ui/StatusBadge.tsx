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
  discovered: 'bg-zinc-700 text-zinc-200',
  new: 'bg-blue-900 text-blue-200',
  pending_setup: 'bg-blue-900 text-blue-200',
  provisioning: 'bg-yellow-900 text-yellow-200',
  force_rebuild: 'bg-orange-900 text-orange-200',
  ready: 'bg-green-900 text-green-200',
  in_use: 'bg-green-900 text-green-200',
  unreachable: 'bg-red-900 text-red-200',
  maintenance: 'bg-yellow-900 text-yellow-200',
  error: 'bg-red-900 text-red-200',
  // Application statuses
  not_deployed: 'bg-zinc-700 text-zinc-200',
  degraded: 'bg-orange-900 text-orange-200',
  down: 'bg-red-950 text-red-300',
  // Deployment statuses
  pending: 'bg-blue-900 text-blue-200',
  stopped: 'bg-zinc-700 text-zinc-200',
  // Worker statuses
  running: 'bg-green-900 text-green-200',
  paused: 'bg-yellow-900 text-yellow-200',
  gone: 'bg-red-900 text-red-200',
  unknown: 'bg-zinc-700 text-zinc-200',
  // Incident statuses
  detected: 'bg-red-900 text-red-200',
  recovering: 'bg-yellow-900 text-yellow-200',
  resolved: 'bg-green-900 text-green-200',
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
