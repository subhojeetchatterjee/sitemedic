"""
Cost estimation for SiteMedic remediation actions.

Two public entry points:
  get_current_traffic_pattern(service) — Gemini calls this as a tool to learn
      whether the incident is happening during peak or trough traffic.
  estimate_remediation_cost(action, params) — called by the orchestrator
      post-plan to fill estimated_hourly_cost_delta_usd before storing.

Pricing strategy (layered):
  1. Cloud Billing Catalog API → exact SKU unit price for the project's region.
  2. Firestore SKU cache (1-hour TTL) — avoids Billing API rate limits.
  3. Hardcoded published rates — fallback when the API is unavailable.

Service account needs:
  roles/billing.viewer  (to read the catalog)
  roles/monitoring.viewer (for traffic pattern)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import billing_v1, monitoring_v3
from tools import firestore_client

logger = logging.getLogger(__name__)

# ── Billing API constants ──────────────────────────────────────────────────

# Cloud Run service ID in the GCP Billing Catalog (stable; published by Google)
_CLOUD_RUN_BILLING_SERVICE = "services/95FF-2EF5-5EA1"

# Fallback published rates (us-central1, 2024) — used when Billing API is unavailable
# Source: https://cloud.google.com/run/pricing
_FALLBACK_RATES: dict[str, float] = {
    # Cloud Run — idle allocation (min-instances > 0)
    "cloud_run_cpu_idle_per_vcpu_hour":    0.000011 * 3600,   # $0.0396 / vCPU-hr
    "cloud_run_mem_idle_per_gb_hour":      0.0000025 * 3600,  # $0.0090 / GB-hr
    # Cloud Run — assumed per-instance config (1 vCPU, 512 MB)
    "cloud_run_instance_idle_per_hour":    0.0396 + 0.0045,   # ≈ $0.044 / instance-hr
    # Cloud Storage $/GB/month → $/GB/hr
    "gcs_standard_per_gb_hour":  0.020 / 730,
    "gcs_nearline_per_gb_hour":  0.010 / 730,
    "gcs_coldline_per_gb_hour":  0.004 / 730,
    "gcs_archive_per_gb_hour":   0.0012 / 730,
}

# Traffic thresholds: ratio of current RPS to 30-day peak
_TROUGH_RATIO = 0.20   # below 20% of peak → trough
_PEAK_RATIO   = 0.80   # above 80% of peak → peak


# ── SKU cache helpers (Firestore) ──────────────────────────────────────────

_SKU_CACHE_COLLECTION = "sku_price_cache"
_SKU_CACHE_TTL_HOURS  = 1


async def _get_cached_sku(cache_key: str) -> float | None:
    doc = await firestore_client._client().collection(_SKU_CACHE_COLLECTION).document(cache_key).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    expires_at = data.get("ttl_expires_at")
    if expires_at and expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return data.get("price_per_unit")


async def _set_cached_sku(cache_key: str, price_per_unit: float, unit: str, description: str) -> None:
    await firestore_client._client().collection(_SKU_CACHE_COLLECTION).document(cache_key).set({
        "price_per_unit": price_per_unit,
        "unit": unit,
        "description": description,
        "fetched_at": datetime.now(timezone.utc),
        "ttl_expires_at": datetime.now(timezone.utc) + timedelta(hours=_SKU_CACHE_TTL_HOURS),
    })


# ── Billing API fetcher ────────────────────────────────────────────────────

def _parse_sku_price(sku) -> float | None:
    """Extract $/unit from a Billing Catalog SKU object."""
    try:
        pi = sku.pricing_info[0]
        tiers = pi.pricing_expression.tiered_rates
        if not tiers:
            return None
        # Use the first (usually only) tier's unit price
        money = tiers[0].unit_price
        return money.units + money.nanos / 1e9
    except (IndexError, AttributeError):
        return None


async def _fetch_sku_price_from_billing(description_keyword: str, region: str) -> float | None:
    """
    Query the Cloud Billing Catalog for a Cloud Run SKU matching description_keyword.
    Returns the $/unit price or None on failure.
    """
    def _sync() -> float | None:
        try:
            client = billing_v1.CloudCatalogClient()
            skus = client.list_skus(parent=_CLOUD_RUN_BILLING_SERVICE)
            for sku in skus:
                desc = sku.description.lower()
                applicable_regions = list(sku.service_regions)
                if description_keyword.lower() in desc and (
                    not applicable_regions or region in applicable_regions or "global" in applicable_regions
                ):
                    price = _parse_sku_price(sku)
                    if price is not None:
                        return price
        except Exception as exc:
            logger.warning(f"Billing API unavailable ({exc}); using fallback rates")
        return None

    return await asyncio.to_thread(_sync)


async def _get_cloud_run_cpu_idle_rate() -> float:
    """$/vCPU-second for Cloud Run idle allocation (cached 1 hr)."""
    cache_key = f"cloud_run_cpu_idle_{os.environ.get('GCP_REGION', 'us-central1')}"
    cached = await _get_cached_sku(cache_key)
    if cached is not None:
        return cached

    fetched = await _fetch_sku_price_from_billing("cpu allocation time", os.environ.get("GCP_REGION", "us-central1"))
    price = fetched if fetched is not None else 0.000011  # fallback
    await _set_cached_sku(cache_key, price, "vCPU-second", "Cloud Run CPU idle allocation")
    return price


async def _get_cloud_run_mem_idle_rate() -> float:
    """$/GB-second for Cloud Run idle memory allocation (cached 1 hr)."""
    cache_key = f"cloud_run_mem_idle_{os.environ.get('GCP_REGION', 'us-central1')}"
    cached = await _get_cached_sku(cache_key)
    if cached is not None:
        return cached

    fetched = await _fetch_sku_price_from_billing("memory allocation time", os.environ.get("GCP_REGION", "us-central1"))
    price = fetched if fetched is not None else 0.0000025  # fallback
    await _set_cached_sku(cache_key, price, "GB-second", "Cloud Run memory idle allocation")
    return price


# ── Traffic pattern ────────────────────────────────────────────────────────

async def get_current_traffic_pattern(service: str) -> dict:
    """
    Classify current traffic as 'peak', 'trough', or 'normal' by comparing
    the last 30-minute request rate to the 7-day rolling peak.

    Returns a JSON-serialisable dict Gemini can use for cost-conscious planning.
    """
    def _sync() -> dict:
        try:
            client = monitoring_v3.MetricServiceClient()
            project_name = f"projects/{os.environ['GCP_PROJECT_ID']}"
            now = datetime.now(timezone.utc)

            def _avg_rps(minutes_back: int, window_minutes: int) -> float:
                start = now - timedelta(minutes=minutes_back + window_minutes)
                end   = now - timedelta(minutes=minutes_back)
                interval = monitoring_v3.TimeInterval(
                    end_time={"seconds": int(end.timestamp())},
                    start_time={"seconds": int(start.timestamp())},
                )
                req = monitoring_v3.ListTimeSeriesRequest(
                    name=project_name,
                    filter=(
                        f'metric.type="run.googleapis.com/request_count" '
                        f'AND resource.label.service_name="{service}"'
                    ),
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                )
                total = 0.0
                count = 0
                for ts in client.list_time_series(request=req):
                    for pt in ts.points:
                        total += pt.value.int64_value or pt.value.double_value
                        count += 1
                if count == 0:
                    return 0.0
                # total is request count over the window; convert to RPS
                return total / (window_minutes * 60)

            current_rps = _avg_rps(0, 30)        # last 30 min
            # 7-day peak: sample 10 windows of 30 min spread over 7 days
            peak_rps = max(
                (_avg_rps(i * 24 * 60, 30) for i in range(7)),
                default=current_rps,
            ) or current_rps or 1.0

            ratio = current_rps / peak_rps if peak_rps > 0 else 0.0

            if ratio <= _TROUGH_RATIO:
                pattern = "trough"
            elif ratio >= _PEAK_RATIO:
                pattern = "peak"
            else:
                pattern = "normal"

            return {
                "pattern": pattern,
                "current_rps": round(current_rps, 2),
                "peak_rps": round(peak_rps, 2),
                "ratio": round(ratio, 3),
                "sampled_at": now.isoformat(),
                "interpretation": (
                    f"Traffic is at {round(ratio * 100)}% of 7-day peak. "
                    f"This is a {pattern} window — "
                    + (
                        "prefer cost-optimised remediation (e.g. min-instances=0, deferred rollback)."
                        if pattern == "trough"
                        else "execute the most reliable remediation immediately."
                    )
                ),
            }
        except Exception as exc:
            logger.warning(f"Traffic pattern check failed: {exc}")
            return {
                "pattern": "normal",
                "current_rps": 0.0,
                "peak_rps": 0.0,
                "ratio": 0.0,
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "interpretation": "Could not determine traffic pattern; defaulting to normal.",
            }

    return await asyncio.to_thread(_sync)


# ── Cost estimation ────────────────────────────────────────────────────────

async def estimate_remediation_cost(action: str, params: dict) -> dict:
    """
    Compute the estimated hourly cost delta (USD) for a remediation action.
    Positive = more expensive; negative = savings.

    Returns a CostEstimate dict ready to merge into RemediationPlan.
    """
    try:
        delta, breakdown, assumptions = await _compute_cost_delta(action, params)
    except Exception as exc:
        logger.warning(f"Cost estimate failed for {action}: {exc}")
        delta, breakdown, assumptions = 0.0, {}, [f"Estimate unavailable: {exc}"]

    return {
        "estimated_hourly_cost_delta_usd": round(delta, 4),
        "cost_breakdown": breakdown,
        "cost_assumptions": assumptions,
    }


async def _compute_cost_delta(action: str, params: dict) -> tuple[float, dict, list[str]]:
    """Return (hourly_delta_usd, breakdown_dict, assumptions_list)."""

    if action == "scale_service":
        return await _cost_scale_service(params)

    if action in ("rollback_revision", "restart_service"):
        # Traffic reroute / restart — no persistent cost change
        return 0.0, {}, ["Rollback/restart does not change resource allocation; cost delta ≈ $0/hr."]

    if action == "no_action_needed":
        return 0.0, {}, ["No action — no cost change."]

    if action in ("failover_cloud_sql_replica", "restart_cloud_sql_instance"):
        return 0.0, {}, ["Cloud SQL failover/restart uses existing tier; no hourly cost change."]

    if action == "change_bucket_storage_class":
        return await _cost_change_storage_class(params)

    if action == "enable_bucket_versioning":
        return 0.0, {}, [
            "Enabling versioning incurs a small cost for additional object versions. "
            "Exact delta depends on write frequency and object churn; not estimable without usage data."
        ]

    if action in ("seek_subscription_to_timestamp", "purge_pubsub_subscription_backlog",
                  "query_subscription_backlog"):
        return 0.0, {}, ["Pub/Sub seek/purge has negligible cost impact."]

    return 0.0, {}, [f"No cost model for action '{action}'."]


async def _cost_scale_service(params: dict) -> tuple[float, dict, list[str]]:
    """
    Cloud Run scale_service cost delta.
    Assumes 1 vCPU + 512 MB per instance (GCP minimum allocation).
    """
    new_min = params.get("min_instances") or 0
    # We don't know the current min-instances without querying Cloud Run; assume 0.
    current_min = 0
    delta_instances = new_min - current_min

    cpu_rate = await _get_cloud_run_cpu_idle_rate()   # $/vCPU-second
    mem_rate = await _get_cloud_run_mem_idle_rate()   # $/GB-second

    vcpu = 1.0
    gb   = 0.5

    per_instance_per_hour = (cpu_rate * vcpu + mem_rate * gb) * 3600

    hourly_delta = delta_instances * per_instance_per_hour
    breakdown = {
        "delta_instances": delta_instances,
        "cpu_usd_per_instance_hr": round(cpu_rate * vcpu * 3600, 4),
        "mem_usd_per_instance_hr": round(mem_rate * gb * 3600, 4),
        "per_instance_usd_per_hr": round(per_instance_per_hour, 4),
    }
    assumptions = [
        f"Current min-instances assumed to be {current_min} (fetching live config not attempted).",
        f"Instance config assumed: {vcpu} vCPU, {gb} GB memory.",
        f"Idle CPU rate: ${cpu_rate:.8f}/vCPU-second, Memory rate: ${mem_rate:.8f}/GB-second.",
    ]
    return hourly_delta, breakdown, assumptions


async def _cost_change_storage_class(params: dict) -> tuple[float, dict, list[str]]:
    """
    GCS storage class change: compute $/GB/hr delta using published tier rates.
    Bucket size is unknown without a GCS list_blobs call, so we return per-GB estimate.
    """
    storage_class = (params.get("storage_class") or "NEARLINE").upper()
    rate_key_map = {
        "STANDARD": "gcs_standard_per_gb_hour",
        "NEARLINE":  "gcs_nearline_per_gb_hour",
        "COLDLINE":  "gcs_coldline_per_gb_hour",
        "ARCHIVE":   "gcs_archive_per_gb_hour",
    }
    current_rate = _FALLBACK_RATES["gcs_standard_per_gb_hour"]
    new_rate = _FALLBACK_RATES.get(rate_key_map.get(storage_class, ""), current_rate)
    rate_delta_per_gb = new_rate - current_rate

    breakdown = {
        "current_class": "STANDARD",
        "new_class": storage_class,
        "current_rate_usd_per_gb_hr": round(current_rate, 8),
        "new_rate_usd_per_gb_hr": round(new_rate, 8),
        "delta_per_gb_hr": round(rate_delta_per_gb, 8),
    }
    assumptions = [
        "Assumes current storage class is STANDARD (most common).",
        "Actual total delta = delta_per_gb_hr × bucket_size_GB.",
        "Retrieval costs (for NEARLINE+) not included; may add cost if reads are frequent.",
    ]
    # Return per-GB hourly rate delta (caller can note this is per-GB)
    return rate_delta_per_gb, breakdown, assumptions
