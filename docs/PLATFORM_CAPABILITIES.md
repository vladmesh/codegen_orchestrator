# Platform capabilities

<!-- Generated from docs/platform_capabilities.yaml by `python -m scripts.platform_capabilities`; edit the YAML, not this file. -->

**Version 8, status: owner-reviewed (product list agreed by the owner 2026-09-28).**

What a product built by this orchestrator can have and what it cannot, with the workaround where one exists. The PO reads the product part of the same source on every turn; the Architect reads the technical part.

## For the product owner

### Can

- **Plain HTTP self links.** The bot can include plain HTTP self links; their address can change when the product moves.
- **Telegram bot.** A Telegram bot people chat with, using commands, buttons and menus.
- **The bot remembers data.** The bot remembers data such as records, lists and history, and keeps it when the bot is updated.
- **Actions on a schedule or later.** The bot does things on a schedule or later, such as a daily message or a reminder in an hour.
- **Settings without a new version.** The owner later changes settings, such as the list of languages or the texts, without a new version of the bot.
- **Connecting to other services.** The bot connects to other online services, such as AI, weather or spreadsheets, with a key the user provides; AI in the bot runs on the user's own key.
- **Who can use the bot.** At first only the customer can use the bot; everyone else is ignored. The customer can give permanent access to specific Telegram users, and it starts working once the running bot confirms it. The customer can also hand the bot over to another Telegram user. Taking access back and opening the bot to everyone are not available yet.

### Cannot

- **A website or web pages** (`web_presence`). The bot has no website, web pages, admin panel or domain of its own, and cannot open a mini app inside Telegram. The product lives entirely inside the Telegram chat. Instead: Everything happens in the chat with buttons.
- **Receiving events other services send** (`inbound_webhooks`). The bot cannot receive events that other services send to it by themselves, such as GitHub notifications or form submissions. Other services have nowhere to send such events to. Instead: The bot checks the service itself from time to time, if the service allows that.
- **Accepting payments** (`payments`). The bot cannot take payments: neither through payment services such as YooKassa or Stripe nor inside Telegram (Stars, Payments). Payment acceptance is not part of what the platform builds. There is no way around it.
- **Signing in with Google and the like** (`oauth_web_redirect`). The bot cannot use "Sign in with Google" or similar sign-in buttons to reach a user's account. Such sign-in needs a web page for the user to return to, which the bot does not have. Instead: The user shares their spreadsheet or calendar with the bot's Google service account, or pastes a personal token.
- **Sending email by itself** (`send_email`). The bot cannot send email by itself. The bot has no mail service of its own. Instead: Send through an email service with a key the user provides, or send the message in Telegram.
- **Keeping uploaded files** (`file_storage`). The bot does not keep uploaded files itself. The bot remembers data, not files. Instead: Photos and documents stay in Telegram and the bot remembers them; large files go to an external storage service with a key the user provides.
- **Direct access to the bot's data store** (`external_database_access`). Nobody can connect to the bot's data store directly. The data is reachable only through the bot. Instead: The bot hands out the data needed through a command, such as an export or a report.
- **Backups of the data** (`backups`). There are no backups of the bot's data; if the server is lost, the bot's data is lost with it. The platform makes no copies of the bot's data. There is no way around it.

## Technical detail

Derived from the kit `gh:vladmesh/codegen-product-kit` at commit `04e2d94826f0dd6b46be3d7345b46cdd677db7ed`. The release pinned in `scheduler.service_template_ref`. `gh:vladmesh/service-template` is still an admitted Copier source, but no new product is scaffolded from it.

Code it was read from:

- `services/langgraph/src/subgraphs/devops/deployer.py`
- `services/langgraph/src/subgraphs/devops/secret_resolver.py`
- `services/langgraph/src/subgraphs/devops/deploy_workflow.py`
- `services/langgraph/src/subgraphs/devops/smoke.py`
- `services/langgraph/src/allocations.py`
- `services/langgraph/src/consumers/deploy.py`
- `services/langgraph/src/agents/po/tools_projects.py`
- `services/api/src/routers/projects/access.py`
- `services/infra-service/ansible/roles/security/tasks/main.yml`
- `shared/contracts/service_ports.py`
- `shared/contracts/env_contract.py`
- `shared/contracts/dto/project.py`
- `shared/contracts/dto/users_grant.py`

### How each capability works

#### Plain HTTP self links

How: Required derived `PUBLIC_BASE_URL` is the single backend's HTTP endpoint shared by resolver, deployer and smoke; IPv6 is bracketed. Missing/ambiguous allocations and effective native/mapped loopback, unspecified or multicast addresses fail with the key named. IPv6 deploy reads released executable transport at the built commit, refusing unverified workflows or .rej with DEPLOY_HOST named. Existing products need a reviewed kit update and reconciled merge. No domain/TLS/frontend/inbound webhook capability is added.

#### Telegram bot

How: Kit module `tg_bot` (python-telegram-bot 21.4, `run_polling`), so the bot needs no web address. Kit module `backend` (FastAPI) is always included: the PO adds it to every project. The deployer reports `http://{server_ip}:{port}` of the backend's allocated port, the only port a product publishes; there is no domain name and no https.

#### The bot remembers data

How: `postgres:16` service `db` with the named volume `db_data`, deployed with the backend, reachable only from the product's own containers (`db:5432`). `redis:7-alpine` at `redis:6379` carries queues, caches and product events (Redis Streams); it has no volume, so it is not permanent storage.

#### Actions on a schedule or later

