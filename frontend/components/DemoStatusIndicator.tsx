"use client";

import { useEffect, useRef, useState } from "react";

interface DemoStatus {
  mode: "demo" | "live" | "hybrid" | "unknown";
  source_type: string;
  is_live: boolean;
  health_status: string;
  demo_mode_active: boolean;
  current_scenario: string | null;
  scenarios_available: number;
  active_scenarios?: number;
  scheduler_paused?: boolean;
  speed?: number;
}

const POLL_INTERVAL_MS = 30_000;
const STATUS_KEY = "sitemedic_source_status";

function fetchStatus(): Promise<DemoStatus> {
  return fetch("/api/demo/status", { cache: "no-store" })
    .then((r) => r.json())
    .catch(() => null);
}

export default function DemoStatusIndicator() {
  const [status, setStatus] = useState<DemoStatus | null>(null);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Load cached status from localStorage while waiting for fetch
    try {
      const cached = localStorage.getItem(STATUS_KEY);
      if (cached) setStatus(JSON.parse(cached));
    } catch {}

    async function refresh() {
      const s = await fetchStatus();
      if (s) {
        setStatus(s);
        try {
          localStorage.setItem(STATUS_KEY, JSON.stringify(s));
        } catch {}
      }
    }

    refresh();
    const timer = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  // Close popover on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  if (!status) {
    return (
      <span className="text-xs text-gray-600 px-2 py-1 rounded-full border border-gray-700">
        ···
      </span>
    );
  }

  const isDemo = status.demo_mode_active || status.mode === "demo";

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border transition-colors ${
          isDemo
            ? "bg-amber-950 border-amber-700 text-amber-300 hover:border-amber-500"
            : "bg-green-950 border-green-800 text-green-400 hover:border-green-600"
        }`}
        title={isDemo ? "Demo mode active" : "Live monitoring active"}
      >
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${
            isDemo ? "bg-amber-400" : "bg-green-400 animate-pulse"
          }`}
        />
        {isDemo ? "Demo mode" : "Live"}
      </button>

      {open && (
        <div className="absolute right-0 top-8 z-50 w-72 bg-gray-900 border border-gray-700 rounded-lg shadow-xl p-4">
          <h4 className="text-sm font-semibold text-gray-100 mb-3">
            Telemetry Source
          </h4>
          <dl className="space-y-2 text-xs">
            <Row label="Mode" value={status.mode} />
            <Row label="Source" value={status.source_type} />
            <Row label="Health" value={status.health_status} />
            {isDemo && (
              <>
                <Row
                  label="Active scenario"
                  value={status.current_scenario ?? "none"}
                />
                <Row
                  label="Scenarios available"
                  value={String(status.scenarios_available)}
                />
                {status.active_scenarios !== undefined && (
                  <Row
                    label="Running"
                    value={`${status.active_scenarios} active`}
                  />
                )}
                {status.scheduler_paused !== undefined && (
                  <Row
                    label="Scheduler"
                    value={status.scheduler_paused ? "paused" : "running"}
                  />
                )}
              </>
            )}
          </dl>
          {isDemo && (
            <p className="mt-3 text-xs text-amber-400/80">
              Real Gemini reasoning is running against pre-recorded Dynatrace
              scenarios.
            </p>
          )}
          <a
            href="/demo"
            className="mt-3 block text-center text-xs text-gray-400 hover:text-gray-200 border border-gray-700 rounded py-1"
          >
            Open demo control panel →
          </a>
        </div>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2">
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-gray-300 font-mono truncate max-w-[160px]">{value}</dd>
    </div>
  );
}
