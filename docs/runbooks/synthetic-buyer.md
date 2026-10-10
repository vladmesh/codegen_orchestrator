# Synthetic buyer: one fresh production order

`python -m src.synthetic_buyer` is an operator entrypoint in the released LangGraph image.
It orders a channel bot through Codegen's real Telegram conversation, observes native
engineering/deploy/QA, probes the product, saves redacted evidence, then tears down and
deletes only the project authenticated as belonging to that order. Nothing starts on import,
in a service or in CI. Production execution is a later scoped operation under standing consent.

## Operator sequencing

Run one buyer operation at a time. Schedule no independent QA using this account during buyer
Telegram phases: registration, BotFather, ordering and post-QA probes. Native QA and
worker-manager retain released behavior. There is no shared-identity lock, bootstrap record or
lifetime authority here. Run status observations establish no concurrency-lock claim.

Before Telegram use, including every send, the buyer connects and proves
`get_me == buyer.telegram_id`. For sprint:1489 configure `8202532144`. After responding to an
admitted brief it disconnects and polls authenticated APIs for Story creation, even if
visibility is delayed. It sends nothing further while native work can start and remains
disconnected throughout engineering/deploy/QA. Product probes reconnect only after correlated
terminal deployment and typed QA passed. Failed, blocked, stopped or untyped QA cannot pass.

Interrupted-run continuation, cleanup reconciliation, concurrent admission and generic
orphan/cancellation recovery belong to `issue:c369b68e3d23896c2d6d`. Interrupted evidence
remains inspectable. Another fresh run of the same operation refuses existing evidence and
preserves it. Account for retained ownership and resources before scheduling a separate fresh
operation; changing an operation id is not a recovery or cleanup mechanism.

## Configuration and commands

Copy [the example](../examples/synthetic-buyer.example.json) to a protected operator location
and replace every placeholder. Config schema 2 forbids unknown fields and requires explicit
facts. Validation names fields without quoting values.

| Field | Required fact |
|---|---|
| `operation_id`, `evidence_dir` | Fresh operation id and persistent destination; evidence lives under `<evidence_dir>/<operation_id>`. |
| `codegen_bot`, `buyer` | Codegen username/id, both verified, and the expected authenticated Telethon user id. |
| `scenario` | Named public channels, initial `ru` and switch to `en`. |
| `model.chain` | Explicit persona channel/model chain using existing LLM adapters. |
| `deadlines` | Reply, settle, conversation turns, order, build, probe, post, teardown and poll bounds; bounded within-run delivery reads and fixed pre-project deferrals. |
| `api`, `telegram` | Actual Codegen API URL and protected handles for Telethon API id/hash/session. |
| `registration` | Promo credits and attempt reservation if registration is needed. |
| `product_token` | Protected token handle, or BotFather username/display name for one deterministic operation-owned bot. |
| `platform` | Protected auth-admin URL/token handles and reader URL from the environment contract. |

