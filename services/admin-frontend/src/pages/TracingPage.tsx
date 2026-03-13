export function TracingPage() {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-6 py-4">
        <h1 className="text-2xl font-bold">LLM Tracing</h1>
        <p className="text-sm text-muted-foreground">Agent execution traces via Langfuse</p>
      </div>
      <iframe
        src="/langfuse/"
        className="flex-1 w-full border-0"
        title="Langfuse Tracing"
      />
    </div>
  )
}
