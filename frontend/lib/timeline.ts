/**
 * Timeline event synthesis.
 *
 * Merges an Incident + AuditEvents into a flat, chronologically sorted array
 * of TLEvent objects that the IncidentTimeline component renders.
 */

import type { Incident, AuditEvent, TraceStep } from "./types";

// ── Types ──────────────────────────────────────────────────────────────────

export type EventType =
  | "dt_problem_fired"
  | "sitemedic_detected"
  | "agent_reasoning"
  | "agent_tool_call"
  | "plan_generated"
  | "plan_blocked"
  | "operator_approved"
  | "operator_rejected"
  | "remediation_start"
  | "remediation_complete"
  | "resolved"
  | "error"
  | "audit"
  | "webhook_received"
  | "notification";

export type ActorLane = "detection" | "agent" | "operator" | "system";

export interface TLEvent {
  id: string;
  ts: string;             // ISO timestamp
  type: EventType;
  lane: ActorLane;
  title: string;
  subtitle: string;
  isMajor: boolean;       // shown in "major events" filter
  isError: boolean;       // shown in "errors only" filter
  isAgentInternal: boolean; // hidden in "hide internals" filter
  details: Record<string, unknown>;
  durationMs?: number;    // tool call response time (estimated from step delta)
  provider?: string;      // "dynatrace" | "gcp"
  stepIndex?: number;     // links back to incident.trace[]
}

// ── Lane and color maps (used by the component) ────────────────────────────

export const LANE_COLORS: Record<ActorLane, string> = {
  detection: "#10b981",  // green
  agent:     "#6366f1",  // indigo
  operator:  "#f97316",  // orange
  system:    "#6b7280",  // gray
};

export const EVENT_COLORS: Record<EventType, string> = {
  dt_problem_fired:    "#ef4444",  // red
  sitemedic_detected:  "#10b981",  // green
  agent_reasoning:     "#6366f1",  // indigo
  agent_tool_call:     "#06b6d4",  // cyan
  plan_generated:      "#8b5cf6",  // violet
  plan_blocked:        "#ef4444",  // red
  operator_approved:   "#10b981",  // green
  operator_rejected:   "#ef4444",  // red
  remediation_start:   "#f97316",  // orange
  remediation_complete:"#10b981",  // green
  resolved:            "#10b981",  // green
  error:               "#ef4444",  // red
  audit:               "#6b7280",  // gray
  webhook_received:    "#10b981",  // green
  notification:        "#6b7280",  // gray
};

export const LANE_LABELS: Record<ActorLane, string> = {
  detection: "Detection",
  agent:     "Agent",
  operator:  "Operator",
  system:    "System",
};

// ── Synthesis ──────────────────────────────────────────────────────────────

let _id = 0;
function uid(prefix: string) {
  return `${prefix}-${++_id}`;
}

function isoOrNow(ts: string | undefined): string {
  return ts && ts !== "null" ? ts : new Date().toISOString();
}

function traceStepTitle(step: TraceStep): string {
  if (step.tool_call) {
    return `Tool: ${step.tool_call.name}`;
  }
  const thought = step.thought || "";
  return thought.length > 80 ? thought.slice(0, 80) + "…" : thought || "Agent reasoning";
}

function traceStepSubtitle(step: TraceStep): string {
  if (step.tool_call) {
    const args = step.tool_call.args;
    const first = Object.entries(args)[0];
    return first ? `${first[0]}: ${String(first[1]).slice(0, 60)}` : "";
  }
  return "";
}

function estimateDurationMs(steps: TraceStep[], idx: number): number | undefined {
  const cur = steps[idx];
  const next = steps[idx + 1];
  if (!cur?.timestamp || !next?.timestamp) return undefined;
  const curTs = new Date(cur.timestamp).getTime();
  const nextTs = new Date(next.timestamp).getTime();
  const delta = nextTs - curTs;
  return delta > 0 && delta < 300_000 ? delta : undefined;
}

