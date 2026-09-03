set -u

# Streamed to a deployment target by the live suite when a deploy reported
# success and the deployed URL then answered nobody. It reads and never writes:
# the container's own state and a bounded log tail, taken while the deployment
# still exists — the suite's own teardown removes these containers minutes
# later, and after that there is nothing here to photograph.
#
# Deliberately not `set -e`. One container whose logs cannot be read must not
# cost the state of every other container: each step names what it could not
# read and the snapshot continues.
PROJECT_NAME=${1:?project name is required}
case "$PROJECT_NAME" in
  *[!A-Za-z0-9_-]*)
    echo "target diagnostics failed: unsafe project name" >&2
    exit 1
    ;;
esac
# Docker replaces dashes with underscores in some compose project names, the
# same tolerance the cleanup script applies when it looks for the same stack.
ALT_PROJECT_NAME=$(printf '%s' "$PROJECT_NAME" | tr '-' '_')

echo "== containers =="
if ! docker ps -a --format '{{.Names}} state={{.State}} status={{.Status}} image={{.Image}} ports={{.Ports}}'; then
  echo "container listing unavailable: docker ps -a exited nonzero"
fi

names=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E "^($PROJECT_NAME|$ALT_PROJECT_NAME)" || true)
if [ -z "$names" ]; then
  echo "== deployment =="
  echo "no container of $PROJECT_NAME is known to docker on this host"
  exit 0
fi

for name in $names; do
  echo "== state $name =="
  if ! docker inspect --format 'status={{.State.Status}} exit_code={{.State.ExitCode}} restarts={{.RestartCount}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} oom={{.State.OOMKilled}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name"; then
    echo "state unavailable for $name: docker inspect exited nonzero"
  fi
  echo "== logs $name =="
  if tail_text=$(docker logs --tail 200 "$name" 2>&1); then
    printf '%s\n' "$tail_text" | tail -c 20000
  else
    echo "log tail unavailable for $name: docker logs exited nonzero"
  fi
done
