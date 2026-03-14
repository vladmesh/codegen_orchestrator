import { useEffect, useRef } from 'react'
import { useParams, Link, useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatDate, relativeTime } from '@/lib/utils'
import type { User, Project, LangfuseTracesResponse } from '@/types/api'

// -- Message types from Langfuse trace output --

interface ToolCall {
  name: string
  args: Record<string, unknown>
  id: string
}

interface TraceMessage {
  type: 'human' | 'ai' | 'tool' | 'system'
  content: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
}

// -- Tab switcher --

type Tab = 'projects' | 'messages'

function TabButton({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={`border-b-2 px-3 pb-2 text-sm font-medium transition-colors ${
        active
          ? 'border-primary text-foreground'
          : 'border-transparent text-muted-foreground hover:text-foreground'
      }`}
    >
      {label}
    </button>
  )
}

// -- Message rendering --

function parseContextPrefix(content: string): { cleaned: string; isSystem: boolean } {
  // Strip [context: user_id=..., user_name=...] prefix
  let cleaned = content.replace(/^\[context: [^\]]+\]\s*/, '')
  // Strip [timestamp UTC] prefix
  cleaned = cleaned.replace(/^\[\d{4}-\d{2}-\d{2}T[\d:]+ UTC\]\s*/, '')

  const isSystem =
    content.startsWith('[system:') || content.includes('[system:')
  return { cleaned, isSystem }
}

function HumanMessage({ msg }: { msg: TraceMessage }) {
  const { cleaned, isSystem } = parseContextPrefix(msg.content || '')
  if (isSystem) {
    return (
      <div className="ml-4 border-l-2 border-zinc-700 py-1 pl-3 text-xs text-muted-foreground italic">
        {cleaned}
      </div>
    )
  }
  return (
    <div className="flex justify-end">
      <div className="max-w-[75%] rounded-lg bg-blue-600/20 px-3 py-2 text-sm text-foreground">
        {cleaned}
      </div>
    </div>
  )
}

function AiMessage({ msg }: { msg: TraceMessage }) {
  const hasText = msg.content && msg.content.trim().length > 0
  const hasTools = msg.tool_calls && msg.tool_calls.length > 0

  return (
    <div className="space-y-1">
      {hasText && (
        <div className="max-w-[75%] rounded-lg bg-zinc-800 px-3 py-2 text-sm text-foreground whitespace-pre-wrap">
          {msg.content}
        </div>
      )}
      {hasTools &&
        msg.tool_calls!.map((tc) => (
          <div
            key={tc.id}
            className="ml-2 rounded border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs"
          >
            <span className="font-mono font-semibold text-purple-400">{tc.name}</span>
            <span className="text-muted-foreground">(</span>
            <span className="text-muted-foreground">
              {formatArgs(tc.args)}
            </span>
            <span className="text-muted-foreground">)</span>
          </div>
        ))}
    </div>
  )
}

function ToolMessage({ msg }: { msg: TraceMessage }) {
  const content = msg.content || ''
  const isError = content.startsWith('Error:')
  return (
    <div
      className={`ml-6 rounded border px-3 py-1.5 text-xs font-mono ${
        isError
          ? 'border-red-800 bg-red-950/30 text-red-400'
          : 'border-zinc-700 bg-zinc-900/50 text-muted-foreground'
      }`}
    >
      {content.length > 300 ? content.slice(0, 300) + '...' : content}
    </div>
  )
}

function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args)
  if (entries.length === 0) return ''
  const parts = entries.map(([k, v]) => {
    const val = typeof v === 'string' ? (v.length > 60 ? `"${v.slice(0, 60)}..."` : `"${v}"`) : JSON.stringify(v)
    return `${k}=${val}`
  })
  return parts.join(', ')
}

// -- Messages tab content --

