"""
Incident correlator — detects when multiple Dynatrace problems share a root cause
and groups them into an IncidentCluster for coordinated remediation.

Entry point: correlate_incidents(new_problem_id) — called as a fire-and-forget
asyncio task immediately after a new incident is created in the detection loop.

Cluster execution entry point: execute_cluster(cluster_id, mode) — called from
the approve API endpoint.

Constraints:
- Maximum 5 incidents per cluster.
- Only correlates incidents in DETECTING or DIAGNOSING status.
- Service dependency graph is cached for 10 minutes.
- Gemini confidence must be >= 0.70 to form a cluster.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import vertexai
from vertexai.generative_models import Content, GenerationConfig, GenerativeModel, Part

from tools import dynatrace_mcp, firestore_client

logger = logging.getLogger(__name__)

CORRELATE_MODEL = "gemini-2.5-pro-preview-05-06"
MAX_CLUSTER_SIZE = 5
TEMPORAL_WINDOW_MINUTES = 5
CORRELATION_CONFIDENCE_THRESHOLD = 0.70
DEPENDENCY_CACHE_TTL_SECONDS = 600  # 10 minutes

_PROMPTS = Path(__file__).parent / "prompts"

# In-process cache for the Dynatrace service topology
_dep_graph_cache: dict[str, Any] = {}
_dep_graph_fetched_at: float = 0.0


# ── Service dependency graph (cached) ─────────────────────────────────────

async def _get_dependency_graph() -> dict:
    global _dep_graph_cache, _dep_graph_fetched_at
    now = time.monotonic()
    if now - _dep_graph_fetched_at < DEPENDENCY_CACHE_TTL_SECONDS and _dep_graph_cache:
        return _dep_graph_cache
    try:
        entities = await dynatrace_mcp.list_entities(entity_type="SERVICE")
        _dep_graph_cache = {"services": entities or []}
        _dep_graph_fetched_at = now
        logger.debug(f"Dependency graph refreshed: {len(_dep_graph_cache['services'])} services")
    except Exception:
        logger.exception("Failed to fetch Dynatrace service topology for correlation")
        _dep_graph_cache = {"services": []}
    return _dep_graph_cache


# ── Correlation candidates ─────────────────────────────────────────────────

async def _get_correlation_candidates(exclude_id: str) -> list[dict]:
    """
    Return DETECTING/DIAGNOSING incidents started within the last 5 minutes that
    are not already in a cluster and are not the newly created incident.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=TEMPORAL_WINDOW_MINUTES)
    all_recent = await firestore_client.list_incidents(limit=20)
    candidates = []
    for inc in all_recent:
        if inc.get("problem_id") == exclude_id:
            continue
        if inc.get("status") not in ("DETECTING", "DIAGNOSING"):
            continue
        if inc.get("cluster_id"):
            continue  # already claimed
        started_raw = inc.get("started_at")
        if not started_raw:
            continue
        try:
            if isinstance(started_raw, str):
                started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
            else:
                started_at = started_raw
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if started_at >= cutoff:
            candidates.append(inc)
    return candidates[: MAX_CLUSTER_SIZE - 1]  # leave room for the triggering incident


# ── Gemini calls ───────────────────────────────────────────────────────────

def _init_vertex() -> None:
    import os
    import vertexai as _vt
    _vt.init(
        project=os.environ["GCP_PROJECT_ID"],
        location=os.environ.get("VERTEX_AI_LOCATION", "us-central1"),
    )


async def _ask_gemini_to_correlate(
    new_incident: dict,
    candidates: list[dict],
    dep_graph: dict,
) -> dict:
    """
    Ask Gemini 2.5 Pro whether N incidents share a single root cause.
    Returns: {should_cluster, confidence, root_cause_summary, member_ids}
    """
    all_incidents = [new_incident] + candidates
    incident_summaries = json.dumps(
        [
            {
                "problem_id": i.get("problem_id"),
                "title": i.get("title"),
                "service": i.get("service"),
                "severity": i.get("severity"),
                "started_at": str(i.get("started_at")),
                "status": i.get("status"),
            }
            for i in all_incidents
        ],
        indent=2,
    )

    prompt = f"""You are an SRE analyzing multiple simultaneous production incidents.

Service topology (from Dynatrace — first 20 entries):
{json.dumps(dep_graph.get('services', [])[:20], indent=2)}

Incidents that fired within the last {TEMPORAL_WINDOW_MINUTES} minutes:
{incident_summaries}

Task: Determine whether these incidents are manifestations of a single underlying cause
(e.g., a shared upstream dependency failure, a network partition, a bad deployment
propagating across services, or a database connection storm).

Respond with a JSON object only — no markdown, no prose:
{{
  "should_cluster": true | false,
  "confidence": 0.0-1.0,
  "root_cause_summary": "One or two sentences on the probable common root cause. Empty string if should_cluster is false.",
  "member_ids": ["list", "of", "problem_ids", "to", "group"]
}}

Rules:
- Set should_cluster=true only when confidence >= {CORRELATION_CONFIDENCE_THRESHOLD}
- member_ids must be a subset of the problem_ids shown above (include all if should_cluster=true)
- If should_cluster=false, set member_ids=[] and root_cause_summary=""
- Never include problem IDs not present in the list above
- Maximum {MAX_CLUSTER_SIZE} entries in member_ids
"""

    model = GenerativeModel(
        model_name=CORRELATE_MODEL,
        generation_config=GenerationConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [Content(role="user", parts=[Part.from_text(prompt)])],
        )
        text = response.candidates[0].content.parts[0].text
        result = json.loads(text)
        return result
    except Exception:
        logger.exception("Gemini correlation call failed")
        return {
            "should_cluster": False,
            "confidence": 0.0,
            "root_cause_summary": "",
            "member_ids": [],
        }


