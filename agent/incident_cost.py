"""
Live cost tracking for active incidents.

Three figures are computed every polling cycle:
  burn_rate_per_min  — estimated revenue loss per minute of degraded service
  cumulative_usd     — total estimated loss since incident start
  break_even_minutes — at burn_rate, how many minutes until incident cost
                       exceeds the plan's remediation overhead

Revenue model:
  affected_rps = current_rps × error_rate
  burn_rate    = affected_rps × 60 × revenue_per_request × criticality_multiplier

Settings are stored in Firestore `cost_settings/{service}` (falls back to
`cost_settings/default`). If no settings exist, burn_rate is omitted and the
UI shows a "Configure revenue impact" CTA.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.cloud import monitoring_v3
from tools import firestore_client

logger = logging.getLogger(__name__)

_DEFAULT_ERROR_RATE = 0.5  # conservative fallback when monitoring unavailable


def _parse_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None
    if hasattr(value, "timestamp"):
        return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)
    return None


async def _get_error_rate(service: str) -> float:
    """
    Fraction of 5xx responses in the last 5 minutes from Cloud Monitoring.
    Falls back to _DEFAULT_ERROR_RATE if the metric is unavailable.
    """
    def _sync() -> float:
        try:
            client = monitoring_v3.MetricServiceClient()
            project_name = f"projects/{os.environ['GCP_PROJECT_ID']}"
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=5)
            interval = monitoring_v3.TimeInterval(
                end_time={"seconds": int(now.timestamp())},
                start_time={"seconds": int(start.timestamp())},
            )

            def _sum(extra: str = "") -> float:
                req = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=(
                        f'metric.type="run.googleapis.com/request_count"'
                        f' AND resource.label.service_name="{service}"'
                        + (f" AND {extra}" if extra else "")
                    ),
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                )
                total = 0.0
                for ts in client.list_time_series(request=req):
                    for pt in ts.points:
                        total += pt.value.int64_value or pt.value.double_value
                return total

            total = _sum()
            if total == 0:
                return _DEFAULT_ERROR_RATE
            errors = _sum('metric.label.response_code_class="5xx"')
            return min(errors / total, 1.0)
        except Exception as exc:
            logger.debug(f"Error rate query failed for {service}: {exc}")
            return _DEFAULT_ERROR_RATE

    return await asyncio.to_thread(_sync)


async def compute_cost_snapshot(problem_id: str) -> dict:
    """
    Compute a live cost snapshot. Called every 10 s by the SSE endpoint.
    Returns a JSON-serializable dict.
    """
    incident = await firestore_client.get_incident(problem_id)
    if not incident:
        return {"error": "incident not found", "problem_id": problem_id}

    service = incident.get("service", "")
    status = incident.get("status", "UNKNOWN")
    resolved = status in ("RESOLVED", "REJECTED")

    # Revenue settings: per-service, then default fallback
    settings = await firestore_client.get_cost_settings(service)
    if not settings:
        settings = await firestore_client.get_cost_settings("default")

    revenue_per_request: Optional[float] = None
    criticality = 1.0
    if settings:
        v = settings.get("revenue_per_request_usd")
        if v is not None:
            revenue_per_request = float(v)
        criticality = float(settings.get("criticality_multiplier", 1.0))

    # Traffic + error rate (skip expensive monitoring calls once resolved)
    if resolved:
        current_rps = 0.0
        error_rate = 0.0
    else:
        from tools.cost_estimator import get_current_traffic_pattern
        traffic = await get_current_traffic_pattern(service)
        current_rps = float(traffic.get("current_rps", 0.0))
        error_rate = await _get_error_rate(service)

    # Burn rate
    burn_rate_per_min: Optional[float] = None
    if revenue_per_request is not None and not resolved:
        affected_rps = current_rps * error_rate
        burn_rate_per_min = round(affected_rps * 60.0 * revenue_per_request * criticality, 4)

    # Duration and cumulative
    started_dt = _parse_dt(incident.get("started_at"))
    now = datetime.now(timezone.utc)
    duration_min = (now - started_dt).total_seconds() / 60.0 if started_dt else 0.0

    cumulative_usd: Optional[float] = None
    if burn_rate_per_min is not None:
        cumulative_usd = round(burn_rate_per_min * duration_min, 2)

    # Remediation cost from plan
    plan = incident.get("plan") or {}
    remediation_hourly = float(plan.get("estimated_hourly_cost_delta_usd") or 0.0)

    # Break-even: when does incident burn = 1 hour of remediation overhead?
    # break_even_min = (remediation_hourly / 60) / burn_rate_per_min
    break_even_minutes: Optional[float] = None
    if burn_rate_per_min and burn_rate_per_min > 0 and remediation_hourly > 0:
        break_even_minutes = round((remediation_hourly / 60.0) / burn_rate_per_min, 1)

    return {
        "problem_id": problem_id,
        "service": service,
        "status": status,
        "resolved": resolved,
        "burn_rate_per_min": burn_rate_per_min,
        "cumulative_usd": cumulative_usd,
        "remediation_hourly_usd": round(remediation_hourly, 4),
        "break_even_minutes": break_even_minutes,
        "current_rps": round(current_rps, 2),
        "error_rate": round(error_rate, 3),
        "duration_minutes": round(duration_min, 2),
        "revenue_configured": revenue_per_request is not None,
        "criticality_multiplier": criticality,
        "sampled_at": now.isoformat(),
    }
