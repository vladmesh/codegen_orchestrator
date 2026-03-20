import { useState, useEffect } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { api } from '@/lib/api'
import type { ChartMetric, ChartResponse, SummaryPeriod } from '@/types/api'
import { cn } from '@/lib/utils'
import Spinner from './Spinner'

const metrics: { value: ChartMetric; label: string }[] = [
  { value: 'users', label: 'Пользователи' },
  { value: 'requests', label: 'Запросы' },
  { value: 'errors', label: 'Ошибки' },
]

interface MetricChartProps {
  projectId: string
  period: SummaryPeriod
}

export default function MetricChart({ projectId, period }: MetricChartProps) {
  const [metric, setMetric] = useState<ChartMetric>('requests')
  const [data, setData] = useState<ChartResponse | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    api.get<ChartResponse>(`/lk/projects/${projectId}/chart?metric=${metric}&period=${period}`)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [projectId, metric, period])

  return (
    <div>
      <div className="flex items-center gap-1 mb-4">
        {metrics.map((m) => (
          <button
            key={m.value}
            onClick={() => setMetric(m.value)}
            className={cn(
              'rounded-md px-3 py-1 text-sm font-medium transition-colors',
              metric === m.value
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {m.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : !data || data.data.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-12">Нет данных для графика</p>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={data.data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e4e7" />
            <XAxis dataKey="date" tick={{ fontSize: 12 }} stroke="#71717a" />
            <YAxis tick={{ fontSize: 12 }} stroke="#71717a" />
            <Tooltip
              contentStyle={{
                backgroundColor: '#fff',
                border: '1px solid #e4e4e7',
                borderRadius: '0.5rem',
                fontSize: '0.875rem',
              }}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke="#2563eb"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
