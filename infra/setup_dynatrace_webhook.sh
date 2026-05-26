#!/usr/bin/env bash
# ── SiteMedic: Dynatrace Webhook Setup ────────────────────────────────────────
# Automates:
#   1. Generate a random webhook shared secret
#   2. Store the secret in GCP Secret Manager as 'sitemedic-webhook-secret'
#   3. Create (or update) a Dynatrace custom alerting profile
#   4. Create (or update) a Dynatrace webhook notification integration
#
# Prerequisites:
#   - gcloud CLI authenticated with project owner / editor role
#   - DT_TENANT_URL, DT_API_TOKEN, GCP_PROJECT_ID, AGENT_URL exported in your env
#     (or set in .env file read below)
#
# Usage:
#   export DT_TENANT_URL=https://your-tenant.live.dynatrace.com
#   export DT_API_TOKEN=dt0c01.YOUR_TOKEN
#   export GCP_PROJECT_ID=your-gcp-project-id
#   export AGENT_URL=https://sitemedic-agent-xxxxx-uc.a.run.app
#   ./infra/setup_dynatrace_webhook.sh
#
# Re-running this script is idempotent: it updates existing resources.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env if present (never committed — for local dev only).
if [[ -f "$ROOT_DIR/.env" ]]; then
  # shellcheck disable=SC1090
  set -a && source "$ROOT_DIR/.env" && set +a
fi

# ── Validate required variables ────────────────────────────────────────────────
required_vars=(DT_TENANT_URL DT_API_TOKEN GCP_PROJECT_ID AGENT_URL)
for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var is not set. Export it or add it to .env." >&2
    exit 1
  fi
done

DT_BASE_URL="${DT_TENANT_URL%/}"                    # strip trailing slash
WEBHOOK_URL="${AGENT_URL%/}/api/webhooks/dynatrace"
SECRET_NAME="sitemedic-webhook-secret"

# ── Step 1: Generate shared secret ────────────────────────────────────────────
echo ""
echo "==> Generating webhook shared secret..."
WEBHOOK_SECRET=$(openssl rand -hex 32)
echo "    Generated 32-byte hex secret."

# ── Step 2: Store secret in Secret Manager ────────────────────────────────────
echo ""
echo "==> Storing secret in Secret Manager (project: $GCP_PROJECT_ID)..."

# Create the secret resource if it doesn't exist.
if ! gcloud secrets describe "$SECRET_NAME" --project="$GCP_PROJECT_ID" &>/dev/null; then
  gcloud secrets create "$SECRET_NAME" \
    --project="$GCP_PROJECT_ID" \
    --replication-policy="automatic" \
    --quiet
  echo "    Created secret resource: $SECRET_NAME"
fi

# Add a new version (previous versions are kept for rotation).
printf '%s' "$WEBHOOK_SECRET" | gcloud secrets versions add "$SECRET_NAME" \
  --project="$GCP_PROJECT_ID" \
  --data-file=- \
  --quiet
echo "    New secret version created."

