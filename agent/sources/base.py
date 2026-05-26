"""
TelemetrySource abstract base class.

All telemetry backends (Dynatrace MCP, Demo/Replay) implement this interface.
The orchestrator and other callers should use TelemetrySource exclusively —
never import tools.dynatrace_mcp directly except through DynatraceMCPSource.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceMetadata:
    """Runtime information about the active telemetry source."""
    is_live: bool
    source_type: str          # "dynatrace" | "demo"
    health_status: str        # "healthy" | "degraded" | "down" | "demo"
    demo_mode_active: bool
    current_scenario: str | None = None
    scenarios_available: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_live": self.is_live,
            "source_type": self.source_type,
            "health_status": self.health_status,
            "demo_mode_active": self.demo_mode_active,
            "current_scenario": self.current_scenario,
            "scenarios_available": self.scenarios_available,
            **self.extra,
        }


class TelemetrySource(ABC):
    """
    Abstract telemetry backend.

    Both DynatraceMCPSource (live) and DemoModeSource (playback) implement
    this interface so the orchestrator can use either interchangeably.
    """

    @abstractmethod
    async def list_problems(self, status: str = "OPEN") -> list[dict]:
        """
        List open/active problems.

        Returns a list of problem dicts with at minimum:
          - problemId: str
          - title: str
          - status: str
          - severityLevel: str
          - startTime: int (epoch ms)
          - impactedEntities: list[dict]
          - rootCauseEntity: dict | None
        """
        ...

    @abstractmethod
    async def get_problem_details(self, problem_id: str) -> dict:
        """
        Fetch full details for a specific problem.

        Returns a problem dict including evidenceDetails, rootCauseEntity, etc.
        """
        ...

    @abstractmethod
    async def query_metrics(
        self,
        metric_selector: str,
        entity_selector: str | None = None,
        from_time: str = "now-1h",
        to_time: str = "now",
        resolution: str = "1m",
    ) -> dict:
        """
        Query a Dynatrace metric time series.

        Returns a dict with 'result' containing the time-series data.
        """
        ...

    @abstractmethod
    async def get_traces(
        self,
        service_name: str,
        from_time: str = "now-30m",
        to_time: str = "now",
        limit: int = 20,
    ) -> list[dict]:
        """
        Fetch distributed traces for a named service.

        Returns a list of trace dicts with spanId, duration, status, tags, etc.
        """
        ...

    @abstractmethod
    async def list_entities(self, entity_type: str = "SERVICE") -> list[dict]:
        """
        List monitored entities of the given type (SERVICE, HOST, etc.).

        Returns a list of entity dicts with entityId, displayName, type, etc.
        """
        ...

    @abstractmethod
    async def get_service_response_time(self, service_name: str) -> dict:
        """
        Convenience helper: p50/p90/p99 response time for a service over 1h.

        Returns a dict with percentile time-series data.
        """
        ...

    @abstractmethod
    async def get_error_rate(self, service_name: str) -> dict:
        """
        Convenience helper: error rate for a service over 1h.

        Returns a dict with error rate time-series data.
        """
        ...

    @abstractmethod
    def get_source_metadata(self) -> SourceMetadata:
        """
        Return metadata describing this source instance.

        Called by the agent API to expose source status to the frontend.
        """
        ...
