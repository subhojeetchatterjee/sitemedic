"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  Cell,
} from "recharts";

// ── Types ─────────────────────────────────────────────────────────────────

type Window = "7d" | "30d" | "90d";

interface Summary {
  total_incidents: number;
  resolved: number;
  rejected: number;
  mttd_seconds: number;
  mttdi_minutes: number;
  mttr_minutes: number;
  approval_rate: number;
  auto_resolution_rate: number;
}

interface VolumePoint     { date: string; [service: string]: number | string }
interface MttrPoint       { date: string; mttr: number | null }
interface Pattern         { action: string; count: number }
interface PredPoint       { date: string; precision: number | null; total: number }
interface Service {
  name: string; total: number; resolved: number; rejected: number;
  avg_mttr: number;
  common_actions: { action: string; count: number }[];
}
interface Cost {
  total_delta_usd: number; remediation_count: number;
  alternative_adopted_count: number; alternative_adoption_rate: number;
}
interface Reasoning {
  avg_steps: number;
  tool_distribution: { tool: string; count: number }[];
}
interface Snapshot {
  window: string; computed_at: string; incident_count: number;
  summary: Summary;
  incident_volume: VolumePoint[];
  mttr_trend: MttrPoint[];
  failure_patterns: Pattern[];
  prediction_accuracy: PredPoint[];
  services: Service[];
  cost: Cost;
  reasoning: Reasoning;
}

interface CalibBucket {
  bucket_idx: number;
  label: string;
  count: number;
  resolved: number;
  accuracy: number | null;
  avg_confidence: number | null;
}
interface CalibSnapshot {
  computed_at: string;
  lookback_incidents: number;
  ece: number;
  buckets: CalibBucket[];
  interpretation: string;
}

// ── Colour palette ─────────────────────────────────────────────────────────

const PALETTE = [
  "#6366f1", "#f59e0b", "#10b981", "#ef4444",
  "#8b5cf6", "#06b6d4", "#f97316", "#84cc16",
];

// ── Formatting helpers ─────────────────────────────────────────────────────

function fmtMin(m: number) {
  if (m < 1)  return `${Math.round(m * 60)}s`;
  if (m < 60) return `${m.toFixed(1)}m`;
  return `${(m / 60).toFixed(1)}h`;
}
function fmtPct(r: number) { return `${(r * 100).toFixed(1)}%`; }
function shortDate(iso: string) {
  const d = new Date(iso + "T00:00:00");
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

// ── Shared UI primitives ───────────────────────────────────────────────────

function Skeleton({ h = "h-48", className = "" }: { h?: string; className?: string }) {
  return <div className={`${h} rounded-lg bg-gray-800 animate-pulse ${className}`} />;
}
function CardSkeleton() {
  return (
    <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 space-y-3">
      <Skeleton h="h-3" className="w-1/3" />
      <Skeleton h="h-7" className="w-1/2" />
      <Skeleton h="h-3" className="w-2/3" />
    </div>
  );
}
function MetricCard({
  label, value, sub, accent = "text-gray-100",
}: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="border border-gray-800 rounded-lg p-4 bg-gray-900">
      <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-2xl font-bold ${accent}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  );
}
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
        {title}
      </h2>
      {children}
    </section>
  );
}
function ChartShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 h-56">
      {children}
    </div>
  );
}
function NoData({ msg = "No data in this window" }: { msg?: string }) {
  return (
    <div className="h-56 flex items-center justify-center border border-dashed border-gray-800 rounded-lg">
      <p className="text-gray-600 text-sm">{msg}</p>
    </div>
  );
}
function EmptyState() {
  return (
    <div className="border border-dashed border-gray-800 rounded-lg p-16 text-center">
      <p className="text-gray-400 text-lg mb-2">No incidents yet</p>
      <p className="text-gray-600 text-sm max-w-sm mx-auto">
        Once SiteMedic detects and resolves a few incidents, charts and metrics will
        populate here automatically.
      </p>
    </div>
  );
}
const TOOLTIP_STYLE = {
  contentStyle: { background: "#111827", border: "1px solid #374151", fontSize: 12 },
};

// ── Service drill-down modal ───────────────────────────────────────────────

