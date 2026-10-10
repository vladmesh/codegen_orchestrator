# Synthetic buyer: autonomous production acceptance of a fresh order

`python -m src.synthetic_buyer` (in the released `langgraph` image) is a product-only customer.
It orders a fresh public-channel bot through the actual Codegen Telegram bot, waits for the
native work, deploy and QA, checks the live product, writes redacted evidence and tears down
its own project. It is an operator tool: nothing in CI or in the running services starts it,
and importing it does nothing.

This runbook describes how a scoped production operation runs it. The code card that added it
proved the driver's behavior only (in-process fakes, no Telegram, no model, no production);
an actual production acceptance and its knowledge report need that later operation.

## The one authority path

Every entrypoint (`run`, `resume`, `cleanup`) and every effect passes the same ordered path in
`controller.py`; `check` and `inspect` read files only and cannot connect or send:

1. **Run identity.** The retained evidence must be this `operation_id` and this buyer.
2. **Redaction before dialogue.** The Telethon session and API hash, the Redis URL, the
   product-token handle and every promo code this operation minted (read back through
   `GET /api/promo-codes` by its retained ids) enter the redaction set before any dialog is
   read or the persona is asked.
3. **Durable intent, reconciled first.** Before every send, callback and promo mint the intent
   is written to the evidence (`pending`); after it, the receipt. Every entrypoint first
   reconciles a retained intent: a send is looked for in its dialog (`delivery_checks` reads,
   inside the identity hold), a mint in the promo list. A send that is found is recorded and
   never sent again; one that is not stays pending (`delivery_unknown`) and refuses every
   further action, cleanup included. A callback press has no visible outgoing message and so
   stays pending.
4. **The verdict is kept.** A failed operation is never continued: `resume` of a failed
   operation reconciles and then only reaches the teardown decision, and nothing turns a
   failure green. A send that becomes visible after the operation failed as `delivery_unknown`
   is reconciled by `resume` or `cleanup`, after which the proven project is torn down and the
   verdict stays failed. A `cleanup` of an operation that had not concluded fails its verdict
   (`cleanup_before_verdict`) because it can never be accepted afterwards.
5. **Ownership proof.** The order's project is the new owner-visible project whose own
   encrypted secrets hold exactly the product token this operation sent in its Telegram order
   (compared by SHA-256), with owner, creation time and `initiating_run_id` read back. Only a
   proven project is admitted to the rollout and torn down; a same-owner project without the
   token is listed as an unproven candidate and never touched.
6. **One holder of the QA identity.** Every Telegram use — connection, `get_me`, dialog read,
   send, callback, channel-post read and reconciliation — runs inside an exclusive hold on
   `qa:telegram-identity:<buyer.telegram_id>` in the platform Redis, the same hold native QA
   takes before its identity proof, preflight, Telegram tools, mechanical probe and executor
   sandbox (`consumers/_qa_telegram_lease.py`). The buyer connects and proves `get_me` after
   admission and disconnects before releasing. It holds nothing while it asks the persona,
   reads the API, or waits for native work, deploy and QA, so native QA is admitted between
   its turns; the buyer's next use waits (bounded by `identity_wait_seconds`) until QA's run,
   its sandbox included, has ended. Queued or running Run rows are never admission authority.
7. **Admitted actions only.** Before the project is proven and admitted to the rollout, the
   buyer sends only the controller's own opening, the token (when the bot asks for it) and at
   most `deferrals` fixed deferrals; the persona is not asked. After admission the persona
   speaks, but while the project's config points at an open brief revision
   (`product_brief_id`) nothing is sent until that revision's stored capability plan routes a
   module with its install and its preview postdates the rollout readback. Model output carries
   no authority flags; a schema-valid affirmative is held at this boundary like any reply.
8. **Judged observations, frozen verdict, owned teardown.**

## The shared QA identity's hold

