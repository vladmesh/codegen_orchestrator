import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Routes, Route } from 'react-router'
import { AppLayout } from '@/components/layout/AppLayout'
import { DashboardPage } from '@/pages/DashboardPage'
import { ProjectsPage } from '@/pages/ProjectsPage'
import { ProjectDetailPage } from '@/pages/ProjectDetailPage'
import { TasksPage } from '@/pages/TasksPage'
import { TaskDetailPage } from '@/pages/TaskDetailPage'
import { WorkersPage } from '@/pages/WorkersPage'
import { WorkerDetailPage } from '@/pages/WorkerDetailPage'
import { QueuesPage } from '@/pages/QueuesPage'
import { QueueDetailPage } from '@/pages/QueueDetailPage'
import { ServersPage } from '@/pages/ServersPage'
import { LogsPage } from '@/pages/LogsPage'
import { TracingPage } from '@/pages/TracingPage'
import { UsersPage } from '@/pages/UsersPage'
import { UserDetailPage } from '@/pages/UserDetailPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { StoryDetailPage } from '@/pages/StoryDetailPage'
import { ApplicationDetailPage } from '@/pages/ApplicationDetailPage'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route index element={<DashboardPage />} />
            <Route path="projects" element={<ProjectsPage />} />
            <Route path="projects/:id" element={<ProjectDetailPage />} />
            <Route path="tasks" element={<TasksPage />} />
            <Route path="tasks/:id" element={<TaskDetailPage />} />
            <Route path="workers" element={<WorkersPage />} />
            <Route path="workers/:id" element={<WorkerDetailPage />} />
            <Route path="queues" element={<QueuesPage />} />
            <Route path="queues/:stream/:group" element={<QueueDetailPage />} />
            <Route path="users" element={<UsersPage />} />
            <Route path="users/:id" element={<UserDetailPage />} />
            <Route path="stories/:id" element={<StoryDetailPage />} />
            <Route path="applications/:id" element={<ApplicationDetailPage />} />
            <Route path="servers" element={<ServersPage />} />
            <Route path="logs" element={<LogsPage />} />
            <Route path="tracing" element={<TracingPage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
