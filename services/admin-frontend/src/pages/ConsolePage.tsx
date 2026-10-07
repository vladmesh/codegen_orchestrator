import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import type { Attention, AttentionItem } from '@/types/api'
import { attentionLink, formatLeadTime, severityCounts } from './attentionView'

const severityStripe: Record<AttentionItem['severity'], string> = {
  critical: 'shadow-[inset_3px_0_0_var(--color-red-500)]',
  warning: 'shadow-[inset_3px_0_0_var(--color-amber-500)]',
  info: 'shadow-[inset_3px_0_0_var(--color-teal-400)]',
}

function Kpi({ value, label, tone }: { value: React.ReactNode; label: string; tone?: string }) {
  return (
    <div className="flex flex-col gap-0.5 border-r border-border px-4 py-3 last:border-r-0">
      <span className={cn('text-2xl font-semibold tabular-nums', tone)}>{value}</span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  )
}

export function ConsolePage() {
  const attention = useQuery({
    queryKey: ['admin-console-attention'],
    queryFn: () => api.get<Attention>('/admin/v2/attention'),
    refetchInterval: 15_000,
  })

  if (attention.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (attention.isError || !attention.data) {
    return <p role="alert" className="text-red-400">Attention feed request failed.</p>
  }

  const { kpis, items } = attention.data
  const counts = severityCounts(items)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-2xl font-bold text-foreground">Needs attention</h1>
        <span className="text-sm text-muted-foreground">
          {counts.critical} critical · {counts.warning} warning · {counts.info} info
        </span>
      </div>

      <div className="grid grid-cols-2 overflow-hidden rounded-lg border border-border sm:grid-cols-3 lg:grid-cols-6">
        <Kpi value={kpis.active_journeys} label="journeys in flight" />
        <Kpi value={kpis.running_runs} label="runs running" />
        <Kpi value={kpis.queued_runs} label="runs queued" />
        <Kpi value={kpis.live_products} label="products live" />
        <Kpi
          value={kpis.degraded_containers}
          label="containers down or degraded"
          tone={kpis.degraded_containers > 0 ? 'text-amber-400' : undefined}
        />
        <Kpi value={formatLeadTime(kpis.median_lead_time_minutes_7d)} label="median story → live, 7 d" />
      </div>

      {items.length === 0 ? (
        <p className="rounded-lg border border-border p-6 text-muted-foreground">
          Nothing is parked, failed or degraded.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-border text-left font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="px-3 py-2 font-medium">What</th>
                <th className="px-3 py-2 font-medium">Where</th>
                <th className="px-3 py-2 font-medium">Since</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => {
                const link = attentionLink(item)
                return (
                  <tr key={`${item.kind}-${index}`} className="border-b border-border/60 align-top last:border-b-0">
                    <td className={cn('px-3 py-2.5', severityStripe[item.severity])}>
                      <div className="text-foreground">{item.title}</div>
                      {item.detail && <div className="mt-0.5 text-xs text-muted-foreground">{item.detail}</div>}
                    </td>
                    <td className="px-3 py-2.5 text-muted-foreground">
                      {[item.project_title, item.server_handle].filter(Boolean).join(' · ') || '—'}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5 text-muted-foreground">{item.since ? relativeTime(item.since) : '—'}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 text-right">
                      {link && (
                        <Link to={link} className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-accent">
                          {item.story_id ? 'Open journey' : 'Open'}
                        </Link>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
