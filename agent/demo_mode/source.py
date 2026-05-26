"""
DemoModeSource — drop-in replacement for tools/dynatrace_mcp.py that serves
pre-recorded MCP responses from a curated scenario file.

Responses are served in call order per tool (FIFO queue per tool name).
If a tool's queue is exhausted, an empty result is returned gracefully.

Realistic latency is optional (enabled by default) and adds a small jitter
around the recorded latency, capped at 2 seconds.

Usage:
    from demo_mode import get_demo_source

    dt = get_demo_source("high_error_rate")
    problems = await dt.list_problems()
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCENARIOS_DIR = Path(__file__).parent / "scenarios"
_MAX_LATENCY_S = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_scenario(name: str) -> dict[str, Any]:
    """Load a curated scenario by name from the scenarios/ directory."""
    path = _SCENARIOS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Scenario '{name}' not found at {path}. "
            f"Run 'python demo_mode/curator.py' to generate scenarios."
        )
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def get_demo_source(
    scenario_name: str,
    realistic_latency: bool = True,
) -> "DemoModeSource":
    """Load scenario and return a ready DemoModeSource."""
    scenario = load_scenario(scenario_name)
    return DemoModeSource(scenario, realistic_latency=realistic_latency)


# ---------------------------------------------------------------------------
# DemoModeSource
# ---------------------------------------------------------------------------

class DemoModeSource:
    """
    Serves pre-recorded MCP responses for demo playback.

    Responses are served in call order per tool (round-robin queue per tool
    name). If a tool's queue is exhausted, returns [] / {} gracefully.
    Optionally adds realistic latency.
    """

    def __init__(
        self,
        scenario: dict[str, Any],
        realistic_latency: bool = True,
    ) -> None:
        self._scenario = scenario
        self._realistic_latency = realistic_latency

        # Build per-tool FIFO queues from scenario["mcp_calls"] in order.
        # Each entry: (response, latency_ms)
        self._queues: dict[str, deque[tuple[Any, int]]] = defaultdict(deque)
        for call in scenario.get("mcp_calls", []):
            tool = call["tool"]
            response = call.get("response")
            latency_ms = call.get("latency_ms", 0)
            self._queues[tool].append((response, latency_ms))

        self._call_counts: dict[str, int] = defaultdict(int)
        logger.info(
            "DemoModeSource loaded scenario '%s' (%d tools, %d total calls)",
            scenario.get("name", "?"),
            len(self._queues),
            sum(len(q) for q in self._queues.values()),
        )

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _serve(self, tool: str, default: Any) -> Any:
        """
        Pop the next queued response for `tool`, sleep for realistic latency,
        and return the response. Falls back to `default` if the queue is empty.

        `tool` here is the *canonical* tool name as stored in the recording
        (e.g. "get-problem-by-id", "query-problems"). We normalise to allow
        callers to use either the raw or the friendly name.
        """
        queue = self._queues.get(tool)
        if not queue:
            # Try all known aliases for this tool.
            for alias in _TOOL_ALIASES.get(tool, []):
                if alias in self._queues and self._queues[alias]:
                    queue = self._queues[alias]
                    break

        if queue:
            response, latency_ms = queue.popleft()
            self._call_counts[tool] += 1
        else:
            logger.debug(
                "DemoModeSource: queue exhausted for tool '%s', returning default",
                tool,
            )
            response = default
            latency_ms = 200  # lightweight fallback latency

        if self._realistic_latency:
            jitter = random.uniform(0.8, 1.2)
            sleep_s = min(latency_ms / 1000.0 * jitter, _MAX_LATENCY_S)
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

        return response

    def _empty_list(self) -> list:
        return []

    def _empty_dict(self) -> dict:
        return {}

    # ------------------------------------------------------------------ #
    #  Public API — matches tools/dynatrace_mcp.py signatures              #
    # ------------------------------------------------------------------ #

    async def list_problems(self, status: str = "OPEN") -> list:
        """List open/active Dynatrace problems."""
        result = await self._serve("query-problems", self._empty_list())
        if isinstance(result, list):
            return result
        return []

    async def get_problem_details(self, problem_id: str) -> dict:
        """Fetch details for a specific Dynatrace problem."""
        result = await self._serve("get-problem-by-id", self._empty_dict())
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and result:
            return result[0] if isinstance(result[0], dict) else {}
        return {}

    async def query_metrics(
        self,
        metric_selector: str,
        entity_selector: str | None = None,
        from_time: str = "now-1h",
        to_time: str = "now",
        resolution: str = "1m",
    ) -> dict:
        """Query Dynatrace metric time-series via DQL / metrics API."""
        result = await self._serve("execute-dql", self._empty_dict())
        if isinstance(result, dict):
            return result
        # Raw recordings sometimes store DQL errors as strings — return as-is
        # wrapped in a dict so callers can inspect.
        if isinstance(result, str):
            return {"raw": result}
        return {}

    async def get_traces(
        self,
        service_name: str,
        from_time: str = "now-30m",
        to_time: str = "now",
        limit: int = 20,
    ) -> list:
        """Fetch distributed traces for a service."""
        result = await self._serve("get-traces", self._empty_list())
        if isinstance(result, list):
            return result
        return []

    async def list_entities(self, entity_type: str = "SERVICE") -> list:
        """List Dynatrace monitored entities."""
        result = await self._serve("get-entity-id", self._empty_list())
        if isinstance(result, list):
            return result
        return []

    async def get_service_response_time(self, service_name: str) -> dict:
        """Convenience: p99 response time for a service."""
        result = await self._serve("execute-dql", self._empty_dict())
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            return {"raw": result}
        return {}

    async def get_error_rate(self, service_name: str) -> dict:
        """Convenience: error rate for a service."""
        result = await self._serve("execute-dql", self._empty_dict())
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            return {"raw": result}
        return {}

    # ------------------------------------------------------------------ #
    #  Introspection                                                        #
    # ------------------------------------------------------------------ #

    def remaining_calls(self) -> dict[str, int]:
        """Return count of remaining queued responses per tool."""
        return {tool: len(q) for tool, q in self._queues.items() if q}

    def call_counts(self) -> dict[str, int]:
        """Return how many times each tool has been called during playback."""
        return dict(self._call_counts)

    @property
    def scenario_name(self) -> str:
        return self._scenario.get("name", "unknown")

    @property
    def scenario_description(self) -> str:
        return self._scenario.get("description", "")


# ---------------------------------------------------------------------------
# Tool name alias mapping
# Raw recordings use the Dynatrace MCP wire names (e.g. "get-problem-by-id").
# This map lets _serve() find responses for either the raw or friendly name.
# ---------------------------------------------------------------------------
_TOOL_ALIASES: dict[str, list[str]] = {
    # friendly -> raw
    "list_problems": ["query-problems"],
    "get_problem_details": ["get-problem-by-id"],
    "query_metrics": ["execute-dql"],
    "get_traces": ["get-traces"],
    "list_entities": ["get-entity-id"],
    "get_service_response_time": ["execute-dql"],
    "get_error_rate": ["execute-dql"],
    # raw -> friendly (reverse lookup for _serve() called with raw name)
    "query-problems": ["list_problems"],
    "get-problem-by-id": ["get_problem_details"],
    "execute-dql": ["query_metrics", "get_service_response_time", "get_error_rate"],
    "get-traces": ["get_traces"],
    "get-entity-id": ["list_entities"],
}
