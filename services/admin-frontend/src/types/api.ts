export interface Project {
  id: string
  name: string
  description: string | null
  status: string
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

// Worker-manager introspection API (/wm-api/*)
export interface WorkerSummary {
  id: string
  status: string
  project_id: string | null
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

export interface PromptsResponse {
  worker_id: string
  claude_md: string | null
  task_md: string | null
}

export interface Server {
  id: string
  name: string
  public_ip: string
  status: string
  cpu_used_pct: number | null
  ram_used_pct: number | null
  disk_used_pct: number | null
  last_health_check: string | null
  created_at: string
  updated_at: string
}
