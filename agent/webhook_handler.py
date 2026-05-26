"""
Dynatrace webhook handler.

Responsibilities:
  - Load shared secret from Secret Manager (env var fallback for local dev)
  - Validate HMAC-SHA256 signature in constant time
  - Normalise Dynatrace problem-notification payload to internal incident fields
"""
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

from secrets import Secrets

logger = logging.getLogger(__name__)


def _get_webhook_secret() -> str:
    """
    Load Dynatrace webhook signing secret.

    Priority:
      1. Secret Manager (production, keyed by ENV suffix)
      2. DT_WEBHOOK_SECRET env var (demo mode / local dev)
      3. Empty string — signature validation will reject the request
    """
    env_name = os.environ.get("ENV", "dev")
    secret_name = f"dynatrace-webhook-secret-{env_name}"

    try:
        return Secrets.get(secret_name)
    except RuntimeError:
        # Secret Manager unavailable or secret missing — fall back to env var
        fallback = os.environ.get("DT_WEBHOOK_SECRET", "")
        if fallback:
            logger.debug("Using DT_WEBHOOK_SECRET env var as webhook secret fallback")
        else:
            logger.warning("Webhook secret not available — all webhook signatures will be rejected")
        return fallback


def validate_signature(body: bytes, signature_header: str) -> bool:
    """
    Validate the X-Hub-Signature-256 header produced by Dynatrace.
    Uses constant-time comparison to prevent timing attacks.

    Expected header format: sha256=<hex_digest>
    """
    if not signature_header:
        logger.warning("Webhook request missing X-Hub-Signature-256 header")
        return False

    secret = _get_webhook_secret()
    if not secret:
        logger.error("Webhook secret not configured — rejecting webhook")
        return False

    expected = (
        "sha256="
        + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


def parse_dynatrace_payload(raw: dict) -> dict:
    """
    Normalise a Dynatrace problem-notification webhook payload to our internal
    incident representation. Handles the uppercase field names Dynatrace uses by
    default as well as camelCase alternatives.

    Returns a dict with keys:
        problem_id, state, title, severity, service, started_at, raw_payload
    """
    # Problem ID — Dynatrace uses "P-XXXXXX" format.
    problem_id = (
        raw.get("ProblemID")
        or raw.get("problemId")
        or raw.get("id")
        or ""
    )

    state = (
        raw.get("State") or raw.get("state") or "OPEN"
    ).upper()

    title = (
        raw.get("ProblemTitle")
        or raw.get("title")
        or "Unknown incident"
    )

    severity = (
        raw.get("Severity")
        or raw.get("severityLevel")
        or raw.get("severity")
        or "UNKNOWN"
    ).upper()

    # Extract the first impacted entity name as the service identifier.
    entities = (
        raw.get("ImpactedEntities")
        or raw.get("impactedEntities")
        or []
    )
    service = "unknown"
    if isinstance(entities, list) and entities:
        first = entities[0]
        service = (
            first.get("name")
            or first.get("entityId")
            or "unknown"
        )
    elif isinstance(entities, dict):
        service = entities.get("name") or "unknown"

    # Timestamp — Dynatrace sends milliseconds since epoch.
    ts_raw = raw.get("Timestamp") or raw.get("timestamp")
    if ts_raw:
        try:
            started_at = datetime.fromtimestamp(int(ts_raw) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            started_at = datetime.now(timezone.utc)
    else:
        started_at = datetime.now(timezone.utc)

    return {
        "problem_id": problem_id,
        "state": state,
        "title": title,
        "severity": severity,
        "service": service,
        "started_at": started_at,
        "raw_payload": raw,
    }
