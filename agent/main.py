from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root (two levels up from agent/) — no-op in Cloud Run
# where env vars are injected directly.
load_dotenv(Path(__file__).parent.parent / ".env")
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from audit import (
    AuditEvent,
    ActionType,
    _agent_id,
    log_audit_event,
    start_audit_worker,
    verify_audit_chain,
)
from correlator import execute_cluster
from environment import Environment
from logging_setup import setup_logging
from orchestrator import detection_loop, generate_postmortem, create_incident_from_webhook
from predictor import predictive_loop
from schemas import ApprovalDecision, ClusterApprovalDecision, DESTRUCTIVE_ACTIONS, DetectRequest, DryRunReport, RemediationAction
from secrets import Secrets
from tools import firestore_client, gcp_actions
from webhook_handler import validate_signature, parse_dynatrace_payload
from webhook_health import webhook_health_loop
from demo_mode.recorder import init_recording

# Fail-fast if ENV not set
if not os.environ.get("ENV"):
    raise RuntimeError("ENV environment variable must be set to 'dev', 'staging', or 'prod' before starting")

# Initialize environment configuration (this will validate and load the config file)
env = Environment.get_instance()

# Configure structured logging with Cloud Logging integration
logging_level = env.get("logging.level", "INFO")
use_cloud_logging = not env.is_dev()  # Use Cloud Logging in staging/prod
setup_logging(
    env_name=env.name(),
    structured=env.get("logging.structured", True),
    use_cloud_logging=use_cloud_logging,
    log_level=logging_level,
)
logger = logging.getLogger(__name__)
logger.info(f"SiteMedic Agent starting in {env.name()} environment")

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

# DEMO_PUBLIC=true disables API key checks so judges can use the hosted demo without credentials.
# This ONLY applies to the public demo deployment — live mode always enforces auth.
_DEMO_PUBLIC: bool = os.environ.get("DEMO_PUBLIC", "").lower() == "true"
_DEMO_OPERATOR_IDENTITY = "demo-operator@sitemedic-demo"


def _require_api_key(key: Optional[str] = Security(API_KEY_HEADER)):
    """Validate API key. Bypassed entirely when DEMO_PUBLIC=true."""
    if _DEMO_PUBLIC:
        return _DEMO_OPERATOR_IDENTITY
    if not key:
        raise HTTPException(status_code=403, detail="X-API-Key header required")
    try:
        agent_api_key = Secrets.get("agent-api-key")
    except RuntimeError:
        agent_api_key = os.environ.get("AGENT_API_KEY")
        if not agent_api_key:
            raise HTTPException(
                status_code=500,
                detail="Agent API key not configured. Set AGENT_API_KEY env var or configure Secret Manager."
            )
    if key.strip() != agent_api_key.strip():
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key


def _actor_identity(key: Optional[str] = None) -> str:
    """Return actor identity string — demo operator when in public mode."""
    if _DEMO_PUBLIC:
        return _DEMO_OPERATOR_IDENTITY
    return "operator@sitemedic"


def _primary_resource_id(plan: dict) -> str | None:
    """Return the resource identifier the operator must type to confirm a destructive action."""
    action = plan.get("action")
    if action == RemediationAction.purge_pubsub_subscription_backlog:
        return plan.get("subscription")
    # Extend here as new destructive actions are added
    return None


def _validate_explicit_confirmation(plan: dict, decision: ApprovalDecision) -> None:
    """Raise 422 if a destructive action is missing or has a wrong confirmation."""
    action = plan.get("action")
    try:
        action_enum = RemediationAction(action)
    except ValueError:
        return  # unknown action; let execute_remediation raise the error

    if action_enum not in DESTRUCTIVE_ACTIONS:
        return

    expected = _primary_resource_id(plan)
    provided = (decision.explicit_confirmation or "").strip()

    if not provided:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Action '{action}' is destructive and requires explicit confirmation. "
                f"Set explicit_confirmation to the resource identifier: '{expected}'"
            ),
        )
    if provided != expected:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Explicit confirmation mismatch. "
                f"Expected '{expected}', got '{provided}'."
            ),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    recorder = init_recording()
    if recorder:
        logger.info("Demo mode recording active — session: %s", recorder.session_id)
    start_audit_worker()

    # Initialise telemetry source (auto-selects demo vs. live)
    from source_factory import get_telemetry_source
    telemetry_source = await get_telemetry_source()
    logger.info(
        "Telemetry source: %s (demo_mode=%s)",
        telemetry_source.get_source_metadata().source_type,
        telemetry_source.get_source_metadata().demo_mode_active,
    )

    # If demo mode, start the replay scheduler
    from demo_mode.replay_source import DemoModeSource as _ReplaySource
    if isinstance(telemetry_source, _ReplaySource):
        telemetry_source.start()
        logger.info("DemoModeSource scheduler started")

    detection_task = asyncio.create_task(detection_loop())
    prediction_task = asyncio.create_task(predictive_loop())
    health_task = asyncio.create_task(webhook_health_loop())
    log_audit_event(AuditEvent(
        actor="system",
        actor_identity=_agent_id(),
        action_type="agent_started",
        payload={
            "version": "1.0.0",
            "source_type": telemetry_source.get_source_metadata().source_type,
            "demo_mode": telemetry_source.get_source_metadata().demo_mode_active,
        },
        result="success",
    ))
    yield
    detection_task.cancel()
    prediction_task.cancel()
    health_task.cancel()

    # Clean up demo scheduler if running
    if isinstance(telemetry_source, _ReplaySource):
        telemetry_source.stop()


