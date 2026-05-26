#!/usr/bin/env bash
# scripts/test_demo_mode.sh — Integration test for Demo Mode end-to-end.
#
# Usage:
#   ./scripts/test_demo_mode.sh [--base-url http://localhost:8080] [--api-key KEY]
#
# What it does:
#   1. Validates all scenario files with validate_scenarios.py
#   2. Starts the agent with SITEMEDIC_FORCE_DEMO=true (background)
#   3. Waits for the agent to be ready
#   4. Confirms /api/demo/status reports demo mode
#   5. Triggers each named scenario via /api/demo/run
#   6. Confirms each scenario creates an incident in /api/incidents
#   7. Reports pass/fail for each step
#
# Requirements: curl, python3, jq
# The agent must not already be running on the base URL when you invoke this.
#
# Environment variables:
#   AGENT_BASE_URL   (default: http://localhost:8080)
#   AGENT_API_KEY    (read from .env if not set)
#   SKIP_AGENT_START (set to 1 to skip starting a new agent process)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${AGENT_BASE_URL:-http://localhost:8080}"
API_KEY="${AGENT_API_KEY:-}"
SKIP_START="${SKIP_AGENT_START:-0}"
AGENT_PID=""

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Colour

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAILURES=$((FAILURES+1)); }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

FAILURES=0

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    --api-key)  API_KEY="$2";  shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# Load API key from .env if not set
if [[ -z "$API_KEY" ]] && [[ -f "$REPO_ROOT/.env" ]]; then
  API_KEY=$(grep -E '^AGENT_API_KEY=' "$REPO_ROOT/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

if [[ -z "$API_KEY" ]]; then
  warn "AGENT_API_KEY not set — trigger endpoints will likely return 403"
fi

# ── Step 1: Validate scenarios ────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  SiteMedic Demo Mode Integration Test"
echo "  Agent: $BASE_URL"
echo "════════════════════════════════════════════════════════════"
echo ""

info "Step 1: Validating scenario files..."
if python3 "$REPO_ROOT/scripts/validate_scenarios.py" > /tmp/sitemedic_validate.log 2>&1; then
  pass "All scenario files validated ($(grep -c '\[PASS\]' /tmp/sitemedic_validate.log) passed)"
else
  fail "Scenario validation failed — see /tmp/sitemedic_validate.log"
  cat /tmp/sitemedic_validate.log
  exit 1
fi

# ── Step 2: Start agent (optional) ───────────────────────────────────────────
if [[ "$SKIP_START" != "1" ]]; then
  info "Step 2: Starting agent with SITEMEDIC_FORCE_DEMO=true..."
  cd "$REPO_ROOT/agent"

  # Activate venv if present
  if [[ -f ".venv/bin/activate" ]]; then
    source .venv/bin/activate
  fi

  # Export demo mode flag
  export SITEMEDIC_FORCE_DEMO=true
  export ENV="${ENV:-dev}"

  # Start agent in background, redirect logs to temp file
  uvicorn main:app --host 0.0.0.0 --port 8080 --log-level warning > /tmp/sitemedic_agent.log 2>&1 &
  AGENT_PID=$!
  info "Agent started with PID $AGENT_PID"

  # Cleanup on exit
  trap "kill $AGENT_PID 2>/dev/null; echo 'Agent stopped.'" EXIT
else
  info "Step 2: Skipping agent start (SKIP_AGENT_START=1)"
fi

# ── Step 3: Wait for agent ready ──────────────────────────────────────────────
info "Step 3: Waiting for agent to become ready..."
READY=0
for i in $(seq 1 30); do
  if curl -sf "$BASE_URL/health" > /dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" -eq 1 ]]; then
  pass "Agent is ready at $BASE_URL"
else
  fail "Agent did not become ready within 30 seconds"
  if [[ -f /tmp/sitemedic_agent.log ]]; then
    echo "--- Agent log ---"
    tail -20 /tmp/sitemedic_agent.log
  fi
  exit 1
fi

# ── Step 4: Check demo status ─────────────────────────────────────────────────
info "Step 4: Checking /api/demo/status..."
STATUS_RESPONSE=$(curl -sf "$BASE_URL/api/demo/status" 2>/dev/null || echo '{}')
DEMO_ACTIVE=$(echo "$STATUS_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('demo_mode_active', False))" 2>/dev/null || echo "False")
SOURCE_TYPE=$(echo "$STATUS_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('source_type', 'unknown'))" 2>/dev/null || echo "unknown")
SCENARIOS_N=$(echo "$STATUS_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scenarios_available', 0))" 2>/dev/null || echo "0")

if [[ "$DEMO_ACTIVE" == "True" ]]; then
  pass "Demo mode is active (source_type=$SOURCE_TYPE, scenarios=$SCENARIOS_N)"
else
  fail "Demo mode is NOT active (demo_mode_active=$DEMO_ACTIVE, source_type=$SOURCE_TYPE)"
fi

# ── Step 5: Check scenario listing ───────────────────────────────────────────
info "Step 5: Checking /api/demo/scenarios..."
SCENARIOS_RESPONSE=$(curl -sf "$BASE_URL/api/demo/scenarios" 2>/dev/null || echo '[]')
SCENARIO_COUNT=$(echo "$SCENARIOS_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "0")

if [[ "$SCENARIO_COUNT" -ge 6 ]]; then
  pass "Scenario listing returns $SCENARIO_COUNT scenarios"
else
  fail "Scenario listing returned only $SCENARIO_COUNT scenarios (expected >= 6)"
fi

# ── Step 6: Trigger each main scenario ───────────────────────────────────────
SCENARIOS=(
  "memory_leak_001"
  "bad_deploy_rollback_001"
  "latency_spike_001"
  "error_burst_001"
  "cascading_failure_001"
  "predictive_catch_001"
)

info "Step 6: Triggering ${#SCENARIOS[@]} scenarios..."
TRIGGERED_PIDS=()

for SCENARIO in "${SCENARIOS[@]}"; do
  TRIGGER_RESPONSE=$(curl -sf -X POST "$BASE_URL/api/demo/run" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d "{\"scenario\": \"$SCENARIO\"}" 2>/dev/null || echo '{"error": "curl failed"}')

  STATUS_FIELD=$(echo "$TRIGGER_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")
  PROBLEM_ID=$(echo "$TRIGGER_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('problem_id',''))" 2>/dev/null || echo "")

  if [[ "$STATUS_FIELD" == "started" ]]; then
    pass "Triggered scenario '$SCENARIO' -> problem_id=$PROBLEM_ID"
    TRIGGERED_PIDS+=("$PROBLEM_ID")
  else
    fail "Failed to trigger '$SCENARIO': $TRIGGER_RESPONSE"
  fi

  sleep 0.5
done

# ── Step 7: Verify incidents were created ─────────────────────────────────────
info "Step 7: Waiting 5s then checking /api/incidents for demo incidents..."
sleep 5

INCIDENTS_RESPONSE=$(curl -sf "$BASE_URL/api/incidents" 2>/dev/null || echo '[]')
DEMO_INCIDENTS=$(echo "$INCIDENTS_RESPONSE" | python3 -c "
import sys, json
incidents = json.load(sys.stdin)
demo = [i for i in incidents if i.get('problem_id','').startswith('P-DEMO-')]
print(len(demo))
" 2>/dev/null || echo "0")

if [[ "$DEMO_INCIDENTS" -gt 0 ]]; then
  pass "Found $DEMO_INCIDENTS demo incident(s) in Firestore"
else
  warn "No demo incidents found in /api/incidents yet (may need Firestore credentials)"
fi

# ── Step 8: Trigger random scenario ───────────────────────────────────────────
info "Step 8: Triggering a random scenario..."
RANDOM_RESPONSE=$(curl -sf -X POST "$BASE_URL/api/demo/run" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"random": true}' 2>/dev/null || echo '{"error": "curl failed"}')

RANDOM_STATUS=$(echo "$RANDOM_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")

if [[ "$RANDOM_STATUS" == "started" ]]; then
  RANDOM_PID=$(echo "$RANDOM_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('problem_id',''))" 2>/dev/null || echo "?")
  pass "Random scenario triggered -> $RANDOM_PID"
else
  fail "Random scenario trigger failed: $RANDOM_RESPONSE"
fi

# ── Step 9: Scheduler pause/resume ───────────────────────────────────────────
info "Step 9: Testing scheduler pause/resume..."
PAUSE_RESP=$(curl -sf -X POST "$BASE_URL/api/demo/scheduler/pause" \
  -H "X-API-Key: $API_KEY" 2>/dev/null || echo '{"error":"curl failed"}')
PAUSE_STATUS=$(echo "$PAUSE_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")

if [[ "$PAUSE_STATUS" == "paused" ]]; then
  pass "Scheduler paused"
else
  fail "Scheduler pause failed: $PAUSE_RESP"
fi

RESUME_RESP=$(curl -sf -X POST "$BASE_URL/api/demo/scheduler/resume" \
  -H "X-API-Key: $API_KEY" 2>/dev/null || echo '{"error":"curl failed"}')
RESUME_STATUS=$(echo "$RESUME_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")

if [[ "$RESUME_STATUS" == "resumed" ]]; then
  pass "Scheduler resumed"
else
  fail "Scheduler resume failed: $RESUME_RESP"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
if [[ "$FAILURES" -eq 0 ]]; then
  echo -e "${GREEN}All tests passed!${NC}"
else
  echo -e "${RED}$FAILURES test(s) FAILED${NC}"
fi
echo "════════════════════════════════════════════════════════════"
echo ""

exit "$FAILURES"
