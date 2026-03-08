#!/bin/bash
# Git credential helper that gets token from GitHub App via the langgraph container
# Usage: git config credential.helper '/path/to/git-credential-github-app.sh'

if [ "$1" != "get" ]; then
    exit 0
fi

# Read input
while IFS= read -r line; do
    case "$line" in
        host=*) HOST="${line#host=}" ;;
        protocol=*) PROTOCOL="${line#protocol=}" ;;
        "") break ;;
    esac
done

if [ "$HOST" != "github.com" ]; then
    exit 0
fi

# Get token from GitHub App
TOKEN=$(docker compose -f /home/vlad/projects/codegen_orchestrator/docker-compose.yml exec -T langgraph python -c "
from shared.clients.github import GitHubAppClient
import asyncio
async def t():
    c = GitHubAppClient()
    print(await c.get_org_token('project-factory-organization'))
asyncio.run(t())
" 2>/dev/null)

if [ -z "$TOKEN" ]; then
    exit 1
fi

echo "protocol=https"
echo "host=github.com"
echo "username=x-access-token"
echo "password=$TOKEN"
