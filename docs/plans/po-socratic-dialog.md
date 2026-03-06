# Plan: PO Socratic Dialog & Requirements Gathering (#43)

## Context

Current PO agent acts as a pass-through router: gets a brief user message ("want a flower shop bot"), immediately calls `trigger_engineering` with that vague description. Engineers receive insufficient context, leading to poor code quality.

**Goal**: Update `SYSTEM_PROMPT` in `services/langgraph/src/po/prompts.py` so PO conducts a Socratic dialog — asks 2-3 clarifying questions, aggregates answers into a detailed spec (TZ), then passes the full spec as `description` to `trigger_engineering`.

Source: [brainstorm po-smart-node](../brainstorms/po-smart-node.md) (Option A).

No graph changes needed — purely prompt engineering. The existing ReactAgent loop already supports multi-turn conversation via PostgreSQL checkpointer.

## Steps

1. [x] Rewrite SYSTEM_PROMPT with Socratic dialog instructions
   - **Input**: `services/langgraph/src/po/prompts.py`
   - **Output**: Updated `SYSTEM_PROMPT` with:
     - New section "## Requirements Gathering" before scenarios
     - Rules: on vague requests, ask 2-3 clarifying questions (target audience, key features, integrations, data model)
     - Only call `trigger_engineering` after collecting enough detail
     - Compose a structured description (TZ) from user answers and pass it as `description` param
     - Explicit instruction: do NOT trigger engineering on the first message if the request is vague
     - Keep existing scenarios (token flow, redeploy, add feature) intact
   - **Test**: Unit test asserting key phrases exist in SYSTEM_PROMPT (`"clarifying"`, `"requirements"`, `"description"` param usage instruction). Test that prompt length stays under 8000 chars (sanity).

2. [x] Update trigger_engineering tool docstring and scenario docs
   - **Input**: `services/langgraph/src/po/tools.py` (trigger_engineering docstring), `services/langgraph/src/po/prompts.py` (scenarios)
   - **Output**:
     - Update "Scenario: NEW Bot/Project" to include requirements gathering step before step 4
     - Update "Scenario: ADD FEATURES" to include clarification step
     - Enhance `trigger_engineering` docstring to emphasize `description` should contain the full gathered spec
   - **Test**: Unit test verifying trigger_engineering docstring mentions "gathered requirements" or "detailed description".

3. [ ] Manual E2E verification (deferred — test during next `/e2e-run`)
   - **Input**: Running system (`make up`)
   - **Output**: Send a vague message to PO via Telegram, verify it asks clarifying questions before triggering engineering
   - **Test**: Manual — document result in plan checkbox

## Deviations

- Steps 1 and 2 were combined into a single commit (both are prompt/docstring changes).
- Prompt rewritten with user feedback: non-technical founder focus, "when to just go" vs "when to clarify" framing instead of rigid question list. No more than 1-2 questions.
- Step 3 (manual E2E) deferred to next E2E run rather than blocking completion.
