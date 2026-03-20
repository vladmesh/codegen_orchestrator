import { useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { Users, Activity, AlertTriangle, Gauge } from 'lucide-react'
import { api } from '@/lib/api'
import type { ProjectSummaryResponse, ProjectStatusResponse, SummaryPeriod } from '@/types/api'
import { formatNumber, formatMs, formatPercent } from '@/lib/utils'
import KpiCard from '@/components/KpiCard'
import PeriodSelector from '@/components/PeriodSelector'
import MetricChart from '@/components/MetricChart'
import ServiceStatusList from '@/components/ServiceStatusList'
import TopEndpoints from '@/components/TopEndpoints'
import ServiceBreakdown from '@/components/ServiceBreakdown'
import Spinner from '@/components/Spinner'
import ErrorMessage from '@/components/ErrorMessage'

export default function DashboardPage() {
  const { id } = useParams<{ id: string }>()
  const [period, setPeriod] = useState<SummaryPeriod>('7d')
  const [summary, setSummary] = useState<ProjectSummaryResponse | null>(null)
  const [status, setStatus] = useState<ProjectStatusResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)

    Promise.all([
      api.get<ProjectSummaryResponse>(`/lk/projects/${id}/summary?period=${period}`),
      api.get<ProjectStatusResponse>(`/lk/projects/${id}/status`),
    ])
      .then(([s, st]) => {
        setSummary(s)
        setStatus(st)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [id, period])

  if (loading) {
    return <div className="flex justify-center py-12"><Spinner /></div>
  }

  if (error) {
    return <ErrorMessage message={error} />
  }

  if (!summary) {
    return <p className="text-center text-muted-foreground py-12">Нет данных</p>
  }

  return (
    <div className="space-y-6">
      {/* Period selector */}
      <div className="flex justify-end">
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <KpiCard
          label="Пользователи"
          value={formatNumber(summary.total_users)}
          icon={<Users className="h-4 w-4" />}
        />
        <KpiCard
          label="Запросы"
          value={formatNumber(summary.total_requests)}
          icon={<Activity className="h-4 w-4" />}
        />
        <KpiCard
          label="Ошибки"
          value={formatPercent(summary.error_rate)}
          icon={<AlertTriangle className="h-4 w-4" />}
        />
        <KpiCard
          label="Скорость"
          value={formatMs(summary.p95_ms)}
          icon={<Gauge className="h-4 w-4" />}
        />
      </div>

      {/* Chart */}
      <div className="rounded-xl border border-border bg-card p-4 shadow-sm">
        <h2 className="font-semibold mb-4">Динамика</h2>
        <MetricChart projectId={id!} period={period} />
      </div>

      {/* Service status */}
      {status && status.services.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-4 shadow-sm">
          <h2 className="font-semibold mb-3">Сервисы</h2>
          <ServiceStatusList services={status.services} />
        </div>
      )}

      {/* Top endpoints */}
      {summary.top_endpoints.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-4 shadow-sm">
          <h2 className="font-semibold mb-3">Популярные эндпоинты</h2>
          <TopEndpoints endpoints={summary.top_endpoints} />
        </div>
      )}

      {/* Service breakdown */}
      {summary.breakdown.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-4 shadow-sm">
          <h2 className="font-semibold mb-3">По сервисам</h2>
          <ServiceBreakdown breakdown={summary.breakdown} />
        </div>
      )}
    </div>
  )
}
