#!/usr/bin/env bash
# Run slow stand bring-up work in the background, and join it bounded and fail-closed.
#
# The stand e2e workflow (.github/workflows/stand-e2e.yml) starts the service release pull,
# the worker release pull and the suite's uv environment on the control-plane host as soon
# as it is bootstrapped, and joins each one right before the step that consumes it. A job
# outlives the ssh session that started it; what it leaves behind is a set of files in
# <dir>, and a join reads nothing else:
#
#   <job>.started   epoch seconds, written before the job is launched
#   <job>.pid       the job's session leader; the job is its own process group
#   <job>.log       the job's combined output
#   <job>.finished  epoch seconds, written when the command returned
#   <job>.status    the command's exit code, written last and atomically
#
# A job that failed, hung or vanished is never passed over: join fails with the job's own
# exit code, or with its own code for a hang or a vanished job, and prints the log tail.
#
# Usage:
#   stand_background.sh start  <dir> <job> <command> [<argument>...]
#   stand_background.sh join   <dir> <job> <timeout-seconds>
#   stand_background.sh report <dir> <job>...
#
# `start` launches the command detached and returns at once; it refuses a job name that
# was already started in <dir>. `join` waits up to <timeout-seconds> for the job and exits
# 0 only when it exited 0; the whole log goes to a collapsed group, the tail to stderr on a
# failure. `report` prints one Markdown table row per job — its status and duration — and
# never fails on a job's outcome: a join is what judges.
#
# Exit codes of join (any other non-zero code is the job's own):
#   2    usage
#   124  the job did not finish within the bound; it is stopped
#   125  the job was never started, died without recording a status, or left a record
#        that is not one

set -uo pipefail

EXIT_USAGE=2
EXIT_TIMEOUT=124
EXIT_NO_RECORD=125
POLL_SECONDS="${STAND_BACKGROUND_POLL_SECONDS:-5}"
TAIL_LINES=60

usage() {
    sed -n '/^# Usage:/,/^# `start`/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
    exit "${EXIT_USAGE}"
}

valid_job_name() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

print_tail() {
    local log="$1"
    if [ -f "${log}" ]; then
        echo "----- last ${TAIL_LINES} lines of ${log} -----" >&2
        tail -n "${TAIL_LINES}" "${log}" >&2
        echo "----- end of ${log} -----" >&2
    else
        echo "(${log} does not exist: the job wrote no output)" >&2
    fi
}

start() {
    [ "$#" -ge 3 ] || usage
    local dir="$1" job="$2"
    shift 2
    valid_job_name "${job}" || usage
    mkdir -p "${dir}" && chmod 700 "${dir}" || exit "${EXIT_USAGE}"
    if [ -e "${dir}/${job}.started" ]; then
        echo "FATAL: background job ${job} was already started in ${dir}" >&2
        exit "${EXIT_USAGE}"
    fi
    date +%s > "${dir}/${job}.started" || exit "${EXIT_USAGE}"
    # A new session, so the job is its own process group (a join that times out stops
    # all of it) and nothing ties it to the ssh session that started it. Every stream
    # is redirected, or that session would wait for the job to close them.
    # shellcheck disable=SC2016  # expanded by the job's own shell
    setsid bash -c '
        dir="$1" job="$2"
        shift 2
        echo "$$" > "${dir}/${job}.pid"
        "$@" > "${dir}/${job}.log" 2>&1 < /dev/null
        status=$?
        date +%s > "${dir}/${job}.finished"
        echo "${status}" > "${dir}/${job}.status.next"
        mv "${dir}/${job}.status.next" "${dir}/${job}.status"
    ' stand-background "${dir}" "${job}" "$@" < /dev/null > /dev/null 2>&1 &
    echo "started background job ${job} (log: ${dir}/${job}.log)"
}

job_alive() {
    local pid_file="$1" pid
    # No pid yet: the job's shell has not got that far, so it is not known to be gone.
    [ -f "${pid_file}" ] || return 0
    pid="$(cat "${pid_file}")"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    kill -0 "${pid}" 2> /dev/null
}

stop_job() {
    local pid_file="$1" pid
    [ -f "${pid_file}" ] || return 0
    pid="$(cat "${pid_file}")"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 0
    kill -TERM -- "-${pid}" 2> /dev/null || true
}

join() {
    [ "$#" -eq 3 ] || usage
    local dir="$1" job="$2" timeout="$3"
    valid_job_name "${job}" || usage
    [[ "${timeout}" =~ ^[1-9][0-9]*$ ]] || usage
    local base="${dir}/${job}"
    if [ ! -f "${base}.started" ]; then
        echo "FATAL: background job ${job} was never started (${base}.started is missing)." >&2
        exit "${EXIT_NO_RECORD}"
    fi
    local deadline=$(($(date +%s) + timeout))
    while [ ! -f "${base}.status" ]; do
        if ! job_alive "${base}.pid"; then
            # It may have recorded its status between the two looks.
            [ -f "${base}.status" ] && break
            echo "FATAL: background job ${job} is gone and recorded no exit status." >&2
            print_tail "${base}.log"
            exit "${EXIT_NO_RECORD}"
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            echo "FATAL: background job ${job} did not finish within ${timeout}s; stopping it." >&2
            stop_job "${base}.pid"
            print_tail "${base}.log"
            exit "${EXIT_TIMEOUT}"
        fi
        sleep "${POLL_SECONDS}"
    done
    local status started finished
    status="$(cat "${base}.status")"
    started="$(cat "${base}.started")"
    finished="$(cat "${base}.finished" 2> /dev/null)"
    if ! [[ "${status}" =~ ^[0-9]+$ && "${started}" =~ ^[0-9]+$ && "${finished}" =~ ^[0-9]+$ ]]
    then
        echo "FATAL: background job ${job} left a record that is not one (status '${status}')." >&2
        print_tail "${base}.log"
        exit "${EXIT_NO_RECORD}"
    fi
    local seconds=$((finished - started))
    echo "::group::background job ${job}: log"
    cat "${base}.log" 2> /dev/null
    echo "::endgroup::"
    if [ "${status}" -ne 0 ]; then
        echo "FATAL: background job ${job} failed with exit ${status} after ${seconds}s." >&2
        print_tail "${base}.log"
        exit "${status}"
    fi
    echo "background job ${job} finished in ${seconds}s"
}

report() {
    [ "$#" -ge 2 ] || usage
    local dir="$1" job base status started finished now
    shift
    echo "| background job | status | seconds |"
    echo "| --- | --- | --- |"
    now="$(date +%s)"
    for job in "$@"; do
        valid_job_name "${job}" || usage
        base="${dir}/${job}"
        started="$(cat "${base}.started" 2> /dev/null)"
        if ! [[ "${started}" =~ ^[0-9]+$ ]]; then
            echo "| ${job} | not started | - |"
            continue
        fi
        status="$(cat "${base}.status" 2> /dev/null)"
        finished="$(cat "${base}.finished" 2> /dev/null)"
        if [[ "${status}" =~ ^[0-9]+$ && "${finished}" =~ ^[0-9]+$ ]]; then
            echo "| ${job} | exit ${status} | $((finished - started)) |"
        else
            echo "| ${job} | unfinished | $((now - started)) so far |"
        fi
    done
}

command="${1:-}"
[ "$#" -gt 0 ] && shift
case "${command}" in
    start) start "$@" ;;
    join) join "$@" ;;
    report) report "$@" ;;
    *) usage ;;
esac
