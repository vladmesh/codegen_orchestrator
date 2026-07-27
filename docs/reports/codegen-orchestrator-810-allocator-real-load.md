# Allocator real-load validation

The allocator admits a project only when its RAM estimate plus the configured
reserve fits both the persisted application reservations and fresh observed RAM
usage. It uses the greater of those two signals. Metrics older than
`ALLOCATION_METRICS_FRESHNESS_SECONDS` are rejected rather than treated as idle.

Live scenarios were not reproduced. The database was cleared on 2026-07-26 and
contains one 1967 MB server and no applications. Its untracked `personal_site`
does demonstrate why observed use remains necessary, but there is no live
allocated-project state for the occupied, stale, and no-candidate cases. The
unit tests use synthetic server and application responses for those cases.
