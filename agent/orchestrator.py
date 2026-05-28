"""
Core agent orchestrator.

Three async tasks run concurrently inside the FastAPI process:
  1. detection_loop  — polls Dynatrace every 30s for new problems
  2. diagnose_and_plan — ReAct loop (called per new incident)
  3. generate_postmortem — called after a remediation is executed
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import vertexai
from vertexai.generative_models import (
    Content,
    FunctionDeclaration,
    GenerationConfig,
    GenerativeModel,
    Part,
    Tool,
)

from tools import dynatrace_mcp, firestore_client, gcp_actions, gcp_observability, cost_estimator
from schemas import Incident, RemediationPlan, TraceStep
import correlator as _correlator
from audit import AuditEvent, ActionType, log_audit_event, prompt_hash, _agent_id
from environment import Environment

logger = logging.getLogger(__name__)


async def _gemini_generate(model: Any, contents: Any, model_name: str, **kwargs) -> Any:
    """Wrap asyncio.to_thread(model.generate_content) and record if a session is active."""
    import time
    t0 = time.monotonic()
    error: str | None = None
    response: Any = None
    try:
        response = await asyncio.to_thread(model.generate_content, contents, **kwargs)
        return response
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            from demo_mode.recorder import get_recorder
            rec = get_recorder()
            if rec is not None:
                response_text = ""
                input_tokens = output_tokens = 0
                if response is not None:
                    try:
                        candidate = response.candidates[0]
                        for part in candidate.content.parts:
                            if hasattr(part, "text") and part.text:
                                response_text = part.text[:500]  # truncate for size
                        usage = getattr(response, "usage_metadata", None)
                        if usage:
                            input_tokens = getattr(usage, "prompt_token_count", 0)
                            output_tokens = getattr(usage, "candidates_token_count", 0)
                    except Exception:
                        pass
                rec.record_gemini_call(
                    model=model_name,
                    prompt_summary=f"contents_length={len(str(contents))}",
                    response_text=response_text,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    error=error,
                )
        except Exception:
            pass

# Polling interval configured per environment (dev=30s, staging/prod=300s)
env = Environment.get_instance()
DETECTION_INTERVAL_SECONDS = env.get("detection.polling_interval_seconds", 300)

# JSON Schema for Gemini structured output — combines Diagnosis + RemediationPlan
_DIAGNOSIS_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "confidence": {"type": "number"},
                "confidence_band": {"type": "string", "enum": ["high", "medium", "low"]},
                "evidence_strength": {"type": "string", "enum": ["direct", "circumstantial", "speculative"]},
                "alternative_explanations": {
                    "type": "array",
                    "minItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "explanation": {"type": "string"},
                            "evidence_for": {"type": "string"},
                            "evidence_against": {"type": "string"},
                            "likelihood": {"type": "number"},
                        },
                        "required": ["explanation", "evidence_for", "evidence_against", "likelihood"],
                    },
                },
                "unknowns": {"type": "array", "items": {"type": "string"}},
                "confidence_rationale": {"type": "string"},
            },
            "required": ["root_cause", "confidence", "confidence_band", "evidence_strength",
                         "alternative_explanations", "unknowns", "confidence_rationale"],
        },
        "plan": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "service": {"type": "string"},
                "revision": {"type": "string"},
                "min_instances": {"type": "integer"},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
                "rollback_safe": {"type": "boolean"},
                "rollback_safety": {"type": "string"},
                "requires_explicit_confirmation": {"type": "boolean"},
                "estimated_impact": {"type": "string"},
            },
            "required": ["action", "reason", "confidence", "rollback_safe", "estimated_impact"],
        },
    },
    "required": ["diagnosis", "plan"],
}
MAX_REACT_STEPS = 10
DIAGNOSE_MODEL = "gemini-2.5-pro"
DETECT_MODEL = "gemini-2.5-flash"

_PROMPTS = Path(__file__).parent / "prompts"


def _read_prompt(name: str) -> str:
    return (_PROMPTS / name).read_text()


# ── Vertex AI init ─────────────────────────────────────────────────────────

def init_vertex():
    vertexai.init(
        project=os.environ["GCP_PROJECT_ID"],
        location=os.environ.get("VERTEX_AI_LOCATION", "us-central1"),
    )


# ── Provider routing table — used by _dispatch_tool to tag steps ───────────

_GCP_TOOL_NAMES = frozenset({
    "query_cloud_logging",
    "query_cloud_monitoring",
    "list_recent_slow_traces",
    "get_cloud_trace_spans",
    "get_current_traffic_pattern",
})

_DT_TOOL_NAMES = frozenset({
    "list_problems",
    "get_problem_details",
    "query_metrics",
    "get_traces",
    "list_entities",
    "get_service_response_time",
    "get_error_rate",
})


# ── Tool declarations for Gemini function calling ──────────────────────────

_DT_FUNCTION_DECLARATIONS = [
    FunctionDeclaration(
        name="list_problems",
        description="List open Dynatrace problems",
        parameters={"type": "object", "properties": {
            "status": {"type": "string", "enum": ["OPEN", "RESOLVED"], "description": "Filter by status"},
        }},
    ),
    FunctionDeclaration(
        name="get_problem_details",
        description="Get full root cause details for a specific Dynatrace problem",
        parameters={"type": "object", "properties": {
            "problem_id": {"type": "string", "description": "Dynatrace problem ID"},
        }, "required": ["problem_id"]},
    ),
    FunctionDeclaration(
        name="query_metrics",
        description="Query a Dynatrace metric time series",
        parameters={"type": "object", "properties": {
            "metric_selector": {"type": "string"},
            "entity_selector": {"type": "string"},
            "from_time": {"type": "string", "description": "e.g. now-1h"},
            "to_time": {"type": "string"},
            "resolution": {"type": "string"},
        }, "required": ["metric_selector"]},
    ),
    FunctionDeclaration(
        name="get_traces",
        description="Fetch distributed traces for a service",
        parameters={"type": "object", "properties": {
            "service_name": {"type": "string"},
            "from_time": {"type": "string"},
            "to_time": {"type": "string"},
            "limit": {"type": "integer"},
        }, "required": ["service_name"]},
    ),
    FunctionDeclaration(
        name="list_entities",
        description="List monitored Dynatrace entities (services, hosts, processes)",
        parameters={"type": "object", "properties": {
            "entity_type": {"type": "string", "enum": ["SERVICE", "HOST", "PROCESS_GROUP"]},
        }},
    ),
    FunctionDeclaration(
        name="get_service_response_time",
        description="Get p50/p90/p99 response time for a service over the last hour",
        parameters={"type": "object", "properties": {
            "service_name": {"type": "string"},
        }, "required": ["service_name"]},
    ),
    FunctionDeclaration(
        name="get_error_rate",
        description="Get error rate for a service over the last hour",
        parameters={"type": "object", "properties": {
            "service_name": {"type": "string"},
        }, "required": ["service_name"]},
    ),
]

_GCP_FUNCTION_DECLARATIONS = [
    FunctionDeclaration(
        name="query_cloud_logging",
        description=(
            "Fetch Cloud Logging entries matching a filter. "
            "Use for raw error logs, stack traces, or structured log payloads from Cloud Run. "
            "Example filter: 'resource.type=\"cloud_run_revision\" severity>=ERROR'"
        ),
        parameters={"type": "object", "properties": {
            "filter": {
                "type": "string",
                "description": "Cloud Logging filter expression",
            },
            "time_range_minutes": {
                "type": "integer",
                "description": "How far back to look (default 30)",
            },
        }, "required": ["filter"]},
    ),
    FunctionDeclaration(
        name="query_cloud_monitoring",
        description=(
            "Fetch a Cloud Monitoring metric time series. "
            "Use to get raw GCP-side latency, request counts, or memory metrics. "
            "Example metric_type: 'run.googleapis.com/request_latencies'"
        ),
        parameters={"type": "object", "properties": {
            "metric_type": {
                "type": "string",
                "description": "Full metric type, e.g. run.googleapis.com/request_latencies",
            },
            "resource_labels": {
                "type": "object",
                "description": "Optional label filters, e.g. {\"service_name\": \"sitemedic-demo-app\"}",
            },
            "time_range_minutes": {
                "type": "integer",
                "description": "How far back to look (default 60)",
            },
        }, "required": ["metric_type"]},
    ),
    FunctionDeclaration(
        name="list_recent_slow_traces",
        description=(
            "List recent Cloud Trace distributed traces slower than a latency threshold. "
            "Use to pinpoint which operation is slow when Dynatrace traces are unavailable."
        ),
        parameters={"type": "object", "properties": {
            "service_name": {
                "type": "string",
                "description": "Cloud Run service name to filter traces by",
            },
            "threshold_ms": {
                "type": "integer",
                "description": "Minimum duration in ms to include a trace (default 1000)",
            },
            "time_range_minutes": {
                "type": "integer",
                "description": "How far back to look (default 30)",
            },
        }, "required": ["service_name"]},
    ),
    FunctionDeclaration(
        name="get_cloud_trace_spans",
        description=(
            "Fetch all spans for a specific Cloud Trace trace ID. "
            "Use when you already have a trace_id from a log entry or Dynatrace."
        ),
        parameters={"type": "object", "properties": {
            "trace_id": {
                "type": "string",
                "description": "The Cloud Trace trace ID (hex string)",
            },
        }, "required": ["trace_id"]},
    ),
    FunctionDeclaration(
        name="get_current_traffic_pattern",
        description=(
            "Classify current traffic for a Cloud Run service as 'peak', 'trough', or 'normal' "
            "by comparing the last 30-minute RPS to the 7-day rolling peak. "
            "Call this before proposing a remediation plan to choose the most cost-appropriate action. "
            "During a 'trough', prefer min-instances=0 or deferred rollback to save cost."
        ),
        parameters={"type": "object", "properties": {
            "service": {
                "type": "string",
                "description": "Cloud Run service name to check traffic for",
            },
        }, "required": ["service"]},
    ),
    # ── Resource diagnostic tools (read-only, from gcp_actions) ───────────
    FunctionDeclaration(
        name="query_cloud_sql_active_connections",
        description=(
            "Read-only: fetch current active DB connection count for a Cloud SQL instance "
            "via Cloud Monitoring. Use when investigating connection exhaustion or deadlocks."
        ),
        parameters={"type": "object", "properties": {
            "instance_id": {"type": "string", "description": "Cloud SQL instance name"},
        }, "required": ["instance_id"]},
    ),
    FunctionDeclaration(
        name="query_subscription_backlog",
        description=(
            "Read-only: get the current Pub/Sub subscription backlog size and oldest "
            "unacked message age. Use when investigating consumer lag or message accumulation."
        ),
        parameters={"type": "object", "properties": {
            "subscription": {
                "type": "string",
                "description": "Full subscription resource name: projects/{p}/subscriptions/{s}",
            },
        }, "required": ["subscription"]},
    ),
    FunctionDeclaration(
        name="query_bucket_anomalies",
        description=(
            "Read-only: surface Cloud Storage objects with unusual size or age. "
            "Use when investigating unexpected storage cost spikes or suspected data corruption."
        ),
        parameters={"type": "object", "properties": {
            "bucket": {"type": "string", "description": "GCS bucket name (no gs:// prefix)"},
            "top_n": {"type": "integer", "description": "Max anomalies to return (default 20)"},
        }, "required": ["bucket"]},
    ),
]

# Single Tool with all function declarations — Gemini requires one Tool for function calling
_ALL_TOOLS = [Tool(function_declarations=_DT_FUNCTION_DECLARATIONS + _GCP_FUNCTION_DECLARATIONS)]


# ── Phase 3: per-incident demo source registry ────────────────────────────
# Maps problem_id -> DemoModeSource for incidents running in playback mode.
_demo_sources: dict[str, Any] = {}


def register_demo_source(problem_id: str, source: Any) -> None:
    """Register a DemoModeSource for a specific incident."""
    _demo_sources[problem_id] = source
    logger.info("Demo source registered for %s (scenario=%s)", problem_id, getattr(source, "scenario_name", "?"))


def unregister_demo_source(problem_id: str) -> None:
    _demo_sources.pop(problem_id, None)


async def _dispatch_tool(name: str, args: dict, problem_id: str = "") -> tuple[Any, str]:
    """
    Route a Gemini function call to the correct tool implementation.
    Uses a per-incident DemoModeSource if one is registered, otherwise live APIs.
    Returns (result, provider) where provider is "dynatrace" or "gcp".
    """
    dt = _demo_sources.get(problem_id) or dynatrace_mcp

    # ── Dynatrace MCP tools ────────────────────────────────────────────────
    if name == "list_problems":
        return await dt.list_problems(**args), "dynatrace"
    elif name == "get_problem_details":
        return await dt.get_problem_details(**args), "dynatrace"
    elif name == "query_metrics":
        return await dt.query_metrics(**args), "dynatrace"
    elif name == "get_traces":
        return await dt.get_traces(**args), "dynatrace"
    elif name == "list_entities":
        return await dt.list_entities(**args), "dynatrace"
    elif name == "get_service_response_time":
        return await dt.get_service_response_time(**args), "dynatrace"
    elif name == "get_error_rate":
        return await dt.get_error_rate(**args), "dynatrace"
    # ── GCP observability tools ────────────────────────────────────────────
    elif name == "query_cloud_logging":
        return await gcp_observability.query_cloud_logging(**args), "gcp"
    elif name == "query_cloud_monitoring":
        return await gcp_observability.query_cloud_monitoring(**args), "gcp"
    elif name == "list_recent_slow_traces":
        return await gcp_observability.list_recent_slow_traces(**args), "gcp"
    elif name == "get_cloud_trace_spans":
        return await gcp_observability.get_cloud_trace_spans(**args), "gcp"
    elif name == "get_current_traffic_pattern":
        return await cost_estimator.get_current_traffic_pattern(**args), "gcp"
    # ── Resource diagnostic tools (read-only, delegate to gcp_actions) ────
    elif name == "query_cloud_sql_active_connections":
        return await gcp_actions.query_cloud_sql_active_connections(**args), "gcp"
    elif name == "query_subscription_backlog":
        return await gcp_actions.query_subscription_backlog(**args), "gcp"
    elif name == "query_bucket_anomalies":
        return await gcp_actions.query_bucket_anomalies(**args), "gcp"
    # ── Action tools called prematurely during diagnosis ───────────────────
    elif name in ("rollback_revision", "restart_service", "scale_service", "no_action_needed"):
        return {"error": f"Tool '{name}' is only available after human approval. Use the remediation plan JSON output to propose this action."}, "agent"
    else:
        raise ValueError(f"Unknown tool: {name}")


# ── Detection loop ─────────────────────────────────────────────────────────

async def _build_incident_data(
    pid: str,
    severity: str,
    title: str,
    service: str,
    detection_method: str,
    started_at_dt: datetime | None = None,
    time_to_detect_ms: int | None = None,
) -> dict:
    """Assemble the Firestore document dict for a new incident."""
    now = datetime.utcnow()
    return {
        "problem_id": pid,
        "status": "DETECTING",
        "severity": severity,
        "title": title,
        "service": service,
        "started_at": (started_at_dt or now).isoformat(),
        "trace": [],
        "plan": None,
        "postmortem": None,
        "updated_at": now.isoformat(),
        "correlation_id": str(uuid.uuid4()),
        "providers_used": [],
        "detection_method": detection_method,
        "time_to_detect_ms": time_to_detect_ms,
        "webhook_received_at": now.isoformat() if detection_method == "webhook" else None,
    }


async def create_incident_from_webhook(parsed: dict) -> str | None:
    """
    Called by the webhook endpoint immediately upon receiving a valid
    Dynatrace problem notification.  Returns the problem_id on success or
    None if the incident already exists (idempotency).

    This is the hot path — target latency < 100 ms from entry to Firestore confirm.
    """
    pid = parsed.get("problem_id", "")
    if not pid:
        logger.error("Webhook payload missing problem_id")
        return None

    # Idempotency: if the problem already exists (created by webhook or polling), skip.
    existing = await firestore_client.get_incident(pid)
    if existing:
        logger.info("Webhook: incident %s already exists (method=%s) — skipping", pid, existing.get("detection_method"))
        return None

    # Calculate time-to-detect from Dynatrace's own timestamp.
    started_at: datetime = parsed.get("started_at") or datetime.utcnow()
    now = datetime.utcnow()
    # started_at may be tz-aware; normalize to naive UTC for delta calculation.
    started_naive = started_at.replace(tzinfo=None) if started_at.tzinfo else started_at
    ttd_ms = int((now - started_naive).total_seconds() * 1000)

    incident_data = await _build_incident_data(
        pid=pid,
        severity=parsed.get("severity", "UNKNOWN"),
        title=parsed.get("title", "Unknown incident"),
        service=parsed.get("service", "unknown"),
        detection_method="webhook",
        started_at_dt=started_at,
        time_to_detect_ms=ttd_ms,
    )

    await firestore_client.create_incident(incident_data)

    logger.info("Webhook: incident created pid=%s ttd=%dms", pid, ttd_ms)

    log_audit_event(AuditEvent(
        actor="system",
        actor_identity=_agent_id(),
        action_type=ActionType.INCIDENT_CREATED,
        incident_id=pid,
        payload={
            "severity": incident_data["severity"],
            "title": incident_data["title"],
            "service": incident_data["service"],
            "detection_method": "webhook",
            "time_to_detect_ms": ttd_ms,
        },
        result="success",
    ))

    asyncio.create_task(diagnose_and_plan(pid))
    asyncio.create_task(_link_prediction_if_exists(pid, incident_data["service"]))
    asyncio.create_task(_correlator.correlate_incidents(pid))

    return pid


async def detection_loop():
    """
    Fallback polling loop — runs every 5 minutes to reconcile any problems the
    webhook missed (delivery failure, signature mismatch, restart gap, etc.).

    For problems already created by the webhook, polling is idempotent (skipped).
    New problems found here are tagged detection_method='polling'.
    """
    init_vertex()
    logger.info("Detection loop started (fallback polling, interval=%ds)", DETECTION_INTERVAL_SECONDS)

    while True:
        try:
            problems = await dynatrace_mcp.list_problems(status="OPEN")
            new_count = 0
            reconciled_count = 0

            for problem in problems or []:
                pid = problem.get("problemId") or problem.get("id")
                if not pid:
                    continue
                existing = await firestore_client.get_incident(pid)
                if existing:
                    reconciled_count += 1
                    continue

                # Missed by webhook — create now via polling fallback.
                logger.info("Polling fallback: new problem detected pid=%s", pid)
                new_count += 1

                # Dynatrace polling payload uses lowercase camelCase.
                raw_started = problem.get("startTime")
                started_at_dt = None
                ttd_ms = None
                if raw_started:
                    try:
                        started_at_dt = datetime.utcfromtimestamp(int(raw_started) / 1000)
                        ttd_ms = int((datetime.utcnow() - started_at_dt).total_seconds() * 1000)
                    except Exception:
                        pass

                incident_data = await _build_incident_data(
                    pid=pid,
                    severity=problem.get("severityLevel", "UNKNOWN"),
                    title=problem.get("title", "Unknown incident"),
                    service=problem.get("impactedEntities", [{}])[0].get("name", "unknown"),
                    detection_method="polling",
                    started_at_dt=started_at_dt,
                    time_to_detect_ms=ttd_ms,
                )
                await firestore_client.create_incident(incident_data)
                log_audit_event(AuditEvent(
                    actor="system",
                    actor_identity=_agent_id(),
                    action_type=ActionType.INCIDENT_CREATED,
                    incident_id=pid,
                    payload={
                        "severity": incident_data["severity"],
                        "title": incident_data["title"],
                        "service": incident_data["service"],
                        "detection_method": "polling",
                        "time_to_detect_ms": ttd_ms,
                    },
                    result="success",
                ))
                asyncio.create_task(diagnose_and_plan(pid))
                asyncio.create_task(
                    _link_prediction_if_exists(pid, incident_data["service"])
                )
                asyncio.create_task(_correlator.correlate_incidents(pid))

            log_audit_event(AuditEvent(
                actor="system",
                actor_identity=_agent_id(),
                action_type=ActionType.DETECT_CYCLE,
                payload={
                    "open_problems": len(problems or []),
                    "new_incidents": new_count,
                    "reconciled": reconciled_count,
                    "detection_method": "polling",
                },
                result="success",
            ))
        except Exception:
            logger.exception("Detection loop error")
            log_audit_event(AuditEvent(
                actor="system",
                actor_identity=_agent_id(),
                action_type=ActionType.DETECT_CYCLE,
                payload={"error": "detection loop exception"},
                result="failure",
            ))

        await asyncio.sleep(DETECTION_INTERVAL_SECONDS)


# ── Prediction feedback loop ───────────────────────────────────────────────

async def _link_prediction_if_exists(problem_id: str, service: str) -> None:
    """
    If an active prediction exists for this service, tag it as validated and
    annotate the new incident with the prediction ID. This closes the feedback
    loop between the predictive layer and the reactive detection loop.
    """
    try:
        pred = await firestore_client.find_active_prediction_for_service(service)
        if not pred:
            return
        prediction_id = pred["prediction_id"]
        await asyncio.gather(
            firestore_client.validate_prediction(prediction_id, problem_id),
            firestore_client.link_incident_to_prediction(problem_id, prediction_id),
        )
        logger.info(
            f"Prediction validated: {prediction_id} → incident {problem_id} "
            f"(service={service})"
        )
    except Exception:
        logger.exception(f"Prediction feedback link failed for {problem_id}")


def _proto_to_python(obj: Any) -> Any:
    """Recursively convert proto MapComposite/ListComposite to plain Python dicts/lists."""
    if hasattr(obj, "items"):
        return {k: _proto_to_python(v) for k, v in obj.items()}
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        return [_proto_to_python(v) for v in obj]
    return obj


# ── ReAct diagnosis loop ───────────────────────────────────────────────────

async def diagnose_and_plan(problem_id: str):
    """
    Runs the Gemini 2.5 Pro ReAct loop.
    Each step is persisted to Firestore so the frontend can stream it live.
    """
    try:
        await _diagnose_and_plan_inner(problem_id)
    except Exception as exc:
        logger.exception(f"diagnose_and_plan failed for {problem_id}: {exc}")
        await firestore_client.append_trace(problem_id, {
            "step": 999,
            "thought": f"Diagnosis loop failed with an unexpected error: {exc}. Manual investigation required.",
            "tool_call": None,
            "tool_result": {"error": str(exc)},
            "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        })
        # Leave status as DIAGNOSING rather than advancing to AWAITING_APPROVAL
        # without a plan — operator will see the error in the trace.
    finally:
        unregister_demo_source(problem_id)


async def _diagnose_and_plan_inner(problem_id: str):
    logger.info(f"Starting diagnosis for {problem_id}")
    init_vertex()
    await firestore_client.set_status(problem_id, "DIAGNOSING")

    model = GenerativeModel(
        model_name=DIAGNOSE_MODEL,
        system_instruction=_read_prompt("diagnose.txt"),
    )

    history: list[Content] = [
        Content(role="user", parts=[Part.from_text(
            f"Investigate Dynatrace problem ID: {problem_id}. "
            "Use the available tools to find the root cause and propose a remediation plan."
        )])
    ]

    for step_num in range(MAX_REACT_STEPS):
        gemini_ok = True
        try:
            response = await _gemini_generate(model, history, DIAGNOSE_MODEL, tools=_ALL_TOOLS)
        except Exception as exc:
            logger.exception(f"Gemini call failed at step {step_num}")
            gemini_ok = False
            log_audit_event(AuditEvent(
                actor="agent",
                actor_identity=_agent_id(),
                action_type=ActionType.GEMINI_CALL,
                incident_id=problem_id,
                payload={"step": step_num, "model": DIAGNOSE_MODEL, "error": str(exc)},
                result="failure",
            ))
            break

        log_audit_event(AuditEvent(
            actor="agent",
            actor_identity=_agent_id(),
            action_type=ActionType.GEMINI_CALL,
            incident_id=problem_id,
            payload={
                "step": step_num,
                "model": DIAGNOSE_MODEL,
                "history_length": len(history),
            },
            result="success",
        ))

        candidate = response.candidates[0]
        thought_text = ""
        function_call = None

        for part in candidate.content.parts:
            if hasattr(part, "text") and part.text:
                thought_text = part.text
            if hasattr(part, "function_call") and part.function_call:
                function_call = part.function_call

        trace_step: dict[str, Any] = {
            "step": step_num,
            "thought": thought_text,
            "tool_call": None,
            "tool_result": None,
            "timestamp": datetime.utcnow().isoformat(),
            "provider": None,
        }

        if function_call:
            tool_name = function_call.name
            tool_args = _proto_to_python(function_call.args)
            trace_step["tool_call"] = {"name": tool_name, "args": tool_args}

            tool_ok = True
            try:
                tool_result, provider = await _dispatch_tool(tool_name, tool_args, problem_id=problem_id)
            except Exception as e:
                tool_result, provider = {"error": str(e)}, None
                tool_ok = False

            trace_step["tool_result"] = tool_result
            trace_step["provider"] = provider

            action_type = (
                ActionType.MCP_TOOL_CALL if tool_name in _DT_TOOL_NAMES
                else ActionType.GCP_TOOL_CALL
            )
            log_audit_event(AuditEvent(
                actor="agent",
                actor_identity=_agent_id(),
                action_type=action_type,
                incident_id=problem_id,
                resource=f"dt:problems/{problem_id}" if provider == "dynatrace" else None,
                payload={"tool": tool_name, "args": tool_args},
                result="success" if tool_ok else "failure",
            ))

            # Persist step and record which provider was used (ArrayUnion deduplicates)
            await asyncio.gather(
                firestore_client.append_trace(problem_id, trace_step),
                firestore_client.record_provider(problem_id, provider) if provider else asyncio.sleep(0),
            )

            logger.info(f"[{problem_id}] step={step_num} tool={tool_name} provider={provider}")

            # Feed result back into history for next iteration
            history.append(candidate.content)
            history.append(Content(
                role="user",
                parts=[Part.from_function_response(
                    name=tool_name,
                    response={"result": tool_result},
                )],
            ))
        else:
            # Gemini finished reasoning — request structured diagnosis + plan
            await firestore_client.append_trace(problem_id, trace_step)
            await _finalize_diagnosis_and_plan(problem_id, history, model, step_num + 1)
            return

    # Exhausted steps without a clean finish — request structured output now
    logger.warning(f"ReAct loop exhausted for {problem_id}, requesting structured output")
    await _finalize_diagnosis_and_plan(problem_id, history, model, MAX_REACT_STEPS)


async def _finalize_diagnosis_and_plan(
    problem_id: str,
    history: list[Content],
    model: GenerativeModel,
    steps_taken: int,
) -> None:
    """
    Final structured-output call: asks Gemini to emit a combined Diagnosis + RemediationPlan
    as validated JSON using response_schema. Stores diagnosis, sets confidence_blocked,
    and fires a self-consistency re-check for low-confidence cases.
    """
    history_with_prompt = history + [Content(
        role="user",
        parts=[Part.from_text(
            "Based on your investigation, output your structured diagnosis and remediation plan now. "
            "Do not call any more tools. Output ONLY valid JSON matching this schema:\n"
            '{"diagnosis": {"root_cause": "string", "confidence": 0.0-1.0, '
            '"confidence_band": "high|medium|low", "evidence_strength": "direct|circumstantial|speculative", '
            '"alternative_explanations": [{"explanation": "string", "evidence_for": "string", '
            '"evidence_against": "string", "likelihood": 0.0-1.0}], '
            '"unknowns": ["string"], "confidence_rationale": "string"}, '
            '"plan": {"action": "rollback_revision|scale_service|restart_service|no_action_needed", '
            '"service": "string", "reason": "string", "confidence": 0.0-1.0, '
            '"rollback_safe": true|false, "estimated_impact": "string"}}'
        )],
    )]

    structured_model = GenerativeModel(
        model_name=DIAGNOSE_MODEL,
        system_instruction=_read_prompt("diagnose.txt"),
        generation_config=GenerationConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    result: dict | None = None
    try:
        response = await _gemini_generate(structured_model, history_with_prompt, DIAGNOSE_MODEL)
        raw = response.candidates[0].content.parts[0].text
        result = json.loads(raw)
    except Exception as exc:
        logger.exception(f"Structured output call failed for {problem_id}: {exc}")

    if not result:
        # Structured output failed — save a safe fallback plan so the operator can
        # review and reject rather than seeing a blank AWAITING_APPROVAL screen.
        incident = await firestore_client.get_incident(problem_id)
        result = {
            "diagnosis": {
                "root_cause": "Automated diagnosis could not produce a structured output. Manual review required.",
                "confidence": 0.0,
                "confidence_band": "low",
                "evidence_strength": "speculative",
                "alternative_explanations": [],
                "unknowns": ["Structured output call failed — see agent logs"],
                "confidence_rationale": "Plan generation error",
            },
            "plan": {
                "action": "no_action_needed",
                "service": incident.get("service", "") if incident else "",
                "reason": "Automated plan generation failed. Please review the trace and take manual action if needed.",
                "confidence": 0.0,
                "rollback_safe": True,
                "estimated_impact": "No automated action will be taken.",
            },
        }
        log_audit_event(AuditEvent(
            actor="agent", actor_identity=_agent_id(),
            action_type=ActionType.PLAN_GENERATED, incident_id=problem_id,
            payload={"error": "structured output failed — fallback plan used", "steps_taken": steps_taken},
            result="failure",
        ))

    diagnosis = result.get("diagnosis", {})
    plan = result.get("plan", {})
    confidence = float(diagnosis.get("confidence", plan.get("confidence", 0.5)))

    # Ensure plan.confidence mirrors diagnosis.confidence if not set independently
    plan.setdefault("confidence", confidence)

    # Store structured diagnosis
    await firestore_client.set_diagnosis(problem_id, diagnosis)

    # Block auto-actions when confidence < 0.6
    if confidence < 0.6:
        await firestore_client.set_confidence_blocked(problem_id, True)
        logger.info(f"[{problem_id}] Confidence {confidence:.2f} < 0.6 — auto-action blocked")

    # Enrich and persist plan
    plan = await _enrich_plan_with_cost(plan)
    await firestore_client.set_plan(problem_id, plan)
    await firestore_client.set_status(problem_id, "AWAITING_APPROVAL")

    logger.info(
        f"Diagnosis complete for {problem_id}: action={plan.get('action')} "
        f"confidence={confidence:.2f} band={diagnosis.get('confidence_band')}"
    )
    log_audit_event(AuditEvent(
        actor="agent", actor_identity=_agent_id(),
        action_type=ActionType.PLAN_GENERATED, incident_id=problem_id,
        payload={
            "action": plan.get("action"),
            "confidence": confidence,
            "confidence_band": diagnosis.get("confidence_band"),
            "evidence_strength": diagnosis.get("evidence_strength"),
            "steps_taken": steps_taken,
            "confidence_blocked": confidence < 0.6,
        },
        result="success",
    ))

    # Async self-consistency check for borderline confidence
    if 0.4 <= confidence < 0.75:
        asyncio.create_task(_self_consistency_check(problem_id, plan, history))


async def _self_consistency_check(
    problem_id: str,
    primary_plan: dict,
    history: list[Content],
) -> None:
    """
    Re-run diagnosis at temperature=1.0 with a fresh context to check consistency.
    If the recommended action differs, store the competing diagnosis so operators
    can see both and make an explicit choice.
    """
    logger.info(f"[{problem_id}] Running self-consistency check")
    check_model = GenerativeModel(
        model_name=DIAGNOSE_MODEL,
        system_instruction=_read_prompt("diagnose.txt"),
        generation_config=GenerationConfig(
            response_mime_type="application/json",
            response_schema=_DIAGNOSIS_PLAN_SCHEMA,
            temperature=1.0,
        ),
    )
    try:
        response = await _gemini_generate(
            check_model,
            history + [Content(
                role="user",
                parts=[Part.from_text(
                    "Re-evaluate the evidence independently and output your diagnosis and plan."
                )],
            )],
            DIAGNOSE_MODEL,
        )
        raw = response.candidates[0].content.parts[0].text
        result = json.loads(raw)
        competing_plan = result.get("plan", {})
        competing_diagnosis = result.get("diagnosis", {})

        if competing_plan.get("action") != primary_plan.get("action"):
            logger.info(
                f"[{problem_id}] Self-consistency disagreement: "
                f"primary={primary_plan.get('action')} competing={competing_plan.get('action')}"
            )
            await firestore_client.set_competing_diagnosis(problem_id, {
                "diagnosis": competing_diagnosis,
                "plan": competing_plan,
                "note": (
                    f"Independent re-run recommended '{competing_plan.get('action')}' "
                    f"vs primary '{primary_plan.get('action')}'. "
                    "Operator must choose before approval."
                ),
            })
        else:
            logger.info(f"[{problem_id}] Self-consistency check agrees: {primary_plan.get('action')}")
    except Exception:
        logger.exception(f"Self-consistency check failed for {problem_id}")


async def _enrich_plan_with_cost(plan: dict) -> dict:
    """
    Server-side: fill estimated_hourly_cost_delta_usd on the plan (and its
    cost_optimized_alternative, if present) using Billing API + Firestore cache.
    Also propagates traffic_context if Gemini already set it.
    """
    try:
        action = plan.get("action", "")
        cost_data = await cost_estimator.estimate_remediation_cost(action, plan)
        plan["estimated_hourly_cost_delta_usd"] = cost_data.get("estimated_hourly_cost_delta_usd", 0.0)
        # Preserve traffic_context if Gemini set it (via get_current_traffic_pattern)
        if not plan.get("traffic_context"):
            plan["traffic_context"] = "normal"
        # Enrich the alternative plan recursively (one level only)
        alt = plan.get("cost_optimized_alternative")
        if isinstance(alt, dict):
            alt_action = alt.get("action", "")
            alt_cost = await cost_estimator.estimate_remediation_cost(alt_action, alt)
            alt["estimated_hourly_cost_delta_usd"] = alt_cost.get("estimated_hourly_cost_delta_usd", 0.0)
            alt.setdefault("traffic_context", plan.get("traffic_context", "normal"))
    except Exception:
        logger.exception("Cost enrichment failed; plan stored without cost fields")
    return plan


# ── Postmortem generation ──────────────────────────────────────────────────

async def generate_postmortem(problem_id: str):
    """Generate a markdown postmortem from the full incident trace."""
    logger.info(f"Generating postmortem for {problem_id}")

    incident = await firestore_client.get_incident(problem_id)
    if not incident:
        return

    trace_summary = "\n\n".join(
        f"Step {s['step']}: {s['thought']}\n"
        f"Tool: {json.dumps(s.get('tool_call'))}\n"
        f"Result: {json.dumps(s.get('tool_result'))}"
        for s in incident.get("trace", [])
    )

    # Fetch cost snapshot for business impact section
    cost_section = ""
    try:
        from incident_cost import compute_cost_snapshot
        cost = await compute_cost_snapshot(problem_id)
        if cost.get("revenue_configured") and cost.get("burn_rate_per_min") is not None:
            dur = cost.get("duration_minutes", 0)
            burn = cost.get("burn_rate_per_min", 0)
            cumulative = cost.get("cumulative_usd") or round(burn * dur, 2)
            remediation = cost.get("remediation_hourly_usd", 0)
            net_savings = round(cumulative - remediation, 2)
            cost_section = (
                f"\n## Cost Data\n"
                f"- Duration: {dur:.1f} minutes\n"
                f"- Burn rate: ${burn:.4f}/min\n"
                f"- Cumulative incident cost: ${cumulative:.2f}\n"
                f"- Remediation cost: ${remediation:.4f}/hr\n"
                f"- Net savings vs no-action: ${net_savings:.2f}\n"
            )
    except Exception:
        logger.debug(f"Cost snapshot unavailable for postmortem {problem_id}")

    prompt = (
        f"{_read_prompt('postmortem.txt')}\n\n"
        f"## Incident Data\n"
        f"- Title: {incident['title']}\n"
        f"- Service: {incident['service']}\n"
        f"- Severity: {incident['severity']}\n"
        f"- Started at: {incident['started_at']}\n"
        f"- Resolved at: {datetime.utcnow().isoformat()}\n"
        f"- Plan executed: {json.dumps(incident.get('plan'))}\n"
        f"{cost_section}\n"
        f"## Investigation Trace\n{trace_summary}"
    )

    model = GenerativeModel(model_name=DIAGNOSE_MODEL)
    try:
        response = await _gemini_generate(
            model,
            [Content(role="user", parts=[Part.from_text(prompt)])],
            DIAGNOSE_MODEL,
        )
        postmortem_text = response.candidates[0].content.parts[0].text
        await firestore_client.set_postmortem(problem_id, postmortem_text)
        logger.info(f"Postmortem saved for {problem_id}")
        log_audit_event(AuditEvent(
            actor="agent",
            actor_identity=_agent_id(),
            action_type=ActionType.POSTMORTEM,
            incident_id=problem_id,
            payload={"model": DIAGNOSE_MODEL, "length_chars": len(postmortem_text)},
            result="success",
        ))
    except Exception as exc:
        logger.exception(f"Postmortem generation failed for {problem_id}")
        log_audit_event(AuditEvent(
            actor="agent",
            actor_identity=_agent_id(),
            action_type=ActionType.POSTMORTEM,
            incident_id=problem_id,
            payload={"error": str(exc)},
            result="failure",
        ))
