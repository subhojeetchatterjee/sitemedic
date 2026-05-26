"use client";

import { useEffect, useState } from "react";
import { doc, onSnapshot } from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { Incident, AuditEvent } from "@/lib/types";
import IncidentTimeline from "@/components/IncidentTimeline";
import ImpactSidePanel from "@/components/ImpactSidePanel";
import PlanReview from "@/components/PlanReview";
import ReasoningTrace from "@/components/ReasoningTrace";
import Link from "next/link";
import { incidentDuration } from "@/lib/timeline";

// ── Styles ─────────────────────────────────────────────────────────────────

const STATUS_STYLES: Record<string, string> = {
  DETECTING:         "bg-yellow-900 text-yellow-300",
  DIAGNOSING:        "bg-blue-900 text-blue-300",
  AWAITING_APPROVAL: "bg-orange-900 text-orange-300",
  REMEDIATING:       "bg-purple-900 text-purple-300",
  RESOLVED:          "bg-green-900 text-green-300",
  REJECTED:          "bg-gray-800 text-gray-400",
};

const SEVERITY_COLORS: Record<string, string> = {
  AVAILABILITY: "text-red-400",
  PERFORMANCE:  "text-orange-400",
  ERROR:        "text-yellow-400",
  RESOURCE_CONTENTION: "text-blue-400",
};

// ── Incident summary header ────────────────────────────────────────────────

