"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import CostImpactPanel from "./CostImpactPanel";
import type { Incident } from "@/lib/types";

// ── Types ─────────────────────────────────────────────────────────────────

interface CostSnapshot {
  current_rps: number;
  error_rate: number;
  duration_minutes: number;
  burn_rate_per_min: number | null;
  revenue_configured: boolean;
  status?: string;
  error?: string;
}

interface SimilarIncident {
  problem_id: string;
  started_at: string;
  updated_at: string;
  status: string;
  service: string;
  plan?: { action?: string };
}

// ── Dependency graph — mini SVG ───────────────────────────────────────────

function DependencyGraph({ service }: { service: string }) {
  // Simple star-topology showing the affected service highlighted
  const cx = 80, cy = 60, r = 55;
  const spokes: { label: string; angle: number }[] = [
    { label: "frontend",  angle: -90 },
    { label: "auth",      angle: 30  },
    { label: "db",        angle: 150 },
    { label: "cache",     angle: 270 },
  ];

  return (
    <svg viewBox="0 0 160 120" className="w-full h-24 text-gray-600">
      {spokes.map(({ label, angle }) => {
        const rad = (angle * Math.PI) / 180;
        const nx = cx + r * Math.cos(rad);
        const ny = cy + r * Math.sin(rad);
        return (
          <g key={label}>
            <line x1={cx} y1={cy} x2={nx} y2={ny} stroke="#374151" strokeWidth={1} />
            <circle cx={nx} cy={ny} r={8} fill="#1f2937" stroke="#374151" strokeWidth={1} />
            <text x={nx} y={ny + 1} textAnchor="middle" dominantBaseline="middle" fontSize={6} fill="#6b7280">
              {label.slice(0, 5)}
            </text>
          </g>
        );
      })}
      {/* Affected service node */}
      <circle cx={cx} cy={cy} r={18} fill="#ef444422" stroke="#ef4444" strokeWidth={1.5} />
      <text x={cx} y={cy + 1} textAnchor="middle" dominantBaseline="middle" fontSize={7} fill="#ef4444" fontWeight="600">
        {service.slice(0, 8)}
      </text>
      {/* Pulse ring */}
      <circle cx={cx} cy={cy} r={22} fill="none" stroke="#ef444433" strokeWidth={1} />
    </svg>
  );
}

// ── Similar incidents ─────────────────────────────────────────────────────

function SimilarIncidentsPanel({ incident }: { incident: Incident }) {
  const [similar, setSimilar] = useState<SimilarIncident[]>([]);

  useEffect(() => {
    fetch("/api/incidents")
      .then(r => r.json())
      .then((rows: SimilarIncident[]) => {
        const resolved = rows.filter(
          i =>
            i.problem_id !== incident.problem_id &&
            i.service === incident.service &&
            i.status === "RESOLVED",
        );
        setSimilar(resolved.slice(0, 5));
      })
      .catch(() => {});
  }, [incident.problem_id, incident.service]);

  if (similar.length === 0) return null;

  const durations = similar.map(i => {
    const ms = new Date(i.updated_at).getTime() - new Date(i.started_at).getTime();
    return ms / 60_000;
  });
  const avgMin = durations.reduce((a, b) => a + b, 0) / durations.length;

  const currentMs = Date.now() - new Date(incident.started_at).getTime();
  const currentMin = currentMs / 60_000;
  const isAhead = currentMin < avgMin;

  return (
    <div className="border border-gray-800 rounded-lg p-3 bg-gray-900 space-y-2">
      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
        Similar past incidents
      </p>
      <p className="text-xs text-gray-400">
        {similar.length} past incident{similar.length !== 1 ? "s" : ""} on{" "}
        <span className="font-mono text-gray-300">{incident.service}</span> averaged{" "}
        <span className="font-medium text-gray-200">{avgMin.toFixed(1)} min</span> to resolve.{" "}
        {incident.status !== "RESOLVED" && incident.status !== "REJECTED" && (
          <span className={isAhead ? "text-green-400" : "text-amber-300"}>
            This one is at {currentMin.toFixed(1)} min —{" "}
            {isAhead ? "ahead of pace" : "running long"}.
          </span>
        )}
      </p>
      <div className="space-y-1">
        {similar.slice(0, 3).map(s => (
          <div key={s.problem_id} className="flex items-center gap-2 text-xs text-gray-600">
            <span className="w-1.5 h-1.5 rounded-full bg-green-700 shrink-0" />
            <span className="font-mono truncate">{s.problem_id}</span>
            <span className="shrink-0">
              {((new Date(s.updated_at).getTime() - new Date(s.started_at).getTime()) / 60_000).toFixed(1)} min
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────

interface Props {
  incident: Incident;
  latestCostSnap?: CostSnapshot | null;
}

export default function ImpactSidePanel({ incident }: Props) {
  const [costSnap, setCostSnap] = useState<CostSnapshot | null>(null);

  // Tap into cost SSE for users-affected estimate, with backoff reconnect
  useEffect(() => {
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelay = 1000;
    let destroyed = false;

    function connect() {
      if (destroyed) return;
      es = new EventSource(`/api/incidents/${incident.problem_id}/cost-stream`);
      es.onmessage = e => {
        retryDelay = 1000;
        try {
          const d: CostSnapshot = JSON.parse(e.data);
          if (!d.error) setCostSnap(d);
          if (d.status === "RESOLVED" || d.status === "REJECTED") {
            es?.close();
            es = null;
          }
        } catch { /* ignore */ }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        if (!destroyed && retryDelay <= 30_000) {
          retryTimer = setTimeout(() => {
            retryDelay = Math.min(retryDelay * 2, 30_000);
            connect();
          }, retryDelay);
        }
      };
    }

    connect();

    return () => {
      destroyed = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      es?.close();
    };
  }, [incident.problem_id]);

  const affectedRps = costSnap
    ? costSnap.current_rps * costSnap.error_rate
    : null;

  // Rough estimate: 1 request ≈ 0.1 unique users within the minute
  const usersEstimate = affectedRps != null
    ? Math.round(affectedRps * 60 * 0.1)
    : null;

  return (
    <div className="space-y-4">
      {/* Services affected */}
      <div className="border border-gray-800 rounded-lg p-3 bg-gray-900">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
          Affected service
        </p>
        <DependencyGraph service={incident.service} />
        <div className="mt-2 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-red-500" />
          <span className="text-xs font-mono text-gray-300">{incident.service}</span>
          <span className="text-xs text-gray-600 ml-auto">{incident.severity}</span>
        </div>
      </div>

      {/* Users estimate */}
      {usersEstimate !== null && costSnap?.revenue_configured && (
        <div className="border border-gray-800 rounded-lg p-3 bg-gray-900">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
            Est. users affected / min
          </p>
          <p className="text-2xl font-bold text-orange-400 tabular-nums">
            ~{usersEstimate.toLocaleString()}
          </p>
          <p className="text-xs text-gray-600 mt-0.5">
            {affectedRps?.toFixed(1)} req/s × {((costSnap.error_rate ?? 0) * 100).toFixed(1)}% error rate
          </p>
        </div>
      )}

      {/* Live cost impact */}
      <CostImpactPanel incidentId={incident.problem_id} />

      {/* Similar incidents */}
      <SimilarIncidentsPanel incident={incident} />

      {/* Compare link */}
      <Link
        href={`/compare?a=${incident.problem_id}`}
        className="block text-center text-xs text-blue-400 hover:text-blue-300 border border-blue-900 rounded-lg py-2 px-3"
      >
        Compare with another incident →
      </Link>
    </div>
  );
}
