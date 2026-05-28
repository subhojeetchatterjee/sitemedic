"use client";

import { useEffect, useRef, useState } from "react";
import type { AuditEvent } from "@/lib/types";
import Link from "next/link";


// ── Colour coding ─────────────────────────────────────────────────────────

const ACTOR_STYLES: Record<string, string> = {
  agent:    "bg-blue-900 text-blue-300",
  operator: "bg-purple-900 text-purple-300",
  system:   "bg-gray-800 text-gray-400",
};

const RESULT_STYLES: Record<string, string> = {
  success: "text-green-400",
  failure: "text-red-400",
  partial: "text-yellow-400",
};

const ACTION_ICONS: Record<string, string> = {
  detect_cycle:           "🔍",
  incident_created:       "🚨",
  gemini_call:            "🤖",
  mcp_tool_call:          "📡",
  gcp_tool_call:          "☁️",
  plan_generated:         "📋",
  approved:               "✅",
  rejected:               "❌",
  executed:               "⚙️",
  postmortem_generated:   "📝",
  prediction_stored:      "🔮",
  cluster_formed:         "🔗",
  cluster_executed:       "🔗",
  chain_verified:         "🔒",
  audit_failure:          "⚠️",
  agent_started:          "🟢",
};

// ── Chain verification banner ─────────────────────────────────────────────

interface ChainStatus {
  valid: boolean;
  checked: number;
  tampered_at: string | null;
}

