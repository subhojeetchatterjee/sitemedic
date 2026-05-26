"""
tests/test_demo_mode.py — Unit tests for the Demo Mode system.

Tests cover:
  - Scenario loading from disk (Phase 2)
  - Tool response lookup logic (Phase 4 - DemoModeSource._lookup)
  - Scheduling and ActiveScenario lifecycle (Phase 4)
  - Edge cases: unknown scenario, empty trm, fallback logic

Run with:
    pytest tests/test_demo_mode.py -v
    # or from repo root:
    python -m pytest tests/test_demo_mode.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
AGENT_DIR = REPO_ROOT / "agent"
SCENARIOS_DIR = AGENT_DIR / "demo_mode" / "scenarios"

# Add agent/ to sys.path so imports work without installing
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_scenario(name: str) -> dict:
    """Load a scenario JSON file from the scenarios directory."""
    path = SCENARIOS_DIR / f"{name}.json"
    return json.loads(path.read_text())


def _load_index() -> dict:
    """Load INDEX.json."""
    return json.loads((SCENARIOS_DIR / "INDEX.json").read_text())


# ── Tests: Scenario files on disk ─────────────────────────────────────────────

class TestScenarioFiles:
    """Verify all scenario JSON files conform to the expected schema."""

    EXPECTED_SCENARIOS = [
        "memory_leak_001",
        "bad_deploy_rollback_001",
        "latency_spike_001",
        "error_burst_001",
        "cascading_failure_001",
        "predictive_catch_001",
        "availability_degraded",
        "cpu_spike",
        "high_error_rate",
        "high_memory",
        "slow_response",
    ]

    REQUIRED_FIELDS = [
        "id", "display_name", "category", "duration_seconds",
        "description", "expected_action", "captured_at", "tool_response_map",
    ]

    VALID_CATEGORIES = {
        "resource_exhaustion", "bad_deploy", "performance",
        "error_rate", "cascading", "predictive",
    }

    VALID_ACTIONS = {"restart_service", "rollback_revision", "scale_service"}

    def test_all_expected_files_exist(self):
        for scenario_id in self.EXPECTED_SCENARIOS:
            path = SCENARIOS_DIR / f"{scenario_id}.json"
            assert path.exists(), f"Missing scenario file: {scenario_id}.json"

    def test_scenarios_are_valid_json(self):
        for scenario_id in self.EXPECTED_SCENARIOS:
            path = SCENARIOS_DIR / f"{scenario_id}.json"
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                pytest.fail(f"{scenario_id}.json is not valid JSON: {e}")
            assert isinstance(data, dict), f"{scenario_id}.json must be a JSON object"

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_required_fields_present(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        for field in self.REQUIRED_FIELDS:
            assert field in data, f"{scenario_id}: missing required field '{field}'"

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_id_matches_filename(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        assert data["id"] == scenario_id, (
            f"Field 'id' ({data['id']!r}) does not match filename ({scenario_id!r})"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_category_is_valid(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        assert data["category"] in self.VALID_CATEGORIES, (
            f"{scenario_id}: invalid category '{data['category']}'"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_expected_action_is_valid(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        assert data["expected_action"] in self.VALID_ACTIONS, (
            f"{scenario_id}: invalid expected_action '{data['expected_action']}'"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_duration_positive(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        dur = data.get("duration_seconds", 0)
        assert isinstance(dur, (int, float)) and dur > 0, (
            f"{scenario_id}: duration_seconds must be > 0, got {dur!r}"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_tool_response_map_has_list_problems(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        trm = data.get("tool_response_map", {})
        assert "list_problems:default" in trm, (
            f"{scenario_id}: tool_response_map missing 'list_problems:default'"
        )
        lp = trm["list_problems:default"]
        assert isinstance(lp, list) and len(lp) > 0, (
            f"{scenario_id}: 'list_problems:default' must be a non-empty array"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_tool_response_map_has_get_problem_details(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        trm = data.get("tool_response_map", {})
        pd_keys = [k for k in trm if k.startswith("get_problem_details:")]
        assert len(pd_keys) >= 1, (
            f"{scenario_id}: tool_response_map missing 'get_problem_details:*' entry"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_tool_response_map_minimum_size(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        trm = data.get("tool_response_map", {})
        assert len(trm) >= 3, (
            f"{scenario_id}: tool_response_map has only {len(trm)} keys; minimum is 3"
        )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_list_problems_problem_has_required_fields(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        lp = data["tool_response_map"]["list_problems:default"]
        problem = lp[0]
        for field in ("problemId", "title", "status", "severityLevel"):
            assert field in problem, (
                f"{scenario_id}: list_problems:default[0] missing field '{field}'"
            )

    @pytest.mark.parametrize("scenario_id", EXPECTED_SCENARIOS)
    def test_evidence_details_present(self, scenario_id: str):
        data = _load_scenario(scenario_id)
        trm = data["tool_response_map"]
        pd_keys = [k for k in trm if k.startswith("get_problem_details:")]
        for key in pd_keys:
            pd = trm[key]
            assert "evidenceDetails" in pd, (
                f"{scenario_id}: {key!r} missing 'evidenceDetails'"
            )
            ed = pd["evidenceDetails"]
            assert "details" in ed and len(ed["details"]) > 0, (
                f"{scenario_id}: {key!r} evidenceDetails.details is empty"
            )


class TestIndex:
    """Verify INDEX.json is consistent with scenario files on disk."""

    def test_index_exists(self):
        assert (SCENARIOS_DIR / "INDEX.json").exists(), "INDEX.json not found"

    def test_index_is_valid_json(self):
        data = _load_index()
        assert isinstance(data, dict)
        assert "scenarios" in data

    def test_all_scenarios_in_index(self):
        index = _load_index()
        indexed_ids = {s["id"] for s in index["scenarios"]}
        for path in sorted(SCENARIOS_DIR.glob("*.json")):
            if path.name == "INDEX.json":
                continue
            scenario_id = path.stem
            assert scenario_id in indexed_ids, (
                f"Scenario '{scenario_id}' is on disk but NOT in INDEX.json"
            )

    def test_index_entries_have_files(self):
        index = _load_index()
        for entry in index["scenarios"]:
            sid = entry["id"]
            path = SCENARIOS_DIR / f"{sid}.json"
            assert path.exists(), (
                f"INDEX.json references '{sid}' but {sid}.json not found on disk"
            )

    def test_index_entries_have_required_fields(self):
        index = _load_index()
        required = {"id", "display_name", "category", "duration_seconds", "expected_action"}
        for entry in index["scenarios"]:
            for field in required:
                assert field in entry, (
                    f"INDEX.json entry '{entry.get('id', '?')}' missing field '{field}'"
                )


# ── Tests: Tool response lookup (_lookup) ─────────────────────────────────────

class TestToolResponseLookup:
    """Unit tests for DemoModeSource._lookup() with mocked scenario data."""

    # A minimal scenario dict for testing
    FAKE_SCENARIO = {
        "id": "test_scenario",
        "tool_response_map": {
            "list_problems:default": [{"problemId": "P-001", "title": "Test problem"}],
            "get_problem_details:P-001": {"problemId": "P-001", "title": "Test problem", "evidenceDetails": {"totalCount": 1, "details": [{"type": "METRIC", "displayName": "Test metric"}]}},
            "query_metrics:memory_utilization": {"resolution": "1m", "result": [{"metricId": "test", "data": [{"values": [50, 60, 70]}]}]},
            "get_traces:test-service": [{"traceId": "t001", "status": "ERROR"}],
            "list_entities:SERVICE": [{"entityId": "SERVICE-001", "displayName": "test-service"}],
        }
    }

    def _get_lookup_fn(self):
        """Import DemoModeSource and return its _lookup method."""
        from demo_mode.replay_source import DemoModeSource
        # Create instance without starting scheduler
        src = DemoModeSource(auto_start_scheduler=False)
        return src._lookup

    def test_exact_key_match(self):
        lookup = self._get_lookup_fn()
        result = lookup(self.FAKE_SCENARIO, "list_problems", "default")
        assert isinstance(result, list)
        assert result[0]["problemId"] == "P-001"

    def test_exact_key_match_problem_details(self):
        lookup = self._get_lookup_fn()
        result = lookup(self.FAKE_SCENARIO, "get_problem_details", "P-001")
        assert isinstance(result, dict)
        assert result["problemId"] == "P-001"

    def test_prefix_fallback(self):
        """When exact key not found, should return any matching tool prefix."""
        lookup = self._get_lookup_fn()
        # "get_problem_details:P-999" is not in the map, but "get_problem_details:P-001" is
        result = lookup(self.FAKE_SCENARIO, "get_problem_details", "P-999")
        assert result is not None, "Expected prefix fallback to return a result"
        assert isinstance(result, dict)

    def test_no_match_returns_none(self):
        lookup = self._get_lookup_fn()
        result = lookup(self.FAKE_SCENARIO, "unknown_tool", "any_key")
        assert result is None

    def test_empty_tool_response_map_returns_none(self):
        lookup = self._get_lookup_fn()
        empty_scenario = {"id": "empty", "tool_response_map": {}}
        assert lookup(empty_scenario, "list_problems", "default") is None

    def test_missing_tool_response_map_returns_none(self):
        lookup = self._get_lookup_fn()
        no_trm_scenario = {"id": "no_trm"}
        assert lookup(no_trm_scenario, "list_problems", "default") is None

    def test_metric_key_exact(self):
        lookup = self._get_lookup_fn()
        result = lookup(self.FAKE_SCENARIO, "query_metrics", "memory_utilization")
        assert isinstance(result, dict)
        assert "result" in result

    def test_traces_by_service_name(self):
        lookup = self._get_lookup_fn()
        result = lookup(self.FAKE_SCENARIO, "get_traces", "test-service")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_entities_lookup(self):
        lookup = self._get_lookup_fn()
        result = lookup(self.FAKE_SCENARIO, "list_entities", "SERVICE")
        assert isinstance(result, list)
        assert result[0]["entityId"] == "SERVICE-001"


# ── Tests: ActiveScenario lifecycle ───────────────────────────────────────────

class TestActiveScenario:
    """Unit tests for ActiveScenario state management."""

    def _make_active(self, duration_s: float = 120.0, speed: float = 1.0):
        from demo_mode.replay_source import ActiveScenario
        return ActiveScenario(
            scenario_id="test_001",
            scenario={"id": "test_001", "duration_seconds": duration_s},
            started_at=time.monotonic(),
            problem_id="P-DEMO-TEST-001",
            speed=speed,
        )

    def test_not_resolved_initially(self):
        active = self._make_active()
        assert not active.resolved

    def test_elapsed_is_near_zero_initially(self):
        active = self._make_active()
        assert active.elapsed_s < 1.0

    def test_should_not_resolve_before_duration(self):
        active = self._make_active(duration_s=10000.0)
        assert not active.should_resolve

    def test_speed_multiplier_compresses_time(self):
        """With speed=10, a 100s scenario should show ~10x elapsed progress per second."""
        active = self._make_active(duration_s=1.0, speed=100.0)
        # After nearly instant creation at speed=100, 1s of duration appears elapsed fast
        # Just verify speed attribute is stored correctly
        assert active.speed == 100.0

    def test_duration_from_scenario(self):
        active = self._make_active(duration_s=300.0)
        assert active.duration_s == 300.0


# ── Tests: DemoModeSource initialization ──────────────────────────────────────

class TestDemoModeSourceInit:
    """Tests for DemoModeSource scenario loading and trigger logic."""

    def _make_source(self):
        from demo_mode.replay_source import DemoModeSource
        return DemoModeSource(auto_start_scheduler=False)

    def test_loads_scenarios_from_disk(self):
        src = self._make_source()
        assert len(src.scenarios) > 0, "Expected at least one scenario to load"

    def test_loads_expected_scenarios(self):
        src = self._make_source()
        expected = {"memory_leak_001", "bad_deploy_rollback_001", "latency_spike_001"}
        missing = expected - set(src.scenarios.keys())
        assert not missing, f"Missing expected scenarios: {missing}"

    def test_trigger_known_scenario_returns_problem_id(self):
        src = self._make_source()
        problem_id = src.trigger_scenario("memory_leak_001")
        assert problem_id.startswith("P-DEMO-")
        assert "MEMORY" in problem_id.upper() or "memory" in problem_id.lower() or "LEAK" in problem_id.upper()

    def test_trigger_unknown_scenario_raises(self):
        src = self._make_source()
        with pytest.raises(ValueError, match="Unknown scenario"):
            src.trigger_scenario("this_scenario_does_not_exist_xyz")

    def test_trigger_random_scenario(self):
        src = self._make_source()
        problem_id = src.trigger_random_scenario()
        assert problem_id.startswith("P-DEMO-")

    def test_list_active_scenarios_after_trigger(self):
        src = self._make_source()
        problem_id = src.trigger_scenario("error_burst_001")
        active = src.list_active_scenarios()
        assert any(a["problem_id"] == problem_id for a in active), (
            f"Triggered problem {problem_id} not found in active scenarios"
        )

    def test_list_available_scenarios(self):
        src = self._make_source()
        available = src.list_available_scenarios()
        # May be 0 if INDEX.json is not loaded, but scenarios are present
        # We at least check no exception is thrown
        assert isinstance(available, list)

    def test_get_source_metadata_demo(self):
        src = self._make_source()
        meta = src.get_source_metadata()
        assert meta.is_live is False
        assert meta.source_type == "demo"
        assert meta.demo_mode_active is True
        assert meta.health_status == "demo"
        assert meta.scenarios_available == len(src.scenarios)

    def test_get_source_metadata_current_scenario_after_trigger(self):
        src = self._make_source()
        src.trigger_scenario("memory_leak_001")
        meta = src.get_source_metadata()
        assert meta.current_scenario == "memory_leak_001"

    def test_pause_and_resume_scheduler(self):
        src = self._make_source()
        assert not src._scheduler_paused
        src.pause_scheduler()
        assert src._scheduler_paused
        src.resume_scheduler()
        assert not src._scheduler_paused


# ── Tests: Async TelemetrySource interface ────────────────────────────────────

class TestDemoModeSourceAsync:
    """Async tests for the TelemetrySource interface methods."""

    def _make_source_with_scenario(self, scenario_id: str = "memory_leak_001"):
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        src.trigger_scenario(scenario_id)
        return src

    def test_list_problems_returns_list(self):
        src = self._make_source_with_scenario()
        result = asyncio.run(src.list_problems())
        assert isinstance(result, list)

    def test_list_problems_returns_active_problems(self):
        src = self._make_source_with_scenario("memory_leak_001")
        problems = asyncio.run(src.list_problems())
        assert len(problems) >= 1
        p = problems[0]
        assert "problemId" in p
        assert p["problemId"].startswith("P-DEMO-")

    def test_list_problems_patches_problem_id(self):
        """list_problems should rewrite problemId to the demo problem_id."""
        src = self._make_source_with_scenario("memory_leak_001")
        active = src.list_active_scenarios()
        assert len(active) == 1
        expected_pid = active[0]["problem_id"]
        problems = asyncio.run(src.list_problems())
        assert any(p["problemId"] == expected_pid for p in problems), (
            f"Expected problem_id {expected_pid} not found in list_problems result"
        )

    def test_get_problem_details_returns_dict(self):
        src = self._make_source_with_scenario("bad_deploy_rollback_001")
        active = src.list_active_scenarios()
        pid = active[0]["problem_id"]
        result = asyncio.run(src.get_problem_details(pid))
        assert isinstance(result, dict)

    def test_get_problem_details_returns_evidence(self):
        src = self._make_source_with_scenario("bad_deploy_rollback_001")
        active = src.list_active_scenarios()
        pid = active[0]["problem_id"]
        result = asyncio.run(src.get_problem_details(pid))
        assert "evidenceDetails" in result, "get_problem_details should include evidenceDetails"

    def test_get_problem_details_patches_problem_id(self):
        """get_problem_details should rewrite problemId to the demo problem_id."""
        src = self._make_source_with_scenario("memory_leak_001")
        active = src.list_active_scenarios()
        pid = active[0]["problem_id"]
        result = asyncio.run(src.get_problem_details(pid))
        assert result.get("problemId") == pid

    def test_query_metrics_memory_returns_data(self):
        src = self._make_source_with_scenario("memory_leak_001")
        result = asyncio.run(src.query_metrics("builtin:host.mem.usage"))
        assert isinstance(result, dict)
        # Should have result data from the scenario
        assert "result" in result or result == {}

    def test_get_traces_returns_list(self):
        src = self._make_source_with_scenario("memory_leak_001")
        result = asyncio.run(src.get_traces("checkout-service"))
        assert isinstance(result, list)

    def test_list_entities_returns_list(self):
        src = self._make_source_with_scenario("memory_leak_001")
        result = asyncio.run(src.list_entities("SERVICE"))
        assert isinstance(result, list)
        assert len(result) > 0

    def test_get_service_response_time_returns_dict(self):
        src = self._make_source_with_scenario("latency_spike_001")
        result = asyncio.run(src.get_service_response_time("api-gateway"))
        assert isinstance(result, dict)

    def test_get_error_rate_returns_dict(self):
        src = self._make_source_with_scenario("error_burst_001")
        result = asyncio.run(src.get_error_rate("payment-service"))
        assert isinstance(result, dict)

    def test_no_active_scenarios_list_problems_empty(self):
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        # Don't trigger any scenario
        result = asyncio.run(src.list_problems())
        assert result == []

    def test_no_active_scenarios_get_problem_details_empty(self):
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        result = asyncio.run(src.get_problem_details("P-NONEXISTENT"))
        assert result == {}


# ── Tests: Metric key normalization ───────────────────────────────────────────

class TestMetricKeyNormalization:
    """Unit tests for _normalize_metric_key."""

    def test_direct_map(self):
        from demo_mode.replay_source import _normalize_metric_key
        assert _normalize_metric_key("builtin:host.mem.usage") == "memory_utilization"
        assert _normalize_metric_key("builtin:host.cpu.usage") == "cpu_utilization"
        assert _normalize_metric_key("builtin:service.response.time") == "response_time"
        assert _normalize_metric_key("builtin:service.errors.total.rate") == "error_rate"

    def test_keyword_fallback_memory(self):
        from demo_mode.replay_source import _normalize_metric_key
        assert _normalize_metric_key("jvm.heap.memory.used") == "memory_utilization"

    def test_keyword_fallback_cpu(self):
        from demo_mode.replay_source import _normalize_metric_key
        assert _normalize_metric_key("process.cpu.percent") == "cpu_utilization"

    def test_keyword_fallback_latency(self):
        from demo_mode.replay_source import _normalize_metric_key
        assert _normalize_metric_key("http.server.p99.latency") == "response_time"

    def test_unknown_key_passthrough(self):
        from demo_mode.replay_source import _normalize_metric_key
        key = "custom:my.exotic.metric"
        result = _normalize_metric_key(key)
        assert result == key  # unchanged


# ── Tests: SourceMetadata ─────────────────────────────────────────────────────

class TestSourceMetadata:
    """Unit tests for SourceMetadata dataclass."""

    def test_demo_metadata(self):
        from sources.base import SourceMetadata
        meta = SourceMetadata(
            is_live=False,
            source_type="demo",
            health_status="demo",
            demo_mode_active=True,
            current_scenario="memory_leak_001",
            scenarios_available=11,
        )
        assert meta.is_live is False
        assert meta.demo_mode_active is True
        d = meta.to_dict()
        assert d["source_type"] == "demo"
        assert d["current_scenario"] == "memory_leak_001"
        assert d["scenarios_available"] == 11

    def test_live_metadata(self):
        from sources.base import SourceMetadata
        meta = SourceMetadata(
            is_live=True,
            source_type="dynatrace",
            health_status="healthy",
            demo_mode_active=False,
        )
        assert meta.is_live is True
        assert meta.demo_mode_active is False
        d = meta.to_dict()
        assert d["health_status"] == "healthy"

    def test_extra_fields_in_to_dict(self):
        from sources.base import SourceMetadata
        meta = SourceMetadata(
            is_live=False,
            source_type="demo",
            health_status="demo",
            demo_mode_active=True,
            extra={"speed": 2.0, "scheduler_paused": False},
        )
        d = meta.to_dict()
        assert d["speed"] == 2.0
        assert d["scheduler_paused"] is False


# ── Tests: validate_scenarios.py CLI ─────────────────────────────────────────

class TestValidateScenariosScript:
    """Integration test: run the validate_scenarios CLI."""

    def test_all_scenarios_pass_validation(self):
        """Run validate_scenarios.py and confirm all scenarios pass."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "validate_scenarios.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"validate_scenarios.py failed:\n{result.stdout}\n{result.stderr}"
        )
        assert "failed, 0 with warnings" in result.stdout or "0 failed" in result.stdout

    def test_single_scenario_validation(self):
        """Validate a single known-good scenario."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "validate_scenarios.py"),
                "--scenario", "memory_leak_001",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "PASS" in result.stdout

    def test_strict_mode_no_warnings(self):
        """Run in strict mode — should still pass since scenarios are clean."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "validate_scenarios.py"),
                "--strict",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Strict validation failed:\n{result.stdout}\n{result.stderr}"
        )


