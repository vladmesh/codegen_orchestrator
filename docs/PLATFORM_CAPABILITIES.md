# Platform capabilities

<!-- Generated from docs/platform_capabilities.yaml by `python -m scripts.platform_capabilities`; edit the YAML, not this file. -->

**Version 3, status: draft (owner read-through pending).**

What a product built by this orchestrator can have and what it cannot, with the workaround where one exists. The PO and the Architect read a compact rendering of the same source on every turn.

Derived from the kit `gh:vladmesh/codegen-product-kit` at commit `9a4acfd8b75fec4aec4ec4bd48805f7f9a2e8914`. The release pinned in `scheduler.service_template_ref`. `gh:vladmesh/service-template` is still an admitted Copier source, but no new product is scaffolded from it.

Code it was read from:

- `services/langgraph/src/subgraphs/devops/deployer.py`
- `services/langgraph/src/subgraphs/devops/secret_resolver.py`
- `services/langgraph/src/allocations.py`
- `services/langgraph/src/consumers/deploy.py`
- `services/langgraph/src/agents/po/tools_projects.py`
- `services/infra-service/ansible/roles/security/tasks/main.yml`
- `shared/contracts/service_ports.py`
- `shared/contracts/env_contract.py`
- `shared/contracts/dto/project.py`

## Can

### Telegram bot (long polling)

A Telegram bot people chat with; it fetches new messages itself, so it needs no web address.

How: Kit module `tg_bot` (python-telegram-bot 21.4, `run_polling`). By default the bot answers only Telegram users the owner has granted access (`grant_project_user`); others are ignored.

### Backend at http://\<server IP>:\<port>

A web API other programs can call over plain http at the server's address and a port.

How: Kit module `backend` (FastAPI), always included: the PO adds it to every project. The deployer reports `http://{server_ip}:{port}` of the backend's allocated port, the only port a product publishes; there is no domain name and no https.

### PostgreSQL database

A database that keeps the product's data across restarts and updates.

How: `postgres:16` service `db` with the named volume `db_data`, deployed with the backend. Reachable only from the product's own containers (`db:5432`), not from the internet.

### Redis

A fast in-memory store for queues, caches and messages between the bot and the backend.

How: `redis:7-alpine` at `redis:6379`, deployed with the backend or the bot. Product events are Redis Streams. No volume is mounted, so do not treat Redis as permanent storage.

### Work on a timer

Doing something regularly or later, such as a daily message, from inside the bot or backend.

How: A timer loop inside the product's own long-running bot or backend process. The kit's jobs core (`POST /jobs/fire`) only records and dispatches a declared named job; it schedules nothing, and the platform fires a product's jobs only while QA checks it. The bot's library is installed without its job-queue extra.

### Settings changed without new code

Values the owner can change later, such as a list of languages, without rebuilding the product.

How: Kit core settings v1 (`POST /settings/get`, `POST /settings/set`) for keys declared in the backend's `manifest.yaml`; the platform holds the write capability and writes the values.

### Calls to other online services

Using another online service, such as an AI model or a weather API, with a key the user provides.

How: Product servers allow all outgoing traffic (ufw default allow outgoing). The key is a `user_secret` the PO asks the user for and stores with `set_project_secret`.

## Cannot

### Public HTTPS address / TLS

The product has no https:// web address and no certificate.

Why: The deployer hands out only `http://{server_ip}:{port}`; a product's compose has no TLS proxy.

Workaround: Use the Telegram bot as the product's interface; it needs no address. Programs that accept plain http can call the backend at http://\<server IP>:\<port>.

### Custom domain

The product cannot be put on its own domain name, such as mybot.example.com.

Why: Nothing allocates, points or verifies a domain for a product; its address is the server IP.

Workaround: Use the Telegram bot, or the backend at http://\<server IP>:\<port>.

### Inbound webhooks from external services

Other services (payments, GitHub, forms, Telegram webhook mode) cannot push events to the product.

Why: Providers require an https URL, and the product only has plain http on an IP and port that can change when it moves server; the resolver derives no `PUBLIC_BASE_URL`.

Workaround: Poll the provider's API on a timer; the Telegram bot already works this way (long polling).

### The product knowing its own public URL

The product cannot build links to itself, such as a link a user opens in a browser.

Why: The resolver computes no `PUBLIC_BASE_URL` or similar derived key. A required derived key it cannot compute fails the engineering attempt, before any deploy (story-92b433c8 failed its deploy with `Unknown computed secret`).

Workaround: Send the information inside the Telegram chat rather than as a link to the product.

### OAuth web sign-in redirects

The product cannot use "Sign in with Google" style web redirects to get access to a user's account.

Why: An OAuth web redirect needs an https redirect URL on a stable domain, which a product does not have.

