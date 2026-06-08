"""
Immutable, queryable audit trail for all SiteMedic agent actions.

Dual-writes to Firestore `audit_events` (queryable, 90-day TTL) and
Google Cloud Logging (immutable, retained per project policy).

Hash chain: SHA-256(previous_hash + json(event_without_hash_chain))
makes retroactive edits detectable — any tampered Firestore document
breaks the chain at that point and is flagged by verify_audit_chain().
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

AUDIT_TTL_DAYS = 90

# Redact field values that look like secrets.
# Matches: "api_key": "abc123...", token=xyz789, etc.
_SECRET_KEY_RE = re.compile(
    r'(?i)"?(api[_\-]?key|token|secret|password|credential|bearer|auth)"?\s*'
    r'[:=]\s*"?([A-Za-z0-9+/=\-_\.]{8,})"?'
)
_DT_TOKEN_RE = re.compile(r"dt0c01\.[A-Za-z0-9_\-]{10,}")
_GCP_KEY_RE = re.compile(r"AIza[0-9A-Za-z\-_]{35}")


# ── Schema ─────────────────────────────────────────────────────────────────

class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: Literal["agent", "operator", "system"]
    actor_identity: str          # service-account email or "operator@sitemedic"
    action_type: str             # see ACTION_TYPES below
    resource: Optional[str] = None   # affected GCP resource path
    incident_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    result: Literal["success", "failure", "partial"]
    hash_chain: str = ""         # filled by worker before write


# Canonical action_type constants kept here so callers never typo them.
class ActionType:
    DETECT_CYCLE       = "detect_cycle"
    INCIDENT_CREATED   = "incident_created"
    GEMINI_CALL        = "gemini_call"
    MCP_TOOL_CALL      = "mcp_tool_call"
    GCP_TOOL_CALL      = "gcp_tool_call"
    PLAN_GENERATED     = "plan_generated"
    APPROVED           = "approved"
    REJECTED           = "rejected"
    EXECUTED           = "executed"
    POSTMORTEM         = "postmortem_generated"
    PREDICTION_CYCLE = "prediction_cycle"
    PREDICTION_GEMINI_CALL = "prediction_gemini_call"
    PREDICTION_STORED = "prediction_stored"
    CLUSTER_FORMED     = "cluster_formed"
    CLUSTER_EXECUTED   = "cluster_executed"
    CHAIN_VERIFIED     = "chain_verified"
    AUDIT_FAILURE      = "audit_failure"
    WEBHOOK_RECEIVED   = "webhook_received"
    WEBHOOK_FAILURE    = "webhook_failure"
    WEBHOOK_HEALTH_CHECK = "webhook_health_check"
    MAINTENANCE        = "maintenance"


# ── Background queue and worker state ─────────────────────────────────────

_audit_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
_worker_task: Optional[asyncio.Task] = None
_last_hash: str = "genesis"
_seq: int = 0
_agent_identity: str = ""


def _agent_id() -> str:
    global _agent_identity
    if not _agent_identity:
        _agent_identity = os.environ.get(
            "AGENT_SERVICE_ACCOUNT",
            "sitemedic-agent@unknown.iam.gserviceaccount.com",
        )
    return _agent_identity


# ── PII / secret redaction ─────────────────────────────────────────────────

def _redact(payload: dict) -> dict:
    """Redact token/key-like values from the payload dict."""
    text = json.dumps(payload, default=str)
    text = _SECRET_KEY_RE.sub(lambda m: f'"{m.group(1)}": "***REDACTED***"', text)
    text = _DT_TOKEN_RE.sub("dt0c01.***REDACTED***", text)
    text = _GCP_KEY_RE.sub("AIza***REDACTED***", text)
    try:
        return json.loads(text)
    except Exception:
        return {"__redacted": True}


# ── Hash chain ─────────────────────────────────────────────────────────────

def _serialise_for_hash(event: AuditEvent) -> str:
    """Deterministic JSON of all event fields except hash_chain."""
    # mode="json" converts datetime → ISO string consistently
    d = event.model_dump(exclude={"hash_chain"}, mode="json")
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def _compute_hash(previous_hash: str, event: AuditEvent) -> str:
    data = (previous_hash + _serialise_for_hash(event)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ── Cloud Logging singleton ────────────────────────────────────────────────

_cl_client: Any = None
_cl_logger: Any = None


def _cloud_logger() -> Any:
    global _cl_client, _cl_logger
    if _cl_logger is None:
        import google.cloud.logging as gcl
        _cl_client = gcl.Client(project=os.environ.get("GCP_PROJECT_ID"))
        _cl_logger = _cl_client.logger("sitemedic-audit")
    return _cl_logger


def _emit_cloud_logging(payload: dict, severity: str = "INFO") -> None:
    try:
        _cloud_logger().log_struct(payload, severity=severity)
    except Exception as exc:
        logger.warning("Cloud Logging emit failed: %s", exc)


# ── Event processing ───────────────────────────────────────────────────────

async def _process_event(event: AuditEvent, seq: int) -> None:
    global _last_hash

    event.payload = _redact(event.payload)
    event.hash_chain = _compute_hash(_last_hash, event)
    _last_hash = event.hash_chain

    # Build the Firestore document (seq added *after* hash computation)
    event_dict = event.model_dump(mode="json")
    event_dict["seq"] = seq

    # Firestore write
    fs_ok = False
    try:
        from tools import firestore_client
        await firestore_client.create_audit_event(event_dict, event.timestamp)
        fs_ok = True
    except Exception as exc:
        logger.exception("Audit Firestore write failed for %s", event.event_id)
        _emit_cloud_logging(
            {
                "audit_failure": True,
                "failed_event_id": event.event_id,
                "action_type": event.action_type,
                "error": str(exc),
            },
            severity="ERROR",
        )

    # Cloud Logging write — always attempted, even if Firestore failed
    _emit_cloud_logging(event_dict)

    # Persist chain state so the chain can resume across restarts
    if fs_ok:
        try:
            from tools import firestore_client
            await firestore_client.set_audit_chain_state(event.hash_chain, seq)
        except Exception:
            pass  # non-critical; chain resumes from last stored state


async def _audit_worker() -> None:
    """Drains the audit queue serially to maintain a consistent hash chain."""
    global _last_hash, _seq
    try:
        from tools import firestore_client
        state = await firestore_client.get_audit_chain_state()
        if state:
            _last_hash = state.get("last_hash", "genesis")
            _seq = state.get("last_seq", 0)
    except Exception:
        pass
    logger.info("Audit worker started (seq=%d)", _seq)

    while True:
        event = await _audit_queue.get()
        try:
            _seq += 1
            await _process_event(event, _seq)
        except Exception:
            logger.exception("Audit worker: unhandled error for %s", event.event_id)
        finally:
            _audit_queue.task_done()


# ── Public API ─────────────────────────────────────────────────────────────

def start_audit_worker() -> None:
    """Start the background audit worker. Call once from the app lifespan."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_audit_worker())