export function synthesizeTimeline(
  incident: Incident,
  auditEvents: AuditEvent[],
): TLEvent[] {
  const events: TLEvent[] = [];

  // 1. Dynatrace problem fired — use incident.started_at
  events.push({
    id: uid("dt"),
    ts: isoOrNow(incident.started_at),
    type: "dt_problem_fired",
    lane: "detection",
    title: "Dynatrace problem opened",
    subtitle: `${incident.severity} · ${incident.service}`,
    isMajor: true,
    isError: false,
    isAgentInternal: false,
    details: {
      problem_id: incident.problem_id,
      severity: incident.severity,
      service: incident.service,
      title: incident.title,
    },
  });

  // 2. Webhook / polling detection
  if (incident.webhook_received_at) {
    events.push({
      id: uid("wh"),
      ts: isoOrNow(incident.webhook_received_at),
      type: "webhook_received",
      lane: "detection",
      title: "SiteMedic detected (webhook)",
      subtitle: incident.time_to_detect_ms
        ? `TTD: ${incident.time_to_detect_ms < 1000
            ? `${incident.time_to_detect_ms}ms`
            : `${(incident.time_to_detect_ms / 1000).toFixed(1)}s`}`
        : "webhook delivery",
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: {
        detection_method: incident.detection_method,
        time_to_detect_ms: incident.time_to_detect_ms,
      },
    });
  } else if (incident.detection_method === "polling") {
    events.push({
      id: uid("poll"),
      ts: isoOrNow(incident.started_at),
      type: "sitemedic_detected",
      lane: "detection",
      title: "SiteMedic detected (polling)",
      subtitle: incident.time_to_detect_ms
        ? `TTD: ${(incident.time_to_detect_ms / 1000 / 60).toFixed(1)} min`
        : "5-min polling fallback",
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: { detection_method: "polling", time_to_detect_ms: incident.time_to_detect_ms },
    });
  }

  // 3. Each trace step
  const trace = incident.trace ?? [];
  for (let i = 0; i < trace.length; i++) {
    const step = trace[i];
    const isToolCall = !!step.tool_call;
    const hasError =
      typeof step.tool_result === "object" &&
      step.tool_result !== null &&
      "error" in (step.tool_result as Record<string, unknown>);

    events.push({
      id: uid(`step-${step.step}`),
      ts: isoOrNow(step.timestamp),
      type: isToolCall ? "agent_tool_call" : "agent_reasoning",
      lane: "agent",
      title: traceStepTitle(step),
      subtitle: traceStepSubtitle(step),
      isMajor: false,
      isError: hasError,
      isAgentInternal: true,
      details: {
        step: step.step,
        thought: step.thought,
        tool_call: step.tool_call,
        tool_result: step.tool_result,
        provider: step.provider,
      },
      durationMs: isToolCall ? estimateDurationMs(trace, i) : undefined,
      provider: step.provider ?? undefined,
      stepIndex: step.step,
    });
  }

  // 4. Plan generated — derive timestamp from last trace step or updated_at
  if (incident.plan) {
    const lastTraceTs = trace.length > 0
      ? trace[trace.length - 1].timestamp
      : incident.updated_at;
    const isBlocked = incident.confidence_blocked;

    events.push({
      id: uid("plan"),
      ts: isoOrNow(lastTraceTs),
      type: isBlocked ? "plan_blocked" : "plan_generated",
      lane: "agent",
      title: isBlocked
        ? `Plan generated — BLOCKED (confidence < 60%)`
        : `Plan generated: ${incident.plan.action?.replace(/_/g, " ")}`,
      subtitle: `${Math.round((incident.plan.confidence ?? 0) * 100)}% confidence · ${incident.plan.rollback_safety ?? "reversible"}`,
      isMajor: true,
      isError: !!isBlocked,
      isAgentInternal: false,
      details: { plan: incident.plan, diagnosis: incident.diagnosis },
    });
  }

  // 5. Operator approval / rejection — from audit events when available
  const approveAudit = auditEvents.find(a => a.action_type === "approved");
  const rejectAudit  = auditEvents.find(a => a.action_type === "rejected");

  if (approveAudit) {
    events.push({
      id: uid("approve"),
      ts: isoOrNow(approveAudit.timestamp),
      type: "operator_approved",
      lane: "operator",
      title: "Operator approved",
      subtitle: String(approveAudit.payload?.action ?? incident.plan?.action ?? ""),
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: approveAudit.payload ?? {},
    });
  } else if (
    ["REMEDIATING", "RESOLVED"].includes(incident.status) &&
    incident.plan
  ) {
    // No audit event — infer approval from status
    events.push({
      id: uid("approve-inferred"),
      ts: isoOrNow(incident.updated_at),
      type: "operator_approved",
      lane: "operator",
      title: "Operator approved (inferred)",
      subtitle: incident.plan.action?.replace(/_/g, " ") ?? "",
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: { action: incident.plan.action },
    });
  }

  if (rejectAudit) {
    events.push({
      id: uid("reject"),
      ts: isoOrNow(rejectAudit.timestamp),
      type: "operator_rejected",
      lane: "operator",
      title: "Operator rejected",
      subtitle: String(rejectAudit.payload?.reason ?? ""),
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: rejectAudit.payload ?? {},
    });
  } else if (incident.status === "REJECTED") {
    events.push({
      id: uid("reject-inferred"),
      ts: isoOrNow(incident.updated_at),
      type: "operator_rejected",
      lane: "operator",
      title: "Operator rejected (inferred)",
      subtitle: "",
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: {},
    });
  }

  // 6. Remediation steps — from trace step 999 (execution step)
  const execStep = trace.find(s => s.step === 999);
  if (execStep) {
    events.push({
      id: uid("remediation-start"),
      ts: isoOrNow(execStep.timestamp),
      type: "remediation_start",
      lane: "system",
      title: `Executing: ${incident.plan?.action?.replace(/_/g, " ") ?? "remediation"}`,
      subtitle: "Cloud Run action dispatched",
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: { tool_call: execStep.tool_call, thought: execStep.thought },
    });
    if (execStep.tool_result) {
      const hasError = typeof execStep.tool_result === "object" && execStep.tool_result !== null && "error" in (execStep.tool_result as Record<string, unknown>);
      events.push({
        id: uid("remediation-complete"),
        ts: isoOrNow(execStep.timestamp),
        type: hasError ? "error" : "remediation_complete",
        lane: "system",
        title: hasError ? "Remediation failed" : "Remediation completed",
        subtitle: "",
        isMajor: true,
        isError: hasError,
        isAgentInternal: false,
        details: { result: execStep.tool_result },
      });
    }
  }

  // 7. Resolved
  if (incident.status === "RESOLVED") {
    events.push({
      id: uid("resolved"),
      ts: isoOrNow(incident.updated_at),
      type: "resolved",
      lane: "system",
      title: "Incident resolved",
      subtitle: incident.postmortem ? "Postmortem available" : "",
      isMajor: true,
      isError: false,
      isAgentInternal: false,
      details: { status: "RESOLVED" },
    });
  }

  // 8. Other notable audit events (chain verified, prediction link, etc.)
  const notableAuditTypes = new Set([
    "cluster_formed",
    "prediction_stored",
    "postmortem_generated",
    "webhook_received",
    "dry_run_requested",
  ]);
  for (const ae of auditEvents) {
    if (!notableAuditTypes.has(ae.action_type)) continue;
    events.push({
      id: uid(`audit-${ae.event_id}`),
      ts: isoOrNow(ae.timestamp),
      type: "audit",
      lane: "system",
      title: ae.action_type.replace(/_/g, " "),
      subtitle: ae.result,
      isMajor: ae.action_type === "postmortem_generated",
      isError: ae.result === "failure",
      isAgentInternal: false,
      details: { ...ae.payload, actor: ae.actor },
    });
  }

  // Sort chronologically
  events.sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime());

  return events;
}

