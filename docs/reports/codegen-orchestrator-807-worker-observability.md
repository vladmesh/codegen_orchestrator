# Worker observability

Run results remain strict, type-specific DTOs. Effort and transcript metadata live in nullable
`runs` columns, so analytics can distinguish an unavailable provider value from zero without
weakening `extra="forbid"`.

Worker-wrapper stores a redacted stdout/stderr transcript under the configured
`WORKER_TRANSCRIPT_STORAGE_PATH`, mounted at `/artifacts/worker-transcripts`. Artifacts are capped
by `WORKER_TRANSCRIPT_MAX_BYTES`; a retained artifact ends with an explicit truncation marker.
`WORKER_TRANSCRIPT_RETENTION_DAYS` controls best-effort pruning during worker creation. An artifact
write failure does not change the worker result.

The wrapper uses `shared.diagnostics.redact_diagnostic` and also removes values from environment
variables whose names indicate a credential. This follows the diagnostic/log redaction policy.

Usage support is deliberately conservative for the pinned adapters:

| Adapter | Usage persisted |
| --- | --- |
| Claude CLI JSON result | `usage.input_tokens`, `usage.output_tokens`, derived/returned total, and `total_cost_usd` when present |
| Factory Droid JSON result | Same fields when present in its JSON result |
| Codex CLI 0.144.6 | No stable non-interactive usage contract, so values remain null |

`agent_profile` records the agent type, configured provider/model when available, and
`worker-wrapper` adapter. Runs can be filtered through existing project/type/user fields and the
new `started_after`/`started_before` API filters.
