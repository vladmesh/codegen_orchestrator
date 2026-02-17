# E2E Investigation: Iteration 10 — Deploy Bugs (BUG 9, 10, 11)

> **Date**: 2026-02-17
> **Project**: reverse-echo-bot (project_id: `733fbf2d`)
> **Branch**: feat/deploy-architecture
> **Status**: Investigating

---

## Timeline

```
09:43:53 — Services started
09:44:12 — Provisioner: fire-and-forget для vps-267179, vps-267180
09:44:50 — Provisioner: vps-267179 success (38s)
09:45:35 — Provisioner: vps-267180 success (83s)
09:45:46 — PO: получил сообщение от пользователя (180 chars)
09:45:55 — PO: создал проект 733fbf2d (reverse-echo-bot), modules: backend, tg_bot
09:45:55 — PO: trigger_engineering → eng-96e3d7cca6a3
09:45:55 — Engineering: scaffolding triggered, ресурсы аллоцированы (backend:8000, tg_bot:8001 на vps-267179)
09:45:57 — PO: set_reminder(12 min)
09:46:06 — Scaffolder: complete (repo created, copier ran, pushed)
09:46:15 — Engineering: scaffolding_complete, worker spawn requested
09:46:42 — Worker-manager: image built (27s), container created: worker-dev-reverse-echo-bot-cb73e2ce
09:46:44 — Claude Code started inside worker
09:51:44 — Claude Code finished (5 min), commit 5d19448d
09:51:45 — Engineering: CI check waiting (ci.yml)
09:54:23 — CI passed (run_id 22093708836, ~2.5 min)
09:54:23 — Deploy auto-triggered → deploy-96e3d7cca6a3
09:54:35 — Deploy worker: 9 secrets configured, deploy.yml dispatched
09:55:40 — Deploy: containers created (backend, db, redis) on vps-267179
09:56:00 — Deploy: db healthy, backend started
09:56:15 — FAIL: health check error — "required variable POSTGRES_USER is missing a value"
09:56:28 — Deploy worker: deploy_workflow_failed
```

**Total time**: ~12 min (до момента failure).
**Всё до deploy отработало идеально** — scaffold, dev, CI, auto-deploy триггер, secrets.

---

## BUG 9: deploy.yml health check missing `--env-file`

### Описание

Health check в `deploy.yml` запускает `docker compose ps` без `--env-file ../.env`:

```yaml
# Pull и up — ЕСТЬ --env-file ✓
docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml pull
docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml up -d

# Health check — НЕТ --env-file ✗
docker compose -f compose.base.yml -f compose.prod.yml ps --format json
```

`compose.base.yml` содержит YAML anchors с `${POSTGRES_USER:?}` и другими required-переменными. Без `--env-file` docker compose не может интерполировать их и падает.

### Ошибка

```
error while interpolating x-backend-env.DATABASE_URL: required variable POSTGRES_USER is missing a value
```

### Результат

Deploy workflow (GitHub Actions) завершается с exit code 1. При этом контейнеры на самом деле **запустились** — `up -d` прошёл до health check.

### Фикс (service-template)

Добавить `--env-file ../.env` к health check команде в `deploy.yml`.

---

## BUG 10: Stale PostgreSQL volume — БД не создаётся при повторном деплое

### Описание

PostgreSQL Docker image создаёт базу из `POSTGRES_DB` env var **только при первой инициализации** (когда data directory пуст). Volume `infra_db_data` сохранился от предыдущего E2E теста — поэтому PostgreSQL проигнорировал `POSTGRES_DB=db_733fbf2d`.

### Данные с сервера

```
# Существующие БД:
db_3ab66132_d83e_4c2f_b3d4_19cfbf3eb98b  ← от предыдущего деплоя
postgres                                    ← дефолтная

# Ожидалась:
db_733fbf2d  ← от текущего деплоя → НЕ СУЩЕСТВУЕТ
```

