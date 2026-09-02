import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { ChevronDown, ChevronRight, Save, X, Pencil, Check } from 'lucide-react'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { cn, relativeTime } from '@/lib/utils'
import type { AgentConfig, AgentConfigUpdate, ExecutorDiagnosticConfirmation, ExecutorDiagnosticConfirmationCommand, ExecutorDiagnosticSnapshot, ExecutorOverride, PaidWorkControls, SystemConfig, SystemConfigUpdate } from '@/types/api'
import { requiresPaidWorkControlConfirmation, type PaidWorkControlField } from './paidWorkControlTransition'

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

  const protectedKeys = new Set([
    'work_admission.emergency_stop',
    'work_admission.max_concurrent_paid_runs',
    'work_admission.engineering_executor_override',
    'work_admission.qa_executor_override',
  ])
  const editableConfigs = (configs ?? []).filter((config) => !protectedKeys.has(config.key))

  if (!editableConfigs.length) {
    return <PaidWorkControlsCard />
  }

  // Group by category
  const grouped = editableConfigs.reduce<Record<string, SystemConfig[]>>((acc, c) => {
    ;(acc[c.category] ??= []).push(c)
    return acc
  }, {})

  const categories = Object.keys(grouped).sort()

  return (
    <div className="space-y-4">
      <PaidWorkControlsCard />
      {categories.map((cat) => (
        <CategorySection key={cat} category={cat} configs={grouped[cat]} />
      ))}
    </div>
  )
}

function PaidWorkControlsCard() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<PaidWorkControls | null>(null)
  const controlsQuery = useQuery({
    queryKey: ['paid-work-controls'],
    queryFn: () => api.get<PaidWorkControls>('/work-admission/controls'),
  })
  const controls = draft ?? controlsQuery.data
  const mutation = useMutation({
    mutationFn: (next: PaidWorkControls) => api.put<PaidWorkControls, PaidWorkControls>('/work-admission/controls', next),
    onSuccess: (committed) => {
      setDraft(committed)
      queryClient.setQueryData(['paid-work-controls'], committed)
      queryClient.invalidateQueries({ queryKey: ['paid-work-controls'] })
    },
  })

  if (controlsQuery.isLoading || !controls) return <p className="text-muted-foreground">Loading paid-work controls...</p>

  const update = (next: PaidWorkControls, field: PaidWorkControlField) => {
    if (requiresPaidWorkControlConfirmation(field) && !window.confirm('This changes admission for new paid work. Continue?')) return
    mutation.mutate(next)
  }
  const updateOverride = (field: 'engineering_executor_override' | 'qa_executor_override', value: ExecutorOverride) => {
    update({ ...controls, [field]: value }, field)
  }

  return (
    <Card className="space-y-4">
      <div>
        <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">Paid-work controls</h2>
        <p className="mt-1 text-xs text-muted-foreground">Changes apply to new attempts only.</p>
      </div>
      {mutation.isError && <p role="alert" className="text-sm text-red-400">{mutation.error.message || 'Unable to save controls.'}</p>}
      <label className="flex items-center justify-between gap-4 text-sm">
        <span>Emergency stop</span>
        <input
          type="checkbox"
          checked={controls.emergency_stop}
          disabled={mutation.isPending}
          onChange={(event) => update({ ...controls, emergency_stop: event.target.checked }, 'emergency_stop')}
        />
      </label>
      <label className="flex items-center justify-between gap-4 text-sm">
        <span>Maximum concurrent paid runs</span>
        <input
          aria-label="Maximum concurrent paid runs"
          type="number"
          min="0"
          value={controls.max_concurrent_paid_runs}
          disabled={mutation.isPending}
          onChange={(event) => {
            const value = Number(event.target.value)
            if (Number.isInteger(value) && value >= 0) setDraft({ ...controls, max_concurrent_paid_runs: value })
          }}
          onBlur={() => draft && update(draft, 'max_concurrent_paid_runs')}
          className="w-24 rounded border border-border bg-background px-2 py-1 text-right"
        />
      </label>
      <OverrideSelect
        label="Engineering executor override"
        value={controls.engineering_executor_override}
        disabled={mutation.isPending}
        onChange={(value) => updateOverride('engineering_executor_override', value)}
      />
      <OverrideSelect
        label="QA executor override"
        value={controls.qa_executor_override}
        disabled={mutation.isPending}
        onChange={(value) => updateOverride('qa_executor_override', value)}
      />
      <ExecutorDiagnosticsCard />
    </Card>
  )
}

