#!/usr/bin/env bash
# SiteMedic full deploy: demo-app + agent + frontend to Cloud Run.
#
# Usage:
#   ./infra/deploy.sh                    # deploy all services
#   ./infra/deploy.sh --demo-public      # deploy with DEMO_PUBLIC=true (hosted judges URL)
#   ./infra/deploy.sh --skip-frontend    # skip frontend deploy (agent only)
#   ./infra/deploy.sh --skip-build       # skip docker build (re-deploy existing images)
#
# Prerequisites: gcloud auth login, gcloud config set project <PROJECT_ID>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/.env" 2>/dev/null || true

# ── Flags ─────────────────────────────────────────────────────────────────
DEMO_PUBLIC=false
SKIP_FRONTEND=false
SKIP_BUILD=false
for arg in "$@"; do
  case $arg in
    --demo-public)    DEMO_PUBLIC=true ;;
    --skip-frontend)  SKIP_FRONTEND=true ;;
    --skip-build)     SKIP_BUILD=true ;;
  esac
done

# ── Required variables ─────────────────────────────────────────────────────
: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID in .env or environment}"
: "${GCP_REGION:=us-central1}"
: "${AGENT_API_KEY:?Set AGENT_API_KEY in .env or environment}"

# Dynatrace optional — if missing, agent auto-falls back to demo mode
DT_TENANT_URL="${DT_TENANT_URL:-}"
DT_API_TOKEN="${DT_API_TOKEN:-}"
DT_MCP_SERVER_URL="${DT_MCP_SERVER_URL:-}"
DT_WEBHOOK_SECRET="${DT_WEBHOOK_SECRET:-change-me}"

REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/sitemedic"
AGENT_SVC="sitemedic-agent"
FRONTEND_SVC="sitemedic-frontend"
DEMO_APP_SVC="sitemedic-demo-app"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  SiteMedic Deploy                                        ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Project     : $GCP_PROJECT_ID"
echo "║  Region      : $GCP_REGION"
echo "║  Demo Public : $DEMO_PUBLIC"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Enable required GCP APIs ────────────────────────────────────────────
echo "[1/7] Enabling GCP APIs…"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  containerregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --project="$GCP_PROJECT_ID" --quiet

# ── 2. Store secrets in Secret Manager ────────────────────────────────────
echo "[2/7] Storing secrets…"
_upsert_secret() {
  local name=$1 value=$2
  if [ -z "$value" ]; then
    echo "  Skipping empty secret: $name"
    return
  fi
  if gcloud secrets describe "$name" --project="$GCP_PROJECT_ID" &>/dev/null; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- \
      --project="$GCP_PROJECT_ID" --quiet
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- \
      --project="$GCP_PROJECT_ID" --replication-policy=automatic --quiet
  fi
  echo "  OK: $name"
}
_upsert_secret "sitemedic-agent-api-key" "$AGENT_API_KEY"
[ -n "$DT_API_TOKEN" ] && _upsert_secret "sitemedic-dt-api-token" "$DT_API_TOKEN"
[ -n "$DT_WEBHOOK_SECRET" ] && _upsert_secret "sitemedic-dt-webhook-secret" "$DT_WEBHOOK_SECRET"

# ── 3. Build & deploy demo-app ─────────────────────────────────────────────
echo "[3/7] Building demo-app…"
if [ "$SKIP_BUILD" = "false" ]; then
  gcloud builds submit "$ROOT/demo-app" \
    --tag="${REGISTRY}/sitemedic-demo-app:latest" \
    --project="$GCP_PROJECT_ID" --quiet
fi

echo "  Deploying demo-app (v1-stable)…"
gcloud run deploy "$DEMO_APP_SVC" \
  --image="${REGISTRY}/sitemedic-demo-app:latest" \
  --region="$GCP_REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --port=3000 \
  --tag=v1-stable \
  --min-instances=0 \
  --max-instances=3 \
  --set-env-vars="OTEL_SERVICE_NAME=sitemedic-demo-app" \
  --project="$GCP_PROJECT_ID" --quiet