app = FastAPI(title="SiteMedic Agent API", version="1.0.0", lifespan=lifespan)

# Configure CORS based on environment
if env.is_dev():
    # Development: allow localhost
    cors_origins = [
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]
else:
    # Staging and production: only allow the frontend Cloud Run URL
    # Must be set via environment variable
    frontend_url = os.environ.get("FRONTEND_URL")
    if not frontend_url:
        raise RuntimeError(
            "FRONTEND_URL environment variable must be set in staging/prod. "
            "Example: https://sitemedic-frontend-prod.run.app"
        )
    # Accept comma-separated list so both URL formats can be allowed
    cors_origins = [u.strip() for u in frontend_url.split(",") if u.strip()]

logger.info(f"CORS origins: {cors_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/maintenance/cleanup", dependencies=[Depends(_require_api_key)])
async def cleanup_stale_data(dry_run: bool = Query(False)):
    """Delete expired predictions, old incidents, and stale clusters. Called by Cloud Scheduler."""
    from firestore_validation import FirestoreRetentionManager

    db = firestore_client.db
    manager = FirestoreRetentionManager(db)

    try:
        results = {}
        results["expired_predictions"] = await manager.cleanup_expired_predictions(dry_run=dry_run)
        results["resolved_incidents"] = await manager.cleanup_resolved_incidents(days=90, dry_run=dry_run)
        results["stale_clusters"] = await manager.cleanup_stale_clusters(days=30, dry_run=dry_run)

        action = "Would delete" if dry_run else "Deleted"
        logger.info(f"{action}: {results}")

        log_audit_event(AuditEvent(
            actor="system",
            actor_identity="cloud-scheduler",
            action_type=ActionType.MAINTENANCE,
            payload={"action": "cleanup_stale_data", "dry_run": dry_run, **results},
            result="success",
        ))

        return {"status": "completed", "dry_run": dry_run, **results}
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        log_audit_event(AuditEvent(
            actor="system",
            actor_identity="cloud-scheduler",
            action_type=ActionType.MAINTENANCE,
            payload={"action": "cleanup_stale_data", "error": str(e)},
            result="failure",
        ))
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {e}")


@app.get("/api/incidents")
async def list_incidents():
    return await firestore_client.list_incidents()


