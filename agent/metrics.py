"""Metrics instrumentation for Cloud Monitoring integration."""

import functools
import logging
import os
import time
from typing import Any, Callable, Optional

from google.cloud import monitoring_v3

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Collect and push metrics to Cloud Monitoring."""

    def __init__(self, project_id: Optional[str] = None):
        """Initialize metrics collector."""
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID")
        self.client = None
        self.enabled = bool(self.project_id)

        if self.enabled:
            try:
                self.client = monitoring_v3.MetricServiceClient()
            except Exception as e:
                logger.warning(f"Could not initialize Cloud Monitoring client: {e}")
                self.enabled = False

    def record_latency(self, operation: str, duration_ms: float, status: str = "success") -> None:
        """Record operation latency."""
        if not self.enabled:
            return

        try:
            series = monitoring_v3.TimeSeries()
            series.metric.type = "custom.googleapis.com/sitemedic/operation_latency"
            series.resource.type = "global"

            now = time.time()
            seconds = int(now)
            nanos = int((now - seconds) * 10 ** 9)
            interval = monitoring_v3.TimeInterval(
                {"end_time": {"seconds": seconds, "nanos": nanos}}
            )
            point = monitoring_v3.Point(
                {"interval": interval, "value": {"double_value": duration_ms}}
            )
            series.points = [point]

            # Add labels
            series.metric.labels["operation"] = operation
            series.metric.labels["status"] = status

            project_name = f"projects/{self.project_id}"
            self.client.create_time_series(name=project_name, time_series=[series])
        except Exception as e:
            logger.debug(f"Failed to record latency metric: {e}")

    def record_error(self, operation: str, error_type: str) -> None:
        """Record operation error."""
        if not self.enabled:
            return

        try:
            series = monitoring_v3.TimeSeries()
            series.metric.type = "custom.googleapis.com/sitemedic/operation_errors"
            series.resource.type = "global"

            now = time.time()
            seconds = int(now)
            nanos = int((now - seconds) * 10 ** 9)
            interval = monitoring_v3.TimeInterval(
                {"end_time": {"seconds": seconds, "nanos": nanos}}
            )
            point = monitoring_v3.Point(
                {"interval": interval, "value": {"int64_value": 1}}
            )
            series.points = [point]

            # Add labels
            series.metric.labels["operation"] = operation
            series.metric.labels["error_type"] = error_type

            project_name = f"projects/{self.project_id}"
            self.client.create_time_series(name=project_name, time_series=[series])
        except Exception as e:
            logger.debug(f"Failed to record error metric: {e}")


# Global metrics instance
_metrics = None


def get_metrics() -> MetricsCollector:
    """Get or create global metrics collector."""
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


def instrument_latency(operation_name: str) -> Callable:
    """Decorator to measure function latency and report to Cloud Monitoring."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.time()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.time() - start) * 1000
                get_metrics().record_latency(operation_name, duration_ms, "success")
                return result
            except Exception as e:
                duration_ms = (time.time() - start) * 1000
                get_metrics().record_latency(operation_name, duration_ms, "error")
                get_metrics().record_error(operation_name, type(e).__name__)
                raise

        return wrapper

    return decorator
