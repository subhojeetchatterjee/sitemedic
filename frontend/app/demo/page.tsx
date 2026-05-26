"use client";

import { useCallback, useEffect, useState } from "react";

interface ScenarioMeta {
  id: string;
  display_name: string;
  category: string;
  duration_seconds: number;
  description: string;
  expected_action: string;
}

interface DemoStatus {
  mode: string;
  source_type: string;
  is_live: boolean;
  health_status: string;
  demo_mode_active: boolean;
  current_scenario: string | null;
  scenarios_available: number;
  active_scenarios?: number;
  scheduler_paused?: boolean;
  speed?: number;
  initialised: boolean;
}

const CATEGORY_LABELS: Record<string, string> = {
  resource_exhaustion: "Resource",
  bad_deploy: "Bad Deploy",
  performance: "Performance",
  error_rate: "Error Rate",
  cascading: "Cascading",
  predictive: "Predictive",
};

const CATEGORY_COLORS: Record<string, string> = {
  resource_exhaustion: "bg-orange-900 text-orange-300",
  bad_deploy: "bg-red-900 text-red-300",
  performance: "bg-yellow-900 text-yellow-300",
  error_rate: "bg-red-900 text-red-300",
  cascading: "bg-purple-900 text-purple-300",
  predictive: "bg-blue-900 text-blue-300",
};

const ACTION_COLORS: Record<string, string> = {
  restart_service: "bg-amber-900 text-amber-300",
  rollback_revision: "bg-red-900 text-red-300",
  scale_service: "bg-teal-900 text-teal-300",
};

