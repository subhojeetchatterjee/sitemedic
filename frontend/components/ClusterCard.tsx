"use client";

import Link from "next/link";
import type { IncidentCluster } from "@/lib/types";

const STATUS_STYLES: Record<string, string> = {
  FORMING:           "bg-yellow-900 text-yellow-300",
  AWAITING_APPROVAL: "bg-orange-900 text-orange-300",
  EXECUTING:         "bg-purple-900 text-purple-300",
  COMPLETE:          "bg-green-900 text-green-300",
  FAILED:            "bg-red-900 text-red-300",
  PARTIAL:           "bg-amber-900 text-amber-300",
};

const STEP_STATUS_ICON: Record<string, string> = {
  pending: "○",
  running: "◉",
  done:    "●",
  failed:  "✕",
};

const STEP_STATUS_COLOR: Record<string, string> = {
  pending: "text-gray-500",
  running: "text-purple-400",
  done:    "text-green-400",
  failed:  "text-red-400",
};

export default function ClusterCard({ cluster }: { cluster: IncidentCluster }) {
  const statusCls = STATUS_STYLES[cluster.status] ?? "bg-gray-800 text-gray-400";
  const confidencePct = Math.round(cluster.confidence * 100);
  const memberCount = cluster.member_incident_ids.length;
  const isExecuting = cluster.status === "EXECUTING";

  return (
    <Link href={`/clusters/${cluster.cluster_id}`}>
      <div className="border border-orange-800 rounded-lg p-4 bg-gray-900 hover:border-orange-600 transition-colors cursor-pointer">
        {/* Header */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-2 min-w-0">
            <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${
              isExecuting ? "bg-purple-400 animate-pulse" : "bg-orange-400"
            }`} />
            <div className="min-w-0">
              <p className="text-xs text-orange-500 uppercase tracking-wide font-semibold mb-0.5">
                Incident Cluster
              </p>
              <p className="text-sm font-medium text-gray-100 truncate">
                {cluster.root_cause_summary || "Correlated incidents"}
              </p>
            </div>
          </div>
          <div className="shrink-0 flex flex-col items-end gap-1">
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${statusCls}`}>
              {cluster.status.replace("_", " ")}
            </span>
            <span className="text-xs text-gray-500">
              {memberCount} incident{memberCount !== 1 ? "s" : ""}
            </span>
          </div>
        </div>

        {/* Member services */}
        {cluster.execution_order.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-3">
            {cluster.execution_order.slice(0, 4).map((svc, i) => (
              <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 font-mono">
                {svc}
              </span>
            ))}
            {cluster.execution_order.length > 4 && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-500">
                +{cluster.execution_order.length - 4} more
              </span>
            )}
          </div>
        )}

        {/* Execution timeline preview */}
        {cluster.coordinated_plan.length > 0 && (
          <div className="border-t border-gray-800 pt-2 flex items-center gap-3 flex-wrap">
            <span className="text-xs text-gray-500">Plan:</span>
            {cluster.coordinated_plan.slice(0, 5).map((step) => (
              <span
                key={step.step_index}
                className={`text-xs font-mono ${STEP_STATUS_COLOR[step.status]}`}
                title={`${step.action} on ${step.service}`}
              >
                {STEP_STATUS_ICON[step.status]} {step.service.split("-").pop()}
              </span>
            ))}
            <span className="text-xs text-gray-600 ml-auto">
              confidence {confidencePct}%
            </span>
          </div>
        )}
      </div>
    </Link>
  );
}
