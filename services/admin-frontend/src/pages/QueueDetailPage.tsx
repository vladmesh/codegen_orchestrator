import { useState } from 'react'
import { useParams, Link } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import type { QueueMessagesResponse, QueuePendingResponse, StreamMessage, PendingEntry } from '@/types/api'

type Tab = 'messages' | 'pending'

export function QueueDetailPage() {
  const { stream, group } = useParams<{ stream: string; group: string }>()
  const [activeTab, setActiveTab] = useState<Tab>('messages')
  const decodedStream = decodeURIComponent(stream ?? '')
  const decodedGroup = decodeURIComponent(group ?? '')

  const tabs: { key: Tab; label: string }[] = [
    { key: 'messages', label: 'Messages' },
    { key: 'pending', label: 'Pending' },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Link to="/queues" className="text-muted-foreground hover:text-foreground">
          Queues
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="font-mono text-xl font-bold text-foreground">{decodedStream}</h1>
        <span className="inline-flex items-center rounded-full bg-zinc-700 px-2.5 py-0.5 text-xs font-medium text-zinc-200">
          {decodedGroup}
        </span>
      </div>

      {/* Tabs */}
      <div className="border-b border-border">
        <nav className="flex gap-4">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`border-b-2 px-1 pb-2 text-sm font-medium transition-colors ${
                activeTab === tab.key
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {activeTab === 'messages' && <MessagesTab stream={decodedStream} />}
      {activeTab === 'pending' && <PendingTab stream={decodedStream} group={decodedGroup} />}
    </div>
  )
}

/* ---------- Messages Tab ---------- */

function MessagesTab({ stream }: { stream: string }) {
  const queryClient = useQueryClient()
  const [count, setCount] = useState(50)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['queue-messages', stream, count],
    queryFn: () => api.raw<QueueMessagesResponse>(`/debug/queues/${encodeURIComponent(stream)}/messages?count=${count}`),
    refetchInterval: 10_000,
  })

  const deleteMutation = useMutation({
    mutationFn: (messageId: string) =>
      api.rawDelete<void>(`/debug/queues/${encodeURIComponent(stream)}/messages/${messageId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue-messages', stream] })
    },
  })

  if (isLoading) return <p className="text-muted-foreground">Loading messages...</p>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Showing {data?.messages.length ?? 0} of {data?.total ?? 0} messages
        </p>
        <div className="flex items-center gap-3">
          <label className="text-sm text-muted-foreground">Count:</label>
          <select
            value={count}
            onChange={(e) => setCount(Number(e.target.value))}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground"
          >
            {[20, 50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
      </div>

      {!data || data.messages.length === 0 ? (
        <Card>
          <p className="text-center text-muted-foreground">Stream is empty</p>
        </Card>
      ) : (
        <div className="space-y-1">
          {data.messages.map((msg) => (
            <MessageRow
              key={msg.id}
              message={msg}
              expanded={expandedId === msg.id}
              onToggle={() => setExpandedId(expandedId === msg.id ? null : msg.id)}
              onDelete={() => deleteMutation.mutate(msg.id)}
              isDeleting={deleteMutation.isPending && deleteMutation.variables === msg.id}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function MessageRow({
  message,
  expanded,
  onToggle,
  onDelete,
  isDeleting,
}: {
  message: StreamMessage
  expanded: boolean
  onToggle: () => void
  onDelete: () => void
  isDeleting: boolean
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const ts = new Date(message.timestamp * 1000)
  const preview = summarizeData(message.data)

  return (
    <div className="rounded-lg border border-border bg-card">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-4 px-4 py-3 text-left hover:bg-muted/30"
      >
        <span className="text-xs text-muted-foreground">{expanded ? '▼' : '▶'}</span>
        <span className="shrink-0 font-mono text-xs text-muted-foreground">{message.id}</span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {ts.toLocaleString()}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm text-foreground">{preview}</span>
      </button>

      {expanded && (
        <div className="border-t border-border px-4 py-3">
          <pre className="max-h-[400px] overflow-auto rounded-lg bg-zinc-950 p-3 font-mono text-xs text-zinc-300">
            {JSON.stringify(message.data, null, 2)}
          </pre>
          <div className="mt-3 flex justify-end">
            {confirmDelete ? (
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">Delete this message?</span>
                <button
                  onClick={() => { onDelete(); setConfirmDelete(false) }}
                  disabled={isDeleting}
                  className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50"
                >
                  {isDeleting ? 'Deleting...' : 'Confirm'}
                </button>
                <button
                  onClick={() => setConfirmDelete(false)}
                  className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirmDelete(true)}
                className="rounded-md border border-red-800 px-3 py-1.5 text-sm text-red-400 hover:bg-red-900/30"
              >
                Delete
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ---------- Pending Tab ---------- */

function PendingTab({ stream, group }: { stream: string; group: string }) {
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['queue-pending', stream, group],
    queryFn: () =>
      api.raw<QueuePendingResponse>(
        `/debug/queues/${encodeURIComponent(stream)}/${encodeURIComponent(group)}/pending`
      ),
    refetchInterval: 10_000,
  })

  const ackMutation = useMutation({
    mutationFn: (messageId: string) =>
      api.rawPost<void>(
        `/debug/queues/${encodeURIComponent(stream)}/${encodeURIComponent(group)}/ack/${messageId}`,
        {}
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queue-pending', stream, group] })
      queryClient.invalidateQueries({ queryKey: ['queues'] })
    },
  })

  if (isLoading) return <p className="text-muted-foreground">Loading pending messages...</p>

  if (!data || data.pending.length === 0) {
    return (
      <Card>
        <p className="text-center text-muted-foreground">No pending messages</p>
      </Card>
    )
  }

  return (
    <div className="overflow-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/30">
            <th className="px-4 py-2 text-left font-medium text-muted-foreground">Message ID</th>
            <th className="px-4 py-2 text-left font-medium text-muted-foreground">Consumer</th>
            <th className="px-4 py-2 text-left font-medium text-muted-foreground">Idle</th>
            <th className="px-4 py-2 text-left font-medium text-muted-foreground">Deliveries</th>
            <th className="px-4 py-2 text-right font-medium text-muted-foreground">Actions</th>
          </tr>
        </thead>
        <tbody>
          {data.pending.map((entry) => (
            <PendingRow
              key={entry.id}
              entry={entry}
              onAck={() => ackMutation.mutate(entry.id)}
              isAcking={ackMutation.isPending && ackMutation.variables === entry.id}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PendingRow({
  entry,
  onAck,
  isAcking,
}: {
  entry: PendingEntry
  onAck: () => void
  isAcking: boolean
}) {
  const idle = formatDuration(entry.idle_ms)

  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-4 py-2 font-mono text-xs text-foreground">{entry.id}</td>
      <td className="px-4 py-2 font-mono text-xs text-foreground">{entry.consumer}</td>
      <td className={`px-4 py-2 text-xs ${entry.idle_ms > 60_000 ? 'text-yellow-400' : 'text-foreground'}`}>
        {idle}
      </td>
      <td className={`px-4 py-2 text-xs ${entry.delivery_count > 3 ? 'text-red-400' : 'text-foreground'}`}>
        {entry.delivery_count}
      </td>
      <td className="px-4 py-2 text-right">
        <button
          onClick={onAck}
          disabled={isAcking}
          className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground disabled:opacity-50"
        >
          {isAcking ? 'Acking...' : 'Ack'}
        </button>
      </td>
    </tr>
  )
}

/* ---------- Helpers ---------- */

function summarizeData(data: Record<string, unknown>): string {
  const parts: string[] = []
  if (data.type) parts.push(String(data.type))
  if (data.task_id) parts.push(`task:${String(data.task_id).slice(0, 8)}`)
  if (data.project_id) parts.push(`proj:${String(data.project_id).slice(0, 8)}`)
  if (data.story_id) parts.push(`story:${String(data.story_id).slice(0, 8)}`)
  if (data.action) parts.push(String(data.action))
  if (data.status) parts.push(String(data.status))
  if (data.worker_id) parts.push(`worker:${String(data.worker_id).slice(0, 12)}`)
  if (parts.length === 0) {
    const keys = Object.keys(data).slice(0, 3)
    return keys.length > 0 ? keys.join(', ') : '(empty)'
  }
  return parts.join(' | ')
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const sec = Math.floor(ms / 1000)
  if (sec < 60) return `${sec}s`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ${sec % 60}s`
  const hr = Math.floor(min / 60)
  return `${hr}h ${min % 60}m`
}
