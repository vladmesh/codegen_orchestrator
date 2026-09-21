set -eu

# This script is streamed to deployment targets by both application undeploy and
# the live-test recovery sweep. Keep the discovery, deletion and verification
# order here so neither caller can grow a second cleanup policy.
PROJECT_NAME=${1:?project name is required}
SERVICE_BASE=${2:-/opt/services}
case "$PROJECT_NAME" in
  *[!A-Za-z0-9_-]*)
    echo "project cleanup failed: unsafe project name" >&2
    exit 1
    ;;
esac
ALT_PROJECT_NAME=$(printf '%s' "$PROJECT_NAME" | tr '-' '_')
SVC_DIR=${SERVICE_BASE%/}/$PROJECT_NAME
CANDIDATES=$(mktemp "${TMPDIR:-/tmp}/codegen-project-cleanup.XXXXXX")
RECOVERY_DIR=${SERVICE_BASE%/}/.codegen-cleanup-candidates
RECOVERY_CANDIDATES="$RECOVERY_DIR/$PROJECT_NAME"

trap 'rm -f "$CANDIDATES"' EXIT HUP INT TERM

fail() {
  echo "project cleanup failed: $*" >&2
  exit 1
}

add_project() {
  candidate=$1
  PROJECTS=$(printf '%s\n%s\n' "$PROJECTS" "$candidate" | awk 'NF && !seen[$0]++')
}

add_candidate() {
  kind=$1
  identifier=$2
  if ! grep -Fqx -- "$kind $identifier" "$CANDIDATES"; then
    printf '%s %s\n' "$kind" "$identifier" >> "$CANDIDATES"
  fi
}

candidate_ids() {
  kind=$1
  awk -v kind="$kind" '$1 == kind { print $2 }' "$CANDIDATES"
}

candidate_identifier_is_safe() {
  case "$1" in
    ""|*[!A-Za-z0-9:._-]*) return 1 ;;
  esac
  return 0
}

load_recovery_candidates() {
  if [ ! -f "$RECOVERY_CANDIDATES" ]; then
    return
  fi
  while IFS=' ' read -r kind identifier extra; do
    if [ -z "$kind" ]; then
      continue
    fi
    if [ -n "$extra" ]; then
      fail "malformed recovery candidate record"
    fi
    case "$kind" in
      image|volume|network|container) ;;
      *) fail "unknown recovery candidate kind $kind" ;;
    esac
    if ! candidate_identifier_is_safe "$identifier"; then
      fail "unsafe recovery candidate $kind"
    fi
    add_candidate "$kind" "$identifier"
  done < "$RECOVERY_CANDIDATES"
}

persist_recovery_candidates() {
  mkdir -p "$RECOVERY_DIR" || fail "cannot create recovery candidate directory"
  temporary="${RECOVERY_CANDIDATES}.tmp.$$"
  (umask 077; awk '$1 == "image" || $1 == "volume" || $1 == "network" || $1 == "container"' "$CANDIDATES" > "$temporary") \
    || fail "cannot write recovery candidates"
  mv "$temporary" "$RECOVERY_CANDIDATES" || fail "cannot retain recovery candidates"
}

clear_recovery_candidates() {
  if [ -f "$RECOVERY_CANDIDATES" ]; then
    rm -f "$RECOVERY_CANDIDATES" || fail "cannot clear recovery candidates"
  fi
}

is_project_label() {
  label=$1
  for project in $PROJECTS; do
    if [ "$label" = "$project" ]; then
      return 0
    fi
  done
  return 1
}

is_project_image_ref() {
  reference=$1
  project=$2
  repository=${reference%:*}
  image_name=${repository##*/}
  case "$image_name" in
    "$project"-backend|"$project"-tg-bot|"$project"-frontend|"$project"-notifications-worker)
      return 0
      ;;
  esac
  return 1
}

is_any_project_image_ref() {
  reference=$1
  for project in $PROJECTS; do
    if is_project_image_ref "$reference" "$project"; then
      return 0
    fi
  done
  return 1
}