function IncidentHeader({ incident }: { incident: Incident }) {
  const statusCls = STATUS_STYLES[incident.status] ?? "bg-gray-800 text-gray-400";
  const severityCls = SEVERITY_COLORS[incident.severity] ?? "text-gray-400";
  const duration = incidentDuration(incident);
  const isActive = !["RESOLVED", "REJECTED"].includes(incident.status);

  return (
    <div className="flex flex-col sm:flex-row sm:items-center gap-3 mb-6">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <Link href="/" className="text-xs text-gray-600 hover:text-gray-400">
            ← All incidents
          </Link>
          {incident.detection_method && (
            <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
              incident.detection_method === "webhook"
                ? "bg-green-900 text-green-300"
                : "bg-yellow-900 text-yellow-300"
            }`}>
              {incident.detection_method === "webhook" ? "⚡ Webhook" : "⏱ Polling"}
            </span>
          )}
        </div>
        <h1 className="text-xl font-bold text-gray-100 truncate">{incident.title}</h1>
        <div className="flex flex-wrap items-center gap-3 mt-1 text-sm">
          <span className={`font-medium ${severityCls}`}>{incident.severity}</span>
          <span className="text-gray-500">·</span>
          <span className="font-mono text-gray-300">{incident.service}</span>
          <span className="text-gray-500">·</span>
          <span className="text-gray-400">
            {isActive && <span className="inline-block w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse mr-1.5 align-middle" />}
            {duration}
          </span>
          {incident.time_to_detect_ms != null && (
            <>
              <span className="text-gray-500">·</span>
              <span className="text-xs text-gray-500">
                TTD: {incident.time_to_detect_ms < 1000
                  ? `${incident.time_to_detect_ms}ms`
                  : `${(incident.time_to_detect_ms / 1000).toFixed(1)}s`}
              </span>
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0">
        <span className={`text-xs px-2.5 py-1.5 rounded-full font-medium ${statusCls}`}>
          {incident.status.replace("_", " ")}
        </span>
        <Link
          href={`/compare?a=${incident.problem_id}`}
          className="text-xs px-2.5 py-1.5 rounded border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500"
        >
          Compare ⊞
        </Link>
        {incident.status === "RESOLVED" && incident.postmortem && (
          <Link
            href={`/incidents/${incident.problem_id}/postmortem`}
            className="text-xs px-2.5 py-1.5 rounded bg-green-900 text-green-300 hover:bg-green-800"
          >
            Postmortem →
          </Link>
        )}
      </div>
    </div>
  );
}

// ── Confidence blocked banner ──────────────────────────────────────────────

function ConfidenceBanner({ incident }: { incident: Incident }) {
  if (!incident.confidence_blocked) return null;
  return (
    <div className="mb-4 px-3 py-2.5 rounded-lg bg-red-950 border border-red-800 text-sm text-red-300">
      <span className="font-semibold">Auto-action blocked</span> — agent confidence below 60%.
      Human review and explicit approval required before any remediation runs.
    </div>
  );
}

// ── Plan generation failure banner ───────────────────────────────────────

function PlanFailureBanner({ incident }: { incident: Incident }) {
  if (incident.status !== "AWAITING_APPROVAL" || incident.plan) return null;
  return (
    <div className="mb-4 px-3 py-2.5 rounded-lg bg-yellow-950 border border-yellow-800 text-sm text-yellow-300">
      <span className="font-semibold">Plan generation failed</span> — the agent could not produce a structured
      remediation plan. Review the trace for details, then{" "}
      <span className="font-mono text-xs">Reject</span> this incident and trigger a new diagnosis if needed.
    </div>
  );
}

// ── Competing diagnosis banner ────────────────────────────────────────────

function CompetingDiagnosisBanner({ incident }: { incident: Incident }) {
  if (!incident.competing_diagnosis) return null;
  const cd = incident.competing_diagnosis;
  return (
    <div className="mb-4 border border-amber-800 rounded-lg p-3 bg-amber-950">
      <p className="text-xs font-semibold text-amber-300 mb-1">Competing diagnosis detected</p>
      <p className="text-xs text-amber-400 mb-2">{cd.note}</p>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="bg-gray-900 rounded p-2">
          <p className="text-gray-400 mb-0.5 font-medium">Primary</p>
          <p className="font-mono text-gray-200">{incident.plan?.action}</p>
          <p className="text-gray-500 mt-0.5">{Math.round((incident.plan?.confidence ?? 0) * 100)}%</p>
        </div>
        <div className="bg-gray-900 rounded p-2">
          <p className="text-gray-400 mb-0.5 font-medium">Alternative</p>
          <p className="font-mono text-gray-200">{cd.plan.action}</p>
          <p className="text-gray-500 mt-0.5">{Math.round((cd.plan.confidence ?? 0) * 100)}%</p>
        </div>
      </div>
    </div>
  );
}

// ── Inline approval widget (appears in the timeline column) ───────────────

function ApprovalWidget({ incident }: { incident: Incident }) {
  if (incident.status !== "AWAITING_APPROVAL" || !incident.plan) return null;
  return (
    <div className="mt-4 border border-orange-900 rounded-xl bg-orange-950/30 p-4">
      <p className="text-xs font-semibold text-orange-300 uppercase tracking-wide mb-3">
        Awaiting your decision
      </p>
      <PlanReview
        problemId={incident.problem_id}
        plan={incident.plan}
        onDecision={() => {}}
      />
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────

export default function IncidentPage({ params }: { params: { id: string } }) {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);

  // Real-time Firestore listener for incident
  useEffect(() => {
    const unsub = onSnapshot(doc(db, "incidents", params.id), snap => {
      if (snap.exists()) setIncident(snap.data() as Incident);
    });
    return unsub;
  }, [params.id]);

  // Fetch audit events (refresh every 30s for live incidents)
  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;
    function load() {
      fetch(`/api/incidents/${params.id}/audit`)
        .then(r => r.json())
        .then(setAuditEvents)
        .catch(() => {});
    }
    load();
    timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, [params.id]);

  if (!incident) {
    return (
      <div className="space-y-4 animate-pulse">
        <div className="h-6 w-1/2 bg-gray-800 rounded" />
        <div className="h-4 w-1/3 bg-gray-800 rounded" />
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 mt-8">
          <div className="lg:col-span-3 h-96 bg-gray-800 rounded-xl" />
          <div className="h-96 bg-gray-800 rounded-xl" />
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* ── Summary header ── */}
      <IncidentHeader incident={incident} />

      {/* ── Alerts ── */}
      <ConfidenceBanner incident={incident} />
      <PlanFailureBanner incident={incident} />
      <CompetingDiagnosisBanner incident={incident} />

      {/* ── Main: timeline (3/4) + impact rail (1/4) ── */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4" style={{ height: "calc(100vh - 18rem)", minHeight: 500 }}>

        {/* Timeline column */}
        <div className="lg:col-span-3 flex flex-col border border-gray-800 rounded-xl bg-gray-950 overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">
              Timeline
            </h2>
            <span className="text-xs text-gray-600">
              {incident.trace.length} agent steps · {incident.problem_id}
            </span>
            {incident.cluster_id && (
              <Link
                href={`/clusters/${incident.cluster_id}`}
                className="ml-auto text-xs font-mono text-orange-400 hover:text-orange-300"
              >
                Cluster {incident.cluster_id} →
              </Link>
            )}
          </div>

          {/* Virtualized timeline — fills remaining height */}
          <div className="flex-1 min-h-0 p-3">
            <IncidentTimeline
              incident={incident}
              auditEvents={auditEvents}
            />
          </div>

          {/* Approval widget docked at bottom when needed */}
          {incident.status === "AWAITING_APPROVAL" && incident.plan && (
            <div className="border-t border-gray-800 p-4 bg-gray-900 overflow-y-auto max-h-[60vh]">
              <ApprovalWidget incident={incident} />
            </div>
          )}
        </div>

        {/* Impact side panel */}
        <div className="lg:col-span-1 overflow-y-auto">
          <ImpactSidePanel incident={incident} />
        </div>
      </div>

      {/* ── Mobile fallback: reasoning trace accordion ── */}
      <div className="mt-4 block lg:hidden border border-gray-800 rounded-xl bg-gray-900 p-4">
        <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
          Agent Reasoning (mobile view)
        </h2>
        {incident.confidence_blocked && (
          <div className="mb-3 px-3 py-2 rounded bg-red-950 border border-red-800 text-xs text-red-300">
            Confidence below 60% — auto-action blocked.
          </div>
        )}
        {incident.trace.length === 0 ? (
          <p className="text-sm text-gray-500 animate-pulse">Waiting for agent…</p>
        ) : (
          <ReasoningTrace
            steps={incident.trace}
            isActive={!["RESOLVED", "REJECTED"].includes(incident.status)}
            diagnosis={incident.diagnosis}
          />
        )}
        {incident.status === "AWAITING_APPROVAL" && incident.plan && (
          <div className="mt-4 pt-4 border-t border-gray-800">
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
              Remediation Plan
            </h2>
            <PlanReview
              problemId={incident.problem_id}
              plan={incident.plan}
              onDecision={() => {}}
            />
          </div>
        )}
      </div>
    </div>
  );
}