// ── Time formatting ────────────────────────────────────────────────────────

export function fmtRelative(ts: string, origin: string): string {
  const diffMs = new Date(ts).getTime() - new Date(origin).getTime();
  if (diffMs < 0) return "T−0s";
  const secs = Math.floor(diffMs / 1000);
  if (secs < 60) return `T+${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem  = secs % 60;
  if (mins < 60) return `T+${mins}m${rem > 0 ? `${rem}s` : ""}`;
  const hrs  = Math.floor(mins / 60);
  const mrem = mins % 60;
  return `T+${hrs}h${mrem > 0 ? `${mrem}m` : ""}`;
}

export function fmtAbsolute(ts: string): string {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ── Filter logic ──────────────────────────────────────────────────────────

export type FilterMode = "all" | "major" | "errors" | "detection" | "agent" | "operator" | "system";

export function applyFilter(events: TLEvent[], mode: FilterMode): TLEvent[] {
  switch (mode) {
    case "all":        return events;
    case "major":      return events.filter(e => e.isMajor);
    case "errors":     return events.filter(e => e.isError);
    case "detection":  return events.filter(e => e.lane === "detection");
    case "agent":      return events.filter(e => e.lane === "agent");
    case "operator":   return events.filter(e => e.lane === "operator");
    case "system":     return events.filter(e => e.lane === "system");
    default:           return events;
  }
}

// ── Duration helpers ──────────────────────────────────────────────────────

export function incidentDuration(incident: Incident): string {
  const startMs = incident.started_at ? new Date(incident.started_at).getTime() : NaN;
  if (isNaN(startMs)) return "—";
  const end   = incident.status === "RESOLVED" || incident.status === "REJECTED"
    ? new Date(incident.updated_at).getTime()
    : Date.now();
  const secs = Math.floor((end - startMs) / 1000);
  if (secs < 0 || isNaN(secs)) return "—";
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}
