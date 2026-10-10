#!/bin/sh
# The one way this product prepares its Python environments.
#
#   sh scripts/prepare-env.sh [--runtime] ENV...
#
# ENV is `root` (the kit tooling at the checkout root) or a service with
# services/<name>/pyproject.toml. Each stage names exactly the environments it needs:
#
#   make setup                  root and every service
#   CI image generation         root, backend and, with a bot, tg_bot (binding preflight
#                               reads both service environments)
#   backend Docker dev target   root backend, then --runtime tg_bot when the bot exists
#   service Docker runtime      --runtime <service>
#
# Every environment is `uv sync --frozen` from its own lock into its own .venv inside this
# checkout. --runtime adds --no-dev --no-install-project and refuses root, so runtime images
# never carry kit tooling or dev dependencies. Unknown or missing requested environments are
# refused before anything is synced; the first failed sync stops the command with a non-zero
# status. Running it again is safe: uv leaves a synced environment unchanged.
set -eu

usage() {
	echo "usage: sh scripts/prepare-env.sh [--runtime] ENV..." >&2
	exit 2
}

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
runtime=0
if [ "${1-}" = "--runtime" ]; then
	runtime=1
	shift
fi
[ "$#" -gt 0 ] || usage
# Environments belong to this checkout, never to a shared or caller-selected location.
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV

directory() {
	if [ "$1" = root ]; then
		echo "$root"
	else
		echo "$root/services/$1"
	fi
}

for env in "$@"; do
	case "$env" in
	root)
		if [ "$runtime" -eq 1 ]; then
			echo "prepare-env: root tooling has no runtime environment" >&2
			exit 2
		fi
		;;
	"" | -* | *[!A-Za-z0-9_]*)
		echo "prepare-env: unknown environment '$env'" >&2
		exit 2
		;;
	esac
	if [ ! -f "$(directory "$env")/pyproject.toml" ]; then
		project=services/$env/pyproject.toml
		[ "$env" != root ] || project=pyproject.toml
		echo "prepare-env: $env environment is required, but $project is missing" >&2
		exit 1
	fi
done

for env in "$@"; do
	dir=$(directory "$env")
	if [ "$runtime" -eq 1 ]; then
		echo ">> Preparing $env runtime environment"
		flags="--frozen --no-dev --no-install-project"
	else
		echo ">> Preparing $env environment"
		flags="--frozen"
	fi
	# shellcheck disable=SC2086 # fixed flag words
	if ! (cd "$dir" && uv sync $flags); then
		echo "prepare-env: $env environment sync failed" >&2
		exit 1
	fi
	if [ ! -x "$dir/.venv/bin/python" ]; then
		echo "prepare-env: $env environment has no interpreter at $dir/.venv/bin/python" >&2
		exit 1
	fi
done