@app.get("/api/incidents/{problem_id}")
async def get_incident(problem_id: str):
    incident = await firestore_client.get_incident(problem_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.post("/api/incidents/{problem_id}/approve", dependencies=[Depends(_require_api_key)])
async def approve_incident(problem_id: str, decision: ApprovalDecision):
    incident = await firestore_client.get_incident(problem_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident["status"] != "AWAITING_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail=f"Incident status is {incident['status']}, not AWAITING_APPROVAL",
        )

    plan = await firestore_client.get_plan(problem_id)
    if not plan:
        raise HTTPException(status_code=422, detail="No plan found for this incident")

    # Dry-run flag: simulate without executing
    if decision.dry_run:
        return await _run_dry_run(problem_id, plan)

    if decision.approved:
        # Guard: destructive actions require the operator to type the resource ID
        _validate_explicit_confirmation(plan, decision)

        log_audit_event(AuditEvent(
            actor="operator",
            actor_identity=_actor_identity(),
            action_type=ActionType.APPROVED,
            incident_id=problem_id,
            payload={"action": plan.get("action"), "service": plan.get("service"), "demo_public": _DEMO_PUBLIC},
            result="success",
        ))

        await firestore_client.set_status(problem_id, "REMEDIATING")
        try:
            result = await gcp_actions.execute_remediation(plan, simulate=problem_id.startswith("P-DEMO-"))
            await firestore_client.append_trace(problem_id, {
                "step": 999,
                "thought": f"Human approved. Executing: {plan['action']}",
                "tool_call": {"name": plan["action"], "args": plan},
                "tool_result": result,
                "timestamp": datetime.datetime.utcnow().isoformat(),
            })
            log_audit_event(AuditEvent(
                actor="agent",
                actor_identity=_agent_id(),
                action_type=ActionType.EXECUTED,
                incident_id=problem_id,
                resource=f"projects/{os.environ.get('GCP_PROJECT_ID', '')}/locations/{os.environ.get('GCP_REGION', '')}/services/{plan.get('service', '')}",
                payload={"action": plan.get("action"), "result_summary": str(result)[:500]},
                result="success",
            ))
            asyncio.create_task(generate_postmortem(problem_id))
        except Exception as e:
            await firestore_client.set_status(problem_id, "AWAITING_APPROVAL")
            log_audit_event(AuditEvent(
                actor="agent",
                actor_identity=_agent_id(),
                action_type=ActionType.EXECUTED,
                incident_id=problem_id,
                payload={"action": plan.get("action"), "error": str(e)},
                result="failure",
            ))
            raise HTTPException(status_code=500, detail=f"Remediation failed: {e}")
    else:
        log_audit_event(AuditEvent(
            actor="operator",
            actor_identity=_actor_identity(),
            action_type=ActionType.REJECTED,
            incident_id=problem_id,
            payload={"action": plan.get("action"), "reason": decision.rejected_reason, "demo_public": _DEMO_PUBLIC},
            result="success",
        ))
        await firestore_client.set_status(problem_id, "REJECTED")
        await firestore_client.append_trace(problem_id, {
            "step": 999,
            "thought": f"Human rejected the plan. Reason: {decision.rejected_reason}",
            "tool_call": None,
            "tool_result": None,
            "timestamp": datetime.datetime.utcnow().isoformat(),
        })

    return {"status": "accepted", "approved": decision.approved}


@app.post("/api/detect", dependencies=[Depends(_require_api_key)])
async def trigger_detection(req: DetectRequest):
    """Manually trigger a detection cycle (for testing / demos)."""
    from orchestrator import detection_loop as _loop
    asyncio.create_task(_loop())
    return {"status": "detection triggered"}


@app.get("/api/predictions")
async def list_predictions():
    """Return active (non-expired) predictions for the Forecasted tab."""
    return await firestore_client.list_active_predictions()


@app.get("/api/predictions/{prediction_id}")
async def get_prediction(prediction_id: str):
    pred = await firestore_client.get_prediction(prediction_id)
    if not pred:
        raise HTTPException(status_code=404, detail="Prediction not found")
    return pred


@app.post("/api/predictions/run", dependencies=[Depends(_require_api_key)])
async def trigger_prediction():
    """Manually trigger one predictive analysis cycle (for testing / demos)."""
    from predictor import predictive_loop as _ploop
    asyncio.create_task(_ploop())
    return {"status": "prediction cycle triggered"}


# ── Cluster endpoints ──────────────────────────────────────────────────────

@app.get("/api/clusters")
async def list_clusters():
    """Return active (non-terminal) incident clusters."""
    return await firestore_client.list_active_clusters()


@app.get("/api/clusters/{cluster_id}")
async def get_cluster(cluster_id: str):
    """Return cluster data enriched with full member incident objects."""
    cluster = await firestore_client.get_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    members = await firestore_client.get_incidents_by_ids(
        cluster.get("member_incident_ids", [])
    )
    return {**cluster, "members": members}


@app.post("/api/clusters/{cluster_id}/approve", dependencies=[Depends(_require_api_key)])
async def approve_cluster(cluster_id: str, decision: ClusterApprovalDecision):
    """Approve or reject a cluster's coordinated remediation plan."""
    cluster = await firestore_client.get_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    if decision.rejected:
        await firestore_client.set_cluster_status(cluster_id, "FAILED")
        return {"status": "rejected", "cluster_id": cluster_id}

    if cluster["status"] != "AWAITING_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail=f"Cluster status is '{cluster['status']}', not AWAITING_APPROVAL",
        )

    asyncio.create_task(execute_cluster(cluster_id, decision.mode))
    return {"status": "accepted", "cluster_id": cluster_id, "mode": decision.mode}


