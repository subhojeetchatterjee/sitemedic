import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud import firestore

_db: Optional[firestore.AsyncClient] = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient(project=os.environ["GCP_PROJECT_ID"])
    return _db


def _incidents():
    return _client().collection("incidents")


def _predictions():
    return _client().collection("predictions")


async def get_incident(problem_id: str) -> Optional[dict]:
    doc = await _incidents().document(problem_id).get()
    return doc.to_dict() if doc.exists else None


async def create_incident(data: dict) -> None:
    await _incidents().document(data["problem_id"]).set(data)
    try:
        from demo_mode.recorder import get_recorder
        rec = get_recorder()
        if rec is not None:
            rec.record_incident_event(data["problem_id"], "created", {
                "severity": data.get("severity"), "title": data.get("title"),
                "service": data.get("service"), "detection_method": data.get("detection_method"),
            })
            rec.record_firestore_write("incidents", data["problem_id"], "set", data)
    except Exception:
        pass


async def set_status(problem_id: str, status: str) -> None:
    await _incidents().document(problem_id).update(
        {"status": status, "updated_at": datetime.utcnow()}
    )
    try:
        from demo_mode.recorder import get_recorder
        rec = get_recorder()
        if rec is not None:
            rec.record_incident_event(problem_id, "status_change", {"status": status})
    except Exception:
        pass


async def append_trace(problem_id: str, step: dict) -> None:
    await _incidents().document(problem_id).update(
        {
            "trace": firestore.ArrayUnion([step]),
            "updated_at": datetime.utcnow(),
        }
    )


async def set_plan(problem_id: str, plan: dict) -> None:
    await _incidents().document(problem_id).update(
        {"plan": plan, "updated_at": datetime.utcnow()}
    )
    try:
        from demo_mode.recorder import get_recorder
        rec = get_recorder()
        if rec is not None:
            rec.record_incident_event(problem_id, "plan_set", {"plan": plan})
    except Exception:
        pass


async def get_plan(problem_id: str) -> Optional[dict]:
    doc = await _incidents().document(problem_id).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("plan")


async def record_provider(problem_id: str, provider: str) -> None:
    """Add a provider tag to the incident's providers_used list (deduplicates)."""
    await _incidents().document(problem_id).update(
        {
            "providers_used": firestore.ArrayUnion([provider]),
            "updated_at": datetime.utcnow(),
        }
    )


async def set_correlation_id(problem_id: str, correlation_id: str) -> None:
    await _incidents().document(problem_id).update(
        {"correlation_id": correlation_id, "updated_at": datetime.utcnow()}
    )


async def set_postmortem(problem_id: str, text: str) -> None:
    await _incidents().document(problem_id).update(
        {"postmortem": text, "status": "RESOLVED", "updated_at": datetime.utcnow()}
    )
    try:
        from demo_mode.recorder import get_recorder
        rec = get_recorder()
        if rec is not None:
            rec.record_incident_event(problem_id, "postmortem", {"postmortem_length": len(text)})
    except Exception:
        pass
    try:
        from demo_mode.replay_source import get_demo_source
        src = get_demo_source()
        if src is not None:
            src.resolve_by_problem_id(problem_id)
    except Exception:
        pass


