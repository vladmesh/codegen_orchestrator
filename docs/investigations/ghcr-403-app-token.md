# GHCR 403 Forbidden: GitHub App + Organization

> **Дата**: 2026-02-16
> **Статус**: Решено — выбран вариант D (self-hosted registry), реализован в Iteration 7
> **Проект**: codegen_orchestrator (deploy-architecture epic)

## Контекст

Оркестратор автоматически создаёт GitHub-репозитории, пушит код через GitHub App,
запускает CI/CD через GitHub Actions и деплоит на VPS. Всё работает — кроме push
Docker-образов в GitHub Container Registry (GHCR).

## Наш сетап

- **GitHub Organization**: `project-factory-organization` (приватная, платный план)
- **GitHub App**: `project-factory-keeper-v1` — установлена на org, имеет permission `packages: write` (и ~50 других)
- **Репозитории**: создаются App'ом, код пушится App installation token
- **CI workflow** (`ci.yml`): триггерится на `push to main`, job `build-and-push` имеет `permissions: packages: write`
- **Actor в CI**: `project-factory-keeper-v1[bot]` (потому что push сделан App'ом)

### CI workflow (build-and-push job)

```yaml
build-and-push:
  permissions:
    contents: read
    packages: write
  steps:
    - uses: docker/login-action@v3
      with:
        registry: ghcr.io
        username: ${{ github.repository_owner }}
        password: ${{ secrets.GITHUB_TOKEN }}
    - uses: docker/build-push-action@v5
      with:
        push: true
        tags: ghcr.io/project-factory-organization/reverse-message-bot-backend:latest
```

### Org settings

| Setting | Value |
|---------|-------|
| Org → Actions → Workflow permissions | `write` (изменено с `read` в процессе отладки) |
| Repo → Actions → Workflow permissions | `write` (изменено с `read`) |
| Org → Packages → Package creation | `Private` — "Members will be able to create private packages" |
| Org → Packages → Default | "Inherit access from source repository" |

## Проблема

При `docker push` в GHCR — 403 Forbidden:

```
ERROR: failed to push ghcr.io/project-factory-organization/reverse-message-bot-tg-bot:latest:
unexpected status from HEAD request to
https://ghcr.io/v2/project-factory-organization/reverse-message-bot-tg-bot/blobs/sha256:...:
403 Forbidden
```

`docker login` проходит успешно. Билд проходит. Ошибка именно на push (HEAD-запрос проверки blob).

## Что пробовали

| # | Действие | Результат |
|---|----------|-----------|
| 1 | CI с `GITHUB_TOKEN` (org permissions = `read`) | 403 |
| 2 | Изменили org workflow permissions → `write` | — |
| 3 | Изменили repo workflow permissions → `write` | — |
| 4 | CI rerun с `GITHUB_TOKEN` (permissions = `write`) | 403 |
| 5 | CI с App token через `actions/create-github-app-token@v1` | 403 |
| 6 | Локальный `docker push` с App installation token | 403 |
| 7 | CI с `GITHUB_TOKEN` + `attestations: write` + `id-token: write` | 403 |
| 8 | `username: github.repository_owner` вместо `github.actor` | 403 |

Все варианты дают одинаковый 403 на HEAD-запрос при push.

## Анализ

### Почему App installation token не работает с GHCR

GitHub Support (2022) подтвердил:
> "You cannot authenticate with a GitHub App token on the GitHub Package Registry."

Несмотря на то что GitHub App UI позволяет выставить permission `packages: write`,
этот permission работает только для GitHub API (list/delete packages), но **не для
Docker Registry V2 API** (push/pull образов). Это разные auth-системы.

**Источники:**
- [Discussion #24636 — Read GitHub Packages permission for GitHub App?](https://github.com/orgs/community/discussions/24636) — GitHub Support confirmation
- [Discussion #34084 — GitHub Container Registry and GitHub App](https://github.com/orgs/community/discussions/34084) — "login works, pull doesn't"
- [Discussion #26921 — Push or pull on ghcr package results in error when authenticating with an oauth app](https://github.com/orgs/community/discussions/26921)

### Почему GITHUB_TOKEN тоже не работает (в нашем случае)

`GITHUB_TOKEN` — это **не** App token. Это автоматический token, который GitHub Actions
генерирует для каждого workflow run. По документации он **должен** работать для GHCR push.

Но в нашем случае все push'ы в репозиторий идут через GitHub App, поэтому:
- `github.actor` = `project-factory-keeper-v1[bot]`
- CI триггерится от bot actor
- GITHUB_TOKEN генерируется для этого bot-контекста

Org setting "Package creation" говорит: **"Members** will be able to create private packages".
Bot actor (`project-factory-keeper-v1[bot]`) — это installation, **не member** организации.

**Гипотеза**: GITHUB_TOKEN в workflow, триггернутом ботом, не может **создать новый** пакет
в org namespace, потому что bot ≠ member. Для push в **существующий** пакет, вероятно,
работает (не проверено).

**Источники:**
- [Discussion #26274 — Unable to push to ghcr.io from Github Actions](https://github.com/orgs/community/discussions/26274)
- [Discussion #32184 — buildx write_package or create_package errors with ghcr.io using GITHUB_TOKEN](https://github.com/orgs/community/discussions/32184)
- [Discussion #57724 — "installation not allowed to Create organization package"](https://github.com/orgs/community/discussions/57724)
- [Fixing 403 Forbidden GHCR — HackMD](https://hackmd.io/@maelvls/fixing-403-forbidden-ghcr)
- [Gist: How to fix Github Actions 403 when pushing to a new private GHCR.io registry in a GH org](https://gist.github.com/mandrean/e992efe79e3c7be75c2864ea3cda4a57)

## Непроверенные гипотезы

1. **Human push → CI → GITHUB_TOKEN**: если человек (member org) пушит коммит,
   CI триггерится с `github.actor` = человек. GITHUB_TOKEN может получить member-level
   доступ и создать пакет. **Не проверено.**

2. **Добавить бота как member**: `project-factory-keeper-v1[bot]` —
   можно ли добавить его как collaborator/member org? Скорее всего нет (бот — не user).

3. **Org package setting расширение**: может есть скрытая настройка
   "Allow GitHub Actions to create packages" помимо "Members can create".

4. **GitHub API обновления**: обсуждения 2022-2023 годов. GitHub мог добавить
   поддержку App tokens для GHCR с тех пор.

## Дополнительные эксперименты (2026-02-16, вечер)

После первоначального анализа были проведены дополнительные эксперименты:

| # | Эксперимент | Результат |
|---|-------------|-----------|
| 9 | Локальный `docker push` с App token, `username=x-access-token` | `installation not allowed to Create organization package` |
| 10 | `FROM scratch` образ с `LABEL org.opencontainers.image.source` + App token | То же самое |
| 11 | `workflow_dispatch` вместо `push` event (тот же bot actor) | 403 Forbidden |
| 12 | GHCR token exchange напрямую + HEAD запрос | 403 Forbidden |

### Ключевое: label не помогает

`docker/metadata-action@v5` уже генерирует `org.opencontainers.image.source` label (видно в CI логах).
Но 403 прилетает на HEAD-запрос проверки blob — **до** отправки манифеста с лейблами.
GHCR не может прочитать лейбл из образа, который ещё не запушен → label не решает chicken-and-egg.

### Проверка App permissions

App `project-factory-keeper-v1` имеет `packages: write` — подтверждено через GitHub API (68 permissions).
Ошибка `installation not allowed to Create organization package` — это **org-level policy**, не token permission.
Org setting "Members can create private packages" не распространяется на App installations.

## Финальный вывод

**App installation tokens не могут пушить в GHCR: 95%+ уверенность.**
Подтверждено локальными тестами с эксплицитной ошибкой `installation not allowed to Create organization package`.

**GITHUB_TOKEN не работает при bot-triggered workflow: подтверждено.**
Протестировано на push и workflow_dispatch events — оба 403. Actor `project-factory-keeper-v1[bot]` не является member org.

**`org.opencontainers.image.source` label не помогает:** уже присутствует через metadata-action, не решает проблему первого push.

## Варианты решения

### A. Убрать GHCR — билдить в CI, доставлять tar по SCP

```
CI: docker build → docker save → gzip → SCP → server: docker load → up
```

- Нет registry вообще
- Нет исходников на проде (только образы)
- Работает с любым GitHub (наш, чужой)
- Минус: нет layer caching, большие артефакты

### B. PAT (classic) с `write:packages`

- Один PAT от member org → org secret `GHCR_PAT`
- CI использует PAT для GHCR login
- Минус: привязан к аккаунту, нужна ротация, не масштабируется на чужие org

### C. Human push для первого создания пакета

- Первый push от member (руками или через PAT) создаёт пакет
- Последующие push через GITHUB_TOKEN (в существующий пакет)
- Минус: нужен manual step для каждого нового проекта

### D. Self-hosted Docker Registry ✅ Выбран

- Свой Docker registry (`registry:2`) в compose оркестратора
- Caddy для TLS (auto Let's Encrypt)
- Полный контроль, универсален (работает с любым GitHub, в т.ч. чужие org)
- Layer caching работает
- Caddy попутно закрывает webhook endpoint через HTTPS

## Решение

**Выбран вариант D** — self-hosted Docker registry.
Реализация: [deploy-architecture.md, Iteration 7](../plans/deploy-architecture.md#iteration-7-self-hosted-docker-registry--caddy-tls).
