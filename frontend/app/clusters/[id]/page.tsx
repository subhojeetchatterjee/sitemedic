"use client";

import { useEffect, useState } from "react";
import { doc, onSnapshot } from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { IncidentCluster, ClusterStep, Incident } from "@/lib/types";
import Link from "next/link";

const STEP_STATUS_STYLES: Record<string, { dot: string; label: string; border: string }> = {
  pending: { dot: "bg-gray-600",               label: "text-gray-500",   border: "border-gray-800" },
  running: { dot: "bg-purple-400 animate-pulse", label: "text-purple-300", border: "border-purple-800" },
  done:    { dot: "bg-green-500",               label: "text-green-300",  border: "border-green-800" },
  failed:  { dot: "bg-red-500",                 label: "text-red-300",    border: "border-red-800"   },
};

const CLUSTER_STATUS_STYLES: Record<string, string> = {
  FORMING:           "bg-yellow-900 text-yellow-300",
  AWAITING_APPROVAL: "bg-orange-900 text-orange-300",
  EXECUTING:         "bg-purple-900 text-purple-300",
  COMPLETE:          "bg-green-900 text-green-300",
  FAILED:            "bg-red-900 text-red-300",
  PARTIAL:           "bg-amber-900 text-amber-300",
};

function StepRow({ step, index, total }: { step: ClusterStep; index: number; total: number }) {
  const style = STEP_STATUS_STYLES[step.status] ?? STEP_STATUS_STYLES.pending;
  return (
    <div className="flex gap-3">
      {/* Timeline spine */}
      <div className="flex flex-col items-center">
        <div className={`w-3 h-3 rounded-full shrink-0 mt-1 ${style.dot}`} />
        {index < total - 1 && (
          <div className="w-px flex-1 bg-gray-800 mt-1" />
        )}
      </div>
      {/* Step card */}
      <div className={`flex-1 border rounded-lg p-3 mb-3 ${style.border} bg-gray-900`}>
        <div className="flex items-center justify-between gap-2 mb-1">
          <span className="text-xs text-gray-500 font-mono">Step {step.step_index + 1}</span>
          <span className={`text-xs font-medium ${style.label}`}>
            {step.status.toUpperCase()}
          </span>
        </div>
        <p className="text-sm font-medium text-gray-100 font-mono">{step.action}</p>
        <p className="text-xs text-gray-400 mt-0.5">
          Service: <span className="font-mono text-gray-300">{step.service}</span>
        </p>
        <p className="text-xs text-gray-500 mt-1 leading-relaxed">{step.reason}</p>
        {step.incident_id && (
          <Link
            href={`/incidents/${step.incident_id}`}
            className="text-xs text-blue-400 hover:text-blue-300 mt-1 inline-block"
            onClick={(e) => e.stopPropagation()}
          >
            → {step.incident_id}
          </Link>
        )}
        {step.result != null && step.status === "failed" && (
          <p className="text-xs text-red-400 mt-2 font-mono">
            {String(
              typeof step.result === "object" && step.result !== null
                ? (step.result as Record<string, string>).error ?? JSON.stringify(step.result)
                : step.result
            )}
          </p>
        )}
      </div>
    </div>
  );
}

function MemberCard({ incident }: { incident: Incident }) {
  return (
    <Link href={`/incidents/${incident.problem_id}`}>
      <div className="border border-gray-800 rounded-lg p-3 hover:border-gray-600 transition-colors bg-gray-900 cursor-pointer">
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm text-gray-100 truncate font-medium">{incident.title}</p>
          <span className="text-xs font-mono text-gray-500 shrink-0">{incident.status}</span>
        </div>
        <p className="text-xs text-gray-500 mt-0.5 font-mono">{incident.service}</p>
      </div>
    </Link>
  );
}


