import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { api } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { relativeTime } from '@/lib/utils'
import type { Server, Application } from '@/types/api'

function usagePercent(used: number, capacity: number): number {
  if (capacity <= 0) return 0
  return Math.round((used / capacity) * 100)
}

function UsageBar({ percent, label }: { percent: number; label: string }) {
  const color =
    percent > 90 ? 'bg-red-500' : percent > 70 ? 'bg-yellow-500' : 'bg-green-500'
  return (
    <div className="flex items-center gap-2">
      <span className="w-10 text-xs text-muted-foreground">{label}</span>
      <div className="h-2 w-20 rounded-full bg-muted">
        <div
          className={`h-2 rounded-full ${color}`}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
      <span className="text-xs text-muted-foreground">{percent}%</span>
    </div>
  )
}

function ServerApplications({ handle }: { handle: string }) {
  const { data: apps, isLoading } = useQuery({
    queryKey: ['server-applications', handle],
    queryFn: () => api.get<Application[]>(`/servers/${handle}/applications`),
  })

  if (isLoading)
    return <p className="py-2 text-sm text-muted-foreground">Loading...</p>
  if (!apps?.length)
    return <p className="py-2 text-sm text-muted-foreground">No applications</p>

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-muted-foreground">
          <th className="px-4 py-1.5 text-left text-xs font-medium">Application</th>
          <th className="px-4 py-1.5 text-left text-xs font-medium">Ports</th>
          <th className="px-4 py-1.5 text-left text-xs font-medium">Status</th>
          <th className="px-4 py-1.5 text-left text-xs font-medium">Last Check</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border/50">
        {apps.map((app) => (
          <tr key={app.id}>
            <td className="px-4 py-1.5 font-medium">{app.service_name}</td>
            <td className="px-4 py-1.5">
              {app.ports.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {app.ports.map((p) => (
                    <span
                      key={p.id}
                      className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                      title={p.service_name}
                    >
                      <span className="text-muted-foreground">{p.service_name}:</span>
                      <span className="ml-0.5 font-semibold">{p.port}</span>
                    </span>
                  ))}
                </div>
              ) : (
                <span className="text-xs text-muted-foreground">—</span>
              )}
            </td>
            <td className="px-4 py-1.5">
              <StatusBadge status={app.status} />
            </td>
            <td className="px-4 py-1.5 text-muted-foreground">
              {app.last_health_check ? relativeTime(app.last_health_check) : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ServerRow({ server }: { server: Server }) {
  const [expanded, setExpanded] = useState(false)

  const ramPct = usagePercent(server.used_ram_mb, server.capacity_ram_mb)
  const diskPct = usagePercent(server.used_disk_mb, server.capacity_disk_mb)

  return (
    <>
      <tr
        className="cursor-pointer hover:bg-muted/30"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="px-4 py-3">
          <span className="mr-2 inline-block w-4 text-muted-foreground">
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </span>
          <span className="font-medium text-foreground">{server.handle}</span>
        </td>
        <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
          {server.public_ip}
        </td>
        <td className="px-4 py-3">
          <StatusBadge status={server.status} />
        </td>
        <td className="px-4 py-3">
          <div className="space-y-1">
            <UsageBar percent={ramPct} label="RAM" />
            <UsageBar percent={diskPct} label="Disk" />
          </div>
        </td>
        <td className="px-4 py-3 text-xs text-muted-foreground">
          {server.os_template ?? '—'}
        </td>
        <td className="px-4 py-3 text-muted-foreground">
          {server.updated_at ? relativeTime(server.updated_at) : '—'}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={6} className="bg-muted/20 px-8 py-3">
            <h4 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
              Applications
            </h4>
            <ServerApplications handle={server.handle} />
          </td>
        </tr>
      )}
    </>
  )
}

export function ServersPage() {
  const { data: servers, isLoading } = useQuery({
    queryKey: ['servers'],
    queryFn: () => api.get<Server[]>('/servers/'),
    refetchInterval: 30_000,
  })

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Servers</h1>

      {isLoading ? (
        <p className="text-muted-foreground">Loading...</p>
      ) : (servers ?? []).length === 0 ? (
        <p className="text-muted-foreground">No servers registered</p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/50">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Server
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  IP
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Resources
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  OS
                </th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                  Updated
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(servers ?? []).map((server) => (
                <ServerRow key={server.handle} server={server} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