Workaround: A Google service account the user shares their calendar or sheet with; a device-code or desktop flow where the provider supports one; or a personal API token the user pastes, stored as a user secret.

### Telegram Mini App or web pages inside Telegram

The bot cannot open its own web app inside Telegram.

Why: A Telegram Mini App needs an https URL, which a product does not have.

Workaround: Build the interaction as chat messages with buttons.

### Sending email by itself

The product has no mail server of its own to send email.

Why: The kit has no mail module and the platform provisions no mail server or SMTP relay.

Workaround: Send through a third-party email API with a key the user provides (an outbound call), or send the message through the Telegram bot.

### File storage beyond the database

Uploaded files are not kept on disk; only data in the database survives an update.

Why: The only persistent volume is Postgres's `db_data`; the backend and bot containers have no volume and are replaced on every deploy, and the kit has no object-storage module.

Workaround: Keep small files in the database; keep Telegram files by their Telegram file id; or use an external storage service with credentials the user provides.

### A schedule run by the platform

The platform does not start the product's jobs on a clock.

Why: The kit's jobs core schedules nothing, and the platform fires a product's jobs only during QA.

Workaround: Run a timer loop inside the product's bot or backend process (see "Work on a timer").

### A separate website or web app module

The product cannot have its own website front end as a separate part.

Why: A product may request only the `backend` and `tg_bot` modules; `frontend` and `notifications` remain only for old records, and only the backend's port is published.

Workaround: Use the Telegram bot as the interface, or simple pages served by the backend at http://\<server IP>:\<port>.

### Reaching the database from outside

Nobody outside the server can connect to the product's database or Redis directly.

Why: Postgres and Redis get host ports allocated, but production compose publishes only the backend's port and the firewall opens only that one.

Workaround: Expose what is needed through backend endpoints.

## Kit at 9a4acfd8b75f

Modules:

- `backend`: FastAPI web API with PostgreSQL and Redis, always part of a product.
- `tg_bot`: Telegram bot on long polling that talks to the backend.

Core contracts every backend carries:

- users: Per-user access for the bot; the platform grants and revokes Telegram users.
- settings v1: Declared product or per-user settings the platform writes and the product reads.
- jobs v1: Declared named jobs a caller fires; records and dispatches them, schedules nothing.
- events v1: Durable product events on Redis Streams, each handled once per consuming service.

Packages:

- `reminders` 0.3.0: One-time text reminders (create, list, cancel); due reminders are emitted only when something fires its `reminders.tick` job.

## Deploy targets

- `backend` (requestable, HTTP health check): public, http://\<server IP>:\<port>; the one published port and health check
- `tg_bot` (requestable, no HTTP health check): none; allocated a port but publishes nothing, reaches Telegram outbound
- `postgres` (not requestable, no HTTP health check): internal only, `db:5432`; allocated a host port that production does not publish
- `redis` (not requestable, no HTTP health check): internal only, `redis:6379`; allocated a host port that production does not publish
- `notifications` (not requestable, no HTTP health check): not deployable for new products; kept only so old project records still read
- `frontend` (not requestable, no HTTP health check): not deployable for new products; kept only so old project records still read

## Secret kinds

- `user_secret`: A key or token the user supplies, such as a bot token or an API key; the PO asks for it.
- `generated_secret`: A secret the platform generates once and keeps, such as the database password.
- `allocation`: A port the platform reserves for the product on its server.
- `derived`: A value the platform computes at deploy time; only the keys listed below exist.
- `literal`: A fixed, non-secret value written in the product's own environment contract.

## Derived keys

The only `derived` keys a deploy can fill. Any other derived key is left out when it is optional and fails the deploy when it is required.

- `APP_ENV`: production
- `ENVIRONMENT`: production
- `DEBUG`: false
- `POSTGRES_HOST`: db
- `POSTGRES_PORT`: 5432
- `POSTGRES_REQUIRE_SSL`: false
- `BACKEND_API_URL`: http://backend:8000
- `API_URL`: http://backend:8000
- `API_BASE_URL`: http://backend:8000
- `BACKEND_URL`: http://backend:8000
- `APP_NAME`: the project's runtime slug
- `PROJECT_NAME`: the project's runtime slug
- `COMPOSE_PROJECT_NAME`: the project's runtime slug
- `POSTGRES_DB`: db_\<project id>
- `ENABLED_MODULES`: the project's modules, comma-separated
- `BACKEND_PORT`: the backend's allocated port
- `FRONTEND_PORT`: the frontend's allocated port
- `TG_BOT_PORT`: the bot's allocated port
- `POSTGRES_HOST_PORT`: the postgres allocated host port
- `REDIS_HOST_PORT`: the redis allocated host port
- `*_IMAGE`: the registry image of that service at the deployed commit

Declared by the kit for production but not computed:

- `PORT`: optional backend key; the backend listens on its own default, 8000
