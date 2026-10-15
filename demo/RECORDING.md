# Запись сцены 1 — шпаргалка

Дашборд Brimos (мок, сцена 1 «бот с нуля») + бот-актёр `@brimos_demo_bot`. Дашборд не
знает о боте: его запускает оператор одной командой в момент нажатия Confirm.

## Открыть

- В браузере с Tailscale: **https://secretary-pulse.tail6e5ce6.ts.net:8443/**
  (сертификат валидный, только внутри tailnet).
- Запасной вариант без Tailscale: `ssh -N -L 8790:127.0.0.1:8790 vlad@185.5.53.105`,
  затем http://127.0.0.1:8790/ (ключ vlad на этом сервере есть).
- Окно браузера 1920×1080, масштаб 100 %, полноэкранный режим (F11), курсор убрать с карты.
- Управление сценой — на сервере (`ssh`, под пользователем dev):
  `cd /home/dev/work/brimos-dashboard-demo/demo`, дальше `./democtl …`.

## Перед каждым дублем

1. `./democtl reset 1` — сцена 1, дашборд вернётся к четырём старым продуктам, новой Journey нет
   (просто `./democtl reset` сбрасывает текущую сцену; после `reset 2` текущая — сцена 2).
2. В боте `/scene1` (если чат был переключён на сцену 2), затем `/reset`.
3. В браузере открыть **Journeys** и обновить страницу (F5).
4. `./democtl state` должен показать `"running": false`.

## В кадре

1. Telegram: пишешь заказ, отвечаешь на 3 вопроса, видишь бриф.
2. Нажимаешь **✅ Confirm brief** и в ту же секунду оператор запускает
   `./democtl go`. Это одна команда: reset + start на 1×.
   Опоздали на несколько секунд? Тогда `./democtl go 5`: сцена стартует с 5-й секунды.
3. Дальше бот и дашборд идут параллельно 10 минут. Экраны открывай по таблице ниже.

| Секунда | Экран | Что видно |
|---|---|---|
| 0–8 | **Journeys** | вверху появляется «Water reminder bot with daily streak» |
| 8 | клик по ней → **Journey** | таймлайн, Architect планирует, в Live log идёт чтение брифа |
| 20–30 | Journey | в шапке «7 requirements ▾»: по клику раскрывается список из 7 требований |
| 33–39 | Journey | 3 задачи по одной падают в Backlog |
| 50–140 | Journey | Developer 1 и 2 пишут код, карточки едут в In Dev → CI → Done |
| 140–175 | Journey, прокрутить вниз к **Repository** | дерево файлов растёт, справа дифф |
| 175–205 | клик по «Water reminder bot…» в крошках → **story** | канбан и лог на странице story |
| 205–222 | **Workers** (сайдбар) | активные разработчики и их задачи |
| 222–250 | клик **Developer 1** → страница воркера | живой лог и дерево воркера |
| 250–395 | **Journeys** → Journey | 290 PR #1, 292 CI run 1; **~360–385 CI падает**: test_streak_resets_at_midnight |
| 395–420 | **Attention** (сайдбар) | «CI failed on PR #1», кнопка Open journey |
| 420–470 | **Open journey** | fix-задача (красная полоса), Developer 3; в полосе PR + CI: CI ✗ → rework → 451 CI run 2 |
| 470–492 | Journey, Repository | дифф `local_day()` в bot/streak.py; 482 CI зелёный, 488 merge |
| 492–565 | Journey, наверх | 492 Deploy, 545 контейнер running, 553 QA проверяет live |
| 565–582 | **Atlas** | Water Reminder Bot на edge-eu-2 |
| 582–600 | клик по **Water Reminder Bot** → паспорт | 597 Live, ссылка на бота; в Telegram «Your bot is live! 🎉» |

Синхронизация с ботом: этапы бота (Planning → Building → Testing → Deploying →
Verifying) в среднем меняются на 57 / 329 / 485 / 565 с, у дашборда — на 49 / 322 / 490 / 553 с.
Бот каждый дубль добавляет разброс ±10–20 с, поэтому стыки могут разойтись на 10–20 с.
Это нормально, на видео незаметно. Если разрыв больше:
- `./democtl state` → `bot_stage` показывает этап, на котором сейчас дашборд;
- `./democtl seek 330` перемотает дашборд к нужной секунде (например, к началу Testing). Вперёд прыгать можно,
  экраны догонят за 1–2 с.

Разделители панелей можно таскать (таймлайн | детали, канбан | лог, дерево | дифф). Раскладка
запоминается в браузере; перед записью двойной клик по разделителю возвращает исходную. На
узком экране панели идут друг под другом. Water Reminder Bot до 598 с в статусе «in development»,
потом «active». Секреты продукта показаны на его карточке в Atlas и на странице проекта.

## Дубль

`./democtl reset`, в боте `/reset`, F5 на Journeys, и снова с заказа. Состояние
браузера сбрасывать не нужно.

## Если что-то пошло не так

- **Страница пустая или «не отвечает»**: `./democtl state`. Если ошибка, перезапусти сервис:
  `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart brimos-dashboard-demo.service`,
  затем `./democtl go <секунда>` с той секунды, где был бот.
- **Не открывается :8443**: проверь, что Tailscale на ноутбуке подключён. На сервере:
  `sudo tailscale serve status` должен показывать строку `:8443 → 127.0.0.1:8790`.
  Если не помогло, используй ssh-туннель (см. выше).
