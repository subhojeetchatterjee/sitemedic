#!/usr/bin/env python3
"""
Scenario capture CLI — triggers demo-app fault injections and waits for a
full incident lifecycle to be recorded.

Usage:
    python scripts/capture_scenarios.py --scenario memory_leak --duration 300
    python scripts/capture_scenarios.py --scenario bad_deploy
    python scripts/capture_scenarios.py --scenario latency_spike --duration 240
    python scripts/capture_scenarios.py --scenario error_burst
    python scripts/capture_scenarios.py --scenario cascading_failure
    python scripts/capture_scenarios.py --scenario predictive_catch

Prerequisites:
  - Agent running at $AGENT_URL (default http://localhost:8080)
    with SITEMEDIC_RECORD=true
  - Demo app running at $DEMO_APP_URL (default http://localhost:3000)
  - Dynatrace connected and healthy
  - AGENT_API_KEY set in environment
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8080")
DEMO_APP_URL = os.environ.get("DEMO_APP_URL", "http://localhost:3000")
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")

# Minimum events required to consider a recording complete.
_REQUIRED_EVENT_TYPES = {
    "incident_event:created",
    "incident_event:status_change",
    "mcp_call:list_problems",
    "mcp_call:get_problem_details",
}

# ── Scenario definitions ────────────────────────────────────────────────────

def _inject(client: httpx.Client, path: str, body: dict = {}) -> dict:
    resp = client.post(f"{DEMO_APP_URL}{path}", json=body, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _reset(client: httpx.Client) -> None:
    try:
        client.post(f"{DEMO_APP_URL}/reset", timeout=10)
    except Exception:
        pass  # best effort


def _scenario_memory_leak(client: httpx.Client) -> dict:
    result = _inject(client, "/inject/memory")
    print(f"  -> Memory leak started: {result}")
    return {"injected": "memory_leak", "endpoint": "/inject/memory"}


def _scenario_bad_deploy(client: httpx.Client) -> dict:
    # Switch demo app to the buggy revision via Cloud Run traffic split.
    # This requires DEMO_APP_BUGGY_REVISION to be deployed.
    buggy = os.environ.get("DEMO_APP_BUGGY_REVISION", "buggy")
    print(f"  -> Bad deploy: to-revisions={buggy}=100 (requires gcloud access)")
    print(f"     If automatic switch is not configured, manually run:")
    print(f"     gcloud run services update-traffic demo-app --to-revisions={buggy}=100")
    return {"injected": "bad_deploy", "revision": buggy}


def _scenario_latency_spike(client: httpx.Client) -> dict:
    result = _inject(client, "/inject/latency", {"ms": 3000})
    print(f"  -> Latency injection started: {result}")
    return {"injected": "latency_spike", "ms": 3000}


def _scenario_error_burst(client: httpx.Client) -> dict:
    result = _inject(client, "/inject/errors", {"rate": 0.5})
    print(f"  -> Error burst started: {result}")
    return {"injected": "error_burst", "rate": 0.5}


def _scenario_cascading_failure(client: httpx.Client) -> dict:
    r1 = _inject(client, "/inject/errors", {"rate": 0.6})
    r2 = _inject(client, "/inject/latency", {"ms": 2000})
    print(f"  -> Cascading failure: errors={r1}, latency={r2}")
    return {"injected": "cascading_failure", "errors": r1, "latency": r2}


def _scenario_predictive_catch(client: httpx.Client) -> dict:
    # Slow degradation — start with small latency, escalate over time
    result = _inject(client, "/inject/latency", {"ms": 800})
    print(f"  -> Predictive catch: slow latency start @ 800ms — escalating manually")
    print(f"     After 2 minutes, run: POST {DEMO_APP_URL}/inject/latency {{\"ms\": 2500}}")
    return {"injected": "predictive_catch", "initial_latency_ms": 800}


_SCENARIOS: dict[str, tuple[int, Callable]] = {
    # name -> (default_duration_seconds, trigger_fn)
    "memory_leak":       (300, _scenario_memory_leak),
    "bad_deploy":        (360, _scenario_bad_deploy),
    "latency_spike":     (240, _scenario_latency_spike),
    "error_burst":       (240, _scenario_error_burst),
    "cascading_failure": (360, _scenario_cascading_failure),
    "predictive_catch":  (480, _scenario_predictive_catch),
}


# ── Recording session discovery ─────────────────────────────────────────────

def _find_latest_session() -> Path | None:
    raw_dir = Path(__file__).parent.parent / "agent" / "demo_mode" / "recordings" / "raw"
    if not raw_dir.exists():
        return None
    sessions = sorted(raw_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


def _count_events_in_session(session_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for jsonl in sorted(session_dir.glob("events_*.jsonl")):
        with jsonl.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    ev_type = ev.get("type", "unknown")
                    if ev_type == "mcp_call":
                        key = f"mcp_call:{ev.get('tool', '?')}"
                    elif ev_type == "incident_event":
                        key = f"incident_event:{ev.get('event_type', '?')}"
                    else:
                        key = ev_type
                    counts[key] = counts.get(key, 0) + 1
                except json.JSONDecodeError:
                    pass
    return counts


def _validate_recording(session_dir: Path, scenario: str) -> tuple[bool, list[str]]:
    counts = _count_events_in_session(session_dir)
    issues = []

    total_events = sum(counts.values())
    if total_events == 0:
        issues.append("No events recorded — is SITEMEDIC_RECORD=true set on the agent?")
        return False, issues

    for required in _REQUIRED_EVENT_TYPES:
        if counts.get(required, 0) == 0:
            issues.append(f"Missing required event type: {required}")

    if counts.get("incident_event:created", 0) == 0:
        issues.append("No incident was created — Dynatrace may not have detected the fault")

    # Postmortem = resolved
    if counts.get("incident_event:postmortem", 0) == 0:
        issues.append("WARNING: No postmortem recorded — incident may not have fully resolved")

    ok = len([i for i in issues if not i.startswith("WARNING")]) == 0
    return ok, issues


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a SiteMedic scenario recording")
    parser.add_argument(
        "--scenario",
        required=True,
        choices=list(_SCENARIOS.keys()),
        help="Which fault scenario to trigger",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="How long to wait for the incident lifecycle (seconds). Defaults per scenario.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Skip the POST /reset call after the scenario completes",
    )
    args = parser.parse_args()

    scenario_name = args.scenario
    default_duration, trigger_fn = _SCENARIOS[scenario_name]
    duration = args.duration or default_duration

    print(f"\n{'='*60}")
    print(f"SiteMedic Scenario Capture")
    print(f"  Scenario : {scenario_name}")
    print(f"  Duration : {duration}s")
    print(f"  Agent    : {AGENT_URL}")
    print(f"  Demo App : {DEMO_APP_URL}")
    print(f"{'='*60}\n")

    # 1. Check agent is running
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{AGENT_URL}/health")
            resp.raise_for_status()
            print(f"[OK] Agent healthy: {resp.json()}")
    except Exception as e:
        print(f"[FAIL] Cannot reach agent at {AGENT_URL}: {e}")
        print("       Start agent with SITEMEDIC_RECORD=true before capturing.")
        sys.exit(1)

    # 2. Check demo app is reachable
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{DEMO_APP_URL}/health")
            resp.raise_for_status()
            print(f"[OK] Demo app healthy: {resp.json()}")
    except Exception as e:
        print(f"[FAIL] Cannot reach demo app at {DEMO_APP_URL}: {e}")
        sys.exit(1)

    # 3. Note the pre-capture session directory (so we can find what was recorded)
    pre_sessions = set()
    raw_dir = Path(__file__).parent.parent / "agent" / "demo_mode" / "recordings" / "raw"
    if raw_dir.exists():
        pre_sessions = {p.name for p in raw_dir.iterdir() if p.is_dir()}

    # 4. Trigger the fault
    print(f"\n[STEP 1] Triggering fault: {scenario_name}")
    captured_at = datetime.now(timezone.utc).isoformat()
    with httpx.Client(timeout=15) as client:
        injection_result = trigger_fn(client)

    print(f"\n[STEP 2] Waiting {duration}s for full incident lifecycle...")
    print(f"         (Detection → Diagnosis → Plan → Approval → Remediation → Postmortem)")
    print(f"         You may need to manually approve the remediation plan.")
    print(f"         Approve endpoint: POST {AGENT_URL}/api/incidents/<id>/approve")
    print(f"         Header: Authorization: Bearer {AGENT_API_KEY[:8]}...")

    # Progress dots
    start = time.monotonic()
    while time.monotonic() - start < duration:
        remaining = duration - int(time.monotonic() - start)
        print(f"         {remaining:3d}s remaining...", end="\r", flush=True)
        time.sleep(10)
    print()

    # 5. Reset demo app
    if not args.no_reset:
        print(f"\n[STEP 3] Resetting demo app faults...")
        with httpx.Client(timeout=10) as client:
            _reset(client)
        print("         Done.")

    # 6. Find the recording session that was active during this capture
    if raw_dir.exists():
        post_sessions = {p.name for p in raw_dir.iterdir() if p.is_dir()}
        new_sessions = post_sessions - pre_sessions
    else:
        new_sessions = set()

    session_dir = _find_latest_session()

    print(f"\n[STEP 4] Validating recording...")
    if session_dir is None:
        print("[FAIL] No recording session found.")
        print("       Ensure the agent was started with SITEMEDIC_RECORD=true")
        sys.exit(1)

    print(f"         Session: {session_dir}")
    counts = _count_events_in_session(session_dir)
    print(f"         Events captured: {sum(counts.values())} total")
    for k, v in sorted(counts.items()):
        print(f"           {k}: {v}")

    ok, issues = _validate_recording(session_dir, scenario_name)
    if issues:
        for issue in issues:
            marker = "[WARN]" if issue.startswith("WARNING") else "[FAIL]"
            print(f"         {marker} {issue}")
    if ok:
        print("\n[SUCCESS] Recording is complete and valid!")
        print(f"          Session dir: {session_dir}")
        print(f"\nNext step: run the curator to build a replayable scenario file:")
        print(f"  python agent/demo_mode/curator.py --session {session_dir.name} --scenario {scenario_name} --captured-at \"{captured_at}\"")
    else:
        print("\n[INCOMPLETE] Recording has issues — see above.")
        print("             You can still inspect the raw events and retry.")
        sys.exit(1)

    # 7. Save a capture manifest alongside the session
    manifest = {
        "scenario": scenario_name,
        "captured_at": captured_at,
        "duration_seconds": duration,
        "injection_result": injection_result,
        "event_counts": counts,
        "validation_ok": ok,
        "validation_issues": issues,
    }
    manifest_path = session_dir / "capture_manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2, default=str)
    print(f"          Manifest saved: {manifest_path}")


if __name__ == "__main__":
    main()
