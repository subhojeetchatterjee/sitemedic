"""
DemoModeSource (replay engine) — Phase 4.

A full TelemetrySource implementation that replays curated scenario files
instead of hitting a live Dynatrace MCP server.

Key behaviours
--------------
* On init, loads INDEX.json + all scenario JSON files from disk.
* Runs a background scenario scheduler: fires one scenario every
  `schedule_interval_s` seconds (default 480s = 8 min).
* Supports on-demand trigger: operator calls trigger_scenario(id) → problem_id.
* list_problems() returns active (non-resolved) scenario problems.
* All other tool calls look up responses from tool_response_map using
  smart key matching (exact → prefix → any).
* Speed multiplier (1x, 2x, 5x) compresses the schedule interval.
* Implements TelemetrySource so it's drop-in with DynatraceMCPSource.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sources.base import TelemetrySource, SourceMetadata

logger = logging.getLogger(__name__)

_SCENARIOS_DIR = Path(__file__).parent / "scenarios"
_INDEX_PATH = _SCENARIOS_DIR / "INDEX.json"
_DEFAULT_SCHEDULE_INTERVAL_S = 480  # 8 minutes
_FAKE_LATENCY_S = 0.05  # 50 ms simulated network latency


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ActiveScenario:
    scenario_id: str
    scenario: dict         # full loaded JSON
    started_at: float      # time.monotonic()
    problem_id: str        # e.g. "P-DEMO-memory_leak_001-20260525T143000"
    speed: float = 1.0
    resolved: bool = False

    @property
    def elapsed_s(self) -> float:
        return (time.monotonic() - self.started_at) * self.speed

    @property
    def duration_s(self) -> float:
        return float(self.scenario.get("duration_seconds", 480))

    @property
    def should_resolve(self) -> bool:
        return self.elapsed_s >= self.duration_s


# ── DemoModeSource ───────────────────────────────────────────────────────────

class DemoModeSource(TelemetrySource):
    """
    TelemetrySource implementation that replays curated scenario files.

    This is the primary demo mode implementation (Phase 4+).
    The simpler DemoModeSource in demo_mode/source.py is kept for backward
    compatibility with the old DemoRunner, but new code should use this class.
    """

    def __init__(
        self,
        scenarios_dir: Path = _SCENARIOS_DIR,
        schedule_interval_s: float = _DEFAULT_SCHEDULE_INTERVAL_S,
        speed: float = 1.0,
        auto_start_scheduler: bool = True,
    ) -> None:
        self._scenarios_dir = scenarios_dir
        self._schedule_interval_s = schedule_interval_s
        self._speed = speed
        self._scenarios: dict[str, dict] = {}   # id → scenario dict
        self._index: list[dict] = []             # from INDEX.json
        self._active: list[ActiveScenario] = []
        self._rotation_idx: int = 0
        self._trigger_counter: int = 0
        self._scheduler_paused: bool = False
        self._scheduler_task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None  # created lazily inside event loop

        self._load_all_scenarios()

        if auto_start_scheduler and self._scenarios:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    self._scheduler_task = loop.create_task(self._scheduler_loop())
            except RuntimeError:
                pass  # no event loop yet — caller must call start() manually

        logger.info(
            "DemoModeSource initialised — %d scenarios loaded, speed=%.1fx, interval=%ds",
            len(self._scenarios), speed, int(schedule_interval_s),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Explicitly start the scheduler (useful when loop is not yet running at init time)."""
        if self._scheduler_task is None and self._scenarios:
            self._scheduler_task = asyncio.get_event_loop().create_task(self._scheduler_loop())
            logger.info("DemoModeSource scheduler started")

    def stop(self) -> None:
        """Cancel the scheduler task."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            logger.info("DemoModeSource scheduler stopped")

    def pause_scheduler(self) -> None:
        """Pause automatic scenario rotation without stopping it."""
        self._scheduler_paused = True
        logger.info("DemoModeSource scheduler paused")

    def resume_scheduler(self) -> None:
        """Resume automatic scenario rotation."""
        self._scheduler_paused = False
        logger.info("DemoModeSource scheduler resumed")

    # ── Scenario loading ──────────────────────────────────────────────────

    def _load_all_scenarios(self) -> None:
        """Load INDEX.json and all scenario JSON files from disk."""
        if not self._scenarios_dir.exists():
            logger.warning("Scenarios directory not found: %s", self._scenarios_dir)
            return

        # Load index
        if _INDEX_PATH.exists():
            try:
                with _INDEX_PATH.open("r", encoding="utf-8") as fh:
                    index_data = json.load(fh)
                    self._index = index_data.get("scenarios", [])
            except Exception as exc:
                logger.warning("Failed to load INDEX.json: %s", exc)

        # Load every scenario file (except INDEX.json)
        for path in sorted(self._scenarios_dir.glob("*.json")):
            if path.name == "INDEX.json":
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    scenario = json.load(fh)
                sid = scenario.get("id") or path.stem
                self._scenarios[sid] = scenario
                logger.debug("Loaded scenario: %s", sid)
            except Exception as exc:
                logger.warning("Failed to load scenario %s: %s", path.name, exc)

    def reload_scenarios(self) -> None:
        """Reload all scenario files from disk (hot-reload without restart)."""
        self._scenarios.clear()
        self._index.clear()
        self._load_all_scenarios()
        logger.info("DemoModeSource: reloaded %d scenarios", len(self._scenarios))

    # ── Scheduler ─────────────────────────────────────────────────────────

    async def _scheduler_loop(self) -> None:
        """Background task: periodically rotate through scenarios."""
        # Wait a brief moment before the first auto-fire so the agent has
        # fully initialised its orchestrator and Firestore connection.
        await asyncio.sleep(5)

        scenario_ids = list(self._scenarios.keys())
        if not scenario_ids:
            logger.warning("DemoModeSource: no scenarios to schedule")
            return

        while True:
            try:
                interval = self._schedule_interval_s / max(self._speed, 0.1)

                # Expire resolved scenarios
                if self._lock is None:
                    self._lock = asyncio.Lock()
                async with self._lock:
                    for active in self._active:
                        if not active.resolved and active.should_resolve:
                            active.resolved = True
                            logger.info(
                                "DemoModeSource: scenario %s resolved (problem %s)",
                                active.scenario_id, active.problem_id,
                            )

                if not self._scheduler_paused:
                    # Rotate to next scenario
                    sid = scenario_ids[self._rotation_idx % len(scenario_ids)]
                    self._rotation_idx += 1
                    problem_id = self._start_scenario(sid)
                    logger.info(
                        "DemoModeSource: auto-scheduled scenario '%s' → %s",
                        sid, problem_id,
                    )

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("DemoModeSource scheduler error: %s", exc)
                await asyncio.sleep(30)  # back off on unexpected errors

    def _start_scenario(self, scenario_id: str) -> str:
        """
        Activate a scenario and return its demo problem_id.
        Internal — does not acquire the lock (caller must if needed).
        """
        if scenario_id not in self._scenarios:
            raise ValueError(f"Unknown scenario: {scenario_id!r}")

        scenario = self._scenarios[scenario_id]
        self._trigger_counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        problem_id = f"P-DEMO-{scenario_id.upper().replace('_', '-')}-{ts}-{self._trigger_counter}"

        active = ActiveScenario(
            scenario_id=scenario_id,
            scenario=scenario,
            started_at=time.monotonic(),
            problem_id=problem_id,
            speed=self._speed,
        )
        self._active.append(active)
        return problem_id

    def trigger_scenario(self, scenario_id: str) -> str:
        """
        Immediately start a specific scenario.
        Returns the demo problem_id so callers can track it in Firestore.
        Raises ValueError if the scenario_id is unknown.
        """
        if scenario_id not in self._scenarios:
            available = sorted(self._scenarios.keys())
            raise ValueError(
                f"Unknown scenario: {scenario_id!r}. Available: {available}"
            )
        problem_id = self._start_scenario(scenario_id)
        logger.info(
            "DemoModeSource: triggered scenario '%s' on-demand → %s",
            scenario_id, problem_id,
        )
        return problem_id

    def set_speed(self, speed: float) -> None:
        """Set the replay speed multiplier (1.0 = real-time, 2.0 = 2x, 5.0 = 5x)."""
        if speed <= 0:
            raise ValueError(f"Speed must be > 0, got {speed}")
        self._speed = speed
        logger.info("DemoModeSource: speed set to %.1fx", speed)

    def trigger_random_scenario(self) -> str:
        """
        Pick a random scenario and start it immediately.
        Returns the problem_id.
        """
        import random
        if not self._scenarios:
            raise RuntimeError("No scenarios loaded")
        sid = random.choice(list(self._scenarios.keys()))
        return self.trigger_scenario(sid)

    # ── Tool response lookup ──────────────────────────────────────────────

    def _lookup(self, scenario: dict, tool: str, key_suffix: str) -> Any | None:
        """
        Look up a response in tool_response_map.

        Resolution order:
        1. Exact match:   f"{tool}:{key_suffix}"
        2. Prefix match:  any key starting with f"{tool}:"
        3. None
        """
        trm: dict = scenario.get("tool_response_map", {})
        if not trm:
            return None

        # Exact match
        exact_key = f"{tool}:{key_suffix}"
        if exact_key in trm:
            return trm[exact_key]

        # Prefix match (first match wins)
        prefix = f"{tool}:"
        for k, v in trm.items():
            if k.startswith(prefix):
                logger.debug(
                    "DemoModeSource: key '%s' not found, using fallback '%s'", exact_key, k
                )
                return v

        return None

    def _active_for_problem(self, problem_id: str) -> ActiveScenario | None:
        """Find the ActiveScenario whose problem_id matches."""
        for active in self._active:
            if active.problem_id == problem_id:
                return active
        return None

    def _expire_resolved(self) -> None:
        """Mark any time-elapsed scenarios as resolved immediately."""
        for active in self._active:
            if not active.resolved and active.should_resolve:
                active.resolved = True

    def _first_active_scenario(self) -> ActiveScenario | None:
        """Return the first non-resolved active scenario."""
        self._expire_resolved()
        for active in self._active:
            if not active.resolved:
                return active
        return None

    async def _fake_latency(self) -> None:
        await asyncio.sleep(_FAKE_LATENCY_S)

    # ── TelemetrySource interface ─────────────────────────────────────────

    async def list_problems(self, status: str = "OPEN") -> list[dict]:
        """
        Return problems for all currently-active (non-resolved) scenarios.

        Each active scenario contributes the problem(s) from its
        tool_response_map["list_problems:default"].
        """
        await self._fake_latency()

        # Expire resolved scenarios first
        for active in self._active:
            if not active.resolved and active.should_resolve:
                active.resolved = True
                logger.info(
                    "DemoModeSource.list_problems: scenario %s expired",
                    active.scenario_id,
                )

        problems: list[dict] = []
        for active in self._active:
            if active.resolved:
                continue
            raw = self._lookup(active.scenario, "list_problems", "default")
            if isinstance(raw, list):
                # Rewrite problemId to the demo problem_id so orchestrator
                # can track it in Firestore by the generated ID.
                for p in raw:
                    if isinstance(p, dict):
                        p_copy = dict(p)
                        p_copy["problemId"] = active.problem_id
                        p_copy.setdefault("_demo_scenario", active.scenario_id)
                        problems.append(p_copy)

        return problems

    async def get_problem_details(self, problem_id: str) -> dict:
        """Return problem details for a demo problem_id."""
        await self._fake_latency()

        active = self._active_for_problem(problem_id)
        if active is None:
            # Fallback: try any active scenario
            active = self._first_active_scenario()
        if active is None:
            return {}

        # Try the specific problem_id key first, then any get_problem_details key
        result = self._lookup(active.scenario, "get_problem_details", problem_id)
        if result is None:
            result = {}

        if isinstance(result, dict):
            # Patch the problemId to match the demo ID
            result = dict(result)
            result["problemId"] = problem_id
            result.setdefault("_demo_scenario", active.scenario_id)
        return result

    async def query_metrics(
        self,
        metric_selector: str,
        entity_selector: str | None = None,
        from_time: str = "now-1h",
        to_time: str = "now",
        resolution: str = "1m",
    ) -> dict:
        """Return pre-recorded metric time-series data."""
        await self._fake_latency()

        active = self._first_active_scenario()
        if active is None:
            return {}

        # Normalize the metric selector to a short key for lookup
        # e.g. "builtin:host.mem.usage" → "memory_utilization"
        normalized = _normalize_metric_key(metric_selector)
        result = self._lookup(active.scenario, "query_metrics", normalized)
        if result is None:
            # Try the raw selector
            result = self._lookup(active.scenario, "query_metrics", metric_selector)
        if result is None:
            result = {}
        return result if isinstance(result, dict) else {}

    async def get_traces(
        self,
        service_name: str,
        from_time: str = "now-30m",
        to_time: str = "now",
        limit: int = 20,
    ) -> list[dict]:
        """Return pre-recorded distributed traces."""
        await self._fake_latency()

        active = self._first_active_scenario()
        if active is None:
            return []

        result = self._lookup(active.scenario, "get_traces", service_name)
        if result is None:
            result = []
        return result if isinstance(result, list) else []

    async def list_entities(self, entity_type: str = "SERVICE") -> list[dict]:
        """Return pre-recorded entity list."""
        await self._fake_latency()

        active = self._first_active_scenario()
        if active is None:
            return []

        result = self._lookup(active.scenario, "list_entities", entity_type)
        if result is None:
            result = self._lookup(active.scenario, "list_entities", "SERVICE")
        if result is None:
            result = []
        return result if isinstance(result, list) else []

    async def get_service_response_time(self, service_name: str) -> dict:
        """Return pre-recorded response time data."""
        await self._fake_latency()

        active = self._first_active_scenario()
        if active is None:
            return {}

        result = self._lookup(active.scenario, "query_metrics", "response_time")
        if result is None:
            result = self._lookup(active.scenario, "query_metrics", f"response_time_{service_name}")
        return result if isinstance(result, dict) else {}

    async def get_error_rate(self, service_name: str) -> dict:
        """Return pre-recorded error rate data."""
        await self._fake_latency()

        active = self._first_active_scenario()
        if active is None:
            return {}

        result = self._lookup(active.scenario, "query_metrics", "error_rate")
        if result is None:
            result = self._lookup(active.scenario, "query_metrics", f"error_rate_{service_name}")
        return result if isinstance(result, dict) else {}

    def get_source_metadata(self) -> SourceMetadata:
        self._expire_resolved()
        active = self._first_active_scenario()
        return SourceMetadata(
            is_live=False,
            source_type="demo",
            health_status="demo",
            demo_mode_active=True,
            current_scenario=active.scenario_id if active else None,
            scenarios_available=len(self._scenarios),
            extra={
                "active_scenarios": len([a for a in self._active if not a.resolved]),
                "scheduler_paused": self._scheduler_paused,
                "speed": self._speed,
            },
        )

    # ── Introspection ─────────────────────────────────────────────────────

    def list_available_scenarios(self) -> list[dict]:
        """Return the index metadata for all loaded scenarios."""
        return list(self._index)

    def list_active_scenarios(self) -> list[dict]:
        """Return summary of currently active (non-resolved) scenarios."""
        return [
            {
                "scenario_id": a.scenario_id,
                "problem_id": a.problem_id,
                "started_at": a.started_at,
                "elapsed_s": round(a.elapsed_s, 1),
                "duration_s": a.duration_s,
                "resolved": a.resolved,
            }
            for a in self._active
            if not a.resolved
        ]

    @property
    def scenarios(self) -> dict[str, dict]:
        return dict(self._scenarios)


# ── Metric key normalization ─────────────────────────────────────────────────

_METRIC_KEY_MAP = {
    "builtin:host.mem.usage": "memory_utilization",
    "builtin:host.cpu.usage": "cpu_utilization",
    "builtin:service.response.time": "response_time",
    "builtin:service.errors.total.rate": "error_rate",
    "builtin:service.requestCount.total": "request_throughput",
    "dt.service.request.response_time": "response_time",
    "dt.service.request.failure_rate": "error_rate",
    "jvm:gc.pause.duration": "gc_pause_duration",
    "dt.runtime.exception.count": "exception_count",
    "http.client.connection.pool.active": "connection_pool",
}

_METRIC_KEYWORD_MAP = [
    (["mem", "memory", "heap"], "memory_utilization"),
    (["cpu", "proc"], "cpu_utilization"),
    (["response", "latency", "p99", "p90", "p50"], "response_time"),
    (["error", "fail", "failure"], "error_rate"),
    (["request", "count", "throughput", "rps"], "request_throughput"),
    (["gc", "garbage"], "gc_pause_duration"),
    (["exception", "npe"], "exception_count"),
    (["connection", "pool"], "connection_pool"),
    (["trend"], "cpu_trend"),
]


def _normalize_metric_key(metric_selector: str) -> str:
    """
    Map a Dynatrace metric selector string to a short human-readable key
    used in tool_response_map.

    Falls back to the raw selector if no mapping is found.
    """
    # Direct map
    if metric_selector in _METRIC_KEY_MAP:
        return _METRIC_KEY_MAP[metric_selector]

    # Keyword-based map (case-insensitive)
    lower = metric_selector.lower()
    for keywords, key in _METRIC_KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return key

    return metric_selector
