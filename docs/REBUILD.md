# Rebuild / Пересборка стенда

Как пересобрать оркестратор целиком и не оставить половину системы на старом коде.

## Два независимых контура

Сборка распадается на две части, и они не связаны между собой:

1. **Compose-сервисы.** 24 сервиса в `docker-compose.yml`. Семь из них (`api`, `langgraph`,
   `scheduler`, `scaffolder`, `worker-manager`, `infra-service`, `telegram_bot`) содержат
   `COPY shared ./shared`, поэтому любая правка под `shared/` требует их пересборки.
2. **Образы воркеров.** `worker-base-common` и производные от него `worker-base-claude`,
   `worker-base-factory`, `worker-base-codex`. Плюс `worker:<tag>` — образы, которые
   worker-manager собирает на лету под конкретный запуск.

`docker compose build` не трогает второй контур, а сборка воркеров не трогает первый. Это
основной источник «пересобрал, а изменения не подхватились».

## Выбор цели

| Цель | Образы сервисов | Образы воркеров | Тома и БД | Миграции |
|---|---|---|---|---|
| `make build` | пересобирает | только если протух хеш | не трогает | нет |
| `make rebuild` | пересобирает | пересобирает всегда | **сохраняет** | через entrypoint `api` |
| `make nuke` | пересобирает | проверяет хеш | **сносит** | явный upgrade + `seed` |
| `make nuke-hard` | `--no-cache` + `builder prune` | то же | **сносит** | явный upgrade + `seed` |

Обычная пересборка после мержа — `make rebuild`. `nuke` нужен, только когда действительно нужна
чистая база: он удаляет тома `db_data`, `redis_data`, `caddy-config`, `registry-data`.
Том `caddy-data` с TLS-сертификатами `nuke` сохраняет намеренно, чтобы не выгребать новые
сертификаты у Let's Encrypt.

Перед сносом базы `nuke` вызывает `infra/scripts/dump-server-keys.sh`, а `make seed` потом
восстанавливает серверы через `restore-server-keys.sh`. Если этот шаг упадёт, SSH-ключи
провизионированных серверов будут потеряны вместе с базой.

## Что делает make rebuild

1. `docker compose down --remove-orphans`.
2. Убивает осиротевшие контейнеры `worker-*`, не принадлежащие проекту.
3. `docker rmi worker:*` — сбрасывает кеш производных образов воркеров.
4. `docker compose build` — все сервисы.
5. `make rebuild-worker-images` — четыре базовых образа воркеров.
6. `docker compose up -d`.

Тома не затрагиваются, поэтому база и реестр переживают пересборку.

## Миграции

`services/api/entrypoint.sh` выполняет `alembic upgrade head` до запуска uvicorn. В compose у `api`
стоит `depends_on: db: {condition: service_healthy}`, а у `db` есть healthcheck, поэтому при
`up -d` база гарантированно готова к моменту миграции. Отдельно дёргать `make migrate` после
`rebuild` не нужно.

Важно: у `api` нет restart-политики. Если миграция упадёт, контейнер останется лежать и сам не
поднимется, а остальной стек будет работать против старой схемы. После пересборки всегда
проверять `docker compose ps api` и `docker compose logs api`.

`make migrate` (`compose exec api alembic upgrade head`) нужен только чтобы накатить схему без
перезапуска сервиса.

## Образы воркеров: почему отдельная механика

**Хеш свежести.** `WORKER_SOURCE_HASH` в Makefile — это sha256 от:

- `shared/__init__.py`, `shared/log_config`, `shared/redis`, `shared/redis_client.py`,
  `shared/config.py`, `shared/queues.py`, `shared/contracts`, `shared/crypto.py`,
  `shared/constants.py`
- `packages/worker-wrapper`
- `services/worker-manager/images`

Это **подмножество** `shared/`. `shared/models`, `shared/clients`, `shared/config_store.py` и
прочее в хеш не входят, потому что код воркера их не импортирует. Если появится новый модуль под
`shared/`, который worker-wrapper начнёт использовать, его нужно добавить в этот список: иначе
`check-worker-images` будет молча считать образы свежими, а внутри окажется старый код.

**Порядок сборки обязателен.** `worker-base-claude`, `-factory`, `-codex` объявлены как
`FROM ${BASE_IMAGE}` с дефолтом `worker-base-common:latest` и наследуют от него label
`org.codegen.worker_source_hash`. Только `worker-base-common` получает `--build-arg SOURCE_HASH`.
Собрать производный образ, не пересобрав common, значит получить старый код с чужим хешем.
`make rebuild-worker-images` соблюдает порядок; собирая руками, соблюдать его самому.

**Производные образы кешируются.** `worker:<tag>` строит worker-manager в рантайме
(`services/worker-manager/src/image_builder.py`), и сами они не инвалидируются при смене базы.
Поэтому и `make rebuild`, и `make rebuild-worker-images` делают `docker rmi worker:*`.

**Проверка без пересборки.** `make check-worker-images` сравнивает текущий `WORKER_SOURCE_HASH` с
label каждого из четырёх образов и вызывает `rebuild-worker-images`, если хоть один отстал.
`make build` вызывает эту проверку сам.

## Чистая пересборка после мержа

```bash
cd /home/dev/projects/codegen_orchestrator
git checkout main && git pull

make rebuild

# схема доехала до головы
docker compose exec -T db psql -U postgres -d orchestrator -tAc \
  "SELECT version_num FROM alembic_version"

# api не лёг на миграции
docker compose ps api
docker compose logs --tail=30 api

# образы воркеров совпадают с исходниками
make check-worker-images
```

Ожидаемое: `alembic_version` равен последней ревизии в
`services/api/migrations/versions/`, `api` в статусе healthy, `check-worker-images` печатает
`up to date`.

## Мелочи, которые сбивают с толку

- `--profile build` в целях Makefile ни на что не влияет: профилей в compose нет,
  `docker compose config --profiles` пуст. Флаг остался от прошлой схемы сборки.
- `make down` не только останавливает стек, но и удаляет осиротевшие контейнеры `worker-*` и сеть
  `codegen_worker`.
- `make stop` — алиас `make down`, не пауза.
- Скрипт `scripts/clean_live_tests.py` читает схему `projects` напрямую. После миграций, меняющих
  эту таблицу, его нужно проверять отдельно: он ломается тихо.
