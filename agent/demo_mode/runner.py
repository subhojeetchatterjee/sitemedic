"""
Phase 4 — Demo Runner.

Triggers a complete incident lifecycle using a curated scenario (pre-recorded
Dynatrace responses) instead of a live Dynatrace tenant.

Usage:
    from demo_mode.runner import DemoRunner
    result = await DemoRunner().run("high_error_rate")

The runner:
  1. Picks a unique demo problem ID (P-DEMO-<scenario>-<ts>)
  2. Registers the DemoModeSource in orchestrator._demo_sources
  3. Creates the incident in Firestore directly (bypassing webhook auth)
  4. Awaits AWAITING_APPROVAL (Gemini ReAct loop completes)
  5. Auto-approves (demo mode — no human required)
  6. Awaits RESOLVED + postmortem
  7. Returns the final incident document
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 3
_DIAGNOSIS_TIMEOUT_S = 300
_RESOLVE_TIMEOUT_S = 120


class DemoRunner:
    """Runs one scenario end-to-end in demo/playback mode."""

    def __init__(self, auto_approve: bool = True, realistic_latency: bool = True):
        self.auto_approve = auto_approve
        self.realistic_latency = realistic_latency

    async def run(self, scenario_name: str) -> dict[str, Any]:
        """
        Execute a full incident lifecycle for the given scenario.
        Returns the resolved incident document.
        """
        from demo_mode.source import get_demo_source
        from tools import firestore_client
        from orchestrator import register_demo_source, diagnose_and_plan, generate_postmortem

        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        problem_id = f"P-DEMO-{scenario_name.upper().replace('_', '-')}-{ts}"

        logger.info("DemoRunner: starting scenario=%s pid=%s", scenario_name, problem_id)

        # Load the scenario and register its DemoModeSource
        demo_source = get_demo_source(scenario_name, realistic_latency=self.realistic_latency)
        register_demo_source(problem_id, demo_source)

        # Create the incident document directly (no webhook needed)
        incident_data = {
            "problem_id": problem_id,
            "status": "DETECTING",
            "severity": "ERROR",
            "title": demo_source.scenario_description or f"Demo: {scenario_name}",
            "service": "sitemedic-demo-app",
            "detection_method": "demo",
            "started_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "trace": [],
            "plan": None,
            "postmortem": None,
            "providers_used": [],
            "time_to_detect_ms": 0,
            "demo_scenario": scenario_name,
        }
        await firestore_client.create_incident(incident_data)
        logger.info("DemoRunner: incident created %s", problem_id)

        # Spawn diagnosis (DemoModeSource handles all Dynatrace calls)
        asyncio.create_task(diagnose_and_plan(problem_id))

        # Wait for AWAITING_APPROVAL
        await self._poll_status(problem_id, "AWAITING_APPROVAL", _DIAGNOSIS_TIMEOUT_S)
        incident = await firestore_client.get_incident(problem_id)
        logger.info(
            "DemoRunner: diagnosis complete pid=%s plan=%s",
            problem_id,
            (incident.get("plan") or {}).get("action"),
        )

        if self.auto_approve:
            plan = incident.get("plan")
            if not plan:
                logger.warning("DemoRunner: no plan generated for %s — skipping approval", problem_id)
                return incident

            # Approve
            await firestore_client.set_status(problem_id, "REMEDIATING")
            await firestore_client.append_trace(problem_id, {
                "step": len(incident.get("trace", [])),
                "thought": f"[Demo] Auto-approved. Action: {plan.get('action')}",
                "tool_call": None,
                "tool_result": {"demo": True, "action": plan.get("action"), "status": "simulated_success"},
                "provider": "demo",
                "timestamp": datetime.utcnow().isoformat(),
            })
            await firestore_client.set_status(problem_id, "RESOLVED")
            logger.info("DemoRunner: auto-approved and resolved %s", problem_id)

            # Generate postmortem
            asyncio.create_task(generate_postmortem(problem_id))
            await self._poll_postmortem(problem_id, _RESOLVE_TIMEOUT_S)

        return await firestore_client.get_incident(problem_id)

    async def _poll_status(self, problem_id: str, target: str, timeout_s: int) -> None:
        from tools import firestore_client
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            inc = await firestore_client.get_incident(problem_id)
            status = (inc or {}).get("status", "")
            if status == target:
                return
            if status in ("RESOLVED", "REJECTED", "FAILED"):
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        logger.warning("DemoRunner: timeout waiting for %s to reach %s", problem_id, target)

    async def _poll_postmortem(self, problem_id: str, timeout_s: int) -> None:
        from tools import firestore_client
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            inc = await firestore_client.get_incident(problem_id)
            if (inc or {}).get("postmortem"):
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
        logger.warning("DemoRunner: timeout waiting for postmortem on %s", problem_id)
