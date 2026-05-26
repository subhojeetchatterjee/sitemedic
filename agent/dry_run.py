"""
Dry-run executor for SiteMedic remediation actions.

CONTRACT: This module NEVER makes state-changing API calls.
All operations are read-only (describe, get, list) or purely local computation.

Each action returns a list of DryRunStep dicts describing:
  - The exact command/API call that WOULD run
  - Current (before) state fetched read-only from GCP
  - Predicted (after) state computed locally
  - Warnings for the operator
  - Reversibility classification
"""
import asyncio
import datetime
import json
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_FLASH_MODEL = "gemini-2.5-flash-preview-05-20"


def _project() -> str:
    return os.environ.get("GCP_PROJECT_ID", "unknown-project")


def _region() -> str:
    return os.environ.get("GCP_REGION", "us-central1")


def _is_demo_mode() -> bool:
    return os.environ.get("SITEMEDIC_FORCE_DEMO", "").lower() in ("true", "1", "yes") or \
           os.environ.get("DEMO_PUBLIC", "").lower() in ("true", "1", "yes")


# ── Read-only state readers ────────────────────────────────────────────────

def _gcloud_describe(args: list[str]) -> dict:
    """Run a read-only gcloud command and return parsed JSON. Never raises."""
    cmd = args + [f"--project={_project()}", "--format=json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"_read_error": result.stderr.strip()}
        return json.loads(result.stdout or "{}")
    except Exception as exc:
        return {"_read_error": str(exc)}


async def _read_cloud_run_service(service: str) -> dict:
    """Read Cloud Run service state — read-only, no mutations."""
    if _is_demo_mode():
        return {
            "service": service,
            "url": f"https://{service}-demo.run.app",
            "latest_ready_revision": f"{service}-00003-abc",
            "traffic": [{"revision": f"{service}-00003-abc", "percent": 100}],
            "min_instances": 0,
            "max_instances": 1000,
            "env_vars": {},
        }

    def _sync() -> dict:
        raw = _gcloud_describe([
            "gcloud", "run", "services", "describe", service,
            f"--region={_region()}",
        ])
        if "_read_error" in raw:
            return {"service": service, "unavailable": raw["_read_error"]}
        return _parse_cloud_run_service(raw, service)

    return await asyncio.to_thread(_sync)


def _parse_cloud_run_service(raw: dict, service: str) -> dict:
    """Extract key operational fields from gcloud describe output (handles v1 + v2)."""
    # Traffic: v2 uses trafficStatuses for actuals, v1 uses status.traffic
    traffic_raw = (
        raw.get("trafficStatuses")
        or raw.get("status", {}).get("traffic")
        or raw.get("traffic")
        or []
    )
    traffic = []
    for t in traffic_raw:
        rev = t.get("revision") or t.get("revisionName") or ""
        if "/" in rev:
            rev = rev.split("/")[-1]
        traffic.append({
            "revision": rev or "LATEST",
            "percent": t.get("percent", 0),
        })

    # Latest revision
    latest = raw.get("latestReadyRevision") or raw.get("status", {}).get("latestReadyRevisionName", "")
    if "/" in latest:
        latest = latest.split("/")[-1]

    # Scaling — v2: template.scaling; v1: template.metadata.annotations
    template = raw.get("template", {})
    scaling = template.get("scaling", {})
    annotations = template.get("metadata", {}).get("annotations", {})
    min_inst = (
        scaling.get("minInstanceCount")
        or annotations.get("autoscaling.knative.dev/minScale")
        or 0
    )
    max_inst = (
        scaling.get("maxInstanceCount")
        or annotations.get("autoscaling.knative.dev/maxScale")
        or 1000
    )

    # Env vars (first container only)
    containers = template.get("containers", [{}])
    env_list = (containers[0] if containers else {}).get("env", [])
    env_vars = {e["name"]: e.get("value", "") for e in env_list if "name" in e}

    return {
        "service": service,
        "url": raw.get("uri") or raw.get("status", {}).get("url", ""),
        "latest_ready_revision": latest,
        "traffic": traffic,
        "min_instances": int(min_inst),
        "max_instances": int(max_inst),
        "env_vars": env_vars,
    }


async def _read_cloud_sql_instance(instance_id: str) -> dict:
    """Read Cloud SQL instance state — read-only."""
    def _sync() -> dict:
        try:
            from googleapiclient import discovery
            svc = discovery.build("sqladmin", "v1", cache_discovery=False)
            inst = svc.instances().get(project=_project(), instance=instance_id).execute()
            settings = inst.get("settings", {})
            return {
                "instance_id": instance_id,
                "state": inst.get("state"),
                "database_version": inst.get("databaseVersion"),
                "tier": settings.get("tier"),
                "ha_enabled": settings.get("availabilityType") == "REGIONAL",
                "failover_replica": inst.get("failoverReplica", {}).get("name"),
                "activation_policy": settings.get("activationPolicy"),
            }
        except Exception as exc:
            return {"instance_id": instance_id, "unavailable": str(exc)}

    return await asyncio.to_thread(_sync)


async def _read_bucket_state(bucket: str) -> dict:
    """Read GCS bucket state — read-only."""
    def _sync() -> dict:
        try:
            from google.cloud import storage
            client = storage.Client(project=_project())
            b = client.get_bucket(bucket)
            return {
                "bucket": bucket,
                "storage_class": b.storage_class,
                "versioning_enabled": b.versioning_enabled,
                "location": b.location,
            }
        except Exception as exc:
            return {"bucket": bucket, "unavailable": str(exc)}

    return await asyncio.to_thread(_sync)


async def _read_subscription_backlog(subscription: str) -> dict:
    """Read Pub/Sub subscription backlog metrics — read-only."""
    from tools.gcp_actions import query_subscription_backlog
    try:
        result = await query_subscription_backlog(subscription)
        return result
    except Exception as exc:
        return {"subscription": subscription, "unavailable": str(exc)}


# ── Per-action dry-run builders ────────────────────────────────────────────

async def _dry_rollback_revision(plan: dict) -> list[dict]:
    service = plan.get("service", "")
    revision = plan.get("revision", "")
    region = _region()
    project = _project()

    before = await _read_cloud_run_service(service)
    command = (
        f"gcloud run services update-traffic {service}"
        f" --to-revisions={revision}=100"
        f" --region={region} --project={project} --quiet"
    )

    after = dict(before)
    after["traffic"] = [{"revision": revision, "percent": 100}]

    warnings = []
    known_revs = [t.get("revision") for t in before.get("traffic", [])]
    known_revs.append(before.get("latest_ready_revision", ""))
    if revision and revision not in known_revs:
        warnings.append(
            f"Revision '{revision}' not in current traffic split — verify it exists with "
            f"`gcloud run revisions list --service={service} --region={region}`"
        )
    if before.get("traffic") and len(before["traffic"]) > 1:
        warnings.append(
            "Traffic is currently split across multiple revisions. "
            "Rollback will consolidate 100% to the target."
        )

    return [_step(
        0,
        f"Reroute 100% of traffic from current split to revision {revision or '(unset)'}",
        command,
        before,
        after,
        "instant",
        warnings,
    )]


async def _dry_scale_service(plan: dict) -> list[dict]:
    service = plan.get("service", "")
    new_min = int(plan.get("min_instances") or 0)
    region = _region()
    project = _project()

    before = await _read_cloud_run_service(service)
    command = (
        f"gcloud run services update {service}"
        f" --min-instances={new_min}"
        f" --region={region} --project={project} --quiet"
    )

    after = dict(before)
    after["min_instances"] = new_min

    warnings = []
    current_min = before.get("min_instances", 0)
    if new_min > current_min:
        warnings.append(
            f"Scaling from {current_min} to {new_min} min-instances will incur idle CPU/memory "
            f"billing even when there is no traffic. Estimated +${new_min * 0.044:.3f}/hr per instance."
        )
    if new_min == 0:
        warnings.append("Setting min-instances to 0 means cold starts are possible during low traffic.")

    return [_step(
        0,
        f"Set min-instances to {new_min} (currently {before.get('min_instances', '?')})",
        command,
        {"min_instances": before.get("min_instances"), "max_instances": before.get("max_instances")},
        {"min_instances": new_min, "max_instances": before.get("max_instances")},
        "instant",
        warnings,
    )]


async def _dry_restart_service(plan: dict) -> list[dict]:
    service = plan.get("service", "")
    region = _region()
    project = _project()
    preview_ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "_DRYRUN"

    before = await _read_cloud_run_service(service)
    command = (
        f"gcloud run services update {service}"
        f" --update-env-vars=SITEMEDIC_RESTART_AT={preview_ts}"
        f" --region={region} --project={project} --quiet"
    )

    after = dict(before)
    after["env_vars"] = dict(before.get("env_vars", {}))
    after["env_vars"]["SITEMEDIC_RESTART_AT"] = preview_ts

    warnings = [
        "A rolling restart deploys a new revision with an updated env var. "
        "In-flight requests may see brief errors during the rollout (typically < 30s).",
    ]
    traffic = before.get("traffic", [])
    if not traffic:
        warnings.append("Could not read current traffic split — verify the service exists before approving.")

    return [_step(
        0,
        f"Force rolling restart of {service} by bumping SITEMEDIC_RESTART_AT env var",
        command,
        {"env_var_SITEMEDIC_RESTART_AT": before.get("env_vars", {}).get("SITEMEDIC_RESTART_AT", "(not set)")},
        {"env_var_SITEMEDIC_RESTART_AT": preview_ts},
        "minutes",
        warnings,
    )]


async def _dry_no_action(plan: dict) -> list[dict]:
    return [_step(
        0,
        "No action — agent determined the problem does not require intervention",
        "(no command)",
        {},
        {},
        "instant",
        ["Verify the incident has self-resolved in Dynatrace before closing."],
    )]


async def _dry_failover_sql(plan: dict) -> list[dict]:
    instance_id = plan.get("instance_id", "")
    project = _project()

    before = await _read_cloud_sql_instance(instance_id)
    api_call = (
        f"POST https://sqladmin.googleapis.com/sql/v1/projects/{project}"
        f"/instances/{instance_id}/failover"
    )

    warnings = []
    if not before.get("ha_enabled"):
        warnings.append(
            f"Instance '{instance_id}' does not appear to have HA (REGIONAL) enabled. "
            "Failover requires a standby replica — the API call will fail if HA is not configured."
        )
    warnings.append(
        "Failover causes 60–90 seconds of downtime while the standby is promoted. "
        "In-progress transactions will be interrupted."
    )

    after = dict(before)
    after["state"] = "RUNNABLE (after failover — primary becomes standby)"

    return [_step(
        0,
        f"Trigger HA failover on Cloud SQL instance {instance_id}",
        api_call,
        before,
        after,
        "minutes",
        warnings,
    )]


async def _dry_restart_sql(plan: dict) -> list[dict]:
    instance_id = plan.get("instance_id", "")
    project = _project()

    before = await _read_cloud_sql_instance(instance_id)
    api_call = (
        f"POST https://sqladmin.googleapis.com/sql/v1/projects/{project}"
        f"/instances/{instance_id}/restart"
    )

    warnings = [
        "Instance restart causes 1–3 minutes of downtime. "
        "All active connections will be dropped."
    ]

    after = dict(before)
    after["state"] = "RUNNABLE (after restart)"

    return [_step(
        0,
        f"Restart Cloud SQL instance {instance_id}",
        api_call,
        before,
        after,
        "minutes",
        warnings,
    )]


async def _dry_change_storage_class(plan: dict) -> list[dict]:
    bucket = plan.get("bucket", "")
    new_class = (plan.get("storage_class") or "NEARLINE").upper()

    before = await _read_bucket_state(bucket)
    command = (
        f"gsutil defstorageclass set {new_class} gs://{bucket}"
    )

    after = dict(before)
    after["storage_class"] = new_class

    warnings = []
    current_class = before.get("storage_class", "STANDARD")
    if new_class in ("COLDLINE", "ARCHIVE"):
        warnings.append(
            f"Changing to {new_class} adds minimum storage duration charges "
            f"(90 days for COLDLINE, 365 days for ARCHIVE). "
            "Early deletion incurs fees."
        )
    if current_class == new_class:
        warnings.append(f"Bucket already uses {new_class} — this change is a no-op.")
    warnings.append("Only affects NEW objects — existing objects retain their current class.")

    return [_step(
        0,
        f"Change default storage class of gs://{bucket} from {current_class} to {new_class}",
        command,
        before,
        after,
        "instant",
        warnings,
    )]


async def _dry_enable_versioning(plan: dict) -> list[dict]:
    bucket = plan.get("bucket", "")

    before = await _read_bucket_state(bucket)
    command = f"gsutil versioning set on gs://{bucket}"

    after = dict(before)
    after["versioning_enabled"] = True

    warnings = []
    if before.get("versioning_enabled"):
        warnings.append("Versioning is already enabled — this change is a no-op.")
    warnings.append(
        "Enabling versioning increases storage costs because old object versions are retained. "
        "Set a lifecycle rule to expire old versions if cost is a concern."
    )

    return [_step(
        0,
        f"Enable object versioning on gs://{bucket}",
        command,
        before,
        after,
        "instant",
        warnings,
    )]


async def _dry_seek_subscription(plan: dict) -> list[dict]:
    subscription = plan.get("subscription", "")
    seek_ts = plan.get("seek_timestamp", "")

    backlog = await _read_subscription_backlog(subscription)
    api_call = (
        f"POST https://pubsub.googleapis.com/v1/{subscription}:seek\n"
        f'  body: {{"time": "{seek_ts}"}}'
    )

    warnings = [
        f"Messages published before {seek_ts} that were previously acked will be redelivered. "
        "Subscribers must handle duplicate messages idempotently.",
        "Seek to a past timestamp can significantly increase message backlog — monitor subscriber lag after execution.",
    ]

    return [_step(
        0,
        f"Seek subscription to timestamp {seek_ts} (replays messages from that point)",
        api_call,
        backlog,
        {"subscription": subscription, "seek_target": seek_ts, "effect": "messages from seek_target redelivered"},
        "instant",
        warnings,
    )]


async def _dry_purge_subscription(plan: dict) -> list[dict]:
    subscription = plan.get("subscription", "")

    backlog = await _read_subscription_backlog(subscription)
    metrics = backlog.get("metrics", {})
    backlog_count = metrics.get("num_undelivered_messages", "unknown")

    api_call = (
        f"POST https://pubsub.googleapis.com/v1/{subscription}:seek\n"
        f"  body: {{\"time\": \"<current UTC timestamp>\"}}  # seek-to-now = purge"
    )

    warnings = [
        f"DESTRUCTIVE: approximately {backlog_count} unacked messages will be PERMANENTLY discarded.",
        "This operation cannot be undone — purged messages are gone forever.",
        "Confirm that losing these messages is acceptable (e.g., they are retried elsewhere or the data is stale).",
    ]

    return [_step(
        0,
        f"Purge all unacked messages from {subscription} (seek to current time)",
        api_call,
        backlog,
        {"subscription": subscription, "num_undelivered_messages": 0, "effect": "all unacked messages permanently discarded"},
        "manual",
        warnings,
    )]


async def _dry_unknown(plan: dict) -> list[dict]:
    action = plan.get("action", "unknown")
    return [_step(
        0,
        f"Action '{action}' has no dry-run implementation",
        f"(action: {action})",
        {},
        {},
        "instant",
        [f"No dry-run preview available for '{action}'. Proceed with caution."],
    )]


# ── Dispatcher ─────────────────────────────────────────────────────────────

_DISPATCH = {
    "rollback_revision":             _dry_rollback_revision,
    "scale_service":                 _dry_scale_service,
    "restart_service":               _dry_restart_service,
    "no_action_needed":              _dry_no_action,
    "failover_cloud_sql_replica":    _dry_failover_sql,
    "restart_cloud_sql_instance":    _dry_restart_sql,
    "change_bucket_storage_class":   _dry_change_storage_class,
    "enable_bucket_versioning":      _dry_enable_versioning,
    "seek_subscription_to_timestamp": _dry_seek_subscription,
    "purge_pubsub_subscription_backlog": _dry_purge_subscription,
}


async def execute_dry_run(plan: dict) -> list[dict]:
    """
    Entry point. Returns a list of DryRunStep dicts.
    NEVER makes state-changing calls.
    """
    action = plan.get("action", "")
    handler = _DISPATCH.get(action, _dry_unknown)
    try:
        return await handler(plan)
    except Exception as exc:
        logger.exception(f"Dry-run handler failed for action={action}")
        return [_step(
            0,
            f"Dry-run failed for action '{action}'",
            f"error: {exc}",
            {},
            {},
            "instant",
            [f"Dry-run computation error: {exc}. The actual action may still work."],
        )]


async def generate_gemini_summary(plan: dict, steps: list[dict]) -> str:
    """
    Use Gemini Flash to write a plain-language summary of the dry-run report.
    Returns an empty string on failure so the caller can degrade gracefully.
    """
    try:
        import vertexai
        from vertexai.generative_models import Content, GenerativeModel, Part

        vertexai.init(
            project=_project(),
            location=os.environ.get("VERTEX_AI_LOCATION", "us-central1"),
        )

        steps_text = "\n".join(
            f"Step {s['step_index']}: {s['action_description']}\n"
            f"  Command: {s['command_or_api_call']}\n"
            f"  Reversibility: {s['reversibility']}\n"
            f"  Warnings: {'; '.join(s['warnings']) or 'none'}"
            for s in steps
        )

        prompt = (
            f"A SiteMedic dry-run was performed for the following remediation plan:\n"
            f"Action: {plan.get('action')}\n"
            f"Service/Resource: {plan.get('service') or plan.get('instance_id') or plan.get('bucket') or plan.get('subscription')}\n"
            f"Reason: {plan.get('reason', '')}\n\n"
            f"Dry-run steps:\n{steps_text}\n\n"
            f"In 2-3 sentences, explain to the operator: (1) what will happen if approved, "
            f"(2) what to verify after execution, and (3) any key concerns. Be direct and specific."
        )

        model = GenerativeModel(model_name=_FLASH_MODEL)
        response = await asyncio.to_thread(
            model.generate_content,
            [Content(role="user", parts=[Part.from_text(prompt)])],
        )
        return response.candidates[0].content.parts[0].text.strip()
    except Exception as exc:
        logger.warning(f"Gemini summary for dry-run failed: {exc}")
        return ""


# ── Utility ────────────────────────────────────────────────────────────────

def _step(
    index: int,
    description: str,
    command: str,
    before: dict,
    after: dict,
    reversibility: str,
    warnings: list[str],
) -> dict:
    return {
        "step_index": index,
        "action_description": description,
        "command_or_api_call": command,
        "predicted_before_state": before,
        "predicted_after_state": after,
        "reversibility": reversibility,
        "warnings": warnings,
    }
