import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { LkProject } from '@/types/api'
import ProjectCard from '@/components/ProjectCard'
import Spinner from '@/components/Spinner'
import ErrorMessage from '@/components/ErrorMessage'

export default function ProjectsPage() {
  const [projects, setProjects] = useState<LkProject[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<LkProject[]>('/lk/projects')
      .then(setProjects)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <div className="flex justify-center py-12"><Spinner /></div>
  }

  if (error) {
    return <ErrorMessage message={error} />
  }

  if (projects.length === 0) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">Нет данных о проектах</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {projects.map((p) => (
        <ProjectCard key={p.id} project={p} />
      ))}
    </div>
  )
}
