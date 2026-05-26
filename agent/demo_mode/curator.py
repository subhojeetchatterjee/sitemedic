"""
Curator — reads raw JSONL session recordings and produces curated scenario
JSON files in agent/demo_mode/scenarios/.

A scenario is a named incident type with its ordered MCP call responses.
The curator extracts all mcp_call events for a given incident_id, preserving
call order, and writes a scenarios/<name>.json file.

Usage:
    python curator.py           # curate all known scenarios
    python curator.py --list    # list available raw sessions
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEMO_MODE_DIR = Path(__file__).parent
_RECORDINGS_DIR = _DEMO_MODE_DIR / "recordings" / "raw"
_SCENARIOS_DIR = _DEMO_MODE_DIR / "scenarios"

# ---------------------------------------------------------------------------
# Known scenario definitions
# Each entry: (scenario_name, description, incident_id, session_id)
# ---------------------------------------------------------------------------
KNOWN_SCENARIOS: list[tuple[str, str, str, str]] = [
    (
        "high_error_rate",
        "High error rate on sitemedic-demo-app",
        "P-REC-001",
        "20260525T051915_01583bd9",
    ),
    (
        "high_memory",
        "High memory utilization on sitemedic-demo-app",
        "P-REC-002",
        "20260525T051915_01583bd9",
    ),
    (
        "slow_response",
        "Slow response time on sitemedic-demo-app",
        "P-REC-003",
        "20260525T051915_01583bd9",
    ),
    (
        "availability_degraded",
        "Service availability degraded on sitemedic-demo-app",
        "P-REC-004",
        "20260525T051915_01583bd9",
    ),
    (
        "cpu_spike",
        "CPU spike on sitemedic-demo-app",
        "P-REC-005B",
        "20260525T053900_941d6e11",
    ),
]


def _iter_events(session_id: str) -> list[dict]:
    """Read all JSONL events from a session directory, in file order."""
    session_dir = _RECORDINGS_DIR / session_id
    if not session_dir.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_dir}")

    events: list[dict] = []
    for jsonl_file in sorted(session_dir.glob("events_*.jsonl")):
        with jsonl_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping malformed line in %s: %s", jsonl_file, exc)
    return events


def _extract_mcp_calls_for_incident(
    events: list[dict],
    incident_id: str,
) -> list[dict[str, Any]]:
    """
    Return all mcp_call events that are 'owned' by the given incident_id.

    Strategy:
    - Track when the incident is first seen (created event) and when it ends
      (postmortem event or next incident_created that is a different incident).
    - Collect all mcp_call events whose timestamp falls within that window.

    This handles sessions with multiple incidents interleaved.
    """
    # Find the start and end timestamps for this incident from incident_events.
    start_ts: str | None = None
    end_ts: str | None = None

    for ev in events:
        if ev.get("type") != "incident_event":
            continue
        if ev.get("incident_id") != incident_id:
            continue
        ev_type = ev.get("event_type", "")
        ts = ev.get("ts", "")
        if ev_type == "created":
            start_ts = ts
        elif ev_type == "postmortem":
            end_ts = ts
            break  # postmortem is the last event; stop scanning

    if start_ts is None:
        logger.warning("No 'created' event found for incident %s", incident_id)
        return []

    # Collect mcp_call events within the incident window.
    mcp_calls: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("type") != "mcp_call":
            continue
        ts = ev.get("ts", "")
        if ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            continue
        mcp_calls.append({
            "tool": ev["tool"],
            "arguments": ev.get("arguments", {}),
            "response": ev.get("response"),
            "latency_ms": ev.get("latency_ms", 0),
        })

    return mcp_calls


def curate_scenario(
    name: str,
    description: str,
    incident_id: str,
    session_id: str,
) -> dict[str, Any]:
    """
    Extract a scenario from raw recordings and return the scenario dict.
    Does NOT write to disk — call write_scenario() for that.
    """
    logger.info(
        "Curating scenario '%s' from incident %s in session %s",
        name,
        incident_id,
        session_id,
    )
    events = _iter_events(session_id)
    mcp_calls = _extract_mcp_calls_for_incident(events, incident_id)

    scenario: dict[str, Any] = {
        "name": name,
        "description": description,
        "source_incident": incident_id,
        "source_session": session_id,
        "curated_at": datetime.now(timezone.utc).isoformat(),
        "mcp_calls": mcp_calls,
    }
    logger.info("  Extracted %d MCP calls", len(mcp_calls))
    return scenario


def write_scenario(scenario: dict[str, Any]) -> Path:
    """Write scenario dict to scenarios/<name>.json. Creates dir if needed."""
    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _SCENARIOS_DIR / f"{scenario['name']}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(scenario, fh, indent=2, ensure_ascii=False)
    logger.info("  Written to %s", out_path)
    return out_path


def curate_all() -> list[dict[str, Any]]:
    """Curate all known scenarios and write them to disk. Returns scenario list."""
    results: list[dict[str, Any]] = []
    for name, description, incident_id, session_id in KNOWN_SCENARIOS:
        try:
            scenario = curate_scenario(name, description, incident_id, session_id)
            write_scenario(scenario)
            results.append(scenario)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to curate scenario '%s': %s", name, exc)
    return results


def list_sessions() -> list[str]:
    """Return sorted list of available session IDs."""
    if not _RECORDINGS_DIR.is_dir():
        return []
    return sorted(d.name for d in _RECORDINGS_DIR.iterdir() if d.is_dir())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Curate SiteMedic demo scenarios from raw JSONL recordings."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available raw sessions and exit",
    )
    parser.add_argument(
        "--scenario",
        metavar="NAME",
        help="Curate a single named scenario (default: curate all)",
    )
    args = parser.parse_args()

    if args.list:
        sessions = list_sessions()
        print("Available sessions:")
        for s in sessions:
            print(f"  {s}")
        return

    if args.scenario:
        match = next(
            (t for t in KNOWN_SCENARIOS if t[0] == args.scenario), None
        )
        if match is None:
            print(f"Unknown scenario '{args.scenario}'. Known: {[t[0] for t in KNOWN_SCENARIOS]}")
            sys.exit(1)
        scenario = curate_scenario(*match)
        path = write_scenario(scenario)
        print(f"Curated '{scenario['name']}': {len(scenario['mcp_calls'])} MCP calls -> {path}")
        return

    # Default: curate all
    scenarios = curate_all()
    print("\nCuration complete:")
    for sc in scenarios:
        tool_counts: dict[str, int] = defaultdict(int)
        for call in sc["mcp_calls"]:
            tool_counts[call["tool"]] += 1
        tools_summary = ", ".join(f"{t}={c}" for t, c in sorted(tool_counts.items()))
        print(
            f"  {sc['name']:25s}  {len(sc['mcp_calls']):3d} calls"
            f"  [{tools_summary}]"
        )


if __name__ == "__main__":
    main()