def log_audit_event(event: AuditEvent) -> None:
    """
    Non-blocking fire-and-forget. Puts the event on the background queue.
    The main action is never delayed — this returns immediately.
    Never raises; drops the event and logs an error if the queue is full.
    """
    try:
        _audit_queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.error(
            "Audit queue full; dropping %s event %s",
            event.action_type,
            event.event_id,
        )


async def verify_audit_chain(limit: int = 1000) -> dict:
    """
    Recompute the hash chain from stored Firestore events.
    Any retroactive document edit produces an expected != stored hash mismatch.
    Returns {"valid": bool, "checked": int, "tampered_at": str | None}.
    """
    from tools import firestore_client

    events = await firestore_client.list_audit_events_for_verify(limit=limit)
    if not events:
        return {"valid": True, "checked": 0, "tampered_at": None}

    prev = "genesis"
    for i, ev in enumerate(events):
        stored_hash = ev.get("hash_chain", "")
        # Reconstruct the exact input that was hashed on write:
        # all fields except hash_chain, seq (added after hash), and expires_at (TTL field).
        ev_copy = {
            k: v
            for k, v in ev.items()
            if k not in ("hash_chain", "seq", "expires_at")
        }
        # Normalise any datetime objects Firestore may return back to ISO strings
        for k, v in ev_copy.items():
            if hasattr(v, "isoformat"):
                ev_copy[k] = v.isoformat()
        candidate = (
            prev
            + json.dumps(ev_copy, sort_keys=True, ensure_ascii=False, default=str)
        ).encode("utf-8")
        expected = hashlib.sha256(candidate).hexdigest()

        if expected != stored_hash:
            return {
                "valid": False,
                "checked": i + 1,
                "tampered_at": ev.get("event_id"),
            }
        prev = stored_hash

    return {"valid": True, "checked": len(events), "tampered_at": None}


def prompt_hash(text: str) -> str:
    """Return a short SHA-256 prefix of a prompt string for privacy-safe logging."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
