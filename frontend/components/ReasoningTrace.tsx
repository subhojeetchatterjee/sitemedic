"use client";

import { useState } from "react";
import type { Diagnosis, TraceStep } from "@/lib/types";

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const color =
    confidence >= 0.75 ? "bg-green-500" :
    confidence >= 0.6  ? "bg-amber-500" :
    "bg-red-500";
  const label =
    confidence >= 0.75 ? "High confidence" :
    confidence >= 0.6  ? "Medium confidence" :
    "Low confidence — treat with caution";

  return (
    <div className="mb-4">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-gray-400 font-medium">{label}</span>
        <span className="text-xs font-mono text-gray-300">{pct}%</span>
      </div>
      <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function DiagnosisPanel({ diagnosis }: { diagnosis: Diagnosis }) {
  const [showUnknowns, setShowUnknowns] = useState(false);
  const [showAlternatives, setShowAlternatives] = useState(false);

  const evidenceBadge = {
    direct:         "bg-green-900 text-green-300",
    circumstantial: "bg-amber-900 text-amber-300",
    speculative:    "bg-red-900 text-red-300",
  }[diagnosis.evidence_strength] ?? "bg-gray-800 text-gray-400";

  return (
    <div className="border border-gray-700 rounded-lg p-4 mb-4 bg-gray-900 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wide">
          Diagnosis
        </span>
        <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${evidenceBadge}`}>
          {diagnosis.evidence_strength} evidence
        </span>
      </div>

      <ConfidenceBar confidence={diagnosis.confidence} />

      <p className="text-sm text-gray-200 leading-relaxed">{diagnosis.root_cause}</p>

      <p className="text-xs text-gray-500 italic">{diagnosis.confidence_rationale}</p>

      {/* Unknowns */}
      {diagnosis.unknowns.length > 0 && (
        <div>
          <button
            onClick={() => setShowUnknowns(v => !v)}
            className="text-xs text-amber-400 hover:text-amber-300 flex items-center gap-1"
          >
            <span>{showUnknowns ? "▾" : "▸"}</span>
            <span>{diagnosis.unknowns.length} unknown{diagnosis.unknowns.length !== 1 ? "s" : ""}</span>
          </button>
          {showUnknowns && (
            <ul className="mt-2 space-y-1 pl-3 border-l border-amber-800">
              {diagnosis.unknowns.map((u, i) => (
                <li key={i} className="text-xs text-amber-300">{u}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Alternatives */}
      {diagnosis.alternative_explanations.length > 0 && (
        <div>
          <button
            onClick={() => setShowAlternatives(v => !v)}
            className="text-xs text-blue-400 hover:text-blue-300 flex items-center gap-1"
          >
            <span>{showAlternatives ? "▾" : "▸"}</span>
            <span>
              {diagnosis.alternative_explanations.length} alternative explanation
              {diagnosis.alternative_explanations.length !== 1 ? "s" : ""}
            </span>
          </button>
          {showAlternatives && (
            <div className="mt-2 space-y-2">
              {diagnosis.alternative_explanations
                .slice()
                .sort((a, b) => b.likelihood - a.likelihood)
                .map((alt, i) => (
                  <div
                    key={i}
                    className="border border-gray-700 rounded p-3 bg-gray-950 text-xs space-y-1"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-gray-200 font-medium">{alt.explanation}</p>
                      <span className="text-gray-500 font-mono shrink-0">
                        {Math.round(alt.likelihood * 100)}%
                      </span>
                    </div>
                    <p className="text-green-400">
                      <span className="text-gray-500">For: </span>{alt.evidence_for}
                    </p>
                    <p className="text-red-400">
                      <span className="text-gray-500">Against: </span>{alt.evidence_against}
                    </p>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ReasoningTrace({
  steps,
  isActive,
  diagnosis,
}: {
  steps: TraceStep[];
  isActive: boolean;
  diagnosis?: Diagnosis | null;
}) {
  return (
    <div className="space-y-4">
      {diagnosis && <DiagnosisPanel diagnosis={diagnosis} />}

      {steps.map((step, i) => (
        <div key={i} className="flex gap-3">
          <div className="flex flex-col items-center">
            <div
              className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-mono shrink-0
                ${isActive && i === steps.length - 1
                  ? "bg-blue-600 text-white animate-pulse"
                  : "bg-gray-800 text-gray-400"
                }`}
            >
              {step.step === 999 ? "✓" : step.step + 1}
            </div>
            {i < steps.length - 1 && (
              <div className="w-px flex-1 bg-gray-800 mt-1" />
            )}
          </div>

          <div className="flex-1 pb-4">
            {step.thought && (
              <p className="text-sm text-gray-300 leading-relaxed font-serif mb-2">
                {step.thought}
              </p>
            )}

            {step.tool_call && (
              <div className="bg-gray-900 border border-gray-800 rounded-md p-3 mb-2">
                <div className="flex items-center gap-2 mb-1">
                  <p className="text-xs text-blue-400 font-mono">
                    → {step.tool_call.name}
                  </p>
                  {step.provider && (
                    <span
                      className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                        step.provider === "dynatrace"
                          ? "bg-purple-900 text-purple-300"
                          : "bg-blue-900 text-blue-300"
                      }`}
                    >
                      {step.provider === "dynatrace" ? "Dynatrace" : "GCP"}
                    </span>
                  )}
                </div>
                <pre className="text-xs text-gray-500 overflow-auto">
                  {JSON.stringify(step.tool_call.args, null, 2)}
                </pre>
              </div>
            )}

            {step.tool_result !== undefined && step.tool_result !== null && (
              <div className="bg-gray-950 border border-gray-800 rounded-md p-3">
                <p className="text-xs text-green-500 font-mono mb-1">← result</p>
                <pre className="text-xs text-gray-400 overflow-auto max-h-48">
                  {JSON.stringify(step.tool_result, null, 2)}
                </pre>
              </div>
            )}

            <p className="text-xs text-gray-600 mt-1 font-mono">
              {new Date(step.timestamp).toLocaleTimeString()}
            </p>
          </div>
        </div>
      ))}

      {isActive && steps.length > 0 && (
        <div className="flex gap-3 items-center pl-9">
          <span className="text-sm text-gray-500 animate-pulse">Agent is thinking…</span>
        </div>
      )}
    </div>
  );
}
