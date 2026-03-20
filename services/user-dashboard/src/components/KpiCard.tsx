import { cn } from '@/lib/utils'
import type { ReactNode } from 'react'

interface KpiCardProps {
  label: string
  value: string
  icon: ReactNode
  className?: string
}

export default function KpiCard({ label, value, icon, className }: KpiCardProps) {
  return (
    <div className={cn(
      'rounded-xl border border-border bg-card p-4 shadow-sm',
      className,
    )}>
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        {icon}
        <span className="text-sm">{label}</span>
      </div>
      <div className="text-2xl font-bold">{value}</div>
    </div>
  )
}