- **Забыли `go` в момент Confirm**: `./democtl go N`, где N — сколько секунд прошло.
- **Нужна пауза/репетиция**: `./democtl start 4` (в 4 раза быстрее) или
  `./democtl seek 380` (сразу к падению CI). Перед настоящим дублем — `reset`.
- **Бот завис или не отвечает**: в Telegram `/reset`. Сервис бота не трогаем, он чужой.

Проверено 13.10: прогон 1× целиком (видео `shots/final/`), кадры там же.

---

# Запись сцены 2 — «переиспользование модуля + платформенный сервис»

Клиент Dana заказывает «weekly digest of competitors' Telegram channels». Архитектор на шаге
**Catalog** находит готовый модуль `tg-channels 0.1.2` (из Morning Tech Digest) и подключает
платформенный сервис `tg-reader`, поэтому задач 2 вместо 3, сцена 4 минуты (240 с) вместо 10.
В конце новый код ранжирования уходит в каталог модулем `tg-post-ranking 0.1.0`. Пользователь и PO
про модули не говорят — переиспользование видно только в разбивке архитектора, на шаге Catalog,
в паспорте продукта и в Atlas. CI проходит с первого раза, human-in-the-loop нет.

## Перед каждым дублем

1. `./democtl reset 2` — сцена 2: пять старых продуктов (вкл. Water Reminder Bot), новой Journey нет,
   у `tg-reader` один потребитель (Morning Tech Digest); на 33 с становится `tg-reader · 2`. `go`/`start`/`seek` дальше играют сцену 2,
   пока не сделаешь `reset 1`.
2. В боте `/scene2` (один раз на чат, запоминается), затем `/reset`. `/scene` — какая сцена у чата.
3. Journeys, F5. `./democtl state` → `"scene": "2", "running": false`.

## В кадре

1. Telegram: заказ «I need a bot that sends me a weekly summary of what my competitors post in their
   Telegram channels», 3 вопроса (каналы → `@northroast, @beanbox_daily and @kaffa_lab`; что считать
   важным → `Top 5 posts per channel by views, and flag anything that took off`; когда →
   `Mondays at 9:00`), бриф.
2. **✅ Confirm brief** + в ту же секунду `./democtl go`.
3. 4 минуты параллельно, экраны по таблице.

| сек | экран | что видно |
|---|---|---|
| 0–10 | **Journeys** | вверху «Weekly digest of competitors' Telegram channels», in progress |
| 10–25 | клик → **Journey** | Plan активен; 12 с «6 requirements» (раскрываются); полоса «Reused, not built» пока «checking the catalog…» |
| 22–40 | Journey, полоса над канбаном | 22 `tg-channels 0.1.2 · from Morning Tech Digest`, 24 `tg-reader · platform service`; Catalog активен (25–38), в логе architect: «2 tasks instead of 3» |
| 30–40 | клик по шагу **Catalog** | детали: Package tg-channels 0.1.2, Platform service tg-reader · access granted, Reused from Morning Tech Digest |
| 40–60 | Journey, канбан + лог | 2 карточки: «Rank posts by views…», «Monday digest and /add, /remove commands»; обе In dev |
| 60–100 | Journey, **Repository** (скролл вниз) | bot/ranking.py, bot/digest.py; в логе dev 1 локальные тесты 2 failed → правка → passed (это не CI) |
| 98–131 | Journey | карточки уходят в CI → Done (110 и 129) |
| 132–165 | Journey, **PR + CI** | PR #1, 160 CI run 1 «11 / 11 tests passed», merge |
| 167–198 | Journey, **Deploy** | edge-eu-2, контейнер competitor-digest-bot, 198 Health 200 OK |
| 200–226 | Journey, **QA** | QA worker, 225 «7 / 7 passed» |
| 214–226 | Journey, внизу **Pull requests / паспорт** | 214 PR «tg-post-ranking 0.1.0» в brimos-catalog; 226 Catalog → Published tg-post-ranking 0.1.0 |
| 226–232 | Journey, **паспорт** внизу | Modules: tg-channels 0.1.2 (reused), tg-post-ranking 0.1.0 (new); Platform services: tg-reader |
| 228–240 | **Atlas** | 228 Live; 230 Competitor Digest «active» (до этого «in development»); `tg-reader · 2` (второй потребитель добавлен ещё на 33 с, когда Catalog дал доступ); клик по tg-reader → подсвечены Morning Tech Digest и Competitor Digest |
| 235–240 | клик по **Competitor Digest** → паспорт | модули, платформенный сервис, секрет; в Telegram «Your bot is live! 🎉» |

Синхронизация: этапы бота сцены 2 (script2.yaml, 240 с) в среднем меняются на 46 / 135 / 171 / 201 с,
у дашборда — на 41 / 134 / 170 / 204 с; разброс бота ±10–15 с. Догнать — `./democtl seek <сек>`.
Репетиция — `./democtl start 4` (1 минута). Кадры всех ключевых экранов: `demo/shots/scene2/`
(снимаются `/tmp/pdfenv/bin/python demo/shots_scene2.py`, оставляет reset сцены 2).

Бот: тот же `@brimos_demo_bot`, сцена выбирается на чат командой `/scene2` / `/scene1`.
Нужен код бота с поддержкой сцен (коммит в brimos-demo-bot от 15.10) — сервис бота должен быть
перезапущен после него один раз: `systemctl --user restart brimos-demo-bot`. Ссылка в финале —
`t.me/brimos_demo_second_part_bot` (бот с таким именем должен существовать к съёмке).
