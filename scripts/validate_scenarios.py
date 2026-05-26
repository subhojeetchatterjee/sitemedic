#!/usr/bin/env python3
"""
validate_scenarios.py — Validate all curated demo scenario files.

Loads every *.json file in agent/demo_mode/scenarios/ (excluding INDEX.json),
validates schema correctness, confirms minimum required data, and reports
broken scenarios with actionable error messages.

Usage:
    python scripts/validate_scenarios.py
    python scripts/validate_scenarios.py --strict   # fail on warnings too
    python scripts/validate_scenarios.py --scenario memory_leak_001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ── Path resolution ──────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
SCENARIOS_DIR = REPO_ROOT / "agent" / "demo_mode" / "scenarios"

# ── Schema constants ─────────────────────────────────────────────────────────

REQUIRED_TOP_LEVEL = [
    "id", "display_name", "category", "duration_seconds",
    "description", "expected_action", "captured_at",
    "tool_response_map",
]

VALID_CATEGORIES = {
    "resource_exhaustion", "bad_deploy", "performance",
    "error_rate", "cascading", "predictive",
}

VALID_EXPECTED_ACTIONS = {
    "restart_service", "rollback_revision", "scale_service",
    "purge_pubsub_subscription_backlog",
}

REQUIRED_TOOL_KEYS_MIN = {
    "list_problems:default",
}

VALID_TOOL_PREFIXES = {
    "list_problems:",
    "get_problem_details:",
    "query_metrics:",
    "get_traces:",
    "list_entities:",
}

MIN_TOOL_RESPONSE_KEYS = 3  # must have at least 3 tool entries


# ── Validation logic ─────────────────────────────────────────────────────────

class ValidationResult:
    def __init__(self, scenario_id: str, path: Path):
        self.scenario_id = scenario_id
        self.path = path
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def __str__(self) -> str:
        status = "PASS" if self.valid else "FAIL"
        lines = [f"[{status}] {self.scenario_id} ({self.path.name})"]
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        if self.valid and not self.warnings:
            lines.append("  All checks passed.")
        return "\n".join(lines)


def validate_scenario(path: Path) -> ValidationResult:
    """Validate a single scenario JSON file. Returns a ValidationResult."""
    scenario_id = path.stem
    result = ValidationResult(scenario_id, path)

    # ── Load JSON ──────────────────────────────────────────────────────────
    try:
        with path.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
    except json.JSONDecodeError as exc:
        result.error(f"Invalid JSON: {exc}")
        return result
    except Exception as exc:
        result.error(f"Cannot read file: {exc}")
        return result

    # ── Required top-level fields ──────────────────────────────────────────
    for field in REQUIRED_TOP_LEVEL:
        if field not in data:
            result.error(f"Missing required field: '{field}'")

    if result.errors:
        return result  # no point continuing if basic structure is broken

    # ── ID matches filename ────────────────────────────────────────────────
    if data["id"] != scenario_id:
        result.error(
            f"Field 'id' ({data['id']!r}) does not match filename ({scenario_id!r})"
        )

    # ── Category ───────────────────────────────────────────────────────────
    if data["category"] not in VALID_CATEGORIES:
        result.error(
            f"Invalid category: {data['category']!r}. Must be one of: {sorted(VALID_CATEGORIES)}"
        )

    # ── Expected action ────────────────────────────────────────────────────
    if data["expected_action"] not in VALID_EXPECTED_ACTIONS:
        result.error(
            f"Invalid expected_action: {data['expected_action']!r}. Must be one of: {sorted(VALID_EXPECTED_ACTIONS)}"
        )

    # ── Duration ───────────────────────────────────────────────────────────
    duration = data.get("duration_seconds", 0)
    if not isinstance(duration, (int, float)) or duration <= 0:
        result.error(f"'duration_seconds' must be a positive number, got: {duration!r}")
    elif duration < 60:
        result.warn(f"'duration_seconds' is very short ({duration}s) — is this intentional?")

    # ── tool_response_map ──────────────────────────────────────────────────
    trm = data.get("tool_response_map", {})
    if not isinstance(trm, dict):
        result.error("'tool_response_map' must be a JSON object (dict)")
        return result

    # Must have list_problems:default
    if "list_problems:default" not in trm:
        result.error("'tool_response_map' must contain 'list_problems:default'")

    # Must have at least one get_problem_details entry
    pd_keys = [k for k in trm if k.startswith("get_problem_details:")]
    if not pd_keys:
        result.error("'tool_response_map' must contain at least one 'get_problem_details:{id}' entry")

    # Minimum number of tool keys
    if len(trm) < MIN_TOOL_RESPONSE_KEYS:
        result.error(
            f"'tool_response_map' has only {len(trm)} keys — minimum required is {MIN_TOOL_RESPONSE_KEYS}"
        )

    # Validate key prefixes
    for key in trm:
        prefix_ok = any(key.startswith(pfx) for pfx in VALID_TOOL_PREFIXES)
        if not prefix_ok:
            result.warn(
                f"Unknown tool key prefix in 'tool_response_map': {key!r}. "
                f"Expected one of: {sorted(VALID_TOOL_PREFIXES)}"
            )

    # list_problems:default must return a non-empty list
    lp_default = trm.get("list_problems:default")
    if not isinstance(lp_default, list):
        result.error("'list_problems:default' must be a JSON array")
    elif len(lp_default) == 0:
        result.error("'list_problems:default' must contain at least one problem object")
    else:
        problem = lp_default[0]
        if not isinstance(problem, dict):
            result.error("'list_problems:default[0]' must be a JSON object (dict)")
        else:
            for req_key in ("problemId", "title", "status", "severityLevel"):
                if req_key not in problem:
                    result.warn(
                        f"'list_problems:default[0]' is missing recommended field: '{req_key}'"
                    )

    # Validate problem_details response shape
    for pd_key in pd_keys:
        pd_val = trm[pd_key]
        if not isinstance(pd_val, dict):
            result.error(f"'{pd_key}' must be a JSON object (dict)")
        else:
            for req_key in ("problemId", "title", "severityLevel", "evidenceDetails"):
                if req_key not in pd_val:
                    result.warn(f"'{pd_key}' is missing recommended field: '{req_key}'")

    # Validate entities response
    entity_keys = [k for k in trm if k.startswith("list_entities:")]
    if not entity_keys:
        result.warn("'tool_response_map' has no 'list_entities:*' entries — Gemini may lack service topology")

    # Validate traces response
    trace_keys = [k for k in trm if k.startswith("get_traces:")]
    if not trace_keys:
        result.warn("'tool_response_map' has no 'get_traces:*' entries — Gemini may lack trace data")

    # Validate metrics responses
    metric_keys = [k for k in trm if k.startswith("query_metrics:")]
    if not metric_keys:
        result.warn("'tool_response_map' has no 'query_metrics:*' entries — Gemini may lack time-series data")

    # ── Descriptions ───────────────────────────────────────────────────────
    if len(data.get("description", "")) < 20:
        result.warn("'description' is very short — add more context for the demo UI")

    if len(data.get("display_name", "")) < 5:
        result.warn("'display_name' is very short — should be human-readable")

    # ── Expected diagnosis keywords ────────────────────────────────────────
    if "expected_diagnosis_keywords" in data:
        kws = data["expected_diagnosis_keywords"]
        if not isinstance(kws, list) or len(kws) == 0:
            result.warn("'expected_diagnosis_keywords' should be a non-empty list of strings")
    else:
        result.warn("'expected_diagnosis_keywords' not present — add for eval harness support")

    return result


def validate_index(index_path: Path, scenario_ids: set[str]) -> list[str]:
    """Validate INDEX.json against the actual scenario files on disk."""
    issues = []
    if not index_path.exists():
        issues.append(f"INDEX.json not found at {index_path}")
        return issues

    try:
        with index_path.open("r", encoding="utf-8") as fh:
            index: dict = json.load(fh)
    except Exception as exc:
        issues.append(f"INDEX.json is invalid JSON: {exc}")
        return issues

    if "scenarios" not in index:
        issues.append("INDEX.json missing 'scenarios' array")
        return issues

    indexed_ids = {s["id"] for s in index["scenarios"] if isinstance(s, dict) and "id" in s}

    # Check for scenarios on disk not in index
    for sid in sorted(scenario_ids):
        if sid not in indexed_ids:
            issues.append(f"Scenario '{sid}' exists on disk but is NOT in INDEX.json")

    # Check for index entries with no file on disk
    for sid in sorted(indexed_ids):
        if sid not in scenario_ids:
            issues.append(f"INDEX.json references '{sid}' but no {sid}.json file exists on disk")

    return issues


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SiteMedic demo scenario files")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (exit non-zero if any warnings)",
    )
    parser.add_argument(
        "--scenario",
        metavar="ID",
        help="Validate only this scenario (e.g. memory_leak_001)",
    )
    parser.add_argument(
        "--scenarios-dir",
        default=str(SCENARIOS_DIR),
        help=f"Path to scenarios directory (default: {SCENARIOS_DIR})",
    )
    args = parser.parse_args()

    scenarios_dir = Path(args.scenarios_dir)
    if not scenarios_dir.exists():
        print(f"ERROR: Scenarios directory not found: {scenarios_dir}", file=sys.stderr)
        return 1

    # Collect scenario files
    if args.scenario:
        files = [scenarios_dir / f"{args.scenario}.json"]
        missing = [f for f in files if not f.exists()]
        if missing:
            print(f"ERROR: Scenario file not found: {missing[0]}", file=sys.stderr)
            return 1
    else:
        files = [
            f for f in sorted(scenarios_dir.glob("*.json"))
            if f.name != "INDEX.json"
        ]

    if not files:
        print("No scenario files found.", file=sys.stderr)
        return 1

    print(f"Validating {len(files)} scenario(s) in {scenarios_dir}\n")

    results: list[ValidationResult] = []
    for path in files:
        r = validate_scenario(path)
        results.append(r)
        print(str(r))
        print()

    # Validate INDEX.json (only when checking all scenarios)
    if not args.scenario:
        scenario_ids = {r.scenario_id for r in results}
        index_path = scenarios_dir / "INDEX.json"
        index_issues = validate_index(index_path, scenario_ids)
        if index_issues:
            print("INDEX.json issues:")
            for issue in index_issues:
                print(f"  ERROR: {issue}")
            print()

    # Summary
    total = len(results)
    failed = sum(1 for r in results if not r.valid)
    warned = sum(1 for r in results if r.valid and r.warnings)
    passed = total - failed

    print("─" * 60)
    print(f"Results: {passed}/{total} passed, {failed} failed, {warned} with warnings")

    if failed > 0:
        print("\nFailed scenarios:")
        for r in results:
            if not r.valid:
                print(f"  - {r.scenario_id}: {len(r.errors)} error(s)")

    if not args.scenario and index_issues:
        return 1

    if failed > 0:
        return 1

    if args.strict and any(r.warnings for r in results):
        print("\n--strict mode: warnings are treated as errors")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
