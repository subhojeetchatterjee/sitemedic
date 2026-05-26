#!/usr/bin/env python3
"""
SiteMedic Eval Harness

Loads synthetic incident scenarios, injects faults into the demo app, waits for the
SiteMedic agent to detect + diagnose each incident, and scores the result.

⚠️  DEVELOPMENT ONLY: This harness must NOT run in production.

Usage:
    # With live infra (agent + demo app running)
    python evals/runner.py --suite all
    python evals/runner.py --suite scenario-001-memory-leak
    python evals/runner.py --suite latency          # run by category
    python evals/runner.py --suite all --no-llm-judge

    # Mock mode (no live infra required — tests eval harness itself)
    python evals/runner.py --suite all --mock --no-llm-judge
    python evals/runner.py --suite all --mock --mock-fail-rate 3  # inject 3 failures
    python evals/runner.py --suite all --output evals/results/my-run.json

Environment (live mode):
    AGENT_URL           http://localhost:8080  (agent API base)
    AGENT_API_KEY       change-me-before-deploy
    DEMO_APP_URL        http://localhost:3000
    GCP_PROJECT_ID      (required for LLM judge)
    VERTEX_AI_LOCATION  us-central1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import string
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

# ⚠️  FAIL-FAST: This module only runs in development
if os.environ.get("ENV") == "prod":
    print("❌ Error: evals/runner.py cannot run in production environment.")
    print("   This module is for development and testing only.")
    sys.exit(1)

# ── Optional rich for colourful output ────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich import print as rprint
    _console = Console()
    def _print(msg: str, style: str = "") -> None:
        _console.print(msg, style=style)
except ImportError:
    _console = None
    def _print(msg: str, style: str = "") -> None:
        print(msg)

logger = logging.getLogger("sitemedic.evals")

# ── Constants ──────────────────────────────────────────────────────────────

SCENARIOS_DIR   = Path(__file__).parent / "scenarios"
RESULTS_DIR     = Path(__file__).parent / "results"

AGENT_URL       = os.environ.get("AGENT_URL", "http://localhost:8080")
AGENT_API_KEY   = os.environ.get("AGENT_API_KEY", "change-me-before-deploy")
DEMO_APP_URL    = os.environ.get("DEMO_APP_URL", "http://localhost:3000")

JUDGE_MODEL     = "gemini-2.5-pro-preview-05-06"
DEFAULT_TIMEOUT = 300
POLL_INTERVAL   = 10

TERMINAL_STATUSES = {"AWAITING_APPROVAL", "RESOLVED", "REJECTED"}


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    scenario_id:      str
    name:             str
    category:         str
    passed:           bool
    timed_out:        bool        = False
    skipped:          bool        = False
    skip_reason:      str         = ""
    action_correct:   bool        = False
    keyword_match:    bool        = False
    judge_score:      int | None  = None  # 1-5
    judge_passed:     bool | None = None  # score >= 3
    judge_justification: str      = ""
    judge_missed_signals: str     = ""
    actual_action:    str | None  = None
    expected_action:  str         = ""
    acceptable_actions: list[str] = field(default_factory=list)
    matched_keywords: list[str]   = field(default_factory=list)
    trace_text:       str         = ""   # concatenated reasoning thoughts
    plan_reason:      str         = ""
    incident_id:      str | None  = None
    duration_seconds: float       = 0.0
    error:            str | None  = None


# ── Scenario loading ───────────────────────────────────────────────────────

def _expand_env(value: str) -> str:
    """Expand ${VAR} references in scenario strings."""
    return string.Template(value).safe_substitute(os.environ)


def load_scenarios(suite: str) -> list[dict]:
    """Return scenarios matching the suite filter (id, category, or 'all')."""
    all_files = sorted(SCENARIOS_DIR.glob("*.yaml"))
    scenarios = []
    for f in all_files:
        with f.open() as fh:
            sc = yaml.safe_load(fh)
        scenarios.append(sc)

    if suite == "all":
        return scenarios
    # Match by exact ID prefix or category
    return [
        s for s in scenarios
        if s["id"].startswith(suite) or s.get("category") == suite or s["id"] == suite
    ]


def _check_required_env(scenario: dict) -> str | None:
    """Return a skip reason if required env vars are missing, else None."""
    for var in scenario.get("requires_env", []):
        if not os.environ.get(var):
            return f"Required env var {var!r} not set"
    return None


# ── Demo app control ───────────────────────────────────────────────────────

async def _reset_demo_app(client: httpx.AsyncClient) -> None:
    try:
        await client.post(f"{DEMO_APP_URL}/reset", timeout=10)
    except Exception as exc:
        logger.warning(f"reset failed: {exc}")


async def _execute_setup_step(client: httpx.AsyncClient, step: dict) -> None:
    """Execute a single setup action against the demo app."""
    if "wait_seconds" in step:
        secs = int(step["wait_seconds"])
        _print(f"    ⏳ waiting {secs}s for faults to manifest in metrics…", "dim")
        await asyncio.sleep(secs)

    elif "inject" in step:
        fault = step["inject"]
        params = step.get("params", {})
        # Expand ${ENV_VAR} in string params
        params = {
            k: _expand_env(v) if isinstance(v, str) else v
            for k, v in params.items()
        }
        url = f"{DEMO_APP_URL}/inject/{fault}"
        resp = await client.post(url, json=params, timeout=15)
        if resp.status_code == 404:
            raise RuntimeError(f"Injection endpoint not found: {url}")
        resp.raise_for_status()
        _print(f"    💉 injected {fault!r} → {resp.json()}", "yellow")

    elif "reset" in step:
        await _reset_demo_app(client)
        _print("    🔄 demo app reset", "dim")

    elif "generate_traffic" in step:
        cfg = step["generate_traffic"]
        path    = cfg.get("path", "/checkout")
        rps     = int(cfg.get("requests_per_second", 50))
        dur     = int(cfg.get("duration_seconds", 30))
        await _generate_traffic(client, path, rps, dur)

    else:
        logger.warning(f"Unknown setup step: {step}")


async def _generate_traffic(
    client: httpx.AsyncClient,
    path: str,
    rps: int,
    duration_seconds: int,
) -> None:
    """Send `rps` concurrent GET requests per second for `duration_seconds`."""
    _print(f"    🚦 generating {rps} RPS to {path} for {duration_seconds}s", "yellow")
    url = f"{DEMO_APP_URL}{path}"
    end = time.monotonic() + duration_seconds
    sent = 0
    while time.monotonic() < end:
        batch = [client.get(url, timeout=15) for _ in range(rps)]
        results = await asyncio.gather(*batch, return_exceptions=True)
        sent += len(results)
        await asyncio.sleep(1)
    _print(f"    🚦 traffic generation done ({sent} requests sent)", "dim")


# ── Agent polling ──────────────────────────────────────────────────────────

async def _get_incident_ids(client: httpx.AsyncClient) -> set[str]:
    """Fetch the current set of incident IDs from the agent."""
    resp = await client.get(f"{AGENT_URL}/api/incidents", timeout=15)
    resp.raise_for_status()
    return {i["problem_id"] for i in resp.json()}


async def _wait_for_new_incident(
    client: httpx.AsyncClient,
    baseline_ids: set[str],
    start_time: float,
    timeout: float,
) -> dict | None:
    """Poll /api/incidents until a new incident appears."""
    while time.monotonic() - start_time < timeout:
        try:
            resp = await client.get(f"{AGENT_URL}/api/incidents", timeout=15)
            for inc in resp.json():
                if inc["problem_id"] not in baseline_ids:
                    return inc
        except Exception as exc:
            logger.warning(f"Poll error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)
    return None


async def _wait_for_terminal_status(
    client: httpx.AsyncClient,
    problem_id: str,
    start_time: float,
    timeout: float,
) -> dict | None:
    """Poll /api/incidents/{id} until the incident reaches a terminal status."""
    while time.monotonic() - start_time < timeout:
        try:
            resp = await client.get(f"{AGENT_URL}/api/incidents/{problem_id}", timeout=15)
            inc = resp.json()
            if inc.get("status") in TERMINAL_STATUSES:
                return inc
        except Exception as exc:
            logger.warning(f"Status poll error for {problem_id}: {exc}")
        await asyncio.sleep(POLL_INTERVAL)
    return None


# ── Scoring ────────────────────────────────────────────────────────────────

def _build_trace_text(incident: dict) -> str:
    """Concatenate all trace step thoughts into a single string for keyword search."""
    parts = []
    for step in incident.get("trace", []):
        if step.get("thought"):
            parts.append(step["thought"])
        # Also include tool result snippets (truncated to avoid noise)
        result = step.get("tool_result")
        if isinstance(result, (dict, list)):
            parts.append(json.dumps(result)[:500])
        elif isinstance(result, str):
            parts.append(result[:500])
    plan = incident.get("plan") or {}
    if plan.get("reason"):
        parts.append(plan["reason"])
    return "\n".join(parts).lower()


def score_keywords(trace_text: str, expected: dict) -> tuple[bool, list[str]]:
    """Return (matched, matched_keywords). Passes if at least one keyword matches."""
    keywords = [k.lower() for k in expected.get("root_cause_keywords", [])]
    matched = [kw for kw in keywords if kw in trace_text]
    return bool(matched), matched


def score_action(incident: dict, expected: dict) -> bool:
    """True if the agent's chosen action is in acceptable_actions."""
    plan = incident.get("plan") or {}
    action = plan.get("action", "")
    return action in expected.get("acceptable_actions", [])


