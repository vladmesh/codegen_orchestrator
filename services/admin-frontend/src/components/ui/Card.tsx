import { cn } from '@/lib/utils'

interface CardProps {
  className?: string
  children: React.ReactNode
}

export function Card({ className, children }: CardProps) {
  return (
    <div className={cn('rounded-lg border border-border bg-card p-6', className)}>
      {children}
    </div>
  )
}

export function CardTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-sm font-medium text-muted-foreground">{children}</h3>
}

export function CardValue({ children }: { children: React.ReactNode }) {
  return <p className="mt-1 text-3xl font-semibold text-foreground">{children}</p>
}