echo "  Deploying demo-app (v2-buggy, no traffic)…"
gcloud run deploy "$DEMO_APP_SVC" \
  --image="${REGISTRY}/sitemedic-demo-app:latest" \
  --region="$GCP_REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --port=3000 \
  --tag=v2-buggy \
  --no-traffic \
  --set-env-vars="OTEL_SERVICE_NAME=sitemedic-demo-app,INJECT_LATENCY_MS=3000,INJECT_ERROR_RATE=0.4" \
  --project="$GCP_PROJECT_ID" --quiet

DEMO_APP_URL=$(gcloud run services describe "$DEMO_APP_SVC" \
  --region="$GCP_REGION" --project="$GCP_PROJECT_ID" --format="value(status.url)")

STABLE_REVISION=$(gcloud run revisions list \
  --service="$DEMO_APP_SVC" --region="$GCP_REGION" --project="$GCP_PROJECT_ID" \
  --filter="metadata.labels.'serving.knative.dev/configurationGeneration'=1" \
  --sort-by="~metadata.creationTimestamp" \
  --format="value(metadata.name)" | head -1)
STABLE_REVISION="${STABLE_REVISION:-stable}"

BUGGY_REVISION=$(gcloud run revisions list \
  --service="$DEMO_APP_SVC" --region="$GCP_REGION" --project="$GCP_PROJECT_ID" \
  --sort-by="~metadata.creationTimestamp" \
  --format="value(metadata.name)" | head -1)
BUGGY_REVISION="${BUGGY_REVISION:-buggy}"

echo "  Demo app: $DEMO_APP_URL (stable=$STABLE_REVISION, buggy=$BUGGY_REVISION)"

# ── 4. Build & deploy agent ────────────────────────────────────────────────
echo "[4/7] Building agent…"
if [ "$SKIP_BUILD" = "false" ]; then
  gcloud builds submit "$ROOT" \
    --config="$ROOT/agent/cloudbuild.yaml" \
    --substitutions="_REGISTRY=${REGISTRY}" \
    --project="$GCP_PROJECT_ID" --quiet
fi

# Determine demo mode flags
FORCE_DEMO="false"
if [ -z "$DT_API_TOKEN" ] || [ -z "$DT_TENANT_URL" ]; then
  echo "  No Dynatrace credentials — enabling SITEMEDIC_FORCE_DEMO=true"
  FORCE_DEMO="true"
fi
[ "${SITEMEDIC_FORCE_DEMO:-false}" = "true" ] && FORCE_DEMO="true"

# Build env vars string for agent
AGENT_ENV_VARS="ENV=prod"
AGENT_ENV_VARS+=",GCP_PROJECT_ID=${GCP_PROJECT_ID}"
AGENT_ENV_VARS+=",GCP_REGION=${GCP_REGION}"
AGENT_ENV_VARS+=",VERTEX_AI_LOCATION=${GCP_REGION}"
AGENT_ENV_VARS+=",DEMO_APP_URL=${DEMO_APP_URL}"
AGENT_ENV_VARS+=",DEMO_APP_SERVICE_NAME=${DEMO_APP_SVC}"
AGENT_ENV_VARS+=",DEMO_APP_STABLE_REVISION=${STABLE_REVISION}"
AGENT_ENV_VARS+=",DEMO_APP_BUGGY_REVISION=${BUGGY_REVISION}"
AGENT_ENV_VARS+=",SITEMEDIC_FORCE_DEMO=${FORCE_DEMO}"
AGENT_ENV_VARS+=",DEMO_PUBLIC=${DEMO_PUBLIC}"

# Resolve FRONTEND_URL: use existing deployed URL if skipping frontend, else use derived name
if [ "$SKIP_FRONTEND" = "true" ]; then
  _EXISTING_FRONTEND=$(gcloud run services describe "${FRONTEND_SVC}" \
    --region="$GCP_REGION" --project="$GCP_PROJECT_ID" --format="value(status.url)" 2>/dev/null || true)
  FRONTEND_URL="${_EXISTING_FRONTEND:-https://${FRONTEND_SVC}-${GCP_PROJECT_ID}.${GCP_REGION}.run.app}"
