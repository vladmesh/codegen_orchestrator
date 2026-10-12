import type { CollectionHealth } from '@/types/api'
import { t } from '@/lib/i18n'

/** Text for an empty metric block — honest about why it is empty. */
export function emptyDataText(collection: CollectionHealth): string {
  return collection.state === 'ok' ? t('noData') : t('collectionDown')
}
