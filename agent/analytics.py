"""
Analytics snapshot computation for the /analytics dashboard.

compute_snapshot(window_days) reads all relevant incidents + predictions from
Firestore, aggregates them into a compact payload, and caches the result in the
analytics_snapshots collection. Snapshots expire after SNAPSHOT_TTL_SECONDS (1 h).

Cloud Scheduler triggers POST /api/analytics/refresh hourly so that the
dashboard always returns a pre-computed document — never touches the raw
incidents collection at page-load time.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SNAPSHOT_TTL_SECONDS = 3600  # 1 hour


# ── Timestamp normalisation ────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    """Parse a timestamp regardless of whether it's an ISO string, Python datetime,
    or a Firestore Timestamp object."""
    if value is None:
        return None
    # Python datetime (possibly naive)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # Firestore Timestamp: has a .timestamp() method but no .tzinfo attribute
    if hasattr(value, "timestamp") and not isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)
        except Exception:
            return None
    # ISO string
    if isinstance(value, str):
        try:
            s = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


# ── Entry points ───────────────────────────────────────────────────────────

async def get_or_refresh_snapshot(window_days: int) -> dict:
    """Return a fresh snapshot from cache, or compute one if stale/absent."""
    from tools import firestore_client

    key = f"{window_days}d"
    cached = await firestore_client.get_analytics_snapshot(key)
    if cached:
        computed_at = _parse_ts(cached.get("computed_at"))
        if computed_at:
            age = (datetime.now(timezone.utc) - computed_at).total_seconds()
            if age < SNAPSHOT_TTL_SECONDS:
                return cached

    return await compute_snapshot(window_days)


async def compute_snapshot(window_days: int) -> dict:
    """Full recompute. Fetches incidents + predictions, builds snapshot, persists it."""
    from tools import firestore_client

    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    incidents = await firestore_client.list_incidents_for_analytics(window_days)
    predictions = await firestore_client.list_predictions_for_analytics(window_days)

    # Filter to window (Firestore query may return extras due to string ordering)
    incidents = [i for i in incidents if (_parse_ts(i.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
    predictions = [p for p in predictions if (_parse_ts(p.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]

    snapshot = {
        "window": f"{window_days}d",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "incident_count": len(incidents),
        "summary":            _compute_summary(incidents),
        "incident_volume":    _compute_volume(incidents, window_days),
        "mttr_trend":         _compute_mttr_trend(incidents, window_days),
        "failure_patterns":   _compute_failure_patterns(incidents),
        "prediction_accuracy": _compute_prediction_accuracy(predictions, window_days),
        "services":           _compute_service_breakdown(incidents),
        "cost":               _compute_cost_impact(incidents),
        "reasoning":          _compute_reasoning_quality(incidents),
    }

    await firestore_client.set_analytics_snapshot(f"{window_days}d", snapshot)
    logger.info("Analytics snapshot computed: window=%dd incidents=%d", window_days, len(incidents))
    return snapshot


# ── Aggregation helpers ────────────────────────────────────────────────────

def _compute_summary(incidents: list[dict]) -> dict:
    resolved  = [i for i in incidents if i.get("status") == "RESOLVED"]
    rejected  = [i for i in incidents if i.get("status") == "REJECTED"]

    # Approval rate = resolved+remediating / (resolved+rejected+remediating)
    reached_decision = [
        i for i in incidents
        if i.get("status") in ("REMEDIATING", "RESOLVED", "REJECTED")
    ]
    approved_count = len([i for i in reached_decision if i.get("status") in ("REMEDIATING", "RESOLVED")])
    approval_rate = approved_count / len(reached_decision) if reached_decision else 0.0

    # MTTDi: started_at → last diagnosis trace step (step < 999)
    mttdi: list[float] = []
    for inc in incidents:
        started = _parse_ts(inc.get("started_at"))
        diag = [s for s in (inc.get("trace") or []) if isinstance(s, dict) and s.get("step", 999) < 999]
        if started and diag:
            last_ts = _parse_ts(max(diag, key=lambda s: s.get("step", 0)).get("timestamp"))
            if last_ts and last_ts > started:
                mttdi.append((last_ts - started).total_seconds() / 60)

    # MTTR: started_at → updated_at for RESOLVED
    mttr: list[float] = []
    for inc in resolved:
        started = _parse_ts(inc.get("started_at"))
        updated = _parse_ts(inc.get("updated_at"))
        if started and updated and updated > started:
            mttr.append((updated - started).total_seconds() / 60)

    return {
        "total_incidents":     len(incidents),
        "resolved":            len(resolved),
        "rejected":            len(rejected),
        "mttd_seconds":        30,          # detection loop cadence; exact value needs DT timestamp
        "mttdi_minutes":       round(sum(mttdi) / len(mttdi), 2) if mttdi else 0,
        "mttr_minutes":        round(sum(mttr) / len(mttr), 2) if mttr else 0,
        "approval_rate":       round(approval_rate, 3),
        "auto_resolution_rate": round(len(resolved) / len(incidents), 3) if incidents else 0,
    }


def _day_str(value) -> str | None:
    ts = _parse_ts(value)
    return ts.date().isoformat() if ts else None


def _date_range(window_days: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=d)).isoformat() for d in range(window_days - 1, -1, -1)]


def _compute_volume(incidents: list[dict], window_days: int) -> list[dict]:
    """Daily incident counts, one column per service."""
    services: set[str] = set()
    daily: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for inc in incidents:
        day = _day_str(inc.get("started_at"))
        svc = inc.get("service", "unknown")
        if day:
            services.add(svc)
            daily[day][svc] += 1

    result = []
    for day in _date_range(window_days):
        row: dict = {"date": day}
        for svc in sorted(services):
            row[svc] = daily[day].get(svc, 0)
        result.append(row)
    return result


def _compute_mttr_trend(incidents: list[dict], window_days: int) -> list[dict]:
    """Daily average MTTR (minutes) for RESOLVED incidents."""
    daily: dict[str, list[float]] = defaultdict(list)

    for inc in incidents:
        if inc.get("status") != "RESOLVED":
            continue
        started = _parse_ts(inc.get("started_at"))
        updated = _parse_ts(inc.get("updated_at"))
        if started and updated and updated > started:
            day = started.date().isoformat()
            daily[day].append((updated - started).total_seconds() / 60)

    result = []
    for day in _date_range(window_days):
        vals = daily.get(day, [])
        result.append({
            "date": day,
            "mttr": round(sum(vals) / len(vals), 2) if vals else None,
        })
    return result


def _compute_failure_patterns(incidents: list[dict]) -> list[dict]:
    counts: dict[str, int] = defaultdict(int)
    for inc in incidents:
        plan = inc.get("plan")
        action = plan.get("action", "unknown") if isinstance(plan, dict) else "no_plan"
        counts[action] += 1
    return sorted(
        [{"action": k, "count": v} for k, v in counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]


def _compute_prediction_accuracy(predictions: list[dict], window_days: int) -> list[dict]:
    daily: dict[str, dict[str, int]] = defaultdict(lambda: {"validated": 0, "fp": 0, "total": 0})

    for pred in predictions:
        day = _day_str(pred.get("created_at"))
        if not day:
            continue
        daily[day]["total"] += 1
        if pred.get("prediction_validated"):
            daily[day]["validated"] += 1
        if pred.get("prediction_false_positive"):
            daily[day]["fp"] += 1

    result = []
    for day in _date_range(window_days):
        row = daily.get(day, {"validated": 0, "fp": 0, "total": 0})
        total = row["total"]
        result.append({
            "date": day,
            "precision": round(row["validated"] / total, 3) if total > 0 else None,
            "total": total,
        })
    return result


def _compute_service_breakdown(incidents: list[dict]) -> list[dict]:
    data: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "resolved": 0, "rejected": 0,
        "mttr_vals": [], "actions": defaultdict(int),
    })

    for inc in incidents:
        svc = inc.get("service", "unknown")
        data[svc]["total"] += 1
        if inc.get("status") == "RESOLVED":
            data[svc]["resolved"] += 1
            started = _parse_ts(inc.get("started_at"))
            updated = _parse_ts(inc.get("updated_at"))
            if started and updated and updated > started:
                data[svc]["mttr_vals"].append((updated - started).total_seconds() / 60)
        elif inc.get("status") == "REJECTED":
            data[svc]["rejected"] += 1
        plan = inc.get("plan")
        if isinstance(plan, dict) and plan.get("action"):
            data[svc]["actions"][plan["action"]] += 1

    result = []
    for name, d in sorted(data.items(), key=lambda x: x[1]["total"], reverse=True):
        vals = d["mttr_vals"]
        result.append({
            "name": name,
            "total": d["total"],
            "resolved": d["resolved"],
            "rejected": d["rejected"],
            "avg_mttr": round(sum(vals) / len(vals), 2) if vals else 0,
            "common_actions": sorted(
                [{"action": a, "count": c} for a, c in d["actions"].items()],
                key=lambda x: x["count"],
                reverse=True,
            )[:5],
        })
    return result


def _compute_cost_impact(incidents: list[dict]) -> dict:
    total_delta = 0.0
    remediation_count = 0
    alt_adopted = 0

    for inc in incidents:
        plan = inc.get("plan")
        if not isinstance(plan, dict):
            continue
        if inc.get("status") in ("RESOLVED", "REMEDIATING"):
            remediation_count += 1
            total_delta += float(plan.get("estimated_hourly_cost_delta_usd") or 0)
        if plan.get("cost_optimized_alternative") and inc.get("status") == "RESOLVED":
            alt_adopted += 1

    return {
        "total_delta_usd": round(total_delta, 4),
        "remediation_count": remediation_count,
        "alternative_adopted_count": alt_adopted,
        "alternative_adoption_rate": (
            round(alt_adopted / remediation_count, 3) if remediation_count else 0
        ),
    }


def _compute_reasoning_quality(incidents: list[dict]) -> dict:
    step_counts: list[int] = []
    tool_counts: dict[str, int] = defaultdict(int)

    for inc in incidents:
        trace = inc.get("trace") or []
        diag = [s for s in trace if isinstance(s, dict) and s.get("step", 999) < 999]
        if not diag:
            continue
        step_counts.append(len(diag))
        for step in diag:
            tc = step.get("tool_call")
            if isinstance(tc, dict) and tc.get("name"):
                tool_counts[tc["name"]] += 1

    return {
        "avg_steps": round(sum(step_counts) / len(step_counts), 2) if step_counts else 0,
        "tool_distribution": sorted(
            [{"tool": k, "count": v} for k, v in tool_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:15],
    }
