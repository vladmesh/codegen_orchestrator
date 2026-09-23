#!/usr/bin/env bash
# Retry CI downloads and name a failure that is the infrastructure's, not the code's.
#
# A job that fails because a download or registry did not answer writes one line:
#
#   CI-INFRA-FAILURE: job=<job> step=<step> cause=<cause>
#
# as an ::error annotation, into the job summary, and into a per-job file that
# `expose` turns into a job output, so the Required CI Gate can repeat it. The marker
# never makes anything pass: every command here keeps the failing exit status. The
# format and the steps that emit it are documented in docs/TESTING.md.
#
# Subcommands:
#   mark  --step S --cause C                   write the marker and succeed
#   retry --step S --cause C -- CMD...         run CMD up to 3 times with backoff;
#                                              mark and fail with its status when
#                                              every attempt failed
#   pull-images --step S COMPOSE_FILE          pull every image COMPOSE_FILE runs but
#                                              does not build, each through retry
#   watch --step S -- CMD...                   run CMD; if it fails and its output
#                                              carries a known CI-INFRA-CAUSE=<cause>
#                                              line, mark with that cause
#   expose --output NAME                       write this job's markers to the step
#                                              output NAME
#
# The job is $CI_INFRA_JOB, or $GITHUB_JOB when that is unset. Every field is one
# word of [A-Za-z0-9._/-], so the marker parses back with one regular expression.

set -euo pipefail

MARKER_PREFIX="CI-INFRA-FAILURE:"
FIELD_PATTERN='^[A-Za-z0-9._/-]+$'
RETRY_ATTEMPTS=3
# Seconds before the second attempt; the third waits twice as long.
RETRY_DELAY="${CI_INFRA_RETRY_DELAY:-10}"
# The only causes `watch` accepts from a command's output. A line a build prints is
# trusted as infrastructure only when it names one of these.
WATCHED_CAUSES=("claude-installer-fetch")

die() {
    echo "ci-infra: $*" >&2
    exit 2
}

markers_file() {
    echo "${RUNNER_TEMP:?RUNNER_TEMP is required}/ci-infra-markers"
}

require_field() {
    local name=$1 value=$2
    [[ "$value" =~ $FIELD_PATTERN ]] || die "$name '$value' is not one word of [A-Za-z0-9._/-]"
}

mark() {
    local step=$1 cause=$2
    local job="${CI_INFRA_JOB:-${GITHUB_JOB:?GITHUB_JOB or CI_INFRA_JOB is required}}"
    require_field job "$job"
    require_field step "$step"
    require_field cause "$cause"
    local marker="$MARKER_PREFIX job=$job step=$step cause=$cause"
    echo "::error title=CI infrastructure failure::$marker"
    {
        echo "$marker"
        echo
        echo "The step failed on CI infrastructure after its retries; code changes are not required."
        echo
    } >>"${GITHUB_STEP_SUMMARY:?GITHUB_STEP_SUMMARY is required}"
    echo "$marker" >>"$(markers_file)"
}

parse_step_and_cause() {
    STEP="" CAUSE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --step) STEP=${2:-}; shift 2 ;;
            --cause) CAUSE=${2:-}; shift 2 ;;
            --) shift; break ;;
            *) break ;;
        esac
    done
    REST=("$@")
}

retry() {
    local step=$1 cause=$2
    shift 2
    [ $# -gt 0 ] || die "retry needs a command after --"
    local attempt status=0
    for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
        if [ "$attempt" -gt 1 ]; then
            local wait=$((RETRY_DELAY * (attempt - 1)))
            echo "ci-infra: attempt $((attempt - 1)) of $RETRY_ATTEMPTS failed with status $status; retrying in ${wait}s"
            sleep "$wait"
        fi
        status=0
        "$@" || status=$?
        [ "$status" -eq 0 ] && return 0
    done
    echo "ci-infra: all $RETRY_ATTEMPTS attempts failed; the last exited with status $status"
    mark "$step" "$cause"
    return "$status"
}

# The images a compose file runs without building them: the ones a registry has to
# serve. An image another service of the file builds is local, not pulled.
external_images() {
    docker compose -f "$1" config --format json | jq -r '
        [.services[] | select(.build != null) | .image | select(. != null)] as $built
        | .services[]
        | select(.build == null and .image != null)
        | .image
        | select(. as $image | $built | index($image) | not)
    ' | sort -u
}

pull_images() {
    local step=$1 compose_file=$2
    local images image
    # Reading the file is not a download: a broken compose file fails here, unmarked.
    images=$(external_images "$compose_file")
    for image in $images; do
        retry "$step" image-pull docker pull "$image"
    done
}

watch() {
    local step=$1
    shift
    [ $# -gt 0 ] || die "watch needs a command after --"
    local log status=0 cause
    log=$(mktemp "${RUNNER_TEMP:?RUNNER_TEMP is required}/ci-infra-watch.XXXXXX")
    set +e
    "$@" 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    if [ "$status" -ne 0 ]; then
        for cause in "${WATCHED_CAUSES[@]}"; do
            if grep -q "CI-INFRA-CAUSE=${cause}\b" "$log"; then
                mark "$step" "$cause"
            fi
        done
    fi
    return "$status"
}

expose() {
    local output=$1 file
    require_field output "$output"
    file=$(markers_file)
    [ -s "$file" ] || return 0
    local delimiter="ci_infra_$RANDOM$RANDOM"
    {
        echo "$output<<$delimiter"
        cat "$file"
        echo "$delimiter"
    } >>"${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
}

command=${1:-}
[ -n "$command" ] || die "usage: ci-infra.sh mark|retry|pull-images|watch|expose ..."
shift
case "$command" in
    mark)
        parse_step_and_cause "$@"
        [ -n "$STEP" ] && [ -n "$CAUSE" ] || die "mark needs --step and --cause"
        mark "$STEP" "$CAUSE"
        ;;
    retry)
        parse_step_and_cause "$@"
        [ -n "$STEP" ] && [ -n "$CAUSE" ] || die "retry needs --step and --cause"
        retry "$STEP" "$CAUSE" "${REST[@]}"
        ;;
    pull-images)
        parse_step_and_cause "$@"
        [ -n "$STEP" ] && [ ${#REST[@]} -eq 1 ] || die "pull-images needs --step and one compose file"
        pull_images "$STEP" "${REST[0]}"
        ;;
    watch)
        parse_step_and_cause "$@"
        [ -n "$STEP" ] || die "watch needs --step"
        watch "$STEP" "${REST[@]}"
        ;;
    expose)
        [ "${1:-}" = "--output" ] && [ -n "${2:-}" ] || die "expose needs --output NAME"
        expose "$2"
        ;;
    *)
        die "unknown command $command"
        ;;
esac
