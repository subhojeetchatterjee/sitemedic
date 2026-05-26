"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Cell,
  PieChart,
  Pie,
  Legend,
  ReferenceLine,
} from "recharts";

// ── Types ─────────────────────────────────────────────────────────────────

interface ProbeRecord {
  probe_id: string;
  checked_at: string;
  latency_ms: number;
  success: boolean;
  status_code: number;
  error?: string | null;
  sla_breach: boolean;
}

interface ProbeStats {
  p95_latency_ms: number | null;
  avg_latency_ms: number | null;
  recent_successes: number;
  total_probes: number;
  sla_target_ms: number;
}

interface DetectionDist {
  webhook: number;
  polling: number;
  unknown: number;
  total: number;
}

interface TimeToDetect {
  avg_ms_all: number | null;
  avg_ms_webhook: number | null;
  avg_ms_polling: number | null;
}

interface WebhookFailure {
  problem_id: string;
  error: string;
  retry_count: number;
  dead_lettered: boolean;
  last_attempt_at: string;
  title?: string;
}

interface SystemHealth {
  health_probes: ProbeRecord[];
  webhook_failures: WebhookFailure[];
  probe_stats: ProbeStats;
  detection_distribution: DetectionDist;
  time_to_detect: TimeToDetect;
}

// ── Helpers ───────────────────────────────────────────────────────────────

function fmtMs(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString();
}

// ── Sub-components ────────────────────────────────────────────────────────

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

function ProbeRow({ probe }: { probe: ProbeRecord }) {
  const statusCls = probe.success
    ? probe.sla_breach ? "text-amber-300" : "text-green-300"
    : "text-red-300";
  return (
    <div className="flex items-center justify-between gap-4 py-2 border-b border-gray-800 last:border-0 text-sm">
      <span className="text-gray-500 text-xs w-20 shrink-0">
        {fmtTime(probe.checked_at)}
      </span>
      <span className={`font-mono text-xs shrink-0 ${statusCls}`}>
        {probe.success ? (probe.sla_breach ? "⚠ SLOW" : "✓ OK") : "✗ FAIL"}
      </span>
      <span className={`font-mono text-xs shrink-0 ${
        probe.sla_breach ? "text-amber-300" : "text-gray-300"
      }`}>
        {fmtMs(probe.latency_ms)}
      </span>
      {probe.error && (
        <span className="text-red-400 text-xs truncate">{probe.error}</span>
      )}
    </div>
  );
}

