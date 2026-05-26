# SiteMedic — Autonomous SRE Agent

[![Live Demo](https://img.shields.io/badge/Live%20Demo-sitemedic-blue?style=for-the-badge&logo=googlecloud)](https://sitemedic-frontend-427842119053.us-central1.run.app)

SiteMedic is an autonomous Site Reliability Engineering agent that detects production incidents, diagnoses root causes using Gemini 2.5 Pro's ReAct reasoning loop, proposes a remediation plan for human approval, executes the fix on Google Cloud Run, and generates a postmortem — all in real time.

Built for the Google Cloud + Gemini hackathon.

---

## Architecture Overview

```
Dynatrace MCP  ──►  Gemini 2.5 Pro (ReAct loop)  ──►  Human Approval  ──►  Cloud Run Remediation
     │                       │                                                        │
     │               Firestore (incident state)  ◄──────────────────────────────────┘
     │                       │
     └──────────────►  Next.js Frontend (real-time streaming via Firebase)
```

**Key components:**

| Component | Tech | Purpose |
|-----------|------|---------|
| `agent/` | Python + FastAPI | Core SRE logic — detection, diagnosis, remediation, postmortem |
| `frontend/` | Next.js 14 + Tailwind | Real-time incident dashboard (Firestore listener) |
| `demo-app/` | Node.js | Synthetic target service for chaos injection |
| `config/` | YAML | Per-environment configuration (dev / staging / prod) |
| `infra/` | Shell + Terraform | GCP infrastructure and Cloud Run deploy scripts |
| `scripts/` | Python | Chaos testing, load testing, analytics seeding |

### Agent flow

1. **Detection** — polls `dynatrace_mcp.list_problems` every 30 s; creates a Firestore incident on a new problem
2. **Diagnosis** — Gemini 2.5 Pro ReAct loop (max 10 steps); each thought + tool call is streamed to the frontend
3. **Planning** — Gemini outputs a `RemediationPlan` as structured JSON and sets status to `AWAITING_APPROVAL`
4. **Approval** — operator clicks Approve/Reject in the UI (or calls the API)
5. **Remediation** — executes `gcloud run` commands (rollback, scale, restart)
6. **Postmortem** — Gemini reads the full trace and writes a markdown postmortem

**Demo Mode** — when Dynatrace is unavailable, the agent auto-falls back to replaying 11 curated synthetic scenarios. Gemini still reasons live over the replayed telemetry, so you see real AI behaviour without a Dynatrace trial.

---

## Prerequisites

You will need accounts and credentials for:

| Requirement | Notes |
|-------------|-------|
| **Google Cloud project** | Billing enabled |
| **Dynatrace tenant** | Free trial at [dynatrace.com/trial](https://www.dynatrace.com/trial/) — needed for production mode; not required for demo mode |
| **Firebase project** | Same GCP project — used for Firestore + real-time frontend listener |
| **Docker** | For local development via `docker-compose` |
| **Node.js >= 18** | For the frontend |
| **Python 3.11** | For the agent |
| **gcloud CLI** | `brew install google-cloud-sdk` or [install guide](https://cloud.google.com/sdk/docs/install) |

---

## Step 1 — Clone the repository

```bash
git clone https://github.com/subhojeetchatterjee/sitemedic.git
cd sitemedic
```

---

## Step 2 — Create a GCP project

```bash
# Replace with your own project ID (must be globally unique)
export PROJECT_ID=my-sitemedic-project
export REGION=us-central1

gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID

# Enable billing in the GCP Console:
# https://console.cloud.google.com/billing
```

---

## Step 3 — Enable required GCP APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  logging.googleapis.com \
  cloudresourcemanager.googleapis.com
```

---

## Step 4 — Create a Firestore database

```bash
gcloud firestore databases create --location=$REGION
```

Then in the **Firebase Console** ([console.firebase.google.com](https://console.firebase.google.com)):
1. Add your GCP project to Firebase
2. Go to **Project Settings → General → Your apps** and add a **Web app**
3. Copy the Firebase config values (you will need them in Step 6)

---

## Step 5 — Create Firestore composite indexes

In the **Firebase Console → Firestore → Indexes → Composite**, create:

| Collection | Fields |
|------------|--------|
| `predictions` | `expires_at ASC`, `prediction_false_positive ASC` |
| `predictions` | `service ASC`, `expires_at DESC`, `prediction_validated ASC`, `prediction_false_positive ASC` |
| `predictions` | `expires_at ASC`, `prediction_validated ASC`, `prediction_false_positive ASC` |
| `incident_clusters` | `status ASC`, `created_at DESC` |
| `audit_events` | `incident_id ASC`, `seq DESC` |
| `audit_events` | `actor ASC`, `seq DESC` |
| `audit_events` | `action_type ASC`, `seq DESC` |

Also enable a **TTL policy** on `audit_events`:
Firebase Console → Firestore → Data → `audit_events` → TTL policy → field: `expires_at`

---

## Step 6 — Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in every value:

```bash
# GCP
GCP_PROJECT_ID=my-sitemedic-project
GCP_REGION=us-central1
VERTEX_AI_LOCATION=us-central1

# Dynatrace (leave blank to use demo mode automatically)
DT_TENANT_URL=https://abc12345.live.dynatrace.com
DT_API_TOKEN=dt0c01.XXXX...
DT_MCP_SERVER_URL=https://abc12345.apps.dynatrace.com/platform-reserved/mcp-gateway/v0.1/servers/dynatrace-mcp/mcp
DT_WEBHOOK_SECRET=some-random-secret

# Firestore
FIRESTORE_DATABASE=(default)

# Agent
AGENT_API_KEY=choose-a-strong-random-secret
AGENT_PORT=8080

# Cloud Run target service
DEMO_APP_URL=http://localhost:3000
DEMO_APP_STABLE_REVISION=v1-stable
DEMO_APP_BUGGY_REVISION=v2-buggy

# Firebase (from Step 4 — Firebase Console → Project Settings → Your apps)
NEXT_PUBLIC_FIREBASE_API_KEY=AIzaSy...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=my-sitemedic-project.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=my-sitemedic-project
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=my-sitemedic-project.firebasestorage.app
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=1234567890
NEXT_PUBLIC_FIREBASE_APP_ID=1:1234567890:web:abc...

# Frontend
NEXT_PUBLIC_AGENT_URL=http://localhost:8080
AGENT_URL=http://localhost:8080
FRONTEND_URL=http://localhost:3001

# Environment tier
ENV=dev
```

**Demo mode flags** (optional — no Dynatrace needed):

```bash
SITEMEDIC_FORCE_DEMO=true   # always use demo mode, ignore Dynatrace
DEMO_PUBLIC=true             # disable API key auth (for public demos)
```

---

## Step 7 — Run locally with Docker Compose

```bash
docker-compose up --build
```

Services start on:
- **Demo app** → http://localhost:3000
- **Agent API** → http://localhost:8080

Then start the frontend:

```bash
cd frontend
npm install
npm run dev   # http://localhost:3001
```

Verify the agent is up:
```bash
curl http://localhost:8080/health
# {"status":"ok"}

curl http://localhost:8080/api/demo/status
# {"mode":"demo","scenarios_available":11,...}
```

---

## Step 8 — Run services individually (without Docker)

### Agent

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

### Demo App

```bash
cd demo-app
npm install
node server.js   # PORT defaults to 3000
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # http://localhost:3001
```

---

## Step 9 — Deploy to GCP Cloud Run

### 9a. Create Artifact Registry

```bash
gcloud artifacts repositories create sitemedic \
  --repository-format=docker \
  --location=$REGION

gcloud auth configure-docker "$REGION-docker.pkg.dev"
```

### 9b. Create a service account for the agent

```bash
gcloud iam service-accounts create sitemedic-agent \
  --display-name="SiteMedic Agent"

for ROLE in roles/datastore.user roles/secretmanager.secretAccessor \
            roles/run.developer roles/aiplatform.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:sitemedic-agent@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role=$ROLE
done
```

### 9c. Store secrets in Secret Manager

```bash
echo -n "$(openssl rand -hex 32)" | \
  gcloud secrets create sitemedic-agent-api-key --data-file=-

echo -n "$DT_API_TOKEN" | \
  gcloud secrets create sitemedic-dt-api-token --data-file=-

echo -n "$DT_WEBHOOK_SECRET" | \
  gcloud secrets create sitemedic-dt-webhook-secret --data-file=-
```

### 9d. Build and push images

```bash
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/sitemedic"

# Agent — MUST build from repo root so config/ directory is included
docker build -f agent/Dockerfile -t "$REGISTRY/agent:latest" .
docker push "$REGISTRY/agent:latest"

# Demo app
docker build -f demo-app/Dockerfile -t "$REGISTRY/demo-app:latest" demo-app/
docker push "$REGISTRY/demo-app:latest"

# Frontend — bake Firebase config at build time
docker build -f frontend/Dockerfile \
  --build-arg NEXT_PUBLIC_AGENT_URL="https://YOUR_AGENT_URL" \
  --build-arg NEXT_PUBLIC_FIREBASE_PROJECT_ID=$PROJECT_ID \
  --build-arg NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN="$PROJECT_ID.firebaseapp.com" \
  --build-arg NEXT_PUBLIC_FIREBASE_API_KEY="$NEXT_PUBLIC_FIREBASE_API_KEY" \
  --build-arg NEXT_PUBLIC_FIREBASE_APP_ID="$NEXT_PUBLIC_FIREBASE_APP_ID" \
  -t "$REGISTRY/frontend:latest" frontend/
docker push "$REGISTRY/frontend:latest"
```

### 9e. Deploy services to Cloud Run

```bash
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/sitemedic"

# 1. Demo app
gcloud run deploy sitemedic-demo-app \
  --image="$REGISTRY/demo-app:latest" \
  --region=$REGION --allow-unauthenticated --port=3000 --memory=512Mi

DEMO_APP_URL=$(gcloud run services describe sitemedic-demo-app \
  --region=$REGION --format="value(status.url)")

# 2. Agent
gcloud run deploy sitemedic-agent \
  --image="$REGISTRY/agent:latest" \
  --region=$REGION --no-allow-unauthenticated --port=8080 \
  --memory=2Gi --cpu=2 --min-instances=1 \
  --set-env-vars="ENV=prod,GCP_PROJECT_ID=$PROJECT_ID,GCP_REGION=$REGION,VERTEX_AI_LOCATION=$REGION,DEMO_APP_URL=$DEMO_APP_URL,SITEMEDIC_FORCE_DEMO=true,DEMO_PUBLIC=true" \
  --set-secrets="AGENT_API_KEY=sitemedic-agent-api-key:latest,DT_API_TOKEN=sitemedic-dt-api-token:latest" \
  --service-account="sitemedic-agent@$PROJECT_ID.iam.gserviceaccount.com"

# Allow unauthenticated calls (individual endpoints guard themselves)
gcloud run services add-iam-policy-binding sitemedic-agent \
  --region=$REGION --member="allUsers" --role="roles/run.invoker"

AGENT_URL=$(gcloud run services describe sitemedic-agent \
  --region=$REGION --format="value(status.url)")

# 3. Frontend
gcloud run deploy sitemedic-frontend \
  --image="$REGISTRY/frontend:latest" \
  --region=$REGION --allow-unauthenticated --port=3000 --memory=512Mi \
  --set-env-vars="AGENT_URL=$AGENT_URL,NODE_ENV=production"

FRONTEND_URL=$(gcloud run services describe sitemedic-frontend \
  --region=$REGION --format="value(status.url)")

# 4. Wire CORS — update agent with the real frontend URL
gcloud run services update sitemedic-agent \
  --region=$REGION --update-env-vars="FRONTEND_URL=$FRONTEND_URL"
```

---

## Testing

### Chaos injection

```bash
# Inject 500 ms latency for 60 s
python scripts/chaos_test.py \
  --environment=dev \
  --demo-app-url=http://localhost:3000 \
  --scenario=latency \
  --duration=60

# All available scenarios: latency | errors | memory | pubsub
```

### Load test

```bash
# Interactive web UI
locust -f scripts/load_test.py --host=http://localhost:8080

# Headless (100 users, 5 minutes)
locust -f scripts/load_test.py --host=http://localhost:8080 \
  --users=100 --spawn-rate=10 --run-time=5m --headless
```

### Validate demo scenarios

```bash
python scripts/validate_scenarios.py --strict
# Expected: 11/11 passed
```

### Dry-run a remediation (read-only, no changes)

```bash
cd agent && python dry_run.py <incident_id>
```

### Seed analytics with synthetic data

```bash
python scripts/seed_analytics.py   # inserts 50 synthetic incidents
```

---

## API Reference

Base URL: `http://localhost:8080` (or your Cloud Run agent URL)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/health` | None | Liveness check |
| `GET` | `/api/info` | None | Env, demo mode flags |
| `GET` | `/api/incidents` | None | List all incidents |
| `GET` | `/api/incidents/{id}` | None | Incident detail + trace |
| `POST` | `/api/incidents/{id}/approve` | API Key | Approve remediation plan |
| `POST` | `/api/incidents/{id}/reject` | API Key | Reject plan |
| `GET` | `/api/demo/status` | None | Demo mode status |
| `GET` | `/api/demo/scenarios` | None | Available scenarios |
| `POST` | `/api/demo/run` | API Key | Trigger a specific scenario |
| `POST` | `/api/demo/scheduler/pause` | API Key | Pause auto-scheduler |
| `POST` | `/api/demo/scheduler/resume` | API Key | Resume auto-scheduler |
| `POST` | `/api/demo/speed` | API Key | Set replay speed (1x / 2x / 5x) |
| `GET` | `/api/analytics` | None | Aggregated analytics snapshot |
| `GET` | `/api/audit` | None | Audit trail |

**API Key header:** `X-API-Key: <your-AGENT_API_KEY>`

When `DEMO_PUBLIC=true`, all API Key requirements are bypassed.

---

## Environment Variable Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `ENV` | Yes | `dev`, `staging`, or `prod` |
| `GCP_PROJECT_ID` | Yes | GCP project ID |
| `GCP_REGION` | Yes | Cloud Run + Artifact Registry region |
| `VERTEX_AI_LOCATION` | Yes | Vertex AI (Gemini) region |
| `DT_TENANT_URL` | No* | Dynatrace environment URL |
| `DT_API_TOKEN` | No* | Dynatrace API token |
| `DT_MCP_SERVER_URL` | No* | Dynatrace MCP server URL |
| `DT_WEBHOOK_SECRET` | No | Webhook HMAC secret |
| `AGENT_API_KEY` | Yes | Protects approve/reject endpoints |
| `DEMO_APP_URL` | Yes | URL of the demo target service |
| `DEMO_APP_STABLE_REVISION` | No | Cloud Run stable revision tag |
| `DEMO_APP_BUGGY_REVISION` | No | Cloud Run buggy revision tag |
| `SITEMEDIC_FORCE_DEMO` | No | `true` = always use demo mode |
| `DEMO_PUBLIC` | No | `true` = disable API key (for public demos) |
| `FRONTEND_URL` | No | Frontend URL for CORS |
| `NEXT_PUBLIC_FIREBASE_*` | Yes | Firebase config (7 variables) |

*If Dynatrace vars are absent, the agent auto-falls back to demo mode.

---

## Project Structure

```
sitemedic/
├── agent/
│   ├── main.py                   # FastAPI app + all routes
│   ├── orchestrator.py           # Detection loop + Gemini ReAct + postmortem
│   ├── correlator.py             # Multi-incident clustering
│   ├── predictor.py              # Predictive forecasting (runs every 5 min)
│   ├── analytics.py              # Analytics aggregation
│   ├── audit.py                  # Immutable audit trail (SHA-256 hash chain)
│   ├── environment.py            # Config loader (reads config/environments/*.yaml)
│   ├── schemas.py                # Pydantic models
│   ├── sources/                  # TelemetrySource abstraction
│   │   ├── base.py               # Abstract base class
│   │   └── dynatrace.py          # Live Dynatrace MCP source
│   ├── source_factory.py         # Auto-fallback: Dynatrace → demo
│   ├── demo_mode/
│   │   ├── replay_source.py      # Demo replay engine
│   │   └── scenarios/            # 11 curated JSON scenario files
│   ├── tools/
│   │   ├── dynatrace_mcp.py      # MCP client
│   │   └── gcp_actions.py        # Cloud Run actions via gcloud
│   └── prompts/
│       ├── diagnose.txt          # ReAct system prompt
│       └── predict.txt           # Prediction system prompt
├── frontend/
│   ├── app/                      # Next.js App Router pages
│   │   ├── page.tsx              # Incident feed (Active | Forecasted | Resolved)
│   │   ├── incidents/[id]/       # Incident detail with streaming trace
│   │   ├── clusters/[id]/        # Cluster detail
│   │   ├── analytics/            # Recharts analytics dashboard
│   │   └── demo/                 # Demo control panel
│   ├── components/               # Shared UI components
│   └── lib/firebase.ts           # Firebase JS SDK
├── demo-app/
│   └── server.js                 # Express app with chaos endpoints
├── config/environments/
│   ├── dev.yaml
│   ├── staging.yaml
│   └── prod.yaml
├── infra/
│   ├── deploy.sh                 # One-shot deploy script
│   └── terraform/
├── scripts/
│   ├── chaos_test.py
│   ├── load_test.py
│   ├── validate_scenarios.py
│   └── seed_analytics.py
├── tests/
│   └── test_demo_mode.py         # 171 unit tests
├── docker-compose.yml
├── cloudbuild.yaml               # Cloud Build CI/CD
├── .env.example                  # Copy to .env and fill in
└── firestore.rules
```

---

## Hard Constraints

- **Gemini only** — no OpenAI, Anthropic, or other LLMs anywhere
- **Dynatrace MCP** must be used for telemetry (`list_problems`, `get_problem_details`, `query_metrics`, `get_traces`, `list_entities`)
- **Apache 2.0 LICENSE** must remain at repo root

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
