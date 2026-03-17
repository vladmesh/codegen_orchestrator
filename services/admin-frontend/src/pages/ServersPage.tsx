import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronRight,
  Activity,
  Container,
  BarChart3,
  AlertTriangle,
} from 'lucide-react'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts'
import { api } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { relativeTime, formatBytes, formatUptime, freshnessColor } from '@/lib/utils'
import type {
  Server,
  Application,
  ApplicationHealthEntry,
  MetricsHistoryEntry,
  Incident,
} from '@/types/api'

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

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

function MetricCard({
  label,
  value,
  sub,
}: {
  label: string
  value: string | number
  sub?: string
}) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-lg font-semibold text-foreground">{value}</p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab: Overview
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Application health helpers
// ---------------------------------------------------------------------------

function sslStatusColor(expiresAt: string | null): string {
  if (!expiresAt) return 'text-muted-foreground'
  const daysLeft = (new Date(expiresAt).getTime() - Date.now()) / 86_400_000
  if (daysLeft <= 7) return 'text-red-400'
  if (daysLeft <= 30) return 'text-yellow-400'
  return 'text-green-400'
}

function sslStatusText(expiresAt: string | null): string {
  if (!expiresAt) return '—'
  const daysLeft = Math.floor((new Date(expiresAt).getTime() - Date.now()) / 86_400_000)
  if (daysLeft < 0) return 'Expired'
  return `${daysLeft}d left`
}

function uptimeColor(pct: number | null): string {
  if (pct == null) return 'text-muted-foreground'
  if (pct >= 99) return 'text-green-400'
  if (pct >= 95) return 'text-yellow-400'
  return 'text-red-400'
}

function healthDotColor(status: string): string {
  switch (status) {
    case 'running':
      return 'bg-green-500'
    case 'degraded':
      return 'bg-yellow-500'
    case 'down':
      return 'bg-red-500'
    case 'stopped':
      return 'bg-zinc-500'
    default:
      return 'bg-zinc-700'
  }
}

// ---------------------------------------------------------------------------
// Application detail panel (expanded row)
// ---------------------------------------------------------------------------

const RESPONSE_TIME_COLOR = '#f59e0b'