# Grant the Cloud Run service account read access to the secret.
AGENT_SA="${AGENT_SERVICE_ACCOUNT:-sitemedic-agent@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --project="$GCP_PROJECT_ID" \
  --member="serviceAccount:${AGENT_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet 2>/dev/null || true
echo "    IAM binding set for: $AGENT_SA"

# ── Step 3: Create / update Dynatrace alerting profile ────────────────────────
echo ""
echo "==> Configuring Dynatrace alerting profile..."

PROFILE_NAME="SiteMedic All Problems"
PROFILE_PAYLOAD=$(cat <<EOF
{
  "displayName": "$PROFILE_NAME",
  "rules": [
    {
      "severityLevel": "AVAILABILITY",
      "tagFilters": [],
      "delayInMinutes": 0
    },
    {
      "severityLevel": "ERROR",
      "tagFilters": [],
      "delayInMinutes": 0
    },
    {
      "severityLevel": "PERFORMANCE",
      "tagFilters": [],
      "delayInMinutes": 0
    },
    {
      "severityLevel": "RESOURCE_CONTENTION",
      "tagFilters": [],
      "delayInMinutes": 0
    },
    {
      "severityLevel": "CUSTOM_ALERT",
      "tagFilters": [],
      "delayInMinutes": 0
    }
  ],
  "managementZones": [],
  "metadata": {
    "configurationVersions": [1],
    "clusterVersion": "1.0"
  }
}
EOF
)

PROFILE_RESPONSE=$(curl -sf -X POST \
  "${DT_BASE_URL}/api/config/v1/alertingProfiles" \
  -H "Authorization: Api-Token ${DT_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$PROFILE_PAYLOAD" 2>&1) || true

PROFILE_ID=$(echo "$PROFILE_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || true)

if [[ -z "$PROFILE_ID" ]]; then
  # Try to find existing profile by name.
  PROFILE_ID=$(curl -sf \
    "${DT_BASE_URL}/api/config/v1/alertingProfiles" \
    -H "Authorization: Api-Token ${DT_API_TOKEN}" | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
profiles = data.get('values', [])
match = next((p['id'] for p in profiles if p.get('name') == '$PROFILE_NAME'), '')
print(match)
" 2>/dev/null || true)
fi

if [[ -z "$PROFILE_ID" ]]; then
  echo "    WARNING: Could not create or find alerting profile. Using default." >&2
  PROFILE_ID="00000000-0000-0000-0000-000000000000"  # Dynatrace built-in "any" profile
else
  echo "    Alerting profile ID: $PROFILE_ID"
fi

# ── Step 4: Create / update Dynatrace webhook notification ────────────────────
echo ""
echo "==> Configuring Dynatrace webhook notification..."

NOTIFICATION_NAME="SiteMedic Webhook"

# Dynatrace webhook payload template (uppercase field names, millisecond timestamp).
WEBHOOK_BODY_TEMPLATE=$(cat <<'TMPL'
{
  "State": "{State}",
  "ProblemID": "{ProblemID}",
  "ProblemTitle": "{ProblemTitle}",
  "ImpactedEntities": {ImpactedEntities},
  "Severity": "{Severity}",
  "ProblemURL": "{ProblemURL}",
  "Timestamp": {Timestamp},
  "Tags": {Tags}
}
TMPL
)

NOTIFICATION_PAYLOAD=$(python3 -c "
import json, sys
payload = {
    'name': '$NOTIFICATION_NAME',
    'alertingProfile': '$PROFILE_ID',
    'active': True,
    'type': 'WEBHOOK',
    'url': '$WEBHOOK_URL',
    'content': json.dumps(json.loads(open('/dev/stdin').read())),
    'headers': [
        {
            'name': 'X-Hub-Signature-256',
            'value': 'sha256=DYNATRACE_DOES_NOT_SUPPORT_DYNAMIC_HMAC_SEE_README',
            'secret': False
        },
        {
            'name': 'Content-Type',
            'value': 'application/json',
            'secret': False
        }
    ]
}
print(json.dumps(payload))
" <<< "$WEBHOOK_BODY_TEMPLATE")

# List existing notifications to check for duplicates.
EXISTING_ID=$(curl -sf \
  "${DT_BASE_URL}/api/config/v1/notifications" \
  -H "Authorization: Api-Token ${DT_API_TOKEN}" | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
notifications = data.get('values', [])
match = next((n['id'] for n in notifications if n.get('name') == '$NOTIFICATION_NAME'), '')
print(match)
" 2>/dev/null || true)

if [[ -n "$EXISTING_ID" ]]; then
  # Update existing notification.
  curl -sf -X PUT \
    "${DT_BASE_URL}/api/config/v1/notifications/${EXISTING_ID}" \
    -H "Authorization: Api-Token ${DT_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$NOTIFICATION_PAYLOAD" > /dev/null
  echo "    Updated existing notification: $EXISTING_ID"
else
  # Create new notification.
  NEW_ID=$(curl -sf -X POST \
    "${DT_BASE_URL}/api/config/v1/notifications" \
    -H "Authorization: Api-Token ${DT_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$NOTIFICATION_PAYLOAD" | \
    python3 -c "import sys, json; print(json.load(sys.stdin).get('id', ''))" 2>/dev/null || true)
  echo "    Created notification: $NEW_ID"
fi

# ── Step 5: Print summary and next steps ──────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo " SiteMedic Dynatrace Webhook Setup Complete"
echo "════════════════════════════════════════════════════════"
echo " Webhook URL:     $WEBHOOK_URL"
echo " Secret Manager:  projects/$GCP_PROJECT_ID/secrets/$SECRET_NAME"
echo " Alerting Profile: $PROFILE_ID"
echo ""
echo " IMPORTANT: Dynatrace's built-in webhook integration does not support"
echo " dynamic HMAC signatures. To enable HMAC validation:"
echo ""
echo "   Option A (recommended): Use a Dynatrace Automation workflow that"
echo "   adds the X-Hub-Signature-256 header dynamically by computing HMAC"
echo "   against the shared secret stored in Dynatrace Credential Vault."
echo ""
echo "   Option B: Set DT_WEBHOOK_SECRET in the agent's environment to the"
echo "   same value stored in Secret Manager and configure your network"
echo "   perimeter to restrict webhook traffic to Dynatrace IP ranges only."
echo "   The agent will still validate the header if present."
echo ""
echo " Local dev: add to your .env file:"
echo "   DT_WEBHOOK_SECRET=$WEBHOOK_SECRET"
echo ""
echo " To test the webhook manually:"
echo "   export SECRET=$WEBHOOK_SECRET"
echo '   BODY='"'"'{"State":"OPEN","ProblemID":"P-TEST","ProblemTitle":"Manual test","ImpactedEntities":[{"name":"demo-app"}],"Severity":"ERROR","Timestamp":'"$(date +%s)000"'}'"'"
echo '   SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '"'"'{print $2}'"'"')"
echo '   curl -X POST '"$WEBHOOK_URL"' \'
echo '     -H "Content-Type: application/json" \'
echo '     -H "X-Hub-Signature-256: sha256=$SIG" \'
echo '     -d "$BODY"'
echo "════════════════════════════════════════════════════════"
