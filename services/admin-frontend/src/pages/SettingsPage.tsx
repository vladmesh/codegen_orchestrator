import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { ChevronDown, ChevronRight, Save, X, Pencil, Check } from 'lucide-react'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { cn, relativeTime } from '@/lib/utils'
import type { SystemConfig, AgentConfig } from '@/types/api'

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

const TABS = ['System Configs', 'Agent Configs'] as const
type Tab = (typeof TABS)[number]

export function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab = (searchParams.get('tab') as Tab) || 'System Configs'

  const setTab = (tab: Tab) => setSearchParams({ tab })

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-foreground">Settings</h1>

      {/* Tab bar */}
      <div className="flex gap-4 border-b border-border">
        {TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setTab(tab)}
            className={cn(
              'pb-2 text-sm font-medium transition-colors',
              activeTab === tab
                ? 'border-b-2 border-primary text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {tab}
          </button>
        ))}
      </div>

      {activeTab === 'System Configs' && <SystemConfigsTab />}
      {activeTab === 'Agent Configs' && <AgentConfigsTab />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// System Configs Tab
// ---------------------------------------------------------------------------

function SystemConfigsTab() {
  const { data: configs, isLoading } = useQuery({
    queryKey: ['system-configs'],
    queryFn: () => api.get<SystemConfig[]>('/system-configs/'),
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!configs?.length) return <p className="text-muted-foreground">No configs found</p>

  // Group by category
  const grouped = configs.reduce<Record<string, SystemConfig[]>>((acc, c) => {
    ;(acc[c.category] ??= []).push(c)
    return acc
  }, {})

  const categories = Object.keys(grouped).sort()

  return (
    <div className="space-y-4">
      {categories.map((cat) => (
        <CategorySection key={cat} category={cat} configs={grouped[cat]} />
      ))}
    </div>
  )
}

function CategorySection({ category, configs }: { category: string; configs: SystemConfig[] }) {
  const [expanded, setExpanded] = useState(true)

  return (
    <Card className="p-0">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 px-6 py-4 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted-foreground" />
        )}
        <span className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
          {category}
        </span>
        <span className="text-xs text-muted-foreground/60">{configs.length} configs</span>
      </button>

      {expanded && (
        <div className="border-t border-border">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-6 py-2 font-medium">Key</th>
                <th className="px-6 py-2 font-medium">Value</th>
                <th className="px-6 py-2 font-medium">Description</th>
                <th className="px-6 py-2 font-medium">Updated</th>
                <th className="w-24 px-6 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {configs
                .sort((a, b) => a.key.localeCompare(b.key))
                .map((c) => (
                  <ConfigRow key={c.key} config={c} />
                ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function ConfigRow({ config }: { config: SystemConfig }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const mutation = useMutation({
    mutationFn: (value: unknown) =>
      api.patch<SystemConfig>(`/system-configs/${config.key}`, {
        value,
        updated_by: 'admin',
      }),
    onSuccess: () => {
      setEditing(false)
      queryClient.invalidateQueries({ queryKey: ['system-configs'] })
    },
  })

  const startEdit = () => {
    setDraft(JSON.stringify(config.value))
    setEditing(true)
    mutation.reset()
  }

  const save = () => {
    try {
      const parsed = JSON.parse(draft)
      mutation.mutate(parsed)
    } catch {
      // If not valid JSON, try as raw string
      mutation.mutate(draft)
    }
  }

  const cancel = () => {
    setEditing(false)
    mutation.reset()
  }

  // Short key: strip category prefix
  const shortKey = config.key.replace(`${config.category}.`, '')

  return (
    <tr className="border-b border-border last:border-0 hover:bg-muted/30">
      <td className="px-6 py-3 text-sm font-mono text-foreground">{shortKey}</td>
      <td className="px-6 py-3 text-sm">
        {editing ? (
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
              if (e.key === 'Escape') cancel()
            }}
            autoFocus
            className="w-full rounded border border-border bg-background px-2 py-1 font-mono text-sm text-foreground"
          />
        ) : (
          <span className="font-mono text-foreground">{JSON.stringify(config.value)}</span>
        )}
      </td>
      <td className="px-6 py-3 text-xs text-muted-foreground">
        {config.description || '—'}
      </td>
      <td className="px-6 py-3 text-xs text-muted-foreground">
        {relativeTime(config.updated_at)}
      </td>
      <td className="px-6 py-3">
        {editing ? (
          <div className="flex items-center gap-1">
            <button
              onClick={save}
              disabled={mutation.isPending}
              className="rounded p-1 text-green-400 hover:bg-green-900/30 disabled:opacity-50"
              title="Save"
            >
              <Check className="h-4 w-4" />
            </button>
            <button
              onClick={cancel}
              className="rounded p-1 text-muted-foreground hover:bg-muted/50"
              title="Cancel"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <button
            onClick={startEdit}
            className="rounded p-1 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
            title="Edit"
          >
            <Pencil className="h-4 w-4" />
          </button>
        )}
        {mutation.isError && (
          <span className="text-xs text-red-400">Save failed</span>
        )}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Agent Configs Tab
// ---------------------------------------------------------------------------

function AgentConfigsTab() {
  const { data: agents, isLoading } = useQuery({
    queryKey: ['agent-configs'],
    queryFn: () => api.get<AgentConfig[]>('/agent-configs/'),
  })

  if (isLoading) return <p className="text-muted-foreground">Loading...</p>
  if (!agents?.length) return <p className="text-muted-foreground">No agent configs found</p>

  return (
    <div className="space-y-4">
      {agents
        .sort((a, b) => a.id.localeCompare(b.id))
        .map((agent) => (
          <AgentConfigCard key={agent.id} agent={agent} />
        ))}
    </div>
  )
}

function AgentConfigCard({ agent }: { agent: AgentConfig }) {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({
    system_prompt: '',
    model_identifier: '',
    temperature: 0,
    is_active: true,
  })

  const mutation = useMutation({
    mutationFn: (update: Record<string, unknown>) =>
      api.patch<AgentConfig>(`/agent-configs/${agent.id}`, update),
    onSuccess: () => {
      setEditing(false)
      queryClient.invalidateQueries({ queryKey: ['agent-configs'] })
    },
  })

  const startEdit = () => {
    setDraft({
      system_prompt: agent.system_prompt,
      model_identifier: agent.model_identifier,
      temperature: agent.temperature,
      is_active: agent.is_active,
    })
    setEditing(true)
    mutation.reset()
  }

  const save = () => {
    const update: Record<string, unknown> = {}
    if (draft.system_prompt !== agent.system_prompt) update.system_prompt = draft.system_prompt
    if (draft.model_identifier !== agent.model_identifier)
      update.model_identifier = draft.model_identifier
    if (draft.temperature !== agent.temperature) update.temperature = draft.temperature
    if (draft.is_active !== agent.is_active) update.is_active = draft.is_active

    if (Object.keys(update).length === 0) {
      setEditing(false)
      return
    }
    mutation.mutate(update)
  }

  const cancel = () => {
    setEditing(false)
    mutation.reset()
  }

  return (
    <Card className="p-0">
      {/* Header */}
      <button
        onClick={() => {
          setExpanded(!expanded)
          if (!expanded && !editing) startEdit()
        }}
        className="flex w-full items-center gap-3 px-6 py-4 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted-foreground" />
        )}
        <span className="font-semibold text-foreground">{agent.name}</span>
        <span className="font-mono text-xs text-muted-foreground">{agent.id}</span>
        <StatusBadge status={agent.is_active ? 'active' : 'cancelled'} />
        <span className="ml-auto flex items-center gap-4 text-xs text-muted-foreground">
          <span>{agent.model_identifier}</span>
          <span>v{agent.version}</span>
          <span>temp {agent.temperature}</span>
        </span>
      </button>

      {/* Expanded editor */}
      {expanded && (
        <div className="space-y-4 border-t border-border px-6 py-4">
          {/* Model & settings row */}
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Model Identifier
              </label>
              {editing ? (
                <input
                  value={draft.model_identifier}
                  onChange={(e) => setDraft({ ...draft, model_identifier: e.target.value })}
                  className="w-full rounded border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground"
                />
              ) : (
                <p className="font-mono text-sm text-foreground">{agent.model_identifier}</p>
              )}
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Temperature
              </label>
              {editing ? (
                <input
                  type="number"
                  step={0.1}
                  min={0}
                  max={2}
                  value={draft.temperature}
                  onChange={(e) => setDraft({ ...draft, temperature: parseFloat(e.target.value) })}
                  className="w-full rounded border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground"
                />
              ) : (
                <p className="font-mono text-sm text-foreground">{agent.temperature}</p>
              )}
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Active
              </label>
              {editing ? (
                <button
                  onClick={() => setDraft({ ...draft, is_active: !draft.is_active })}
                  className={cn(
                    'rounded px-3 py-1.5 text-sm font-medium',
                    draft.is_active
                      ? 'bg-green-900 text-green-200'
                      : 'bg-zinc-800 text-zinc-400',
                  )}
                >
                  {draft.is_active ? 'Active' : 'Inactive'}
                </button>
              ) : (
                <StatusBadge status={agent.is_active ? 'active' : 'cancelled'} />
              )}
            </div>
          </div>

          {/* System prompt */}
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              System Prompt
            </label>
            {editing ? (
              <textarea
                value={draft.system_prompt}
                onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })}
                rows={16}
                className="w-full rounded border border-border bg-background px-3 py-2 font-mono text-sm leading-relaxed text-foreground"
              />
            ) : (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded bg-muted/30 px-3 py-2 font-mono text-sm leading-relaxed text-foreground">
                {agent.system_prompt}
              </pre>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2">
            {editing ? (
              <>
                <button
                  onClick={save}
                  disabled={mutation.isPending}
                  className="flex items-center gap-1.5 rounded-md bg-blue-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50"
                >
                  <Save className="h-4 w-4" />
                  {mutation.isPending ? 'Saving...' : 'Save'}
                </button>
                <button
                  onClick={cancel}
                  className="rounded-md border border-border px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                onClick={startEdit}
                className="flex items-center gap-1.5 rounded-md border border-border px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                <Pencil className="h-4 w-4" />
                Edit
              </button>
            )}
            {mutation.isError && (
              <span className="text-sm text-red-400">Save failed</span>
            )}
          </div>

          {/* Metadata */}
          <div className="flex gap-6 text-xs text-muted-foreground">
            <span>Provider: {agent.llm_provider}</span>
            <span>Model name: {agent.model_name}</span>
            <span>Updated: {relativeTime(agent.updated_at)}</span>
          </div>
        </div>
      )}
    </Card>
  )
}
