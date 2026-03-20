import { cn } from '@/lib/utils'

interface MetricValueProps {
  label: string
  value: string
  className?: string
}

export default function MetricValue({ label, value, className }: MetricValueProps) {
  return (
    <div className={cn('text-center', className)}>
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  )
}