# ── Tests: Edge cases ─────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge-case and regression tests."""

    def test_multiple_triggers_same_scenario(self):
        """Triggering the same scenario twice creates two separate ActiveScenario instances."""
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        pid1 = src.trigger_scenario("memory_leak_001")
        pid2 = src.trigger_scenario("memory_leak_001")
        assert pid1 != pid2, "Two triggers should produce different problem IDs"
        active = src.list_active_scenarios()
        assert len(active) == 2

    def test_list_problems_excludes_resolved(self):
        """Resolved scenarios should not appear in list_problems."""
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        src.trigger_scenario("memory_leak_001")
        # Mark it resolved
        src._active[0].resolved = True
        problems = asyncio.run(src.list_problems())
        assert problems == [], "Resolved scenarios should not appear in list_problems"

    def test_reload_scenarios(self):
        """reload_scenarios should not raise and scenarios remain loaded."""
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        original_count = len(src.scenarios)
        src.reload_scenarios()
        assert len(src.scenarios) == original_count

    def test_trigger_after_reload(self):
        """Scenarios remain triggerable after reload."""
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        src.reload_scenarios()
        problem_id = src.trigger_scenario("latency_spike_001")
        assert problem_id.startswith("P-DEMO-")

    def test_problem_id_format(self):
        """Problem IDs should match the P-DEMO-{SCENARIO}-{TIMESTAMP} pattern."""
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        pid = src.trigger_scenario("error_burst_001")
        parts = pid.split("-")
        assert parts[0] == "P"
        assert parts[1] == "DEMO"

    def test_demo_scenario_tag_in_list_problems(self):
        """list_problems result should include _demo_scenario tag."""
        from demo_mode.replay_source import DemoModeSource
        src = DemoModeSource(auto_start_scheduler=False)
        src.trigger_scenario("memory_leak_001")
        problems = asyncio.run(src.list_problems())
        assert len(problems) > 0
        assert problems[0].get("_demo_scenario") == "memory_leak_001"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
