import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import { Chip, Dot, type DotTone } from '@/components/console/Chips'
import { containerTone } from '@/components/console/tones'
import { Passport } from '@/components/console/Passport'
import type { PlacementView, ProductView, Topology } from '@/types/api'
import {
  UNPLACED,
  groupByPlacement,
  highlight,
  packageKind,
  packageUsage,
  productHealth,
  productPlacement,
  secretUsage,
  type Highlight,
  type Selection,
  type SelectionKind,
} from './atlasModel'

type View = 'layers' | 'matrix' | 'product'
const VIEWS: { id: View; label: string }[] = [
  { id: 'layers', label: 'Layers' },
  { id: 'matrix', label: 'Matrix' },
  { id: 'product', label: 'Product' },
]

const healthTone: Record<ReturnType<typeof productHealth>, DotTone> = {
  ok: 'ok',
  warn: 'warn',
  down: 'down',
  idle: 'idle',
}

function placementTone(placement: PlacementView | null): DotTone {
  if (!placement) return 'idle'
  if (['error', 'unreachable'].includes(placement.status)) return 'down'
  if (['active', 'in_use', 'ready'].includes(placement.status)) return 'ok'
  return 'warn'
}

function Band({ title, note, children, tone }: { title: string; note: string; children: React.ReactNode; tone?: string }) {
  return (
    <section className={cn('space-y-2.5 border-b border-border p-4 last:border-b-0', tone)}>
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        <span className="text-xs text-muted-foreground">{note}</span>
      </div>
      {children}
    </section>
  )
}

function RamBar({ placement }: { placement: PlacementView }) {
  const pct = placement.capacity_ram_mb > 0 ? (placement.used_ram_mb / placement.capacity_ram_mb) * 100 : 0
  return (
    <div className="h-1.5 overflow-hidden rounded bg-zinc-800" title={`RAM ${Math.round(pct)}%`}>
      <div className={cn('h-full', pct > 80 ? 'bg-amber-500' : 'bg-zinc-500')} style={{ width: `${Math.min(pct, 100)}%` }} />
    </div>
  )
}

function ProductTile({
  product,
  lit,
  selection,
  select,
}: {
  product: ProductView
  lit: Highlight | null
  selection: Selection | null
  select: (kind: SelectionKind, id: string) => void
}) {
  const selected = selection?.kind === 'product' && selection.id === product.project_id
  const dim = lit !== null && !lit.products.has(product.project_id)
  const hit = lit !== null && lit.products.has(product.project_id) && !selected
  const backend = product.passport.containers.find((c) => c.name === 'backend')
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => select('product', product.project_id)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          select('product', product.project_id)
        }
      }}
      className={cn(
        'flex cursor-pointer flex-col gap-1.5 rounded-lg border border-border bg-card p-2.5 text-left transition-opacity',
        dim && 'opacity-25',
        hit && 'border-teal-400 shadow-[0_0_0_2px_rgba(45,212,191,0.25)]',
        selected && 'border-teal-400 ring-2 ring-teal-400',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400',
      )}
    >
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Dot tone={healthTone[productHealth(product)]} />
        <span className="truncate">{product.title}</span>
        <span className="ml-auto font-mono text-[10.5px] font-normal text-muted-foreground">
          {backend?.port ? `:${backend.port}` : product.status}
        </span>
      </div>
      <div className="flex flex-wrap gap-1">
        {product.passport.containers.length > 0
          ? product.passport.containers.map((c) => (
              <Chip key={c.name} kind="container">
                <Dot tone={containerTone(c.status)} />
                {c.name}
              </Chip>
            ))
          : product.passport.modules.map((m) => (
              <Chip key={m} kind="container">
                {m}
              </Chip>
            ))}
      </div>
      {product.passport.packages.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {product.passport.packages.map((p) => (
            <Chip
              key={p.name}
              kind={p.kind}
              active={selection?.kind === 'package' && selection.id === p.name}
              onClick={() => select('package', p.name)}
            >
              {p.name}
            </Chip>
          ))}
        </div>
      )}
    </div>
  )
}

function PlatformAbsent() {
  return (
    <p className="rounded-md border border-dashed border-violet-700/70 p-3 text-xs text-violet-200/80">
      codegen-platform-services is not integrated yet: the orchestrator reads no auth grants and no{' '}
      <code>platform_key</code> env sources, so no product is shown as connected to a shared service.
      Products reach outside APIs with their own keys, listed below.
    </p>
  )
}

