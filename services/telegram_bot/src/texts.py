"""Every fixed text the bot shows a user, per language.

The language is the user's Telegram client language (`User.language_code`):
Russian for a code starting with ``ru``, English for anything else, including
no code at all. A language with no table here falls back to `en`, as the brief's
labels do (`shared.product_brief_text.LABELS`).

Texts whose key ends in ``_md`` are Telegram MarkdownV2 and arrive already
escaped; values put into them must be escaped by the caller. Texts ending in
``_html`` are Telegram HTML. Everything else is plain text. Placeholders are
`str.format` fields.
"""

from __future__ import annotations

from telegram import Update

DEFAULT_LOCALE = "en"

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # Keyboards
        "btn_projects_list": "📦 Projects",
        "btn_new_project": "➕ New project",
        "btn_my_dashboard": "📊 My dashboard",
        "btn_servers_list": "🖥️ Servers",
        "btn_add_user": "👤 Add user",
        "btn_main_menu": "← Main menu",
        "btn_back_to_projects": "← Projects",
        "btn_maintenance": "🔧 Maintenance",
        "btn_deploy": "🚀 Deploy",
        "btn_open_dashboard": "📊 Open dashboard",
        # Commands
        "start_html": (
            "🏠 <b>Main menu</b>\n\n"
            "Hi! I build and deploy projects for you.\n\n"
            "Choose an action or describe your project in the chat:"
        ),
        "menu_html": "🏠 <b>Main menu</b>\n\nChoose an action:",
        "no_projects": "You have no projects yet.",
        "dashboard_link": (
            "Tap the button to open your dashboard.\nThe link is valid for 5 minutes."
        ),
        "balance_failed": "Could not get your balance. Please try again later.",
        "balance_title": "💰 Development balance",
        "balance_spent": "Spent: {amount}",
        "balance_available": "Available now: {amount}",
        "balance_exhausted": "The limit is used up.",
        "balance_not_enforced": "The limit is currently off.",
        "balance_unlimited": "Available: unlimited — no limit is set.",
        "balance_unknown_cost": (
            "⚠️ Attempts with an unknown cost: {count}. Actual spending may be higher."
        ),
        "balance_incomplete": "⚠️ Some costs are still being settled.",
        "po_timeout": "Timed out waiting for a reply. Please try again later.",
        "message_failed": "Could not process your message. Please try again later.",
        # Registration
        "promo_activated": "Promo code activated. Welcome!",
        "promo_required": "To get started, send your one-time promo code.",
        "promo_first": "Activate your promo code first",
        # Inline menu
        "action_failed_md": "⚠️ Something went wrong\\. Please try again later\\.",
        "no_projects_md": "📊 You have no projects yet\\.",
        "dashboard_link_md": (
            "Tap the button to open your dashboard\\.\nThe link is valid for 5 minutes\\."
        ),
        "main_menu_md": "🏠 *Main menu*\n\nChoose an action:",
        "projects_load_failed_md": "⚠️ Could not load projects",
        "projects_empty_md": (
            "📦 No projects yet\\.\n\nCreate a new project or describe your idea in the chat\\."
        ),
        "projects_list_md": "📦 *Projects:*\n\nTap a project for details:",
        "new_project_md": (
            "➕ *New project*\n\n"
            "Describe your project in the chat\\. For example:\n\n"
            "_Build a weather Telegram bot with /weather and /settings commands_"
        ),
        "project_id_missing_md": "⚠️ Project ID is missing",
        "project_not_found_md": "⚠️ Project {project_id} not found",
        "maintenance_md": (
            "🔧 *Maintenance mode*\n\nDescribe what to change in project `{project_id}`:"
        ),
        "deploy_md": (
            "🚀 *Project deploy*\n\n"
            "To deploy project `{project_id}`, write:\n"
            "_Deploy project {project_id}_"
        ),
        "servers_denied_md": "🚫 *Access denied*\n\nOnly administrators can view servers\\.",
        "admin_denied_md": "🚫 *Access denied*\n\nAdministrators only\\.",
        "servers_load_failed_md": "⚠️ Could not load servers",
        "servers_empty_md": "🖥️ No active servers\\.",
        "servers_title_md": "🖥️ *Servers:*",
        "add_user_prompt_md": (
            "👤 *Add user*\n\nEnter the new user's Telegram ID\\.\nSend /cancel to cancel\\."
        ),
        "no_description": "No description",
        "status_label": "Status",
        "repo_label": "Repo",
        # Admin invite flow
        "add_user_cancelled": "Adding a user was cancelled.",
        "add_user_not_active": "No user is being added.",
        "telegram_id_not_number": "⚠️ Telegram ID must be a number. Try again or /cancel.",
        "promo_minted_html": (
            "✅ Promo code for {telegram_id}: <code>{code}</code>\n"
            "Starting balance: {credits}, reservation per attempt: {reservation}"
        ),
        "missing_system_config": "⚠️ System config {key} is not set. No promo code was issued.",
        "user_exists": "⚠️ User {telegram_id} already exists.",
        "api_error": "⚠️ API error: {status}",
        "api_unreachable": "⚠️ Could not connect to the API.",
    },
    "ru": {
        # Keyboards
        "btn_projects_list": "📦 Список проектов",
        "btn_new_project": "➕ Новый проект",
        "btn_my_dashboard": "📊 Мой дашборд",
        "btn_servers_list": "🖥️ Список серверов",
        "btn_add_user": "👤 Добавить пользователя",
        "btn_main_menu": "← Главное меню",
        "btn_back_to_projects": "← Список проектов",
        "btn_maintenance": "🔧 Maintenance",
        "btn_deploy": "🚀 Deploy",
        "btn_open_dashboard": "📊 Открыть дашборд",
        # Commands
        "start_html": (
            "🏠 <b>Главное меню</b>\n\n"
            "Привет! Я оркестратор для генерации проектов.\n\n"
            "Выберите действие или опишите проект в чате:"
        ),
        "menu_html": "🏠 <b>Главное меню</b>\n\nВыберите действие:",
        "no_projects": "У вас пока нет проектов.",
        "dashboard_link": "Нажмите кнопку, чтобы открыть дашборд.\nСсылка действительна 5 минут.",
        "balance_failed": "Не удалось получить баланс. Попробуйте позже.",
        "balance_title": "💰 Баланс разработки",
        "balance_spent": "Потрачено: {amount}",
        "balance_available": "Доступно сейчас: {amount}",
        "balance_exhausted": "Лимит исчерпан.",
        "balance_not_enforced": "Лимит сейчас отключён.",
        "balance_unlimited": "Доступно: без ограничений — лимит не установлен.",
        "balance_unknown_cost": (
            "⚠️ Попыток с неизвестной стоимостью: {count}. Фактические расходы могут быть выше."
        ),
        "balance_incomplete": "⚠️ Часть расходов ещё уточняется.",
        "po_timeout": "Таймаут ожидания ответа. Попробуйте позже.",
        "message_failed": "Не удалось обработать сообщение. Попробуйте позже.",
        # Registration
        "promo_activated": "Промокод активирован. Добро пожаловать!",
        "promo_required": "Чтобы начать, пришлите одноразовый промокод.",
        "promo_first": "Сначала активируйте промокод",
        # Inline menu
        "action_failed_md": "⚠️ Не удалось выполнить действие\\. Попробуйте позже\\.",
        "no_projects_md": "📊 У вас пока нет проектов\\.",
        "dashboard_link_md": (
            "Нажмите кнопку, чтобы открыть дашборд\\.\nСсылка действительна 5 минут\\."
        ),
        "main_menu_md": "🏠 *Главное меню*\n\nВыберите действие:",
        "projects_load_failed_md": "⚠️ Не удалось загрузить проекты",
        "projects_empty_md": (
            "📦 Проектов пока нет\\.\n\nСоздайте новый проект или опишите идею в чате\\."
        ),
        "projects_list_md": "📦 *Проекты:*\n\nНажмите на проект для подробностей:",
        "new_project_md": (
            "➕ *Новый проект*\n\n"
            "Опишите ваш проект в чате\\. Например:\n\n"
            "_Создай телеграм\\-бота для погоды с командами /weather и /settings_"
        ),
        "project_id_missing_md": "⚠️ ID проекта не указан",
        "project_not_found_md": "⚠️ Проект {project_id} не найден",
        "maintenance_md": (
            "🔧 *Режим обслуживания*\n\nОпишите, что нужно изменить в проекте `{project_id}`:"
        ),
        "deploy_md": (
            "🚀 *Деплой проекта*\n\n"
            "Для запуска деплоя проекта `{project_id}` напишите:\n"
            "_Задеплой проект {project_id}_"
        ),
        "servers_denied_md": (
            "🚫 *Доступ запрещён*\n\nПросмотр серверов доступен только администраторам\\."
        ),
        "admin_denied_md": "🚫 *Доступ запрещён*\n\nДоступно только администраторам\\.",
        "servers_load_failed_md": "⚠️ Не удалось загрузить серверы",
        "servers_empty_md": "🖥️ Активных серверов нет\\.",
        "servers_title_md": "🖥️ *Серверы:*",
        "add_user_prompt_md": (
            "👤 *Добавить пользователя*\n\n"
            "Введите Telegram ID нового пользователя\\.\n"
            "Отправьте /cancel для отмены\\."
        ),
        "no_description": "Нет описания",
        "status_label": "Статус",
        "repo_label": "Репо",
        # Admin invite flow
        "add_user_cancelled": "Добавление пользователя отменено.",
        "add_user_not_active": "Нет активного добавления пользователя.",
        "telegram_id_not_number": (
            "⚠️ Telegram ID должен быть числом. Попробуйте ещё раз или /cancel."
        ),
        "promo_minted_html": (
            "✅ Промокод для {telegram_id}: <code>{code}</code>\n"
            "Стартовый баланс: {credits}, резерв на попытку: {reservation}"
        ),
        "missing_system_config": "⚠️ Не задан системный конфиг {key}. Промокод не выпущен.",
        "user_exists": "⚠️ Пользователь {telegram_id} уже существует.",
        "api_error": "⚠️ Ошибка API: {status}",
        "api_unreachable": "⚠️ Ошибка соединения с API.",
    },
}


def locale_for(language_code: str | None) -> str:
    """Russian for a Telegram language code starting with ``ru``, English otherwise."""
    if language_code is not None and language_code.lower().startswith("ru"):
        return "ru"
    return DEFAULT_LOCALE


def user_locale(update: Update) -> str:
    """The locale of the user an update comes from; English when it has no user."""
    user = update.effective_user
    return locale_for(user.language_code if user is not None else None)


def text(locale: str, key: str, /, **values: object) -> str:
    """One fixed text in `locale` (falling back to `en`), with `values` filled in."""
    template = TEXTS.get(locale, TEXTS[DEFAULT_LOCALE])[key]
    return template.format(**values) if values else template
