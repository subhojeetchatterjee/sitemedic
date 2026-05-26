"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { doc, onSnapshot } from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { Incident, AuditEvent } from "@/lib/types";
import IncidentTimeline from "@/components/IncidentTimeline";
import { incidentDuration } from "@/lib/timeline";
import Link from "next/link";

// ── Styles ─────────────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  RESOLVED: "text-green-400",
  REJECTED: "text-gray-400",
  REMEDIATING: "text-purple-400",
  AWAITING_APPROVAL: "text-orange-400",
  DIAGNOSING: "text-blue-400",
  DETECTING: "text-yellow-400",
};

// ── Single column ─────────────────────────────────────────────────────────

function CompareColumn({ incidentId }: { incidentId: string }) {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [audits, setAudits] = useState<AuditEvent[]>([]);

  useEffect(() => {
    const unsub = onSnapshot(doc(db, "incidents", incidentId), snap => {
      if (snap.exists()) setIncident(snap.data() as Incident);
    });
    return unsub;
  }, [incidentId]);

  useEffect(() => {
    fetch(`/api/incidents/${incidentId}/audit`)
      .then(r => r.json())
      .then(setAudits)
      .catch(() => {});
  }, [incidentId]);

  if (!incident) {
    return (
      <div className="flex flex-col h-full border border-gray-800 rounded-xl bg-gray-900">
        <div className="p-4 border-b border-gray-800 animate-pulse">
          <div className="h-4 bg-gray-700 rounded w-3/4 mb-2" />
          <div className="h-3 bg-gray-800 rounded w-1/2" />
        </div>
        <div className="flex-1 flex items-center justify-center text-gray-500 text-sm">
          Loading…
        </div>
      </div>
    );
  }

  const statusColor = STATUS_COLORS[incident.status] ?? "text-gray-400";
  const duration = incidentDuration(incident);

  return (
    <div className="flex flex-col h-full border border-gray-800 rounded-xl bg-gray-900 overflow-hidden">
      <div className="p-3 border-b border-gray-800">
        <div className="flex items-start justify-between gap-2 mb-1">
          <h3 className="text-sm font-semibold text-gray-100 truncate flex-1">
            {incident.title}
          </h3>
          <Link
            href={`/incidents/${incidentId}`}
            className="text-xs text-blue-400 hover:text-blue-300 shrink-0"
          >
            Open ↗
          </Link>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
          <span className="font-mono">{incident.service}</span>
          <span className={`font-medium ${statusColor}`}>{incident.status}</span>
          <span>{duration}</span>
          <span className="text-gray-600">{incident.trace.length} steps</span>
        </div>
      </div>
      <div className="flex-1 min-h-0 p-3">
        <IncidentTimeline incident={incident} auditEvents={audits} compact />
      </div>
    </div>
  );
}

// ── Incident picker ────────────────────────────────────────────────────────

function IncidentPicker({ onPick, exclude }: { onPick: (id: string) => void; exclude: string[] }) {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [query, setQuery] = useState("");

  useEffect(() => {
    fetch("/api/incidents")
      .then(r => r.json())
      .then(setIncidents)
      .catch(() => {});
  }, []);

  const shown = incidents
    .filter(i => !exclude.includes(i.problem_id))
    .filter(i =>
      query
        ? i.problem_id.includes(query) ||
          i.title.toLowerCase().includes(query.toLowerCase()) ||
          i.service.includes(query)
        : true,
    )
    .slice(0, 8);

  return (
    <div className="border border-gray-700 rounded-xl bg-gray-900 p-4 flex flex-col gap-3 h-full">
      <p className="text-sm font-medium text-gray-300">Pick incident to compare</p>
      <input
        className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-600"
        placeholder="Search by ID, title or service…"
        value={query}
        onChange={e => setQuery(e.target.value)}
      />
      <div className="flex-1 overflow-y-auto space-y-1">
        {shown.map(i => (
          <button
            key={i.problem_id}
            onClick={() => onPick(i.problem_id)}
            className="w-full text-left border border-gray-800 rounded-lg p-2.5 hover:border-gray-600 bg-gray-950 group"
          >
            <p className="text-xs font-medium text-gray-200 group-hover:text-white truncate">
              {i.title}
            </p>
            <div className="flex gap-2 mt-0.5 text-xs text-gray-600">
              <span className="font-mono">{i.service}</span>
              <span>{i.status}</span>
              <span className="ml-auto">{new Date(i.started_at).toLocaleDateString()}</span>
            </div>
          </button>
        ))}
        {shown.length === 0 && (
          <p className="text-xs text-gray-600 text-center py-4">No incidents found</p>
        )}
      </div>
    </div>
  );
}

// ── Inner page (needs useSearchParams) ────────────────────────────────────

function ComparePageInner() {
  const params = useSearchParams();
  const router = useRouter();
  const [idA, setIdA] = useState(params.get("a") ?? "");
  const [idB, setIdB] = useState(params.get("b") ?? "");

  useEffect(() => {
    const q = new URLSearchParams();
    if (idA) q.set("a", idA);
    if (idB) q.set("b", idB);
    router.replace(`/compare?${q.toString()}`, { scroll: false });
  }, [idA, idB, router]);

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)] gap-4">
      <div className="flex items-center gap-4">
        <Link href="/" className="text-sm text-gray-500 hover:text-gray-300">
          ← Dashboard
        </Link>
        <h1 className="text-lg font-bold text-gray-100">Timeline Comparison</h1>
        {(idA || idB) && (
          <button
            onClick={() => { setIdA(""); setIdB(""); }}
            className="ml-auto text-xs text-gray-500 hover:text-gray-300 border border-gray-800 rounded px-2 py-1"
          >
            Reset
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1 min-h-0">
        {!idA ? (
          <IncidentPicker onPick={setIdA} exclude={[idB]} />
        ) : (
          <div className="flex flex-col h-full gap-2">
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-600 font-mono truncate">{idA}</span>
              <button onClick={() => setIdA("")} className="text-xs text-gray-600 hover:text-gray-400 ml-auto shrink-0">
                Change ✕
              </button>
            </div>
            <div className="flex-1 min-h-0">
              <CompareColumn incidentId={idA} />
            </div>
          </div>
        )}

        {!idB ? (
          <IncidentPicker onPick={setIdB} exclude={[idA]} />
        ) : (
          <div className="flex flex-col h-full gap-2">
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-600 font-mono truncate">{idB}</span>
              <button onClick={() => setIdB("")} className="text-xs text-gray-600 hover:text-gray-400 ml-auto shrink-0">
                Change ✕
              </button>
            </div>
            <div className="flex-1 min-h-0">
              <CompareColumn incidentId={idB} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Page export ───────────────────────────────────────────────────────────

export default function ComparePage() {
  return (
    <Suspense fallback={<div className="text-gray-500 text-sm animate-pulse">Loading comparison…</div>}>
      <ComparePageInner />
    </Suspense>
  );
}
