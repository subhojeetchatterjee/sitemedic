"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Incident, AuditEvent } from "@/lib/types";
import {
  synthesizeTimeline,
  applyFilter,
  fmtRelative,
  fmtAbsolute,
  type TLEvent,
  type FilterMode,
  EVENT_COLORS,
  LANE_LABELS,
  LANE_COLORS,
} from "@/lib/timeline";

// ── Constants ──────────────────────────────────────────────────────────────

const FILTER_OPTIONS: { label: string; mode: FilterMode }[] = [
  { label: "All",         mode: "all" },
  { label: "Major",       mode: "major" },
  { label: "Errors",      mode: "errors" },
  { label: "Detection",   mode: "detection" },
  { label: "Agent",       mode: "agent" },
  { label: "Operator",    mode: "operator" },
  { label: "System",      mode: "system" },
];

// Estimated collapsed row height; expanded rows are measured dynamically.
const ROW_ESTIMATE = 64;

// ── Sub-components ─────────────────────────────────────────────────────────

function LaneBadge({ lane }: { lane: TLEvent["lane"] }) {
  const color = LANE_COLORS[lane];
  return (
    <span
      className="text-xs px-1.5 py-0.5 rounded font-medium shrink-0"
      style={{ background: color + "22", color }}
    >
      {LANE_LABELS[lane]}
    </span>
  );
}

function EventDot({ type, size = 10 }: { type: TLEvent["type"]; size?: number }) {
  const color = EVENT_COLORS[type] ?? "#6b7280";
  return (
    <span
      className="rounded-full shrink-0 border-2 border-gray-950"
      style={{
        width: size,
        height: size,
        background: color,
        boxShadow: `0 0 6px ${color}55`,
      }}
    />
  );
}

function ProviderBadge({ provider }: { provider?: string }) {
  if (!provider) return null;
  const styles: Record<string, string> = {
    dynatrace: "bg-purple-900 text-purple-300",
    gcp:       "bg-blue-900 text-blue-300",
  };
  return (
    <span className={`text-xs px-1 py-0.5 rounded ${styles[provider] ?? "bg-gray-800 text-gray-400"}`}>
      {provider === "dynatrace" ? "DT" : "GCP"}
    </span>
  );
}

function DurationBadge({ ms }: { ms?: number }) {
  if (!ms) return null;
  const label = ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  return (
    <span className="text-xs text-gray-600 font-mono shrink-0">{label}</span>
  );
}

// ── Detail renderer ────────────────────────────────────────────────────────