# ── LLM-as-judge ──────────────────────────────────────────────────────────
# This prompt is deliberately structured DIFFERENTLY from the agent's diagnose.txt.
# The agent prompt is first-person SRE investigation instructions.
# This judge prompt is third-person evaluator with an explicit rubric.

_JUDGE_PROMPT = """\
EVALUATION TASK
===============
You are a quality auditor reviewing an AI agent's incident response.
Your job is to score how accurately the agent identified the root cause.

Do NOT use the same reasoning approach the agent used — your role is to
evaluate it from the outside, using the reference answer as ground truth.

SYNTHETIC INCIDENT DESCRIPTION
-------------------------------
{scenario_description}

REFERENCE ANSWER (written by an experienced SRE — treat as ground truth)
-------------------------------------------------------------------------
{reference_diagnosis}

AGENT'S ACTUAL RESPONSE
------------------------
Reasoning trace (concatenated thoughts):
{trace_text}

Final action chosen: {actual_action}
Agent's stated reason: {plan_reason}

SCORING RUBRIC
--------------
5 — Excellent  : Agent pinpointed the exact root cause with solid evidence chain;
                 action is the optimal fix; reasoning is clear and complete.
4 — Good       : Root cause correctly identified; action is appropriate; minor gaps
                 in evidence or one overlooked signal.
3 — Acceptable : Root cause partially correct or inferred indirectly; action is
                 defensible even if not optimal; noticeable reasoning gaps.
2 — Weak       : Root cause mentioned tangentially but not properly diagnosed;
                 action may be accidentally correct.
1 — Wrong      : Root cause misidentified or missed entirely; action is inappropriate.

RESPONSE FORMAT (JSON only — no markdown, no extra text)
--------------------------------------------------------
{{
  "score": <integer 1-5>,
  "justification": "<2-3 sentences explaining why you gave this score>",
  "missed_signals": "<key evidence the agent should have used but ignored, or empty string>"
}}
"""


