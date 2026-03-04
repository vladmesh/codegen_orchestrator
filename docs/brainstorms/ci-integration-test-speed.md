# Brainstorm: CI Integration Test Speed Optimization

> **Дата**: 2026-03-05
> **Контекст**: Интеграционные тесты занимают ~10 минут в CI, хочется сократить
> **Status**: done

---

## Current State

**~50 интеграционных тестов** в 5 compose-стеках + 2 service-стека:

| Стек | Сервисы | Тестов | Тяжёлое |
|------|---------|--------|---------|
| backend | api, db, redis, langgraph, engineering-worker, worker-manager, **DIND** | ~18 | Docker-in-Docker, сборка worker images |
| cli | api, db, redis | ~6 | — |
| template | только test-runner | ~14 | copier clone с GitHub |
| frontend | api, db, redis, telegram-bot | ~1 | — |
| infra | api, db, redis, scheduler | ~2 | — |
| service/api | api, db, redis | ~3 | — |
| service/scheduler | api, db, redis | ~2 | — |

**Всё запускается последовательно** через `make test-integration` → `$(INTEGRATION_TESTS)`.

CI pipeline: `detect-changes → test-unit → test-service → test-integration` (всё sequential).

## Problem

10 минут — это:
- ~2-3 мин на сборку Docker images (каждый стек пересобирает свои)
- ~1-2 мин на healthcheck ожидания (5s interval × 5 retries на каждый стек)
- ~3-4 мин на сами тесты (включая sleep/polling)
- ~1-2 мин на teardown/cleanup

Основная проблема: **последовательный запуск независимых стеков**.

## Options

### Option A: Параллельные CI jobs (matrix strategy)

Разбить `test-integration` на матрицу параллельных jobs:

```yaml
test-integration:
  strategy:
    fail-fast: false
    matrix:
      suite: [backend, cli, template, frontend, infra]
  steps:
    - run: make test-integration-${{ matrix.suite }}
```

- (+) Самый большой выигрыш: 5 стеков параллельно → время = max(backend) ≈ 3-4 мин
- (+) Минимальные изменения: только ci.yml
- (+) Каждый стек в изолированном runner — нет конфликтов ресурсов
- (-) Больше billable minutes (5 runners вместо 1), но wall-clock быстрее
- (-) Нужен Docker cache per-suite (уже есть buildx cache)

### Option B: Кеширование Docker images между стеками

Собрать общие images (api, db, redis) один раз, шарить между стеками.

- (+) Экономит ~30-60с на image build
- (-) Сложная настройка (GHA artifacts для Docker images)
- (-) Не решает главную проблему — последовательность

### Option C: pytest-xdist внутри compose

Запускать тесты внутри одного compose-стека параллельно.

- (+) Ускоряет backend (18 тестов) на 30-50%
- (-) Нужна изоляция между тестами (worker cleanup, Redis streams)
- (-) Не помогает с inter-stack parallelism
- (-) Может быть flaky

### Option D: Объединить мелкие стеки

frontend (1 тест) + infra (2 теста) → один compose-стек.

- (+) Минус один compose up/down цикл (~30с)
- (-) Нужно объединять сервисы, merge compose files
- (-) Marginal gain

## Recommendation

**Option A** — параллельные CI jobs через matrix. Это:
1. Самый большой выигрыш при минимальных изменениях
2. Naturally fits existing Makefile structure (`make test-integration-SUITE`)
3. Уже есть прецедент — `test-service` использует matrix

Дополнительно (quick wins, можно сделать сразу или позже):
- Кешировать copier template локально для template-тестов
- Уменьшить healthcheck intervals (5s→2s) в compose-файлах без DIND

## Action Items

- → backlog #4: обновить описание — добавить "параллельные integration tests через matrix strategy" (уже есть упоминание в brief)
- → new task: "CI: parallelize integration tests via matrix strategy" — разбить test-integration на 5 параллельных jobs
- → new task: "CI: cache copier template for template integration tests" — передавать local path вместо GitHub URL
- → idea: "pytest-xdist для backend integration tests" — исследовать после параллелизации стеков
