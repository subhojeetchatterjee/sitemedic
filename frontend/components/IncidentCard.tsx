"use client";

import Link from "next/link";
import type { Incident } from "@/lib/types";

const STATUS_STYLES: Record<string, string> = {
  DETECTING:        "bg-yellow-900 text-yellow-300",
  DIAGNOSING:       "bg-blue-900 text-blue-300",
  AWAITING_APPROVAL:"bg-orange-900 text-orange-300",
  REMEDIATING:      "bg-purple-900 text-purple-300",
  RESOLVED:         "bg-green-900 text-green-300",
  REJECTED:         "bg-gray-800 text-gray-400",
  PREDICTIVE:       "bg-amber-900 text-amber-300",
};

const SEVERITY_STYLES: Record<string, string> = {
  AVAILABILITY:     "text-red-400",
  PERFORMANCE:      "text-orange-400",
  ERROR:            "text-yellow-400",
  RESOURCE_CONTENTION: "text-blue-400",
  UNKNOWN:          "text-gray-400",
};

export default function IncidentCard({ incident }: { incident: Incident }) {
  const statusCls = STATUS_STYLES[incident.status] ?? "bg-gray-800 text-gray-400";
  const severityCls = SEVERITY_STYLES[incident.severity] ?? "text-gray-400";
  const isActive = !["RESOLVED", "REJECTED"].includes(incident.status);

  const isPredictive = incident.status === "PREDICTIVE";

  return (
    <Link href={`/incidents/${incident.problem_id}`}>
      <div className={`border rounded-lg p-4 hover:border-gray-600 transition-colors cursor-pointer bg-gray-900 ${
        isPredictive ? "border-amber-800" : "border-gray-800"
      }`}>
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              {isPredictive ? (
                <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              ) : isActive ? (
                <span className="inline-block w-2 h-2 rounded-full bg-red-500 animate-pulse" />
              ) : null}
              <h3 className="font-medium text-gray-100 truncate">{incident.title}</h3>
            </div>
            <p className="text-sm text-gray-400">
              <span className={severityCls}>{incident.severity}</span>
              {" · "}
              <span className="font-mono">{incident.service}</span>
            </p>
          </div>
          <div className="flex flex-col items-end gap-2 shrink-0">
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${statusCls}`}>
              {incident.status.replace("_", " ")}
            </span>
            <span className="text-xs text-gray-500">
              {new Date(incident.started_at).toLocaleTimeString()}
            </span>
          </div>
        </div>
        {(incident.demo_scenario || incident.plan || (incident.providers_used && incident.providers_used.length > 0)) && (
          <div className="mt-3 text-xs text-gray-500 border-t border-gray-800 pt-2 flex items-center gap-3 flex-wrap">
            {incident.demo_scenario && (
              <span className="px-1.5 py-0.5 rounded bg-amber-900 text-amber-300 font-mono">
                Demo: {incident.demo_scenario}
              </span>
            )}
            {incident.plan && (
              <>
                <span>Plan: <span className="font-mono text-gray-400">{incident.plan.action}</span></span>
                <span>confidence: <span className="text-gray-300">{Math.round(incident.plan.confidence * 100)}%</span></span>
              </>
            )}
            {incident.providers_used && incident.providers_used.length > 0 && (
              <span className="flex gap-1 ml-auto">
                {incident.providers_used.includes("dynatrace") && (
                  <span className="px-1.5 py-0.5 rounded bg-purple-900 text-purple-300">DT</span>
                )}
                {incident.providers_used.includes("gcp") && (
                  <span className="px-1.5 py-0.5 rounded bg-blue-900 text-blue-300">GCP</span>
                )}
              </span>
            )}
          </div>
        )}
      </div>
    </Link>
  );
}
