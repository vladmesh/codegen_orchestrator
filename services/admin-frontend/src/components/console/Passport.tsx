import type { ProductPassport } from '@/types/api'
import { Chip, Dot } from './Chips'
import { containerTone } from './tones'

function Column({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 border-b border-border p-4 sm:border-r last:border-r-0">
      <div className="font-mono text-[10.5px] uppercase tracking-wider text-muted-foreground">{title}</div>
      {children}
    </div>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return <span className="text-sm text-muted-foreground">{children}</span>
}

/** What a product is built from, what it connects to and where it runs. */
export function Passport({ passport }: { passport: ProductPassport }) {
  return (
    <section aria-label="Product passport" className="grid overflow-hidden rounded-lg border border-border sm:grid-cols-2 xl:grid-cols-4">
      <Column title="Containers">
        {passport.containers.length === 0 ? (
          <Empty>Not deployed. Modules: {passport.modules.join(', ') || '—'}</Empty>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {passport.containers.map((c) => (
              <li key={`${c.placement}-${c.name}`} className="flex items-center gap-2">
                <Dot tone={containerTone(c.status)} />
                <Chip kind="container">{c.name}</Chip>
                <span className="ml-auto font-mono text-xs text-muted-foreground">
                  {c.port ? `:${c.port}` : c.status}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Column>
      <Column title="Catalog packages">
        {passport.packages.length === 0 ? (
          <Empty>None installed</Empty>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {passport.packages.map((p) => (
              <li key={`${p.kind}-${p.name}`} className="flex items-center gap-2">
                <Chip kind={p.kind}>{p.name}</Chip>
                <span className="ml-auto font-mono text-xs text-muted-foreground">{p.version}</span>
              </li>
            ))}
          </ul>
        )}
      </Column>
      <Column title="Connected">
        <div className="flex flex-wrap gap-1.5">
          {passport.user_secrets.map((name) => (
            <Chip key={name} kind="secret" title="user secret (name only)">
              {name}
            </Chip>
          ))}
        </div>
        {passport.user_secrets.length === 0 && <Empty>No user keys</Empty>}
        <span className="text-xs text-muted-foreground">Platform services: not integrated yet</span>
      </Column>
      <Column title="Runs on">
        {passport.containers.length === 0 ? (
          <Empty>—</Empty>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {[...new Set(passport.containers.map((c) => c.placement))].map((handle) => {
              const here = passport.containers.filter((c) => c.placement === handle)
              const ram = here.reduce((sum, c) => sum + c.reserved_ram_mb, 0)
              const sha = here.find((c) => c.deployed_sha)?.deployed_sha
              return (
                <li key={handle} className="space-y-0.5">
                  <div className="font-mono">{handle}</div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {ram} MB reserved{sha ? ` · ${sha.slice(0, 7)}` : ''}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Column>
    </section>
  )
}
