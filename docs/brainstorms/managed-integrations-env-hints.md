---
id: bs-bf115899
status: done
title: "Managed Integrations — Env Hints"
created_at: 2026-03-07T12:37:04.843472Z
---

# Context-Aware Environment Variables Injection

**Date:** 2026-03-06
**Status:** DONE
**Context:** User wants the ability to restrict bot access to themselves without hardcoding credentials or prompting the user to manually enter an ID during deployment. Furthermore, the system should generically support passing any pre-known credentials (like OpenAI keys) from the PO chat directly to the Developer Worker.

## Problem Statement
Currently, the PO ReactAgent communicates with the user but lacks context about the user's numeric `telegram_id` generated under-the-hood. The Developer Worker only receives a raw string `description` from the PO. If a user provides an API key or asks to restrict access to their own ID, the PO cannot easily pre-fill these secrets in a way that the Developer Worker understands how to use them, causing the worker to either hardcode the values or invent incorrect environment variable names.

## Proposed Solution: The "Env Hints" Architecture

To achieve a seamless, dynamic secret injection pipeline, we need to enhance three points:

1. **PO Context Injection:** Inject the current `user_id` and `user_name` into the PO ReactAgent's context (e.g., via a `SystemMessage` in the Graph thread initiation) so the PO knows who they are talking to.
2. **Secret Hints Support:** Extend `ProjectDTO.config` to support `env_hints: dict[str, str]`. When the PO sets a secret via `set_project_secret`, they will also pass a `hint` explaining what the variable is for.
3. **Developer Prompt Injection:** In the Developer Subgraph, before creating the system prompt for the coding agent, read the project's config. If `secrets` and `env_hints` exist, append a dedicated block to the prompt:
   *"The PO has already defined the following environment variables. You MUST use them in your code via `os.getenv()` or `pydantic-settings`: [VAR_NAME]: [HINT]"*

## Action Points

### Phase 1: PO ReactAgent Upgrades
- [ ] Modify `services/langgraph/src/po/consumer.py` (`_handle_message`) to prefix the first message of a thread (or dynamically inject) with context about the user's ID to empower the PO.
- [ ] Modify `services/langgraph/src/po/tools.py` -> `set_project_secret`: add a `hint: str = ""` parameter.
- [ ] Modify the API project patch logic inside the tool to save the `hint` into `project_spec.config.env_hints` (in plaintext, unlike the secret value itself).
- [ ] Update `services/langgraph/src/po/prompts.py` to instruct the PO to use `set_project_secret` with hints whenever the user provides API keys or requests context-bound access restriction.

### Phase 2: Developer Graph Injection
- [ ] Modify `services/langgraph/src/subgraphs/developer/prompts.py` or the specific node that formats the prompt.
- [ ] Fetch the project config and extract `env_hints`.
- [ ] Append the formatted hints to the system prompt so the Developer Worker uses exactly those variable names.

## Result
If a user says *"I want a private bot"*, the PO immediately calls `set_project_secret(key="ADMIN_TELEGRAM_ID", value="[USER_ID]", hint="Telegram ID of the bot admin to restrict access")`. 
The Developer Worker sees this hint, writes `admin_id = os.getenv("ADMIN_TELEGRAM_ID")`, and the app deploys successfully and securely on the first try without additional user input.