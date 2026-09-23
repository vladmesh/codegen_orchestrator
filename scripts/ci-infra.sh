#!/usr/bin/env bash
# Retry CI downloads, bound the docker steps in time, and name a failure that is the
# infrastructure's, not the code's.
#
# A job that fails because a download or registry did not answer, or because a step ran
# past its time bound, writes one line:
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
#   retry --step S --cause C [--attempt-timeout D] -- CMD...
#                                              run CMD up to 3 times with backoff;
#                                              mark and fail with its status when
#                                              every attempt failed. With D, each
#                                              attempt that runs past D is stopped
#                                              and counts as a failed attempt; when
#                                              the last one timed out, the cause is
#                                              C-timeout
#   pull-images --step S COMPOSE_FILE          pull every image COMPOSE_FILE runs but
#                                              does not build, each through retry with
#                                              a bound on every attempt
#   bound --step S --timeout D -- CMD...       run CMD for at most D; if it runs past
#                                              D, stop it, mark cause=step-timeout and
#                                              fail with timeout's status
#   watch --step S -- CMD...                   run CMD; if it fails and its output
#                                              carries a known CI-INFRA-CAUSE=<cause>
#                                              line, mark with that cause
#   expose --output NAME                       write this job's markers to the step
#                                              output NAME
#
# The job is $CI_INFRA_JOB, or $GITHUB_JOB when that is unset. Every field is one
# word of [A-Za-z0-9._/-], so the marker parses back with one regular expression.
#
# A duration D is a whole number with an optional s, m or h suffix (seconds without
# one), the form timeout(1) takes. A bounded command runs under coreutils timeout, which
# stops the command's whole process group, so a docker CLI a script started goes with
# the script. retry exports the attempt number as CI_INFRA_ATTEMPT to the command.

set -euo pipefail

MARKER_PREFIX="CI-INFRA-FAILURE:"
FIELD_PATTERN='^[A-Za-z0-9._/-]+$'
RETRY_ATTEMPTS=3
# Seconds before the second attempt; the third waits twice as long.
RETRY_DELAY="${CI_INFRA_RETRY_DELAY:-10}"
# The bound on one image pull attempt. A pull that hangs (an anonymous Docker Hub rate
# limit neither fails nor finishes) becomes a failed attempt the next one runs after.
# Measured pull steps take under 20 s. An exhausted image costs at most 3 x (90 s + the
# 30 s KILL_AFTER) plus 30 s of backoff; scripts/check-ci-gate.py reads these constants
# and fits that worst case, for every image a job pulls, into the job's timeout-minutes.
PULL_ATTEMPT_TIMEOUT="${CI_INFRA_PULL_ATTEMPT_TIMEOUT:-90s}"
# How long a bounded command gets to exit after TERM before timeout sends KILL.
KILL_AFTER=30s
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
    local note=${3:-"The step failed on CI infrastructure after its retries; code changes are not required."}
    local job="${CI_INFRA_JOB:-${GITHUB_JOB:?GITHUB_JOB or CI_INFRA_JOB is required}}"
    require_field job "$job"
    require_field step "$step"
    require_field cause "$cause"
    local marker="$MARKER_PREFIX job=$job step=$step cause=$cause"
    echo "::error title=CI infrastructure failure::$marker"
    {
        echo "$marker"
        echo
        echo "$note"
        echo
    } >>"${GITHUB_STEP_SUMMARY:?GITHUB_STEP_SUMMARY is required}"
    echo "$marker" >>"$(markers_file)"
}

parse_step_and_cause() {
    STEP="" CAUSE="" TIMEOUT="" ATTEMPT_TIMEOUT=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --step) STEP=${2:-}; shift 2 ;;
            --cause) CAUSE=${2:-}; shift 2 ;;
            --timeout) TIMEOUT=${2:-}; shift 2 ;;
            --attempt-timeout) ATTEMPT_TIMEOUT=${2:-}; shift 2 ;;
            --) shift; break ;;
            *) break ;;
        esac
    done
    REST=("$@")
}

