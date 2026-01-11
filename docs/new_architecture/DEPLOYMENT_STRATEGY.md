# Deployment Strategy Transition

> **Status:** Proposed
> **Date:** 2026-01-11
> **Context:** Переход от Ansible-driven деплоя (шаблон Pull) к GitHub-Actions-driven деплою (шаблон Push/CI), инициируемому Оркестратором.

---

## 1. The Problem: "Secrets Paradox" & "Noise"

### Проблема А: Доступ к секретам (The Secrets Paradox)
*   **Old Way:** `infra-service` заходил по SSH на сервер и делал `git pull`.
*   **Blocker:** Чтобы запустить приложение, нужны ENV-переменные (ключи API, токены). Эти секреты лежат в **GitHub Secrets** репозитория (согласно [SECRETS.md](./SECRETS.md)).
*   **Conflict:** У `infra-service` нет доступа к GitHub Secrets (они write-only или доступны только Actions). Передавать их через `infra-service` небезопасно и сложно (дублирование).

### Проблема Б: Шум от коммитов (The Noise)
*   Инженерный агент может делать серию микро-коммитов ("fix typo", "revert", "try again").
*   Если настроить `on: push` деплой, мы будем пытаться выкатить на прод каждую опечатку, забивая очередь билдов и рискуя стабильностью сервера.

---

## 2. The Solution: Orchestrated CI/CD

Мы переносим ответственность за **сборку и доставку** кода на `service_template` (GitHub Actions), а ответственность за **тайминг (когда)** оставляем за Оркестратором.

### Architecture Shift

| Feature | Old Architecture (Ansible) | New Architecture (Orchestrated CI) |
|---------|----------------------------|------------------------------------|
| **Builder** | Server (docker compose build) | GitHub Runner (docker build) |
| **Secrets Access** | ❌ Hard (Need to pass via SSH) | ✅ Native (`${{ secrets.KEY }}`) |
| **Trigger** | Manual (Graph calls Service) | Explicit API Call (`workflow_dispatch`) |
| **Deploy Node** | Executor (Runs Ansible) | **Controller** (Triggers & Monitors) |
| **Infra Service** | Provision + Application Deploy | **Provision Only** (OS, Docker, Firewall) |

---

## 3. Detailed Workflow

### Phase 1: Provisioning (Infra Service)
*Ничего не меняется.* `infra-service` готовит "голое железо":
1.  Покупает сервер.
2.  Ставит Docker, Nginx (как reverse proxy).
3.  **NEW:** Добавляет SSH Public Key (от GitHub Repo) в `authorized_keys`, чтобы GitHub Actions могли деплоить.

### Phase 2: Development (Engineering Node)
1.  Агент пишет код, делает коммиты.
2.  В репо работает **CI (Tests)** (`on: push`).
3.  **CD (Deploy)** НЕ запускается автоматически.

### Phase 3: Deployment (Deploy Node)
Когда граф решает "Пора на прод":

1.  **Trigger**: Deploy Node вызывает GitHub API: `.github/workflows/deploy.yml` -> `workflow_dispatch`.
2.  **Monitor**: Deploy Node поллит API GitHub:
    *   `GET /repos/{owner}/{repo}/actions/runs?event=workflow_dispatch`
    *   Ждёт `status="completed"`.
3.  **Result**:
    *   **Success**: Возвращает `deployed_url`.
    *   **Failure**: Скачивает логи, формирует отчет об ошибке, возвращает `AgentVerdict(status=failure)`.

---

## 4. Updates Required

### 4.1. Orchestrator (`services/langgraph`)
*   **Deploy Node Logic**:
    *   Вместо посылки сообщения в `ansible:deploy:queue`...
    *   Вызывает `GitHubAppClient.trigger_workflow(repo_id, "deploy.yml")`.
    *   Затем входит в цикл `await_workflow_completion(run_id)`.

### 4.2. Service Template (`.github/workflows/deploy.yml`)
*   Должен принимать `workflow_dispatch`.
*   Шаги:
    1.  Checkout.
    2.  Build Docker Image.
    3.  Push to Registry (GHCR).
    4.  SSH to User Server (using stored secret Key).
    5.  `docker pull` & `docker compose up -d`.

### 4.3. Infra Service
*   **Удалить**: Логику деплоя приложения (`deploy.yml` playbook).
*   **Оставить**: Логику настройки сервера (`provision.yml`).
*   **Добавить**: Генерация SSH-пары для деплоя. Private Key кладется в GitHub Secrets репозитория (`DEPLOY_SSH_KEY`), Public Key — в `authorized_keys` на сервере пользователя.

---

## 5. Error Handling Flow

Если GitHub Action упал:
1.  **Deploy Node** видит `conclusion="failure"`.
2.  Качает логи (`jobs/steps/logs`).
3.  Возвращает управление в **Engineering Subgraph**.
4.  **Developer Worker** получает задачу: *"Fix deployment failure. Logs attached."*.
5.  Цикл повторяется.