async def list_incidents(limit: int = 50) -> list[dict]:
    docs = (
        _incidents()
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


# ── Predictions CRUD ───────────────────────────────────────────────────────

async def create_prediction(data: dict) -> None:
    await _predictions().document(data["prediction_id"]).set(data)


async def get_prediction(prediction_id: str) -> Optional[dict]:
    doc = await _predictions().document(prediction_id).get()
    return doc.to_dict() if doc.exists else None


async def list_active_predictions(limit: int = 50) -> list[dict]:
    """Predictions that haven't expired and haven't been tagged (for the live feed)."""
    now = datetime.now(timezone.utc)
    docs = (
        _predictions()
        .where("expires_at", ">", now)
        .where("prediction_false_positive", "==", False)
        .order_by("expires_at", direction=firestore.Query.ASCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


async def find_active_prediction_for_service(service: str) -> Optional[dict]:
    """Find the most recent non-expired, non-validated prediction for a service."""
    now = datetime.now(timezone.utc)
    docs = (
        _predictions()
        .where("service", "==", service)
        .where("expires_at", ">", now)
        .where("prediction_validated", "==", False)
        .where("prediction_false_positive", "==", False)
        .order_by("expires_at", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    results = [doc.to_dict() async for doc in docs]
    return results[0] if results else None


async def validate_prediction(prediction_id: str, incident_id: str) -> None:
    """Tag a prediction as confirmed when Dynatrace fires a matching problem."""
    await _predictions().document(prediction_id).update({
        "prediction_validated": True,
        "materialized_incident_id": incident_id,
        "updated_at": datetime.now(timezone.utc),
    })


async def mark_prediction_false_positive(prediction_id: str) -> None:
    """Tag a prediction as a false positive once its window expires without a breach."""
    now = datetime.now(timezone.utc)
    pred_ref = _predictions().document(prediction_id)
    await pred_ref.update({
        "prediction_false_positive": True,
        "updated_at": now,
    })
    # Close any linked PREDICTIVE incident so it stops showing in the Forecasted tab
    pred_snap = await pred_ref.get()
    if pred_snap.exists:
        pred_data = pred_snap.to_dict() or {}
        linked_id = pred_data.get("materialized_incident_id")
        if linked_id:
            inc_ref = _incidents().document(linked_id)
            inc_snap = await inc_ref.get()
            if inc_snap.exists and (inc_snap.to_dict() or {}).get("status") == "PREDICTIVE":
                await inc_ref.update({"status": "REJECTED", "updated_at": now})


async def list_expired_untagged_predictions() -> list[dict]:
    """Predictions whose window closed but were never validated or marked FP."""
    now = datetime.now(timezone.utc)
    docs = (
        _predictions()
        .where("expires_at", "<=", now)
        .where("prediction_validated", "==", False)
        .where("prediction_false_positive", "==", False)
        .limit(100)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


async def link_incident_to_prediction(problem_id: str, prediction_id: str) -> None:
    """Annotate the incident record with the prediction that preceded it."""
    await _incidents().document(problem_id).update({
        "linked_prediction_id": prediction_id,
        "prediction_validated": True,
        "updated_at": datetime.utcnow(),
    })


# ── Incident Clusters CRUD ─────────────────────────────────────────────────

def _clusters():
    return _client().collection("incident_clusters")


async def create_cluster(data: dict) -> None:
    await _clusters().document(data["cluster_id"]).set(data)


async def get_cluster(cluster_id: str) -> Optional[dict]:
    doc = await _clusters().document(cluster_id).get()
    return doc.to_dict() if doc.exists else None


async def list_active_clusters(limit: int = 20) -> list[dict]:
    """Return clusters not yet complete or failed, newest first."""
    docs = (
        _clusters()
        .where("status", "in", ["FORMING", "AWAITING_APPROVAL", "EXECUTING", "PARTIAL"])
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


async def add_incidents_to_cluster(cluster_id: str, incident_ids: list[str]) -> None:
    await _clusters().document(cluster_id).update({
        "member_incident_ids": firestore.ArrayUnion(incident_ids),
        "updated_at": datetime.now(timezone.utc),
    })


async def set_cluster_status(cluster_id: str, status: str) -> None:
    await _clusters().document(cluster_id).update({
        "status": status,
        "updated_at": datetime.now(timezone.utc),
    })


async def update_cluster_step(cluster_id: str, step_index: int, status: str, result: Optional[Any] = None) -> None:
    """Update a single step's status (and optional result) inside coordinated_plan."""
    cluster = await get_cluster(cluster_id)
    if not cluster:
        return
    steps = cluster.get("coordinated_plan", [])
    for s in steps:
        if s.get("step_index") == step_index:
            s["status"] = status
            if result is not None:
                s["result"] = result
    await _clusters().document(cluster_id).update({
        "coordinated_plan": steps,
        "updated_at": datetime.now(timezone.utc),
    })


async def set_incident_cluster(problem_id: str, cluster_id: str) -> None:
    """Tag an incident as belonging to a cluster."""
    await _incidents().document(problem_id).update({
        "cluster_id": cluster_id,
        "updated_at": datetime.utcnow(),
    })


async def get_incidents_by_ids(ids: list[str]) -> list[dict]:
    """Fetch multiple incidents in parallel; silently drops missing ones."""
    results = await asyncio.gather(*[get_incident(i) for i in ids])
    return [r for r in results if r is not None]


# ── Audit events ───────────────────────────────────────────────────────────

def _audit_events():
    return _client().collection("audit_events")


def _audit_meta():
    """Stores the running chain state (last_hash + last_seq)."""
    return _client().collection("audit_meta")


async def create_audit_event(data: dict, timestamp: datetime) -> None:
    """
    Write one audit event document.
    `expires_at` is stored as a Python datetime so Firestore TTL policy can
    index it as a Firestore Timestamp. All other fields are stored as-is
    (ISO strings, strings, dicts) to ensure hash-chain verification reads
    back the same serialised form.
    """
    doc = dict(data)
    doc["expires_at"] = timestamp + timedelta(days=90)
    await _audit_events().document(data["event_id"]).set(doc)


async def list_audit_events(
    limit: int = 200,
    incident_id: Optional[str] = None,
    actor: Optional[str] = None,
    action_type: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[dict]:
    """
    Query audit events with optional filters, newest-first.
    Filtered queries require Firestore composite indexes:
      audit_events: (incident_id ASC, seq DESC)
      audit_events: (actor ASC, seq DESC)
      audit_events: (action_type ASC, seq DESC)
    Create them in Firebase Console or firestore.indexes.json.
    """
    q = _audit_events().order_by("seq", direction=firestore.Query.DESCENDING)
    if incident_id:
        q = q.where("incident_id", "==", incident_id)
    if actor:
        q = q.where("actor", "==", actor)
    if action_type:
        q = q.where("action_type", "==", action_type)
    if since:
        q = q.where("timestamp", ">=", since.isoformat() if isinstance(since, datetime) else since)
    if until:
        q = q.where("timestamp", "<=", until.isoformat() if isinstance(until, datetime) else until)
    q = q.limit(limit)
    try:
        return [doc.to_dict() async for doc in q.stream()]
    except Exception as exc:
        # Likely a missing composite index — return empty list with error context
        import logging
        logging.getLogger(__name__).warning("audit list_audit_events query failed: %s", exc)
        return []


async def list_audit_events_for_verify(limit: int = 1000) -> list[dict]:
    """Return events in insertion order (ascending seq) for chain verification."""
    docs = (
        _audit_events()
        .order_by("seq", direction=firestore.Query.ASCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


async def get_audit_chain_state() -> Optional[dict]:
    doc = await _audit_meta().document("chain_state").get()
    return doc.to_dict() if doc.exists else None


async def set_audit_chain_state(last_hash: str, last_seq: int) -> None:
    await _audit_meta().document("chain_state").set(
        {"last_hash": last_hash, "last_seq": last_seq, "updated_at": datetime.now(timezone.utc)}
    )


# ── Analytics snapshots ────────────────────────────────────────────────────

def _analytics_snapshots():
    return _client().collection("analytics_snapshots")


async def get_analytics_snapshot(window: str) -> Optional[dict]:
    doc = await _analytics_snapshots().document(window).get()
    return doc.to_dict() if doc.exists else None


async def set_analytics_snapshot(window: str, data: dict) -> None:
    await _analytics_snapshots().document(window).set(data)


async def list_incidents_for_analytics(window_days: int) -> list[dict]:
    """
    Fetch up to 2000 most-recent incidents for analytics aggregation.
    Python-side date filtering is applied by analytics.py.
    Ordered by started_at DESC so we get the most relevant incidents first.
    """
    docs = (
        _incidents()
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(2000)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


async def list_predictions_for_analytics(window_days: int) -> list[dict]:
    """Fetch up to 2000 most-recent predictions for analytics aggregation."""
    docs = (
        _predictions()
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(2000)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


# ── Diagnosis ─────────────────────────────────────────────────────────────

async def set_diagnosis(problem_id: str, diagnosis: dict) -> None:
    await _incidents().document(problem_id).update(
        {"diagnosis": diagnosis, "updated_at": datetime.utcnow()}
    )


async def set_competing_diagnosis(problem_id: str, data: dict) -> None:
    await _incidents().document(problem_id).update(
        {"competing_diagnosis": data, "updated_at": datetime.utcnow()}
    )


async def set_confidence_blocked(problem_id: str, blocked: bool) -> None:
    await _incidents().document(problem_id).update(
        {"confidence_blocked": blocked, "updated_at": datetime.utcnow()}
    )


async def list_incidents_with_diagnosis(limit: int = 100) -> list[dict]:
    """Incidents that have a structured diagnosis, newest-first. Used for calibration."""
    docs = (
        _incidents()
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [d.to_dict() async for d in docs if d.to_dict().get("diagnosis")]


# ── Calibration snapshots ─────────────────────────────────────────────────

def _calibration_snapshots():
    return _client().collection("calibration_snapshots")


async def get_calibration_snapshot() -> Optional[dict]:
    doc = await _calibration_snapshots().document("latest").get()
    return doc.to_dict() if doc.exists else None


async def set_calibration_snapshot(data: dict) -> None:
    await _calibration_snapshots().document("latest").set(data)


# ── Cost settings ─────────────────────────────────────────────────────────

def _cost_settings():
    return _client().collection("cost_settings")


async def get_cost_settings(service: str) -> Optional[dict]:
    """Return cost settings for *service*, or None if not configured."""
    doc = await _cost_settings().document(service).get()
    return doc.to_dict() if doc.exists else None


async def set_cost_settings(service: str, data: dict) -> None:
    await _cost_settings().document(service).set(
        {**data, "service": service, "updated_at": datetime.now(timezone.utc)}
    )


async def list_cost_settings() -> list[dict]:
    docs = _cost_settings().stream()
    return [doc.to_dict() async for doc in docs]


# ── Dry-run report cache (5-minute TTL) ──────────────────────────────────

def _dry_run_cache():
    return _client().collection("dry_run_cache")


async def get_dry_run_report(problem_id: str) -> Optional[dict]:
    """Return cached dry-run report if still within 5-minute TTL."""
    doc = await _dry_run_cache().document(problem_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    expires_at = data.get("expires_at")
    if expires_at:
        exp = expires_at if isinstance(expires_at, datetime) else None
        if exp and exp.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            return None
    return data.get("report")


async def set_dry_run_report(problem_id: str, report: dict) -> None:
    await _dry_run_cache().document(problem_id).set({
        "report": report,
        "computed_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    })


# ── Global settings ────────────────────────────────────────────────────────

def _global_settings():
    return _client().collection("global_settings")


async def get_global_settings() -> dict:
    doc = await _global_settings().document("config").get()
    return doc.to_dict() if doc.exists else {}


async def set_global_settings(data: dict) -> None:
    await _global_settings().document("config").set(
        {**data, "updated_at": datetime.now(timezone.utc)}
    )


# ── Webhook failures (dead-letter queue) ──────────────────────────────────

def _webhook_failures():
    return _client().collection("webhook_failures")


async def create_webhook_failure(data: dict) -> None:
    """Write a webhook processing failure to the dead-letter collection."""
    import uuid as _uuid
    doc_id = data.get("problem_id") or str(_uuid.uuid4())
    existing_doc = await _webhook_failures().document(doc_id).get()
    if existing_doc.exists:
        existing = existing_doc.to_dict()
        retry_count = existing.get("retry_count", 0) + 1
        await _webhook_failures().document(doc_id).update({
            "retry_count": retry_count,
            "last_error": data.get("error"),
            "last_attempt_at": datetime.now(timezone.utc),
            "dead_lettered": retry_count >= 3,
        })
    else:
        await _webhook_failures().document(doc_id).set({
            **data,
            "retry_count": 1,
            "dead_lettered": False,
            "created_at": datetime.now(timezone.utc),
            "last_attempt_at": datetime.now(timezone.utc),
        })


async def get_webhook_failure_count(problem_id: str) -> int:
    """Return the current retry count for a given problem_id."""
    doc = await _webhook_failures().document(problem_id).get()
    if not doc.exists:
        return 0
    return doc.to_dict().get("retry_count", 0)


async def list_webhook_failures(limit: int = 50) -> list[dict]:
    """Return recent webhook failures, newest first."""
    docs = (
        _webhook_failures()
        .order_by("last_attempt_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]


# ── Webhook health checks ─────────────────────────────────────────────────

def _webhook_health():
    return _client().collection("webhook_health")


async def create_webhook_health_check(data: dict) -> None:
    """Store one health-probe result, keyed by probe_id."""
    probe_id = data.get("probe_id", "unknown")
    await _webhook_health().document(probe_id).set({
        **data,
        "stored_at": datetime.now(timezone.utc),
    })


async def list_webhook_health_checks(limit: int = 20) -> list[dict]:
    """Return recent health-probe results, newest first."""
    docs = (
        _webhook_health()
        .order_by("stored_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() async for doc in docs]