export default function ClusterPage({ params }: { params: { id: string } }) {
  const [cluster, setCluster] = useState<IncidentCluster | null>(null);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);

  useEffect(() => {
    const unsub = onSnapshot(doc(db, "incident_clusters", params.id), (snap) => {
      if (snap.exists()) setCluster(snap.data() as IncidentCluster);
    });
    return unsub;
  }, [params.id]);

  if (!cluster) {
    return <div className="text-gray-500 text-sm">Loading cluster…</div>;
  }

  const canApprove = cluster.status === "AWAITING_APPROVAL";
  const isTerminal = ["COMPLETE", "FAILED"].includes(cluster.status);
  const statusCls = CLUSTER_STATUS_STYLES[cluster.status] ?? "bg-gray-800 text-gray-400";
  const confidencePct = Math.round(cluster.confidence * 100);

  async function sendApproval(mode: "all_at_once" | "step_by_step", rejected = false) {
    setApproving(true);
    setApproveError(null);
    try {
      const res = await fetch(`/api/clusters/${cluster!.cluster_id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, rejected }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        setApproveError(err.detail ?? "Approval failed");
      }
    } catch (e) {
      setApproveError(String(e));
    } finally {
      setApproving(false);
    }
  }

  const steps = [...(cluster.coordinated_plan ?? [])].sort(
    (a, b) => a.step_index - b.step_index
  );

  return (
    <div>
      {/* Back + header */}
      <div className="mb-6">
        <Link href="/" className="text-sm text-gray-500 hover:text-gray-300 mb-3 inline-block">
          ← All incidents
        </Link>
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs text-orange-500 uppercase tracking-wide font-semibold mb-1">
              Incident Cluster
            </p>
            <h1 className="text-xl font-bold text-gray-100">
              {cluster.root_cause_summary || "Correlated incidents"}
            </h1>
          </div>
          <span className={`text-xs px-2 py-1 rounded-full font-medium shrink-0 ${statusCls}`}>
            {cluster.status.replace("_", " ")}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: metadata + member incidents */}
        <div className="space-y-4">
          {/* Cluster metadata */}
          <div className="border border-gray-800 rounded-lg p-4 bg-gray-900">
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
              Cluster Details
            </h2>
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="text-gray-500">Cluster ID</dt>
                <dd className="font-mono text-gray-400 text-xs break-all">{cluster.cluster_id}</dd>
              </div>
              <div>
                <dt className="text-gray-500">Correlation confidence</dt>
                <dd className="text-gray-200">{confidencePct}%</dd>
              </div>
              <div>
                <dt className="text-gray-500">Member incidents</dt>
                <dd className="text-gray-200">{cluster.member_incident_ids.length}</dd>
              </div>
              <div>
                <dt className="text-gray-500">Remediation steps</dt>
                <dd className="text-gray-200">{steps.length}</dd>
              </div>
              <div>
                <dt className="text-gray-500">Created</dt>
                <dd className="text-gray-200">{new Date(cluster.created_at).toLocaleString()}</dd>
              </div>
            </dl>
          </div>

          {/* Member incidents */}
          <div className="border border-gray-800 rounded-lg p-4 bg-gray-900">
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
              Member Incidents ({cluster.member_incident_ids.length})
            </h2>
            <div className="space-y-2">
              {cluster.members && cluster.members.length > 0 ? (
                cluster.members.map((inc) => (
                  <MemberCard key={inc.problem_id} incident={inc} />
                ))
              ) : (
                cluster.member_incident_ids.map((id) => (
                  <Link key={id} href={`/incidents/${id}`}>
                    <div className="border border-gray-800 rounded p-2 hover:border-gray-600 transition-colors bg-gray-900 cursor-pointer">
                      <p className="text-xs font-mono text-gray-400">{id}</p>
                    </div>
                  </Link>
                ))
              )}
            </div>
          </div>
        </div>

        {/* Center: execution timeline */}
        <div className="lg:col-span-1 border border-gray-800 rounded-lg p-4 bg-gray-900">
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-4">
            Coordinated Remediation Plan
          </h2>
          {steps.length === 0 ? (
            <p className="text-sm text-gray-500">No steps generated yet.</p>
          ) : (
            <div>
              {steps.map((step, i) => (
                <StepRow key={step.step_index} step={step} index={i} total={steps.length} />
              ))}
            </div>
          )}
        </div>

        {/* Right: approval panel */}
        <div className="lg:col-span-1">
          {canApprove && (
            <div className="border border-orange-800 rounded-lg p-4 bg-gray-900">
              <h2 className="text-xs font-semibold text-orange-500 uppercase tracking-wide mb-3">
                Approve Coordinated Plan
              </h2>
              <p className="text-sm text-gray-400 mb-4 leading-relaxed">
                The agent will execute all {steps.length} step{steps.length !== 1 ? "s" : ""} in
                sequence, halting on any failure.
              </p>

              <div className="space-y-2">
                <button
                  disabled={approving}
                  onClick={() => sendApproval("all_at_once")}
                  className="w-full px-4 py-2 rounded-lg text-sm font-medium bg-orange-700 hover:bg-orange-600 text-white disabled:opacity-50 transition-colors"
                >
                  {approving ? "Executing…" : "Approve All at Once"}
                </button>
                <button
                  disabled={approving}
                  onClick={() => sendApproval("step_by_step")}
                  className="w-full px-4 py-2 rounded-lg text-sm font-medium bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-50 transition-colors"
                >
                  Step-by-Step
                </button>
                <button
                  disabled={approving}
                  onClick={() => sendApproval("all_at_once", true)}
                  className="w-full px-4 py-2 rounded-lg text-sm font-medium bg-transparent hover:bg-gray-800 text-gray-500 hover:text-gray-300 border border-gray-800 disabled:opacity-50 transition-colors"
                >
                  Reject
                </button>
              </div>

              {approveError && (
                <p className="text-xs text-red-400 mt-3">{approveError}</p>
              )}
            </div>
          )}

          {cluster.status === "EXECUTING" && (
            <div className="border border-purple-800 rounded-lg p-4 bg-gray-900 text-center">
              <span className="inline-block w-2 h-2 rounded-full bg-purple-400 animate-pulse mr-2" />
              <span className="text-sm text-purple-300">Executing cluster plan…</span>
            </div>
          )}

          {cluster.status === "COMPLETE" && (
            <div className="border border-green-800 rounded-lg p-4 bg-green-950 text-center">
              <p className="text-sm text-green-300 font-medium">All steps completed.</p>
              <p className="text-xs text-gray-500 mt-1">
                {steps.filter((s) => s.status === "done").length}/{steps.length} steps executed
              </p>
            </div>
          )}

          {cluster.status === "FAILED" && (
            <div className="border border-red-800 rounded-lg p-4 bg-red-950">
              <p className="text-sm text-red-300 font-medium">Execution failed</p>
              <p className="text-xs text-gray-500 mt-1">
                {steps.filter((s) => s.status === "done").length} of {steps.length} steps completed
                before failure.
              </p>
            </div>
          )}

          {isTerminal && (
            <div className="mt-4 space-y-2">
              {cluster.member_incident_ids.map((id) => (
                <Link key={id} href={`/incidents/${id}`}>
                  <div className="border border-gray-800 rounded-lg p-3 hover:border-gray-600 transition-colors text-xs font-mono text-gray-400 bg-gray-900 cursor-pointer">
                    View incident {id} →
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
