#!/bin/bash
set -e

# ===================================
# Universal Worker Entrypoint
# Handles dynamic setup and agent startup
# ===================================

# Run install commands from INSTALL_COMMANDS env var (JSON array)
if [ -n "$INSTALL_COMMANDS" ]; then
    echo "[entrypoint] Running install commands..."
    echo "$INSTALL_COMMANDS" | jq -r '.[]' | while read -r cmd; do
        echo "[entrypoint] $ $cmd"
        eval "$cmd"
    done
fi

# Export additional env vars from ENV_VARS (JSON object)
if [ -n "$ENV_VARS" ]; then
    echo "[entrypoint] Setting environment variables..."
    for key in $(echo "$ENV_VARS" | jq -r 'keys[]'); do
        value=$(echo "$ENV_VARS" | jq -r --arg k "$key" '.[$k]')
        export "$key=$value"
        echo "[entrypoint] $key=***"
    done
fi

# Run the agent command or fall back to shell
if [ -n "$AGENT_COMMAND" ]; then
    echo "[entrypoint] Starting agent: $AGENT_COMMAND"
    exec $AGENT_COMMAND "$@"
else
    echo "[entrypoint] No AGENT_COMMAND set, starting shell"
    exec /bin/bash
fi
