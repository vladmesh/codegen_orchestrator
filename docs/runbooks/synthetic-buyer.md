# Synthetic buyer: autonomous production acceptance of a fresh order

`python -m src.synthetic_buyer` (in the released `langgraph` image) is a product-only customer.
It orders a fresh public-channel bot through the actual Codegen Telegram bot, waits for the
native work, deploy and QA, checks the live product, writes redacted evidence and tears down
its own project. It is an operator tool: nothing in CI or in the running services starts it,
and importing it does nothing.

This runbook describes how a scoped production operation runs it. The code card that added it
proved the driver's behavior only (in-process fakes, no Telegram, no model, no production);
an actual production acceptance and its knowledge report need that later operation.

## What one operation does

Phases run in this order and are recorded in `evidence.json` as they complete:

| Phase | What happens | Session |
|---|---|---|
| `preflight` | Connect, `get_me` must equal `buyer.telegram_id`; the Codegen bot must resolve to `codegen_bot.username` **and** `codegen_bot.user_id`. Nothing is sent before this holds. | connected |
| `registration` | If `GET /api/users/by-telegram/{id}` knows the buyer, it is reused. Otherwise one code is minted by `POST /api/promo-codes/batch` and redeemed by sending it to the Codegen bot (the customer door), then read back. | connected |
| `product_token` | The product bot token: a protected handle, or one bot created in BotFather for this operation (`sb<sha256(operation_id)[:12]>_bot`). | connected |
| `order` | The scripted persona orders through the Codegen bot. When the owner-scoped project list shows the order's new project, the conversation pauses: ownership is read back, the project id is appended to `capabilities.module_rollout` and read back. Only then is the channel feature described. The brief is confirmed only after the bot stated a module route; the order is accepted when the project has a story backed by a confirmed brief. | connected |
| `handoff` | The shared session is disconnected. | released |
| `build` | Story, brief, capability plan, preview, tasks and runs are read through the API until the story is `completed` (or stops: `failed`, `waiting_human_review`, `waiting_user_secret`, `archived`, or the build deadline). Deploy must be typed `success`, QA typed `passed` on the same deployed URL after the deploy, and the bound product bot alive. | released |
| `qa_quiet` | Wait until no QA run is `running` or `queued` anywhere: QA uses the same Telegram account. | released |
| `product_probe` | Reconnect; `/start` (Russian), `/channels` (every scenario channel listed), `/digest`; then disconnect and wait for an unsolicited post linking a configured channel post that the digest did not contain, connecting only for each read after a QA check. | per read |
| `platform` | Platform auth: the project's stored `PLATFORM_KEY` id is a registered, non-revoked key of `orch-<sha256(project)[:58]>`. Reader: `GET <reader>/v1/usage` with the product's own key shows used channels and requests. | released |
| `language` | `language=en` written and read back through the product's core settings (`SETTINGS_WRITE_CAPABILITY`), then `/channels` must answer in English. | per command |
| `auth_recheck` | The key is checked again while the product is still live. | released |
| `freeze` | The verdict is computed and written. | released |
| `teardown` | `POST /api/projects/{id}/teardown` as the owner, polled until `completed`, and the project read back `archived`. Only the project this operation detected is torn down. | released |

The verdict is `passed` only when every required observation is `observed`; a missing fact is
`unknown` and makes it `incomplete`; any contradiction or stop is `failed` with its phase and
reason. Cleanup is recorded beside the verdict and never changes it. The exit code is 0 only for
`passed` with cleanup `completed`.

## Where it runs

Run it as a one-off container of the released production `deploy-worker` service. That service
already has the API on `internal`, the platform auth admin API on `codegen-orch-link` and the
runtime environment from the production `.env`. On h01o, as `vlad`:

```bash
export DOCKER_HOST=unix:///run/user/1001/docker.sock
cd /home/vlad/codegen_orchestrator
COMPOSE=(docker compose -p codegen_orchestrator
  -f docker-compose.yml -f docker-compose.prod.yml
  -f /home/vlad/codegen-h01o.override.yml -f deployed-service-images.compose.yml)
RUN=("${COMPOSE[@]}" run --rm --no-deps
  -v /home/vlad/synthetic-buyer/buyer.json:/buyer/buyer.json:ro
  -v /home/vlad/synthetic-buyer/evidence:/buyer/evidence
  deploy-worker python -m src.synthetic_buyer)
"${RUN[@]}" check --config /buyer/buyer.json
```

`--orchestrator-revision` is the 40-hex commit of the released revision the production deploy
recorded (the same revision the `deployed-service-images.compose.yml` digests were built from).
It is required for `run`, `resume` and `cleanup`, and written into the evidence.

## Configuration

One JSON file, validated before anything connects; an unknown or missing field is a refusal
naming the field, never its value. `docs/examples/synthetic-buyer.example.json` is a complete
example with placeholders. There are no defaults for identities, credentials, endpoints, the
model or deadlines.

