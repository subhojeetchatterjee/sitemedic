"use client";

import { useEffect, useState } from "react";
import type { DryRunReport, RemediationPlan } from "@/lib/types";
import DryRunReportView from "./DryRunReportView";

// ── Helpers ────────────────────────────────────────────────────────────────

function formatCostDelta(delta: number | undefined): string | null {
  if (delta === undefined || delta === null) return null;
  if (Math.abs(delta) < 0.0001) return "$0.00/hr";
  const sign = delta >= 0 ? "+" : "−";
  return `${sign}$${Math.abs(delta).toFixed(4)}/hr`;
}

const ACTION_LABELS: Record<string, string> = {
  rollback_revision:                 "Rollback Revision",
  scale_service:                     "Scale Service",
  restart_service:                   "Restart Service",
  no_action_needed:                  "No Action Needed",
  failover_cloud_sql_replica:        "Failover Cloud SQL Replica",
  restart_cloud_sql_instance:        "Restart Cloud SQL Instance",
  change_bucket_storage_class:       "Change Bucket Storage Class",
  enable_bucket_versioning:          "Enable Bucket Versioning",
  seek_subscription_to_timestamp:    "Seek Subscription to Timestamp",
  purge_pubsub_subscription_backlog: "Purge Pub/Sub Subscription Backlog",
};

const SAFETY_STYLES: Record<string, { badge: string; border: string }> = {
  reversible:        { badge: "bg-green-900 text-green-300",  border: "border-gray-800" },
  "non-destructive": { badge: "bg-blue-900 text-blue-300",    border: "border-gray-800" },
  destructive:       { badge: "bg-red-900 text-red-300",      border: "border-red-800" },
};

function primaryResourceId(plan: RemediationPlan): string | null {
  if (plan.action === "purge_pubsub_subscription_backlog") return plan.subscription ?? null;
  return null;
}

// ── Plan summary card (step 1) ─────────────────────────────────────────────

