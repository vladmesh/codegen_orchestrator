# Docker Build Optimization Plan

> **Цель**: Сократить время сборки Docker с 3-4 минут (cached) / 10 минут (clean) до 30-60 секунд / 5-6 минут.

---

## Текущее состояние

| Метрика | Сейчас | Цель |
|---------|--------|------|
| Cached build | 3-4 мин | 30-60 сек |
| Clean build | ~10 мин | 5-6 мин |
| Сервисов в compose | 8 (включая tooling) | 7 (tooling в profile) |

---

## Фаза 1: Quick Wins (Минимальные изменения, максимальный эффект)

### 1.1 Убрать tooling из основной сборки

**Файл**: `docker-compose.yml`

**Проблема**: Сервис `tooling` определён без `profiles`, поэтому собирается при любом `docker compose build`.

**Текущий код** (строки 156-167):
```yaml
tooling:
  build:
    context: .
    dockerfile: tooling/Dockerfile
  user: "${HOST_UID:-1000}:${HOST_GID:-1000}"
  working_dir: /workspace
  volumes:
    - .:/workspace:delegated
  env_file:
    - .env
```

**Решение**:
```yaml
tooling:
  profiles: ["dev"]  # ← Добавить эту строку
  build:
    context: .
    dockerfile: tooling/Dockerfile
  # ... остальное без изменений
```

**Как использовать после изменения**:
```bash
# Обычный запуск (без tooling)
docker compose up -d

# С tooling (для lint/format)
docker compose --profile dev run --rm tooling ruff check .

# Или через Makefile (уже работает корректно)
make lint
make format
```

**Ожидаемый выигрыш**: ~30 секунд на каждую сборку

---

### 1.2 Убрать дублирование Ansible в LangGraph

**Файл**: `services/langgraph/Dockerfile`

**Проблема**: Ansible устанавливается дважды:
1. Через `pyproject.toml`: `"ansible-core>=2.16.0"` 
2. Явно в Dockerfile: `pip install ansible`

При этом `ansible` (~180MB) значительно тяжелее `ansible-core` (~15MB).

**Текущий код** (строки 22-24):
```dockerfile
COPY services/langgraph/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install . && \
    pip install --no-cache-dir --prefix=/install ansible
```

**Решение**:
```dockerfile
COPY services/langgraph/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .
# ansible-core уже установлен через pyproject.toml
```

**Проверка**: После изменения убедиться что `ansible-playbook --version` работает в контейнере.

**Ожидаемый выигрыш**: ~60 секунд (langgraph build)

---

### 1.3 Использовать shared/*.py без pip install в API

**Файл**: `services/api/Dockerfile`

**Проблема**: В API `shared` просто копируется как папка, а не устанавливается через pip. Это несогласованно с другими сервисами.

**Текущий код** (строки 16-31):
```dockerfile
# Builder stage - shared НЕ устанавливается
COPY services/api/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

# Production stage
COPY shared ./shared  # ← просто копируется
```

**Решение** (унификация с langgraph/telegram_bot):
```dockerfile
# Builder stage
COPY shared ./shared
RUN pip install --no-cache-dir --prefix=/install ./shared

COPY services/api/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .
```

**Альтернатива**: Оставить как есть, если всё работает. Это влияет больше на консистентность, чем на скорость.

---

## Фаза 2: Оптимизация зависимостей

### 2.1 Создать lock-файл для LangGraph

**Цель**: Избежать dependency resolution при каждой сборке.

**Проблема**: `langgraph`, `langchain`, `langchain-openai` имеют десятки транзитивных зависимостей. Pip выполняет resolution "на лету", что занимает 1-2 минуты.

**Текущий pyproject.toml**:
```toml
dependencies = [
    "langgraph>=1.0.5",
    "langchain>=1.0.0",
    "langchain-openai>=0.2.0",
    # ...
]
```

**Решение с uv** (рекомендуется):
```bash
cd services/langgraph
pip install uv
uv pip compile pyproject.toml -o requirements.lock
```

**Изменения в Dockerfile**:
```dockerfile
# Было:
COPY services/langgraph/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

# Стало:
COPY services/langgraph/requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock
```