capture_image() {
  image=$1
  label=$(docker image inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$image") \
    || fail "cannot inspect image candidate $image"
  tags=$(docker image inspect -f '{{range .RepoTags}}{{println .}}{{end}}' "$image") \
    || fail "cannot inspect image tags for $image"
  selected=false

  if is_project_label "$label"; then
    selected=true
  fi
  for tag in $tags; do
    if is_any_project_image_ref "$tag"; then
      selected=true
    fi
  done
  if [ "$selected" != true ]; then
    return
  fi

  # Deleting an image ID removes every tag on it. A project label or one matching
  # tag is not enough when that ID also carries another application's tag.
  for tag in $tags; do
    if ! is_any_project_image_ref "$tag"; then
      fail "ambiguous image candidate $image has non-project tag $tag"
    fi
  done
  add_candidate image "$image"
}

is_anonymous_volume() {
  # Docker generates 64-hex names for anonymous volumes. A named external
  # volume is deliberately not selected without the Compose project label.
  printf '%s' "$1" | grep -Eq '^[0-9a-f]{64}$'
}

capture_container_volumes() {
  container=$1
  volumes=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' "$container") \
    || fail "cannot inspect mounts for container $container"
  for volume in $volumes; do
    label=$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$volume") \
      || fail "cannot inspect volume candidate $volume"
    if is_project_label "$label" || is_anonymous_volume "$volume"; then
      add_candidate volume "$volume"
    fi
  done
}

capture_project_containers() {
  for project in $PROJECTS; do
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$project") \
      || fail "cannot list containers for project label $project"
    for container in $ids; do
      add_candidate container "$container"
    done
    ids=$(docker ps -aq --filter "name=^/${project}[-_]") \
      || fail "cannot list containers for project name $project"
    for container in $ids; do
      add_candidate container "$container"
    done
  done
}

capture_project_artifacts() {
  capture_project_containers

  for container in $(candidate_ids container); do
    # A recovery record can outlive a target container: the first attempt removes
    # it before discovering that another live container still references one of
    # its volumes. The image and volume candidates remain the recovery contract;
    # a vanished container must not prevent their next cleanup attempt.
    if ! docker container inspect "$container" >/dev/null 2>&1; then
      continue
    fi
    capture_container_volumes "$container"
    image=$(docker inspect -f '{{.Image}}' "$container") \
      || fail "cannot inspect image for container $container"
    capture_image "$image"
  done

  # A crashed run can leave no containers or service directory. The only
  # directory-less candidates accepted are Compose-labelled resources and the
  # exact project/service image names above.
  for project in $PROJECTS; do
    ids=$(docker volume ls -q --filter "label=com.docker.compose.project=$project") \
      || fail "cannot list volumes for project $project"
    for volume in $ids; do
      add_candidate volume "$volume"
    done
    ids=$(docker network ls -q --filter "label=com.docker.compose.project=$project") \
      || fail "cannot list networks for project $project"
    for network in $ids; do
      add_candidate network "$network"
    done
    ids=$(docker image ls -q --filter "label=com.docker.compose.project=$project") \
      || fail "cannot list images for project $project"
    for image in $ids; do
      capture_image "$image"
    done
  done

  # Image labels are not guaranteed for pulled application images. Inspecting
  # IDs is read-only; `capture_image` still accepts only the bounded service
  # repository names and rejects a multi-owner image ID.
  ids=$(docker image ls -q) || fail "cannot list local images"
  for image in $ids; do
    capture_image "$image"
  done
}

remove_project_containers() {
  for container in $(candidate_ids container); do
    if docker container inspect "$container" >/dev/null 2>&1; then
      docker rm -f -v "$container" || fail "cannot remove container $container"
    fi
  done
}

assert_no_project_containers() {
  remaining=
  for project in $PROJECTS; do
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$project") \
      || fail "cannot verify containers for project label $project"
    if [ -n "$ids" ]; then
      remaining="$remaining label:$project:$ids"
    fi
    ids=$(docker ps -aq --filter "name=^/${project}[-_]") \
      || fail "cannot verify containers for project name $project"
    if [ -n "$ids" ]; then
      remaining="$remaining name:$project:$ids"
    fi
  done
  if [ -n "$remaining" ]; then
    fail "project containers remain:$remaining"
  fi
}

assert_candidates_unreferenced() {
  for image in $(candidate_ids image); do
    ids=$(docker ps -aq --filter "ancestor=$image") \
      || fail "cannot inspect live references to image $image"
    if [ -n "$ids" ]; then
      fail "image candidate $image remains referenced by containers:$ids"
    fi
  done
  for volume in $(candidate_ids volume); do
    if ! docker volume inspect "$volume" >/dev/null 2>&1; then
      continue
    fi
    ids=$(docker ps -aq --filter "volume=$volume") \
      || fail "cannot inspect live references to volume $volume"
    if [ -n "$ids" ]; then
      fail "volume candidate $volume remains referenced by containers:$ids"
    fi
  done
}

remove_candidates() {
  for image in $(candidate_ids image); do
    if docker image inspect "$image" >/dev/null 2>&1; then
      docker image rm "$image" || fail "cannot remove image $image"
    fi
  done
  for volume in $(candidate_ids volume); do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      docker volume rm "$volume" || fail "cannot remove volume $volume"
    fi
  done
  for network in $(candidate_ids network); do
    if docker network inspect "$network" >/dev/null 2>&1; then
      docker network rm "$network" || fail "cannot remove network $network"
    fi
  done
}

verify_candidates_absent() {
  remaining=
  for image in $(candidate_ids image); do
    if docker image inspect "$image" >/dev/null 2>&1; then
      remaining="$remaining image:$image"
    fi
  done
  for volume in $(candidate_ids volume); do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      remaining="$remaining volume:$PROJECT_NAME:$volume"
    fi
  done
  for network in $(candidate_ids network); do
    if docker network inspect "$network" >/dev/null 2>&1; then
      remaining="$remaining network:$PROJECT_NAME:$network"
    fi
  done
  if [ -n "$remaining" ]; then
    fail "selected project artifacts remain:$remaining"
  fi
}

PROJECTS=$(printf '%s\n%s\n' "$PROJECT_NAME" "$ALT_PROJECT_NAME" | awk 'NF && !seen[$0]++')

# Resolve an actual Compose label from either supported container-name spelling
# before candidates are captured. A directory-less historical residue cannot
# add a guessed label: it must match the explicit project-artifact selectors.
for project in $PROJECTS; do
  ids=$(docker ps -aq --filter "name=^/${project}[-_]") \
    || fail "cannot discover Compose project for $project"
  for container in $ids; do
    label=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$container") \
      || fail "cannot inspect Compose label for container $container"
    if [ -n "$label" ] && [ "$label" != "<no value>" ]; then
      add_project "$label"
    fi
  done
done

# Recovery order is intentional: capture exact candidates, bring the Compose
# project down, remove its containers, prove no live reference remains, then
# remove and verify artifacts. The recovery record and then the service directory
# are the final commit points, so directory-less runs can retry too.
load_recovery_candidates
capture_project_artifacts
persist_recovery_candidates

if [ -d "$SVC_DIR/infra" ]; then
  for project in $PROJECTS; do
    (
      cd "$SVC_DIR/infra"
      docker compose -p "$project" --env-file ../.env \
        -f compose.base.yml -f compose.prod.yml down --remove-orphans -v
    ) || fail "docker compose down failed for project $project"
  done
fi

remove_project_containers
assert_no_project_containers
assert_candidates_unreferenced
remove_candidates
verify_candidates_absent
clear_recovery_candidates

rm -rf "$SVC_DIR"
if [ -e "$SVC_DIR" ]; then
  fail "service directory remains:$SVC_DIR"
fi
