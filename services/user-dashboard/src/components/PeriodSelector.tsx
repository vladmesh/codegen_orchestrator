import { cn } from '@/lib/utils'
import type { SummaryPeriod } from '@/types/api'
import { t } from '@/lib/i18n'

const periods: { value: SummaryPeriod; label: string }[] = [
  { value: '24h', label: t('period24h') },
  { value: '7d', label: t('period7d') },
  { value: '30d', label: t('period30d') },
]

interface PeriodSelectorProps {
  value: SummaryPeriod
  onChange: (period: SummaryPeriod) => void
}

export default function PeriodSelector({ value, onChange }: PeriodSelectorProps) {
  return (
    <div className="inline-flex rounded-lg border border-border bg-muted p-0.5">
      {periods.map((p) => (
        <button
          key={p.value}
          onClick={() => onChange(p.value)}
          className={cn(
            'rounded-md px-3 py-1 text-sm font-medium transition-colors',
            value === p.value
              ? 'bg-card text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
}
