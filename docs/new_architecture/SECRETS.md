# Secrets Management Architecture

Архитектура управления секретами в Codegen Orchestrator разделяет ответственности между Оркестратором и Пользовательскими проектами.

## 1. Classification of Secrets

Мы выделяем три уровня секретов, которые имеют разные модели жизненного цикла и хранения.

| Уровень | Описание | Примеры | Владелец | Хранение (Master) | Использование |
|---------|----------|---------|----------|-------------------|---------------|
| **L1. Platform** | Инфраструктурные ключи самого Оркестратора | `GH_APP_PRIVATE_KEY`, `POSTGRES_URL`, `OPENAI_API_KEY`, `CLOUDFLARE_API_TOKEN` | DevOps Оркестратора | K8s Secrets / `.env` | Внедряются в контейнеры сервисов (Worker, API, Infra) |
| **L2. Project** | Секреты для запуска сгенерированных приложений | `TELEGRAM_BOT_TOKEN`, `STRIPE_KEY` | Пользователь | **GitHub Repository Secrets** (в репо проекта) | CI/CD пайплайны (`service_template`), Ansible |
| **L3. User** | Личные ключи пользователя для провайдеров (Future) | User's Cloudflare Key, AWS Key | Пользователь | **Encrypted DB Table** (`user_vault`) | Infra Service (для провижининга от имени юзера) |

---

## 2. Detailed Strategy

### Level 1: Platform Secrets (The Orchestrator)
Эти секреты необходимы для функционирования самой платформы.
*   **Storage**: Переменные окружения.
*   **Access**: Читаются при старте сервисов (`os.getenv`).
*   **Repo**: Хранятся в `.env` (локально) или в Secret Manager платформы хостинга.

### Level 2: Project Secrets (The Generated App)
Ключевой момент: **Оркестратор не хранит значения этих секретов в открытом виде**.
*   **Lifecycle**:
    1.  Пользователь вводит токен (например, Telegram Token) в UI/Bot.
    2.  Оркестратор (Scaffolder или Setup Service) сразу отправляет этот секрет в **GitHub Repository Secrets** созданного репозитория (через GitHub API).
    3.  В базе данных оркестратора сохраняется только **Reference** (ссылка/флаг), подтверждающая наличие секрета:
        *   `project_config = { "secrets": { "TELEGRAM_TOKEN": "present" } }`
*   **Usage**:
    *   **CI/CD**: GitHub Actions в репо пользователя используют `${{ secrets.TELEGRAM_TOKEN }}` для деплоя или внедрения в рантайм.
    *   **Deployment (Ansible)**: Если деплой идет через Ansible (запускаемый из GitHub Actions), секреты пробрасываются как `extra-vars`.

### Level 3: User Secrets (The Provider Accounts) - *Future/Complex*
Если пользователю нужно провижить ресурсы на своём аккаунте (например, VPS на его DigitalOcean).
*   **Storage**: Таблица `user_vault` (user_id, key, encrypted_value).
*   **Encryption**: Symmetric Key Encryption (AES-GCM), ключ шифрования — это L1 Secret (`VAULT_MASTER_KEY`).
*   **Usage**: Infra Service расшифровывает "на лету" перед вызовом провайдера (Terraform/Ansible).

---

## 3. Integration with Components

### Infra Service & Ansible
Как `infrastructure-service` деплоит проект, если у него нет секретов L2?

**Вариант А: CI-Driven Deployment (Recommended)**
1.  `infra-service` настраивает сервер (устанавливает Docker, Nginx). Для этого ему нужны L1 Secrets (SSH key оркестратора to connect).
2.  Сборка образа и деплой приложения (`docker run`) происходит внутри **GitHub Actions** репозитория пользователя.
3.  Там есть доступ к `secrets.TELEGRAM_TOKEN`.
4.  Action заходит по SSH и перезапускает контейнер с нужными ENV.

**Вариант Б: Orchestrator-Driven Deployment (Legacy/Fallback)**
Если `infra-service` должен сам запустить контейнер:
1.  Ему нужно получить значение секрета.
2.  GitHub Secrets — write-only (обычно).
3.  **Вывод**: Если мы идем по пути L2 Secrets in GitHub, то деплой приложения **обязан** инициироваться из CI (GitHub Actions), либо мы должны дублировать секреты в Encrypted DB (L3/L2 hybrid).

**Решение**: Мы используем **Вариант А**.
*   `infra-service`: Подготовка "железа" (OS, Docker, Firewall).
*   `GitHub Actions` (из `service_template`): Сборка, Push в Registry, Deploy (docker-compose up на сервере).

### Ref definition in Contracts
В `CONTRACTS.md` поле `secrets_ref`:
*   Это словарь, мапящий абстрактные ключи на ключи в GitHub Secrets или Vault.
*   Пример: `{"bot_token": "TELEGRAM_TOKEN_SECRET_NAME"}`.
*   Для L1 секретов (GitHub Token для клонирования): Infra Service генерирует Installation Token "на лету".

---

## 4. Summary of Flows

1.  **User creates Project** -> Orchestrator creates GitHub Repo.
2.  **User provides Bot Token** -> Orchestrator puts it into GitHub Repo Secrets `TELEGRAM_TOKEN`.
3.  **Infra Service provisions Server** -> Uses L1 Keys (Cloudflare/DigitalOcean) to buy server and setup DNS. Puts SSH Key into GitHub Repo Secrets.
4.  **GitHub Actions (triggered by commit)** -> Builds Docker Image -> SSH to Server -> Runs Container with `-e BOT_TOKEN=${{ secrets.TELEGRAM_TOKEN }}`.