function FailureRow({ f }: { f: WebhookFailure }) {
  return (
    <div className={`border rounded-lg p-3 text-xs space-y-1 ${
      f.dead_lettered
        ? "border-red-800 bg-red-950"
        : "border-yellow-800 bg-yellow-950"
    }`}>
      <div className="flex items-center justify-between">
        <span className="font-mono text-gray-300">{f.problem_id}</span>
        <span className={`px-1.5 py-0.5 rounded font-medium ${
          f.dead_lettered
            ? "bg-red-900 text-red-300"
            : "bg-yellow-900 text-yellow-300"
        }`}>
          {f.dead_lettered ? "dead-lettered" : `retry ${f.retry_count}`}
        </span>
      </div>
      {f.title && <p className="text-gray-400">{f.title}</p>}
      <p className="text-gray-500">{f.error}</p>
      <p className="text-gray-600">{new Date(f.last_attempt_at).toLocaleString()}</p>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────

export default function SystemHealthPage() {
  const [data, setData] = useState<SystemHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [mounted, setMounted] = useState(false);

  useEffect(() => { setMounted(true); }, []);

  useEffect(() => {
    function load() {
      fetch("/api/system-health")
        .then(r => r.json())
        .then(d => { setData(d); setLoading(false); })
        .catch(() => setLoading(false));
    }
    load();
    const iv = setInterval(load, 60_000);
    return () => clearInterval(iv);
  }, []);

  const latencyChartData = (data?.health_probes ?? [])
    .slice(0, 12)
    .reverse()
    .map(p => ({
      time: fmtTime(p.checked_at),
      latency: p.latency_ms,
      ok: p.success,
    }));

  const distData = data?.detection_distribution
    ? [
        { name: "Webhook", value: data.detection_distribution.webhook, fill: "#10b981" },
        { name: "Polling", value: data.detection_distribution.polling, fill: "#f59e0b" },
        { name: "Unknown", value: data.detection_distribution.unknown, fill: "#6b7280" },
      ].filter(d => d.value > 0)
    : [];

  const stats = data?.probe_stats;
  const ttd = data?.time_to_detect;
  const dist = data?.detection_distribution;

  return (
    <div className="max-w-4xl space-y-8">
      {/* Header */}
      <div>
        <Link href="/" className="text-sm text-gray-500 hover:text-gray-300 mb-2 inline-block">
          ← Dashboard
        </Link>
        <h1 className="text-xl font-bold text-gray-100">System Health</h1>
        <p className="text-sm text-gray-500 mt-1">
          Webhook delivery reliability, detection latency, and dead-letter queue.
          Refreshes every 60 seconds.
        </p>
      </div>

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-20 rounded-lg bg-gray-800 animate-pulse" />
          ))}
        </div>
      ) : (
        <>
          {/* Probe stats */}
          <section>
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
              Webhook Health Probes (15-min interval, SLA: {stats?.sla_target_ms ?? 500}ms p95)
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
              <MetricCard
                label="p95 Latency"
                value={fmtMs(stats?.p95_latency_ms ?? null)}
                accent={
                  stats?.p95_latency_ms == null ? "text-gray-400"
                  : stats.p95_latency_ms > (stats?.sla_target_ms ?? 500) ? "text-red-400"
                  : stats.p95_latency_ms > 300 ? "text-amber-300"
                  : "text-green-400"
                }
                sub="probe round-trip"
              />
              <MetricCard
                label="Avg Latency"
                value={fmtMs(stats?.avg_latency_ms ?? null)}
                sub="all probes"
              />
              <MetricCard
                label="Recent Success"
                value={stats ? `${stats.recent_successes}/5` : "—"}
                accent={
                  (stats?.recent_successes ?? 0) >= 5 ? "text-green-400"
                  : (stats?.recent_successes ?? 0) >= 3 ? "text-amber-300"
                  : "text-red-400"
                }
                sub="last 5 probes"
              />
              <MetricCard
                label="Total Probes"
                value={String(stats?.total_probes ?? "—")}
                sub="stored"
              />
            </div>

            {/* Latency sparkline */}
            {mounted && latencyChartData.length > 0 && (
              <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 h-44">
                <p className="text-xs text-gray-500 mb-2">Probe latency (ms) — last {latencyChartData.length} checks</p>
                <ResponsiveContainer width="100%" height="85%">
                  <BarChart data={latencyChartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#374151" vertical={false} />
                    <XAxis dataKey="time" tick={{ fontSize: 10, fill: "#6b7280" }} />
                    <YAxis tick={{ fontSize: 10, fill: "#6b7280" }} unit="ms" width={45} />
                    <Tooltip
                      contentStyle={{ background: "#111827", border: "1px solid #374151" }}
                      formatter={(v: number | string) => [`${v}ms`, "latency"]}
                    />
                    <ReferenceLine y={500} stroke="#ef4444" strokeDasharray="4 4" label={{ value: "SLA 500ms", fill: "#ef4444", fontSize: 10 }} />
                    <Bar dataKey="latency" radius={[2, 2, 0, 0]}>
                      {latencyChartData.map((entry, i) => (
                        <Cell
                          key={i}
                          fill={!entry.ok ? "#ef4444" : entry.latency > 500 ? "#f59e0b" : "#10b981"}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}

            {/* Probe log */}
            {(data?.health_probes ?? []).length > 0 && (
              <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 mt-3">
                <p className="text-xs text-gray-500 font-medium mb-2">Recent probe results</p>
                {data!.health_probes.slice(0, 8).map(p => (
                  <ProbeRow key={p.probe_id} probe={p} />
                ))}
              </div>
            )}
          </section>

          {/* Detection distribution */}
          <section>
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
              Detection Method Distribution
            </h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="grid grid-cols-3 gap-3">
                <MetricCard
                  label="Webhook"
                  value={String(dist?.webhook ?? "—")}
                  accent="text-green-400"
                  sub="near-instant"
                />
                <MetricCard
                  label="Polling"
                  value={String(dist?.polling ?? "—")}
                  accent="text-yellow-300"
                  sub="fallback"
                />
                <MetricCard
                  label="Unknown"
                  value={String(dist?.unknown ?? "—")}
                  accent="text-gray-400"
                  sub="pre-webhook"
                />
              </div>

              {mounted && distData.length > 0 && (
                <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 h-44">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={distData}
                        dataKey="value"
                        nameKey="name"
                        cx="50%"
                        cy="50%"
                        outerRadius={60}
                        label={({ name, percent }) =>
                          `${name} ${(percent * 100).toFixed(0)}%`
                        }
                        labelLine={false}
                      >
                        {distData.map((entry, i) => (
                          <Cell key={i} fill={entry.fill} />
                        ))}
                      </Pie>
                      <Tooltip
                        contentStyle={{ background: "#111827", border: "1px solid #374151" }}
                        formatter={(v: number | string) => [String(v), "incidents"]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          </section>

          {/* Time-to-detect comparison */}
          <section>
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
              Average Time-to-Detect
            </h2>
            <div className="grid grid-cols-3 gap-3">
              <MetricCard
                label="All methods"
                value={fmtMs(ttd?.avg_ms_all ?? null)}
                sub="mean TTD"
              />
              <MetricCard
                label="Webhook"
                value={fmtMs(ttd?.avg_ms_webhook ?? null)}
                accent="text-green-400"
                sub="from DT timestamp"
              />
              <MetricCard
                label="Polling fallback"
                value={fmtMs(ttd?.avg_ms_polling ?? null)}
                accent="text-yellow-300"
                sub="up to 5 min delay"
              />
            </div>
            {ttd?.avg_ms_webhook != null && ttd?.avg_ms_polling != null && (
              <div className="mt-3 border border-green-900 rounded-lg p-3 bg-green-950 text-xs text-green-300">
                Webhook detection is{" "}
                <strong>
                  {Math.round(ttd.avg_ms_polling / ttd.avg_ms_webhook)}×
                </strong>{" "}
                faster than polling on average.
              </div>
            )}
          </section>

          {/* Dead-letter queue */}
          <section>
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
              Webhook Failures / Dead-Letter Queue
            </h2>
            {(data?.webhook_failures ?? []).length === 0 ? (
              <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 text-center text-sm text-gray-500">
                No webhook failures. All clear.
              </div>
            ) : (
              <div className="space-y-2">
                {data!.webhook_failures.map(f => (
                  <FailureRow key={f.problem_id} f={f} />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
