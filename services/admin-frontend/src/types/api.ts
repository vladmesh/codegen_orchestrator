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
  description: string | null
  status: string
  owner_id: number
  github_repo: string | null
  domain: string | null
  created_at: string
  updated_at: string
}

export interface Story {
  id: string
  project_id: string
  title: string
  description: string | null
  status: string
  priority: number
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
  git_url: string | null
  role: string
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

export interface PromptsResponse {
  worker_id: string
  claude_md: string | null
  task_md: string | null
}

export interface PromptHistoryEntry {
  prompt: string
  ts: number
  source: string
}

export interface PromptHistoryResponse {
  worker_id: string
  entries: PromptHistoryEntry[]
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
}

export interface Application {
  id: number
  repo_id: string
  server_handle: string
  service_name: string
  port: number
  status: string
  last_health_check: string | null
  created_at: string
  updated_at: string
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
