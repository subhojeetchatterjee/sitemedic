"""
Execute remediation actions across Cloud Run, Cloud SQL, Cloud Storage, and Pub/Sub.

Credential strategy: Application Default Credentials (ADC) on Cloud Run.
The service account must have:
  - roles/run.admin                   (Cloud Run)
  - roles/cloudsql.admin              (Cloud SQL)
  - roles/storage.admin               (Cloud Storage)
  - roles/pubsub.admin                (Pub/Sub)

Safety ratings per action:
  reversible       — can be undone without data loss
  non-destructive  — read-only; causes no side-effects
  destructive      — irreversible; requires explicit typed confirmation
"""
import asyncio
import datetime
import os
import subprocess
from typing import Optional

from google.cloud import monitoring_v3
from google.cloud import pubsub_v1
from google.cloud import storage
from googleapiclient import discovery as gapi_discovery
from googleapiclient.errors import HttpError


# ── Helpers ────────────────────────────────────────────────────────────────

def _project() -> str:
    return os.environ["GCP_PROJECT_ID"]


def _region() -> str:
    return os.environ.get("GCP_REGION", "us-central1")


def _is_demo_mode() -> bool:
    return os.environ.get("SITEMEDIC_FORCE_DEMO", "").lower() in ("true", "1", "yes") or \
           os.environ.get("DEMO_PUBLIC", "").lower() in ("true", "1", "yes")


def _simulate_remediation(plan: dict) -> dict:
    """Return a plausible success response without touching real infrastructure."""
    action = plan.get("action", "unknown")
    service = plan.get("service", "sitemedic-demo-app")
    return {
        "action": action,
        "service": service,
        "demo_mode": True,
        "status": "simulated",
        "stdout": f"[DEMO] {action} on {service} completed successfully.",
        "returncode": 0,
    }


def _run_gcloud(cmd: list[str]) -> dict:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"gcloud failed: {result.stderr.strip()}")
    return {"stdout": result.stdout.strip(), "returncode": result.returncode}


async def _shell(cmd: list[str]) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_gcloud, cmd)


# ── Cloud Run ──────────────────────────────────────────────────────────────
# rollback_safety: reversible

async def rollback_revision(service: str, revision: str) -> dict:
    """Route 100% of traffic to a specific Cloud Run revision."""
    result = await _shell([
        "gcloud", "run", "services", "update-traffic", service,
        f"--to-revisions={revision}=100",
        f"--region={_region()}", f"--project={_project()}", "--quiet",
    ])
    return {"action": "rollback_revision", "service": service, "revision": revision, **result}


async def scale_service(service: str, min_instances: int, max_instances: Optional[int] = None) -> dict:
    """Update min (and optionally max) instance count for a Cloud Run service."""
    cmd = [
        "gcloud", "run", "services", "update", service,
        f"--min-instances={min_instances}",
        f"--region={_region()}", f"--project={_project()}", "--quiet",
    ]
    if max_instances is not None:
        cmd.append(f"--max-instances={max_instances}")
    result = await _shell(cmd)
    return {"action": "scale_service", "service": service, "min_instances": min_instances, **result}


async def restart_service(service: str) -> dict:
    """Force a rolling restart by bumping an env var timestamp."""
    now = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    result = await _shell([
        "gcloud", "run", "services", "update", service,
        f"--update-env-vars=SITEMEDIC_RESTART_AT={now}",
        f"--region={_region()}", f"--project={_project()}", "--quiet",
    ])
    return {"action": "restart_service", "service": service, "restart_at": now, **result}


# ── Cloud SQL ──────────────────────────────────────────────────────────────

def _sqladmin():
    """Build the Cloud SQL Admin API service client (uses ADC)."""
    return gapi_discovery.build("sqladmin", "v1", cache_discovery=False)


