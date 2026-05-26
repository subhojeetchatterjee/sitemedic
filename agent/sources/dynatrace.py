"""
DynatraceMCPSource — live TelemetrySource backed by tools/dynatrace_mcp.py.

This is a thin delegation wrapper that satisfies the TelemetrySource interface
without modifying the underlying MCP client.  All calls are forwarded as-is;
this class only adds the interface conformance and metadata reporting.
"""
from __future__ import annotations

import logging

from sources.base import TelemetrySource, SourceMetadata

logger = logging.getLogger(__name__)


class DynatraceMCPSource(TelemetrySource):
    """
    Live telemetry source backed by the Dynatrace MCP server.

    Delegates every call to tools.dynatrace_mcp module functions.
    The tools/dynatrace_mcp.py module is NOT modified — this wrapper
    simply adapts it to the TelemetrySource interface.
    """

    def __init__(self) -> None:
        self._healthy: bool = True
        logger.debug("DynatraceMCPSource initialised (live mode)")

    # ── TelemetrySource interface ──────────────────────────────────────────

    async def list_problems(self, status: str = "OPEN") -> list[dict]:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.list_problems(status=status)

    async def get_problem_details(self, problem_id: str) -> dict:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.get_problem_details(problem_id)

    async def query_metrics(
        self,
        metric_selector: str,
        entity_selector: str | None = None,
        from_time: str = "now-1h",
        to_time: str = "now",
        resolution: str = "1m",
    ) -> dict:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.query_metrics(
            metric_selector=metric_selector,
            entity_selector=entity_selector,
            from_time=from_time,
            to_time=to_time,
            resolution=resolution,
        )

    async def get_traces(
        self,
        service_name: str,
        from_time: str = "now-30m",
        to_time: str = "now",
        limit: int = 20,
    ) -> list[dict]:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.get_traces(
            service_name=service_name,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
        )

    async def list_entities(self, entity_type: str = "SERVICE") -> list[dict]:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.list_entities(entity_type=entity_type)

    async def get_service_response_time(self, service_name: str) -> dict:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.get_service_response_time(service_name)

    async def get_error_rate(self, service_name: str) -> dict:
        from tools import dynatrace_mcp
        return await dynatrace_mcp.get_error_rate(service_name)

    def get_source_metadata(self) -> SourceMetadata:
        return SourceMetadata(
            is_live=True,
            source_type="dynatrace",
            health_status="healthy" if self._healthy else "degraded",
            demo_mode_active=False,
            current_scenario=None,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    def mark_unhealthy(self) -> None:
        """Called by source_factory when a health check fails."""
        self._healthy = False
        logger.warning("DynatraceMCPSource marked as unhealthy")

    def mark_healthy(self) -> None:
        """Called by source_factory when health is restored."""
        self._healthy = True
        logger.info("DynatraceMCPSource marked as healthy")