function Layers({
  topology,
  lit,
  selection,
  select,
}: {
  topology: Topology
  lit: Highlight | null
  selection: Selection | null
  select: (kind: SelectionKind, id: string) => void
}) {
  const control = topology.placements.filter((p) => p.role === 'control')
  const groups = groupByPlacement(topology)
  const secrets = secretUsage(topology)
  return (
    <div>
      <Band title="Orchestrator" note="builds and deploys" tone="bg-zinc-900/60">
        <div className="flex flex-wrap gap-2">
          {control.length === 0 && <span className="text-xs text-muted-foreground">No control host is registered.</span>}
          {control.map((p) => (
            <span key={p.handle} className="inline-flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1 text-xs">
              <Dot tone={placementTone(p)} />
              <span className="font-mono">{p.handle}</span>
              <span className="text-muted-foreground">
                {p.cpu_usage_pct !== null ? `CPU ${Math.round(p.cpu_usage_pct)}% · ` : ''}RAM {p.used_ram_mb}/{p.capacity_ram_mb} MB
              </span>
            </span>
          ))}
          <Link to="/workers" className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-accent">workers</Link>
          <Link to="/queues" className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-accent">queues</Link>
        </div>
      </Band>
      <Band title="Platform services" note="run once, shared by products" tone="bg-violet-950/30">
        {topology.platform_services.length === 0 ? (
          <PlatformAbsent />
        ) : (
          <div className="flex flex-wrap gap-2">
            {topology.platform_services.map((s) => (
              <Chip key={s.name} kind="platform">
                {s.name} · {s.products.length}
              </Chip>
            ))}
          </div>
        )}
      </Band>
      <Band title="Products" note="grouped by where they run">
        {groups.length === 0 ? (
          <span className="text-sm text-muted-foreground">No products yet.</span>
        ) : (
          <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(240px,1fr))]">
            {groups.map((group) => (
              <div key={group.handle} className="flex flex-col gap-2">
                <button
                  type="button"
                  onClick={() => select('placement', group.handle)}
                  className={cn(
                    'flex flex-col gap-1 rounded-md px-1 py-0.5 text-left text-xs hover:bg-accent/50',
                    selection?.kind === 'placement' && selection.id === group.handle && 'bg-accent',
                  )}
                >
                  <span className="flex items-center gap-2 font-mono">
                    <Dot tone={placementTone(group.placement)} />
                    {group.handle === UNPLACED ? 'not deployed' : group.handle}
                    {group.placement && (
                      <span className="ml-auto font-sans text-muted-foreground">
                        {group.placement.capacity_cpu} vCPU · {Math.round(group.placement.capacity_ram_mb / 1024)} GB
                      </span>
                    )}
                  </span>
                  {group.placement && <RamBar placement={group.placement} />}
                </button>
                {group.products.length === 0 && <span className="px-1 text-xs text-muted-foreground">empty</span>}
                {group.products.map((product) => (
                  <ProductTile key={product.project_id} product={product} lit={lit} selection={selection} select={select} />
                ))}
              </div>
            ))}
          </div>
        )}
      </Band>
      <Band title="Outside world" note="keys users gave their products (names only)" tone="bg-zinc-900/60">
        <div className="flex flex-wrap gap-1.5">
          {secrets.length === 0 && <span className="text-xs text-muted-foreground">No user keys.</span>}
          {secrets.map((s) => (
            <Chip
              key={s.name}
              kind="secret"
              active={selection?.kind === 'secret' && selection.id === s.name}
              onClick={() => select('secret', s.name)}
            >
              {s.name} <span className="text-zinc-500">{s.count}</span>
            </Chip>
          ))}
        </div>
      </Band>
    </div>
  )
}