async def llm_judge(incident: dict, scenario: dict, trace_text: str) -> tuple[int, str, str]:
    """
    Call Gemini 2.5 Pro as an independent judge.
    Returns (score, justification, missed_signals).
    Score of 0 indicates a judge failure (network error etc.).
    """
    import os
    import vertexai
    from vertexai.generative_models import Content, GenerationConfig, GenerativeModel, Part

    vertexai.init(
        project=os.environ.get("GCP_PROJECT_ID", ""),
        location=os.environ.get("VERTEX_AI_LOCATION", "us-central1"),
    )

    plan = incident.get("plan") or {}
    prompt = _JUDGE_PROMPT.format(
        scenario_description=scenario.get("description", "").strip(),
        reference_diagnosis=scenario.get("reference_diagnosis", "").strip(),
        trace_text=(trace_text[:3000] + "…") if len(trace_text) > 3000 else trace_text,
        actual_action=plan.get("action", "unknown"),
        plan_reason=(plan.get("reason", "")[:500]),
    )

    model = GenerativeModel(
        model_name=JUDGE_MODEL,
        generation_config=GenerationConfig(
            response_mime_type="application/json",
            temperature=0,  # deterministic scores
        ),
    )
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [Content(role="user", parts=[Part.from_text(prompt)])],
        )
        raw = response.candidates[0].content.parts[0].text
        data = json.loads(raw)
        score = int(data.get("score", 0))
        score = max(1, min(5, score))  # clamp
        return score, data.get("justification", ""), data.get("missed_signals", "")
    except Exception as exc:
        logger.warning(f"LLM judge failed: {exc}")
        return 0, f"Judge error: {exc}", ""