function EventDetails({ event }: { event: TLEvent }) {
  const d = event.details;

  if (event.type === "agent_tool_call") {
    return (
      <div className="mt-3 space-y-3">
        {!!d.thought && (
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Thought</p>
            <p className="text-sm text-gray-300 leading-relaxed">{String(d.thought)}</p>
          </div>
        )}
        {!!d.tool_call && (
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">
              Tool call · {String((d.tool_call as { name: string }).name)}
            </p>
            <pre className="text-xs font-mono bg-gray-950 border border-gray-800 rounded p-3 overflow-auto text-cyan-300 whitespace-pre-wrap max-h-48">
              {JSON.stringify((d.tool_call as { args: unknown }).args, null, 2)}
            </pre>
          </div>
        )}
        {d.tool_result !== undefined && (
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Result</p>
            <pre className="text-xs font-mono bg-gray-950 border border-gray-800 rounded p-3 overflow-auto text-green-300 whitespace-pre-wrap max-h-48">
              {JSON.stringify(d.tool_result, null, 2)}
            </pre>
          </div>
        )}
      </div>
    );
  }

  if (event.type === "agent_reasoning") {
    return (
      <div className="mt-3">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Reasoning</p>
        <p className="text-sm text-gray-300 leading-relaxed">
          {String(d.thought || "No reasoning text.")}
        </p>
      </div>
    );
  }

  if (event.type === "plan_generated" || event.type === "plan_blocked") {
    const plan = d.plan as Record<string, unknown>;
    const diag = d.diagnosis as Record<string, unknown> | undefined;
    return (
      <div className="mt-3 space-y-3">
        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <p className="text-gray-500 mb-0.5">Action</p>
            <p className="font-mono text-indigo-300">{String(plan?.action ?? "—")}</p>
          </div>
          <div>
            <p className="text-gray-500 mb-0.5">Confidence</p>
            <p className="text-gray-200">{Math.round(Number(plan?.confidence ?? 0) * 100)}%</p>
          </div>
          <div>
            <p className="text-gray-500 mb-0.5">Safety</p>
            <p className="text-gray-200">{String(plan?.rollback_safety ?? "reversible")}</p>
          </div>
          <div>
            <p className="text-gray-500 mb-0.5">Cost delta</p>
            <p className="text-gray-200">
              {plan?.estimated_hourly_cost_delta_usd !== undefined
                ? `$${Number(plan.estimated_hourly_cost_delta_usd).toFixed(4)}/hr`
                : "—"}
            </p>
          </div>
        </div>
        {!!plan?.reason && (
          <div>
            <p className="text-xs text-gray-500 mb-1">Reason</p>
            <p className="text-sm text-gray-300">{String(plan.reason)}</p>
          </div>
        )}
        {!!plan?.estimated_impact && (
          <div>
            <p className="text-xs text-gray-500 mb-1">Estimated impact</p>
            <p className="text-sm text-gray-300">{String(plan.estimated_impact)}</p>
          </div>
        )}
        {!!diag && (
          <div>
            <p className="text-xs text-gray-500 mb-1">Root cause ({String(diag.confidence_band)} confidence)</p>
            <p className="text-sm text-gray-300">{String(diag.root_cause)}</p>
          </div>
        )}
      </div>
    );
  }

  if (event.type === "remediation_start" || event.type === "remediation_complete") {
    return (
      <div className="mt-3">
        <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Result</p>
        <pre className="text-xs font-mono bg-gray-950 border border-gray-800 rounded p-3 overflow-auto text-orange-300 whitespace-pre-wrap max-h-48">
          {JSON.stringify(d.result ?? d.tool_call, null, 2)}
        </pre>
      </div>
    );
  }

  // Generic fallback
  const keys = Object.keys(d).filter(k => d[k] !== null && d[k] !== undefined && d[k] !== "");
  if (keys.length === 0) return null;

  return (
    <div className="mt-3">
      <pre className="text-xs font-mono bg-gray-950 border border-gray-800 rounded p-3 overflow-auto text-gray-400 whitespace-pre-wrap max-h-48">
        {JSON.stringify(
          Object.fromEntries(keys.map(k => [k, d[k]])),
          null,
          2,
        )}
      </pre>
    </div>
  );
}

// ── Single timeline row ────────────────────────────────────────────────────

interface RowProps {
  event: TLEvent;
  isExpanded: boolean;
  onToggle: () => void;
  timeMode: "absolute" | "relative";
  origin: string;
  isLast: boolean;
  compact?: boolean;
}