The hold is one Redis hash with no TTL, changed only by compare-and-act scripts keyed on the
holder's token. A holder renews it while in use; if the record stops naming it (an operator
released it, or Redis lost it) the holder's use is cancelled and it reports the loss instead of
carrying on beside a new holder. A holder that cannot show its use ended — a Telegram
disconnect that failed, a QA executor served the session whose removal worker-manager did not
confirm — leaves the hold *retained*, naming why. Time never releases a hold: a process killed
while holding leaves it, and every later user is refused with a diagnostic naming the holder,
when it last renewed and its token.

To recover, verify that the named holder's use has ended — its process is gone, and for a
native QA run its executor container (`qa-…` worker) and egress proxy are removed — then:

```bash
"${RUN[@]}" identity --config /buyer/buyer.json                    # who holds it, since when
"${RUN[@]}" identity --config /buyer/buyer.json --release <token>  # only that exact token
```

A native QA run that cannot take the hold within 15 minutes, or loses it while in use, ends as
the `qa_probe_unavailable` infrastructure blocker with an administrator alert, never as a
product verdict; a retained hold after a QA run also alerts the administrators.

## What one operation does

Phases run in this order and are recorded in `evidence.json` as they complete:

| Phase | What happens | Session |
|---|---|---|
| `preflight` | Connect, `get_me` must equal `buyer.telegram_id`; the Codegen bot must resolve to `codegen_bot.username` **and** `codegen_bot.user_id`. Nothing is sent before this holds. | held |
| `registration` | If `GET /api/users/by-telegram/{id}` knows the buyer, it is reused. Otherwise one code is minted by `POST /api/promo-codes/batch` (outside the hold) and redeemed by sending it to the Codegen bot (the customer door), then read back. | held per exchange |
| `product_token` | The product bot token: a protected handle, or one bot created in BotFather for this operation (`sb<sha256(operation_id)[:12]>_bot`); an interrupted creation is read back with `/token`. | held for the BotFather dialog |
| `order` | Opening, token, deferrals until the project is proven and admitted (see above); then the persona describes the channel feature and answers; the brief is answered only after its route is admitted. The order is accepted when the project has a story backed by a confirmed brief. Persona and API calls hold nothing. | held per send / reply wait |
| `handoff` | The buyer holds nothing and is disconnected. | free |
| `build` | API reads until the story is `completed` (or stops). Observations: frozen brief and module plan; preview after the rollout; each install's typed operation starting on the pull request's base (the scaffold), with its admitted preflight, matching verification and published commits read from the repository, and the story head containing it; the files engineering changed after the install against the kit's admitted glue files; the deploy provenance below; QA typed `passed` on the deployed URL after the deploy; the bound bot alive. | free |
| `product_probe` | `/start` (Russian) and `/channels` (every scenario channel); then a bounded wait for an unsolicited delivery **before** `/digest` is sent (below); then `/digest` on its own. | held per exchange / per read |
| `platform` | Platform auth: the stored `PLATFORM_KEY` id is a registered, non-revoked key of `orch-<sha256(project)[:58]>`. Reader: `GET <reader>/v1/usage` with the product's own key; `product_id` must be this product's and `used.channels` with `used.requests_this_minute` or `used.resolves_today` must show activity (up to three reads). | released |
| `language` | `language=en` written and read back through core settings, then `/channels` in English. | per command |
| `auth_recheck` | The key is checked again while the product is still live. | released |
| `freeze` | The verdict is computed and written. | released |
| `teardown` | Only after the retained ownership proof still holds (same `initiating_run_id`, same token): `POST /api/projects/{id}/teardown` as the owner, polled until `completed`, project read back `archived`. | released |

