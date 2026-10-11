// Every fixed text of the dashboard, per language. The language is the browser's:
// Russian for a `ru*` navigator language, English for anything else.

export type Locale = 'en' | 'ru'

const strings = {
  en: {
    appTitle: 'Dashboard',
    projects: 'Projects',
    myProjects: 'My projects',
    logOut: 'Log out',
    signingIn: 'Signing in...',
    linkInvalid: 'This link is not valid. Request a new one in Telegram.',
    tokenInvalid: 'The token is invalid or has expired',
    noProjectData: 'No project data',
    noData: 'No data',
    noServices: 'No services',
    collectionDown: 'Data collection is not working',
    users: 'Users',
    requests: 'Requests',
    errors: 'Errors',
    speed: 'Speed',
    service: 'Service',
    trend: 'Trend',
    services: 'Services',
    topEndpoints: 'Top endpoints',
    byService: 'By service',
    chartCollectionDown: 'Data collection is not working, so there is no chart',
    chartNoData: 'No data for the chart',
    period24h: '24h',
    period7d: '7d',
    period30d: '30d',
    seconds: 's',
    milliseconds: 'ms',
    noticeNever:
      'Analytics collection has never run. The numbers below are empty because of that, not because there is no traffic.',
    noticeStale:
      'Analytics collection is not working: the latest data has not been updated. The numbers shown are out of date.',
    noticeFailing:
      'Analytics collection is failing for this project: the last cycle collected no data. The numbers below are incomplete.',
    lastCycle: 'Last completed collection cycle:',
  },
  ru: {
    appTitle: 'Личный кабинет',
    projects: 'Проекты',
    myProjects: 'Мои проекты',
    logOut: 'Выйти',
    signingIn: 'Входим...',
    linkInvalid: 'Ссылка недействительна. Запросите новую через Telegram.',
    tokenInvalid: 'Токен недействителен или истёк',
    noProjectData: 'Нет данных о проектах',
    noData: 'Нет данных',
    noServices: 'Нет сервисов',
    collectionDown: 'Сбор данных не работает',
    users: 'Пользователи',
    requests: 'Запросы',
    errors: 'Ошибки',
    speed: 'Скорость',
    service: 'Сервис',
    trend: 'Динамика',
    services: 'Сервисы',
    topEndpoints: 'Популярные эндпоинты',
    byService: 'По сервисам',
    chartCollectionDown: 'Сбор данных не работает — графика нет по этой причине',
    chartNoData: 'Нет данных для графика',
    period24h: '24ч',
    period7d: '7д',
    period30d: '30д',
    seconds: 'с',
    milliseconds: 'мс',
    noticeNever:
      'Сбор аналитики ни разу не отработал. Цифры ниже пустые из-за этого, а не из-за отсутствия трафика.',
    noticeStale:
      'Сбор аналитики не работает: последние данные не обновлялись. Показанные цифры устарели.',
    noticeFailing:
      'Сбор аналитики по этому проекту падает: последний цикл не собрал данные. Цифры ниже неполные.',
    lastCycle: 'Последний завершённый цикл сбора:',
  },
} satisfies Record<Locale, Record<string, string>>

export type StringKey = keyof typeof strings.en

/** The locale for a browser language: Russian for `ru*`, English otherwise. */
export function localeFor(language: string | undefined): Locale {
  return language?.toLowerCase().startsWith('ru') ? 'ru' : 'en'
}

export const locale: Locale = localeFor(
  typeof navigator === 'undefined' ? undefined : navigator.language,
)

/** The BCP 47 tag dates and numbers are formatted with. */
export const dateLocale = locale === 'ru' ? 'ru-RU' : 'en-US'

/** One fixed text in the user's locale. */
export function t(key: StringKey): string {
  return strings[locale][key]
}