async def _generate_coordinated_plan(
    cluster_id: str,
    member_incidents: list[dict],
    root_cause_summary: str,
) -> list[dict]:
    """
    Ask Gemini 2.5 Pro for an ordered remediation plan across all cluster members.
    Returns a list of ClusterStep dicts sorted by step_index.
    """
    available_actions = [
        "rollback_revision",
        "scale_service",
        "restart_service",
        "no_action_needed",
        "failover_cloud_sql_replica",
        "restart_cloud_sql_instance",
        "purge_pubsub_subscription_backlog",
        "seek_subscription_to_timestamp",
    ]

    incident_summaries = json.dumps(
        [
            {
                "problem_id": i.get("problem_id"),
                "title": i.get("title"),
                "service": i.get("service"),
                "severity": i.get("severity"),
                "plan": i.get("plan"),
            }
            for i in member_incidents
        ],
        indent=2,
    )

    prompt = f"""You are generating a coordinated remediation plan for a cluster of {len(member_incidents)} correlated incidents.

Root cause: {root_cause_summary}

Incidents (some may already have individual remediation plans from prior Gemini diagnosis):
{incident_summaries}

Available actions: {json.dumps(available_actions)}

Generate an ordered list of remediation steps. ORDER MATTERS — fix the root cause service first,
then dependent services. Each step targets exactly one service.

Respond with a JSON array only — no markdown, no prose:
[
  {{
    "step_index": 0,
    "service": "cloud-run-service-name",
    "action": "one_of_the_available_actions",
    "reason": "Why this step is first and what it fixes",
    "incident_id": "the problem_id this step addresses, or null if cross-cutting"
  }}
]

Rules:
- Maximum {MAX_CLUSTER_SIZE} steps total
- Services that are root-cause contributors come first
- If a service already has a good individual plan, prefer its action
- Use no_action_needed for services that will self-heal once upstream is fixed
- incident_id must be a problem_id from the list above, or null
"""

    model = GenerativeModel(
        model_name=CORRELATE_MODEL,
        generation_config=GenerationConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [Content(role="user", parts=[Part.from_text(prompt)])],
        )
        text = response.candidates[0].content.parts[0].text
        raw = json.loads(text)
        # Handle both bare array and {"steps": [...]} wrapper
        if isinstance(raw, dict) and "steps" in raw:
            raw = raw["steps"]
        steps = []
        for s in raw[: MAX_CLUSTER_SIZE]:
            steps.append(
                {
                    "step_index": s.get("step_index", len(steps)),
                    "service": s.get("service", ""),
                    "action": s.get("action", "no_action_needed"),
                    "reason": s.get("reason", ""),
                    "incident_id": s.get("incident_id"),
                    "status": "pending",
                    "result": None,
                }
            )
        return steps
    except Exception:
        logger.exception(f"Coordinated plan generation failed for cluster {cluster_id}")
        return []


# ── Correlate entry point ──────────────────────────────────────────────────

