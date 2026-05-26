"""
Confidence calibration for SiteMedic diagnoses.

Computes Expected Calibration Error (ECE) over the last N resolved/rejected incidents
that have a structured diagnosis. Results are cached in Firestore and surfaced in
the analytics dashboard.

ECE = sum over buckets of (bucket_count / total) * |bucket_midpoint - bucket_accuracy|
where bucket_accuracy = fraction of incidents in that confidence bucket that were
RESOLVED (i.e. the plan was executed and considered correct) rather than REJECTED.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from tools import firestore_client

logger = logging.getLogger(__name__)

BUCKET_EDGES = [0.0, 0.2, 0.4, 0.6, 0.75, 0.9, 1.01]
BUCKET_LABELS = ["0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.75", "0.75–0.9", "0.9–1.0"]


def _bucket_index(confidence: float) -> int:
    for i, edge in enumerate(BUCKET_EDGES[1:], start=0):
        if confidence < edge:
            return i
    return len(BUCKET_LABELS) - 1


def compute_ece(buckets: list[dict]) -> float:
    total = sum(b["count"] for b in buckets)
    if total == 0:
        return 0.0
    ece = 0.0
    for b in buckets:
        if b["count"] == 0:
            continue
        accuracy = b["resolved"] / b["count"]
        midpoint = (BUCKET_EDGES[b["bucket_idx"]] + BUCKET_EDGES[b["bucket_idx"] + 1]) / 2
        ece += (b["count"] / total) * abs(midpoint - accuracy)
    return round(ece, 4)


async def compute_calibration(lookback: int = 100) -> dict:
    """
    Read the last `lookback` resolved/rejected incidents with a structured diagnosis,
    compute ECE, and return a calibration snapshot dict.
    """
    incidents = await firestore_client.list_incidents_with_diagnosis(limit=lookback * 2)

    buckets: list[dict] = [
        {"bucket_idx": i, "label": BUCKET_LABELS[i], "count": 0, "resolved": 0, "confidences": []}
        for i in range(len(BUCKET_LABELS))
    ]

    used = 0
    for inc in incidents:
        if inc.get("status") not in ("RESOLVED", "REJECTED"):
            continue
        diagnosis = inc.get("diagnosis")
        if not isinstance(diagnosis, dict):
            continue
        conf = diagnosis.get("confidence")
        if conf is None:
            continue
        try:
            conf = float(conf)
        except (ValueError, TypeError):
            continue

        idx = _bucket_index(conf)
        buckets[idx]["count"] += 1
        buckets[idx]["confidences"].append(conf)
        if inc.get("status") == "RESOLVED":
            buckets[idx]["resolved"] += 1
        used += 1
        if used >= lookback:
            break

    # Compute per-bucket accuracy for chart
    for b in buckets:
        b["accuracy"] = round(b["resolved"] / b["count"], 3) if b["count"] else None
        b["avg_confidence"] = (
            round(sum(b["confidences"]) / len(b["confidences"]), 3)
            if b["confidences"] else None
        )
        del b["confidences"]  # don't store raw list

    ece = compute_ece(buckets)

    snapshot = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "lookback_incidents": used,
        "ece": ece,
        "buckets": buckets,
        "interpretation": _interpret_ece(ece),
    }

    await firestore_client.set_calibration_snapshot(snapshot)
    logger.info(f"Calibration computed: ECE={ece} over {used} incidents")
    return snapshot


def _interpret_ece(ece: float) -> str:
    if ece < 0.05:
        return "Well calibrated — confidence scores match observed accuracy."
    if ece < 0.10:
        return "Slightly overconfident or underconfident in some ranges."
    if ece < 0.20:
        return "Moderately miscalibrated — treat confidence scores with caution."
    return "Poorly calibrated — confidence scores are unreliable."
