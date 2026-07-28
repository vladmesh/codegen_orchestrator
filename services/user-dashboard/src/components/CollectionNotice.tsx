import { AlertTriangle } from 'lucide-react'
import type { CollectionHealth } from '@/types/api'

const NOTICE: Record<string, string> = {
  never: 'Сбор аналитики ни разу не отработал. Цифры ниже пустые из-за этого, а не из-за отсутствия трафика.',
  stale: 'Сбор аналитики не работает: последние данные не обновлялись. Показанные цифры устарели.',
}

/** Text for an empty metric block — honest about why it is empty. */
export function emptyDataText(collection: CollectionHealth): string {
  return collection.state === 'ok' ? 'Нет данных' : 'Сбор данных не работает'
}

/** Banner shown whenever analytics collection is not running. */
export default function CollectionNotice({ collection }: { collection: CollectionHealth }) {
  if (collection.state === 'ok') return null

  const since = collection.last_success_at
    ? ` Последний успешный сбор: ${new Date(collection.last_success_at).toLocaleString('ru-RU')}.`
    : ''

  return (
    <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
      <span>{NOTICE[collection.state]}{since}</span>
    </div>
  )
}