async def correlate_incidents(new_problem_id: str) -> None:
    """
    Called (fire-and-forget) after each new incident is created.
    If correlated with other recent incidents, forms or joins an IncidentCluster.
    """
    try:
        _init_vertex()
        new_incident = await firestore_client.get_incident(new_problem_id)
        if not new_incident:
            return

        candidates = await _get_correlation_candidates(new_problem_id)
        if not candidates:
            logger.debug(f"No correlation candidates for {new_problem_id} — skipping")
            return

        dep_graph, correlation = await asyncio.gather(
            _get_dependency_graph(),
            _ask_gemini_to_correlate(new_incident, candidates, {}),
        )
        # Re-run with dep_graph (gather above returns dep_graph separately)
        correlation = await _ask_gemini_to_correlate(new_incident, candidates, dep_graph)

        if not correlation.get("should_cluster"):
            logger.info(
                f"No cluster formed for {new_problem_id} "
                f"(confidence={correlation.get('confidence', 0):.2f})"
            )
            return

        member_ids: list[str] = correlation.get("member_ids", [])
        if new_problem_id not in member_ids:
            member_ids.append(new_problem_id)
        member_ids = member_ids[:MAX_CLUSTER_SIZE]

        root_cause_summary = correlation.get("root_cause_summary", "")
        confidence = float(correlation.get("confidence", 0.0))

        logger.info(
            f"Cluster forming: {len(member_ids)} incidents, "
            f"confidence={confidence:.2f}, root='{root_cause_summary[:80]}'"
        )

        # If any candidate is already in a cluster, join it instead of creating a new one
        existing_cluster_id: str | None = None
        for mid in member_ids:
            inc = await firestore_client.get_incident(mid)
            if inc and inc.get("cluster_id"):
                existing_cluster_id = inc["cluster_id"]
                break

        if existing_cluster_id:
            await firestore_client.add_incidents_to_cluster(existing_cluster_id, member_ids)
            cluster_id = existing_cluster_id
            logger.info(f"Joined existing cluster {cluster_id}")
        else:
            cluster_id = f"cluster_{uuid.uuid4().hex[:12]}"
            member_incidents = [
                inc
                for mid in member_ids
                if (inc := await firestore_client.get_incident(mid))
            ]
            coordinated_plan = await _generate_coordinated_plan(
                cluster_id, member_incidents, root_cause_summary
            )
            cluster_data = {
                "cluster_id": cluster_id,
                "member_incident_ids": member_ids,
                "root_cause_summary": root_cause_summary,
                "confidence": confidence,
                "coordinated_plan": coordinated_plan,
                "execution_order": [s["service"] for s in coordinated_plan],
                "status": "AWAITING_APPROVAL",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await firestore_client.create_cluster(cluster_data)
            logger.info(f"Cluster created: {cluster_id} with {len(coordinated_plan)} steps")

        # Tag every member incident with the cluster_id
        await asyncio.gather(
            *[firestore_client.set_incident_cluster(mid, cluster_id) for mid in member_ids]
        )

    except Exception:
        logger.exception(f"Correlator failed for {new_problem_id}")


# ── Cluster execution ──────────────────────────────────────────────────────

async def execute_cluster(cluster_id: str, mode: str = "all_at_once") -> None:
    """
    Execute a cluster's coordinated plan in declared step order.
    Halts on the first failed step and marks the cluster FAILED.
    Spawns postmortem generation for each member incident after its step completes.
    """
    from orchestrator import generate_postmortem
    from tools import gcp_actions

    cluster = await firestore_client.get_cluster(cluster_id)
    if not cluster:
        logger.error(f"execute_cluster: cluster {cluster_id} not found")
        return

    await firestore_client.set_cluster_status(cluster_id, "EXECUTING")
    steps = sorted(cluster.get("coordinated_plan", []), key=lambda s: s["step_index"])

    for step in steps:
        step_idx = step["step_index"]
        incident_id = step.get("incident_id")

        await firestore_client.update_cluster_step(cluster_id, step_idx, "running")

        try:
            # Prefer the individual incident's existing plan (it has full detail like revision)
            plan_dict: dict | None = None
            if incident_id:
                incident = await firestore_client.get_incident(incident_id)
                if incident and incident.get("plan"):
                    plan_dict = incident["plan"]

            # Fallback: build a minimal plan from the cluster step
            if not plan_dict:
                plan_dict = {
                    "action": step["action"],
                    "service": step["service"],
                    "reason": step["reason"],
                    "confidence": 0.9,
                    "rollback_safe": True,
                    "rollback_safety": "reversible",
                    "requires_explicit_confirmation": False,
                    "estimated_impact": f"Cluster step {step_idx}: {step['action']} on {step['service']}",
                }

            result = await gcp_actions.execute_remediation(plan_dict)
            await firestore_client.update_cluster_step(cluster_id, step_idx, "done", result)
            logger.info(
                f"Cluster {cluster_id} step {step_idx} done: "
                f"{step['action']} on {step['service']}"
            )

            if incident_id:
                await firestore_client.append_trace(
                    incident_id,
                    {
                        "step": 998,
                        "thought": (
                            f"Cluster remediation step {step_idx}: "
                            f"{step['action']} on {step['service']} — part of cluster {cluster_id}"
                        ),
                        "tool_call": {"name": step["action"], "args": plan_dict},
                        "tool_result": result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
                asyncio.create_task(generate_postmortem(incident_id))

        except Exception as exc:
            logger.error(
                f"Cluster {cluster_id} step {step_idx} FAILED "
                f"({step['action']} on {step['service']}): {exc}"
            )
            await firestore_client.update_cluster_step(
                cluster_id, step_idx, "failed", {"error": str(exc)}
            )
            await firestore_client.set_cluster_status(cluster_id, "FAILED")
            return

    await firestore_client.set_cluster_status(cluster_id, "COMPLETE")
    logger.info(f"Cluster {cluster_id} execution complete ({len(steps)} steps)")
