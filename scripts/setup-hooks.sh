#!/bin/bash
# Setup script to install git hooks
# This configures git to use hooks from .githooks/ directory

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}🔧 Setting up git hooks...${NC}"

# Get the root of the git repository
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$REPO_ROOT"

# Make hooks executable
chmod +x .githooks/pre-commit
chmod +x .githooks/pre-push

# Configure git to use .githooks directory
git config core.hooksPath .githooks

echo -e "${GREEN}✅ Git hooks installed successfully!${NC}"
echo ""
echo "Installed hooks:"
echo "  • pre-commit: Format and lint checks"
echo "  • pre-push: Unit tests"
echo ""
echo "To skip hooks temporarily (NOT recommended):"
echo "  git commit --no-verify"
echo "  git push --no-verify"