**Deploy provenance.** `deployment_result.run_id` is the `deploy.yml` run the deployer
dispatched and waited for; it is not the publication. The images are published by the
deployed commit's own `ci.yml` run on `main`, whose `build-and-push` job pushes them (the run
the scheduler records in the story timeline). The observation reads both from GitHub with the
platform's App and requires: the deployed commit is the pull request's merge commit; a
successful `ci.yml` run on `main` at that commit whose `build-and-push` job(s) all succeeded,
agreeing with the timeline's record when it has one; a distinct successful `deploy.yml` run of
that commit; and every deployed image reference this repository's, tagged `sha-<commit[:7]>`,
with a `sha256` digest. Missing, failed or unrelated publication — a `main.yml` run, the deploy
run itself, a green pull-request run — fails it; an unreadable fact leaves it `unknown`.

**Unsolicited delivery.** `/digest` is the released product's only command whose answers
carry channel posts, and a multipart `/digest` answer can arrive late, unquoted, linking a post
newer than the command. So delivery is observed before `/digest` is sent, and only while this
operation's product history (its retained conversation and pending intent, never a watermark)
holds no such command. It counts a product message in the released `tg-channels.post` event's
own form for the product language (`Новая публикация: @<channel>` / `New post: @<channel>`,
codegen-kit-tg-channels 0.1.2), not a reply, linking a post of the channel it names that the
channel itself dates no later than the delivery. Anything else stays unattributed and the
observation `unknown`; a resumed operation whose `/digest` was already sent or is unresolved
cannot observe it at all.

The verdict is `passed` only when every required observation is `observed`; a missing fact is
`unknown` and makes it `incomplete`; any contradiction or stop is `failed` with its phase and
reason. Cleanup (`completed`, `failed`, `refused`, `nothing_owned`) is recorded beside the
verdict and never changes it. The exit code is 0 only for `passed` with cleanup `completed`.

## Where it runs

Run it as a one-off container of the released production `deploy-worker` service. That service
already has the API on `internal`, the platform auth admin API on `codegen-orch-link`, the
GitHub App key the repository provenance reads use, and the runtime environment from the
production `.env`. On h01o, as `vlad`:

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
| `deadlines.*` | Reply, settle, turn, order, build, identity-wait, probe, post, teardown and poll bounds; `delivery_checks` (reads that look for a send whose receipt was lost) and `deferrals` (fixed deferrals before the project is proven). `identity_wait_seconds` bounds each wait for the shared identity's hold; keep `reply_seconds` well under native QA's own 15-minute wait. |
| `api.base_url` | The internal API (`http://api:8000` inside the stack). Calls use the shared internal API transport, which authenticates with the runtime's `INTERNAL_API_KEY`. |
| `telegram.api_id`, `api_hash`, `session` | Handles of the QA account's Telethon credentials. |
| `identity_lease.redis_url` | Handle of the platform Redis URL native QA holds the identity in (`REDIS_URL` of the stack). |
| `registration` | Credits and attempt reservation armed by a newly minted promo code. Unused when the buyer is already registered. |
| `product_token` | `{"mode": "handle", "handle": …}` or `{"mode": "botfather", "botfather_username": "BotFather", "bot_display_name": …}`. |
| `platform.auth_admin_url`, `auth_admin_token` | Handles of `PLATFORM_AUTH_ADMIN_URL` and `PLATFORM_AUTH_ADMIN_TOKEN`. |
| `platform.reader_base_url` | The reader base URL the tg-channels environment contract names. |

