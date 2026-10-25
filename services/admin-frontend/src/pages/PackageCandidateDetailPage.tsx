import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { Card } from '@/components/ui/Card'
import { ApiError, api } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import { candidateDetail } from './packageCandidatesView'

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words text-sm text-foreground">{children}</dd>
    </div>
  )
}

export function PackageCandidateDetailPage() {
  const { id } = useParams<{ id: string }>()
  const candidate = useQuery({
    queryKey: ['package-candidate', id],
    queryFn: async () => candidateDetail(await api.get<unknown>(`/admin/v2/package-candidates/${id}`)),
    enabled: Boolean(id),
  })

  if (candidate.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (candidate.error instanceof ApiError && candidate.error.status === 404) {
    return <p className="text-muted-foreground">Package candidate not found.</p>
  }
  if (candidate.isError || !candidate.data) {
    return <p role="alert" className="text-red-400">Package candidate request failed.</p>
  }

  const detail = candidate.data
  return (
    <div className="min-w-0 space-y-5">
      <div className="space-y-1">
        <div className="text-sm text-muted-foreground"><Link to="/package-candidates" className="hover:underline">Package candidates</Link> / {detail.packageName}</div>
        <h1 className="break-words text-2xl font-bold text-foreground">{detail.packageName} {detail.packageVersion}</h1>
      </div>

      <Card className="border-amber-700/70 bg-amber-950/20">
        <p className="break-words text-sm text-amber-100">This code is unreviewed, exclusive to its product, and awaits the owner's personal code and security review before any release.</p>
      </Card>

      <Card className="min-w-0">
        <dl className="grid min-w-0 gap-x-6 gap-y-4 md:grid-cols-2">
          <Fact label="Product"><Link to={detail.product.href} className="text-primary hover:underline">{detail.product.id}</Link></Fact>
          <Fact label="Story"><Link to={detail.story.href} className="text-primary hover:underline">{detail.story.id}</Link></Fact>
          <Fact label="Source FEATURE"><Link to={detail.sourceTask.href} className="text-primary hover:underline">{detail.sourceTask.id}</Link></Fact>
          <Fact label="INSTALL task"><Link to={detail.installTask.href} className="text-primary hover:underline">{detail.installTask.id}</Link></Fact>
          <Fact label="Repository"><span className="font-mono">{detail.repositoryId}</span></Fact>
          <Fact label="Package path"><span className="font-mono">{detail.path}</span></Fact>
          <Fact label="Package name"><span className="font-mono">{detail.packageName}</span></Fact>
          <Fact label="Distribution"><span className="font-mono">{detail.packageDistribution}</span></Fact>
          <Fact label="Version">{detail.packageVersion}</Fact>
          <Fact label="Full commit"><span className="font-mono">{detail.commit}</span></Fact>
          <Fact label="Core version">{detail.coreVersion}</Fact>
          <Fact label="Python version">{detail.pythonVersion}</Fact>
          <Fact label="Tooling commit"><span className="font-mono">{detail.toolingCommit}</span></Fact>
          <Fact label="Capability">{detail.capability}</Fact>
          <Fact label="Recorded">{formatDate(detail.createdAt)}</Fact>
          <Fact label="Updated">{formatDate(detail.updatedAt)}</Fact>
        </dl>
      </Card>

      <Card className="min-w-0">
        <h2 className="text-lg font-semibold text-foreground">Rationale</h2>
        <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-foreground">{detail.rationale}</p>
      </Card>
    </div>
  )
}
