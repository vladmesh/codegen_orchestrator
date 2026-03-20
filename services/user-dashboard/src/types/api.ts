// Matches services/api/src/schemas/lk.py

export interface TokenExchangeResponse {
  access_token: string
  token_type: string
}

export interface LatestDailySummary {
  date: string
  total_requests: number
  unique_users: number
  error_rate: number | null
  p95_ms: number | null
}

export interface LkProject {
  id: string
  name: string
  status: string
  latest_daily: LatestDailySummary | null
}

export type SummaryPeriod = '24h' | '7d' | '30d'

export interface ServiceBreakdown {
  service_name: string
  total_requests: number
  error_count: number
  unique_users: number
  p95_ms: number | null
}

export interface ProjectSummaryResponse {
  total_users: number
  new_users: number
  dau: number
  wau: number
  returning_pct: number
  total_requests: number
  error_rate: number
  p95_ms: number | null
  top_endpoints: Array<Record<string, unknown>>
  breakdown: ServiceBreakdown[]
}

export type ChartMetric = 'users' | 'requests' | 'errors'

export interface ChartDataPoint {
  date: string
  value: number
}

export interface ChartResponse {
  metric: ChartMetric
  period: SummaryPeriod
  data: ChartDataPoint[]
}

export interface ServiceStatus {
  name: string
  status: string
  last_seen: string | null
}

export interface ProjectStatusResponse {
  services: ServiceStatus[]
}
