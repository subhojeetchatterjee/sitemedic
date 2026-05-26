"use client";

import { useState } from "react";
import type { DryRunReport, DryRunStep } from "@/lib/types";

// ── Reversibility badge ────────────────────────────────────────────────────

const REVERSIBILITY_STYLES = {
  instant: { cls: "bg-green-900 text-green-300", label: "Instant rollback" },
  minutes: { cls: "bg-amber-900 text-amber-300", label: "Rollback in minutes" },
  manual:  { cls: "bg-red-900 text-red-300",    label: "Manual recovery only" },
};

function ReversibilityBadge({ value }: { value: DryRunStep["reversibility"] }) {
  const s = REVERSIBILITY_STYLES[value] ?? REVERSIBILITY_STYLES.instant;
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.cls}`}>
      {s.label}
    </span>
  );
}

// ── State diff card ────────────────────────────────────────────────────────

function StatePanel({
  title,
  state,
  accent,
}: {
  title: string;
  state: Record<string, unknown>;
  accent: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const json = JSON.stringify(state, null, 2);
  const lines = json.split("\n");
  const preview = lines.slice(0, 8).join("\n");
  const hasMore = lines.length > 8;

  return (
    <div className={`flex-1 border rounded-md p-3 bg-gray-950 ${accent}`}>
      <p className="text-xs font-semibold text-gray-400 mb-2">{title}</p>
      {Object.keys(state).length === 0 ? (
        <p className="text-xs text-gray-600 italic">— (no state)</p>
      ) : (
        <>
          <pre className="text-xs text-gray-300 font-mono overflow-auto whitespace-pre-wrap">
            {expanded ? json : preview}
            {!expanded && hasMore && "…"}
          </pre>
          {hasMore && (
            <button
              onClick={() => setExpanded(v => !v)}
              className="text-xs text-blue-400 hover:text-blue-300 mt-1"
            >
              {expanded ? "Show less" : `Show all (${lines.length} lines)`}
            </button>
          )}
        </>
      )}
    </div>
  );
}

// ── Single step card ───────────────────────────────────────────────────────

function StepCard({ step }: { step: DryRunStep }) {
  const [showCommand, setShowCommand] = useState(true);

  return (
    <div className="border border-gray-800 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-4 py-3 bg-gray-900 border-b border-gray-800">
        <div className="flex items-center gap-2">
          <span className="w-5 h-5 rounded-full bg-gray-800 text-gray-400 text-xs flex items-center justify-center font-mono shrink-0">
            {step.step_index + 1}
          </span>
          <p className="text-sm text-gray-200 font-medium">{step.action_description}</p>
        </div>
        <ReversibilityBadge value={step.reversibility} />
      </div>

      <div className="p-4 space-y-4">
        {/* Command / API call */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">
              Command that will run
            </p>
            <button
              onClick={() => setShowCommand(v => !v)}
              className="text-xs text-blue-400 hover:text-blue-300"
            >
              {showCommand ? "hide" : "show"}
            </button>
          </div>
          {showCommand && (
            <pre className="text-xs font-mono bg-gray-950 border border-gray-800 rounded p-3 text-green-300 overflow-auto whitespace-pre-wrap">
              {step.command_or_api_call}
            </pre>
          )}
        </div>

        {/* Before / after */}
        <div className="flex gap-3">
          <StatePanel
            title="Before"
            state={step.predicted_before_state}
            accent="border-gray-700"
          />
          <div className="flex items-center text-gray-600 text-lg shrink-0">→</div>
          <StatePanel
            title="After"
            state={step.predicted_after_state}
            accent="border-blue-900"
          />
        </div>

        {/* Warnings */}
        {step.warnings.length > 0 && (
          <div className="space-y-1">
            {step.warnings.map((w, i) => (
              <div key={i} className="flex gap-2 items-start text-xs text-amber-300">
                <span className="shrink-0 mt-0.5">⚠</span>
                <span>{w}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

export default function DryRunReportView({
  report,
  onClose,
}: {
  report: DryRunReport;
  onClose?: () => void;
}) {
  const hasDestructive = report.steps.some(s => s.reversibility === "manual");

  return (
    <div className="space-y-4">
      {/* Header strip */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-0.5">
            Dry-Run Preview
          </p>
          <p className="text-sm font-semibold text-gray-200">
            {report.plan_action.replace(/_/g, " ")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {report.cached && (
            <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">
              cached
            </span>
          )}
          {onClose && (
            <button
              onClick={onClose}
              className="text-xs text-gray-500 hover:text-gray-300"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Destructive banner */}
      {hasDestructive && (
        <div className="border border-red-800 rounded-lg p-3 bg-red-950">
          <p className="text-xs font-semibold text-red-300 mb-1">
            Destructive action — irreversible
          </p>
          <p className="text-xs text-red-400">
            One or more steps cannot be undone. Review the before-state carefully
            and confirm data loss is acceptable before approving.
          </p>
        </div>
      )}

      {/* Gemini summary */}
      {report.gemini_summary && (
        <div className="border border-indigo-900 rounded-lg p-3 bg-indigo-950">
          <p className="text-xs text-indigo-400 font-medium mb-1 flex items-center gap-1">
            <span>✦</span> AI Summary
          </p>
          <p className="text-sm text-indigo-200 leading-relaxed">{report.gemini_summary}</p>
        </div>
      )}

      {/* Steps */}
      <div className="space-y-3">
        {report.steps.map(step => (
          <StepCard key={step.step_index} step={step} />
        ))}
      </div>

      <p className="text-xs text-gray-600 text-right">
        Computed {new Date(report.computed_at).toLocaleTimeString()} ·{" "}
        no state changes were made
      </p>
    </div>
  );
}
