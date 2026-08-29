export interface User {
  id: number
  telegram_id: number
  username?: string | null
  first_name?: string | null
  last_name?: string | null
  is_admin: boolean
  created_at: string
  updated_at?: string | null
  last_seen: string
}

export interface Project {
  id: string
  title: string
  slug: string
  status?: string
  config?: Record<string, unknown>
  owner_id: number
  project_spec?: Record<string, unknown> | null
  initiating_run_id?: string | null
  created_at: string
  updated_at?: string | null
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
  quarantine_reason?: Record<string, unknown> | null
  operator_acceptance?: StoryAcceptance | null
  reopened_at?: string | null
  pr_number?: number | null
  created_at: string
  updated_at?: string | null
}

export interface StoryAcceptance {
  actor: string
  basis: string
  accepted_at: string
}

export interface Task {
  id: string
  project_id: string
  story_id?: string | null
  type: string
  title: string
  description: string | null
  plan?: string | null
  status: string
  priority: number
  acceptance_criteria: string | null
  current_iteration: number
  max_iterations: number
  need_e2e?: boolean
  created_by: string
  source_brainstorm_id?: string | null
  repository_id?: string | null
  blocked_by_task_id?: string | null
  failure_metadata?: Record<string, unknown> | null
  created_at: string
  updated_at?: string | null
  last_event?: string | null
  elapsed_minutes?: number | null
}

export type TaskStatus =
  | 'backlog'
  | 'todo'
  | 'in_dev'
  | 'in_ci'
  | 'testing'
  | 'done'
  | 'blocked'
  | 'waiting_human_review'
  | 'waiting_resources'
  | 'failed'
  | 'cancelled'

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
  updated_at?: string | null
}

export interface TaskTransition {
  reason?: string | null
  actor?: string
  details?: Record<string, unknown>
}

export interface TaskResume {
  guidance: string
  actor?: string
}

export interface SpawnWorkerRequest {
  actor?: string
  description?: string | null
}

export type SpawnWorkerResponse = Record<string, unknown>

export interface QueueStreamInfo {
  length: number
}

export interface QueueGroupInfo {
  consumers: number
  pending: number
  last_delivered_id: string
}

export interface QueueBinding {
  stream: string
  group: string
  description: string
  stream_info: QueueStreamInfo | null
  group_info: QueueGroupInfo | null
}

export interface DebugQueuesResponse {
  status: 'ok' | 'degraded'
  bindings: QueueBinding[]
  issues: string[]
}

export type ExecutorDecisionSource =
  | 'global_override'
  | 'project_pin'
  | 'api_default'
  | 'qa_api_setting'

export interface ExecutorDecision {
  attempt_kind: 'engineering' | 'qa'
  agent_type: 'claude' | 'factory' | 'codex' | 'noop'
  source: ExecutorDecisionSource
  policy_version: 'v1' | 'v2'
  reason: string
}

export interface PaidRunStateCounts {
  queued: number
  running: number
}

export interface TaskStatusCounts {
  backlog: number
  todo: number
  in_dev: number
  in_ci: number
  testing: number
  done: number
  blocked: number
  waiting_human_review: number
  waiting_resources: number
  failed: number
  cancelled: number
}

export interface PaidRunCounts {
  queued: number
  running: number
  by_executor: Partial<Record<ExecutorDecision['agent_type'], PaidRunStateCounts>>
  unavailable_executor_decisions: number
}

export interface AdminOverview {
  queues: DebugQueuesResponse
  task_counts: TaskStatusCounts
  paid_runs: PaidRunCounts
  recent_failed_runs: RecentFailedRun[]
}

export interface RecentFailedRun {
  id: string
  type: 'engineering' | 'qa' | 'deploy'
  project_id: string | null
  task_id: string | null
  story_id: string | null
  error_message: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  executor_decision: ExecutorDecision | null
  executor_decision_availability: 'available' | 'legacy' | 'invalid'
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
  acceptance_criteria?: string | null
  bot_username?: string | null
  created_at: string
  updated_at?: string | null
}

export interface MergeSecretsRequest {
  secrets: Record<string, string>
  env_hints?: Record<string, string> | null
}

export type SecretKeys = Record<string, string[]>