How: A timer loop inside the product's own long-running bot or backend process. The kit's jobs core (`POST /jobs/fire`) only records and dispatches a declared named job; it schedules nothing, and the platform fires a product's jobs only while QA checks it. Trap: nothing calls the `reminders` package's `reminders.tick` in production, so a product with reminders must run its own timer loop that fires it, or no reminder is ever sent. The bot's library is installed without its job-queue extra.

#### Settings without a new version

How: Kit core settings v1 (`POST /settings/get`, `POST /settings/set`) for keys declared in the backend's `manifest.yaml`; the platform holds the write capability and writes the values.

#### Connecting to other services

How: Product servers allow all outgoing traffic (ufw default allow outgoing). The key is a `user_secret` the PO asks the user for and stores with `set_project_secret`.

#### Who can use the bot

How: Kit core `users` (grant, revoke, resolve by channel and external id); the bot admits a Telegram user only when the backend resolves that identity with status `active`. The platform records a durable grant intent (`initial_owner` on the first deploy, `add_user` from the PO's `grant_project_user`, `incoming_owner` from `transfer_project_ownership`) that the deploy worker applies and reads back. The PO has no revoke tool and there is no public or mode-switch setting.

### Why each limitation holds

#### A website or web pages

Why: The deployer hands out only `http://{server_ip}:{port}` of the backend; a product's compose has no TLS proxy and nothing allocates or verifies a domain. Derived `PUBLIC_BASE_URL` is that allocated backend HTTP address, with brackets for IPv6, for plain self links; it promises no frontend, TLS or webhook availability. A Mini App needs an https URL. A product may request only the `backend` and `tg_bot` modules; `frontend` remains only for old records.

Merges the former ids `https_domain`, `custom_domain`, `telegram_mini_app`, `web_frontend`.

#### Receiving events other services send

Why: Providers require an https URL, and the product only has plain http on an IP and port that can change when it moves server; derived `PUBLIC_BASE_URL` is only that HTTP address. Poll the provider's API from a timer loop; the Telegram bot already works this way (long polling).

#### Accepting payments

Why: No kit module or package handles payments, and a payment provider's confirmation arrives as an inbound webhook on an https URL, which a product does not have.

#### Signing in with Google and the like

Why: An OAuth web redirect needs an https redirect URL on a stable domain. Use a Google service account the user shares with, a device-code or desktop flow where the provider supports one, or a personal API token stored as a user secret.

#### Sending email by itself

Why: The kit has no mail module and the platform provisions no mail server or SMTP relay; a third-party email API is an ordinary outbound call with a `user_secret` key.

#### Keeping uploaded files

Why: The only persistent volume is Postgres's `db_data`; the backend and bot containers have no volume and are replaced on every deploy, and the kit has no object-storage module. Keep Telegram files by their Telegram file id; small files may go into the database.

#### Direct access to the bot's data store

Why: Postgres and Redis get host ports allocated, but production compose publishes only the backend's port and the firewall opens only that one. Expose data through bot commands or backend endpoints.

#### Backups of the data

Why: Nothing snapshots or copies the `db_data` volume; it lives only on the product's server.

### Kit at 04e2d94826f0

Modules:

- `backend`: FastAPI web API with PostgreSQL and Redis, always part of a product.
- `tg_bot`: Telegram bot on long polling that talks to the backend.

Core contracts every backend carries:

- users: Per-user access for the bot; the platform grants and revokes Telegram users.
- settings v1: Declared product or per-user settings the platform writes and the product reads.
- jobs v1: Declared named jobs a caller fires; records and dispatches them, schedules nothing.
- events v1: Durable product events on Redis Streams, each handled once per consuming service.

Packages:

- `reminders` 0.3.0: One-time text reminders (create, list, cancel); due reminders are emitted only when something fires its `reminders.tick` job, and nothing fires it in production: the product must run its own timer loop.

### Deploy targets

- `backend` (requestable, HTTP health check): public, http://\<server IP>:\<port>; the one published port and health check
- `tg_bot` (requestable, no HTTP health check): none; allocated a port but publishes nothing, reaches Telegram outbound
- `postgres` (not requestable, no HTTP health check): internal only, `db:5432`; allocated a host port that production does not publish
- `redis` (not requestable, no HTTP health check): internal only, `redis:6379`; allocated a host port that production does not publish
- `notifications` (not requestable, no HTTP health check): not deployable for new products; kept only so old project records still read
- `frontend` (not requestable, no HTTP health check): not deployable for new products; kept only so old project records still read

### Secret kinds

- `user_secret`: A key or token the user supplies, such as a bot token or an API key; the PO asks for it.
- `generated_secret`: A secret the platform generates once and keeps, such as the database password.
- `allocation`: A port the platform reserves for the product on its server.
- `derived`: A value the platform computes at deploy time; only the keys listed below exist.
- `literal`: A fixed, non-secret value written in the product's own environment contract.

### Derived keys

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
- `PUBLIC_BASE_URL`: the single allocated backend's HTTP address and port, with bracketed IPv6
- `BACKEND_PORT`: the backend's allocated port
- `FRONTEND_PORT`: the frontend's allocated port
- `TG_BOT_PORT`: the bot's allocated port
- `POSTGRES_HOST_PORT`: the postgres allocated host port
- `REDIS_HOST_PORT`: the redis allocated host port
- `*_IMAGE`: the registry image of that service at the deployed commit

Declared by the kit for production but not computed:

- `PORT`: optional backend key; the backend listens on its own default, 8000
