import type { ServiceStatus } from '@/types/api'
import StatusBadge from './StatusBadge'

export default function ServiceStatusList({ services }: { services: ServiceStatus[] }) {
  if (services.length === 0) {
    return <p className="text-sm text-muted-foreground">Нет сервисов</p>
  }

  return (
    <div className="space-y-2">
      {services.map((s) => (
        <div key={s.name} className="flex items-center justify-between rounded-lg border border-border px-3 py-2">
          <span className="text-sm font-medium">{s.name}</span>
          <StatusBadge status={s.status} />
        </div>
      ))}
    </div>
  )
}