async def failover_cloud_sql_replica(instance_id: str) -> dict:
    """
    Trigger an HA failover on a Cloud SQL instance (primary → standby).
    rollback_safety: reversible — the original primary becomes standby and can fail back.

    Requires: Cloud SQL HA (regional) configuration enabled on the instance.
    """
    def _sync() -> dict:
        svc = _sqladmin()
        try:
            op = (
                svc.instances()
                .failover(project=_project(), instance=instance_id, body={})
                .execute()
            )
            return {
                "action": "failover_cloud_sql_replica",
                "instance_id": instance_id,
                "operation_id": op.get("name"),
                "status": op.get("status"),
            }
        except HttpError as e:
            raise RuntimeError(f"Cloud SQL failover failed: {e}") from e

    return await asyncio.to_thread(_sync)


async def restart_cloud_sql_instance(instance_id: str) -> dict:
    """
    Restart a Cloud SQL instance — clears stuck connections and OOM state.
    rollback_safety: reversible — instance comes back automatically; brief downtime expected.
    """
    def _sync() -> dict:
        svc = _sqladmin()
        try:
            op = (
                svc.instances()
                .restart(project=_project(), instance=instance_id)
                .execute()
            )
            return {
                "action": "restart_cloud_sql_instance",
                "instance_id": instance_id,
                "operation_id": op.get("name"),
                "status": op.get("status"),
            }
        except HttpError as e:
            raise RuntimeError(f"Cloud SQL restart failed: {e}") from e

    return await asyncio.to_thread(_sync)


async def query_cloud_sql_active_connections(instance_id: str) -> dict:
    """
    Read-only: fetch current active connection count via Cloud Monitoring.
    rollback_safety: non-destructive — diagnostic only.
    """
    def _sync() -> dict:
        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{_project()}"
        now = datetime.datetime.now(datetime.timezone.utc)
        interval = monitoring_v3.TimeInterval(
            end_time={"seconds": int(now.timestamp())},
            start_time={"seconds": int((now - datetime.timedelta(minutes=15)).timestamp())},
        )
        # Works for both MySQL (threads_connected) and PostgreSQL (num_backends)
        results = {}
        for metric in [
            "cloudsql.googleapis.com/database/mysql/threads_connected",
            "cloudsql.googleapis.com/database/postgresql/num_backends",
        ]:
            try:
                req = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="{metric}" AND resource.label.database_id="{_project()}:{instance_id}"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                )
                for ts in client.list_time_series(request=req):
                    if ts.points:
                        latest = ts.points[0]
                        results[metric] = {
                            "value": latest.value.int64_value or latest.value.double_value,
                            "timestamp": latest.interval.end_time.isoformat(),
                        }
            except Exception:
                pass  # metric may not apply to this engine type
        return {
            "action": "query_cloud_sql_active_connections",
            "instance_id": instance_id,
            "connections": results,
        }

    return await asyncio.to_thread(_sync)


# ── Cloud Storage ──────────────────────────────────────────────────────────

def _gcs_client():
    return storage.Client(project=_project())


async def change_bucket_storage_class(bucket: str, storage_class: str) -> dict:
    """
    Change the default storage class for new objects in a bucket.
    rollback_safety: reversible — can be changed back; existing objects unaffected.

    Valid storage_class values: STANDARD, NEARLINE, COLDLINE, ARCHIVE
    """
    def _sync() -> dict:
        client = _gcs_client()
        b = client.get_bucket(bucket)
        previous_class = b.storage_class
        b.storage_class = storage_class.upper()
        b.patch()
        return {
            "action": "change_bucket_storage_class",
            "bucket": bucket,
            "previous_storage_class": previous_class,
            "new_storage_class": storage_class.upper(),
        }

    return await asyncio.to_thread(_sync)


async def enable_bucket_versioning(bucket: str) -> dict:
    """
    Enable object versioning on a bucket.
    rollback_safety: reversible — versioning can be suspended (not deleted) later.
    Enables recovery from accidental object overwrites or corruption.
    """
    def _sync() -> dict:
        client = _gcs_client()
        b = client.get_bucket(bucket)
        was_enabled = b.versioning_enabled
        b.versioning_enabled = True
        b.patch()
        return {
            "action": "enable_bucket_versioning",
            "bucket": bucket,
            "was_already_enabled": was_enabled,
            "versioning_enabled": True,
        }

    return await asyncio.to_thread(_sync)