# A duration in seconds, or die: a whole number with an optional s, m or h suffix.
duration_seconds() {
    local duration=$1
    [[ "$duration" =~ ^([0-9]+)([smh]?)$ ]] || die "duration '$duration' is not N, Ns, Nm or Nh"
    local value=${BASH_REMATCH[1]}
    case "${BASH_REMATCH[2]}" in
        m) value=$((value * 60)) ;;
        h) value=$((value * 3600)) ;;
    esac
    [ "$value" -gt 0 ] || die "duration '$duration' is not positive"
    echo "$value"
}

# Run CMD under timeout for at most $1 seconds; its status is the return status, and
# TIMED_OUT says whether the bound stopped it. timeout exits 124 when its TERM stopped
# the command and 137 when it had to KILL, but a command can exit 124 or 137 on its own
# too, so a status alone proves nothing: the bound fired only if the command also ran
# for the whole bound. Whole seconds suffice, since timeout never fires early and the
# bound is a whole number of seconds.
bounded() {
    local seconds=$1 status=0 started
    shift
    started=$(date +%s)
    timeout --kill-after="$KILL_AFTER" "${seconds}s" "$@" || status=$?
    TIMED_OUT=false
    if { [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; } \
        && [ $(($(date +%s) - started)) -ge "$seconds" ]; then
        TIMED_OUT=true
    fi
    return "$status"
}

retry() {
    local step=$1 cause=$2 timeout=$3
    shift 3
    [ $# -gt 0 ] || die "retry needs a command after --"
    local seconds=""
    [ -z "$timeout" ] || seconds=$(duration_seconds "$timeout")
    local attempt status=0 failure
    TIMED_OUT=false
    for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
        if [ "$attempt" -gt 1 ]; then
            local wait=$((RETRY_DELAY * (attempt - 1)))
            echo "ci-infra: attempt $((attempt - 1)) of $RETRY_ATTEMPTS $failure; retrying in ${wait}s"
            sleep "$wait"
        fi
        status=0
        if [ -n "$seconds" ]; then
            CI_INFRA_ATTEMPT=$attempt bounded "$seconds" "$@" || status=$?
        else
            CI_INFRA_ATTEMPT=$attempt "$@" || status=$?
        fi
        [ "$status" -eq 0 ] && return 0
        if [ "$TIMED_OUT" = true ]; then
            failure="timed out after $timeout"
        else
            failure="failed with status $status"
        fi
    done
    echo "ci-infra: all $RETRY_ATTEMPTS attempts failed; the last $failure"
    if [ "$TIMED_OUT" = true ]; then
        mark "$step" "$cause-timeout"
    else
        mark "$step" "$cause"
    fi
    return "$status"
}

bound() {
    local step=$1 timeout=$2
    shift 2
    [ $# -gt 0 ] || die "bound needs a command after --"
    local seconds status=0
    seconds=$(duration_seconds "$timeout")
    bounded "$seconds" "$@" || status=$?
    if [ "$TIMED_OUT" = true ]; then
        echo "ci-infra: the step ran past its bound of $timeout and was stopped"
        mark "$step" step-timeout "The step ran past its bound of $timeout and was stopped. A registry or download that hangs is the usual cause; a hang in the code under test is possible too, so read the step log before rerunning."
    fi
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
        retry "$step" image-pull "$PULL_ATTEMPT_TIMEOUT" docker pull "$image"
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
[ -n "$command" ] || die "usage: ci-infra.sh mark|retry|pull-images|bound|watch|expose ..."
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
        retry "$STEP" "$CAUSE" "$ATTEMPT_TIMEOUT" "${REST[@]}"
        ;;
    pull-images)
        parse_step_and_cause "$@"
        [ -n "$STEP" ] && [ ${#REST[@]} -eq 1 ] || die "pull-images needs --step and one compose file"
        pull_images "$STEP" "${REST[0]}"
        ;;
    bound)
        parse_step_and_cause "$@"
        [ -n "$STEP" ] && [ -n "$TIMEOUT" ] || die "bound needs --step and --timeout"
        bound "$STEP" "$TIMEOUT" "${REST[@]}"
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