| Field | Meaning |
|---|---|
| `operation_id` | This operation's identity; the evidence directory is `<evidence_dir>/<operation_id>`. A new operation needs a new id. |
| `codegen_bot.username`, `codegen_bot.user_id` | The actual Codegen bot. Both must match what Telegram resolves. |
| `buyer.telegram_id` | The QA account the session must prove (`8202532144` in production). |
| `scenario.public_channels` | 1–5 public channels the customer asks for. Choose channels that publish often: the unsolicited-post observation waits only `post_delivery_seconds`. |
| `scenario.product_language`, `switch_language` | `ru` and `en`; the only supported pair. |
| `model.chain` | The persona's explicit channel chain (`shared.contracts.dto.llm_channel`), built with the existing channel adapters under the PO summarizer identity. |
| `deadlines.*` | Reply, settle, turn, order, build, QA-quiet, probe, post, teardown and poll bounds, and send attempts. |
| `api.base_url` | The internal API (`http://api:8000` inside the stack). Calls use the shared internal API transport, which authenticates with the runtime's `INTERNAL_API_KEY`. |
| `telegram.api_id`, `api_hash`, `session` | Handles of the QA account's Telethon credentials. |
| `registration` | Credits and attempt reservation armed by a newly minted promo code. Unused when the buyer is already registered. |
| `product_token` | `{"mode": "handle", "handle": …}` or `{"mode": "botfather", "botfather_username": "BotFather", "bot_display_name": …}`. |
| `platform.auth_admin_url`, `auth_admin_token` | Handles of `PLATFORM_AUTH_ADMIN_URL` and `PLATFORM_AUTH_ADMIN_TOKEN`. |
| `platform.reader_base_url` | The reader base URL the tg-channels environment contract names. |

A secret handle is `{"env": "NAME"}` or `{"file": "/path"}`. Values are resolved only by the
adapter that uses them and are added to the operation's redaction set at once. Two runtime
credentials are read the way every service reads them, not through a handle: `INTERNAL_API_KEY`
(the shared internal API transport) and `SECRETS_ENCRYPTION_KEY` (the project-secret cipher);
`check` reports both. Project secrets
(`PLATFORM_KEY`, `SETTINGS_WRITE_CAPABILITY`) are decrypted in the process with the runtime's
`SECRETS_ENCRYPTION_KEY`, exactly as the QA runtime reads them.

## Commands

```bash
"${RUN[@]}" check   --config /buyer/buyer.json            # offline: config, handle presence, evidence state
"${RUN[@]}" run     --config /buyer/buyer.json --orchestrator-revision <sha>
"${RUN[@]}" inspect --config /buyer/buyer.json            # offline: retained phase, verdict, ids
"${RUN[@]}" resume  --config /buyer/buyer.json --orchestrator-revision <sha>
"${RUN[@]}" cleanup --config /buyer/buyer.json --orchestrator-revision <sha>
```

`check` connects to nothing and prints only handle names and whether each resolves. `run`
refuses an operation whose evidence already exists. `resume` continues an interrupted
operation from its first incomplete phase, from its retained ids: it never registers, orders
or creates a BotFather bot twice, answers the bot's unanswered messages first, and adopts only
the one new project that appeared after the order's recorded baseline. A failed operation is
not resumed; `resume` and `cleanup` then only finish its teardown.

## Ownership and shared state

- **Promo codes.** Minted only when the buyer is not yet registered; the code is redeemed by
  the buyer through the Codegen bot. Reruns reuse the registered buyer.
- **Module rollout.** `capabilities.module_rollout` stays operator-owned. The driver reads it,
  appends only its project's id, preserves every other id and key, and reads it back before
  the feature is described. It never removes an id; the archived project's id stays listed.
- **The shared QA session.** The session is connected only for the customer conversation and
  for product reads and probes, after the API shows no QA run `running` or `queued`. Before
  each probe message the check is repeated; a QA run that started meanwhile makes the driver
  disconnect and wait. There is no lock: a QA run admitted between the check and a probe can
  still overlap one probe's few seconds. Never run two operations at once.
- **Teardown.** Only the project this operation detected and proved it owns. No user, dialogue,
  repository or registry deletion, and nothing of the separately authorized old user.

## Evidence

`<evidence_dir>/<operation_id>/evidence.json` (schema version 1) and `report.md` are rewritten
at every phase boundary and before cleanup. They hold the orchestrator revision, the handle
names, the user/project/story/brief/task/deploy/QA/application ids, the product and BotFather
bot usernames, timestamps, the redacted Codegen, BotFather and product dialogs, each persona
decision, the rollout readback, every observation with its provenance, the verdict and the
cleanup. Promo codes, tokens, the session, API hashes, internal and admin keys, platform keys,
capabilities and Fernet envelopes are never written: announced values and credential-shaped
text are replaced, including when a bot or an exception echoes them. Messages the driver sent
that carried a secret appear as a placeholder.

## Operator facts to confirm before the first run

1. `check` reports every handle present inside the `deploy-worker` one-off container, in
   particular `TELETHON_API_ID`, `TELETHON_API_HASH` and `TELETHON_SESSION` (qa-worker reads
   them from the same `.env`), `INTERNAL_API_KEY` and `SECRETS_ENCRYPTION_KEY`.
2. The persona's channel has its credential in that container. `openrouter` needs the PO's
   `PO_LLM_BASE_URL` and `PO_LLM_API_KEY` (and a model in the chain entry). A `codex` or `claude`
   channel needs its own profile mounted; never copy a subscription profile for this.
3. The Codegen bot's username and numeric id, and `reader_base_url`, are confirmed from the
   production configuration, not guessed.
4. For BotFather mode, the QA account may create bots; otherwise a product token is placed in
   a protected file or variable and named by its handle.
5. The QA account receives product messages only while the product admits it. If the product
   answers the owner's `/start` with an access refusal after QA revoked its temporary access,
   the operation stops with `product_access_denied`; that is a product-access defect for the
   observer, not something this driver works around.
