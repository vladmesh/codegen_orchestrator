# Provider output fixtures

## `claude_code_2.1.278_result.json`

The unmodified stdout of one real `claude --dangerously-skip-permissions -p <prompt>
--output-format json` turn, captured on 2026-09-25 from Claude Code 2.1.278. That is the
`CLAUDE_CODE_VERSION` pinned in `services/worker-manager/images/worker-base-claude/Dockerfile`,
and the binary was checked against the release manifest's sha256. In the captured turn, the
agent wrote a file, posted its result with `curl` to a local `/result` endpoint and ended its own
turn. The CLI exited 1.65 s after the POST; two more captures exited 1.84 s and 0.94 s after it.

The only change is redaction: `session_id` and `uuid` are replaced with fixed placeholder UUIDs.
The document carries no credential and no filesystem path.

## `claude_code_2.1.278_stream.jsonl`

The stdout of one real turn of Claude Code 2.1.278 (the same pinned version, binary checked
against the release manifest's sha256), captured in the argument shape `ClaudeRunner.build_command`
produces: `claude --dangerously-skip-permissions -p <prompt> --output-format stream-json
--verbose`. The prompt asked the agent, in an empty git repository, to run `echo hello-from-bash`,
write `notes.txt` with the Write tool and change it with the Edit tool. The CLI exited 0. The
stream holds one event per line: `system` init, `assistant` thinking, `tool_use` (Bash, Write,
Edit) and text blocks, `user` `tool_result` blocks, a `rate_limit_event` and the final `result`
event.

Only these values were replaced: `session_id` with a fixed placeholder UUID, every `uuid` with
sequential placeholder UUIDs, the thinking block's `signature` with `signature-removed`, the
scratch repository path with `/workspace`, and `memory_paths` and `messaging_socket_path` with
neutral paths. Every other byte is as captured. The stream carries no credential.

## `codex_0.144.6_exec_stream.jsonl`

A hand-written stdout of one `codex exec --json` turn of Codex CLI 0.144.6, the
`CODEX_CLI_VERSION` pinned in `services/worker-manager/images/worker-base-codex/Dockerfile`, in
the argument shape `CodexRunner.build_command` produces for a developer turn: `codex exec --json
--sandbox danger-full-access -c <shell_environment_policy> <prompt>`. It is not a capture: no
API key or dedicated Codex profile was reachable, and a shared refreshable profile must never run
it. Every event follows the types the CLI serialises at the release tag `rust-v0.144.6`
(commit `5d1fbf26c43abc65a203928b2e31561cb039e06d` of `github.com/openai/codex`):
`codex-rs/exec/src/exec_events.rs` (`ThreadEvent`, `ThreadItemDetails` and their payloads) and
`codex-rs/exec/src/event_processor_with_jsonl_output.rs` (`item_<n>` ids, the in-progress
`item.started` and the terminal `item.completed` of a command, a completed-only `file_change`,
and `turn.completed` with its usage). A command's `command` is the shell argv joined with
`shlex_join` (`codex-rs/app-server-protocol/src/protocol/v2/item.rs`).

The turn reads `TASK.md`, runs `echo hello-from-bash`, adds `/workspace/notes.txt`, runs a `cat`
that exits 1, answers with an agent message and completes. Nothing was stripped; the
`thread_id` is a fixed placeholder UUID and the workspace path is `/workspace`. The stream
carries no credential.
