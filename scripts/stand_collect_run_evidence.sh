# Streamed to the stand host by the handoff step: writes the suite's own run
# evidence to stdout as a tar archive, so it survives the ephemeral host.
#
# The one invariant this file holds, in one place: **the evidence step never
# aborts.** Every piece is either collected or named; an optional file the run
# did not write is a named absence — the workflow says why, for the run that
# owed one — and never a failure of the collection. So a candidate is added
# only once it is known to exist: a missing operand makes `tar` exit 2, and
# under the caller's `pipefail` that would kill the very step that writes the
# stated reasons, taking the service tails and both fallbacks with it.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: stand_collect_run_evidence.sh <run-directory>" >&2
  exit 64
fi
run_directory="$1"

if [ ! -d "${run_directory}" ]; then
  # Nothing to collect is not a failure of the collection: hand back an empty
  # archive and let the caller name what is missing and why.
  tar -cf - --files-from=/dev/null
  exit 0
fi
cd "${run_directory}"

# Patterns and literals go through the same existence test, so neither kind can
# reach `tar` as a name that is not there.
files=()
for name in run-evidence-*.json debug-*.md target-app.log; do
  test ! -f "${name}" || files+=("${name}")
done

if [ "${#files[@]}" -eq 0 ]; then
  tar -cf - --files-from=/dev/null
else
  tar -cf - -- "${files[@]}"
fi
