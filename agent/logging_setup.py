"""Structured logging configuration with Cloud Logging integration."""

import json
import logging
import os
import sys
from typing import Any, Dict

from google.cloud import logging as cloud_logging


class StructuredJsonFormatter(logging.Formatter):
    """Format logs as structured JSON with Cloud Logging severity mapping."""

    def format(self, record: logging.LogRecord) -> str:
        """Convert log record to structured JSON."""
        log_obj: Dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "severity": self._map_severity(record.levelno),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add trace context if available (from OpenTelemetry or Cloud Trace)
        if hasattr(record, "trace_id"):
            log_obj["trace_id"] = record.trace_id
        if hasattr(record, "span_id"):
            log_obj["span_id"] = record.span_id

        # Add request context if available
        if hasattr(record, "request_id"):
            log_obj["request_id"] = record.request_id

        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
            }

        # Add any extra fields (from logger.info(..., extra={...}))
        if hasattr(record, "custom_fields"):
            log_obj.update(record.custom_fields)

        return json.dumps(log_obj, default=str)

    @staticmethod
    def _map_severity(level: int) -> str:
        """Map Python logging level to Cloud Logging severity."""
        severity_map = {
            logging.DEBUG: "DEBUG",
            logging.INFO: "INFO",
            logging.WARNING: "WARNING",
            logging.ERROR: "ERROR",
            logging.CRITICAL: "CRITICAL",
        }
        return severity_map.get(level, "DEFAULT")


def setup_logging(
    env_name: str = "dev",
    structured: bool = True,
    use_cloud_logging: bool = False,
    log_level: str = "INFO",
) -> logging.Logger:
    """
    Initialize logging configuration.

    Args:
        env_name: Environment name (dev/staging/prod)
        structured: Whether to use JSON structured logging
        use_cloud_logging: Whether to send logs to Cloud Logging (GCP)
        log_level: Logging level (DEBUG/INFO/WARNING/ERROR/CRITICAL)

    Returns:
        Configured root logger
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Setup Cloud Logging if requested and GCP_PROJECT_ID is set
    if use_cloud_logging and os.environ.get("GCP_PROJECT_ID"):
        try:
            cloud_client = cloud_logging.Client()
            cloud_handler = cloud_client.logging_handler_class()
            root_logger.addHandler(cloud_handler)
        except Exception as e:
            print(f"Warning: Could not initialize Cloud Logging: {e}", file=sys.stderr)

    # Always add console handler for local visibility
    console_handler = logging.StreamHandler(sys.stdout)
    if structured:
        console_handler.setFormatter(StructuredJsonFormatter())
    else:
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
        )
    root_logger.addHandler(console_handler)

    return root_logger
