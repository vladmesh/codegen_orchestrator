---
id: bs-9c41af07
status: draft
title: "A bot is a service, not an application — project/service/bot cardinality"
created_at: 2026-07-24T15:00:00.000000Z
---

# Brainstorm: «Бот» — это сервис, а не Application

> **Дата**: 2026-07-24
> **Контекст**: во время первого Telegram E2E всплыло, что `bot_username` и
> `acceptance_criteria` хранятся на Repository, и разбор упёрся в вопрос «а бот это вообще
> что». Оказалось — не первоклассная сущность.
> **Связано с**: [domain-model-project-service-split.md](domain-model-project-service-split.md),
> [domain-model-project-vs-service-status-split.md](domain-model-project-vs-service-status-split.md),
> [project-vs-repository-entity-model.md](project-vs-repository-entity-model.md),
> [failure-provenance-and-steward-loop.md](failure-provenance-and-steward-loop.md)
> **Status**: draft — зафиксировать мысль, не переносить сейчас

---

## Текущая топология

```
Project ──1:1── Repository ──1:1── Application ──1:N── PortAllocation
                (service-template)   (стек на сервере)   (по одному на модуль)
```

- **Application** — весь развёрнутый compose-стек: `service_name` = слаг проекта,
  `repo_id` + `server_handle`, внутри `port_allocations` по одной на модуль. У бота-эхо:
  один Application, аллокации `backend` и `tg_bot`.
- **Модули** — enum-множество `ServiceModule = {backend, tg_bot, notifications, frontend}`,
  поле `modules: list[ServiceModule]`. То есть `tg_bot` в проекте — 0 или 1, не больше.
- **db** — контейнер внутри того же стека, общий для backend и tg_bot.

## Что такое «бот»

Не Application: Application — это стек целиком (бот + backend + db + redis). Бот — это
**`tg_bot`-модуль (микросервис) внутри Application**, и он не первоклассная сущность: нет
таблицы «бот», есть строка `tg_bot` в списке модулей и один скалярный `bot_username`. Бот
существует только как элемент enum-списка плюс одно строковое поле.

## Кардинальность: как есть и как надо

| Сущность | Сейчас | «Несколько ботов, общая db» требует |
|---|---|---|
| Project | 1 | 1 |
| Application (стек, общая db) | 1 | 1 |
| bot-сервис | 0..1 | 0..N |
| `bot_username` | 1 скаляр | по одному на bot-сервис |

Сценарий «проект с несколькими связанными ботами, читающими одну базу» — это естественная
модель «бот = деплоимый сервис»: один Application (один стек, одна db, возможно общий backend)
и несколько bot-сервисов, у каждого свой токен/username, все ходят в общую `db`. Сегодня это
**невыразимо**: `modules` — множество с единственным `tg_bot`, `bot_username` скалярный,
service-template даёт ровно один `tg_bot`-сервис.

## Целевая модель

Бот — **first-class deployable service** (микросервис внутри Application) со своей
идентичностью: token, username, статус, health. Application остаётся стеком с общей
инфраструктурой (db, redis) и содержит 0..N сервисов, часть которых — боты. Тогда:

- «N ботов на одной базе» выражается тривиально — N bot-сервисов, одна db в стеке;
- `bot_username` живёт на bot-сервисе, а не на Repository и не на Project (у проекта тоже
  может быть несколько ботов, так что перенос «на Project» проблему кардинальности не решает);
- у каждого бота свой health/статус, а не «статус проекта» скопом.

## Почему всплыло сейчас (симптомы)

- `bot_username` на Repository: не на своём владельце И единственное число там, где реальная
  кардинальность 0..N. Перенос на Project не лечит — нужна per-bot сущность.
- `acceptance_criteria` дублируется на task/story/repository, QA читает копию с Repository
  (см. [failure-provenance-and-steward-loop.md]); это та же болезнь — данные оседают там, где
  удобно прочитать на нужной стадии, а не там, где живут по смыслу.

Обе истории — частные случаи отсутствия разделения Project / Application / Service(Bot).
Три существующих брейншторма про project↔service↔repository split уже про это; недостающий
кусок — явная сущность «сервис/бот» с 0..N на Application и переносом идентичности на неё.

## Открытые вопросы

- Границы Application: один стек = одна db всегда? Или бывает «несколько ботов, каждый со
  своей db, но один проект»? От этого зависит, db-per-Application или db-per-service.
- service-template: как описать N однотипных сервисов (N ботов) — повторяемый модуль с
  инстансами против плоского enum модулей.
- Совместимость: сегодня Project≈Application≈Repository 1:1:1; миграция должна оставить
  однобот-проекты работающими без изменений.

Не переносим сейчас — это большая арка с миграцией. Заметка, чтобы мысль не потерялась.