A handle names exactly one environment variable or file. Credential values never belong in
config. The released runtime also needs `INTERNAL_API_KEY`, `SECRETS_ENCRYPTION_KEY`,
`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, and the selected model channel's credentials.
Use the existing protected environment and mounts. No installation host or credential is
built into the driver. Repository evidence uses the platform GitHub App read adapter.

The later operator operation supplies a released image with Codegen API, auth-admin and reader
reachability, its protected environment, the GitHub App key mount, read-only config/credential
files and a writable persistent evidence mount. Inside that runtime invoke:

```bash
python -m src.synthetic_buyer check --config /buyer/buyer.json
python -m src.synthetic_buyer run --config /buyer/buyer.json --orchestrator-revision <40-hex-sha>
python -m src.synthetic_buyer inspect --config /buyer/buyer.json
```

`check` validates config and reports handle presence, identities, channels, model and evidence
location without values or connections. `inspect` reports retained ids, ownership, verdict,
cleanup and pending diagnostic intent without modifying evidence. Both are offline.
The revision on `run` identifies the released orchestrator image's source commit.

## Ordering and evidence

The controller reuses a registered buyer or mints one promo through the existing API, sends
it through Codegen and requires registration readback. It obtains a token from its protected
handle or a bounded BotFather creation conversation. Failure stops; there is no interrupted
creation continuation. It creates no brief or Story through the API to replace ordering.

Before describing the feature, it proves a new owner-visible project's authenticated owner,
initiating run and stored token match this order's sent token. A same-owner project's timestamp
cannot establish attribution. Ambiguous or unproven candidates are never adopted, allowlisted
or deleted. It appends only the proven id to `capabilities.module_rollout.project_ids` through
the config API, preserves unrelated ids/values and verifies readback before feature preview.

The persona sees only a redacted product scenario: named channel posts, Russian language,
answers to PO questions and agreement to story splitting. It receives no module, package,
key, platform or credential instructions. The controller handles credentials and visible
buttons. Before replying to the current presented brief, it proves that revision's frozen
plan routes through a module and its preview postdates rollout readback. After that response,
it observes the confirmed Story through APIs; absence stops within the order deadline.

Intent precedes each effect. A lost send receipt is searched for boundedly within this
invocation and never resent. Unproven sends/callbacks stop with diagnostics and refuse cleanup.
Explicit deadlines bound stalled conversations, native work and product probes.

| Observation | Required proof |
|---|---|
| Brief and route | Confirmed brief, frozen plan and rollout-before-preview correlation. |
| Immediate install | Matched typed installer preflight/verification and published scaffold/install/head chain before engineering. |
| Worker glue | Repository delta after install stays within admitted glue; task/engineering order agrees. |
| Publication/deploy | Successful built merge commit `ci.yml` with `build-and-push`; distinct successful `deploy.yml` agrees with deployed SHA, SHA-tagged images and digests. |
| QA | Terminal typed pass for this project/Story, deployed URL and ordering after deploy. |
| Product | Bound live bot, Russian reply, configured `/channels`, unsolicited configured-channel post, then `/digest` reply. |
| Language | Core settings `language=en` write/readback, then an English reply. |
| Platform | Product's own registered non-revoked auth key and attributed reader activity; key rechecked while live. |

Post delivery is witnessed before `/digest`, whose multipart answer can arrive late and
unquoted. The witness requires the released `tg-channels.post` form, matching configured
channel/link and independent channel post date no later than delivery. A delayed digest item,
inconsistent event or absent post stays unknown. Unreadable install/Actions facts likewise
stay unknown; missing or contradictory facts cannot pass. Fixture tests prove judgments,
not delivered production events.

## Evidence and ordinary cleanup

Atomic `evidence.json` and `report.md` keep revision, user/project/initiating-run/brief/Story/
task/install/deploy/QA/workflow ids, conversations, decisions, plan, installer/glue proof,
provenance and observations. The original verdict is saved before deletion and preserved
through cleanup failures. Resolved secrets and credential-shaped text are scrubbed before
persona input, artifact writes and diagnostics: sessions, tokens, promos, internal/admin/
platform keys, encrypted envelopes and echoed values.

In the uninterrupted terminal path cleanup revalidates authenticated ownership, initiating
run and token attribution. It performs owner `POST /api/projects/{id}/teardown`, polls
`GET /api/projects/{id}/teardown` to completed and requires owner project readback archived.
Only then, with matching owner/run identity on that readback, it calls owner
`DELETE /api/projects/{id}`, requires 204 and subsequent owner GET 404. `teardown` and
`deletion` are separate evidence records. Refused, failed, unknown or timed-out teardown
never permits DELETE. Uncertain DELETE or unreadable/present GET never reports cleanup success.
There is no interrupted cleanup reconciliation.

Exit 0 requires acceptance passed and deletion confirmed. A failed/incomplete acceptance may
clean up its proven project but still exits 1. No user or unrelated project is deleted.
External repository/registry cleanup and the separately authorized old `7192117299` user's
project are later scoped operations outside this driver.
