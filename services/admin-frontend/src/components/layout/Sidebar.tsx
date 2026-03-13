import { Link, useLocation } from 'react-router'
import { cn } from '@/lib/utils'
import {
  LayoutDashboard,
  FolderKanban,
  ListTodo,
  Container,
  Layers,
  Server,
  ScrollText,
  BrainCircuit,
} from 'lucide-react'

interface NavItem {
  label: string
  path: string
  icon: React.ElementType
  external?: boolean
}

const navItems: NavItem[] = [
  { label: 'Dashboard', path: '/', icon: LayoutDashboard },
  { label: 'Projects', path: '/projects', icon: FolderKanban },
  { label: 'Tasks', path: '/tasks', icon: ListTodo },
  { label: 'Workers', path: '/workers', icon: Container },
  { label: 'Queues', path: '/queues', icon: Layers },
  { label: 'Servers', path: '/servers', icon: Server },
  { label: 'Logs', path: 'http://localhost:3000', icon: ScrollText, external: true },
  { label: 'LLM Tracing', path: 'http://localhost:3002', icon: BrainCircuit, external: true },
]

export function Sidebar() {
  const location = useLocation()

  return (
    <aside className="flex h-full w-56 flex-col border-r border-border bg-sidebar-background">
      <div className="flex h-14 items-center gap-2 border-b border-border px-4">
        <BrainCircuit className="h-6 w-6 text-primary" />
        <span className="text-lg font-semibold text-sidebar-foreground">Orchestrator</span>
      </div>
      <nav className="flex-1 space-y-1 p-2">
        {navItems.map((item) => {
          const isActive =
            !item.external &&
            (item.path === '/'
              ? location.pathname === '/'
              : location.pathname.startsWith(item.path))

          if (item.external) {
            return (
              <a
                key={item.label}
                href={item.path}
                target="_blank"
                rel="noopener noreferrer"
                className={cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm',
                  'text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
                )}
              >
                <item.icon className="h-4 w-4" />
                {item.label}
              </a>
            )
          }

          return (
            <Link
              key={item.label}
              to={item.path}
              className={cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-sm',
                isActive
                  ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                  : 'text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
              )}
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </Link>
          )
        })}
      </nav>
    </aside>
  )
}