function PlanCard({ plan, label }: { plan: RemediationPlan; label?: string }) {
  const isDestructive = plan.rollback_safety === "destructive" || plan.requires_explicit_confirmation;
  const safety = plan.rollback_safety ?? (plan.rollback_safe ? "reversible" : "destructive");
  const safetyStyle = SAFETY_STYLES[safety] ?? SAFETY_STYLES.reversible;
  const confidencePct = Math.round(plan.confidence * 100);
  const confidenceColor =
    confidencePct >= 80 ? "text-green-400" : confidencePct >= 60 ? "text-yellow-400" : "text-red-400";
  const costStr = formatCostDelta(plan.estimated_hourly_cost_delta_usd);
  const costColor =
    (plan.estimated_hourly_cost_delta_usd ?? 0) <= 0 ? "text-green-400" : "text-yellow-400";

  return (
    <div className={`border rounded-lg p-4 bg-gray-900 space-y-3 ${
      isDestructive ? safetyStyle.border : "border-gray-800"
    }`}>
      {label && (
        <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">{label}</p>
      )}
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-gray-100">
            {ACTION_LABELS[plan.action] ?? plan.action}
          </p>
          {plan.service && (
            <p className="font-mono text-xs text-gray-400 mt-0.5">{plan.service}</p>
          )}
        </div>
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium shrink-0 ${safetyStyle.badge}`}>
          {safety}
        </span>
      </div>
      <p className="text-sm text-gray-400 leading-relaxed">{plan.reason}</p>
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <p className="text-xs text-gray-500 mb-0.5">Confidence</p>
          <p className={`font-semibold ${confidenceColor}`}>{confidencePct}%</p>
        </div>
        <div>
          <p className="text-xs text-gray-500 mb-0.5">Safety</p>
          <p className={plan.rollback_safe ? "text-green-400" : "text-red-400"}>
            {plan.rollback_safe ? "Safe to execute" : "Risk of data loss"}
          </p>
        </div>
        {costStr && (
          <div>
            <p className="text-xs text-gray-500 mb-0.5">Est. Cost Delta</p>
            <p className={`font-semibold font-mono ${costColor}`}>{costStr}</p>
          </div>
        )}
        {plan.traffic_context && (
          <div>
            <p className="text-xs text-gray-500 mb-0.5">Traffic Window</p>
            <p className={`font-semibold capitalize ${
              plan.traffic_context === "trough" ? "text-blue-400" :
              plan.traffic_context === "peak" ? "text-red-400" : "text-gray-300"
            }`}>{plan.traffic_context}</p>
          </div>
        )}
      </div>
      <div>
        <p className="text-xs text-gray-500 mb-0.5">Expected Outcome</p>
        <p className="text-sm text-gray-400 italic">{plan.estimated_impact}</p>
      </div>
    </div>
  );
}

// ── Step indicator ─────────────────────────────────────────────────────────

function StepDots({
  current,
  total,
}: {
  current: number;
  total: number;
}) {
  return (
    <div className="flex items-center gap-1.5">
      {Array.from({ length: total }, (_, i) => (
        <div
          key={i}
          className={`h-1.5 rounded-full transition-all ${
            i < current
              ? "w-4 bg-green-500"
              : i === current
              ? "w-4 bg-blue-400"
              : "w-1.5 bg-gray-700"
          }`}
        />
      ))}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

type Step = "review" | "preview" | "approve";

export default function PlanReview({
  problemId,
  plan,
  onDecision,
}: {
  problemId: string;
  plan: RemediationPlan;
  onDecision: (approved: boolean) => void;
}) {
  const [step, setStep] = useState<Step>("review");
  const [dryRunReport, setDryRunReport] = useState<DryRunReport | null>(null);
  const [dryRunLoading, setDryRunLoading] = useState(false);
  const [dryRunError, setDryRunError] = useState<string | null>(null);
  const [dryRunViewed, setDryRunViewed] = useState(false);

  const [executing, setExecuting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [showRejectBox, setShowRejectBox] = useState(false);
  const [confirmationInput, setConfirmationInput] = useState("");
  const [selectedPlan, setSelectedPlan] = useState<"primary" | "alternative">("primary");
  const [alwaysDryRun, setAlwaysDryRun] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const activePlan = selectedPlan === "alternative" && plan.cost_optimized_alternative
    ? plan.cost_optimized_alternative
    : plan;

  const isDestructive = activePlan.rollback_safety === "destructive" || activePlan.requires_explicit_confirmation;
  const expectedConfirmation = primaryResourceId(activePlan);
  const confirmationValid = !isDestructive || confirmationInput.trim() === expectedConfirmation;
  const hasAlternative = !!plan.cost_optimized_alternative;
  const savings = (plan.estimated_hourly_cost_delta_usd ?? 0) - (plan.cost_optimized_alternative?.estimated_hourly_cost_delta_usd ?? 0);

  // Dry-run is mandatory for destructive actions; optional globally
  const dryRunRequired = isDestructive || alwaysDryRun;
  // Approve button only enabled once dry-run has been viewed (when required)
  const canApprove = !dryRunRequired || dryRunViewed;

  // Load global always-dry-run setting
  useEffect(() => {
    fetch("/api/settings/global")
      .then(r => r.json())
      .then(d => d?.always_dry_run && setAlwaysDryRun(true))
      .catch(() => {});
  }, []);

  async function runPreview() {
    setDryRunLoading(true);
    setDryRunError(null);
    setStep("preview");
    try {
      const res = await fetch(`/api/incidents/${problemId}/dry-run`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const report: DryRunReport = await res.json();
      setDryRunReport(report);
      setDryRunViewed(true);
    } catch (e) {
      setDryRunError(String(e));
    } finally {
      setDryRunLoading(false);
    }
  }

  async function submit(approved: boolean) {
    setExecuting(true);
    setSubmitError(null);
    try {
      const res = await fetch(
        `/api/incidents/${problemId}/approve`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            approved,
            rejected_reason: rejectReason || null,
            explicit_confirmation: isDestructive ? confirmationInput.trim() : null,
            selected_plan: selectedPlan,
            dry_run: false,
          }),
        },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setSubmitError((err as { detail?: string }).detail || `Request failed (${res.status})`);
        return;
      }
      onDecision(approved);
    } catch (e) {
      setSubmitError(String(e));
    } finally {
      setExecuting(false);
    }
  }

  // ── Step 1: Review plan ─────────────────────────────────────────────────
  if (step === "review") {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Step 1 of 3 — Review Plan</p>
          <StepDots current={0} total={3} />
        </div>

        {/* Plan selector tabs */}
        {hasAlternative && (
          <div className="flex gap-2">
            {(["primary", "alternative"] as const).map(k => (
              <button
                key={k}
                onClick={() => setSelectedPlan(k)}
                className={`flex-1 py-2 rounded-lg text-sm font-medium border transition-colors ${
                  selectedPlan === k
                    ? k === "primary"
                      ? "bg-gray-700 border-orange-500 text-orange-300"
                      : "bg-gray-700 border-green-500 text-green-300"
                    : "bg-gray-900 border-gray-700 text-gray-400 hover:border-gray-500"
                }`}
              >
                {k === "primary" ? "Primary Plan" : "Cost-Optimised"}
                {k === "alternative" && savings > 0 && (
                  <span className="ml-2 text-xs text-green-400">−${savings.toFixed(4)}/hr</span>
                )}
              </button>
            ))}
          </div>
        )}

        {hasAlternative ? (
          <div className="flex gap-4">
            <div className={`flex-1 transition-opacity ${selectedPlan === "primary" ? "opacity-100" : "opacity-40"}`}>
              <PlanCard plan={plan} label="Primary" />
            </div>
            <div className={`flex-1 transition-opacity ${selectedPlan === "alternative" ? "opacity-100" : "opacity-40"}`}>
              <PlanCard plan={plan.cost_optimized_alternative!} label="Cost-Optimised" />
            </div>
          </div>
        ) : (
          <PlanCard plan={plan} />
        )}

        {dryRunRequired && (
          <p className="text-xs text-amber-400 flex items-center gap-1">
            <span>⚠</span>
            {isDestructive
              ? "Destructive action — preview required before approve button enables."
              : "Dry-run mode is on — preview required."}
          </p>
        )}

        <div className="flex gap-3 pt-1">
          <button
            onClick={runPreview}
            className="flex-1 bg-indigo-800 hover:bg-indigo-700 text-white font-semibold py-2.5 rounded-lg transition-colors"
          >
            Preview remediation →
          </button>
          {!dryRunRequired && (
            <button
              onClick={() => setStep("approve")}
              className="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-300 font-semibold py-2.5 rounded-lg border border-gray-700 transition-colors"
            >
              Skip preview
            </button>
          )}
        </div>
      </div>
    );
  }

  // ── Step 2: Dry-run preview ─────────────────────────────────────────────
  if (step === "preview") {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Step 2 of 3 — Preview</p>
          <StepDots current={1} total={3} />
        </div>

        {dryRunLoading && (
          <div className="space-y-3 animate-pulse">
            <div className="h-3 bg-gray-800 rounded w-2/3" />
            <div className="h-24 bg-gray-800 rounded" />
            <div className="h-24 bg-gray-800 rounded" />
          </div>
        )}

        {dryRunError && (
          <div className="border border-red-800 rounded-lg p-4 bg-red-950">
            <p className="text-xs text-red-300 font-medium mb-1">Preview failed</p>
            <p className="text-xs text-red-400 font-mono">{dryRunError}</p>
          </div>
        )}

        {dryRunReport && !dryRunLoading && (
          <DryRunReportView report={dryRunReport} />
        )}

        <div className="flex gap-3 pt-1">
          <button
            onClick={() => setStep("approve")}
            disabled={!dryRunViewed && !dryRunError}
            className="flex-1 bg-green-700 hover:bg-green-600 disabled:opacity-40 text-white font-semibold py-2.5 rounded-lg transition-colors"
          >
            Proceed to approval →
          </button>
          <button
            onClick={() => setStep("review")}
            className="px-4 bg-gray-800 hover:bg-gray-700 text-gray-400 rounded-lg border border-gray-700 transition-colors text-sm"
          >
            ← Back
          </button>
        </div>
      </div>
    );
  }

  // ── Step 3: Approve / reject ────────────────────────────────────────────
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wide">Step 3 of 3 — Approve</p>
        <StepDots current={2} total={3} />
      </div>

      <PlanCard plan={activePlan} />

      {!canApprove && (
        <p className="text-xs text-amber-400 flex items-center gap-1">
          <span>⚠</span> View the dry-run preview before approving.
        </p>
      )}

      {/* Explicit confirmation for destructive actions */}
      {isDestructive && expectedConfirmation && (
        <div className="bg-red-950 border border-red-800 rounded-md p-3 space-y-2">
          <p className="text-xs text-red-300 font-medium">
            Type the resource identifier to confirm this is irreversible:
          </p>
          <p className="font-mono text-xs text-red-200 bg-red-900 px-2 py-1 rounded select-all">
            {expectedConfirmation}
          </p>
          <input
            type="text"
            value={confirmationInput}
            onChange={e => setConfirmationInput(e.target.value)}
            placeholder="Type the resource ID to confirm…"
            className="w-full bg-gray-900 border border-red-700 rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 font-mono focus:outline-none focus:border-red-500"
          />
          {confirmationInput && !confirmationValid && (
            <p className="text-xs text-red-400">Confirmation does not match</p>
          )}
        </div>
      )}

      {showRejectBox && (
        <textarea
          className="w-full bg-gray-800 border border-gray-700 rounded p-2 text-sm text-gray-200 placeholder-gray-500 resize-none"
          rows={3}
          placeholder="Reason for rejection (optional)"
          value={rejectReason}
          onChange={e => setRejectReason(e.target.value)}
        />
      )}

      <div className="flex gap-3">
        <button
          onClick={() => setStep("preview")}
          className="text-xs text-indigo-400 hover:text-indigo-300 self-center"
        >
          ← View preview
        </button>
        <div className="flex-1 flex gap-3">
          <button
            onClick={() => submit(true)}
            disabled={executing || !confirmationValid || !canApprove}
            className={`flex-1 font-semibold py-2.5 rounded-lg transition-colors disabled:opacity-50 ${
              isDestructive
                ? "bg-red-800 hover:bg-red-700 text-white"
                : "bg-green-700 hover:bg-green-600 text-white"
            }`}
          >
            {executing
              ? "Executing…"
              : isDestructive
              ? "Confirm & Execute (Irreversible)"
              : "Approve & Execute"}
          </button>
          <button
            onClick={() => {
              if (!showRejectBox) { setShowRejectBox(true); return; }
              submit(false);
            }}
            disabled={executing}
            className="flex-1 bg-gray-800 hover:bg-red-900 disabled:opacity-50 text-gray-300 hover:text-red-300 font-semibold py-2.5 rounded-lg border border-gray-700 transition-colors"
          >
            Reject
          </button>
        </div>
      </div>
      {submitError && (
        <p className="mt-2 text-sm text-red-400 text-center">{submitError}</p>
      )}
    </div>
  );
}
