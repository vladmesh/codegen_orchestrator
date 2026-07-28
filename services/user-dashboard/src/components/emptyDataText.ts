import type { CollectionHealth } from '@/types/api'

/** Text for an empty metric block — honest about why it is empty. */
export function emptyDataText(collection: CollectionHealth): string {
  return collection.state === 'ok' ? 'Нет данных' : 'Сбор данных не работает'
}
