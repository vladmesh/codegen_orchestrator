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

# For persistent containers: don't run agent command directly
# Instead, keep container alive and wait for commands via docker exec
if [ -n "$AGENT_COMMAND" ]; then
    echo "[entrypoint] Agent configured: $AGENT_COMMAND"
    echo "[entrypoint] Container ready to receive commands via 'docker exec'"
else
    echo "[entrypoint] No AGENT_COMMAND set"
fi

# Keep container running (acts as daemon)
echo "[entrypoint] Starting daemon mode..."
exec tail -f /dev/null