**Важно**: Регенерировать `requirements.lock` при изменении `pyproject.toml`:
```bash
make langgraph-lock  # добавить в Makefile
```

**Ожидаемый выигрыш**: ~90 секунд (убираем dependency resolution)

---

### 2.2 Аналогично для других сервисов

Повторить процесс для:
- `services/api/`
- `services/scheduler/`
- `services/telegram_bot/`

---

## Фаза 3: Архитектурные улучшения

### 3.1 Создать общий base image

**Цель**: Избежать повторной установки общих system packages в каждом сервисе.

**Новый файл**: `docker/base.Dockerfile`
```dockerfile
FROM python:3.12-slim AS base

# Общие system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Общие Python packages (если есть)
# RUN pip install --no-cache-dir structlog pydantic
```

**Использование в сервисах**:
```dockerfile
# services/api/Dockerfile
FROM codegen-base:latest AS builder
# ...
```

**Build workflow**:
```bash
# В Makefile
build-base:
    docker build -f docker/base.Dockerfile -t codegen-base:latest .

build: build-base
    docker compose build
```

**Ожидаемый выигрыш**: ~30 сек × количество сервисов

---

### 3.2 Multi-stage для worker-spawner

**Файл**: `services/worker-spawner/Dockerfile`

**Текущий код** (без multi-stage):
```dockerfile
FROM python:3.12-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y ...
RUN pip install --no-cache-dir .
COPY services/worker-spawner/src ./src
```

**Решение**:
```dockerfile
# === Builder ===
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY services/worker-spawner/pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

# === Production ===
FROM python:3.12-slim-bookworm AS production
WORKDIR /app

# Docker CLI (нужен в runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY services/worker-spawner/src ./src
COPY shared ./shared

CMD ["python", "-m", "src.main"]
```

---

### 3.3 Оптимизация infrastructure Dockerfile

**Файл**: `services/infrastructure/Dockerfile`

**Текущий код**:
```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir ansible httpx
CMD ["tail", "-f", "/dev/null"]  # Контейнер ничего не делает
```

**Варианты решения**:

1. **Убрать из compose** если не используется активно
2. **Объединить с langgraph** (который уже имеет ansible)
3. **Оставить как есть** но добавить в `profiles: ["infra"]`

---

## Фаза 4: CI/CD оптимизации

### 4.1 BuildKit cache mounts

**Применить ко всем Dockerfiles**:
```dockerfile
# syntax=docker/dockerfile:1.4

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefix=/install .
```

### 4.2 GitHub Actions cache

```yaml
# .github/workflows/ci.yml
- uses: docker/build-push-action@v5
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

---

## Чеклист выполнения

### Фаза 1 (Quick Wins)
- [x] 1.1 Добавить `profiles: ["dev"]` к tooling
- [x] 1.2 Убрать дубликат `pip install ansible` в langgraph
- [x] 1.3 (Опционально) Унифицировать установку shared

### Фаза 2 (Dependencies)
- [ ] 2.1 Создать `requirements.lock` для langgraph
- [ ] 2.2 Создать lock-файлы для остальных сервисов
- [ ] 2.3 Обновить Dockerfiles для использования lock-файлов

### Фаза 3 (Architecture)
- [ ] 3.1 Создать base image
- [ ] 3.2 Переписать worker-spawner на multi-stage
- [ ] 3.3 Решить судьбу infrastructure контейнера

### Фаза 4 (CI/CD)
- [ ] 4.1 Добавить BuildKit cache mounts
- [ ] 4.2 Настроить GitHub Actions cache

---

## Метрики успеха

После каждой фазы измерять:
```bash
# Cached build
time docker compose build

# Clean build
docker compose down -v
docker system prune -af
time docker compose build
```

| После фазы | Cached | Clean |
|------------|--------|-------|
| Baseline | 3-4 мин | 10 мин |
| Фаза 1 | ~2 мин | ~8 мин |
| Фаза 2 | ~1 мин | ~6 мин |
| Фаза 3 | ~45 сек | ~5 мин |
| Фаза 4 | ~30 сек | ~5 мин |