async def query_bucket_anomalies(bucket: str, top_n: int = 20) -> dict:
    """
    Read-only: surface objects with unusual size or stale modification timestamps.
    rollback_safety: non-destructive — lists objects only; no mutations.

    Useful for diagnosing data corruption, unexpected growth, or forgotten large objects.
    """
    def _sync() -> dict:
        client = _gcs_client()
        b = client.get_bucket(bucket)

        blobs = list(client.list_blobs(bucket, max_results=500))
        if not blobs:
            return {"action": "query_bucket_anomalies", "bucket": bucket, "objects": [], "total_scanned": 0}

        sizes = [blob.size or 0 for blob in blobs]
        avg_size = sum(sizes) / len(sizes)
        stddev = (sum((s - avg_size) ** 2 for s in sizes) / len(sizes)) ** 0.5
        threshold = avg_size + 2 * stddev

        anomalies = []
        for blob in blobs:
            blob_size = blob.size or 0
            reasons = []
            if blob_size > threshold and blob_size > 10 * 1024 * 1024:  # >10 MB and >2σ
                reasons.append(f"large: {blob_size // 1024 // 1024} MB")
            age_days = (
                (datetime.datetime.now(datetime.timezone.utc) - blob.time_created).days
                if blob.time_created else 0
            )
            if age_days > 365:
                reasons.append(f"stale: {age_days} days old")
            if reasons:
                anomalies.append({
                    "name": blob.name,
                    "size_bytes": blob_size,
                    "age_days": age_days,
                    "reasons": reasons,
                })

        anomalies.sort(key=lambda x: x["size_bytes"], reverse=True)
        return {
            "action": "query_bucket_anomalies",
            "bucket": bucket,
            "total_scanned": len(blobs),
            "anomaly_count": len(anomalies),
            "objects": anomalies[:top_n],
            "avg_size_bytes": int(avg_size),
        }

    return await asyncio.to_thread(_sync)


# ── Pub/Sub ────────────────────────────────────────────────────────────────

def _subscriber():
    return pubsub_v1.SubscriberClient()


async def query_subscription_backlog(subscription: str) -> dict:
    """
    Read-only: get current backlog size and oldest unacked message age.
    rollback_safety: non-destructive — reads subscription metadata only.

    subscription: full resource name, e.g. projects/my-project/subscriptions/my-sub
    """
    def _sync() -> dict:
        # Use Cloud Monitoring for backlog metrics — more reliable than Pub/Sub API alone
        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{_project()}"
        now = datetime.datetime.now(datetime.timezone.utc)
        interval = monitoring_v3.TimeInterval(
            end_time={"seconds": int(now.timestamp())},
            start_time={"seconds": int((now - datetime.timedelta(minutes=30)).timestamp())},
        )
        # Extract subscription id from full path
        sub_id = subscription.split("/")[-1]
        metrics_out = {}
        for metric, label in [
            ("pubsub.googleapis.com/subscription/num_undelivered_messages", "num_undelivered_messages"),
            ("pubsub.googleapis.com/subscription/oldest_unacked_message_age", "oldest_unacked_message_age_seconds"),
        ]:
            try:
                req = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=f'metric.type="{metric}" AND resource.label.subscription_id="{sub_id}"',
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                )
                for ts in client.list_time_series(request=req):
                    if ts.points:
                        p = ts.points[0]
                        metrics_out[label] = p.value.int64_value or p.value.double_value
            except Exception:
                pass

        return {
            "action": "query_subscription_backlog",
            "subscription": subscription,
            "metrics": metrics_out,
            "sampled_at": now.isoformat(),
        }

    return await asyncio.to_thread(_sync)


