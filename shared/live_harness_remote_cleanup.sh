set -eu
PROJECT_NAME=${1:?project name is required}
SERVICE_BASE=${2:-/opt/services}
ALT_PROJECT_NAME=$(printf '%s' "$PROJECT_NAME" | tr '-' '_')
SVC_DIR=${SERVICE_BASE%/}/$PROJECT_NAME
PROJECTS=$(printf '%s\n%s\n' "$PROJECT_NAME" "$ALT_PROJECT_NAME" | awk 'NF && !seen[$0]++')
for project in $PROJECTS; do
  for c in $(docker ps -aq --filter "name=^/${project}[-_]"); do
    label=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$c")
    if [ -n "$label" ] && [ "$label" != "<no value>" ]; then
      PROJECTS=$(printf '%s\n%s\n' "$PROJECTS" "$label" | awk 'NF && !seen[$0]++')
    fi
  done
done
if [ -d "$SVC_DIR/infra" ]; then
  for project in $PROJECTS; do
    (cd "$SVC_DIR/infra" && docker compose -p "$project" down --remove-orphans -v)
  done
fi
for project in $PROJECTS; do
  for c in $(docker ps -aq --filter "label=com.docker.compose.project=$project"); do
    docker rm -f -v "$c"
  done
  for c in $(docker ps -aq --filter "name=^/${project}[-_]"); do
    docker rm -f -v "$c"
  done
  for resource in volume network; do
    for id in $(docker "$resource" ls -q --filter "label=com.docker.compose.project=$project"); do
      docker "$resource" rm "$id"
    done
  done
done
rm -rf "$SVC_DIR"
remaining=
for project in $PROJECTS; do
  ids=$(docker ps -aq --filter "label=com.docker.compose.project=$project")
  if [ -n "$ids" ]; then
    remaining="$remaining label:$project:$ids"
  fi
  ids=$(docker ps -aq --filter "name=^/${project}[-_]")
  if [ -n "$ids" ]; then
    remaining="$remaining name:$project:$ids"
  fi
  for resource in volume network; do
    ids=$(docker "$resource" ls -q --filter "label=com.docker.compose.project=$project")
    if [ -n "$ids" ]; then
      remaining="$remaining $resource:$project:$ids"
    fi
  done
done
if [ -n "$remaining" ]; then
  echo "compose residue remains:$remaining" >&2
  exit 1
fi
test ! -e "$SVC_DIR"
