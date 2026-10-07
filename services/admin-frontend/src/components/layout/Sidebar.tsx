import { Link, useLocation } from 'react-router'
import { cn } from '@/lib/utils'
import {
  Siren,
  Route as RouteIcon,
  Map as MapIcon,
  LayoutDashboard,
  FolderKanban,
  ListTodo,
  Container,
  Layers,
  Server,
  ScrollText,
  BrainCircuit,
  Users,
  Settings,
} from 'lucide-react'

interface NavItem {
  label: string
  path: string
  icon: React.ElementType
  disabled?: boolean
}

const consoleItems: NavItem[] = [
  { label: 'Attention', path: '/', icon: Siren },
  { label: 'Journeys', path: '/journeys', icon: RouteIcon },
  { label: 'Atlas', path: '/atlas', icon: MapIcon },
]

const navItems: NavItem[] = [
  { label: 'Overview', path: '/overview', icon: LayoutDashboard },
  { label: 'Users', path: '/users', icon: Users },
  { label: 'Projects', path: '/projects', icon: FolderKanban },
  { label: 'Tasks', path: '/tasks', icon: ListTodo },
  { label: 'Workers', path: '/workers', icon: Container },
  { label: 'Queues', path: '/queues', icon: Layers },
  { label: 'Servers', path: '/servers', icon: Server },
  { label: 'Logs', path: '/logs', icon: ScrollText },
  { label: 'Settings', path: '/settings', icon: Settings },
]

export function Sidebar() {
  const location = useLocation()

  return (
    <aside className="flex h-full w-56 flex-col border-r border-border bg-sidebar-background">
      <div className="flex h-14 items-center gap-2 border-b border-border px-4">
        <BrainCircuit className="h-6 w-6 text-primary" />
        <span className="text-lg font-semibold text-sidebar-foreground">Orchestrator</span>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto p-2">
        {[...consoleItems, null, ...navItems].map((item) => {
          if (item === null) {
            return (
              <div key="reference" className="px-3 pb-1 pt-4 font-mono text-[10.5px] uppercase tracking-wider text-muted-foreground">
                Reference
              </div>
            )
          }
          if (item.disabled) {
            return (
              <span
                key={item.label}
                className="flex items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground/40 cursor-not-allowed"
              >
                <item.icon className="h-4 w-4" />
                {item.label}
                <span className="ml-auto text-xs">soon</span>
              </span>
            )
          }

          const isActive =
            item.path === '/'
              ? location.pathname === '/'
              : location.pathname.startsWith(item.path)

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