async def seek_subscription_to_timestamp(subscription: str, seek_timestamp: str) -> dict:
    """
    Seek a Pub/Sub subscription to a past timestamp for non-destructive message replay.
    rollback_safety: reversible — a subsequent seek can advance or rewind again.

    Messages published before seek_timestamp and not yet acked will be redelivered.
    seek_timestamp: ISO-8601 string, e.g. "2024-01-15T14:30:00Z"
    """
    def _sync() -> dict:
        from google.protobuf.timestamp_pb2 import Timestamp as PbTimestamp
        import google.protobuf.timestamp_pb2

        ts = datetime.datetime.fromisoformat(seek_timestamp.replace("Z", "+00:00"))
        pb_ts = PbTimestamp()
        pb_ts.FromDatetime(ts)

        client = _subscriber()
        try:
            client.seek(
                request=pubsub_v1.types.SeekRequest(
                    subscription=subscription,
                    time=pb_ts,
                )
            )
            return {
                "action": "seek_subscription_to_timestamp",
                "subscription": subscription,
                "seek_timestamp": seek_timestamp,
                "status": "ok",
            }
        except Exception as e:
            raise RuntimeError(f"Pub/Sub seek failed: {e}") from e
        finally:
            client.close()

    return await asyncio.to_thread(_sync)


async def purge_pubsub_subscription_backlog(subscription: str) -> dict:
    """
    Purge all unacked messages by seeking to the current time.
    rollback_safety: DESTRUCTIVE — unacked messages are permanently discarded.

    This is implemented as seek-to-now, which is the canonical GCP-recommended purge.
    Requires explicit_confirmation == subscription name in the approval payload.
    """
    def _sync() -> dict:
        now_pb = pubsub_v1.types.SeekRequest(
            subscription=subscription,
            time=_now_pb_timestamp(),
        )
        client = _subscriber()
        try:
            client.seek(request=now_pb)
            return {
                "action": "purge_pubsub_subscription_backlog",
                "subscription": subscription,
                "status": "purged — all unacked messages discarded",
                "purged_at": datetime.datetime.utcnow().isoformat(),
            }
        except Exception as e:
            raise RuntimeError(f"Pub/Sub purge failed: {e}") from e
        finally:
            client.close()

    return await asyncio.to_thread(_sync)


def _now_pb_timestamp():
    """Return a protobuf Timestamp for the current UTC time."""
    from google.protobuf.timestamp_pb2 import Timestamp as PbTimestamp
    ts = PbTimestamp()
    ts.GetCurrentTime()
    return ts


# ── Dispatch ───────────────────────────────────────────────────────────────

async def execute_remediation(plan: dict) -> dict:
    """
    Dispatch to the correct action based on plan["action"].
    Destructive actions must have already been validated by main.py before this is called.
    """
    if _is_demo_mode():
        return _simulate_remediation(plan)

    action = plan.get("action")

    # Cloud Run
    if action == "rollback_revision":
        return await rollback_revision(plan["service"], plan["revision"])
    elif action == "scale_service":
        return await scale_service(plan["service"], plan["min_instances"])
    elif action == "restart_service":
        return await restart_service(plan["service"])
    elif action == "no_action_needed":
        return {"action": "no_action_needed", "message": "Agent determined no action was required."}

    # Cloud SQL
    elif action == "failover_cloud_sql_replica":
        return await failover_cloud_sql_replica(plan["instance_id"])
    elif action == "restart_cloud_sql_instance":
        return await restart_cloud_sql_instance(plan["instance_id"])

    # Cloud Storage
    elif action == "change_bucket_storage_class":
        return await change_bucket_storage_class(plan["bucket"], plan["storage_class"])
    elif action == "enable_bucket_versioning":
        return await enable_bucket_versioning(plan["bucket"])

    # Pub/Sub
    elif action == "seek_subscription_to_timestamp":
        return await seek_subscription_to_timestamp(plan["subscription"], plan["seek_timestamp"])
    elif action == "purge_pubsub_subscription_backlog":
        return await purge_pubsub_subscription_backlog(plan["subscription"])

    else:
        raise ValueError(f"Unknown action: {action}")
