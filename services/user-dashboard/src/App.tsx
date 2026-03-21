import { BrowserRouter, Routes, Route, Navigate } from 'react-router'
import AuthPage from '@/pages/AuthPage'
import ProjectsPage from '@/pages/ProjectsPage'
import DashboardPage from '@/pages/DashboardPage'
import ProtectedRoute from '@/components/ProtectedRoute'
import Layout from '@/components/Layout'

export default function App() {
  return (
    <BrowserRouter basename="/lk">
      <Routes>
        <Route path="/auth" element={<AuthPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/projects/:id" element={<DashboardPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/projects" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
