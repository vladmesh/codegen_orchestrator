export function LogsPage() {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-6 py-4">
        <h1 className="text-2xl font-bold">Logs</h1>
        <p className="text-sm text-muted-foreground">Service logs via Grafana</p>
      </div>
      <iframe
        src="/grafana/d/service-logs/service-logs?orgId=1&kiosk"
        className="flex-1 w-full border-0"
        title="Grafana Logs"
      />
    </div>
  )
}