function TimelineRow({
  event, isExpanded, onToggle, timeMode, origin, isLast, compact,
}: RowProps) {
  const timeLabel = timeMode === "relative"
    ? fmtRelative(event.ts, origin)
    : fmtAbsolute(event.ts);
  const dotColor = EVENT_COLORS[event.type] ?? "#6b7280";

  return (
    <div className={`flex gap-0 ${compact ? "" : ""}`}>
      {/* Time column */}
      <div className="w-16 sm:w-20 shrink-0 pt-3 pr-2 text-right">
        <span className="text-xs text-gray-500 font-mono">{timeLabel}</span>
      </div>

      {/* Spine */}
      <div className="flex flex-col items-center w-5 shrink-0">
        <div className="mt-3.5">
          <EventDot type={event.type} size={10} />
        </div>
        {!isLast && (
          <div
            className="w-0.5 flex-1 mt-1"
            style={{ background: `${dotColor}33`, minHeight: 16 }}
          />
        )}
      </div>

      {/* Content */}
      <div
        className={`flex-1 min-w-0 pb-3 pl-3 cursor-pointer group ${
          isExpanded ? "bg-gray-900/40 rounded-lg" : ""
        }`}
        onClick={onToggle}
        role="button"
        tabIndex={0}
        onKeyDown={e => e.key === "Enter" && onToggle()}
      >
        <div className="flex items-start gap-2 pt-2.5 flex-wrap">
          <div className="flex-1 min-w-0">
            <p className={`text-sm font-medium truncate ${
              event.isError ? "text-red-300" : "text-gray-200"
            } group-hover:text-white`}>
              {event.title}
            </p>
            {event.subtitle && (
              <p className="text-xs text-gray-500 mt-0.5 truncate">{event.subtitle}</p>
            )}
          </div>
          <div className="flex items-center gap-1.5 shrink-0 mt-0.5">
            <LaneBadge lane={event.lane} />
            <ProviderBadge provider={event.provider} />
            <DurationBadge ms={event.durationMs} />
            <span className="text-gray-700 text-xs group-hover:text-gray-400">
              {isExpanded ? "▲" : "▼"}
            </span>
          </div>
        </div>

        {/* Expanded detail */}
        {isExpanded && (
          <div className="mt-1 mb-2 pr-2" onClick={e => e.stopPropagation()}>
            <EventDetails event={event} />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Export helpers ─────────────────────────────────────────────────────────

function exportJson(events: TLEvent[], incidentId: string) {
  const json = JSON.stringify(events, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = `incident-${incidentId}-timeline.json`;
  a.click();
  URL.revokeObjectURL(url);
}

async function exportPng(containerRef: React.RefObject<HTMLDivElement>, incidentId: string) {
  try {
    const { toPng } = await import("html-to-image");
    const dataUrl = await toPng(containerRef.current!, {
      backgroundColor: "#030712",
      cacheBust: true,
    });
    const a = document.createElement("a");
    a.download = `incident-${incidentId}-timeline.png`;
    a.href = dataUrl;
    a.click();
  } catch (err) {
    console.error("PNG export failed", err);
  }
}

function copyShareLink(incidentId: string, ts?: string) {
  const url = `${window.location.origin}/incidents/${incidentId}${ts ? `?t=${encodeURIComponent(ts)}` : ""}`;
  navigator.clipboard.writeText(url).catch(() => {});
}

// ── Main component ─────────────────────────────────────────────────────────

interface Props {
  incident: Incident;
  auditEvents: AuditEvent[];
  compact?: boolean;  // for comparison side-by-side mode
}

export default function IncidentTimeline({ incident, auditEvents, compact }: Props) {
  const [filter, setFilter]     = useState<FilterMode>("all");
  const [timeMode, setTimeMode] = useState<"absolute" | "relative">("relative");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [copied, setCopied]     = useState(false);

  const parentRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const origin = incident.started_at;

  const allEvents = useMemo(
    () => synthesizeTimeline(incident, auditEvents),
    [incident, auditEvents],
  );

  const filtered = useMemo(
    () => applyFilter(allEvents, filter),
    [allEvents, filter],
  );

  // Virtual list — renders only visible rows, handles 500+ events gracefully
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: useCallback(
      (i: number) => (expandedId === filtered[i]?.id ? 280 : ROW_ESTIMATE),
      [expandedId, filtered],
    ),
    overscan: 8,
  });

  // Re-measure when an item is expanded/collapsed
  useEffect(() => {
    virtualizer.measure();
  }, [expandedId, virtualizer]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedId(prev => (prev === id ? null : id));
  }, []);

  const handleCopyLink = () => {
    copyShareLink(incident.problem_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (allEvents.length === 0) {
    return (
      <div className="flex items-center justify-center h-32 text-gray-500 text-sm">
        Waiting for events…
      </div>
    );
  }

  return (
    <div ref={containerRef} className="flex flex-col h-full">
      {/* ── Toolbar ── */}
      <div className="flex flex-wrap items-center gap-2 mb-3 px-1">
        {/* Filter chips */}
        <div className="flex gap-1 flex-wrap">
          {FILTER_OPTIONS.map(({ label, mode }) => (
            <button
              key={mode}
              onClick={() => setFilter(mode)}
              className={`text-xs px-2.5 py-1 rounded-full font-medium transition-colors ${
                filter === mode
                  ? "bg-indigo-700 text-white"
                  : "bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700"
              }`}
            >
              {label}
              {mode === "all" && (
                <span className="ml-1 text-gray-500 text-xs">{allEvents.length}</span>
              )}
              {mode !== "all" && filter === mode && (
                <span className="ml-1 text-indigo-300 text-xs">{filtered.length}</span>
              )}
            </button>
          ))}
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Time mode toggle */}
        <div className="flex bg-gray-900 border border-gray-800 rounded-lg p-0.5">
          {(["relative", "absolute"] as const).map(m => (
            <button
              key={m}
              onClick={() => setTimeMode(m)}
              className={`text-xs px-2.5 py-1 rounded-md transition-colors ${
                timeMode === m
                  ? "bg-gray-700 text-white font-medium"
                  : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {m === "relative" ? "T+" : "Clock"}
            </button>
          ))}
        </div>

        {/* Export menu */}
        {!compact && (
          <div className="flex gap-1">
            <button
              onClick={() => exportJson(filtered, incident.problem_id)}
              className="text-xs px-2.5 py-1 rounded bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700"
              title="Export timeline as JSON"
            >
              JSON
            </button>
            <button
              onClick={() => exportPng(containerRef, incident.problem_id)}
              className="text-xs px-2.5 py-1 rounded bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700"
              title="Export timeline as PNG"
            >
              PNG
            </button>
            <button
              onClick={handleCopyLink}
              className={`text-xs px-2.5 py-1 rounded transition-colors ${
                copied
                  ? "bg-green-900 text-green-300"
                  : "bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700"
              }`}
              title="Copy shareable link"
            >
              {copied ? "Copied!" : "Link"}
            </button>
          </div>
        )}
      </div>

      {/* ── Virtual scroll container ── */}
      <div
        ref={parentRef}
        className="overflow-y-auto flex-1 min-h-0"
        style={{ contain: "strict" }}
      >
        <div
          style={{ height: virtualizer.getTotalSize(), position: "relative" }}
        >
          {virtualizer.getVirtualItems().map(vItem => {
            const event = filtered[vItem.index];
            if (!event) return null;
            return (
              <div
                key={vItem.key}
                data-index={vItem.index}
                ref={virtualizer.measureElement}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${vItem.start}px)`,
                }}
              >
                <TimelineRow
                  event={event}
                  isExpanded={expandedId === event.id}
                  onToggle={() => toggleExpand(event.id)}
                  timeMode={timeMode}
                  origin={origin}
                  isLast={vItem.index === filtered.length - 1}
                  compact={compact}
                />
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Legend (mobile-hidden) ── */}
      {!compact && (
        <div className="hidden sm:flex flex-wrap gap-3 mt-3 pt-3 border-t border-gray-800 px-1">
          {(Object.entries(LANE_COLORS) as [TLEvent["lane"], string][]).map(([lane, color]) => (
            <span key={lane} className="flex items-center gap-1.5 text-xs text-gray-500">
              <span className="w-2 h-2 rounded-full" style={{ background: color }} />
              {LANE_LABELS[lane]}
            </span>
          ))}
          <span className="text-xs text-gray-600 ml-auto">
            {filtered.length} / {allEvents.length} events
          </span>
        </div>
      )}
    </div>
  );
}