export interface StoryCreate {
  project_id: string
  title: string
  description?: string | null
  acceptance_criteria?: string | null
  parent_story_id?: string | null
  type?: 'product' | 'technical'
  priority?: number
  blocked_by_story_id?: string | null
  created_by?: string
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
  created_at: string
  updated_at?: string | null
  handle: string
  host: string
  public_ip: string
  ssh_user?: string
  status?: string
  is_managed?: boolean
  capacity_cpu?: number
  capacity_ram_mb?: number
  capacity_disk_mb?: number
  used_ram_mb?: number
  used_disk_mb?: number
  os_template?: string | null
  labels?: Record<string, unknown>
  provider?: string | null
  provider_id?: string | null
  notes?: string | null
  provisioning_started_at?: string | null
  cpu_usage_pct?: number | null
  load_avg_1m?: number | null
  load_avg_5m?: number | null
  load_avg_15m?: number | null
  network_rx_errors?: number | null
  network_tx_errors?: number | null
  container_count_running?: number | null
  container_count_total?: number | null
  uptime_seconds?: number | null
  last_health_check?: string | null
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
  created_at: string
  updated_at?: string | null
  id: number
  server_handle: string
  port: number
  service_name: string
  application_id?: number | null
}

export interface Application {
  created_at: string
  updated_at?: string | null
  id: number
  repo_id: string
  server_handle: string
  service_name: string
  reserved_ram_mb: number
  ports?: PortAllocation[]
  status: string
  last_health_check?: string | null
  response_time_ms?: number | null
  ssl_expires_at?: string | null
  uptime_pct_24h?: number | null
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
  user_id: number | null
  story_id: string | null
  task_id: string | null
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
  started_at: string | null
  callback_stream: string | null
  iteration: number | null
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  cost_usd: number | null
  agent_profile: Record<string, unknown> | null
  transcript_path: string | null
  transcript_truncated: boolean | null
}

// System configuration (key-value, grouped by category)
export interface SystemConfig {
  key: string
  value: unknown
  description?: string | null
  category: string
  updated_by?: string | null
  created_at: string
  updated_at?: string | null
}

export interface SystemConfigUpdate {
  value?: unknown | null
  description?: string | null
  category?: string | null
  updated_by?: string | null
}

export interface FromRepoRequest {
  repo_url: string
  project_id: string
  server_handle: string
  service_name: string
  actor?: string
}

export type FromRepoResponse = Record<string, unknown>

export type ExecutorOverride = 'none' | 'claude' | 'codex'

export interface PaidWorkControls {
  emergency_stop: boolean
  max_concurrent_paid_runs: number
  engineering_executor_override: ExecutorOverride
  qa_executor_override: ExecutorOverride
}

export type ExecutorAvailability = 'available' | 'degraded' | 'unavailable' | 'unknown'
export type ExecutorAuthMode = 'host_session' | 'api_key' | 'stand_token' | 'unknown'

export interface ExecutorDiagnostic {
  executor: 'claude' | 'codex'
  enabled: boolean
  auth_mode: ExecutorAuthMode
  availability: ExecutorAvailability
  observed_at: string
  expires_at: string
  active_lease_count?: number | null
  reason_code: string
  reason: string
}

export interface ExecutorDiagnosticSnapshot {
  schema_version: 'v1'
  version: string
  observed_at: string
  expires_at: string
  diagnostics: ExecutorDiagnostic[]
}

export interface ExecutorDiagnosticConfirmationCommand {
  executor: 'claude' | 'codex'
  snapshot_version: string
}

export interface ExecutorDiagnosticConfirmation {
  executor: 'claude' | 'codex'
  snapshot_version: string
  expires_at: string
}

// Agent configuration (prompts, model settings)
export interface AgentConfig {
  id: string
  name: string
  system_prompt: string
  model_name?: string
  temperature?: number
  is_active?: boolean
  llm_provider?: string
  model_identifier?: string
  openrouter_site_url?: string | null
  openrouter_app_name?: string | null
  version: number
  created_at: string
  updated_at?: string | null
}

export interface AgentConfigUpdate {
  name?: string | null
  system_prompt?: string | null
  model_name?: string | null
  temperature?: number | null
  is_active?: boolean | null
  llm_provider?: string | null
  model_identifier?: string | null
  openrouter_site_url?: string | null
  openrouter_app_name?: string | null
}
