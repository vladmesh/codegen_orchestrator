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

The same turn as the `--output-format stream-json --verbose` stdout the wrapper reads: one event per
line. The last line is `claude_code_2.1.278_result.json` byte for byte, which is the CLI's final
`result` event in both output forms. The lines before it are written by hand in the CLI's
stream-json event shape (`system` init, `assistant` text and `tool_use` blocks, `user`
`tool_result` blocks) and are not a capture; they carry no credential.
