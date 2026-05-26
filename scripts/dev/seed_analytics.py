#!/usr/bin/env python3
"""
Seed Firestore with 50 synthetic historical incidents spread over the last 30 days.
Used for the analytics acceptance test.

⚠️  DEVELOPMENT ONLY: This script seeds test data and must NOT run in production.

Usage:
  cd /path/to/SiteMedic
  ENV=dev GCP_PROJECT_ID=your-project python scripts/dev/seed_analytics.py

After seeding, trigger a snapshot refresh:
  curl -X POST http://localhost:8080/api/analytics/refresh?window=all \
       -H "X-API-Key: change-me-before-deploy"

Then load http://localhost:3001/analytics.
"""
import asyncio
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ⚠️  FAIL-FAST: This script only runs in development
if os.environ.get("ENV") != "dev":
    print("❌ Error: seed_analytics.py must only run in development environment.")
    print(f"   Current ENV: {os.environ.get('ENV', 'not set')}")
    print("   Set ENV=dev to run this script.")
    sys.exit(1)

# Make agent code importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "agent"))

from google.cloud import firestore

GCP_PROJECT = os.environ.get("GCP_PROJECT_ID") or sys.exit("Set GCP_PROJECT_ID")

SERVICES = ["demo-app", "auth-service", "payment-api", "notification-worker"]
SEVERITIES = ["AVAILABILITY", "PERFORMANCE", "ERROR", "RESOURCE_CONTENTION"]
ACTIONS = [
    "rollback_revision", "scale_service", "restart_service",
    "failover_cloud_sql_replica", "no_action_needed",
]
STATUSES_WEIGHTS = [
    ("RESOLVED",          0.60),
    ("REJECTED",          0.15),
    ("AWAITING_APPROVAL", 0.10),
    ("DIAGNOSING",        0.10),
    ("DETECTING",         0.05),
]
TOOLS = [
    "get_problem_details", "list_problems", "query_metrics",
    "get_traces", "list_entities", "query_cloud_logging",
    "query_cloud_monitoring", "get_service_response_time",
    "get_error_rate", "get_current_traffic_pattern",
]

rng = random.Random(42)


def _weighted_choice(choices):
    items, weights = zip(*choices)
    return rng.choices(items, weights=weights, k=1)[0]


def _rand_ts(days_ago_max: float, days_ago_min: float = 0) -> datetime:
    now = datetime.now(timezone.utc)
    offset = timedelta(
        seconds=rng.uniform(days_ago_min * 86400, days_ago_max * 86400)
    )
    return now - offset


def _make_trace(started: datetime, steps: int) -> list[dict]:
    trace = []
    for i in range(steps):
        ts = started + timedelta(seconds=rng.uniform(10, 90) * (i + 1))
        tool = rng.choice(TOOLS)
        trace.append({
            "step": i,
            "thought": f"Step {i}: investigating {tool} data to understand the root cause.",
            "tool_call": {"name": tool, "args": {"service_name": "demo-app"}},
            "tool_result": {"status": "ok", "value": rng.uniform(0.1, 99.9)},
            "timestamp": ts.isoformat(),
            "provider": "dynatrace" if tool in ("get_problem_details", "list_problems", "query_metrics", "get_traces", "list_entities", "get_service_response_time", "get_error_rate") else "gcp",
        })
    return trace


def _make_incident(idx: int) -> dict:
    pid = f"P-SEED-{idx:04d}-{uuid.uuid4().hex[:6].upper()}"
    service   = rng.choice(SERVICES)
    severity  = rng.choice(SEVERITIES)
    status    = _weighted_choice(STATUSES_WEIGHTS)
    started   = _rand_ts(days_ago_max=29, days_ago_min=0.5)

    # Resolution time: 1–30 minutes after start
    resolution_offset = timedelta(minutes=rng.uniform(1, 30))
    updated = started + resolution_offset

    # Plan (only for incidents that reached planning stage)
    plan = None
    if status in ("RESOLVED", "REJECTED", "AWAITING_APPROVAL", "REMEDIATING"):
        action = rng.choice(ACTIONS)
        plan = {
            "action": action,
            "service": service,
            "reason": f"Detected {severity.lower()} degradation. {action.replace('_', ' ')} is the recommended fix.",
            "confidence": round(rng.uniform(0.65, 0.98), 2),
            "rollback_safe": True,
            "rollback_safety": "reversible",
            "requires_explicit_confirmation": False,
            "estimated_impact": "Minimal user impact during execution.",
            "estimated_hourly_cost_delta_usd": round(rng.uniform(-2.0, 5.0), 4),
            "traffic_context": rng.choice(["peak", "trough", "normal"]),
            "cost_optimized_alternative": None,
        }

    steps = rng.randint(2, 9)
    trace = _make_trace(started, steps)

    postmortem = None
    if status == "RESOLVED":
        postmortem = (
            f"## Postmortem: {service} {severity.title()} incident\n\n"
            f"**Root cause**: Transient {severity.lower()} spike in `{service}`.\n\n"
            f"**Resolution**: Applied `{plan['action'] if plan else 'n/a'}` successfully.\n\n"
            f"**Action items**: Monitor {service} for recurrence over the next 24h."
        )

    providers = list({step["provider"] for step in trace if step.get("provider")})

    return {
        "problem_id": pid,
        "status": status,
        "severity": severity,
        "title": f"{severity.title()} degradation on {service}",
        "service": service,
        "started_at": started.isoformat(),
        "updated_at": updated.isoformat(),
        "trace": trace,
        "plan": plan,
        "postmortem": postmortem,
        "correlation_id": str(uuid.uuid4()),
        "providers_used": providers,
        "linked_prediction_id": None,
        "prediction_validated": False,
        "cluster_id": None,
    }


async def seed():
    db = firestore.AsyncClient(project=GCP_PROJECT)
    incidents_col = db.collection("incidents")

    # Check for existing seeds
    existing = [doc async for doc in incidents_col.where("problem_id", ">=", "P-SEED-").limit(5).stream()]
    if existing:
        print(f"Found {len(existing)}+ existing seed documents — skipping (delete them first to re-seed).")
        return

    print("Seeding 50 synthetic incidents …")
    batch = db.batch()
    count = 0

    for i in range(50):
        inc = _make_incident(i)
        ref = incidents_col.document(inc["problem_id"])
        batch.set(ref, inc)
        count += 1
        if count % 20 == 0:
            await batch.commit()
            print(f"  committed {count}/50")
            batch = db.batch()

    if count % 20 != 0:
        await batch.commit()
        print(f"  committed {count}/50")

    print("Done. Trigger analytics refresh:")
    print("  curl -X POST 'http://localhost:8080/api/analytics/refresh?window=all' -H 'X-API-Key: change-me-before-deploy'")


if __name__ == "__main__":
    asyncio.run(seed())
