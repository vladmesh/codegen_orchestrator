import { Outlet, useNavigate, useLocation } from 'react-router'
import { ArrowLeft, LayoutDashboard } from 'lucide-react'
import { clearToken } from '@/lib/auth'

export default function Layout() {
  const navigate = useNavigate()
  const location = useLocation()
  const isProjectPage = location.pathname.startsWith('/projects/')
    && location.pathname !== '/projects'

  return (
    <div className="min-h-svh bg-background">
      <header className="sticky top-0 z-10 border-b border-border bg-card">
        <div className="mx-auto flex max-w-4xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-3">
            {isProjectPage ? (
              <button
                onClick={() => navigate('/projects')}
                className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
              >
                <ArrowLeft className="h-4 w-4" />
                Проекты
              </button>
            ) : (
              <div className="flex items-center gap-2 font-semibold">
                <LayoutDashboard className="h-5 w-5 text-primary" />
                Мои проекты
              </div>
            )}
          </div>
          <button
            onClick={() => { clearToken(); navigate('/auth') }}
            className="text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            Выйти
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-4xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
