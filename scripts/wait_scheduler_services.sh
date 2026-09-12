#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 TIMEOUT_SECONDS [docker compose arguments...]" >&2
  exit 2
fi

timeout_seconds="$1"
shift
poll_seconds="${SCHEDULER_READINESS_POLL_SECONDS:-2}"
compose=(docker compose "$@")
services=(scheduler-pipeline scheduler-infrastructure scheduler-maintenance)
declare -A stable_checks=()
declare -A observed_id=()
declare -A observed_restarts=()
deadline=$((SECONDS + timeout_seconds))

while [ "$SECONDS" -lt "$deadline" ]; do
  all_ready=true
  for service in "${services[@]}"; do
    container_id="$("${compose[@]}" ps --all -q "$service" 2>/dev/null || true)"
    state=""
    if [ -n "$container_id" ]; then
      state="$(docker inspect --format '{{.State.Running}}|{{.RestartCount}}' "$container_id" 2>/dev/null || true)"
    fi
    IFS='|' read -r running restart_count <<< "$state"

    ready_service=""
    if [ "$running" = "true" ]; then
      ready_service="$("${compose[@]}" exec -T "$service" cat /tmp/scheduler-service-ready 2>/dev/null || true)"
    fi

    if [ "$running" = "true" ] \
      && [ "$ready_service" = "$service" ]; then
      if [ "${observed_id[$service]:-}" = "$container_id" ] \
        && [ "${observed_restarts[$service]:-}" = "$restart_count" ]; then
        stable_checks[$service]=$(( ${stable_checks[$service]:-0} + 1 ))
      else
        observed_id[$service]="$container_id"
        observed_restarts[$service]="$restart_count"
        stable_checks[$service]=1
      fi
    else
      stable_checks[$service]=0
    fi

    if [ "${stable_checks[$service]}" -lt 3 ]; then
      all_ready=false
    fi
  done

  if [ "$all_ready" = true ]; then
    echo "Scheduler services are ready and stable"
    exit 0
  fi
  sleep "$poll_seconds"
done

echo "FATAL: scheduler services did not become ready within ${timeout_seconds}s" >&2
for service in "${services[@]}"; do
  echo "--- ${service} state ---" >&2
  "${compose[@]}" ps "$service" >&2 || true
  echo "--- ${service} recent logs ---" >&2
  "${compose[@]}" logs --no-color --tail 50 "$service" >&2 || true
done
exit 1
