"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

const AGENT_URL = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8080";
const API_KEY   = process.env.NEXT_PUBLIC_AGENT_API_KEY ?? "";

// ── Types ─────────────────────────────────────────────────────────────────

interface ServiceSettings {
  service: string;
  revenue_per_request_usd: number | null;
  criticality_multiplier: number;
  updated_at?: string;
}

const KNOWN_SERVICES = ["demo-app", "auth-service", "payment-api", "notification-worker"];

const DEFAULT_SETTINGS: Omit<ServiceSettings, "service"> = {
  revenue_per_request_usd: null,
  criticality_multiplier: 1.0,
};

// ── Row editor ─────────────────────────────────────────────────────────────

function ServiceRow({
  service,
  initial,
  onSaved,
}: {
  service: string;
  initial: Omit<ServiceSettings, "service">;
  onSaved: (s: string, v: Omit<ServiceSettings, "service">) => void;
}) {
  const [revenue, setRevenue] = useState(
    initial.revenue_per_request_usd != null
      ? String(initial.revenue_per_request_usd)
      : "",
  );
  const [criticality, setCriticality] = useState(String(initial.criticality_multiplier));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = {
        criticality_multiplier: parseFloat(criticality) || 1.0,
      };
      if (revenue.trim() !== "") {
        body.revenue_per_request_usd = parseFloat(revenue);
      } else {
        body.revenue_per_request_usd = null;
      }

      const res = await fetch(`${AGENT_URL}/api/cost-settings/${service}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": API_KEY,
        },
        body: JSON.stringify(body),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onSaved(service, body as Omit<ServiceSettings, "service">);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="border border-gray-800 rounded-lg p-4 bg-gray-900 space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-mono text-gray-200">{service}</p>
        <button
          onClick={save}
          disabled={saving}
          className="text-xs px-3 py-1 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50 text-white font-medium"
        >
          {saving ? "Saving…" : saved ? "Saved ✓" : "Save"}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Revenue per successful request (USD)
          </label>
          <div className="relative">
            <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-500 text-xs">$</span>
            <input
              type="number"
              step="0.001"
              min="0"
              placeholder="0.01"
              value={revenue}
              onChange={e => setRevenue(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 pl-5 text-sm text-gray-200 focus:outline-none focus:border-blue-600"
            />
          </div>
          <p className="text-xs text-gray-600 mt-1">
            Leave blank to disable revenue tracking for this service.
          </p>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Criticality multiplier
          </label>
          <input
            type="number"
            step="0.1"
            min="0.1"
            max="10"
            value={criticality}
            onChange={e => setCriticality(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-600"
          />
          <p className="text-xs text-gray-600 mt-1">
            1.0 = normal. Use higher values for business-critical services.
          </p>
        </div>
      </div>

      {err && <p className="text-xs text-red-400">{err}</p>}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────

export default function CostSettingsPage() {
  const [settings, setSettings] = useState<Record<string, Omit<ServiceSettings, "service">>>({});
  const [loading, setLoading] = useState(true);
  const [addService, setAddService] = useState("");
  const [alwaysDryRun, setAlwaysDryRun] = useState(false);
  const [dryRunSaving, setDryRunSaving] = useState(false);

  async function toggleDryRun(value: boolean) {
    setAlwaysDryRun(value);
    setDryRunSaving(true);
    try {
      await fetch("/api/settings/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ always_dry_run: value }),
      });
    } finally {
      setDryRunSaving(false);
    }
  }

  useEffect(() => {
    fetch("/api/settings/global")
      .then(r => r.json())
      .then(d => d?.always_dry_run && setAlwaysDryRun(true))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetch("/api/cost-settings")
      .then(r => r.json())
      .then((rows: ServiceSettings[]) => {
        const map: Record<string, Omit<ServiceSettings, "service">> = {};
        for (const row of rows) {
          map[row.service] = {
            revenue_per_request_usd: row.revenue_per_request_usd ?? null,
            criticality_multiplier: row.criticality_multiplier ?? 1.0,
          };
        }
        setSettings(map);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const allServices = Array.from(
    new Set([...KNOWN_SERVICES, ...Object.keys(settings)]),
  ).sort();

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <Link
          href="/"
          className="text-sm text-gray-500 hover:text-gray-300 mb-2 inline-block"
        >
          ← Dashboard
        </Link>
        <h1 className="text-xl font-bold text-gray-100">Cost Settings</h1>
        <p className="text-sm text-gray-500 mt-1">
          Configure revenue-per-request so the cost impact panel can estimate
          the business cost of incidents in real time.
        </p>
      </div>

      <div className="border border-blue-900 rounded-lg p-4 bg-blue-950 text-xs text-blue-300 space-y-1">
        <p className="font-semibold">How the burn rate is calculated</p>
        <p>
          <span className="font-mono">burn_rate = affected_rps × 60 × revenue_per_request × criticality</span>
        </p>
        <p>
          <span className="font-mono">affected_rps = current_rps × error_rate</span>
          {" "}— pulled from Cloud Monitoring every 10 seconds.
        </p>
      </div>

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-24 rounded-lg bg-gray-800 animate-pulse" />
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {allServices.map(svc => (
            <ServiceRow
              key={svc}
              service={svc}
              initial={settings[svc] ?? DEFAULT_SETTINGS}
              onSaved={(s, v) => setSettings(prev => ({ ...prev, [s]: v }))}
            />
          ))}
        </div>
      )}

      {/* Add a custom service */}
      <div className="border border-dashed border-gray-700 rounded-lg p-4">
        <p className="text-xs text-gray-500 mb-2">Add a service not listed above</p>
        <div className="flex gap-2">
          <input
            type="text"
            placeholder="my-service-name"
            value={addService}
            onChange={e => setAddService(e.target.value)}
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-600 font-mono"
          />
          <button
            disabled={!addService.trim()}
            onClick={() => {
              const svc = addService.trim();
              if (svc && !allServices.includes(svc)) {
                setSettings(prev => ({ ...prev, [svc]: DEFAULT_SETTINGS }));
              }
              setAddService("");
            }}
            className="text-sm px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-gray-200"
          >
            Add
          </button>
        </div>
      </div>
    </div>
  );
}
