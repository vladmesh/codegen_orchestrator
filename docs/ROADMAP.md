# Roadmap

> **Updated**: 2026-07-23 (hand-maintained)
>
> Story-level arcs only. Active tasks live on the external Pipeline board, the deferred pool in
> [backlog.md](backlog.md), sequencing for the current arc in
> [plans/codegen-stabilization-v1.md](plans/codegen-stabilization-v1.md). The internal dogfooding
> generators that used to rebuild this file were removed (`codegen_orchestrator-668`); pre-668
> generated content, including the client-bot story dumps, is in git history.

## Current arc: stabilize core pipeline

Довести генерацию Telegram-ботов до стабильного E2E. Юзер приходит в Telegram → общается с PO →
получает работающего бота за 20-30 минут. Потом просит доработки через диалог → получает
обновлённого бота.

Stages 1-7 of the stabilization plan are complete: CI gate, template contract audit and
corrections, Sprint 002 hardening, deterministic mock smoke, template matrix, and live validation
(Mega 2.0: live LLM worker through generated code, CI, merge, deploy and QA). Next:

- Stage 8: Telegram end-to-end on top of the stabilized layers.
- Stage 9: worker isolation hardening — обязателен до онбординга внешних пользователей.
- Stage 10: swarm seams — по триггеру (второй worker-хост или устойчивая параллельная нагрузка).

Stage 7 tail debt is on the board (600, 548, 676→527, 597, 673) and does not gate Stage 8.

## Next arcs

### Autonomy: smart steward

Пайплайн чинит себя сам, человек — последняя ступень эскалации, а не первая. Пререквизит
(fail-fast и типизированные границы, фазы 2-4 спринта 002) выполнен.

- Починить incident-подсистему infra-service (реализовать client-методы, убрать swallow-обёртки)
- Память фейлов: дистилляция транскриптов ранов в базу знаний для architect и fix-тасков
- Triage-агент: ступень эскалации перед WAITING_HUMAN_REVIEW
- Активная доска: события доски как шина, агенты подписываются, треды на карточках

### Multi-tenant hardening

Реальная изоляция юзеров и проектов до того, как появятся чужие клиенты. Пересекается со
Stage 9/10 плана стабилизации.

- API auth + enforcement owner_id на эндпоинтах (backlog #1022)
- Сетевая изоляция и CPU/RAM-лимиты проектов на общих VPS (backlog #10)
- Parallel Server Provisioning (#41)
- Метеринг стоимости per-user (LLM-токены, серверные ресурсы)
- MicroVM worker runtime / elastic worker hosts — по триггерам (backlog #1050, #1051)

### Product decomposition + Architect node

PO принимает высокоуровневое описание и формулирует продуктовые stories; Architect дробит story
на технические tasks с зависимостями. Юзер видит stories, tasks абстрагированы. Спека:
[PIPELINE_V2.md](PIPELINE_V2.md), brainstorm bs-d302b6a1.

- Architect: story decomposition into tasks (остаток арки)
- Architect: sub-story decomposition — определять, что story слишком большая, дробить или
  возвращать PO на уточнение scope

## Later arcs (укрупнённо, порядок не зафиксирован)

- **Frontend generation** — модуль фронтенда в service-template; описание → сайт с доменом.
- **Post-release testing** — QA через Claude Code на прод-сервере после деплоя: story → TESTING →
  тест по описанию как реальный пользователь → pass/fail loop. Brainstorm bs-eece61a8.
- **Pre-release testing** — feature-стенды, preview environments; E2E completion (#11),
  contract testing (schemathesis).
- **GitHub integration** — юзер подключает свой GitHub, видит репозиторий, может форкнуть.
  Остаток: Repository model в production flows (backlog #1024).
- **Admin dashboard v2/v3** — логи воркеров, вмешательство оператора; затем полная observability
  с алертами. Остатки конфиг-арки: ConfigStore TTL cache, перевод сервисов на ConfigStore.
- **User dashboard** — ЛК для нетехнического фаундера: базовая версия готова (auth через
  Telegram, аналитика из Loki); развитие по мере спроса.
- **Conversation summarization** — сжатие переписки PO↔юзер, контекст-менеджмент.
- **Worker swarm** — параллельные воркеры, переиспользование контейнеров (после Stage 10 seams).
- **Security hardening** — deploy cleanup audit (#7), key encryption (#20), agent hierarchy &
  incident response (#2), rate limiting.
- **Full RAG** — поиск по проекту/докам/переписке для агентов.

## Codegen features (deferred, по мере спроса)

Фичи генератора и service-template, не привязанные к аркам: scaffolder ensure-workspace gate;
eager import chains (backlog #1025); авто-роутеры из domain specs (#1026); make add-module
(#1027); unified handlers error strategy; авто-обновление `__init__.py` re-exports; notifications
через Redis Stream (#26); enum types в model fields; Celery worker support; ddgs rename (#46);
high-level architecture spec; spec-first observability (OpenTelemetry); spec-only module storage;
standardize PYTHONPATH (backlog #1005); integration test scheduler-langgraph lifecycle (#1003).

## Deferred (после product-market fit)

- **Rust migration** — service-template и сгенерированные сервисы на Rust (Axum + SeaORM, Tera);
  сначала language-agnostic YAML-спеки и PoC.
- **Human-in-the-loop** — тарифная модель с эскалацией задач от AI к живым разработчикам.

## Closed arcs

- **Dev process automation** (internal tasks/skills/doc generation) — закрыта `codegen_orchestrator-668`:
  внутренний dogfooding снят, задачи оркестратора идут через внешний пайплайн. Tasks/Stories API
  остаётся для клиентских проектов.
- **Admin dashboard v1** — read-only админка готова.
- **Server & application health monitoring** — node_exporter/cadvisor, health_checker, admin UI
  готовы; остатки drift detection в backlog (#1017, #1018).
- Клиентские боты эпохи dogfooding (LessWrong bot, fortune teller, cat bot, reverse bot и пр.) —
  это client-project stories, а не milestones оркестратора; тексты в git history.