### Почему volume выжил

Вероятно, `docker system prune --volumes` (запускался вчера) не удалил volume потому что контейнеры были ещё attached (или он был пересоздан provisioner'ом). Нужно исследовать.

### Фикс (service-template)

**Варианты:**
1. `deploy.yml`: всегда делать `docker compose down -v` перед `up` (жёстко — теряет данные)
2. `deploy.yml`: при первом деплое (нет `.env.bak`) делать `down -v`
3. Backend entrypoint: `CREATE DATABASE IF NOT EXISTS` до запуска Alembic
4. Убрать уникальные имена БД — использовать просто `app_db` (одна БД per compose project)

### Связь с BUG 9

BUG 9 маскирует реальную ошибку. Даже если починить health check, backend crash-loopит из-за невозможности подключиться к несуществующей БД.

---

## BUG 11: tg_bot не стартует — profiles: ["tg"] не активирован

### Описание

В `compose.base.yml` сервис `tg_bot` имеет `profiles: ["tg"]`:

```yaml
  tg_bot:
    ...
    profiles: ["tg"]
    networks:
      - internal
```

`compose.prod.yml` использует `extends` → profile наследуется. Deploy команда:

```bash
docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml up -d
```

Без `--profile tg` сервис tg_bot **не стартует**.

### Результат

На сервере работают только 3 контейнера: backend, db, redis. Telegram-бот не запущен.

### Фикс (service-template)

**Варианты:**
1. Убрать `profiles: ["tg"]` из `compose.base.yml` (раньше использовался для dev-only, но в prod нужен всегда)
2. `compose.prod.yml`: убрать profile override (если extends не наследует profiles, это не нужно)
3. `deploy.yml`: добавить `--profile tg` (хрупко — зависит от модулей проекта)

---

## BUG 12: `_save_secrets_to_project` двойно шифрует секреты

### Описание

В `services/langgraph/src/subgraphs/devops/nodes.py:192-196`, метод `_save_secrets_to_project`:

```python
config_secrets = config.get("secrets", {}) or {}  # ← ENCRYPTED from DB
config_secrets.update(secrets)                      # ← adds PLAINTEXT newly_generated
config["secrets"] = encrypt_dict(config_secrets)    # ← encrypts ALL → double-encrypts old!
```

Читает секреты из БД (уже зашифрованные), добавляет новые plaintext-секреты, и шифрует ВСЁ. Старые секреты получают `encrypt(encrypt(value))`.

### Воздействие

- **Первый деплой**: `.env` генерируется корректно (из `resolved_secrets`, которые были правильно расшифрованы ДО вызова `_save_secrets_to_project`). Но БД портится.
- **Повторный деплой**: `decrypt_dict(encrypt(encrypt(token)))` → `encrypt(token)` (одна обёртка снята, вторая осталась). `.env` получает зашифрованный токен.

### Как обнаружилось

PO ретриггернул deploy после failure (BUG 9). Второй/третий деплой перезаписали DOTENV в GitHub Secrets с двойно-зашифрованным TELEGRAM_BOT_TOKEN → tg_bot получает `gAAAAAB...` вместо настоящего токена.

### Фикс (codegen_orchestrator)

```python
config_secrets = config.get("secrets", {}) or {}
config_secrets = decrypt_dict(config_secrets) if config_secrets else {}  # ← ADD THIS
config_secrets.update(secrets)
config["secrets"] = encrypt_dict(config_secrets)
```

---

## BUG 13: PO триггерит повторные деплои после failure

### Описание

Dedup guard в deploy_worker проверяет `status=running`. Но к моменту повторного деплоя первый уже в статусе `failed`. Guard не срабатывает → PO может создать 2-3 деплоя подряд.

Не критично само по себе (повторные деплои просто тоже упадут), но в комбинации с BUG 12 каждый новый деплой ещё больше портит секреты в БД.

### Наблюдение

```
deploy-96e3d7cca6a3 (engineering, 09:54) → failed
deploy-56766d471050 (PO, 09:58)          → failed  ← PO ретриггернул
deploy-65464f943cf3 (PO, 09:59)          → failed  ← PO ещё раз
```

### Фикс

Рассмотреть: guard проверяет `status IN (running, queued)` вместо только `running`. Или добавить cooldown.

---

## Эксперименты

### Эксперимент 1: Очистка volume + перезапуск с --profile tg

**Цель**: Проверить, что после удаления stale volume контейнеры стартуют нормально.

**Шаги**:
```bash
# 1. Остановить и удалить volumes
cd /opt/services/reverse-echo-bot/infra
docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml down -v

# 2. Запустить с --profile tg
docker compose --env-file ../.env -f compose.base.yml -f compose.prod.yml --profile tg up -d
```

**Результат**: **SUCCESS**
- БД `db_733fbf2d` создана корректно (fresh volume)
- Backend: Alembic миграции прошли, Uvicorn running, стабилен
- Redis: healthy
- tg_bot: запустился (но crashed — BUG 12)

**Вывод**: BUG 10 (stale volume) подтверждён и решается `down -v`.

---

### Эксперимент 2: Ручной фикс .env + force-recreate tg_bot

**Цель**: Подтвердить что с расшифрованным токеном tg_bot работает.

**Шаги**:
```bash
# 1. Расшифровать токен (в deploy-worker контейнере)
# gAAAAABplDjQ... → 8442757449:AAFeVd8ZHSH6dByo-nEGQjNhcW4Bmb6wJYU

# 2. Заменить в .env
sed -i 's|TELEGRAM_BOT_TOKEN=gAAAAABplDjQ.*|TELEGRAM_BOT_TOKEN=8442757449:...|' \
  /opt/services/reverse-echo-bot/.env

# 3. Force recreate (restart не перечитывает env_file!)
docker compose ... --profile tg up -d --force-recreate tg_bot
```

**Результат**: **SUCCESS**
- tg_bot: `Application started`, Telegram API 200 OK
- Все 4 контейнера стабильно работают (backend 25s+, tg_bot 24s+, db healthy, redis healthy)

**Вывод**: BUG 12 подтверждён. С plaintext-токеном бот работает.

---

### Эксперимент 3: Повторный деплой через orchestrator (TODO)

**Цель**: Проверить что после фиксов BUG 9, 10, 11, 12 деплой проходит end-to-end.

**Предусловия**:
1. Починить `_save_secrets_to_project` (BUG 12) — в codegen_orchestrator
2. Починить deploy.yml health check (BUG 9), profiles (BUG 11), volume handling (BUG 10) — в service-template
3. Пересоздать проект с нуля или ре-scaffold

**Результат**: (pending)

---

## Сводка багов

| # | Баг | Где | Severity | Фикс |
|---|-----|-----|----------|------|
| **BUG 9** | deploy.yml health check без `--env-file` | service-template | HIGH | Добавить `--env-file ../.env` к `ps` |
| **BUG 10** | Stale DB volume → база не создаётся | service-template | HIGH | `down -v` перед первым деплоем |
| **BUG 11** | tg_bot profiles: ["tg"] не активирован | service-template | HIGH | Убрать profile или добавить в deploy |
| **BUG 12** | Double encryption в `_save_secrets_to_project` | codegen_orchestrator | CRITICAL | `decrypt_dict` перед update |
| **BUG 13** | PO ретриггерит деплои после failure | codegen_orchestrator | LOW | Guard: check queued + running |

**Корневая причина каскада**: BUG 9 (ложный failure) → PO ретриггерит → BUG 12 (double encrypt) → .env с ciphertext → tg_bot crash. Даже если бы BUG 9 не было, BUG 10 всё равно сломал бы деплой (wrong DB name).
