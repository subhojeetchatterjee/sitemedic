"use client";

import { useEffect, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// ── Types ─────────────────────────────────────────────────────────────────

interface CostSnapshot {
  problem_id: string;
  service: string;
  status: string;
  resolved: boolean;
  burn_rate_per_min: number | null;
  cumulative_usd: number | null;
  remediation_hourly_usd: number;
  break_even_minutes: number | null;
  current_rps: number;
  error_rate: number;
  duration_minutes: number;
  revenue_configured: boolean;
  criticality_multiplier: number;
  sampled_at: string;
  error?: string;
}

interface SparkPoint {
  t: number;          // seconds since incident start
  burn: number;       // burn_rate_per_min at this sample
  cumulative: number; // cumulative cost at this sample
}

// ── Formatting ─────────────────────────────────────────────────────────────

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtUsd(n: number) {
  return usd.format(n);
}

function fmtRate(n: number) {
  if (n < 0.01) return `${(n * 100).toFixed(3)}¢/min`;
  return `${usd.format(n)}/min`;
}

// ── Animated counter ───────────────────────────────────────────────────────

function useAnimatedValue(target: number, durationMs = 800) {
  const [display, setDisplay] = useState(target);
  const frameRef = useRef<number | null>(null);
  const startRef = useRef<{ from: number; to: number; start: number } | null>(null);

  useEffect(() => {
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    const from = display;
    startRef.current = { from, to: target, start: performance.now() };

    function tick(now: number) {
      const s = startRef.current!;
      const elapsed = now - s.start;
      const t = Math.min(elapsed / durationMs, 1);
      // ease-out cubic
      const ease = 1 - Math.pow(1 - t, 3);
      setDisplay(s.from + (s.to - s.from) * ease);
      if (t < 1) {
        frameRef.current = requestAnimationFrame(tick);
      } else {
        setDisplay(s.to);
        frameRef.current = null;
      }
    }
    frameRef.current = requestAnimationFrame(tick);
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  return display;
}

// ── Break-even label ───────────────────────────────────────────────────────

function BreakEvenBadge({
  breakEvenMinutes,
  durationMinutes,
}: {
  breakEvenMinutes: number | null;
  durationMinutes: number;
}) {
  if (breakEvenMinutes === null) return null;

  if (breakEvenMinutes <= 0) {
    return (
      <span className="text-xs bg-green-900 text-green-300 px-2 py-0.5 rounded-full">
        Remediation already paid off
      </span>
    );
  }

  const minutesUntil = breakEvenMinutes - durationMinutes;
  if (minutesUntil <= 0) {
    return (
      <span className="text-xs bg-green-900 text-green-300 px-2 py-0.5 rounded-full">
        Break-even passed — act now
      </span>
    );
  }

  return (
    <span className="text-xs bg-amber-900 text-amber-300 px-2 py-0.5 rounded-full">
      Break-even in {minutesUntil.toFixed(1)} min
    </span>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

export default function CostImpactPanel({ incidentId }: { incidentId: string }) {
  const [snap, setSnap] = useState<CostSnapshot | null>(null);
  const [history, setHistory] = useState<SparkPoint[]>([]);
  const [liveUsd, setLiveUsd] = useState(0);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const snapRef = useRef<CostSnapshot | null>(null);
  const liveRef = useRef(0);

  // ── SSE connection with exponential-backoff reconnect ───────────────────
  useEffect(() => {
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelay = 1000;
    let destroyed = false;

    function connect() {
      if (destroyed) return;
      es = new EventSource(`/api/incidents/${incidentId}/cost-stream`);

      es.onmessage = (e) => {
        retryDelay = 1000; // reset backoff on successful message
        try {
          const data: CostSnapshot = JSON.parse(e.data);
          if (data.error) return;
          setSnap(data);
          snapRef.current = data;

          const burn = data.burn_rate_per_min ?? 0;
          const cum = data.cumulative_usd ?? 0;
          setHistory(h => {
            const next: SparkPoint = {
              t: Math.round(data.duration_minutes * 10) / 10,
              burn,
              cumulative: cum,
            };
            return [...h.slice(-29), next];
          });

          liveRef.current = cum;
          setLiveUsd(cum);

          if (data.resolved) {
            es?.close();
            es = null;
          }
        } catch {
          // malformed event — ignore
        }
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
  }, [incidentId]);

  // ── 1-second tick: interpolate cumulative between SSE events ───────────
  useEffect(() => {
    tickRef.current = setInterval(() => {
      const s = snapRef.current;
      if (!s || s.resolved || s.burn_rate_per_min === null) return;
      // Add 1 second worth of burn
      const delta = s.burn_rate_per_min / 60;
      liveRef.current = liveRef.current + delta;
      setLiveUsd(liveRef.current);
    }, 1000);

    return () => {
      if (tickRef.current) clearInterval(tickRef.current);
    };
  }, []);

  const animatedUsd = useAnimatedValue(liveUsd, 600);

  // ── No data yet ─────────────────────────────────────────────────────────
  if (!snap) {
    return (
      <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 space-y-3">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Cost Impact
        </h3>
        <div className="h-2 bg-gray-800 rounded animate-pulse" />
        <div className="h-8 w-24 bg-gray-800 rounded animate-pulse" />
      </div>
    );
  }

  // ── Revenue not configured ───────────────────────────────────────────────
  if (!snap.revenue_configured) {
    return (
      <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 space-y-3">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Cost Impact
        </h3>
        {snap.remediation_hourly_usd > 0 && (
          <div>
            <p className="text-xs text-gray-500 mb-1">Remediation overhead</p>
            <p className="text-lg font-bold font-mono text-yellow-400">
              {usd.format(snap.remediation_hourly_usd)}/hr
            </p>
          </div>
        )}
        <a
          href="/settings/cost"
          className="block text-center text-xs text-blue-400 hover:text-blue-300 border border-blue-900 rounded px-3 py-2"
        >
          Configure revenue impact →
        </a>
      </div>
    );
  }

  const isIncreasing = !snap.resolved && (snap.burn_rate_per_min ?? 0) > 0;
  const burnColor = snap.resolved ? "text-green-400" : isIncreasing ? "text-red-400" : "text-gray-300";
  const areaColor = snap.resolved ? "#10b981" : "#ef4444";

  return (
    <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Cost Impact
        </h3>
        {snap.resolved ? (
          <span className="text-xs bg-green-900 text-green-300 px-2 py-0.5 rounded-full">
            Resolved
          </span>
        ) : (
          <span className="flex items-center gap-1 text-xs text-red-400">
            <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
            Live
          </span>
        )}
      </div>

      {/* Cumulative big number */}
      <div>
        <p className="text-xs text-gray-500 mb-1">Cumulative incident cost</p>
        <p className={`text-3xl font-bold font-mono tabular-nums ${burnColor}`}>
          {fmtUsd(animatedUsd)}
        </p>
        <p className="text-xs text-gray-600 mt-0.5">
          {snap.duration_minutes.toFixed(1)} min elapsed
        </p>
      </div>

      {/* Burn rate + remediation */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <p className="text-xs text-gray-500 mb-1">Burn rate</p>
          <p className={`text-sm font-semibold font-mono ${burnColor}`}>
            {snap.burn_rate_per_min !== null
              ? fmtRate(snap.burn_rate_per_min)
              : "—"}
          </p>
          <p className="text-xs text-gray-600">
            {(snap.error_rate * 100).toFixed(1)}% error · {snap.current_rps.toFixed(1)} rps
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500 mb-1">Remediation cost</p>
          <p className="text-sm font-semibold font-mono text-yellow-400">
            {snap.remediation_hourly_usd > 0
              ? `${usd.format(snap.remediation_hourly_usd)}/hr`
              : "—"}
          </p>
          {snap.criticality_multiplier !== 1 && (
            <p className="text-xs text-gray-600">
              ×{snap.criticality_multiplier} criticality
            </p>
          )}
        </div>
      </div>

      {/* Break-even */}
      {snap.break_even_minutes !== null && (
        <BreakEvenBadge
          breakEvenMinutes={snap.break_even_minutes}
          durationMinutes={snap.duration_minutes}
        />
      )}

      {/* Sparkline — last 30 samples */}
      {history.length >= 2 && (
        <div className="pt-1">
          <p className="text-xs text-gray-600 mb-1">Cumulative cost (last 30 samples)</p>
          <div className="h-16">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={history} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="costGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={areaColor} stopOpacity={0.25} />
                    <stop offset="95%" stopColor={areaColor} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="t" hide />
                <YAxis hide domain={["auto", "auto"]} />
                <Tooltip
                  contentStyle={{
                    background: "#111827",
                    border: "1px solid #1f2937",
                    borderRadius: 6,
                    fontSize: 11,
                  }}
                  formatter={(v: number | string) => [fmtUsd(Number(v)), "Cumulative"]}
                  labelFormatter={(v: number | string) => `${Number(v).toFixed(1)} min`}
                />
                {/* Break-even reference line on the time axis */}
                {snap.break_even_minutes !== null && (
                  <ReferenceLine
                    x={snap.break_even_minutes}
                    stroke="#f59e0b"
                    strokeDasharray="3 3"
                    label={{ value: "break-even", fill: "#f59e0b", fontSize: 9, position: "top" }}
                  />
                )}
                <Area
                  type="monotone"
                  dataKey="cumulative"
                  stroke={areaColor}
                  strokeWidth={1.5}
                  fill="url(#costGrad)"
                  dot={false}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}
