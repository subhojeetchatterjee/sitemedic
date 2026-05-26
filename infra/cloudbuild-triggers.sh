#!/bin/bash
# Cloud Build trigger setup for SiteMedic
# Creates three automated build triggers for dev, staging, and prod environments
# Usage: bash infra/cloudbuild-triggers.sh YOUR_PROJECT_ID YOUR_GITHUB_REPO

set -e

PROJECT_ID=${1:-}
GITHUB_REPO=${2:-}  # Format: your-org/sitemedic
GITHUB_OWNER=${GITHUB_REPO%/*}

if [[ -z "$PROJECT_ID" ]] || [[ -z "$GITHUB_REPO" ]]; then
    echo "Usage: bash infra/cloudbuild-triggers.sh PROJECT_ID GITHUB_ORG/GITHUB_REPO"
    echo "Example: bash infra/cloudbuild-triggers.sh my-project-id your-org/sitemedic"
    exit 1
fi

echo "Creating Cloud Build triggers for ${GITHUB_REPO} in project ${PROJECT_ID}..."

# Trigger 1: Pull Requests (tests only, no deployment)
echo "Creating PR trigger (tests only)..."
gcloud builds triggers create github \
    --project="${PROJECT_ID}" \
    --name="sitemedic-pr-tests" \
    --repo-name="sitemedic" \
    --repo-owner="${GITHUB_OWNER}" \
    --pull-request-pattern="^.*$" \
    --build-config="cloudbuild.yaml" \
    --substitutions="_ENVIRONMENT=dev,_REGION=us-central1" \
    --no-autodetect \
    --require-approval

# Trigger 2: Development branch (auto-deploy to dev)
echo "Creating dev trigger (auto-deploy)..."
gcloud builds triggers create github \
    --project="${PROJECT_ID}" \
    --name="sitemedic-dev-deploy" \
    --repo-name="sitemedic" \
    --repo-owner="${GITHUB_OWNER}" \
    --branch-pattern="^develop$" \
    --build-config="cloudbuild.yaml" \
    --substitutions="_ENVIRONMENT=dev,_REGION=us-central1" \
    --no-autodetect

# Trigger 3: Staging branch (auto-deploy to staging)
echo "Creating staging trigger (auto-deploy)..."
gcloud builds triggers create github \
    --project="${PROJECT_ID}" \
    --name="sitemedic-staging-deploy" \
    --repo-name="sitemedic" \
    --repo-owner="${GITHUB_OWNER}" \
    --branch-pattern="^staging$" \
    --build-config="cloudbuild.yaml" \
    --substitutions="_ENVIRONMENT=staging,_REGION=us-central1" \
    --no-autodetect

# Trigger 4: Main branch (requires approval before deploy to prod)
echo "Creating prod trigger (requires approval)..."
gcloud builds triggers create github \
    --project="${PROJECT_ID}" \
    --name="sitemedic-prod-deploy" \
    --repo-name="sitemedic" \
    --repo-owner="${GITHUB_OWNER}" \
    --branch-pattern="^main$" \
    --build-config="cloudbuild.yaml" \
    --substitutions="_ENVIRONMENT=prod,_REGION=us-central1" \
    --no-autodetect \
    --require-approval

echo ""
echo "✅ Cloud Build triggers created successfully!"
echo ""
echo "Next steps:"
echo "1. Connect your GitHub repository in Cloud Console:"
echo "   https://console.cloud.google.com/cloud-build/triggers"
echo ""
echo "2. Verify triggers:"
echo "   gcloud builds triggers list --project=${PROJECT_ID}"
echo ""
echo "3. Test a trigger manually:"
echo "   gcloud builds submit --config=cloudbuild.yaml --substitutions=_ENVIRONMENT=dev,_REGION=us-central1"
