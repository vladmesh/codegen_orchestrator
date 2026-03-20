import type { ServiceBreakdown as ServiceBreakdownType } from '@/types/api'
import { formatNumber, formatMs, formatPercent } from '@/lib/utils'

export default function ServiceBreakdown({ breakdown }: { breakdown: ServiceBreakdownType[] }) {
  if (breakdown.length === 0) {
    return <p className="text-sm text-muted-foreground">Нет данных</p>
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="pb-2 font-medium">Сервис</th>
            <th className="pb-2 font-medium text-right">Запросы</th>
            <th className="pb-2 font-medium text-right">Ошибки</th>
            <th className="pb-2 font-medium text-right">Пользователи</th>
            <th className="pb-2 font-medium text-right">Скорость</th>
          </tr>
        </thead>
        <tbody>
          {breakdown.map((s) => {
            const errorRate = s.total_requests > 0 ? s.error_count / s.total_requests : 0
            return (
              <tr key={s.service_name} className="border-b border-border last:border-0">
                <td className="py-2 font-medium">{s.service_name}</td>
                <td className="py-2 text-right">{formatNumber(s.total_requests)}</td>
                <td className="py-2 text-right">{formatPercent(errorRate)}</td>
                <td className="py-2 text-right">{formatNumber(s.unique_users)}</td>
                <td className="py-2 text-right">{formatMs(s.p95_ms)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
