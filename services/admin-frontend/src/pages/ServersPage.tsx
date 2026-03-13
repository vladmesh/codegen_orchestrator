import { Server } from 'lucide-react'
import { Card } from '@/components/ui/Card'

export function ServersPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Servers</h1>
      <Card className="flex flex-col items-center justify-center py-12">
        <Server className="mb-4 h-12 w-12 text-muted-foreground" />
        <p className="text-lg font-medium text-foreground">Server Management</p>
        <p className="text-sm text-muted-foreground">
          Coming in Phase 2 — server resources, deployments, incidents
        </p>
      </Card>
    </div>
  )
}
