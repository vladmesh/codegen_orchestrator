#!/bin/bash
set -e

echo "=== Coding Worker Starting ==="
echo "Repository: ${REPO}"
echo "Model: ${MODEL:-claude-sonnet-4-5-20250929}"

# Validate required environment variables
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${FACTORY_API_KEY:?FACTORY_API_KEY is required}"
: "${REPO:?REPO is required}"
: "${TASK_CONTENT:?TASK_CONTENT is required}"

# Configure git
git config --global user.email "factory-bot@codegen.ai"
git config --global user.name "Factory Bot"

# Clone repository
echo "=== Cloning repository ==="
git clone "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" .

# Write context files
echo "=== Writing context files ==="
if [ -n "${AGENTS_CONTENT}" ]; then
    echo "${AGENTS_CONTENT}" > AGENTS.md
    echo "AGENTS.md written"
fi

echo "${TASK_CONTENT}" > TASK.md
echo "TASK.md written"

# Show task
echo "=== Task Content ==="
cat TASK.md

# Run Factory Droid
echo "=== Running Factory Droid ==="
droid exec -f TASK.md \
    --skip-permissions-unsafe \
    -m "${MODEL:-claude-sonnet-4-5-20250929}" \
    2>&1 | tee /tmp/droid_output.txt

DROID_EXIT_CODE=${PIPESTATUS[0]}

# Check for changes
echo "=== Checking for changes ==="
if git status --porcelain | grep -q .; then
    echo "Changes detected, committing..."
    git add -A
    git commit -m "feat: ${TASK_TITLE:-AI generated changes}"
    
    echo "=== Pushing changes ==="
    git push
    
    COMMIT_SHA=$(git rev-parse HEAD)
    echo "Pushed commit: ${COMMIT_SHA}"
else
    echo "No changes to commit"
fi

echo "=== Coding Worker Complete ==="
echo "Exit code: ${DROID_EXIT_CODE}"

exit ${DROID_EXIT_CODE}
