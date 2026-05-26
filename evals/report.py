#!/usr/bin/env python3
"""
SiteMedic Eval Report Generator

Reads a results JSON file produced by runner.py and generates a Markdown report.
Optionally compares to a previous run to surface regressions and improvements.

Usage:
    python evals/report.py                            # uses the latest results file
    python evals/report.py --results evals/results/20240101T120000Z.json
    python evals/report.py --compare evals/results/20231231T120000Z.json
    python evals/report.py --output report.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

RESULTS_DIR = Path(__file__).parent / "results"

CATEGORY_LABELS = {
    "resource":       "Resource (Memory / CPU)",
    "latency":        "Latency",
    "error":          "Error Rate",
    "compound":       "Compound Failure",
    "infrastructure": "Infrastructure (PubSub / DB)",
    "unknown":        "Other",
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _latest_results_file() -> Path | None:
    files = sorted(RESULTS_DIR.glob("*.json"), reverse=True)
    return files[0] if files else None


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _accuracy_bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"`{bar}` {pct:.1f}%"


def _status_icon(passed: bool | None, timed_out: bool = False, skipped: bool = False) -> str:
    if skipped:
        return "⏭"
    if timed_out:
        return "⏱"
    return "✅" if passed else "❌"


# ── Report sections ────────────────────────────────────────────────────────

def _section_summary(data: dict) -> str:
    ts = data.get("run_id", "unknown")
    try:
        dt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = ts

    lines = [
        "## Summary\n",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Run timestamp | {dt} |",
        f"| Suite | `{data.get('suite', 'all')}` |",
        f"| Agent URL | `{data.get('agent_url', '')}` |",
        f"| Total scenarios | {data.get('total', 0)} |",
        f"| Runnable | {data.get('runnable', 0)} |",
        f"| Passed | {data.get('passed', 0)} |",
        f"| Failed | {data.get('failed', 0)} |",
        f"| Timed out | {data.get('timed_out', 0)} |",
        f"| Skipped | {data.get('skipped', 0)} |",
        f"| **Accuracy** | **{data.get('accuracy_pct', 0):.1f}%** |",
        "",
        f"**Overall accuracy:** {_accuracy_bar(data.get('accuracy_pct', 0))}",
    ]
    return "\n".join(lines)


def _section_by_category(data: dict) -> str:
    by_cat = data.get("by_category", {})
    if not by_cat:
        return ""

    lines = [
        "\n## Per-Category Breakdown\n",
        "| Category | Passed | Total | Accuracy | Bar |",
        "|----------|--------|-------|----------|-----|",
    ]
    for cat, stats in sorted(by_cat.items()):
        pct = round(stats["passed"] / stats["total"] * 100, 1) if stats["total"] else 0
        label = CATEGORY_LABELS.get(cat, cat)
        bar = _accuracy_bar(pct, width=10)
        lines.append(
            f"| {label} | {stats['passed']} | {stats['total']} | {pct:.1f}% | {bar} |"
        )
    return "\n".join(lines)


def _section_scenario_table(scenarios: list[dict]) -> str:
    lines = [
        "\n## Scenario Results\n",
        "| # | Scenario | Category | Action | Keywords | Judge | Status |",
        "|---|----------|----------|--------|----------|-------|--------|",
    ]
    for i, s in enumerate(scenarios, 1):
        icon = _status_icon(s.get("passed"), s.get("timed_out", False), s.get("skipped", False))
        judge = f"{s.get('judge_score', '—')}/5" if s.get("judge_score") else "—"
        kw = "✓" if s.get("keyword_match") else "✗"
        actual = s.get("actual_action") or "—"
        cat = CATEGORY_LABELS.get(s.get("category", ""), s.get("category", ""))
        name = s.get("name", s.get("scenario_id", ""))
        sid = s.get("scenario_id", "")
        lines.append(
            f"| {i} | **{name}** `{sid}` | {cat} | `{actual}` | {kw} | {judge} | {icon} |"
        )
    return "\n".join(lines)


def _section_failures(scenarios: list[dict]) -> str:
    failures = [s for s in scenarios if not s.get("passed") and not s.get("skipped")]
    if not failures:
        return "\n## Failures\n\nNo failures. 🎉"

    lines = ["\n## Failure Deep-Dives\n"]
    for s in failures:
        sid = s.get("scenario_id", "?")
        name = s.get("name", sid)
        lines.append(f"### ❌ {name}\n")
        lines.append(f"**ID:** `{sid}`  ")
        lines.append(f"**Category:** {CATEGORY_LABELS.get(s.get('category',''), s.get('category',''))}  ")
        lines.append(f"**Duration:** {s.get('duration_seconds', 0):.1f}s\n")

        if s.get("timed_out"):
            lines.append(f"> ⏱ **Timed out.** {s.get('error', '')}\n")
            lines.append("")
            continue

        if s.get("error"):
            lines.append(f"> ⚠️ **Error:** {s['error']}\n")
            lines.append("")
            continue

        # Action mismatch
        actual = s.get("actual_action") or "none"
        acceptable = s.get("acceptable_actions", [])
        if not s.get("action_correct"):
            lines.append(
                f"**Action mismatch:** agent chose `{actual}`, "
                f"expected one of `{acceptable}`.\n"
            )

        # Keyword miss
        if not s.get("keyword_match"):
            expected_kws = []  # not stored in result, but we can note the miss
            lines.append(
                "**Root-cause keywords not found** in trace. "
                "The agent's reasoning may have missed the key signal.\n"
            )

        # Judge feedback
        judge_score = s.get("judge_score")
        if judge_score is not None:
            lines.append(f"**Judge score:** {judge_score}/5")
            lines.append(f"> {s.get('judge_justification', '')}\n")
            missed = s.get("judge_missed_signals", "")
            if missed:
                lines.append(f"**Missed signals:** {missed}\n")

        # Trace excerpt — first failed thought
        trace = s.get("trace_text", "")
        if trace:
            excerpt = trace[:600].strip()
            lines.append("<details>")
            lines.append("<summary>Trace excerpt (first 600 chars)</summary>\n")
            lines.append("```")
            lines.append(excerpt)
            lines.append("```")
            lines.append("</details>\n")

        lines.append("")

    return "\n".join(lines)


def _section_regression(current: dict, previous: dict) -> str:
    cur_map = {s["scenario_id"]: s for s in current.get("scenarios", [])}
    prev_map = {s["scenario_id"]: s for s in previous.get("scenarios", [])}

    regressions: list[str] = []
    improvements: list[str] = []

    for sid, cur in cur_map.items():
        prev = prev_map.get(sid)
        if not prev:
            continue
        was_passing = prev.get("passed", False)
        now_passing = cur.get("passed", False)
        if was_passing and not now_passing:
            regressions.append(f"- ❌ `{sid}` — was PASS, now FAIL")
        elif not was_passing and now_passing:
            improvements.append(f"- ✅ `{sid}` — was FAIL, now PASS")

    prev_acc = previous.get("accuracy_pct", 0)
    cur_acc  = current.get("accuracy_pct", 0)
    delta    = cur_acc - prev_acc
    arrow    = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"

    lines = [
        "\n## Comparison with Previous Run\n",
        f"Previous accuracy: {prev_acc:.1f}%  ",
        f"Current accuracy:  {cur_acc:.1f}%  ",
        f"{arrow} Delta: {delta:+.1f}%\n",
    ]

    if regressions:
        lines.append("### Regressions (were passing, now failing)\n")
        lines.extend(regressions)
        lines.append("")
    else:
        lines.append("✅ No regressions.\n")

    if improvements:
        lines.append("### Improvements (were failing, now passing)\n")
        lines.extend(improvements)
        lines.append("")

    return "\n".join(lines)


def _section_skipped(scenarios: list[dict]) -> str:
    skipped = [s for s in scenarios if s.get("skipped")]
    if not skipped:
        return ""
    lines = ["\n## Skipped Scenarios\n"]
    for s in skipped:
        lines.append(f"- `{s['scenario_id']}` — {s.get('skip_reason', 'unknown reason')}")
    return "\n".join(lines)


# ── Main report assembly ───────────────────────────────────────────────────

def generate_report(results_path: Path, previous_path: Path | None = None) -> str:
    data = _load(results_path)
    scenarios = data.get("scenarios", [])

    ts = data.get("run_id", "?")
    try:
        dt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = ts

    sections = [
        f"# SiteMedic Eval Report — {dt}\n",
        _section_summary(data),
        _section_by_category(data),
        _section_scenario_table(scenarios),
        _section_failures(scenarios),
        _section_skipped(scenarios),
    ]

    if previous_path:
        previous = _load(previous_path)
        sections.append(_section_regression(data, previous))

    sections.append(
        "\n---\n_Generated by `evals/report.py` · "
        "[SiteMedic](https://github.com/your-org/sitemedic)_\n"
    )

    return "\n".join(sections)


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Markdown eval report")
    parser.add_argument(
        "--results", default=None,
        help="Path to results JSON (default: latest in evals/results/)",
    )
    parser.add_argument(
        "--compare", default=None,
        help="Path to a previous results JSON for regression comparison",
    )
    parser.add_argument(
        "--output", default=None,
        help="Write report to this file instead of stdout",
    )
    args = parser.parse_args()

    results_path = Path(args.results) if args.results else _latest_results_file()
    if not results_path or not results_path.exists():
        print("No results file found. Run runner.py first.", file=sys.stderr)
        sys.exit(1)

    previous_path = Path(args.compare) if args.compare else None

    report = generate_report(results_path, previous_path)

    if args.output:
        Path(args.output).write_text(report)
        print(f"Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
