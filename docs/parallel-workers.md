# Параллельные Workers

Для кодогенерации используются изолированные Docker-контейнеры с AI coding agents.

## Текущая архитектура

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│          (Developer node в Engineering)             │
└─────────────────────────────────────────────────────┘
                         │
                  Redis streams
          (cli-agent:commands / cli-agent:responses)
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              workers-spawner Service                │
│    (слушает cli-agent:commands Redis stream)        │
└─────────────────────────────────────────────────────┘
                         │
                  Docker API
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│        universal-worker Container (ephemeral)       │
│  - Базовый образ: Ubuntu 24.04 + Python + Node.js  │
│  - Устанавливает нужный agent (Droid / Claude)     │
│  - git clone репозитория                           │
│  - Выполняет coding task                           │
│  - git commit + git push                           │
└─────────────────────────────────────────────────────┘
                         │
                  Redis streams
                         │
                         ▼
              cli-agent:responses:{request_id}
```

## Docker-in-Docker с Sysbox

Для запуска `docker compose` внутри контейнера используем [Sysbox](https://github.com/nestybox/sysbox) — безопасный Docker-in-Docker без privileged mode.

**Установка на хост:**
```bash
wget https://downloads.nestybox.com/sysbox/releases/v0.6.4/sysbox-ce_0.6.4-0.linux_amd64.deb
sudo dpkg -i sysbox-ce_0.6.4-0.linux_amd64.deb
```

**Запуск worker контейнера с Docker capability:**
```bash
docker run --runtime=sysbox-runc -it --rm \
    -e FACTORY_API_KEY=... \
    universal-worker:latest
```

**Внутри контейнера доступно:**
- Полноценный Docker daemon (при Docker capability)
- `git clone`, `git push`
- `docker compose up -d`
- Factory.ai Droid CLI или Claude Code CLI

## Universal Worker Dockerfile

Актуальный Dockerfile находится в `services/universal-worker/Dockerfile`.

Основные характеристики:
- Ubuntu 24.04 + Python 3.12 + Node.js
- Non-root user `worker` (uid 1000)
- Pre-installed `orchestrator-cli`
- Динамическая настройка через ENV (`INSTALL_COMMANDS`, `AGENT_COMMAND`)
- Daemon mode для persistent containers

## Ограничения

| Аспект | Ограничение |
|--------|-------------|
| RAM | ~2-4GB на worker (Docker daemon + контейнеры) |
| Startup | Docker daemon стартует 5-10 сек |
| Disk | Образы качаются в каждый worker (кэшировать через volumes) |
| GitHub API | Rate limits — добавить throttling |