# ── Mock mode ─────────────────────────────────────────────────────────────

def _build_mock_incident(scenario: dict, force_fail: bool = False) -> dict:
    """
    Synthesise a synthetic agent API response from a scenario YAML.
    If force_fail=True, inject a completely wrong diagnosis to test failure messages.
    """
    expected = scenario["expected_diagnosis"]
    sid = scenario["id"]

    if force_fail:
        # Deliberately wrong: empty trace + wrong action + nonsensical reason
        action = "rollback_revision"
        trace = [{"thought": "no relevant data found", "tool_result": "{}"}]
        reason = "Could not determine root cause."
    else:
        action = expected.get("expected_action", expected.get("acceptable_actions", ["unknown"])[0])
        keywords_str = " ".join(expected.get("root_cause_keywords", []))
        trace = [
            {"thought": keywords_str, "tool_result": scenario.get("description", "")}
        ]
        reason = scenario.get("reference_diagnosis", "")

    return {
        "problem_id": f"mock-{sid}",
        "status": "AWAITING_APPROVAL",
        "trace": trace,
        "plan": {
            "action": action,
            "reason": reason,
        },
    }


# ── Scenario runner ────────────────────────────────────────────────────────

async def run_scenario(
    scenario: dict,
    use_llm_judge: bool = True,
    mock_mode: bool = False,
    force_fail: bool = False,
) -> ScenarioResult:
    sid  = scenario["id"]
    name = scenario.get("name", sid)
    cat  = scenario.get("category", "unknown")

    _print(f"\n[bold]▶ {sid}[/bold] — {name}", "")

    # Check required env vars
    skip_reason = _check_required_env(scenario)
    if skip_reason:
        _print(f"  ⏭  SKIP: {skip_reason}", "dim")
        return ScenarioResult(
            scenario_id=sid, name=name, category=cat,
            passed=False, skipped=True, skip_reason=skip_reason,
        )

    timeout  = scenario.get("timeout_seconds", DEFAULT_TIMEOUT)
    expected = scenario["expected_diagnosis"]

    # Mock mode: skip all HTTP calls and synthesise response
    if mock_mode:
        _print(f"  🎭 mock mode: synthesising incident…", "dim")
        start = time.monotonic()
        await asyncio.sleep(0.5)  # simulate detection time
        final_inc = _build_mock_incident(scenario, force_fail=force_fail)
        pid = final_inc["problem_id"]
        duration = time.monotonic() - start
        _print(f"  📌 synthetic incident {pid} (mock)", "cyan")
    else:
        async with httpx.AsyncClient() as client:
            try:
                # 1. Reset demo app
                await _reset_demo_app(client)
                await asyncio.sleep(2)  # brief pause so reset propagates

                # 2. Baseline snapshot
                baseline_ids = await _get_incident_ids(client)
                start = time.monotonic()

                # 3. Setup
                for step in scenario.get("setup", []):
                    await _execute_setup_step(client, step)

                # 4. Wait for agent to detect a new incident
                wait_budget = timeout - (time.monotonic() - start)
                _print(f"  🔍 polling for new incident (budget {wait_budget:.0f}s)…", "dim")
                new_inc = await _wait_for_new_incident(
                    client, baseline_ids, start, timeout * 0.6
                )

                if new_inc is None:
                    return ScenarioResult(
                        scenario_id=sid, name=name, category=cat,
                        passed=False, timed_out=True,
                        error=(
                            f"No new incident detected within {timeout * 0.6:.0f}s. "
                            "Ensure Dynatrace is monitoring the demo app and the agent is running."
                        ),
                        duration_seconds=time.monotonic() - start,
                    )

                pid = new_inc["problem_id"]
                _print(f"  📌 incident {pid} detected (status={new_inc['status']})", "cyan")

                # 5. Wait for terminal status (AWAITING_APPROVAL / RESOLVED / REJECTED)
                final_inc = await _wait_for_terminal_status(
                    client, pid, start, timeout
                )
                duration = time.monotonic() - start

                if final_inc is None:
                    return ScenarioResult(
                        scenario_id=sid, name=name, category=cat,
                        passed=False, timed_out=True, incident_id=pid,
                        error=(
                            f"Incident {pid} did not reach terminal status within {timeout}s "
                            f"(last status: {new_inc['status']})."
                        ),
                        duration_seconds=duration,
                    )

                _print(f"  ✅ incident reached {final_inc['status']} in {duration:.1f}s", "green")

            except Exception as exc:
                logger.exception(f"Error running scenario {sid}")
                return ScenarioResult(
                    scenario_id=sid, name=name, category=cat,
                    passed=False, error=str(exc),
                    duration_seconds=time.monotonic() - start if 'start' in dir() else 0,
                )

            finally:
                # Teardown regardless of result
                for step in scenario.get("teardown", [{"reset": True}]):
                    try:
                        await _execute_teardown_step(client, step)
                    except Exception:
                        pass

    # 6. Score
    trace_text       = _build_trace_text(final_inc)
    keyword_ok, kws  = score_keywords(trace_text, expected)
    action_ok        = score_action(final_inc, expected)
    plan             = final_inc.get("plan") or {}

    judge_score: int | None = None
    judge_passed: bool | None = None
    justification = ""
    missed = ""

    if use_llm_judge and os.environ.get("GCP_PROJECT_ID"):
        _print("  🧑‍⚖️  running LLM judge…", "dim")
        judge_score, justification, missed = await llm_judge(final_inc, scenario, trace_text)
        if judge_score > 0:
            judge_passed = judge_score >= 3
        _print(
            f"  🧑‍⚖️  judge score: {judge_score}/5 — {'PASS' if judge_passed else 'FAIL'}",
            "green" if judge_passed else "red",
        )
    elif use_llm_judge:
        logger.warning("GCP_PROJECT_ID not set — skipping LLM judge")

    # Overall pass: action correct AND (keyword match OR judge passed)
    # If judge unavailable, fall back to keyword-only
    if judge_passed is not None:
        passed = action_ok and (keyword_ok or judge_passed)
    else:
        passed = action_ok and keyword_ok

    badge = "✅ PASS" if passed else "❌ FAIL"
    _print(
        f"  {badge}  action={plan.get('action','?')}  "
        f"keywords={'✓' if keyword_ok else '✗'}  "
        f"judge={judge_score or '—'}",
        "green" if passed else "red",
    )

    if not passed:
        if not action_ok:
            _print(
                f"  ⚠️  Wrong action: got '{plan.get('action')}', "
                f"expected one of {expected.get('acceptable_actions')}",
                "yellow",
            )
        if not keyword_ok:
            _print(
                f"  ⚠️  No root-cause keywords found. Expected one of: "
                f"{expected.get('root_cause_keywords')}",
                "yellow",
            )
        if missed:
            _print(f"  ⚠️  Missed signals: {missed}", "yellow")

    return ScenarioResult(
        scenario_id=sid,
        name=name,
        category=cat,
        passed=passed,
        action_correct=action_ok,
        keyword_match=keyword_ok,
        matched_keywords=kws,
        judge_score=judge_score,
        judge_passed=judge_passed,
        judge_justification=justification,
        judge_missed_signals=missed,
        actual_action=plan.get("action"),
        expected_action=expected.get("expected_action", ""),
        acceptable_actions=expected.get("acceptable_actions", []),
        trace_text=trace_text[:2000],  # truncate for output file
        plan_reason=plan.get("reason", ""),
        incident_id=pid if 'pid' in dir() else None,
        duration_seconds=duration,
    )