function ServiceModal({ svc, onClose }: { svc: Service; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl p-6 w-full max-w-lg mx-4 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-gray-100 font-mono text-lg">{svc.name}</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-200 text-2xl leading-none">×</button>
        </div>
        <div className="grid grid-cols-3 gap-3 mb-5">
          {[
            { label: "Total",    value: String(svc.total),             colour: "text-gray-100" },
            { label: "Resolved", value: String(svc.resolved),          colour: "text-green-400" },
            { label: "Avg MTTR", value: svc.avg_mttr > 0 ? fmtMin(svc.avg_mttr) : "—", colour: "text-indigo-400" },
          ].map(({ label, value, colour }) => (
            <div key={label} className="bg-gray-800 rounded-lg p-3 text-center">
              <p className={`text-2xl font-bold ${colour}`}>{value}</p>
              <p className="text-xs text-gray-500 mt-0.5">{label}</p>
            </div>
          ))}
        </div>
        {svc.common_actions.length > 0 && (
          <>
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Failure modes</p>
            <div className="space-y-2">
              {svc.common_actions.map(a => {
                const pct = Math.max(4, Math.round((a.count / svc.total) * 100));
                return (
                  <div key={a.action} className="flex items-center gap-2">
                    <div className="flex-1 bg-gray-800 rounded-full h-1.5 overflow-hidden">
                      <div className="h-full bg-indigo-500 rounded-full" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-xs font-mono text-gray-300 w-40 truncate">{a.action.replace(/_/g, " ")}</span>
                    <span className="text-xs text-gray-500 w-6 text-right">{a.count}</span>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Window toggle ──────────────────────────────────────────────────────────

function WindowToggle({ value, onChange }: { value: Window; onChange: (w: Window) => void }) {
  return (
    <div className="flex bg-gray-900 border border-gray-800 rounded-lg p-0.5 gap-0.5">
      {(["7d", "30d", "90d"] as Window[]).map(w => (
        <button
          key={w}
          onClick={() => onChange(w)}
          className={`px-3 py-1 text-sm rounded-md transition-colors ${
            value === w
              ? "bg-gray-700 text-white font-medium"
              : "text-gray-500 hover:text-gray-300"
          }`}
        >
          {w}
        </button>
      ))}
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────

export default function AnalyticsPage() {
  const [win, setWin]               = useState<Window>("30d");
  const [snap, setSnap]             = useState<Snapshot | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [selectedSvc, setSelectedSvc] = useState<Service | null>(null);
  const [calib, setCalib]           = useState<CalibSnapshot | null>(null);
  const [sysHealth, setSysHealth]   = useState<{ detection_distribution?: { webhook: number; polling: number; unknown: number; total: number }; time_to_detect?: { avg_ms_webhook: number | null; avg_ms_polling: number | null } } | null>(null);
  // Guard: charts use browser APIs — only render after mount
  const [mounted, setMounted]       = useState(false);
  useEffect(() => setMounted(true), []);

  const load = useCallback(async (w: Window) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/analytics?window=${w}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: Snapshot | null = await res.json();
      setSnap(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(win); }, [win, load]);

  // Calibration and system health load once (not window-dependent)
  useEffect(() => {
    fetch("/api/calibration")
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setCalib(d))
      .catch(() => {});
    fetch("/api/system-health")
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setSysHealth(d))
      .catch(() => {});
  }, []);

  const isEmpty       = !loading && (!snap || snap.incident_count === 0);
  const serviceKeys   = snap?.incident_volume?.length
    ? Object.keys(snap.incident_volume[0]).filter(k => k !== "date")
    : [];
  const hasVolume     = serviceKeys.length > 0 && snap!.incident_volume.some(d => serviceKeys.some(k => (d[k] as number) > 0));
  const hasMttr       = (snap?.mttr_trend ?? []).some(d => d.mttr !== null);
  const hasPatterns   = (snap?.failure_patterns ?? []).length > 0;
  const hasPred       = (snap?.prediction_accuracy ?? []).some(d => d.total > 0);
  const hasTools      = (snap?.reasoning?.tool_distribution ?? []).length > 0;

  return (
    <div className="space-y-10">

      {/* ── Header ── */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <Link href="/" className="text-sm text-gray-500 hover:text-gray-300 mb-1 inline-block">
            ← Dashboard
          </Link>
          <h1 className="text-2xl font-bold text-gray-100">Analytics</h1>
          {snap?.computed_at && (
            <p className="text-xs text-gray-600 mt-0.5">
              Snapshot {new Date(snap.computed_at).toLocaleString()} ·{" "}
              {snap.incident_count} incident{snap.incident_count !== 1 ? "s" : ""} in window
            </p>
          )}
        </div>
        <WindowToggle value={win} onChange={w => setWin(w)} />
      </div>

      {error && (
        <div className="bg-red-950 border border-red-800 text-red-300 text-sm px-4 py-3 rounded-lg">
          Failed to load: {error}. Is the agent running?
        </div>
      )}

      {isEmpty && !error && <EmptyState />}

      {/* ── 1. Top-line metrics ── */}
      {(loading || (!isEmpty && snap?.summary)) && (
        <Section title="Top-line metrics">
          {loading ? (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              {Array.from({ length: 5 }).map((_, i) => <CardSkeleton key={i} />)}
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              <MetricCard
                label="MTTD"
                value={`~${snap!.summary.mttd_seconds}s`}
                sub="Detection loop cadence"
              />
              <MetricCard
                label="MTTDi"
                value={snap!.summary.mttdi_minutes > 0 ? fmtMin(snap!.summary.mttdi_minutes) : "—"}
                sub="Creation → plan ready"
                accent={
                  snap!.summary.mttdi_minutes > 0 && snap!.summary.mttdi_minutes < 5
                    ? "text-green-400"
                    : "text-yellow-400"
                }
              />
              <MetricCard
                label="MTTR"
                value={snap!.summary.mttr_minutes > 0 ? fmtMin(snap!.summary.mttr_minutes) : "—"}
                sub={`${snap!.summary.resolved} incidents resolved`}
                accent={
                  snap!.summary.mttr_minutes > 0 && snap!.summary.mttr_minutes < 10
                    ? "text-green-400"
                    : "text-orange-400"
                }
              />
              <MetricCard
                label="Approval rate"
                value={fmtPct(snap!.summary.approval_rate)}
                sub="Plans approved by operator"
              />
              <MetricCard
                label="Auto-resolution"
                value={fmtPct(snap!.summary.auto_resolution_rate)}
                sub={`${snap!.summary.resolved} / ${snap!.summary.total_incidents} total`}
                accent={
                  snap!.summary.auto_resolution_rate > 0.7
                    ? "text-green-400"
                    : "text-yellow-400"
                }
              />
            </div>
          )}
        </Section>
      )}

      {/* ── 2. Charts ── */}
      {!isEmpty && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

          {/* Incident volume */}
          <Section title="Incident volume by service">
            {loading ? <Skeleton h="h-56" /> : !mounted ? <Skeleton h="h-56" /> : !hasVolume ? (
              <NoData msg="No incidents in this window" />
            ) : (
              <ChartShell>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={snap!.incident_volume} barSize={6}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fill: "#6b7280", fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fill: "#6b7280", fontSize: 10 }} allowDecimals={false} />
                    <Tooltip {...TOOLTIP_STYLE} labelFormatter={v => new Date(String(v) + "T00:00:00").toLocaleDateString()} />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    {serviceKeys.map((svc, i) => (
                      <Bar key={svc} dataKey={svc} stackId="vol" fill={PALETTE[i % PALETTE.length]} />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </ChartShell>
            )}
          </Section>

          {/* MTTR trend */}
          <Section title="MTTR trend (target ≤ 5 min)">
            {loading ? <Skeleton h="h-56" /> : !mounted ? <Skeleton h="h-56" /> : !hasMttr ? (
              <NoData msg="No resolved incidents yet" />
            ) : (
              <ChartShell>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={snap!.mttr_trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fill: "#6b7280", fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fill: "#6b7280", fontSize: 10 }} unit="m" domain={[0, "auto"]} />
                    <Tooltip
                      {...TOOLTIP_STYLE}
                      labelFormatter={v => new Date(String(v) + "T00:00:00").toLocaleDateString()}
                      formatter={(v: number | string) => [`${Number(v).toFixed(1)} min`, "MTTR"]}
                    />
                    <ReferenceLine
                      y={5}
                      stroke="#ef4444"
                      strokeDasharray="4 4"
                      label={{ value: "5m target", fill: "#ef4444", fontSize: 10 }}
                    />
                    <Line type="monotone" dataKey="mttr" stroke="#6366f1" strokeWidth={2} dot={false} connectNulls={false} />
                  </LineChart>
                </ResponsiveContainer>
              </ChartShell>
            )}
          </Section>

          {/* Failure patterns */}
          <Section title="Top failure patterns">
            {loading ? <Skeleton h="h-56" /> : !mounted ? <Skeleton h="h-56" /> : !hasPatterns ? (
              <NoData msg="No remediation data yet" />
            ) : (
              <ChartShell>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={snap!.failure_patterns} layout="vertical" margin={{ left: 8 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" horizontal={false} />
                    <XAxis type="number" tick={{ fill: "#6b7280", fontSize: 10 }} allowDecimals={false} />
                    <YAxis
                      type="category"
                      dataKey="action"
                      width={130}
                      tick={{ fill: "#9ca3af", fontSize: 10 }}
                      tickFormatter={v => String(v).replace(/_/g, " ")}
                    />
                    <Tooltip {...TOOLTIP_STYLE} formatter={(v: unknown) => [String(v), "incidents"]} />
                    <Bar dataKey="count" radius={[0, 3, 3, 0]}>
                      {snap!.failure_patterns.map((_, i) => (
                        <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </ChartShell>
            )}
          </Section>

          {/* Prediction accuracy */}
          <Section title="Prediction accuracy over time">
            {loading ? <Skeleton h="h-56" /> : !mounted ? <Skeleton h="h-56" /> : !hasPred ? (
              <NoData msg="No predictions recorded in this window" />
            ) : (
              <ChartShell>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={snap!.prediction_accuracy}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fill: "#6b7280", fontSize: 10 }} interval="preserveStartEnd" />
                    <YAxis tick={{ fill: "#6b7280", fontSize: 10 }} domain={[0, 1]} tickFormatter={v => `${(Number(v) * 100).toFixed(0)}%`} />
                    <Tooltip
                      {...TOOLTIP_STYLE}
                      labelFormatter={v => new Date(String(v) + "T00:00:00").toLocaleDateString()}
                      formatter={(v: number | string, name: string) => [
                        name === "precision" ? `${(Number(v) * 100).toFixed(1)}%` : String(v),
                        name,
                      ]}
                    />
                    <Line type="monotone" dataKey="precision" name="Precision" stroke="#10b981" strokeWidth={2} dot={false} connectNulls={false} />
                  </LineChart>
                </ResponsiveContainer>
              </ChartShell>
            )}
          </Section>
        </div>
      )}

      {/* ── 3. Per-service drill-down ── */}
      {!isEmpty && (
        <Section title="Services — click for drill-down">
          {loading ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} />)}
            </div>
          ) : (snap?.services ?? []).length === 0 ? (
            <p className="text-gray-600 text-sm">No service data available.</p>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {snap!.services.map((svc, i) => (
                <button
                  key={svc.name}
                  onClick={() => setSelectedSvc(svc)}
                  className="border border-gray-800 rounded-lg p-4 bg-gray-900 hover:border-gray-600 transition-colors text-left group"
                >
                  <div className="flex items-center gap-2 mb-2">
                    <span
                      className="inline-block w-2 h-2 rounded-full shrink-0"
                      style={{ background: PALETTE[i % PALETTE.length] }}
                    />
                    <span className="font-mono text-gray-200 text-sm truncate flex-1">{svc.name}</span>
                    <span className="text-xs text-gray-500">{svc.total}</span>
                  </div>
                  <div className="flex gap-4 text-xs text-gray-500">
                    <span>
                      <span className="text-green-400 font-medium">{svc.resolved}</span> resolved
                    </span>
                    <span>
                      MTTR: <span className="text-gray-300">{svc.avg_mttr > 0 ? fmtMin(svc.avg_mttr) : "—"}</span>
                    </span>
                  </div>
                  {svc.common_actions[0] && (
                    <p className="text-xs text-gray-600 mt-1.5 font-mono truncate group-hover:text-gray-500">
                      Top: {svc.common_actions[0].action.replace(/_/g, " ")}
                    </p>
                  )}
                </button>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* ── 4. Cost impact ── */}
      {!isEmpty && (
        <Section title="Cost impact">
          {loading ? (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              {Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} />)}
            </div>
          ) : (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <MetricCard
                label="Total cost delta"
                value={
                  snap!.cost.total_delta_usd >= 0
                    ? `+$${snap!.cost.total_delta_usd.toFixed(2)}/h`
                    : `-$${Math.abs(snap!.cost.total_delta_usd).toFixed(2)}/h`
                }
                sub="Estimated hourly impact of all remediations"
                accent={snap!.cost.total_delta_usd <= 0 ? "text-green-400" : "text-yellow-400"}
              />
              <MetricCard
                label="Remediations executed"
                value={String(snap!.cost.remediation_count)}
                sub="Plans approved and run"
              />
              <MetricCard
                label="Cost-optimised alternatives"
                value={String(snap!.cost.alternative_adopted_count)}
                sub="Times Gemini offered a cheaper option"
              />
              <MetricCard
                label="Adoption rate"
                value={fmtPct(snap!.cost.alternative_adoption_rate)}
                sub="Of cheaper plans actually selected"
                accent={snap!.cost.alternative_adoption_rate > 0.3 ? "text-green-400" : "text-gray-400"}
              />
            </div>
          )}
        </Section>
      )}

      {/* ── 5. Agent reasoning quality ── */}
      {!isEmpty && (
        <Section title="Agent reasoning quality">
          {loading ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <CardSkeleton />
              <Skeleton h="h-52" />
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <div className="border border-gray-800 rounded-lg p-5 bg-gray-900 space-y-3">
                <p className="text-xs text-gray-500 uppercase tracking-wide">
                  Avg reasoning steps / incident
                </p>
                <p className="text-5xl font-bold text-indigo-400">
                  {snap!.reasoning.avg_steps.toFixed(1)}
                </p>
                <p className="text-xs text-gray-500 max-w-xs">
                  4–8 steps is the target range: enough to diagnose thoroughly,
                  not so many that it signals a confused ReAct loop.
                </p>
                <div className="border-t border-gray-800 pt-3 grid grid-cols-3 text-xs text-gray-500 gap-2">
                  <div>
                    <p className="text-gray-300 font-medium">{snap!.summary.total_incidents}</p>
                    <p>incidents</p>
                  </div>
                  <div>
                    <p className="text-green-400 font-medium">{snap!.summary.resolved}</p>
                    <p>resolved</p>
                  </div>
                  <div>
                    <p className="text-gray-300 font-medium">{snap!.summary.rejected}</p>
                    <p>rejected</p>
                  </div>
                </div>
              </div>

              <div className="border border-gray-800 rounded-lg p-4 bg-gray-900">
                <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">
                  Tool call distribution
                </p>
                {!mounted || !hasTools ? (
                  <Skeleton h="h-44" />
                ) : (
                  <div className="h-44">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart
                        data={snap!.reasoning.tool_distribution.slice(0, 10)}
                        layout="vertical"
                        margin={{ left: 0 }}
                      >
                        <XAxis type="number" tick={{ fill: "#6b7280", fontSize: 10 }} allowDecimals={false} />
                        <YAxis
                          type="category"
                          dataKey="tool"
                          width={165}
                          tick={{ fill: "#9ca3af", fontSize: 10 }}
                          tickFormatter={v => String(v).replace(/_/g, " ")}
                        />
                        <Tooltip {...TOOLTIP_STYLE} formatter={(v: number | string) => [String(v), "calls"]} />
                        <Bar dataKey="count" fill="#4f46e5" radius={[0, 3, 3, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>
            </div>
          )}
        </Section>
      )}

      {/* ── Confidence Calibration ── */}
      {calib && (
        <Section title="Confidence Calibration">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="border border-gray-800 rounded-lg p-5 bg-gray-900 space-y-3">
              <p className="text-xs text-gray-500 uppercase tracking-wide">
                Expected Calibration Error (ECE)
              </p>
              <p className={`text-5xl font-bold ${
                calib.ece < 0.05 ? "text-green-400" :
                calib.ece < 0.10 ? "text-amber-400" : "text-red-400"
              }`}>
                {(calib.ece * 100).toFixed(1)}%
              </p>
              <p className="text-xs text-gray-400">{calib.interpretation}</p>
              <p className="text-xs text-gray-600">
                Over {calib.lookback_incidents} resolved/rejected incidents ·{" "}
                {new Date(calib.computed_at).toLocaleDateString()}
              </p>
            </div>

            <div className="border border-gray-800 rounded-lg p-4 bg-gray-900">
              <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">
                Accuracy vs. Stated Confidence
              </p>
              {!mounted ? (
                <Skeleton h="h-44" />
              ) : (
                <div className="h-44">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={calib.buckets.filter(b => b.count > 0)}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                      <XAxis dataKey="label" tick={{ fill: "#6b7280", fontSize: 10 }} />
                      <YAxis
                        domain={[0, 1]}
                        tickFormatter={v => `${Math.round((v as number) * 100)}%`}
                        tick={{ fill: "#6b7280", fontSize: 10 }}
                      />
                      <Tooltip
                        {...TOOLTIP_STYLE}
                        formatter={(v: number | string, name: string) => [
                          `${Math.round(Number(v) * 100)}%`, name,
                        ]}
                      />
                      <Legend wrapperStyle={{ fontSize: 11, color: "#9ca3af" }} />
                      <Bar dataKey="accuracy" name="Actual accuracy" fill="#10b981" radius={[3, 3, 0, 0]} />
                      <Bar dataKey="avg_confidence" name="Avg confidence" fill="#6366f1" radius={[3, 3, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </div>
        </Section>
      )}

      {/* ── Detection method distribution ── */}
      {sysHealth?.detection_distribution && (
        <Section title="Detection method distribution">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: "Webhook", value: sysHealth.detection_distribution.webhook, accent: "text-green-400", sub: "near-instant" },
                { label: "Polling", value: sysHealth.detection_distribution.polling, accent: "text-yellow-300", sub: "≤5 min fallback" },
                { label: "Unknown", value: sysHealth.detection_distribution.unknown, accent: "text-gray-400", sub: "pre-webhook era" },
              ].map(({ label, value, accent, sub }) => (
                <MetricCard key={label} label={label} value={String(value)} accent={accent} sub={sub} />
              ))}
              {sysHealth.time_to_detect?.avg_ms_webhook != null && sysHealth.time_to_detect?.avg_ms_polling != null && (
                <div className="col-span-3 border border-green-900 rounded-lg p-3 bg-green-950 text-xs text-green-300">
                  Webhook is{" "}
                  <strong>
                    {Math.round(sysHealth.time_to_detect.avg_ms_polling! / sysHealth.time_to_detect.avg_ms_webhook!)}×
                  </strong>{" "}
                  faster than polling ({Math.round(sysHealth.time_to_detect.avg_ms_webhook!)}ms vs{" "}
                  {Math.round(sysHealth.time_to_detect.avg_ms_polling!)}ms avg TTD).
                </div>
              )}
            </div>

            {mounted && sysHealth.detection_distribution.total > 0 && (
              <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 h-44">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={[
                      { method: "Webhook", count: sysHealth.detection_distribution.webhook, fill: "#10b981" },
                      { method: "Polling", count: sysHealth.detection_distribution.polling, fill: "#f59e0b" },
                      { method: "Unknown", count: sysHealth.detection_distribution.unknown, fill: "#6b7280" },
                    ].filter(d => d.count > 0)}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="method" tick={{ fill: "#9ca3af", fontSize: 11 }} />
                    <YAxis tick={{ fill: "#6b7280", fontSize: 10 }} allowDecimals={false} />
                    <Tooltip {...TOOLTIP_STYLE} formatter={(v: number | string) => [String(v), "incidents"]} />
                    <Bar dataKey="count" radius={[3, 3, 0, 0]}>
                      {[
                        { method: "Webhook", fill: "#10b981" },
                        { method: "Polling", fill: "#f59e0b" },
                        { method: "Unknown", fill: "#6b7280" },
                      ].filter((_, i) => [
                        sysHealth.detection_distribution!.webhook,
                        sysHealth.detection_distribution!.polling,
                        sysHealth.detection_distribution!.unknown,
                      ][i] > 0).map((entry, i) => (
                        <Cell key={i} fill={entry.fill} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
          <p className="text-xs text-gray-600 mt-2">
            <Link href="/system-health" className="text-blue-500 hover:text-blue-400">
              → Full webhook health dashboard
            </Link>
          </p>
        </Section>
      )}

      {/* Service drill-down modal */}
      {selectedSvc && (
        <ServiceModal svc={selectedSvc} onClose={() => setSelectedSvc(null)} />
      )}
    </div>
  );
}