A secret handle is `{"env": "NAME"}` or `{"file": "/path"}`. Values are resolved only by the
adapter that uses them and are added to the operation's redaction set at once. Two runtime
credentials are read the way every service reads them, not through a handle: `INTERNAL_API_KEY`
(the shared internal API transport), `SECRETS_ENCRYPTION_KEY` (the project-secret cipher) and
`GITHUB_APP_ID` with `GITHUB_APP_PRIVATE_KEY_PATH` (read-only repository provenance through the
platform's GitHub App); `check` reports all of them. Project secrets
(`PLATFORM_KEY`, `SETTINGS_WRITE_CAPABILITY`) are decrypted in the process with the runtime's
`SECRETS_ENCRYPTION_KEY`, exactly as the QA runtime reads them.

## Commands

```bash
"${RUN[@]}" check   --config /buyer/buyer.json            # offline: config, handle presence, evidence state
"${RUN[@]}" run     --config /buyer/buyer.json --orchestrator-revision <sha>
"${RUN[@]}" inspect --config /buyer/buyer.json            # offline: retained phase, verdict, ids
"${RUN[@]}" resume  --config /buyer/buyer.json --orchestrator-revision <sha>
"${RUN[@]}" cleanup --config /buyer/buyer.json --orchestrator-revision <sha>
"${RUN[@]}" identity --config /buyer/buyer.json [--release <token>]  # the shared identity's hold
```

`check` connects to nothing and prints only handle names and whether each resolves. `run`
refuses an operation whose evidence already exists. `resume` continues an interrupted
operation from its first incomplete phase, from its retained ids and its one retained intent:
it never registers, orders or creates a BotFather bot twice, answers the bot's unanswered
messages first, and adopts only the project proven by the order's token. A failed operation is
not resumed; `resume` and `cleanup` then reconcile its retained intent and only finish its
teardown, which is refused while a delivery stays unknown or the retained ownership proof no
longer holds. Evidence of another schema version is refused and left untouched for an operator
to read.

## Ownership and shared state

- **Promo codes.** Minted only when the buyer is not yet registered; the code is redeemed by
  the buyer through the Codegen bot. Reruns reuse the registered buyer.
- **Module rollout.** `capabilities.module_rollout` stays operator-owned. The driver reads it,
  appends only its project's id, preserves every other id and key, and reads it back before
  the feature is described. It never removes an id; the archived project's id stays listed.
- **The shared QA session.** One holder at a time through the identity hold (above), native QA
  included. Never run two operations at once: the second one only waits for the first's holds
  and would interleave its order with the first's.
- **Teardown.** Only the project this operation proved it owns by its token. No user, dialogue,
  repository or registry deletion, and nothing of the separately authorized old user.

## Evidence

`<evidence_dir>/<operation_id>/evidence.json` (schema version 2) and `report.md` are rewritten
at every phase boundary and before cleanup. They hold the orchestrator revision, the handle
names, the user/project/story/brief/task/deploy/QA/application ids, the product and BotFather
bot usernames, the publication and deploy workflow run ids, each identity hold's admission and
release time, timestamps, the redacted Codegen, BotFather and product dialogs, each persona
decision, the rollout readback, every observation with its provenance, the verdict and the
cleanup. Promo codes, tokens, the session, API hashes, internal and admin keys, platform keys,
capabilities and Fernet envelopes are never written: announced values and credential-shaped
text are replaced, including when a bot or an exception echoes them. Messages the driver sent
that carried a secret appear as a placeholder.

## Operator facts to confirm before the first run

1. `check` reports every handle present inside the `deploy-worker` one-off container, in
   particular `TELETHON_API_ID`, `TELETHON_API_HASH` and `TELETHON_SESSION` (qa-worker reads
   them from the same `.env`), `REDIS_URL` (the Redis qa-worker holds the identity in),
   `INTERNAL_API_KEY`, `SECRETS_ENCRYPTION_KEY`, `GITHUB_APP_ID` and
   `GITHUB_APP_PRIVATE_KEY_PATH` (whose key file the service mounts). `check` also prints
   `shares_native_qa_identity`: it must be `true` (the buyer is the QA account native QA holds),
   and the released qa-worker must be the revision that takes the hold.
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
6. Deploy and install provenance needs the generated repository's Actions runs and jobs
   readable by the platform's GitHub App and the deploy result's `deployment_result`
   (`deploy.yml` run, deployed commit, image references and digests) as the released deployer
   writes it. A product deployed before those fields existed, or a repository the App cannot
   read, leaves those observations `unknown` and the verdict `incomplete`.
7. Before the first run, `identity` shows the hold free. A hold left by an earlier killed
   process is released only as described above, never by deleting the key by hand.