else
  FRONTEND_URL="https://${FRONTEND_SVC}-${GCP_PROJECT_ID}.${GCP_REGION}.run.app"
fi
AGENT_ENV_VARS+=",FRONTEND_URL=${FRONTEND_URL}"

[ -n "$DT_TENANT_URL" ]    && AGENT_ENV_VARS+=",DT_TENANT_URL=${DT_TENANT_URL}"
[ -n "$DT_MCP_SERVER_URL" ] && AGENT_ENV_VARS+=",DT_MCP_SERVER_URL=${DT_MCP_SERVER_URL}"

# Secrets to mount (only those that exist)
AGENT_SECRETS="AGENT_API_KEY=sitemedic-agent-api-key:latest"
if gcloud secrets describe "sitemedic-dt-api-token" --project="$GCP_PROJECT_ID" &>/dev/null; then
  AGENT_SECRETS+=",DT_API_TOKEN=sitemedic-dt-api-token:latest"
fi
if gcloud secrets describe "sitemedic-dt-webhook-secret" --project="$GCP_PROJECT_ID" &>/dev/null; then
  AGENT_SECRETS+=",DT_WEBHOOK_SECRET=sitemedic-dt-webhook-secret:latest"
fi

echo "  Deploying agent (FORCE_DEMO=${FORCE_DEMO}, DEMO_PUBLIC=${DEMO_PUBLIC})…"
gcloud run deploy "$AGENT_SVC" \
  --image="${REGISTRY}/sitemedic-agent:latest" \
  --region="$GCP_REGION" \
  --platform=managed \
  --no-allow-unauthenticated \
  --port=8080 \
  --min-instances=0 \
  --max-instances=5 \
  --memory=1Gi \
  --cpu=1 \
  --set-env-vars="$AGENT_ENV_VARS" \
  --set-secrets="$AGENT_SECRETS" \
  --service-account="sitemedic-agent@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --project="$GCP_PROJECT_ID" --quiet

# Allow unauthenticated access to agent (frontend calls it; API key guards sensitive endpoints)
gcloud run services add-iam-policy-binding "$AGENT_SVC" \
  --region="$GCP_REGION" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --project="$GCP_PROJECT_ID" --quiet 2>/dev/null || true

AGENT_URL=$(gcloud run services describe "$AGENT_SVC" \
  --region="$GCP_REGION" --project="$GCP_PROJECT_ID" --format="value(status.url)")

# Update FRONTEND_URL env var on agent now that we know it
# (set temporarily; will be finalized after frontend deploy)
gcloud run services update "$AGENT_SVC" \
  --region="$GCP_REGION" \
  --update-env-vars="FRONTEND_URL=https://${FRONTEND_SVC}-${GCP_PROJECT_ID}.${GCP_REGION}.run.app" \
  --project="$GCP_PROJECT_ID" --quiet 2>/dev/null || true

echo "  Agent: $AGENT_URL"

# ── 5. Ensure Firestore database exists ────────────────────────────────────
echo "[5/7] Ensuring Firestore database…"
gcloud firestore databases describe --project="$GCP_PROJECT_ID" &>/dev/null || \
  gcloud firestore databases create --region="$GCP_REGION" --project="$GCP_PROJECT_ID" --quiet
echo "  OK"

