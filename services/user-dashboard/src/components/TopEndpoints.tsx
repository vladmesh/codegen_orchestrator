interface TopEndpointsProps {
  endpoints: Array<Record<string, unknown>>
}

export default function TopEndpoints({ endpoints }: TopEndpointsProps) {
  if (endpoints.length === 0) {
    return <p className="text-sm text-muted-foreground">Нет данных</p>
  }

  return (
    <div className="space-y-1">
      {endpoints.slice(0, 5).map((ep, i) => (
        <div key={i} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
          <span className="font-mono truncate mr-2">{String(ep.path ?? ep.endpoint ?? '—')}</span>
          <span className="text-muted-foreground shrink-0">{String(ep.count ?? ep.requests ?? '')}</span>
        </div>
      ))}
    </div>
  )
}
