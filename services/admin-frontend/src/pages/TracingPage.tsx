import { ExternalLink } from 'lucide-react'
import { langfuseUrl } from '@/lib/langfuse'

export function TracingPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">LLM Tracing</h1>
      <p className="text-muted-foreground">
        Agent execution traces are collected via Langfuse.
      </p>
      <a
        href={langfuseUrl()}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
      >
        Open Langfuse
        <ExternalLink className="h-4 w-4" />
      </a>
    </div>
  )
}