async def _execute_teardown_step(client: httpx.AsyncClient, step: dict) -> None:
    if "reset" in step or step == {"reset": True} or step == "reset":
        await _reset_demo_app(client)


# ── Aggregate scoring ──────────────────────────────────────────────────────

def _summarise(results: list[ScenarioResult]) -> dict:
    runnable  = [r for r in results if not r.skipped]
    passed    = [r for r in runnable if r.passed]
    failed    = [r for r in runnable if not r.passed and not r.timed_out]
    timed_out = [r for r in runnable if r.timed_out]
    skipped   = [r for r in results if r.skipped]

    accuracy = (len(passed) / len(runnable) * 100) if runnable else 0.0

    by_category: dict[str, dict] = {}
    for r in runnable:
        cat = r.category
        by_category.setdefault(cat, {"total": 0, "passed": 0})
        by_category[cat]["total"] += 1
        if r.passed:
            by_category[cat]["passed"] += 1

    return {
        "total":     len(results),
        "runnable":  len(runnable),
        "passed":    len(passed),
        "failed":    len(failed),
        "timed_out": len(timed_out),
        "skipped":   len(skipped),
        "accuracy_pct": round(accuracy, 1),
        "by_category": by_category,
    }


def _write_results(
    results: list[ScenarioResult],
    summary: dict,
    suite: str,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id":    datetime.now(timezone.utc).isoformat(),
        "suite":     suite,
        "agent_url": AGENT_URL,
        **summary,
        "scenarios": [asdict(r) for r in results],
    }
    output_path.write_text(json.dumps(data, indent=2, default=str))
    _print(f"\n📄 Results written to {output_path}", "dim")

    # Also create a 'latest.json' symlink for easy reference (used by report.py and workflows)
    latest_link = output_path.parent / "latest.json"
    try:
        latest_link.unlink(missing_ok=True)
        latest_link.symlink_to(output_path.name)
    except Exception as exc:
        logger.warning(f"Could not create latest.json symlink: {exc}")


