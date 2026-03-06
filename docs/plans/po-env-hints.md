# Plan: PO Context-Aware Env Variables & Hints (#45)

## Context

PO ReactAgent communicates with users but passes only a raw `description` string to the Developer Worker. When users provide API keys or ask for access restrictions (e.g., "make the bot private — only for me"), the PO cannot pass these as ready-to-use env vars with explanations. The Developer Worker ends up hardcoding values or inventing wrong variable names.

**Source**: brainstorms `po-env-hints.md`, `managed-integrations-env-hints.md`

**Current state**:
- `set_project_secret` (`po/tools.py:153`) saves encrypted secrets to `config.secrets` but has no `hint` parameter
- `POUserMessage` (`shared/contracts/queues/po.py:18`) carries `user_id` but no `user_name`
- PO consumer (`po/consumer.py:226`) passes `user_id` in LangGraph configurable but PO has no awareness of who the user is
- Developer node (`nodes/developer.py:339`) builds TASK.md with description/modules but no env hints
- PO prompt (`po/prompts.py`) has no instructions about using hints with secrets

**Three changes needed**:
1. Inject user context (user_id, user_name) into PO agent's awareness
2. Add `hint` parameter to `set_project_secret`, persist in `config.env_hints`
3. Inject env_hints into Developer Worker's TASK.md

## Steps

1. [x] Add `user_name` to POUserMessage and telegram bot
   - **Input**: `shared/contracts/queues/po.py` (POUserMessage), `services/telegram_bot/src/main.py` (_send_to_po_and_wait)
   - **Output**: POUserMessage gains `user_name: str = ""` field; telegram bot populates it from `tg_user.first_name`
   - **Test**: Unit test in `shared/tests/test_po_contracts.py` — POUserMessage accepts and serializes user_name. Unit test in `services/telegram_bot/tests/unit/test_po_flow.py` — verify user_name in published fields.
   - **Note**: `user_name` is optional (default "") for backward compat with system events and reminders that don't have it. ⚠️ needs-approval (shared/contracts change)

2. [x] Inject user context into PO graph invocation
   - **Input**: `services/langgraph/src/po/consumer.py` (_handle_message)
   - **Output**: Pass `user_name` from message data into LangGraph configurable. Prepend a system-level context line to the first message of each thread (or always include): `"[context: user_id={user_id}, user_name={user_name}]"` so PO knows who it's talking to.
   - **Test**: Unit test in `services/langgraph/tests/unit/test_po_consumer.py` (new file) — mock graph.ainvoke and verify configurable contains user_name, and message content includes context prefix.

3. [x] Add `hint` parameter to `set_project_secret` tool
   - **Input**: `services/langgraph/src/po/tools.py` (set_project_secret)
   - **Output**: New `hint: str = ""` param. When hint is provided, save it to `proj_config["env_hints"][key] = hint` alongside the encrypted secret. Return message includes hint confirmation.
   - **Test**: Extend `services/langgraph/tests/unit/test_po_tools.py` — test that hint is saved to config.env_hints in the PATCH payload. Test without hint (backward compat) — env_hints not added.

4. [x] Update PO system prompt with env hints + access control question
   - **Input**: `services/langgraph/src/po/prompts.py`
   - **Output**: Two additions to the prompt:
     (a) **Env hints instructions**: use `set_project_secret` with `hint` whenever user provides API keys or sensitive data; use user's telegram_id (from context) for access restriction secrets like `ADMIN_TELEGRAM_ID`; always provide descriptive hints.
     (b) **Access control question for tg_bot projects**: During requirements gathering, if user hasn't explicitly specified access control, PO MUST ask "Who should have access to the bot?" with options:
       1. "Only me" → PO sets `ADMIN_TELEGRAM_ID` secret with user's telegram_id from context, hint="Telegram ID of the bot admin — only this user can interact with the bot"
       2. "Everyone" → no restriction, note in description that bot is public
       3. "Admin-first with invite" → default auth: bot starts admin-only (ADMIN_TELEGRAM_ID), admin has /add_user command to whitelist others. Note in description.
       4. "Custom" → user describes their auth scheme, PO includes it in description
   - **Test**: Extend `services/langgraph/tests/unit/test_po_prompts.py` — test that prompt contains access control question, mentions "Only me"/"Everyone" variants, and mentions `ADMIN_TELEGRAM_ID`.

5. [x] Inject env_hints into Developer Worker TASK.md
   - **Input**: `services/langgraph/src/nodes/developer.py` (_build_create_task, _build_feature_task)
   - **Output**: Read `config.env_hints` from project_spec. If present, append a `## Provided Environment Variables` section to TASK.md listing each var with its hint. Developer MUST use these exact names via `os.getenv()`.
   - **Test**: Extend `services/langgraph/tests/unit/test_developer_node.py` — test _build_create_task and _build_feature_task include env_hints block when hints exist, and omit it when empty.

6. [x] Integration test: full env_hints flow
   - **Input**: All modified files
   - **Output**: Integration test that verifies: PO tool sets secret+hint → project config contains env_hints → developer node task message includes the hints section.
   - **Test**: `services/langgraph/tests/unit/test_env_hints_flow.py` — end-to-end mock test: call set_project_secret with hint, build project_spec with resulting config, call _build_create_task, assert hint appears in output.

## Deviations

- **Step 2**: Tests added to existing `services/langgraph/tests/unit/po/test_consumer.py` instead of creating new `test_po_consumer.py`. Also fixed pre-existing `test_handles_missing_timestamp` that broke due to context prefix injection.
- **Step 4**: `MAX_PROMPT_LENGTH` bumped from 8000 to 12000 (prompt grew to ~10027 chars with new sections). Access control question was added per user request (not in original brainstorm).
- **Step 6**: Integration test placed in `tests/integration/backend/test_task_injection.py` (real Docker worker) instead of `services/langgraph/tests/unit/test_env_hints_flow.py` (mock). User correctly pointed out this should be a real integration test, not a unit test.
- **CI**: Pre-existing failure `test_post_projects_pure_db` (#42) unrelated to this task.
