"use client";

import Link from "next/link";
import type { Prediction } from "@/lib/types";

const ACTION_LABELS: Record<string, string> = {
  rollback_revision:  "Rollback Revision",
  scale_service:      "Scale Service",
  restart_service:    "Restart Service",
  no_action_needed:   "No Action Needed",
};

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const color =
    pct >= 85 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-yellow-500";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-xs font-semibold tabular-nums ${
        pct >= 85 ? "text-red-400" : pct >= 70 ? "text-amber-400" : "text-yellow-400"
      }`}>{pct}%</span>
    </div>
  );
}

function minutesRemaining(expiresAt: string): number {
  const ms = new Date(expiresAt).getTime() - Date.now();
  return Math.max(0, Math.round(ms / 60_000));
}

export default function PredictionCard({ prediction }: { prediction: Prediction }) {
  const confidencePct = Math.round(prediction.confidence * 100);
  const isHighConfidence = confidencePct >= 85;
  const minsLeft = minutesRemaining(prediction.expires_at);
  const validated = prediction.prediction_validated;

  const borderColor = validated
    ? "border-green-700"
    : isHighConfidence
      ? "border-amber-600"
      : "border-amber-900";

  const content = (
    <div className={`border rounded-lg p-4 bg-gray-900 hover:bg-gray-850 transition-colors cursor-pointer ${borderColor}`}>
      {/* Header row */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${
            validated ? "bg-green-500" : "bg-amber-400 animate-pulse"
          }`} />
          <div className="min-w-0">
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-0.5">Forecast</p>
            <p className="font-medium text-gray-100 truncate font-mono text-sm">
              {prediction.service}
            </p>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
            validated
              ? "bg-green-900 text-green-300"
              : "bg-amber-900 text-amber-300"
          }`}>
            {validated ? "Validated" : `Breach in ~${prediction.predicted_breach_in_minutes} min`}
          </span>
          <p className="text-xs text-gray-600 mt-1">
            {minsLeft > 0 ? `${minsLeft} min window left` : "Window expired"}
          </p>
        </div>
      </div>

      {/* Confidence bar */}
      <div className="mb-3">
        <p className="text-xs text-gray-500 mb-1">Confidence</p>
        <ConfidenceBar confidence={prediction.confidence} />
      </div>

      {/* Trend description */}
      <p className="text-xs text-gray-400 leading-relaxed mb-3 line-clamp-2">
        {prediction.trend_description}
      </p>

      {/* Leading indicators */}
      {prediction.leading_indicator_metrics.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-3">
          {prediction.leading_indicator_metrics.map(m => (
            <span key={m} className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 font-mono">
              {m.split(":")[0].split("/").pop() ?? m}
            </span>
          ))}
        </div>
      )}

      {/* Recommended action */}
      {prediction.recommended_preemptive_action && (
        <div className="border-t border-gray-800 pt-2 flex items-center justify-between">
          <span className="text-xs text-gray-500">Suggested preemptive action</span>
          <span className="text-xs font-mono text-amber-300 font-medium">
            {ACTION_LABELS[prediction.recommended_preemptive_action] ?? prediction.recommended_preemptive_action}
          </span>
        </div>
      )}

      {/* Validated badge */}
      {validated && prediction.materialized_incident_id && (
        <div className="border-t border-gray-800 pt-2 mt-2 flex items-center gap-2">
          <span className="text-xs text-green-400">Prediction confirmed</span>
          <span className="text-xs font-mono text-gray-500">→ {prediction.materialized_incident_id}</span>
        </div>
      )}
    </div>
  );

  // If validated, link to the materialized incident
  if (validated && prediction.materialized_incident_id) {
    return (
      <Link href={`/incidents/${prediction.materialized_incident_id}`}>
        {content}
      </Link>
    );
  }

  // High-confidence predictions link to the PREDICTIVE incident
  const predictiveIncidentId = `predictive_pred_${prediction.service}_${
    new Date(prediction.created_at).toISOString().slice(0, 16).replace(/[-:T]/g, "").slice(0, 15)
  }`;
  return <div>{content}</div>;
}