export default function DemoPage() {
  const [scenarios, setScenarios] = useState<ScenarioMeta[]>([]);
  const [status, setStatus] = useState<DemoStatus | null>(null);
  const [triggering, setTriggering] = useState<string | null>(null);
  const [lastTriggered, setLastTriggered] = useState<{ id: string; problemId: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [schedulerBusy, setSchedulerBusy] = useState(false);

  const fetchAll = useCallback(async () => {
    const [scenariosRes, statusRes] = await Promise.allSettled([
      fetch("/api/demo/scenarios").then((r) => r.json()),
      fetch("/api/demo/status").then((r) => r.json()),
    ]);

    if (scenariosRes.status === "fulfilled") {
      const data = scenariosRes.value;
      // Handle both old (mcp_calls list) and new (INDEX.json) format
      const normalized: ScenarioMeta[] = Array.isArray(data)
        ? data.map((s: Record<string, unknown>) => ({
            id: String(s.id || s.name || ""),
            display_name: String(s.display_name || s.name || ""),
            category: String(s.category || ""),
            duration_seconds: Number(s.duration_seconds || 0),
            description: String(s.description || ""),
            expected_action: String(s.expected_action || ""),
          }))
        : [];
      setScenarios(normalized);
    }

    if (statusRes.status === "fulfilled") {
      setStatus(statusRes.value);
    }

    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
    const timer = setInterval(fetchAll, 15_000);
    return () => clearInterval(timer);
  }, [fetchAll]);

  async function triggerScenario(scenarioId: string) {
    setTriggering(scenarioId);
    setError(null);
    try {
      const res = await fetch("/api/demo/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario: scenarioId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || "Trigger failed");
      setLastTriggered({ id: scenarioId, problemId: data.problem_id || "" });
      await fetchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setTriggering(null);
    }
  }

  async function toggleScheduler() {
    if (!status) return;
    setSchedulerBusy(true);
    const action = status.scheduler_paused ? "resume" : "pause";
    try {
      await fetch(`/api/demo/scheduler/${action}`, { method: "POST" });
      await fetchAll();
    } catch {
      setError(`Failed to ${action} scheduler`);
    } finally {
      setSchedulerBusy(false);
    }
  }

  async function setSpeed(speed: number) {
    try {
      await fetch("/api/demo/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ set_speed: speed }),
      });
      await fetchAll();
    } catch {
      setError("Failed to set speed");
    }
  }

  async function triggerRandom() {
    setTriggering("__random__");
    setError(null);
    try {
      const res = await fetch("/api/demo/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ random: true }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || "Trigger failed");
      setLastTriggered({ id: data.scenario || "random", problemId: data.problem_id || "" });
      await fetchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setTriggering(null);
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-100">Demo Control Panel</h1>
        <p className="mt-1 text-sm text-gray-400">
          Trigger pre-recorded incident scenarios. Gemini will reason against the
          recorded Dynatrace telemetry and propose a real remediation plan.
        </p>
      </div>

      {/* Status card */}
      {status && (
        <div className={`rounded-lg border p-4 ${
          status.demo_mode_active
            ? "border-amber-800 bg-amber-950/30"
            : "border-green-800 bg-green-950/30"
        }`}>
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${
                status.demo_mode_active ? "bg-amber-400" : "bg-green-400 animate-pulse"
              }`} />
              <span className="font-semibold text-sm text-gray-100">
                {status.demo_mode_active ? "Demo mode active" : "Live monitoring active"}
              </span>
            </div>
            <div className="flex gap-4 text-xs text-gray-400">
              <span>Source: <span className="text-gray-200 font-mono">{status.source_type}</span></span>
              <span>Scenarios: <span className="text-gray-200">{status.scenarios_available}</span></span>
              {status.active_scenarios !== undefined && (
                <span>Running: <span className="text-gray-200">{status.active_scenarios}</span></span>
              )}
              {status.current_scenario && (
                <span>Current: <span className="text-gray-200 font-mono">{status.current_scenario}</span></span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="bg-red-950 border border-red-700 rounded-lg p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Success */}
      {lastTriggered && (
        <div className="bg-green-950 border border-green-700 rounded-lg p-3 text-sm text-green-300 flex items-center justify-between">
          <span>
            Scenario <span className="font-mono">{lastTriggered.id}</span> triggered.
            {lastTriggered.problemId && (
              <> Problem ID: <span className="font-mono">{lastTriggered.problemId}</span></>
            )}
          </span>
          <a href="/" className="text-green-400 hover:text-green-200 underline ml-4">
            View incidents →
          </a>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={triggerRandom}
          disabled={triggering !== null || loading}
          className="px-4 py-2 bg-amber-700 hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm rounded-lg transition-colors"
        >
          {triggering === "__random__" ? "Triggering…" : "Trigger random scenario"}
        </button>

        {/* Scheduler pause/resume */}
        {status?.demo_mode_active && (
          <button
            onClick={toggleScheduler}
            disabled={schedulerBusy}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed text-gray-200 text-sm rounded-lg transition-colors"
          >
            {schedulerBusy ? "…" : status.scheduler_paused ? "Resume scheduler" : "Pause scheduler"}
          </button>
        )}

        {/* Speed selector */}
        {status?.demo_mode_active && (
          <div className="flex items-center gap-1 bg-gray-900 border border-gray-700 rounded-lg px-2 py-1">
            <span className="text-xs text-gray-500 mr-1">Speed:</span>
            {[1, 2, 5].map((s) => (
              <button
                key={s}
                onClick={() => setSpeed(s)}
                className={`text-xs px-2 py-0.5 rounded transition-colors ${
                  status.speed === s
                    ? "bg-amber-700 text-white"
                    : "text-gray-400 hover:text-gray-200"
                }`}
              >
                {s}x
              </button>
            ))}
          </div>
        )}

        <span className="text-xs text-gray-500">or choose a specific scenario below</span>
      </div>

      {/* Scenario grid */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="border border-gray-800 rounded-lg p-4 animate-pulse">
              <div className="h-4 bg-gray-800 rounded w-3/4 mb-2" />
              <div className="h-3 bg-gray-800 rounded w-full mb-1" />
              <div className="h-3 bg-gray-800 rounded w-5/6" />
            </div>
          ))}
        </div>
      ) : scenarios.length === 0 ? (
        <p className="text-gray-500 text-sm">No scenarios found.</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {scenarios.map((s) => (
            <div
              key={s.id}
              className="border border-gray-800 bg-gray-900 rounded-lg p-4 flex flex-col gap-3 hover:border-gray-700 transition-colors"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="font-medium text-gray-100 text-sm leading-tight">
                    {s.display_name || s.id}
                  </h3>
                  {s.category && (
                    <span className={`mt-1 inline-block text-xs px-1.5 py-0.5 rounded ${CATEGORY_COLORS[s.category] ?? "bg-gray-800 text-gray-400"}`}>
                      {CATEGORY_LABELS[s.category] ?? s.category}
                    </span>
                  )}
                </div>
                {s.duration_seconds > 0 && (
                  <span className="text-xs text-gray-500 shrink-0">
                    {Math.round(s.duration_seconds / 60)}m
                  </span>
                )}
              </div>

              {s.description && (
                <p className="text-xs text-gray-400 leading-relaxed line-clamp-3">
                  {s.description}
                </p>
              )}

              <div className="flex items-center justify-between gap-2 mt-auto pt-1">
                {s.expected_action && (
                  <span className={`text-xs px-2 py-0.5 rounded font-mono ${ACTION_COLORS[s.expected_action] ?? "bg-gray-800 text-gray-400"}`}>
                    {s.expected_action}
                  </span>
                )}
                <button
                  onClick={() => triggerScenario(s.id)}
                  disabled={triggering !== null}
                  className="ml-auto text-xs px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed text-gray-200 rounded transition-colors"
                >
                  {triggering === s.id ? "Triggering…" : "Trigger now"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="text-xs text-gray-600">
        Incidents appear in the{" "}
        <a href="/" className="underline hover:text-gray-400">main feed</a> within
        a few seconds of triggering. Gemini&apos;s reasoning trace streams in real time.
      </p>
    </div>
  );
}
