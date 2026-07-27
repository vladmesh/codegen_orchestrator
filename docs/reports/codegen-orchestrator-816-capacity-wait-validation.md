# Capacity wait validation

The capacity-wait scenarios were tested with synthetic server, application and task data in unit tests. They were not run against live infrastructure because exercising the failure path would require deliberately exhausting a managed server or placing a project whose reservation cannot fit any managed server. Either action would affect unrelated deployments and is outside this task's safe validation scope.

The tests cover capacity parking without an iteration increment, one PO request on entry, fresh-metrics admission before redispatch, terminal escalation for an impossible request, and a visible timeout event. `no_fresh_metrics` remains a technical failure: it is not parked as a user-facing capacity shortage and follows the existing technical failure handling path.
