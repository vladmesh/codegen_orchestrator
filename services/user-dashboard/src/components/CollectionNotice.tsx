import { AlertTriangle } from 'lucide-react'
import type { CollectionHealth, CollectionState } from '@/types/api'
import { dateLocale, t } from '@/lib/i18n'

const NOTICE: Record<Exclude<CollectionState, 'ok'>, string> = {
  never: t('noticeNever'),
  stale: t('noticeStale'),
  failing: t('noticeFailing'),
}

/** Banner shown whenever analytics collection is not running. */
export default function CollectionNotice({ collection }: { collection: CollectionHealth }) {
  if (collection.state === 'ok') return null

  const since = collection.last_cycle_at
    ? ` ${t('lastCycle')} ${new Date(collection.last_cycle_at).toLocaleString(dateLocale)}.`
    : ''

  return (
    <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
      <span>{NOTICE[collection.state]}{since}</span>
    </div>
  )
}