function MessagesTab({ userId }: { userId: string }) {
  const bottomRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['langfuse-traces', 'user', userId],
    queryFn: () =>
      api.raw<LangfuseTracesResponse>(
        `/langfuse-api/traces?userId=${userId}&limit=1`
      ),
    refetchInterval: 7_000,
  })

  // Messages are in trace output (full conversation history)
  const trace = data?.data?.[0]
  const rawOutput = trace
    ? ((trace as unknown as Record<string, unknown>).output as
        | { messages?: TraceMessage[] }
        | undefined)
    : undefined
  const messages: TraceMessage[] = rawOutput?.messages ?? []

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'instant' })
  }, [messages.length])

  if (isLoading) return <p className="text-muted-foreground">Loading traces...</p>
  if (isError) return <p className="text-muted-foreground">Langfuse unavailable</p>
  if (!trace) return <p className="text-muted-foreground">No PO conversation found for this user</p>

  if (messages.length === 0) {
    return <p className="text-muted-foreground">Trace found but no messages</p>
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        {messages.length} messages &middot; last trace {relativeTime(trace.timestamp)}
      </p>
      <div className="max-h-[600px] space-y-2 overflow-y-auto rounded-lg border border-border p-3">
        {messages.map((msg, i) => {
          if (msg.type === 'human') return <HumanMessage key={i} msg={msg} />
          if (msg.type === 'ai') return <AiMessage key={i} msg={msg} />
          if (msg.type === 'tool') return <ToolMessage key={i} msg={msg} />
          return null
        })}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

// -- Main page --

export function UserDetailPage() {
  const { id } = useParams<{ id: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = (searchParams.get('tab') as Tab) || 'projects'
  const setTab = (t: Tab) => setSearchParams({ tab: t }, { replace: true })

  const { data: user, isLoading } = useQuery({
    queryKey: ['user', id],
    queryFn: () => api.get<User>(`/users/${id}`),
    enabled: !!id,
  })

  const { data: projects } = useQuery({
    queryKey: ['projects', 'owner', id],
    queryFn: () => api.get<Project[]>(`/projects/?owner_id=${id}`),
    enabled: !!id,
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!user) return <p className="text-muted-foreground">User not found</p>

  const displayName = user.first_name
    ? `${user.first_name}${user.last_name ? ` ${user.last_name}` : ''}`
    : user.username ?? `User #${user.id}`

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/users" className="text-muted-foreground hover:text-foreground">
          Users
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-2xl font-bold text-foreground">{displayName}</h1>
        {user.is_admin && (
          <span className="inline-flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-500">
            admin
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <p className="text-sm text-muted-foreground">Telegram ID</p>
          <p className="mt-1 font-mono text-sm text-foreground">{user.telegram_id}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Username</p>
          <p className="mt-1 text-foreground">{user.username ? `@${user.username}` : '—'}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Last seen</p>
          <p className="mt-1 text-foreground">{relativeTime(user.last_seen)}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Registered</p>
          <p className="mt-1 text-foreground">{formatDate(user.created_at)}</p>
        </Card>
      </div>

      <div className="flex gap-4 border-b border-border">
        <TabButton label="Projects" active={tab === 'projects'} onClick={() => setTab('projects')} />
        <TabButton
          label="Messages"
          active={tab === 'messages'}
          onClick={() => setTab('messages')}
        />
      </div>

      {tab === 'projects' && (
        <>
          {(projects ?? []).length === 0 ? (
            <p className="text-muted-foreground">No projects yet</p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead className="border-b border-border bg-muted/50">
                  <tr>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">Name</th>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                      Status
                    </th>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                      Domain
                    </th>
                    <th className="px-4 py-3 text-left font-medium text-muted-foreground">
                      Updated
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(projects ?? []).map((project) => (
                    <tr key={project.id} className="hover:bg-muted/30">
                      <td className="px-4 py-3">
                        <Link
                          to={`/projects/${project.id}`}
                          className="font-medium text-primary hover:underline"
                        >
                          {project.name}
                        </Link>
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={project.status} />
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {project.domain ?? '—'}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {relativeTime(project.updated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {tab === 'messages' && user.telegram_id && (
        <MessagesTab userId={String(user.telegram_id)} />
      )}
    </div>
  )
}