function ApplicationDetail({ app: application }: { app: Application }) {
  const [hours, setHours] = useState(1)

  const { data: history, isLoading } = useQuery({
    queryKey: ['app-health-history', application.id, hours],
    queryFn: () =>
      api.get<ApplicationHealthEntry[]>(
        `/applications/${application.id}/health-history?hours=${hours}`,
      ),
    refetchInterval: 60_000,
  })

  const chartData = (history ?? [])
    .slice()
    .reverse()
    .map((entry) => ({
      time: entry.recorded_at,
      responseTime: entry.metrics.response_time_ms ?? null,
    }))

  return (
    <div className="space-y-4 py-2">
      {/* Overview cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard
          label="Response Time"
          value={application.response_time_ms != null ? `${application.response_time_ms}ms` : '—'}
        />
        <MetricCard
          label="Uptime (24h)"
          value={application.uptime_pct_24h != null ? `${application.uptime_pct_24h.toFixed(1)}%` : '—'}
        />
        <MetricCard
          label="SSL Certificate"
          value={sslStatusText(application.ssl_expires_at)}
        />
        <MetricCard
          label="Last Check"
          value={application.last_health_check ? relativeTime(application.last_health_check) : '—'}
        />
      </div>

      {/* Response time chart */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h5 className="text-xs font-semibold uppercase text-muted-foreground">
            Response Time
          </h5>
          <div className="flex gap-2">
            {[1, 24].map((h) => (
              <button
                key={h}
                onClick={() => setHours(h)}
                className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                  hours === h
                    ? 'bg-foreground text-background'
                    : 'bg-muted text-muted-foreground hover:bg-muted/80'
                }`}
              >
                {h}h
              </button>
            ))}
          </div>
        </div>

        {isLoading ? (
          <p className="py-4 text-sm text-muted-foreground">Loading...</p>
        ) : chartData.length === 0 ? (
          <p className="py-4 text-sm text-muted-foreground">
            No health history available — health prober has not reported yet.
          </p>
        ) : (
          <div className="rounded-lg border border-border bg-card p-4">
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                  <XAxis
                    dataKey="time"
                    tickFormatter={(v: string) => formatChartTime(v, hours)}
                    tick={{ fill: '#71717a', fontSize: 10 }}
                    stroke="#27272a"
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tick={{ fill: '#71717a', fontSize: 10 }}
                    stroke="#27272a"
                    tickFormatter={(v: number) => `${v}ms`}
                    width={50}
                  />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    labelFormatter={(v) => formatChartTime(String(v), hours)}
                    formatter={(value) => [`${Number(value)}ms`, 'Response Time']}
                  />
                  <defs>
                    <linearGradient id="fill-responseTime" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={RESPONSE_TIME_COLOR} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={RESPONSE_TIME_COLOR} stopOpacity={0.05} />
                    </linearGradient>
                  </defs>
                  <Area
                    type="monotone"
                    dataKey="responseTime"
                    stroke={RESPONSE_TIME_COLOR}
                    fill="url(#fill-responseTime)"
                    strokeWidth={1.5}
                    dot={false}
                    connectNulls
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Applications table (with health columns + expandable rows)
// ---------------------------------------------------------------------------

function ServerApplications({ handle }: { handle: string }) {
  const { data: apps, isLoading } = useQuery({
    queryKey: ['server-applications', handle],
    queryFn: () => api.get<Application[]>(`/servers/${handle}/applications`),
  })

  const [expandedId, setExpandedId] = useState<number | null>(null)

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
          <th className="px-4 py-1.5 text-left text-xs font-medium">Response</th>
          <th className="px-4 py-1.5 text-left text-xs font-medium">Uptime 24h</th>
          <th className="px-4 py-1.5 text-left text-xs font-medium">SSL</th>
          <th className="px-4 py-1.5 text-left text-xs font-medium">Last Check</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border/50">
        {apps.map((a) => (
          <>
            <tr
              key={a.id}
              className="cursor-pointer hover:bg-muted/30"
              onClick={() => setExpandedId(expandedId === a.id ? null : a.id)}
            >
              <td className="px-4 py-1.5">
                <span className="mr-1.5 inline-block w-3 text-muted-foreground">
                  {expandedId === a.id ? (
                    <ChevronDown className="h-3 w-3" />
                  ) : (
                    <ChevronRight className="h-3 w-3" />
                  )}
                </span>
                <span className="font-medium">{a.service_name}</span>
              </td>
              <td className="px-4 py-1.5">
                {a.ports.length > 0 ? (
                  <div className="flex flex-wrap gap-1.5">
                    {a.ports.map((p) => (
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
                <span className="flex items-center gap-1.5">
                  <span className={`inline-block h-2 w-2 rounded-full ${healthDotColor(a.status)}`} />
                  <StatusBadge status={a.status} />
                </span>
              </td>
              <td className="px-4 py-1.5 font-mono text-xs">
                {a.response_time_ms != null ? `${a.response_time_ms}ms` : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              <td className="px-4 py-1.5">
                <span className={`text-xs font-medium ${uptimeColor(a.uptime_pct_24h)}`}>
                  {a.uptime_pct_24h != null ? `${a.uptime_pct_24h.toFixed(1)}%` : '—'}
                </span>
              </td>
              <td className="px-4 py-1.5">
                <span className={`text-xs ${sslStatusColor(a.ssl_expires_at)}`}>
                  {sslStatusText(a.ssl_expires_at)}
                </span>
              </td>
              <td className="px-4 py-1.5 text-xs text-muted-foreground">
                {a.last_health_check ? relativeTime(a.last_health_check) : '—'}
              </td>
            </tr>
            {expandedId === a.id && (
              <tr key={`${a.id}-detail`}>
                <td colSpan={7} className="bg-muted/10 px-6 py-3">
                  <ApplicationDetail app={a} />
                </td>
              </tr>
            )}
          </>
        ))}
      </tbody>
    </table>
  )
}

function OverviewTab({ server }: { server: Server }) {
  const hasHealth = server.last_health_check != null

  return (
    <div className="space-y-4">
      {/* Health summary cards */}
      {hasHealth ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
          <MetricCard
            label="CPU"
            value={server.cpu_usage_pct != null ? `${server.cpu_usage_pct.toFixed(1)}%` : '—'}
          />
          <MetricCard
            label="Load Average"
            value={server.load_avg_1m?.toFixed(2) ?? '—'}
            sub={
              server.load_avg_5m != null && server.load_avg_15m != null
                ? `${server.load_avg_5m.toFixed(2)} / ${server.load_avg_15m.toFixed(2)}`
                : undefined
            }
          />
          <MetricCard
            label="Network Errors"
            value={
              (server.network_rx_errors ?? 0) + (server.network_tx_errors ?? 0)
            }
            sub={`rx: ${server.network_rx_errors ?? 0} / tx: ${server.network_tx_errors ?? 0}`}
          />
          <MetricCard
            label="Containers"
            value={`${server.container_count_running ?? 0} / ${server.container_count_total ?? 0}`}
            sub="running / total"
          />
          <MetricCard
            label="Uptime"
            value={
              server.uptime_seconds != null
                ? formatUptime(server.uptime_seconds)
                : '—'
            }
          />
          <div className="rounded-lg border border-border bg-card px-4 py-3">
            <p className="text-xs text-muted-foreground">Last Health Check</p>
            <p className="mt-0.5 flex items-center gap-1.5 text-sm font-medium">
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  freshnessColor(server.last_health_check!).replace('text-', 'bg-')
                }`}
              />
              <span className="text-foreground">
                {relativeTime(server.last_health_check!)}
              </span>
            </p>
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          No health data available — health checker has not reported yet.
        </p>
      )}

      {/* Applications */}
      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
          Applications
        </h4>
        <ServerApplications handle={server.handle} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab: Containers
// ---------------------------------------------------------------------------

function ContainersTab({ handle }: { handle: string }) {
  const { data: history, isLoading } = useQuery({
    queryKey: ['server-metrics-history', handle, 1],
    queryFn: () =>
      api.get<MetricsHistoryEntry[]>(`/servers/${handle}/metrics-history?hours=1`),
  })

  const latest = history?.[0]
  const containers = latest?.metrics?.containers ?? []

  if (isLoading)
    return <p className="py-4 text-sm text-muted-foreground">Loading...</p>
  if (containers.length === 0)
    return <p className="py-4 text-sm text-muted-foreground">No container data available</p>

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-muted-foreground">
          <th className="px-4 py-2 text-left text-xs font-medium">Container</th>
          <th className="px-4 py-2 text-left text-xs font-medium">CPU (sec)</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Memory</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Memory Usage</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border/50">
        {containers.map((c) => {
          const memPct =
            c.memory_limit_bytes > 0
              ? Math.round((c.memory_usage_bytes / c.memory_limit_bytes) * 100)
              : 0
          return (
            <tr key={c.name}>
              <td className="px-4 py-2 font-mono text-xs font-medium">{c.name}</td>
              <td className="px-4 py-2 font-mono text-xs">
                {c.cpu_usage_seconds.toFixed(2)}s
              </td>
              <td className="px-4 py-2 text-xs">
                {formatBytes(c.memory_usage_bytes)}
                {c.memory_limit_bytes > 0 && (
                  <span className="text-muted-foreground">
                    {' '}
                    / {formatBytes(c.memory_limit_bytes)}
                  </span>
                )}
              </td>
              <td className="px-4 py-2">
                {c.memory_limit_bytes > 0 ? (
                  <UsageBar percent={memPct} label="" />
                ) : (
                  <span className="text-xs text-muted-foreground">no limit</span>
                )}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

// ---------------------------------------------------------------------------
// Tab: Charts
// ---------------------------------------------------------------------------

const CHART_COLORS = {
  cpu: '#22c55e',
  ram: '#3b82f6',
  disk: '#a855f7',
} as const

const TOOLTIP_STYLE = {
  backgroundColor: '#18181b',
  border: '1px solid #3f3f46',
  borderRadius: '6px',
  fontSize: '12px',
}

function formatChartTime(iso: string, hours: number): string {
  const d = new Date(iso)
  if (hours <= 1) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function ChartsTab({ handle }: { handle: string }) {
  const [hours, setHours] = useState(1)

  const { data: history, isLoading } = useQuery({
    queryKey: ['server-metrics-history', handle, hours],
    queryFn: () =>
      api.get<MetricsHistoryEntry[]>(
        `/servers/${handle}/metrics-history?hours=${hours}`,
      ),
    refetchInterval: 60_000,
  })

  const chartData = (history ?? [])
    .slice()
    .reverse()
    .map((entry) => ({
      time: entry.recorded_at,
      cpu: entry.metrics.cpu_usage_pct ?? null,
      ram:
        entry.metrics.ram_total_bytes && entry.metrics.ram_total_bytes > 0
          ? (
              ((entry.metrics.ram_used_bytes ?? 0) / entry.metrics.ram_total_bytes) *
              100
            )
          : null,
      disk:
        entry.metrics.disk_total_bytes && entry.metrics.disk_total_bytes > 0
          ? (
              ((entry.metrics.disk_used_bytes ?? 0) / entry.metrics.disk_total_bytes) *
              100
            )
          : null,
    }))

  return (
    <div className="space-y-4">
      {/* Time range selector */}
      <div className="flex gap-2">
        {[1, 24].map((h) => (
          <button
            key={h}
            onClick={() => setHours(h)}
            className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
              hours === h
                ? 'bg-foreground text-background'
                : 'bg-muted text-muted-foreground hover:bg-muted/80'
            }`}
          >
            {h}h
          </button>
        ))}
      </div>

      {isLoading ? (
        <p className="py-4 text-sm text-muted-foreground">Loading...</p>
      ) : chartData.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">No metrics history available</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-1">
          {/* CPU Chart */}
          <ChartCard title="CPU Usage %" data={chartData} dataKey="cpu" color={CHART_COLORS.cpu} hours={hours} />
          {/* RAM Chart */}
          <ChartCard title="RAM Usage %" data={chartData} dataKey="ram" color={CHART_COLORS.ram} hours={hours} />
          {/* Disk Chart */}
          <ChartCard title="Disk Usage %" data={chartData} dataKey="disk" color={CHART_COLORS.disk} hours={hours} />
        </div>
      )}
    </div>
  )
}

function ChartCard({
  title,
  data,
  dataKey,
  color,
  hours,
}: {
  title: string
  data: { time: string; [key: string]: string | number | null }[]
  dataKey: string
  color: string
  hours: number
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h5 className="mb-3 text-xs font-semibold text-muted-foreground">{title}</h5>
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis
              dataKey="time"
              tickFormatter={(v: string) => formatChartTime(v, hours)}
              tick={{ fill: '#71717a', fontSize: 10 }}
              stroke="#27272a"
              interval="preserveStartEnd"
            />
            <YAxis
              domain={[0, 100]}
              tick={{ fill: '#71717a', fontSize: 10 }}
              stroke="#27272a"
              tickFormatter={(v: number) => `${v}%`}
              width={45}
            />
            <Tooltip
              contentStyle={TOOLTIP_STYLE}
              labelFormatter={(v) => formatChartTime(String(v), hours)}
              formatter={(value) => [`${Number(value).toFixed(1)}%`, title]}
            />
            <defs>
              <linearGradient id={`fill-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={color} stopOpacity={0.3} />
                <stop offset="95%" stopColor={color} stopOpacity={0.05} />
              </linearGradient>
            </defs>
            <Area
              type="monotone"
              dataKey={dataKey}
              stroke={color}
              fill={`url(#fill-${dataKey})`}
              strokeWidth={1.5}
              dot={false}
              connectNulls
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab: Incidents
// ---------------------------------------------------------------------------

function IncidentsTab({ handle }: { handle: string }) {
  const { data: incidents, isLoading } = useQuery({
    queryKey: ['server-incidents', handle],
    queryFn: () => api.get<Incident[]>(`/servers/${handle}/incidents`),
  })

  if (isLoading)
    return <p className="py-4 text-sm text-muted-foreground">Loading...</p>
  if (!incidents?.length)
    return <p className="py-4 text-sm text-muted-foreground">No incidents recorded</p>

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-muted-foreground">
          <th className="px-4 py-2 text-left text-xs font-medium">Type</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Status</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Detected</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Resolved</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Affected</th>
          <th className="px-4 py-2 text-left text-xs font-medium">Retries</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border/50">
        {incidents.map((inc) => (
          <tr key={inc.id}>
            <td className="px-4 py-2">
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                {inc.incident_type}
              </span>
            </td>
            <td className="px-4 py-2">
              <StatusBadge status={inc.status} />
            </td>
            <td className="px-4 py-2 text-xs text-muted-foreground">
              {relativeTime(inc.detected_at)}
            </td>
            <td className="px-4 py-2 text-xs text-muted-foreground">
              {inc.resolved_at ? relativeTime(inc.resolved_at) : 'Ongoing'}
            </td>
            <td className="px-4 py-2 text-xs text-muted-foreground">
              {inc.affected_services.length > 0
                ? inc.affected_services.join(', ')
                : '—'}
            </td>
            <td className="px-4 py-2 text-xs text-muted-foreground">
              {inc.recovery_attempts}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ---------------------------------------------------------------------------
// Server row with tabs
// ---------------------------------------------------------------------------

type ServerTab = 'overview' | 'containers' | 'charts' | 'incidents'

const SERVER_TABS: { key: ServerTab; label: string; icon: React.ReactNode }[] = [
  { key: 'overview', label: 'Overview', icon: <Activity className="h-3.5 w-3.5" /> },
  { key: 'containers', label: 'Containers', icon: <Container className="h-3.5 w-3.5" /> },
  { key: 'charts', label: 'Charts', icon: <BarChart3 className="h-3.5 w-3.5" /> },
  { key: 'incidents', label: 'Incidents', icon: <AlertTriangle className="h-3.5 w-3.5" /> },
]

function ServerRow({ server }: { server: Server }) {
  const [expanded, setExpanded] = useState(false)
  const [activeTab, setActiveTab] = useState<ServerTab>('overview')

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
            {server.cpu_usage_pct != null && (
              <UsageBar percent={Math.round(server.cpu_usage_pct)} label="CPU" />
            )}
            <UsageBar percent={ramPct} label="RAM" />
            <UsageBar percent={diskPct} label="Disk" />
          </div>
        </td>
        <td className="px-4 py-3 text-xs text-muted-foreground">
          {server.os_template ?? '—'}
        </td>
        <td className="px-4 py-3">
          {server.last_health_check ? (
            <span className="flex items-center gap-1.5 text-sm">
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  freshnessColor(server.last_health_check).replace('text-', 'bg-')
                }`}
              />
              <span className="text-xs text-muted-foreground">
                {relativeTime(server.last_health_check)}
              </span>
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">
              {server.updated_at ? relativeTime(server.updated_at) : '—'}
            </span>
          )}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={6} className="bg-muted/20 px-6 py-4">
            {/* Tab bar */}
            <div className="mb-4 flex gap-4 border-b border-border">
              {SERVER_TABS.map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  className={`flex items-center gap-1.5 border-b-2 px-1 pb-2 text-sm font-medium transition-colors ${
                    activeTab === tab.key
                      ? 'border-foreground text-foreground'
                      : 'border-transparent text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {tab.icon}
                  {tab.label}
                </button>
              ))}
            </div>

            {/* Tab content */}
            {activeTab === 'overview' && <OverviewTab server={server} />}
            {activeTab === 'containers' && <ContainersTab handle={server.handle} />}
            {activeTab === 'charts' && <ChartsTab handle={server.handle} />}
            {activeTab === 'incidents' && <IncidentsTab handle={server.handle} />}
          </td>
        </tr>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

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
                  Health
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
