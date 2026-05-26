"""
Google Cloud observability tools for Gemini function calling.

All three tools use Application Default Credentials (ADC) — no explicit key
management needed on Cloud Run when the service account has the right bindings:
  - roles/logging.viewer
  - roles/monitoring.viewer
  - roles/cloudtrace.user

Each function returns a JSON-serializable dict capped at 100 entries / ~5 MB.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import logging as gcloud_logging
from google.cloud import monitoring_v3
from google.cloud import trace_v1

_MAX_ENTRIES = 100
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB safety cap


def _project() -> str:
    return os.environ["GCP_PROJECT_ID"]


def _truncate(obj: Any) -> Any:
    """Best-effort JSON serialisation with byte-cap."""
    serialized = json.dumps(obj, default=str)
    if len(serialized.encode()) > _MAX_BYTES:
        # Return a truncated string representation rather than blowing the context window
        return {"truncated": True, "preview": serialized[: _MAX_BYTES // 2]}
    return obj


# ── Cloud Logging ──────────────────────────────────────────────────────────

async def query_cloud_logging(
    filter: str,
    time_range_minutes: int = 30,
) -> dict:
    """
    Fetch Cloud Logging entries matching *filter* over the last *time_range_minutes*.

    Example filter: 'resource.type="cloud_run_revision" severity>=ERROR'
    Returns at most 100 log entries with timestamp, severity, and text payload.
    """
    def _sync() -> dict:
        client = gcloud_logging.Client(project=_project())
        since = datetime.now(timezone.utc) - timedelta(minutes=time_range_minutes)
        # Append time bound to avoid full-table scans
        time_filter = f'timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
        full_filter = f"({filter}) AND {time_filter}" if filter else time_filter

        entries = []
        for entry in client.list_entries(
            filter_=full_filter,
            order_by=gcloud_logging.DESCENDING,
            max_results=_MAX_ENTRIES,
        ):
            payload: Any
            if hasattr(entry, "payload") and isinstance(entry.payload, dict):
                payload = entry.payload
            elif hasattr(entry, "payload"):
                payload = str(entry.payload)
            else:
                payload = None

            entries.append({
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                "severity": entry.severity,
                "log_name": entry.log_name,
                "resource_type": entry.resource.type if entry.resource else None,
                "resource_labels": dict(entry.resource.labels) if entry.resource else {},
                "payload": payload,
                "trace": getattr(entry, "trace", None),
            })

        return _truncate({
            "filter": full_filter,
            "time_range_minutes": time_range_minutes,
            "entry_count": len(entries),
            "entries": entries,
        })

    return await asyncio.to_thread(_sync)


# ── Cloud Monitoring ───────────────────────────────────────────────────────

async def query_cloud_monitoring(
    metric_type: str,
    resource_labels: dict | None = None,
    time_range_minutes: int = 60,
) -> dict:
    """
    Fetch a Cloud Monitoring time series for *metric_type*.

    Example metric_type: 'run.googleapis.com/request_latencies'
    resource_labels example: {"service_name": "sitemedic-demo-app", "location": "us-central1"}
    Returns the last *time_range_minutes* of data points (up to 100 series, 100 points each).
    """
    def _sync() -> dict:
        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{_project()}"

        now = datetime.now(timezone.utc)
        interval = monitoring_v3.TimeInterval(
            end_time={"seconds": int(now.timestamp())},
            start_time={"seconds": int((now - timedelta(minutes=time_range_minutes)).timestamp())},
        )

        # Build filter string
        parts = [f'metric.type="{metric_type}"']
        if resource_labels:
            for k, v in resource_labels.items():
                parts.append(f'resource.label.{k}="{v}"')
        metric_filter = " AND ".join(parts)

        series_out = []
        request = monitoring_v3.ListTimeSeriesRequest(
            name=project_name,
            filter=metric_filter,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )
        for ts in client.list_time_series(request=request):
            points = []
            for point in ts.points[:_MAX_ENTRIES]:
                val = point.value
                # Extract whichever value type is set
                scalar = (
                    val.double_value
                    or val.int64_value
                    or val.bool_value
                    or (val.distribution_value.mean if val.distribution_value else None)
                )
                points.append({
                    "interval_end": point.interval.end_time.isoformat()
                    if point.interval.end_time else None,
                    "value": scalar,
                })
            series_out.append({
                "metric_type": ts.metric.type,
                "metric_labels": dict(ts.metric.labels),
                "resource_type": ts.resource.type,
                "resource_labels": dict(ts.resource.labels),
                "points": points,
            })
            if len(series_out) >= _MAX_ENTRIES:
                break

        return _truncate({
            "metric_type": metric_type,
            "filter": metric_filter,
            "time_range_minutes": time_range_minutes,
            "series_count": len(series_out),
            "series": series_out,
        })

    return await asyncio.to_thread(_sync)


# ── Cloud Trace ────────────────────────────────────────────────────────────

async def list_recent_slow_traces(
    service_name: str,
    threshold_ms: int = 1000,
    time_range_minutes: int = 30,
) -> dict:
    """
    List recent distributed traces for *service_name* where total duration
    exceeds *threshold_ms* milliseconds.

    Returns at most 100 traces with their root span details and duration.
    """
    def _sync() -> dict:
        client = trace_v1.TraceServiceClient()
        project_id = _project()

        since = datetime.now(timezone.utc) - timedelta(minutes=time_range_minutes)

        request = trace_v1.ListTracesRequest(
            project_id=project_id,
            start_time=since,
            filter=f"+{service_name}",
            page_size=_MAX_ENTRIES,
        )

        traces_out = []
        try:
            page = client.list_traces(request=request)
            for trace in page:
                if not trace.spans:
                    continue
                # Find root span (parent_span_id == 0 or "0")
                root = next(
                    (s for s in trace.spans if not s.parent_span_id or s.parent_span_id == "0"),
                    trace.spans[0],
                )
                start_ms = root.start_time.timestamp() * 1000 if root.start_time else 0
                end_ms = root.end_time.timestamp() * 1000 if root.end_time else 0
                duration_ms = end_ms - start_ms

                if duration_ms < threshold_ms:
                    continue

                traces_out.append({
                    "trace_id": trace.trace_id,
                    "duration_ms": round(duration_ms, 2),
                    "root_span_name": root.name,
                    "span_count": len(trace.spans),
                    "spans": [
                        {
                            "span_id": s.span_id,
                            "name": s.name,
                            "start_time": s.start_time.isoformat() if s.start_time else None,
                            "end_time": s.end_time.isoformat() if s.end_time else None,
                            "labels": dict(s.labels) if s.labels else {},
                        }
                        for s in trace.spans[:20]  # cap spans per trace
                    ],
                })

                if len(traces_out) >= _MAX_ENTRIES:
                    break
        except Exception as exc:
            return {"error": str(exc), "traces": []}

        return _truncate({
            "service_name": service_name,
            "threshold_ms": threshold_ms,
            "time_range_minutes": time_range_minutes,
            "trace_count": len(traces_out),
            "traces": traces_out,
        })

    return await asyncio.to_thread(_sync)


async def get_cloud_trace_spans(trace_id: str) -> dict:
    """
    Fetch all spans for a specific Cloud Trace trace ID.
    Use this when you already have a trace_id from a log entry or Dynatrace.
    """
    def _sync() -> dict:
        client = trace_v1.TraceServiceClient()
        try:
            trace = client.get_trace(
                request=trace_v1.GetTraceRequest(
                    project_id=_project(),
                    trace_id=trace_id,
                )
            )
            spans = [
                {
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "name": s.name,
                    "start_time": s.start_time.isoformat() if s.start_time else None,
                    "end_time": s.end_time.isoformat() if s.end_time else None,
                    "labels": dict(s.labels) if s.labels else {},
                }
                for s in trace.spans[:_MAX_ENTRIES]
            ]
            return _truncate({
                "trace_id": trace_id,
                "span_count": len(trace.spans),
                "spans": spans,
            })
        except Exception as exc:
            return {"error": str(exc), "trace_id": trace_id}

    return await asyncio.to_thread(_sync)
