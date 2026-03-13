import { Container } from 'lucide-react'
import { Card } from '@/components/ui/Card'

export function WorkersPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Workers</h1>
      <Card className="flex flex-col items-center justify-center py-12">
        <Container className="mb-4 h-12 w-12 text-muted-foreground" />
        <p className="text-lg font-medium text-foreground">Worker Inspector</p>
        <p className="text-sm text-muted-foreground">
          Coming in Phase 2 — live worker status, logs, file browser, prompts
        </p>
      </Card>
    </div>
  )
}
