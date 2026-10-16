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