# ── Audit endpoints ────────────────────────────────────────────────────────

@app.get("/api/audit")
async def list_audit_events(
    incident_id: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="ISO datetime lower bound"),
    until: Optional[str] = Query(None, description="ISO datetime upper bound"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Return audit events with optional filters, newest-first."""
    since_dt = datetime.datetime.fromisoformat(since) if since else None
    until_dt = datetime.datetime.fromisoformat(until) if until else None
    events = await firestore_client.list_audit_events(
        limit=limit,
        incident_id=incident_id,
        actor=actor,
        action_type=action_type,
        since=since_dt,
        until=until_dt,
    )
    return events


@app.get("/api/audit/verify")
async def audit_verify(limit: int = Query(1000, ge=1, le=5000)):
    """Recompute the hash chain and report any tampered documents."""
    result = await verify_audit_chain(limit=limit)
    log_audit_event(AuditEvent(
        actor="operator",
        actor_identity="operator@sitemedic",
        action_type=ActionType.CHAIN_VERIFIED,
        payload=result,
        result="success" if result["valid"] else "partial",
    ))
    return result


@app.get("/api/audit/export")
async def audit_export(
    fmt: str = Query("json", pattern="^(json|csv)$"),
    incident_id: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=5000),
):
    """Export audit events as JSON or CSV."""
    events = await firestore_client.list_audit_events(
        limit=limit, incident_id=incident_id
    )
    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        if events:
            fieldnames = ["seq", "timestamp", "actor", "actor_identity", "action_type",
                          "incident_id", "resource", "result", "hash_chain"]
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(events)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_events.csv"},
        )
    return JSONResponse(content=events)


# ── Analytics endpoints ────────────────────────────────────────────────────

_VALID_WINDOWS = {"7d": 7, "30d": 30, "90d": 90}


@app.get("/api/analytics")
async def get_analytics(window: str = Query("30d", pattern="^(7d|30d|90d)$")):
    """Return pre-computed analytics snapshot (cached 1 h). Computes on first call."""
    from analytics import get_or_refresh_snapshot
    window_days = _VALID_WINDOWS[window]
    snapshot = await get_or_refresh_snapshot(window_days)
    return snapshot


@app.post("/api/analytics/refresh", dependencies=[Depends(_require_api_key)])
async def refresh_analytics(window: str = Query("all", pattern="^(7d|30d|90d|all)$")):
    """Force-recompute analytics snapshots. Intended for Cloud Scheduler."""
    from analytics import compute_snapshot
    windows = list(_VALID_WINDOWS.values()) if window == "all" else [_VALID_WINDOWS[window]]
    for w in windows:
        asyncio.create_task(compute_snapshot(w))
    return {"status": "refresh triggered", "windows": [f"{w}d" for w in windows]}


@app.get("/api/calibration")
async def get_calibration():
    """Return the latest confidence calibration snapshot (cached in Firestore)."""
    snapshot = await firestore_client.get_calibration_snapshot()
    if not snapshot:
        from calibration import compute_calibration
        snapshot = await compute_calibration()
    return snapshot


@app.post("/api/calibration/refresh", dependencies=[Depends(_require_api_key)])
async def refresh_calibration():
    """Force-recompute calibration. Intended for weekly Cloud Scheduler job."""
    from calibration import compute_calibration
    asyncio.create_task(compute_calibration())
    return {"status": "calibration refresh triggered"}


# ── Live cost stream (SSE) ─────────────────────────────────────────────────

@app.get("/api/incidents/{problem_id}/cost-stream")
async def cost_stream(problem_id: str):
    """
    Server-Sent Events endpoint — pushes a cost snapshot every 10 seconds.
    Stops automatically once the incident reaches RESOLVED or REJECTED.
    Max 360 pushes (1 hour) to prevent zombie streams.
    """
    incident = await firestore_client.get_incident(problem_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    from incident_cost import compute_cost_snapshot

    async def generate():
        resolved_statuses = {"RESOLVED", "REJECTED"}
        max_pushes = 360
        count = 0
        while count < max_pushes:
            try:
                snapshot = await compute_cost_snapshot(problem_id)
                yield f"data: {json.dumps(snapshot)}\n\n"
                if snapshot.get("status") in resolved_statuses:
                    # One final update after a short wait so UI can settle
                    await asyncio.sleep(10)
                    final = await compute_cost_snapshot(problem_id)
                    yield f"data: {json.dumps(final)}\n\n"
                    return
            except Exception:
                logger.exception(f"Cost stream error for {problem_id}")
                yield f"data: {json.dumps({'error': 'compute failed'})}\n\n"
            count += 1
            await asyncio.sleep(10)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Cost settings CRUD ─────────────────────────────────────────────────────

class CostSettings(BaseModel):
    revenue_per_request_usd: Optional[float] = None
    criticality_multiplier: float = 1.0


@app.get("/api/cost-settings")
async def list_cost_settings():
    return await firestore_client.list_cost_settings()


@app.get("/api/cost-settings/{service}")
async def get_cost_settings(service: str):
    doc = await firestore_client.get_cost_settings(service)
    if not doc:
        raise HTTPException(status_code=404, detail="No settings for this service")
    return doc


@app.put("/api/cost-settings/{service}", dependencies=[Depends(_require_api_key)])
async def upsert_cost_settings(service: str, settings: CostSettings):
    await firestore_client.set_cost_settings(service, settings.model_dump(exclude_none=False))
    return {"status": "saved", "service": service}


# ── Dry-run ────────────────────────────────────────────────────────────────

async def _run_dry_run(problem_id: str, plan: dict) -> dict:
    """Shared logic for both the /dry-run endpoint and approve with dry_run=True."""
    from dry_run import execute_dry_run, generate_gemini_summary
    import datetime as _dt

    # Check 5-minute cache
    cached = await firestore_client.get_dry_run_report(problem_id)
    if cached:
        cached["cached"] = True
        return cached

    steps = await execute_dry_run(plan)
    summary = await generate_gemini_summary(plan, steps)

    report = {
        "problem_id": problem_id,
        "plan_action": plan.get("action", ""),
        "steps": steps,
        "gemini_summary": summary,
        "cached": False,
        "computed_at": _dt.datetime.utcnow().isoformat(),
    }
    await firestore_client.set_dry_run_report(problem_id, report)
    return report


@app.post("/api/incidents/{problem_id}/dry-run", dependencies=[Depends(_require_api_key)])
async def dry_run_incident(problem_id: str):
    """
    Run a full dry-run simulation of the incident's remediation plan.
    No state-changing calls are made. Results are cached for 5 minutes.
    """
    incident = await firestore_client.get_incident(problem_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.get("status") not in ("AWAITING_APPROVAL", "RESOLVED", "REJECTED"):
        raise HTTPException(
            status_code=409,
            detail=f"Incident must be in AWAITING_APPROVAL to preview (status: {incident.get('status')})",
        )

    plan = await firestore_client.get_plan(problem_id)
    if not plan:
        raise HTTPException(status_code=422, detail="No plan found for this incident")

    log_audit_event(AuditEvent(
        actor="operator",
        actor_identity="operator@sitemedic",
        action_type="dry_run_requested",
        incident_id=problem_id,
        payload={"action": plan.get("action")},
        result="success",
    ))

    return await _run_dry_run(problem_id, plan)


# ── Global settings ────────────────────────────────────────────────────────

class GlobalSettings(BaseModel):
    always_dry_run: bool = False


@app.get("/api/settings/global")
async def get_global_settings():
    return await firestore_client.get_global_settings()


@app.put("/api/settings/global", dependencies=[Depends(_require_api_key)])
async def update_global_settings(settings: GlobalSettings):
    await firestore_client.set_global_settings(settings.model_dump())
    return {"status": "saved"}


# ── Dynatrace webhook receiver ─────────────────────────────────────────────

# Maximum retries from Dynatrace before we dead-letter the payload.
_WEBHOOK_DEAD_LETTER_THRESHOLD = 3


@app.post("/api/webhooks/dynatrace")
async def dynatrace_webhook(request: Request):
    """
    Receive Dynatrace problem-notification webhooks.

    Security: HMAC-SHA256 signature validated in constant time before any
    Firestore access.  Returns 200 OK only after a confirmed Firestore write.
    All payloads are logged (sanitized) to the audit trail.
    """
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    # ── Signature validation (non-negotiable) ─────────────────────────────
    if not validate_signature(body, signature):
        log_audit_event(AuditEvent(
            actor="system",
            actor_identity=_agent_id(),
            action_type=ActionType.WEBHOOK_RECEIVED,
            payload={"error": "invalid_signature", "ip": str(request.client)},
            result="failure",
        ))
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        raw = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ── Health probe — respond immediately without creating an incident ────
    if raw.get("_health_probe"):
        return {"status": "ok", "probe_id": raw.get("_probe_id")}

    # ── Parse and sanitize payload before any logging ─────────────────────
    try:
        parsed = parse_dynatrace_payload(raw)
    except Exception as exc:
        logger.exception("Webhook payload parse error")
        raise HTTPException(status_code=422, detail=f"Unparseable payload: {exc}")

    problem_id = parsed.get("problem_id", "")
    state = parsed.get("state", "OPEN")

    # Log sanitized payload to audit trail (raw_payload excluded from the log).
    log_audit_event(AuditEvent(
        actor="system",
        actor_identity=_agent_id(),
        action_type=ActionType.WEBHOOK_RECEIVED,
        incident_id=problem_id or None,
        payload={
            "problem_id": problem_id,
            "state": state,
            "title": parsed.get("title"),
            "severity": parsed.get("severity"),
            "service": parsed.get("service"),
        },
        result="success",
    ))

    # Skip RESOLVED / CLOSED notifications — we handle resolution via polling.
    if state in ("RESOLVED", "CLOSED"):
        return {"status": "ignored", "reason": "resolved_event"}

    if not problem_id:
        raise HTTPException(status_code=422, detail="Missing problem_id in payload")

    # ── Check dead-letter threshold ────────────────────────────────────────
    failure_count = await firestore_client.get_webhook_failure_count(problem_id)
    if failure_count >= _WEBHOOK_DEAD_LETTER_THRESHOLD:
        # Already dead-lettered after exhausted retries — stop returning 5xx.
        logger.warning("Webhook dead-lettered for %s (retries=%d)", problem_id, failure_count)
        return {"status": "dead_lettered", "problem_id": problem_id}

    # ── Create incident (idempotent) ───────────────────────────────────────
    try:
        created_id = await create_incident_from_webhook(parsed)
    except Exception as exc:
        logger.exception("Webhook incident creation failed for %s", problem_id)
        # Write to dead-letter queue and return 500 so Dynatrace retries.
        await firestore_client.create_webhook_failure({
            "problem_id": problem_id,
            "error": str(exc),
            "title": parsed.get("title"),
            "severity": parsed.get("severity"),
        })
        log_audit_event(AuditEvent(
            actor="system",
            actor_identity=_agent_id(),
            action_type=ActionType.WEBHOOK_FAILURE,
            incident_id=problem_id,
            payload={"error": str(exc)},
            result="failure",
        ))
        raise HTTPException(status_code=500, detail="Failed to process webhook")

    if created_id:
        return {"status": "created", "problem_id": created_id}
    else:
        # Idempotent duplicate — already exists.
        return {"status": "duplicate", "problem_id": problem_id}


# ── System health ──────────────────────────────────────────────────────────

@app.get("/api/demo/scenarios")
async def list_demo_scenarios():
    """List all available curated demo scenarios (from INDEX.json or file scan)."""
    from pathlib import Path
    import json as _json
    scenarios_dir = Path(__file__).parent / "demo_mode" / "scenarios"

    # Prefer INDEX.json if available (rich metadata)
    index_path = scenarios_dir / "INDEX.json"
    if index_path.exists():
        try:
            index_data = _json.loads(index_path.read_text())
            return index_data.get("scenarios", [])
        except Exception:
            pass

    # Fallback: scan individual files
    result = []
    for f in sorted(scenarios_dir.glob("*.json")):
        if f.name == "INDEX.json":
            continue
        try:
            data = _json.loads(f.read_text())
            # Support both old (mcp_calls) and new (tool_response_map) format
            if "tool_response_map" in data:
                result.append({
                    "id": data.get("id", f.stem),
                    "display_name": data.get("display_name", data.get("name", f.stem)),
                    "category": data.get("category", ""),
                    "duration_seconds": data.get("duration_seconds", 0),
                    "description": data.get("description", ""),
                    "expected_action": data.get("expected_action", ""),
                })
            else:
                calls = data.get("mcp_calls", [])
                result.append({
                    "id": data.get("name", f.stem),
                    "display_name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "mcp_call_count": len(calls),
                    "tools": list(dict.fromkeys(c["tool"] for c in calls)),
                })
        except Exception:
            pass
    return result


@app.get("/api/demo/status")
async def get_demo_status():
    """Return current telemetry source status (demo vs. live, active scenario, etc.)."""
    from source_factory import get_source_status
    status = await get_source_status()
    from pathlib import Path as _Path
    scenarios_dir = _Path(__file__).parent / "demo_mode" / "scenarios"
    n_scenarios = len([
        f for f in scenarios_dir.glob("*.json") if f.name != "INDEX.json"
    ]) if scenarios_dir.exists() else 0
    if status.get("scenarios_available", 0) == 0:
        status["scenarios_available"] = n_scenarios
    status["demo_public"] = _DEMO_PUBLIC

    # Override active_scenarios with Firestore truth so the count stays
    # accurate even after the in-memory scenario timer expires.
    try:
        from tools import firestore_client as _fsc
        _ACTIVE_STATUSES = {"DETECTING", "DIAGNOSING", "AWAITING_APPROVAL", "REMEDIATING", "PREDICTIVE"}
        _db = _fsc._get_db()
        _active_docs = _db.collection("incidents").where("status", "in", list(_ACTIVE_STATUSES)).stream()
        _active_list = list(_active_docs)
        status["active_scenarios"] = len(_active_list)
        if _active_list and not status.get("current_scenario"):
            # Show the most-recently-started active incident name
            _newest = sorted(
                _active_list,
                key=lambda d: d.to_dict().get("startedAt") or 0,
                reverse=True,
            )[0].to_dict()
            _title = _newest.get("title", "")
            # Strip "Demo: " prefix for brevity
            status["current_scenario"] = _title.replace("Demo: ", "").strip() or None
    except Exception:
        pass  # fall back to in-memory values already set

    return status


@app.get("/api/info")
async def get_info():
    """Public endpoint — no auth. Returns deploy-time flags for the frontend."""
    return {
        "demo_public": _DEMO_PUBLIC,
        "force_demo": os.environ.get("SITEMEDIC_FORCE_DEMO", "false").lower() == "true",
        "env": os.environ.get("ENV", "unknown"),
    }


@app.post("/api/demo/run", dependencies=[Depends(_require_api_key)])
async def run_demo_scenario(request: Request):
    """
    Trigger a full incident lifecycle using a curated demo scenario.
    Body: {"scenario": "memory_leak_001", "auto_approve": true}
         {"random": true}  — picks a random scenario
    """
    body = await request.json()
    random_pick = body.get("random", False)

    # Try new replay engine first
    try:
        from source_factory import get_telemetry_source
        src = await get_telemetry_source()
        from demo_mode.replay_source import DemoModeSource as _ReplaySource
        if isinstance(src, _ReplaySource):
            if random_pick:
                problem_id = src.trigger_random_scenario()
                scenario_name = next(
                    (a["scenario_id"] for a in src.list_active_scenarios()
                     if a["problem_id"] == problem_id),
                    "random"
                )
            else:
                scenario_name = body.get("scenario", "memory_leak_001")
                problem_id = src.trigger_scenario(scenario_name)

            # Immediately create the Firestore incident and spawn diagnosis.
            # The detection loop only polls dynatrace_mcp directly so it never
            # sees replay-source scenarios — we bridge that gap here.
            from orchestrator import _build_incident_data, diagnose_and_plan, register_demo_source
            from tools import firestore_client as _fsc

            existing = await _fsc.get_incident(problem_id)
            if not existing:
                scenario_meta = src._scenarios.get(scenario_name, {})
                problem_details = scenario_meta.get("problem_details", {})
                incident_data = await _build_incident_data(
                    pid=problem_id,
                    severity=problem_details.get("severityLevel", "ERROR"),
                    title=problem_details.get("title", f"Demo: {scenario_name}"),
                    service=problem_details.get("impactedEntities", [{}])[0].get("name", "sitemedic-demo-app")
                        if problem_details.get("impactedEntities") else "sitemedic-demo-app",
                    detection_method="demo_trigger",
                )
                register_demo_source(problem_id, src)
                await _fsc.create_incident(incident_data)
                asyncio.create_task(diagnose_and_plan(problem_id))

            return {
                "status": "started",
                "scenario": scenario_name,
                "problem_id": problem_id,
                "message": (
                    f"Demo incident '{problem_id}' created for scenario '{scenario_name}'. "
                    "Poll /api/incidents to track progress."
                ),
            }
    except Exception as exc:
        logger.warning("Could not use replay engine for demo run: %s", exc)

    # Fallback to legacy DemoRunner
    scenario_name = body.get("scenario", "high_error_rate") if not random_pick else "high_error_rate"
    auto_approve = body.get("auto_approve", True)

    from demo_mode.runner import DemoRunner
    runner = DemoRunner(auto_approve=auto_approve, realistic_latency=False)

    async def _run():
        try:
            await runner.run(scenario_name)
        except Exception as exc:
            logger.exception("Demo run failed for scenario %s: %s", scenario_name, exc)

    asyncio.create_task(_run())
    return {
        "status": "started",
        "scenario": scenario_name,
        "problem_id": None,
        "message": f"Demo incident for '{scenario_name}' is being created. Poll /api/incidents to track progress.",
    }


@app.post("/api/demo/scheduler/pause", dependencies=[Depends(_require_api_key)])
async def pause_demo_scheduler():
    """Pause the automatic scenario scheduler (demo mode only)."""
    from source_factory import get_telemetry_source
    src = await get_telemetry_source()
    from demo_mode.replay_source import DemoModeSource as _ReplaySource
    if not isinstance(src, _ReplaySource):
        raise HTTPException(status_code=409, detail="Agent is not in demo mode")
    src.pause_scheduler()
    return {"status": "paused"}


@app.post("/api/demo/scheduler/resume", dependencies=[Depends(_require_api_key)])
async def resume_demo_scheduler():
    """Resume the automatic scenario scheduler (demo mode only)."""
    from source_factory import get_telemetry_source
    src = await get_telemetry_source()
    from demo_mode.replay_source import DemoModeSource as _ReplaySource
    if not isinstance(src, _ReplaySource):
        raise HTTPException(status_code=409, detail="Agent is not in demo mode")
    src.resume_scheduler()
    return {"status": "resumed"}


@app.post("/api/demo/speed", dependencies=[Depends(_require_api_key)])
async def set_demo_speed(request: Request):
    """Set the replay speed multiplier for the demo scheduler (1x / 2x / 5x)."""
    body = await request.json()
    speed = float(body.get("set_speed", 1.0))
    if speed <= 0 or speed > 10:
        raise HTTPException(status_code=422, detail="speed must be between 0 and 10")
    from source_factory import get_telemetry_source
    src = await get_telemetry_source()
    from demo_mode.replay_source import DemoModeSource as _ReplaySource
    if not isinstance(src, _ReplaySource):
        raise HTTPException(status_code=409, detail="Speed control only available in demo mode")
    src.set_speed(speed)
    return {"status": "ok", "speed": speed}


@app.get("/api/system-health")
async def get_system_health():
    """
    Returns webhook health probe results and detection-method distribution
    for the last 50 incidents.
    """
    health_checks, all_incidents = await asyncio.gather(
        firestore_client.list_webhook_health_checks(limit=20),
        firestore_client.list_incidents(limit=200),
    )
    webhook_failures = await firestore_client.list_webhook_failures(limit=20)

    # Detection method distribution
    webhook_count = sum(1 for i in all_incidents if i.get("detection_method") == "webhook")
    polling_count = sum(1 for i in all_incidents if i.get("detection_method") == "polling")
    unknown_count = sum(1 for i in all_incidents if not i.get("detection_method"))

    # Latency stats from health checks
    latencies = [h["latency_ms"] for h in health_checks if h.get("latency_ms") is not None]
    p95_ms = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else None
    avg_ms = round(sum(latencies) / len(latencies), 1) if latencies else None
    recent_success = sum(1 for h in health_checks[:5] if h.get("success"))

    # TTD stats from incidents with time_to_detect_ms
    ttd_values = [
        i["time_to_detect_ms"]
        for i in all_incidents
        if i.get("time_to_detect_ms") is not None
    ]
    ttd_webhook = [
        i["time_to_detect_ms"]
        for i in all_incidents
        if i.get("detection_method") == "webhook" and i.get("time_to_detect_ms") is not None
    ]
    ttd_polling = [
        i["time_to_detect_ms"]
        for i in all_incidents
        if i.get("detection_method") == "polling" and i.get("time_to_detect_ms") is not None
    ]

    def _avg(lst):
        return round(sum(lst) / len(lst)) if lst else None

    return {
        "health_probes": health_checks,
        "webhook_failures": webhook_failures,
        "probe_stats": {
            "p95_latency_ms": p95_ms,
            "avg_latency_ms": avg_ms,
            "recent_successes": recent_success,
            "total_probes": len(health_checks),
            "sla_target_ms": 500,
        },
        "detection_distribution": {
            "webhook": webhook_count,
            "polling": polling_count,
            "unknown": unknown_count,
            "total": len(all_incidents),
        },
        "time_to_detect": {
            "avg_ms_all": _avg(ttd_values),
            "avg_ms_webhook": _avg(ttd_webhook),
            "avg_ms_polling": _avg(ttd_polling),
        },
    }