function ChainBanner({ status }: { status: ChainStatus | null }) {
  if (!status) return null;
  if (status.valid) {
    return (
      <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-green-950 border border-green-800 text-green-300 text-sm">
        <span>🔒</span>
        <span>
          Hash chain intact — {status.checked} event{status.checked !== 1 ? "s" : ""} verified.
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-red-950 border border-red-700 text-red-300 text-sm">
      <span>⚠️</span>
      <span>
        <strong>Chain break detected</strong> after {status.checked} events.
        First tampered event ID:{" "}
        <code className="font-mono text-xs">{status.tampered_at}</code>
      </span>
    </div>
  );
}

// ── Event row ─────────────────────────────────────────────────────────────

function EventRow({ ev }: { ev: AuditEvent }) {
  const [open, setOpen] = useState(false);
  const icon = ACTION_ICONS[ev.action_type] ?? "•";
  const actorCls = ACTOR_STYLES[ev.actor] ?? "bg-gray-800 text-gray-400";
  const resultCls = RESULT_STYLES[ev.result] ?? "text-gray-400";

  return (
    <div className="border border-gray-800 rounded-lg bg-gray-900">
      <button
        className="w-full text-left px-4 py-3 flex items-center gap-3 hover:bg-gray-800 transition-colors rounded-lg"
        onClick={() => setOpen(o => !o)}
      >
        {/* seq */}
        <span className="text-gray-600 font-mono text-xs w-8 shrink-0">
          #{ev.seq}
        </span>
        {/* icon */}
        <span className="text-base w-5 shrink-0">{icon}</span>
        {/* action_type */}
        <span className="font-mono text-sm text-gray-200 flex-1 truncate">
          {ev.action_type}
        </span>
        {/* actor badge */}
        <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${actorCls}`}>
          {ev.actor}
        </span>
        {/* result */}
        <span className={`text-xs font-medium shrink-0 ${resultCls}`}>
          {ev.result}
        </span>
        {/* timestamp */}
        <span className="text-xs text-gray-500 shrink-0 hidden sm:block">
          {new Date(ev.timestamp).toLocaleString()}
        </span>
        {/* expand caret */}
        <span className="text-gray-600 shrink-0">{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="px-4 pb-4 border-t border-gray-800 mt-1 pt-3 space-y-2 text-xs">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-gray-400">
            <div>
              <span className="text-gray-500">Event ID</span>
              <p className="font-mono text-gray-300 break-all">{ev.event_id}</p>
            </div>
            <div>
              <span className="text-gray-500">Actor identity</span>
              <p className="font-mono text-gray-300">{ev.actor_identity}</p>
            </div>
            {ev.incident_id && (
              <div>
                <span className="text-gray-500">Incident</span>
                <p>
                  <Link
                    href={`/incidents/${ev.incident_id}`}
                    className="font-mono text-blue-400 hover:text-blue-300"
                  >
                    {ev.incident_id}
                  </Link>
                </p>
              </div>
            )}
            {ev.resource && (
              <div>
                <span className="text-gray-500">Resource</span>
                <p className="font-mono text-gray-300 break-all">{ev.resource}</p>
              </div>
            )}
          </div>

          {Object.keys(ev.payload).length > 0 && (
            <div>
              <p className="text-gray-500 mb-1">Payload</p>
              <pre className="bg-gray-950 rounded p-2 overflow-x-auto text-gray-300 text-xs">
                {JSON.stringify(ev.payload, null, 2)}
              </pre>
            </div>
          )}

          <div>
            <p className="text-gray-500 mb-1">Hash chain</p>
            <p className="font-mono text-gray-600 break-all">{ev.hash_chain}</p>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────

export default function AuditPage() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [chainStatus, setChainStatus] = useState<ChainStatus | null>(null);
  const [verifying, setVerifying] = useState(false);

  // Filters
  const [filterIncident, setFilterIncident] = useState("");
  const [filterActor, setFilterActor] = useState("");
  const [filterAction, setFilterAction] = useState("");
  const [filterSince, setFilterSince] = useState("");
  const [filterUntil, setFilterUntil] = useState("");

  // Load audit events from the API proxy (Firestore rules block direct client reads)
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch("/api/audit?limit=200");
        if (!cancelled) {
          const data: AuditEvent[] = res.ok ? await res.json() : [];
          setEvents(data);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    // Refresh every 30s so new events appear without a page reload
    const interval = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  // Client-side filter application
  const filtered = events.filter(ev => {
    if (filterIncident && ev.incident_id !== filterIncident) return false;
    if (filterActor && ev.actor !== filterActor) return false;
    if (filterAction && !ev.action_type.includes(filterAction)) return false;
    if (filterSince && ev.timestamp < filterSince) return false;
    if (filterUntil && ev.timestamp > filterUntil) return false;
    return true;
  });

  async function runVerify() {
    setVerifying(true);
    try {
      const res = await fetch("/api/audit/verify");
      const data: ChainStatus = await res.json();
      setChainStatus(data);
    } catch {
      setChainStatus(null);
    } finally {
      setVerifying(false);
    }
  }

  function exportData(fmt: "json" | "csv") {
    const params = new URLSearchParams({ fmt, limit: "1000" });
    if (filterIncident) params.set("incident_id", filterIncident);
    window.open(`/api/audit/export?${params.toString()}`, "_blank");
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <Link href="/" className="text-sm text-gray-500 hover:text-gray-300 mb-1 inline-block">
            ← Dashboard
          </Link>
          <h1 className="text-2xl font-bold text-gray-100">Audit Trail</h1>
          <p className="text-sm text-gray-500 mt-1">
            Tamper-evident log of every agent, operator, and system action.
            90-day Firestore retention · Cloud Logging copy retained per project policy.
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={runVerify}
            disabled={verifying}
            className="px-3 py-1.5 text-sm rounded-lg bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700 disabled:opacity-50"
          >
            {verifying ? "Verifying…" : "🔒 Verify chain"}
          </button>
          <button
            onClick={() => exportData("json")}
            className="px-3 py-1.5 text-sm rounded-lg bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700"
          >
            ↓ JSON
          </button>
          <button
            onClick={() => exportData("csv")}
            className="px-3 py-1.5 text-sm rounded-lg bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700"
          >
            ↓ CSV
          </button>
        </div>
      </div>

      {/* Chain verification result */}
      {chainStatus && (
        <div className="mb-4">
          <ChainBanner status={chainStatus} />
        </div>
      )}

      {/* Filters */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2 mb-6">
        <input
          type="text"
          placeholder="Incident ID"
          value={filterIncident}
          onChange={e => setFilterIncident(e.target.value.trim())}
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-gray-500"
        />
        <select
          value={filterActor}
          onChange={e => setFilterActor(e.target.value)}
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-gray-500"
        >
          <option value="">All actors</option>
          <option value="agent">agent</option>
          <option value="operator">operator</option>
          <option value="system">system</option>
        </select>
        <input
          type="text"
          placeholder="Action type (partial)"
          value={filterAction}
          onChange={e => setFilterAction(e.target.value.trim())}
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-gray-500"
        />
        <input
          type="datetime-local"
          value={filterSince}
          onChange={e => setFilterSince(e.target.value)}
          title="Since"
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-gray-500"
        />
        <input
          type="datetime-local"
          value={filterUntil}
          onChange={e => setFilterUntil(e.target.value)}
          title="Until"
          className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-gray-500"
        />
      </div>

      {/* Stats bar */}
      {!loading && (
        <div className="flex items-center gap-4 text-xs text-gray-500 mb-4">
          <span>{filtered.length} event{filtered.length !== 1 ? "s" : ""} shown</span>
          {filtered.length !== events.length && (
            <span>({events.length} total in window)</span>
          )}
          <button
            onClick={() => {
              setFilterIncident("");
              setFilterActor("");
              setFilterAction("");
              setFilterSince("");
              setFilterUntil("");
            }}
            className="text-gray-600 hover:text-gray-400 underline"
          >
            Clear filters
          </button>
        </div>
      )}

      {/* Event list */}
      {loading ? (
        <div className="text-gray-500 text-sm">Connecting to Firestore…</div>
      ) : filtered.length === 0 ? (
        <div className="border border-dashed border-gray-800 rounded-lg p-12 text-center">
          <p className="text-gray-500">No audit events match the current filters.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {filtered.map(ev => (
            <EventRow key={ev.event_id} ev={ev} />
          ))}
        </div>
      )}
    </div>
  );
}
