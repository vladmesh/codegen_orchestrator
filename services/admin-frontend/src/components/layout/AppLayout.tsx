import { useLayoutEffect, useRef } from 'react'
import { Outlet, useLocation } from 'react-router'
import { isDemo } from '@/lib/utils'
import { Sidebar } from './Sidebar'

export function AppLayout() {
  const main = useRef<HTMLElement>(null)
  const { pathname } = useLocation()
  // Demo: <main> is the scroller and outlives route changes, so a page opened from a
  // scrolled Journey would land mid-page. Every new screen starts at the top.
  useLayoutEffect(() => {
    if (isDemo) main.current?.scrollTo({ top: 0 })
  }, [pathname])
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main ref={main} className="min-w-0 flex-1 overflow-y-auto p-3 sm:p-6">
        <Outlet />
      </main>
    </div>
  )
}
