export interface User {
  id: number
  telegram_id: number
  username: string | null
  first_name: string | null
  last_name: string | null
  is_admin: boolean
  created_at: string
  last_seen: string
}

export interface Project {
  id: string
  name: string
  status: string
  config: Record<string, unknown>
  owner_id: number
  created_at: string
  updated_at: string
}

export interface Story {
  id: string
  project_id: string
  parent_story_id: string | null
  title: string
  description: string | null
  acceptance_criteria: string | null
  type: string
  status: string
  priority: number
  blocked_by_story_id: string | null
  created_by: string
  user_report: string | null
  created_at: string
  updated_at: string
}

export interface Task {
  id: string
  project_id: string
  story_id: string | null
  type: string
  title: string
  description: string | null
  plan: string | null
  status: string
  priority: number
  acceptance_criteria: string | null
  current_iteration: number
  max_iterations: number
  need_e2e: boolean
  created_by: string
  source_brainstorm_id: string | null
  blocked_by_task_id: string | null
  failure_metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
  last_event: string | null
  elapsed_minutes: number
}

export interface TaskEvent {
  id: number
  task_id: string
  event_type: string
  from_status: string | null
  to_status: string | null
  iteration: number | null
  details: Record<string, unknown>
  actor: string
  created_at: string
}

export interface QueueHealth {
  [queueName: string]: {
    length: number
    pending: number
    consumers: number
    last_delivery_id: string | null
  }
}

// /debug/queues actual response
export interface QueueBinding {
  stream: string
  group: string
  description: string
  stream_info: { length: number }
  group_info: {
    consumers: number
    pending: number
    last_delivered_id: string | null
  }
}

export interface DebugQueuesResponse {
  status: 'ok' | 'degraded'
  bindings: QueueBinding[]
  issues: string[]
}

export interface Repository {
  id: string
  project_id: string
  name: string
  git_url: string
  provider_repo_id: number | null
  role: string
  visibility: string
  is_managed: boolean
  acceptance_criteria: string | null
  bot_username: string | null
  created_at: string
  updated_at: string
}

// Worker-manager introspection API (/wm-api/*)
export interface WorkerSummary {
  id: string
  status: string
  project_id: string | null
  repo_id: string | null
  workspace_path: string | null
  dev_network: string | null
  last_activity: string | null
  error: string | null
}

export interface WorkerDetail extends WorkerSummary {
  container_id: string | null
  image: string | null
}

export interface WorkerLogsResponse {
  worker_id: string
  logs: string
  tail: number
}

export interface FileTreeEntry {
  path: string
  is_dir: boolean
  size: number
}

export interface FileContentResponse {
  worker_id: string
  path: string
  content: string
  size: number
}

export interface WorkspaceFileContentResponse {
  repo_id: string
  path: string
  content: string
  size: number
}

// Queue message browser
export interface StreamMessage {
  id: string
  timestamp: number
  data: Record<string, unknown>
  raw_fields: Record<string, string>
}

export interface QueueMessagesResponse {
  stream: string
  messages: StreamMessage[]
  total: number
}

export interface PendingEntry {
  id: string
  consumer: string
  idle_ms: number
  delivery_count: number
}

export interface QueuePendingResponse {
  stream: string
  group: string
  pending: PendingEntry[]
}

export interface Server {
  handle: string
  host: string
  public_ip: string
  ssh_user: string
  status: string
  is_managed: boolean
  capacity_cpu: number
  capacity_ram_mb: number
  capacity_disk_mb: number
  used_ram_mb: number
  used_disk_mb: number
  os_template: string | null
  labels: Record<string, string>
  notes: string | null
  provisioning_started_at: string | null
  created_at: string
  updated_at: string
  // Health metrics (from node_exporter + cadvisor)
  cpu_usage_pct: number | null
  load_avg_1m: number | null
  load_avg_5m: number | null
  load_avg_15m: number | null
  network_rx_errors: number | null
  network_tx_errors: number | null
  container_count_running: number | null
  container_count_total: number | null
  uptime_seconds: number | null
  last_health_check: string | null
}

export interface ContainerMetrics {
  name: string
  cpu_usage_seconds: number
  memory_usage_bytes: number
  memory_limit_bytes: number
}

export interface MetricsSnapshot {
  cpu_usage_pct?: number
  ram_used_bytes?: number
  ram_total_bytes?: number
  disk_used_bytes?: number
  disk_total_bytes?: number
  load_avg_1m?: number
  load_avg_5m?: number
  load_avg_15m?: number
  uptime_seconds?: number
  network_rx_errors?: number
  network_tx_errors?: number
  containers?: ContainerMetrics[]
}

export interface MetricsHistoryEntry {
  id: number
  server_handle: string
  recorded_at: string
  metrics: MetricsSnapshot
}

export interface Incident {
  id: number
  server_handle: string
  incident_type: string
  status: string
  detected_at: string
  resolved_at: string | null
  details: Record<string, unknown>
  affected_services: string[]
  recovery_attempts: number
}

export interface PortAllocation {
  id: number
  server_handle: string
  port: number
  service_name: string
  application_id: number | null
}

export interface Application {
  id: number
  repo_id: string
  server_handle: string
  service_name: string
  ports: PortAllocation[]
  status: string
  last_health_check: string | null
  response_time_ms: number | null
  ssl_expires_at: string | null
  uptime_pct_24h: number | null
  created_at: string
  updated_at: string
}

export interface ApplicationHealthMetrics {
  response_time_ms?: number
  status_code?: number
  ssl_days_remaining?: number
  healthy?: boolean
}

export interface ApplicationHealthEntry {
  id: number
  application_id: number
  recorded_at: string
  metrics: ApplicationHealthMetrics
  created_at: string
  updated_at: string | null
}

export interface QACheck {
  name: string
  pass: boolean
  detail: string
}

export interface Run {
  id: string
  type: string
  status: string
  project_id: string | null
  run_metadata: Record<string, unknown>
  result: {
    qa_outcome?: string
    summary?: string
    failed_checks?: QACheck[]
    report?: string
    error?: string
  } | null
  error_message: string | null
  created_at: string
  completed_at: string | null
}

// Langfuse traces (proxied via /langfuse-api/)
export interface LangfuseTrace {
  id: string
  name: string
  timestamp: string
  userId: string | null
  sessionId: string | null
  tags: string[]
  latency: number | null
  totalCost: number
  metadata: Record<string, unknown>
  htmlPath: string
}

export interface LangfuseTracesResponse {
  data: LangfuseTrace[]
  meta: { totalItems: number; page: number; totalPages: number }
}

// System configuration (key-value, grouped by category)
export interface SystemConfig {
  key: string
  value: unknown
  description: string | null
  category: string
  updated_by: string | null
  created_at: string
  updated_at: string
}

// Agent configuration (prompts, model settings)
export interface AgentConfig {
  id: string
  name: string
  system_prompt: string
  model_name: string
  temperature: number
  is_active: boolean
  llm_provider: string
  model_identifier: string
  openrouter_site_url: string | null
  openrouter_app_name: string | null
  version: number
  created_at: string
  updated_at: string
}
