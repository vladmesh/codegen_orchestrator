import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import { candidateList } from './packageCandidatesView'

export function PackageCandidatesPage() {
  const candidates = useQuery({
    queryKey: ['package-candidates'],
    queryFn: async () => candidateList(await api.get<unknown>('/admin/v2/package-candidates')),
  })

  if (candidates.isLoading) return <p className="text-muted-foreground">Loading…</p>
  if (candidates.isError || !candidates.data) {
    return <p role="alert" className="text-red-400">Package candidates request failed.</p>
  }
  if (candidates.data.length === 0) {
    return <p className="rounded-lg border border-border p-6 text-muted-foreground">No package candidates recorded.</p>
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Package candidates</h1>
        <p className="mt-1 text-sm text-muted-foreground">Unreviewed packages that remain exclusive to their product.</p>
      </div>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[960px] text-sm">
          <thead>
            <tr className="border-b border-border text-left font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              <th className="px-3 py-2 font-medium">Package</th>
              <th className="px-3 py-2 font-medium">Product</th>
              <th className="px-3 py-2 font-medium">Story</th>
              <th className="px-3 py-2 font-medium">Commit</th>
              <th className="px-3 py-2 font-medium">Path</th>
              <th className="px-3 py-2 font-medium">Recorded / updated</th>
            </tr>
          </thead>
          <tbody>
            {candidates.data.map((candidate) => (
              <tr key={candidate.id} className="border-b border-border/60 align-top last:border-b-0">
                <td className="px-3 py-2.5">
                  <Link to={`/package-candidates/${candidate.id}`} className="font-medium text-primary hover:underline">
                    {candidate.packageName} {candidate.packageVersion}
                  </Link>
                </td>
                <td className="px-3 py-2.5"><Link to={candidate.product.href} className="text-primary hover:underline">{candidate.product.id}</Link></td>
                <td className="px-3 py-2.5"><Link to={candidate.story.href} className="text-primary hover:underline">{candidate.story.id}</Link></td>
                <td className="px-3 py-2.5 font-mono text-muted-foreground">{candidate.shortCommit}</td>
                <td className="max-w-xs break-all px-3 py-2.5 font-mono text-muted-foreground">{candidate.path}</td>
                <td className="whitespace-nowrap px-3 py-2.5 text-muted-foreground">{formatDate(candidate.createdAt)} / {formatDate(candidate.updatedAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
