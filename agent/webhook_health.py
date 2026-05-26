"""
Webhook health check loop.

Every 15 minutes:
  1. Sends a signed synthetic probe to the agent's own webhook endpoint.
  2. Measures end-to-end response latency.
  3. Stores results in the `webhook_health` Firestore collection.
  4. Logs a warning (and audit event) if p95 latency target (500 ms) is breached.

The probe payload includes `_health_probe: true` — the webhook endpoint treats
it as a no-op (no incident created, no diagnosis spawned) and returns 200 OK.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import httpx

from audit import AuditEvent, log_audit_event, _agent_id
from tools import firestore_client
from environment import Environment

logger = logging.getLogger(__name__)

# Health check interval configured per environment
env = Environment.get_instance()
HEALTH_CHECK_INTERVAL_SECONDS = env.get("detection.webhook_enabled", True) and (15 * 60) or (60 * 60)  # 15 min if webhooks enabled, 1 hour otherwise
SLA_P95_MS = 500  # latency target

_PROBE_TIMEOUT_SECONDS = 10.0


def _sign_probe(body: bytes) -> str:
    """Return sha256=<hmac> using the same secret resolution as validate_signature."""
    from webhook_handler import _get_webhook_secret
    secret = _get_webhook_secret() or "health-check-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _run_probe() -> dict:
    """Send a synthetic webhook probe and return a health record."""
    probe_id = str(uuid.uuid4())
    payload = {
        "State": "OPEN",
        "ProblemID": f"PROBE-{probe_id[:8]}",
        "ProblemTitle": "SiteMedic webhook health probe",
        "ImpactedEntities": [{"name": "health-check", "entityId": "PROBE"}],
        "Severity": "INFO",
        "Timestamp": int(time.time() * 1000),
        "_health_probe": True,
        "_probe_id": probe_id,
    }
    body = json.dumps(payload).encode()
    signature = _sign_probe(body)

    agent_port = os.environ.get("AGENT_PORT", "8080")
    url = f"http://127.0.0.1:{agent_port}/api/webhooks/dynatrace"

    start_ms = time.monotonic() * 1000
    success = False
    status_code = 0
    error_msg = ""

    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
        status_code = resp.status_code
        success = resp.status_code == 200
    except Exception as exc:
        error_msg = str(exc)
        logger.warning("Webhook health probe failed: %s", exc)

    latency_ms = (time.monotonic() * 1000) - start_ms

    return {
        "probe_id": probe_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": round(latency_ms, 1),
        "success": success,
        "status_code": status_code,
        "error": error_msg or None,
        "sla_breach": latency_ms > SLA_P95_MS,
    }


async def webhook_health_loop() -> None:
    """Run health probes on a fixed interval."""
    logger.info("Webhook health check loop started (interval=%ds)", HEALTH_CHECK_INTERVAL_SECONDS)

    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

        try:
            record = await _run_probe()

            await firestore_client.create_webhook_health_check(record)

            result_str = "success" if record["success"] else "failure"
            log_audit_event(AuditEvent(
                actor="system",
                actor_identity=_agent_id(),
                action_type="webhook_health_check",
                payload={
                    "probe_id": record["probe_id"],
                    "latency_ms": record["latency_ms"],
                    "sla_breach": record["sla_breach"],
                    "status_code": record["status_code"],
                },
                result=result_str,
            ))

            if record["sla_breach"]:
                logger.warning(
                    "Webhook SLA breach: latency=%.1fms (target=%dms)",
                    record["latency_ms"],
                    SLA_P95_MS,
                )
            else:
                logger.info(
                    "Webhook health OK: latency=%.1fms",
                    record["latency_ms"],
                )

        except Exception:
            logger.exception("Webhook health check loop error")
