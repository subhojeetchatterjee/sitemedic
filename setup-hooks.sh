#!/bin/bash
# Set up Git hooks for SiteMedic development
# Run this once after cloning: ./setup-hooks.sh

set -e

echo "Installing Git hooks..."

# Create .git/hooks directory if it doesn't exist
mkdir -p .git/hooks

# Copy pre-commit hook
cp .githooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit

echo "✅ Git hooks installed successfully"
echo ""
echo "Pre-commit hook will prevent you from committing:"
echo "  - .env files (use .env.template instead)"
echo "  - .tfvars files (keep locally, don't commit)"
echo "  - Private keys and service account files"
echo "  - Possible API keys and secrets"
echo ""
echo "To bypass the hook (not recommended): git commit --no-verify"