function ExecutorDiagnosticsCard() {
  const queryClient = useQueryClient()
  const [stale, setStale] = useState(false)
  const diagnostics = useQuery({
    queryKey: ['executor-diagnostics'],
    queryFn: () => api.get<ExecutorDiagnosticSnapshot>('/work-admission/executor-diagnostics'),
    refetchInterval: 30_000,
  })
  const confirmation = useMutation({
    mutationFn: (command: ExecutorDiagnosticConfirmationCommand) => api.post<ExecutorDiagnosticConfirmation, ExecutorDiagnosticConfirmationCommand>(
      '/work-admission/executor-diagnostics/confirmations',
      command,
    ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['executor-diagnostics'] }),
  })

  useEffect(() => {
    if (!diagnostics.data) return

    const expiresAt = new Date(diagnostics.data.expires_at).getTime()
    const updateStaleness = () => setStale(expiresAt <= Date.now())
    updateStaleness()
    const timer = window.setTimeout(updateStaleness, Math.max(0, expiresAt - Date.now()))
    return () => window.clearTimeout(timer)
  }, [diagnostics.data])

  if (diagnostics.isLoading) return <p className="text-xs text-muted-foreground">Loading executor diagnostics...</p>
  if (diagnostics.isError || !diagnostics.data) return <p role="alert" className="text-xs text-red-400">Executor diagnostics are unavailable.</p>
  return (
    <section aria-labelledby="executor-diagnostics-heading" className="space-y-2 border-t border-border pt-4">
      <h3 id="executor-diagnostics-heading" className="text-sm font-semibold">Executor diagnostics</h3>
      {stale && <p role="alert" className="text-xs text-amber-400">Diagnostic snapshot is stale. New paid starts will require confirmation.</p>}
      {confirmation.isError && <p role="alert" className="text-xs text-red-400">Unable to confirm the current unknown state.</p>}
      {diagnostics.data.diagnostics.map((item) => (
        <div key={item.executor} className="rounded border border-border p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">{item.executor === 'claude' ? 'Claude' : 'Codex'}</span>
            <StatusBadge status={item.availability} />
          </div>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <dt>Enabled</dt><dd>{item.enabled ? 'Yes' : 'No'}</dd>
            <dt>Auth mode</dt><dd>{item.auth_mode}</dd>
            <dt>Active leases</dt><dd>{item.active_lease_count ?? 'Unknown'}</dd>
            <dt>Observed</dt><dd>{relativeTime(item.observed_at)}</dd>
            <dt>Expires</dt><dd>{relativeTime(item.expires_at)}</dd>
            <dt>Reason</dt><dd>{item.reason}</dd>
          </dl>
          {item.availability === 'unknown' && !stale && (
            <button
              type="button"
              disabled={confirmation.isPending}
            onClick={() => confirmation.mutate({ executor: item.executor, snapshot_version: diagnostics.data.version })}
              className="mt-3 rounded bg-amber-600 px-3 py-1 text-xs text-white disabled:opacity-50"
            >
              {confirmation.isPending ? 'Confirming…' : 'Confirm current unknown state'}
            </button>
          )}
        </div>
      ))}
    </section>
  )
}

function OverrideSelect({ label, value, disabled, onChange }: {
  label: string
  value: ExecutorOverride
  disabled: boolean
  onChange: (value: ExecutorOverride) => void
}) {
  return (
    <label className="flex items-center justify-between gap-4 text-sm">
      <span>{label}</span>
      <select aria-label={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value as ExecutorOverride)} className="rounded border border-border bg-background px-2 py-1">
        <option value="none">No override (use policy)</option>
        <option value="claude">Claude</option>
        <option value="codex">Codex</option>
      </select>
    </label>
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
    mutationFn: (value: unknown) => {
      const update: SystemConfigUpdate = {
        value,
        updated_by: 'admin',
      }
      return api.patch<SystemConfig, SystemConfigUpdate>(`/system-configs/${config.key}`, update)
    },
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
    mutationFn: (update: AgentConfigUpdate) =>
      api.patch<AgentConfig, AgentConfigUpdate>(`/agent-configs/${agent.id}`, update),
    onSuccess: () => {
      setEditing(false)
      queryClient.invalidateQueries({ queryKey: ['agent-configs'] })
    },
  })

  const startEdit = () => {
    setDraft({
      system_prompt: agent.system_prompt,
      model_identifier: agent.model_identifier ?? '',
      temperature: agent.temperature ?? 0,
      is_active: agent.is_active ?? true,
    })
    setEditing(true)
    mutation.reset()
  }

  const save = () => {
    const update: AgentConfigUpdate = {}
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
