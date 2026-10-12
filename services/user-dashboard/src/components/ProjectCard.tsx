import { useNavigate } from 'react-router'
import { ChevronRight } from 'lucide-react'
import type { LkProject } from '@/types/api'
import StatusBadge from './StatusBadge'
import MetricValue from './MetricValue'
import { formatNumber, formatMs, formatPercent } from '@/lib/utils'
import CollectionNotice from './CollectionNotice'
import { emptyDataText } from './emptyDataText'
import { t } from '@/lib/i18n'

export default function ProjectCard({ project }: { project: LkProject }) {
  const navigate = useNavigate()
  const d = project.latest_daily

  return (
    <button
      onClick={() => navigate(`/projects/${project.id}`)}
      className="w-full rounded-xl border border-border bg-card p-4 shadow-sm text-left hover:border-primary/30 transition-colors"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">{project.name}</h3>
          <StatusBadge status={project.status} />
        </div>
        <ChevronRight className="h-4 w-4 text-muted-foreground" />
      </div>

      {/* Per-project: a card may be empty because collection failed for it */}
      {project.collection.state !== 'ok' && (
        <div className="mb-3">
          <CollectionNotice collection={project.collection} />
        </div>
      )}

      {d ? (
        <div className="grid grid-cols-4 gap-2">
          <MetricValue label={t('users')} value={formatNumber(d.unique_users)} />
          <MetricValue label={t('requests')} value={formatNumber(d.total_requests)} />
          <MetricValue label={t('speed')} value={formatMs(d.p95_ms)} />
          <MetricValue label={t('errors')} value={formatPercent(d.error_rate)} />
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">{emptyDataText(project.collection)}</p>
      )}
    </button>
  )
}
