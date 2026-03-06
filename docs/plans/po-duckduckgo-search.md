# Plan: PO DuckDuckGo Search Tool (#44)

## Context

Brainstorm [po-smart-node.md](../brainstorms/po-smart-node.md), Option B.
#43 (Socratic dialog) is done — PO now gathers requirements before triggering engineering.
This task adds a web search tool so PO can look up third-party API docs when the user's request involves external services (e.g. "currency exchange rates API", "Telegram payments").

Current state:
- `services/langgraph/src/po/tools.py` — 9 tools, registered via `get_all_tools()`
- `services/langgraph/src/po/prompts.py` — system prompt, already has Requirements Gathering section
- `services/langgraph/src/po/graph.py` — `create_react_agent` with `get_all_tools()`
- Tests: `services/langgraph/tests/unit/po/test_tools.py` — covers all 9 tools

Approach: use `duckduckgo-search` Python package (lightweight, no API key needed, async support via `AsyncDDGS`). Add a single `web_search` tool that returns top N results as text. Update prompt to instruct PO when to use it.

## Steps

1. [ ] Add `duckduckgo-search` dependency
   - **Input**: `services/langgraph/pyproject.toml`
   - **Output**: `duckduckgo-search` added to dependencies, `requirements.lock` regenerated
   - **Test**: `make lock-deps` succeeds, package appears in lock file

2. [ ] Implement `web_search` tool
   - **Input**: `services/langgraph/src/po/tools.py`
   - **Output**: New `web_search` async tool function using `AsyncDDGS().atext()`. Parameters: `query: str`, `max_results: int = 5`. Returns formatted string with title + snippet + URL for each result. Add to `get_all_tools()` list.
   - **Test**: Unit test in `test_tools.py` — mock `AsyncDDGS` context manager, verify formatted output, verify it's in `get_all_tools()` (count becomes 10)

3. [ ] Update system prompt with search guidance
   - **Input**: `services/langgraph/src/po/prompts.py`
   - **Output**: Add a short paragraph in Requirements Gathering section: use `web_search` when the user mentions a third-party API/service you're unfamiliar with, to find official docs and include relevant details in the spec. Do NOT search for common/obvious things.
   - **Test**: Verify prompt contains `web_search` keyword (existing prompt test pattern or manual check)

4. [ ] Run full test suite
   - **Input**: all changes from steps 1-3
   - **Output**: `make test-langgraph-unit` passes, `make lint` passes
   - **Test**: CI-equivalent local run
