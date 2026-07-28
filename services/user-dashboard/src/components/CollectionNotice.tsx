import { AlertTriangle } from 'lucide-react'
import type { CollectionHealth, CollectionState } from '@/types/api'

const NOTICE: Record<Exclude<CollectionState, 'ok'>, string> = {
  never: 'Сбор аналитики ни разу не отработал. Цифры ниже пустые из-за этого, а не из-за отсутствия трафика.',
  stale: 'Сбор аналитики не работает: последние данные не обновлялись. Показанные цифры устарели.',
  failing: 'Сбор аналитики по этому проекту падает: последний цикл не собрал данные. Цифры ниже неполные.',
}

/** Banner shown whenever analytics collection is not running. */
export default function CollectionNotice({ collection }: { collection: CollectionHealth }) {
  if (collection.state === 'ok') return null

  const since = collection.last_cycle_at
    ? ` Последний завершённый цикл сбора: ${new Date(collection.last_cycle_at).toLocaleString('ru-RU')}.`
    : ''

  return (
    <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
      <span>{NOTICE[collection.state]}{since}</span>
    </div>
  )
}