# ── 6. Build & deploy frontend ─────────────────────────────────────────────
if [ "$SKIP_FRONTEND" = "false" ]; then
  echo "[6/7] Building frontend…"

  # Fetch Firebase config from env or Secret Manager
  FIREBASE_API_KEY="${NEXT_PUBLIC_FIREBASE_API_KEY:-}"
  FIREBASE_AUTH_DOMAIN="${NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN:-${GCP_PROJECT_ID}.firebaseapp.com}"
  FIREBASE_PROJECT_ID="${NEXT_PUBLIC_FIREBASE_PROJECT_ID:-${GCP_PROJECT_ID}}"
  FIREBASE_APP_ID="${NEXT_PUBLIC_FIREBASE_APP_ID:-}"

  FRONTEND_BUILD_ARGS=""
  FRONTEND_BUILD_ARGS+=" --build-arg NEXT_PUBLIC_AGENT_URL=${AGENT_URL}"
  FRONTEND_BUILD_ARGS+=" --build-arg NEXT_PUBLIC_FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID}"
  FRONTEND_BUILD_ARGS+=" --build-arg NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN}"
  [ -n "$FIREBASE_API_KEY" ] && \
    FRONTEND_BUILD_ARGS+=" --build-arg NEXT_PUBLIC_FIREBASE_API_KEY=${FIREBASE_API_KEY}"
  [ -n "$FIREBASE_APP_ID" ] && \
    FRONTEND_BUILD_ARGS+=" --build-arg NEXT_PUBLIC_FIREBASE_APP_ID=${FIREBASE_APP_ID}"

  if [ "$SKIP_BUILD" = "false" ]; then
    gcloud builds submit "$ROOT/frontend" \
      --config="$ROOT/frontend/cloudbuild.yaml" \
      --substitutions="_REGION=${GCP_REGION},_AGENT_URL=${AGENT_URL},_FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID},_FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN},_FIREBASE_API_KEY=${FIREBASE_API_KEY:-},_FIREBASE_APP_ID=${FIREBASE_APP_ID:-}" \
      --project="$GCP_PROJECT_ID" --quiet
  fi

  echo "  Deploying frontend…"
  gcloud run deploy "$FRONTEND_SVC" \
    --image="${REGISTRY}/sitemedic-frontend:latest" \
    --region="$GCP_REGION" \
    --platform=managed \
    --allow-unauthenticated \
    --port=3000 \
    --min-instances=0 \
    --max-instances=5 \
    --memory=512Mi \
    --set-env-vars="AGENT_URL=${AGENT_URL},NODE_ENV=production" \
    --set-secrets="AGENT_API_KEY=sitemedic-agent-api-key:latest" \
    --project="$GCP_PROJECT_ID" --quiet

  FRONTEND_URL=$(gcloud run services describe "$FRONTEND_SVC" \
    --region="$GCP_REGION" --project="$GCP_PROJECT_ID" --format="value(status.url)")

  # Update FRONTEND_URL (CORS origin) on agent with the real URL
  gcloud run services update "$AGENT_SVC" \
    --region="$GCP_REGION" \
    --update-env-vars="FRONTEND_URL=${FRONTEND_URL}" \
    --project="$GCP_PROJECT_ID" --quiet

  echo "  Frontend: $FRONTEND_URL"
else
  FRONTEND_URL="(skipped)"
  echo "[6/7] Skipping frontend deploy."
fi

# ── 7. Summary ─────────────────────────────────────────────────────────────
echo ""
echo "[7/7] Deploy complete!"
echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
printf "│  Demo app URL  : %-44s│\n" "$DEMO_APP_URL"
printf "│  Agent URL     : %-44s│\n" "$AGENT_URL"
printf "│  Frontend URL  : %-44s│\n" "$FRONTEND_URL"
printf "│  Demo mode     : FORCE_DEMO=%-34s│\n" "$FORCE_DEMO  DEMO_PUBLIC=$DEMO_PUBLIC"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
if [ "$DEMO_PUBLIC" = "true" ]; then
  echo "  Hosted public demo (share with judges):"
  echo "    $FRONTEND_URL"
  echo ""
  echo "  The agent runs with DEMO_PUBLIC=true — no login required."
  echo "  Approve/Reject actions are attributed to demo-operator."
fi
echo "  Fault injection:"
echo "    curl -X POST $DEMO_APP_URL/inject/latency -d '{\"ms\":3000}' -H 'Content-Type: application/json'"
echo "    curl -X POST $DEMO_APP_URL/inject/errors  -d '{\"rate\":0.5}'  -H 'Content-Type: application/json'"
echo "    curl -X POST $DEMO_APP_URL/reset"
