"""
Predictive SLO breach forecasting.

predictive_loop() runs every 5 minutes:
  1. Discover up to MAX_SERVICES_PER_CYCLE monitored services
  2. Fetch the last 30 minutes of key metrics (error rate, p99 latency, memory)
     from Dynatrace MCP and GCP Monitoring
  3. Send the time-series snapshot to Gemini 2.5 Pro for trend analysis
  4. Persist predictions (confidence >= 0.70) to Firestore `predictions` collection
  5. Auto-open PREDICTIVE incidents for high-confidence predictions (>= 0.85)
  6. Expire false-positive predictions from the previous cycle
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vertexai
from vertexai.generative_models import Content, GenerationConfig, GenerativeModel, Part

from schemas import Prediction, RemediationAction
from tools import dynatrace_mcp, firestore_client, gcp_observability
from environment import Environment

logger = logging.getLogger(__name__)

env = Environment.get_instance()

PREDICT_MODEL = "gemini-2.5-pro"
PREDICT_INTERVAL_SECONDS = env.get("prediction.interval_seconds", 300)  # 5 minutes default
MAX_SERVICES_PER_CYCLE = 4
PREDICTION_TTL_MINUTES = env.get("prediction.ttl_minutes", 30)
MIN_CONFIDENCE_TO_STORE = 0.70
MIN_CONFIDENCE_TO_INCIDENT = env.get("correlation.confidence_threshold", 0.85)

_PROMPTS = Path(__file__).parent / "prompts"


def _read_prompt(name: str) -> str:
    return (_PROMPTS / name).read_text()


# ── Service discovery ──────────────────────────────────────────────────────

async def _discover_services() -> list[str]:
    """
    Return up to MAX_SERVICES_PER_CYCLE unique service names to monitor.

    Priority:
      1. MONITORED_SERVICES env var (comma-separated)
      2. Distinct service names from the 20 most recent Firestore incidents
    """
    env_services = os.environ.get("MONITORED_SERVICES", "")
    if env_services.strip():
        services = [s.strip() for s in env_services.split(",") if s.strip()]
        return services[:MAX_SERVICES_PER_CYCLE]

    recent = await firestore_client.list_incidents(limit=20)
    seen: list[str] = []
    for inc in recent:
        svc = inc.get("service", "")
        if svc and svc not in seen:
            seen.append(svc)
    return seen[:MAX_SERVICES_PER_CYCLE]


# ── Metric collection ──────────────────────────────────────────────────────

async def _collect_metrics_for_service(service: str) -> dict:
    """
    Gather last 30 minutes of error rate, p99 latency, and memory utilisation
    for a single service. Returns a dict safe to serialise for the Gemini prompt.
    Uses 5-minute resolution (6 data points per metric) for trend visibility.
    """
    metrics: dict = {"service": service, "window": "last 30 minutes", "resolution": "5-minute buckets"}

    # Dynatrace: p99 latency time series
    try:
        latency = await dynatrace_mcp.query_metrics(
            metric_selector="builtin:service.response.time:percentile(99)",
            entity_selector=f'type(SERVICE),entityName("{service}")',
            from_time="now-30m",
            to_time="now",
            resolution="5m",
        )
        metrics["p99_latency_ms"] = latency
    except Exception as exc:
        logger.debug(f"DT latency metrics unavailable for {service}: {exc}")
        metrics["p99_latency_ms"] = "unavailable"

    # Dynatrace: error rate time series
    try:
        errors = await dynatrace_mcp.query_metrics(
            metric_selector="builtin:service.errors.total.rate",
            entity_selector=f'type(SERVICE),entityName("{service}")',
            from_time="now-30m",
            to_time="now",
            resolution="5m",
        )
        metrics["error_rate_pct"] = errors
    except Exception as exc:
        logger.debug(f"DT error metrics unavailable for {service}: {exc}")
        metrics["error_rate_pct"] = "unavailable"

    # GCP Monitoring: Cloud Run container memory utilisation
    try:
        memory = await gcp_observability.query_cloud_monitoring(
            metric_type="run.googleapis.com/container/memory/utilizations",
            resource_labels={"service_name": service},
            time_range_minutes=30,
        )
        metrics["memory_utilisation_pct"] = memory
    except Exception as exc:
        logger.debug(f"GCP memory metrics unavailable for {service}: {exc}")
        metrics["memory_utilisation_pct"] = "unavailable"

    # GCP Monitoring: Cloud Run request latencies (corroborates DT)
    try:
        gcp_latency = await gcp_observability.query_cloud_monitoring(
            metric_type="run.googleapis.com/request_latencies",
            resource_labels={"service_name": service},
            time_range_minutes=30,
        )
        metrics["gcp_request_latency_ms"] = gcp_latency
    except Exception as exc:
        logger.debug(f"GCP latency metrics unavailable for {service}: {exc}")
        metrics["gcp_request_latency_ms"] = "unavailable"

    return metrics


# ── Gemini prediction call ─────────────────────────────────────────────────

async def _run_prediction(all_metrics: list[dict]) -> list[dict]:
    """
    Send all services' metrics to Gemini 2.5 Pro for trend analysis.
    Returns a list of raw prediction dicts from Gemini.
    """
    system_prompt = _read_prompt("predict.txt")
    metrics_payload = json.dumps(all_metrics, indent=2, default=str)

    user_message = (
        f"Analyse the following metric snapshots and return your predictions as a JSON array.\n\n"
        f"```json\n{metrics_payload}\n```"
    )

    model = GenerativeModel(
        model_name=PREDICT_MODEL,
        system_instruction=system_prompt,
    )
    generation_config = GenerationConfig(
        response_mime_type="application/json",
        temperature=0.2,    # low temperature for numerical extrapolation
    )

    def _sync_call():
        return model.generate_content(
            [Content(role="user", parts=[Part.from_text(user_message)])],
            generation_config=generation_config,
        )

    try:
        response = await asyncio.to_thread(_sync_call)
        raw_text = response.candidates[0].content.parts[0].text
        predictions = json.loads(raw_text)
        if isinstance(predictions, list):
            return predictions
        if isinstance(predictions, dict) and "predictions" in predictions:
            return predictions["predictions"]
        return []
    except Exception as exc:
        logger.warning(f"Gemini prediction call failed: {exc}")
        return []


# ── Prediction persistence ─────────────────────────────────────────────────

def _safe_action(raw: str | None) -> RemediationAction | None:
    if not raw:
        return None
    try:
        return RemediationAction(raw)
    except ValueError:
        return None


async def _persist_prediction(raw: dict, raw_metrics: dict) -> str | None:
    """
    Validate and store one prediction. Returns the prediction_id or None if skipped.
    """
    try:
        confidence = float(raw.get("confidence", 0))
        if confidence < MIN_CONFIDENCE_TO_STORE:
            return None

        service = raw.get("service", "").strip()
        if not service:
            return None

        breach_minutes = int(raw.get("predicted_breach_in_minutes", 10))
        breach_minutes = max(5, min(15, breach_minutes))

        now = datetime.now(timezone.utc)
        prediction_id = f"pred_{service}_{now.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"

        data = {
            "prediction_id": prediction_id,
            "service": service,
            "created_at": now,
            "expires_at": now + timedelta(minutes=PREDICTION_TTL_MINUTES),
            "predicted_breach_in_minutes": breach_minutes,
            "confidence": confidence,
            "trend_description": raw.get("trend_description", ""),
            "leading_indicator_metrics": raw.get("leading_indicator_metrics", []),
            "recommended_preemptive_action": raw.get("recommended_preemptive_action"),
            "prediction_validated": False,
            "prediction_false_positive": False,
            "materialized_incident_id": None,
            "raw_metrics": raw_metrics,
        }
        await firestore_client.create_prediction(data)
        logger.info(f"Prediction stored: {prediction_id} service={service} confidence={confidence:.2f}")
        return prediction_id
    except Exception as exc:
        logger.warning(f"Failed to persist prediction: {exc}")
        return None


async def _open_predictive_incident(raw: dict, prediction_id: str, raw_metrics: dict) -> None:
    """Open a PREDICTIVE status incident for high-confidence predictions."""
    service = raw.get("service", "unknown")
    confidence_pct = int(float(raw.get("confidence", 0)) * 100)
    breach_in = int(raw.get("predicted_breach_in_minutes", 10))
    action = raw.get("recommended_preemptive_action")

    # Use prediction_id as the incident ID so it is stable and linkable
    problem_id = f"predictive_{prediction_id}"

    existing = await firestore_client.get_incident(problem_id)
    if existing:
        return  # already opened

    incident_data = {
        "problem_id": problem_id,
        "status": "PREDICTIVE",
        "severity": "PERFORMANCE",
        "title": f"[FORECAST] {service} — SLO breach predicted in {breach_in} min ({confidence_pct}% confidence)",
        "service": service,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "trace": [{
            "step": 0,
            "thought": (
                f"Predictive analysis detected a high-confidence SLO breach trend.\n\n"
                f"{raw.get('trend_description', '')}\n\n"
                f"Leading indicators: {', '.join(raw.get('leading_indicator_metrics', []))}"
            ),
            "tool_call": None,
            "tool_result": {"raw_metrics_snapshot": raw_metrics},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": "gcp",
        }],
        "plan": {
            "action": action or "no_action_needed",
            "service": service,
            "reason": raw.get("trend_description", ""),
            "confidence": float(raw.get("confidence", 0)),
            "rollback_safe": True,
            "rollback_safety": "reversible",
            "requires_explicit_confirmation": False,
            "estimated_impact": (
                f"Preemptive action before the SLO breach window (~{breach_in} min). "
                "If not acted upon, Dynatrace will open a reactive incident."
            ),
            "estimated_hourly_cost_delta_usd": 0.0,
            "traffic_context": "normal",
            "cost_optimized_alternative": None,
        } if action and action != "no_action_needed" else None,
        "postmortem": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": str(uuid.uuid4()),
        "providers_used": ["gcp", "dynatrace"],
        "linked_prediction_id": prediction_id,
        "prediction_validated": False,
    }
    await firestore_client.create_incident(incident_data)
    logger.info(
        f"Opened PREDICTIVE incident {problem_id} for {service} "
        f"(confidence={confidence_pct}%, action={action})"
    )


# ── False-positive sweep ───────────────────────────────────────────────────

async def _sweep_expired_predictions() -> None:
    """Mark predictions whose window closed without a Dynatrace confirmation."""
    expired = await firestore_client.list_expired_untagged_predictions()
    for pred in expired:
        pid = pred.get("prediction_id", "")
        if pid:
            await firestore_client.mark_prediction_false_positive(pid)
            logger.debug(f"Marked false positive: {pid}")


# ── Main loop ──────────────────────────────────────────────────────────────

async def predictive_loop() -> None:
    """
    Runs every PREDICT_INTERVAL_SECONDS (5 min).
    Discovers services → collects metrics → asks Gemini → persists predictions.
    """
    logger.info("Predictive loop started")

    while True:
        try:
            # 1. Expire old predictions first
            await _sweep_expired_predictions()

            # 2. Discover services to analyse
            services = await _discover_services()
            if not services:
                logger.debug("No services to analyse; skipping prediction cycle")
                await asyncio.sleep(PREDICT_INTERVAL_SECONDS)
                continue

            logger.info(f"Prediction cycle: analysing {services}")

            # 3. Collect metrics for all services in parallel
            metric_snapshots = await asyncio.gather(
                *[_collect_metrics_for_service(svc) for svc in services],
                return_exceptions=True,
            )
            valid_snapshots = [
                m for m in metric_snapshots if isinstance(m, dict)
            ]

            if not valid_snapshots:
                await asyncio.sleep(PREDICT_INTERVAL_SECONDS)
                continue

            # 4. Run Gemini prediction
            raw_predictions = await _run_prediction(valid_snapshots)

            # Build a quick lookup: service → its metric snapshot
            metrics_by_service = {m["service"]: m for m in valid_snapshots}

            # 5. Persist qualifying predictions and open incidents
            for raw in raw_predictions:
                service = raw.get("service", "")
                raw_metrics = metrics_by_service.get(service, {})
                prediction_id = await _persist_prediction(raw, raw_metrics)
                if prediction_id is None:
                    continue

                confidence = float(raw.get("confidence", 0))
                if confidence >= MIN_CONFIDENCE_TO_INCIDENT:
                    await _open_predictive_incident(raw, prediction_id, raw_metrics)

        except Exception:
            logger.exception("Predictive loop error")

        await asyncio.sleep(PREDICT_INTERVAL_SECONDS)