function Matrix({ topology, select }: { topology: Topology; select: (kind: SelectionKind, id: string) => void }) {
  const packages = packageUsage(topology)
  const secrets = secretUsage(topology)
  const order = groupByPlacement(topology).flatMap((group) => group.products)
  const cell = 'border-b border-border/60 px-2 py-1.5 text-center'
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-xs">
        <thead>
          <tr className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            <th className="sticky left-0 bg-background px-2 pt-2 text-left" />
            <th className="px-2 pt-2" />
            {packages.length > 0 && <th colSpan={packages.length} className="border-l border-border px-2 pt-2 text-amber-400">catalog</th>}
            {secrets.length > 0 && <th colSpan={secrets.length} className="border-l border-border px-2 pt-2">user keys</th>}
          </tr>
          <tr className="font-mono text-[10.5px] text-muted-foreground">
            <th className="sticky left-0 border-b border-border bg-background px-2 py-1.5 text-left font-medium">product</th>
            <th className="border-b border-border px-2 py-1.5 text-left font-medium">runs on</th>
            {packages.map((p, i) => (
              <th key={p.name} className={cn('border-b border-border px-2 py-1.5 font-medium text-amber-300', i === 0 && 'border-l')}>
                {p.name}
              </th>
            ))}
            {secrets.map((s, i) => (
              <th key={s.name} className={cn('border-b border-border px-2 py-1.5 font-medium', i === 0 && 'border-l')}>
                {s.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {order.map((product) => (
            <tr key={product.project_id} className="hover:bg-accent/40">
              <td className="sticky left-0 border-b border-border/60 bg-background px-2 py-1.5">
                <button type="button" onClick={() => select('product', product.project_id)} className="flex items-center gap-2 hover:underline">
                  <Dot tone={healthTone[productHealth(product)]} />
                  {product.title}
                </button>
              </td>
              <td className="border-b border-border/60 px-2 py-1.5 font-mono text-muted-foreground">
                {productPlacement(product) === UNPLACED ? '—' : productPlacement(product)}
              </td>
              {packages.map((p, i) => {
                const has = product.passport.packages.some((x) => x.name === p.name)
                const lib = packageKind(topology, p.name) === 'library'
                return (
                  <td key={p.name} className={cn(cell, i === 0 && 'border-l border-l-border')}>
                    {has && <span className={cn('inline-block h-2.5 w-2.5 rounded-sm', lib ? 'border border-dashed border-amber-500' : 'bg-amber-500')} />}
                  </td>
                )
              })}
              {secrets.map((s, i) => (
                <td key={s.name} className={cn(cell, i === 0 && 'border-l border-l-border')}>
                  {product.passport.user_secrets.includes(s.name) && <span className="inline-block h-2.5 w-2.5 rounded-sm bg-zinc-400" />}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="font-mono text-[11px] text-muted-foreground">
            <td className="sticky left-0 bg-background px-2 py-1.5">total</td>
            <td />
            {packages.map((p, i) => (
              <td key={p.name} className={cn('px-2 py-1.5 text-center', i === 0 && 'border-l border-border')}>{p.count}</td>
            ))}
            {secrets.map((s, i) => (
              <td key={s.name} className={cn('px-2 py-1.5 text-center', i === 0 && 'border-l border-border')}>{s.count}</td>
            ))}
          </tr>
        </tfoot>
      </table>
      {topology.products.length === 0 && <p className="p-4 text-sm text-muted-foreground">No products yet.</p>}
    </div>
  )
}

function FlowColumn({ title, children, tone }: { title: string; children: React.ReactNode; tone?: string }) {
  return (
    <div className={cn('flex flex-col gap-2 rounded-lg border border-border bg-card p-3', tone)}>
      <div className="font-mono text-[10.5px] uppercase tracking-wider text-muted-foreground">{title}</div>
      {children}
    </div>
  )
}

function Arrow() {
  return <div aria-hidden className="grid place-items-center text-muted-foreground max-lg:rotate-90">→</div>
}

function ProductFocus({ topology, productId, select }: { topology: Topology; productId: string | null; select: (kind: SelectionKind, id: string) => void }) {
  const product = topology.products.find((p) => p.project_id === productId) ?? topology.products[0]
  if (!product) return <p className="p-4 text-sm text-muted-foreground">No products yet.</p>
  const { passport } = product
  const bot = passport.containers.find((c) => c.name === 'tg_bot') ?? (passport.modules.includes('tg_bot') ? null : undefined)
  const app = passport.containers.filter((c) => c.name !== 'tg_bot')
  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <label htmlFor="atlas-product" className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">product</label>
        <select
          id="atlas-product"
          value={product.project_id}
          onChange={(event) => select('product', event.target.value)}
          className="rounded-md border border-border bg-card px-2 py-1 text-sm"
        >
          {topology.products.map((p) => (
            <option key={p.project_id} value={p.project_id}>{p.title}</option>
          ))}
        </select>
        <Dot tone={healthTone[productHealth(product)]} />
        <span className="font-mono text-xs text-muted-foreground">{productPlacement(product)}</span>
        {product.latest_story_id && (
          <Link to={`/journeys/${product.latest_story_id}`} className="rounded border border-border px-2 py-0.5 font-mono text-xs hover:bg-accent">
            latest journey →
          </Link>
        )}
      </div>
      <div className="grid gap-2 lg:grid-cols-[minmax(0,0.8fr)_24px_minmax(0,1.4fr)_24px_minmax(0,1.1fr)_24px_minmax(0,0.9fr)]">
        <FlowColumn title="entry">
          <div className="rounded-md border border-border p-2 text-sm">Telegram users</div>
          {bot !== undefined && <div className="rounded-md border border-border p-2 text-sm">Telegram Bot API</div>}
        </FlowColumn>
        <Arrow />
        <FlowColumn title={`product · ${productPlacement(product) === UNPLACED ? 'not deployed' : productPlacement(product)}`} tone="border-zinc-600">
          {bot !== undefined && (
            <div className="flex items-center gap-2 rounded-md border border-border p-2 text-sm">
              <Dot tone={bot ? containerTone(bot.status) : 'idle'} />
              <Chip kind="container">tg_bot</Chip>
            </div>
          )}
          {app.map((c) => (
            <div key={c.name} className="flex flex-col gap-1.5 rounded-md border border-border p-2 text-sm">
              <div className="flex items-center gap-2">
                <Dot tone={containerTone(c.status)} />
                <Chip kind="container">{c.name}</Chip>
                <span className="ml-auto font-mono text-xs text-muted-foreground">{c.port ? `:${c.port}` : c.status}</span>
              </div>
              {c.name === 'backend' && passport.packages.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {passport.packages.map((p) => (
                    <Chip key={p.name} kind={p.kind}>{p.name}</Chip>
                  ))}
                </div>
              )}
            </div>
          ))}
          {passport.containers.length === 0 && (
            <span className="text-xs text-muted-foreground">Not deployed. Modules: {passport.modules.join(', ') || '—'}</span>
          )}
        </FlowColumn>
        <Arrow />
        <FlowColumn title="platform" tone="border-dashed border-violet-800 bg-violet-950/20">
          <span className="text-xs text-violet-200/70">No platform service is connected; the integration does not exist yet.</span>
        </FlowColumn>
        <Arrow />
        <FlowColumn title="outside, user keys">
          {passport.user_secrets.length === 0 && <span className="text-xs text-muted-foreground">none</span>}
          {passport.user_secrets.map((name) => (
            <Chip key={name} kind="secret">{name}</Chip>
          ))}
        </FlowColumn>
      </div>
      <Passport passport={passport} />
    </div>
  )
}

function SelectionDetail({ topology, selection, lit }: { topology: Topology; selection: Selection | null; lit: Highlight | null }) {
  if (!selection || !lit) {
    return (
      <p className="text-sm text-muted-foreground">
        Click a product, a placement, a catalog package or a user key: everything connected to it lights up.
      </p>
    )
  }
  const users = topology.products.filter((p) => lit.products.has(p.project_id))
  if (selection.kind === 'product') {
    const product = topology.products.find((p) => p.project_id === selection.id)
    if (!product) return null
    return (
      <div className="space-y-3">
        <div>
          <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">product · {product.status}</div>
          <h2 className="text-lg font-semibold">{product.title}</h2>
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="font-mono text-xs text-muted-foreground">runs on</dt>
          <dd className="font-mono">{productPlacement(product)}</dd>
          <dt className="font-mono text-xs text-muted-foreground">containers</dt>
          <dd>{product.passport.containers.length}</dd>
          <dt className="font-mono text-xs text-muted-foreground">packages</dt>
          <dd>{product.passport.packages.length}</dd>
          <dt className="font-mono text-xs text-muted-foreground">user keys</dt>
          <dd>{product.passport.user_secrets.length}</dd>
        </dl>
        <div className="flex flex-wrap gap-2 text-xs">
          <Link to={`/atlas?view=product&product=${product.project_id}`} className="rounded border border-border px-2 py-1 hover:bg-accent">Focus →</Link>
          {product.latest_story_id && (
            <Link to={`/journeys/${product.latest_story_id}`} className="rounded border border-border px-2 py-1 hover:bg-accent">Latest journey</Link>
          )}
          <Link to={`/projects/${product.project_id}`} className="rounded border border-border px-2 py-1 hover:bg-accent">Project</Link>
        </div>
      </div>
    )
  }
  const placement = topology.placements.find((p) => p.handle === selection.id)
  const title = {
    package: packageKind(topology, selection.id) === 'library' ? 'catalog library' : 'catalog package',
    secret: 'user key',
    placement: 'placement',
  }[selection.kind]
  return (
    <div className="space-y-3">
      <div>
        <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">{title}</div>
        <h2 className="break-words font-mono text-lg font-semibold">{selection.id === UNPLACED ? 'not deployed' : selection.id}</h2>
      </div>
      {placement && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="font-mono text-xs text-muted-foreground">status</dt>
          <dd>{placement.status}</dd>
          <dt className="font-mono text-xs text-muted-foreground">address</dt>
          <dd className="font-mono">{placement.public_ip}</dd>
          <dt className="font-mono text-xs text-muted-foreground">RAM</dt>
          <dd>{placement.used_ram_mb} / {placement.capacity_ram_mb} MB</dd>
          <dt className="font-mono text-xs text-muted-foreground">checked</dt>
          <dd>{relativeTime(placement.last_health_check)}</dd>
        </dl>
      )}
      <div>
        <div className="mb-1 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">products · {users.length}</div>
        <ul className="space-y-0.5 text-sm">
          {users.map((p) => (
            <li key={p.project_id}>{p.title}</li>
          ))}
        </ul>
      </div>
    </div>
  )
}

export function AtlasPage() {
  const [params, setParams] = useSearchParams()
  const view = (VIEWS.find((v) => v.id === params.get('view'))?.id ?? 'layers') as View
  const selectionKind = params.get('kind') as SelectionKind | null
  const selectionId = params.get('id')
  const selection: Selection | null = selectionKind && selectionId ? { kind: selectionKind, id: selectionId } : null

  const topology = useQuery({
    queryKey: ['admin-console-topology'],
    queryFn: () => api.get<Topology>('/admin/v2/topology'),
    refetchInterval: 30_000,
  })

  const setView = (next: View) => {
    const updated = new URLSearchParams(params)
    updated.set('view', next)
    setParams(updated, { replace: true })
  }
  const select = (kind: SelectionKind, id: string) => {
    const updated = new URLSearchParams(params)
    const same = selection?.kind === kind && selection.id === id && view !== 'product'
    if (same) {
      updated.delete('kind')
      updated.delete('id')
    } else {
      updated.set('kind', kind)
      updated.set('id', id)
    }
    if (kind === 'product') updated.set('product', id)
    setParams(updated, { replace: true })
  }

  if (topology.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (topology.isError || !topology.data) {
    return <p role="alert" className="text-red-400">Topology request failed.</p>
  }
  const lit = highlight(topology.data, selection)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-foreground">Atlas</h1>
        <span className="text-sm text-muted-foreground">
          {topology.data.products.length} products · {topology.data.placements.filter((p) => p.role === 'product').length} placements
        </span>
        <div className="ml-auto inline-flex overflow-hidden rounded-md border border-border" role="group" aria-label="Atlas view">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              type="button"
              aria-pressed={view === v.id}
              onClick={() => setView(v.id)}
              className={cn(
                'border-r border-border px-3 py-1 text-xs last:border-r-0',
                view === v.id ? 'bg-teal-500 text-zinc-950' : 'text-muted-foreground hover:bg-accent',
              )}
            >
              {v.label}
            </button>
          ))}
        </div>
      </div>
      <div className="grid overflow-hidden rounded-lg border border-border xl:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 border-b border-border xl:border-b-0 xl:border-r">
          {view === 'layers' && <Layers topology={topology.data} lit={lit} selection={selection} select={select} />}
          {view === 'matrix' && <Matrix topology={topology.data} select={select} />}
          {view === 'product' && <ProductFocus topology={topology.data} productId={params.get('product')} select={select} />}
        </div>
        <aside className="bg-zinc-950/60 p-4" aria-live="polite">
          <SelectionDetail topology={topology.data} selection={selection} lit={lit} />
        </aside>
      </div>
    </div>
  )
}