# ── CLI entry point ────────────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    scenarios = load_scenarios(args.suite)
    if not scenarios:
        _print(f"No scenarios matched '{args.suite}'", "red")
        return 1

    _print(f"\n[bold]SiteMedic Eval Harness[/bold]", "")
    if args.mock:
        _print(f"🎭 MOCK MODE (no live infra required)", "yellow")
    _print(f"Suite   : {args.suite}", "")
    if not args.mock:
        _print(f"Agent   : {AGENT_URL}", "")
        _print(f"Demo app: {DEMO_APP_URL}", "")
    _print(f"Scenarios: {len(scenarios)}", "")

    if not args.no_llm_judge:
        if not os.environ.get("GCP_PROJECT_ID"):
            _print("⚠️  GCP_PROJECT_ID not set — LLM judge will be skipped", "yellow")

    if args.mock and args.mock_fail_rate:
        _print(f"⚠️  Injecting {args.mock_fail_rate} intentional failures (last N scenarios)", "yellow")

    results: list[ScenarioResult] = []
    for i, sc in enumerate(scenarios):
        # Inject intentional failures for the last N scenarios (if --mock-fail-rate set)
        force_fail = (
            args.mock
            and args.mock_fail_rate > 0
            and i >= (len(scenarios) - args.mock_fail_rate)
        )
        result = await run_scenario(
            sc,
            use_llm_judge=not args.no_llm_judge,
            mock_mode=args.mock,
            force_fail=force_fail,
        )
        results.append(result)

    summary = _summarise(results)

    # Print summary table
    _print("\n" + "=" * 60, "")
    _print(f"Results: {summary['passed']}/{summary['runnable']} passed "
           f"({summary['accuracy_pct']}% accuracy)", "")
    if summary["skipped"]:
        _print(f"Skipped: {summary['skipped']} (missing required env vars)", "dim")
    if summary["timed_out"]:
        _print(f"Timed out: {summary['timed_out']}", "red")

    for cat, stats in summary["by_category"].items():
        pct = round(stats["passed"] / stats["total"] * 100)
        _print(f"  {cat:<20} {stats['passed']}/{stats['total']}  ({pct}%)", "")

    _print("=" * 60, "")

    # Failure deep-dives
    failures = [r for r in results if not r.passed and not r.skipped]
    if failures:
        _print("\n[bold]Failure details:[/bold]", "")
        for r in failures:
            _print(f"\n  ❌ {r.scenario_id}", "red")
            if r.timed_out:
                _print(f"     TIMEOUT: {r.error}", "red")
            elif r.error:
                _print(f"     ERROR: {r.error}", "red")
            else:
                if not r.action_correct:
                    _print(
                        f"     Action mismatch: got '{r.actual_action}', "
                        f"expected {r.acceptable_actions}",
                        "yellow",
                    )
                if not r.keyword_match:
                    _print(
                        f"     Missing keywords: {r.acceptable_actions!r} — "
                        f"none of {[]}found in trace",
                        "yellow",
                    )
                if r.judge_score is not None and not r.judge_passed:
                    _print(f"     Judge ({r.judge_score}/5): {r.judge_justification}", "yellow")
                    if r.judge_missed_signals:
                        _print(f"     Missed signals: {r.judge_missed_signals}", "yellow")

    # Write output file
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(args.output) if args.output else RESULTS_DIR / f"{ts}.json"
    _write_results(results, summary, args.suite, output)

    # Exit code: 0 if accuracy >= 70%, else 1
    target = args.pass_threshold
    if summary["accuracy_pct"] >= target:
        _print(f"\n✅ Accuracy {summary['accuracy_pct']}% meets target {target}%", "green")
        return 0
    else:
        _print(
            f"\n❌ Accuracy {summary['accuracy_pct']}% is below target {target}%. "
            f"See failure details above.",
            "red",
        )
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SiteMedic Eval Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--suite", default="all",
        help="Scenario ID, category name, or 'all' (default: all)",
    )
    parser.add_argument(
        "--no-llm-judge", action="store_true",
        help="Skip Gemini LLM-as-judge; score by keyword match + action only",
    )
    parser.add_argument(
        "--output", default=None,
        help="Override output file path (default: evals/results/<timestamp>.json)",
    )
    parser.add_argument(
        "--pass-threshold", type=float, default=70.0,
        help="Minimum accuracy %% to exit with code 0 (default: 70)",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Mock mode: bypass live infra (demo app, agent) and synthesise incidents from YAML",
    )
    parser.add_argument(
        "--mock-fail-rate", type=int, default=0,
        help="Mock mode: inject N intentional failures to test failure messages (default: 0)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
