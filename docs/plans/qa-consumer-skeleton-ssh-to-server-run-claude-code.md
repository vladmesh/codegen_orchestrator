# QA consumer skeleton — SSH to server, run Claude Code, parse result

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

After deploy+smoke, stories go straight to `completed`. We need a QA step where Claude Code runs on the prod server to test the deployed project against the story description.

**Prior work** (task-4dbe7a76, done): `TESTING` status in StoryStatus/TaskStatus, `QAMessage` contract in `shared/contracts/queues/qa.py`, `QA_QUEUE`/`QA_GROUP` in `shared/queues.py`.

**This task**: Build the QA consumer that reads from `qa:queue`, SSHes to the server, runs Claude Code, parses the result, and routes pass/fail outcomes.

## Steps

1. [ ] QA consumer core — `services/langgraph/src/consumers/qa.py`
   - **Input**: `_base.py` (start_worker pattern), `_events.py` (publish_story_event), `QAMessage`, `QA_QUEUE`, `QA_GROUP`
   - **Output**: `qa.py` with `process_qa_job()` function + `main()` entrypoint. Validates `QAMessage`, fetches story description via `api_client.get_story()`, fetches server IP via project allocations. Delegates SSH+Claude to helper (step 2). Routes result: pass → complete story + notify; fail → create fix task + rollback story.
   - **Test**: Unit test `services/langgraph/tests/unit/test_qa_consumer.py` — mock API client and Redis, verify pass/fail routing, story transitions, task creation on failure.

2. [ ] SSH runner helper — `services/langgraph/src/consumers/_qa_runner.py`
   - **Input**: asyncssh pattern from deploy.py, `api_client.get_server_ssh_key()`
   - **Output**: `run_qa_on_server(server_ip, ssh_key, project_name, story_description, deployed_url, bot_username, timeout) -> QAResult` — SSHes to server, runs `claude -p <prompt> --output-format json`, parses JSON result. Returns typed dataclass `QAResult(passed: bool, checks: list, summary: str, raw: str)`.
   - **Test**: Unit test `services/langgraph/tests/unit/test_qa_runner.py` — mock asyncssh, verify prompt construction, JSON parsing (valid, malformed, timeout).

3. [ ] Inflight dedup + retry limits
   - **Input**: scaffold inflight pattern from `scaffold_trigger.py`
   - **Output**: In `qa.py`: inflight marker `qa:inflight:{story_id}` (25 min TTL), cleared after processing. `qa_attempt` counter from QAMessage — if >= `MAX_QA_LOOPS` (2), story → failed instead of re-dispatching.
   - **Test**: Unit test — verify dedup skips duplicate messages, verify max attempts triggers story failure.

4. [ ] Docker compose service for qa-worker
   - **Input**: `docker-compose.yml` deploy-worker service pattern
   - **Output**: `qa-worker` service — same image (langgraph Dockerfile), `command: python -m src.consumers.qa`, `SERVICE_NAME: qa-worker`, same deps/networks/volumes as deploy-worker. Add `ANTHROPIC_API_KEY` env var (for Claude Code on servers).
   - **Test**: `docker compose config --services` includes qa-worker.

5. [ ] Integration test — consumer processes mock QA message end-to-end
   - **Input**: All above components
   - **Output**: `services/langgraph/tests/integration/test_qa_consumer_integration.py` — publishes a QAMessage to qa:queue, verifies consumer picks it up and processes (with mocked SSH). Verifies story transition and event publication.
   - **Test**: Integration test itself (requires Redis).

